"""
states/tx/enrich.py — Texas Comptroller Taxable Entity Search scraper

Given a CSV of TX franchise entity names, looks each up in the Texas Comptroller's
Taxable Entity Search (the comptroller's office indexes franchise tax records,
which include officer/director info from filed Public Information Reports).

URLs:
    Search form:  https://comptroller.texas.gov/taxes/franchise/account-status/search
    Detail page:  https://comptroller.texas.gov/taxes/franchise/account-status/search/<taxpayer_number>

Anti-bot: none detected on initial recon (no Imperva, Cloudflare, Akamai signals).
Default headless mode; bump to --show only if blocking emerges in production.

Install (once):
    pip install playwright playwright-stealth
    playwright install chromium

Input CSV must have a column named `entity_name`.

Run (trial):
    python states/tx/enrich.py states/tx/input/franchisees.csv states/tx/output/enriched.csv --show --limit 5

Run (full batch, headless):
    python states/tx/enrich.py states/tx/input/franchisees.csv states/tx/output/enriched.csv

Re-derive intelligence columns without re-scraping:
    python states/tx/enrich.py states/tx/input/franchisees.csv states/tx/output/enriched.csv --rescore-only

Notes:
    * Resumable — rows where tx_fetched_at is filled are skipped.
    * Detail pages are reached via URL (no clicking needed once we have the taxpayer number).
    * On scrape errors, screenshot + HTML lands in states/tx/debug/.
    * Logging follows the skill convention: writes to states/tx/enrich.log.
"""
import argparse
import asyncio
import csv
import json
import random
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from playwright_stealth import Stealth

SEARCH_URL = "https://comptroller.texas.gov/taxes/franchise/account-status/search"
DETAIL_URL = "https://comptroller.texas.gov/taxes/franchise/account-status/search/{}"

OUT_OF_STATE_HINTS = re.compile(
    r"\b(colorado|dakot|rocky\s*mountain|new\s*england|california|florida|nevada|"
    r"arizona|oregon|washington|illinois|midwest|northwest|southeast|northeast|"
    r"hawaii|atlantic|rockies|northern|southern)\b",
    re.IGNORECASE,
)

PREFIX = "tx_"

UNIVERSAL = [
    "match_count", "first_result_name", "entity_number", "status", "entity_type",
    "formation_date", "jurisdiction", "agent_name", "agent_type", "agent_is_individual",
    "agent_address", "principal_address", "mailing_address",
    "statement_due_date", "inactive_date",
    "agent_address_matches_principal", "likely_owner_name", "operator_pattern",
    "multi_entity_operator", "query_used", "query_attempts", "name_similarity",
    "score", "alternates", "fetched_at", "error",
]
TX_EXTRAS = [
    "taxpayer_number",         # 11-digit TX Comptroller ID
    "sos_file_number",         # TX SOS file number (10-digit)
    "right_to_transact",       # ACTIVE / FORFEITED / etc.
    "sos_registration_status", # ACTIVE / etc.
    "zip",                     # zip from result row
]
COLS = [PREFIX + c for c in UNIVERSAL] + [PREFIX + c for c in TX_EXTRAS]


def blank_row():
    return {k: "" for k in COLS}


# ---------------------------------------------------------------------------
# Logger (skill convention)
# ---------------------------------------------------------------------------

_LOG_FILE = None

def _make_logger(log_path):
    global _LOG_FILE
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _LOG_FILE = open(log_path, "a", encoding="utf-8", buffering=1)

    def log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        _LOG_FILE.write(line + "\n")
        _LOG_FILE.flush()
    return log


# ---------------------------------------------------------------------------
# Name matching helpers
# ---------------------------------------------------------------------------

def normalize_for_similarity(name):
    s = (name or "").lower()
    s = re.sub(r"\([^)]*\)", " ", s)
    s = s.replace("&", " and ")
    s = re.sub(
        r"\b(llc|l\.l\.c\.?|inc|incorporated|limited|ltd|llp|l\.l\.p\.?|l\.p\.?|corp|corporation|company|co)\b\.?",
        " ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def name_similarity(a, b):
    na = set(normalize_for_similarity(a).split())
    nb = set(normalize_for_similarity(b).split())
    if not na or not nb:
        return 0.0
    return len(na & nb) / len(na | nb)


def name_variants(name):
    variants = []
    seen = {(name or "").lower()}
    if not name:
        return variants

    def add(v):
        v = v.strip().rstrip(",.")
        if v and v.lower() not in seen and len(v) >= 4:
            variants.append(v)
            seen.add(v.lower())

    no_paren = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()
    add(no_paren)

    suffix_pat = (
        r"\s*[,\.]?\s*\b(LLC|L\.L\.C\.?|Llc|Inc\.?|Incorporated|Limited|Ltd\.?|"
        r"Llp|L\.L\.P\.?|L\.P\.?|Corp\.?|Corporation|Company|Co\.?)\s*$"
    )
    base = re.sub(suffix_pat, "", no_paren, flags=re.IGNORECASE).strip().rstrip(",")
    add(base)

    SKIP = {"holdings", "group", "management", "ventures", "enterprises",
            "investments", "concepts", "operating", "operations", "the", "a", "an"}
    tokens = [t for t in normalize_for_similarity(name).split() if t not in SKIP]
    if len(tokens) >= 3:
        add(" ".join(tokens[:3]))
    if len(tokens) >= 2:
        add(" ".join(tokens[:2]))
    return variants


def normalize_address(addr):
    if not addr:
        return ""
    s = addr.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    for w in ("street", "avenue", "blvd", "boulevard", "drive", "lane", "road",
              "suite", "apt", "ste", "floor", "fl", "unit"):
        s = re.sub(rf"\b{w}\b\.?", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def addresses_match(a, b, threshold=0.6):
    na = set(normalize_address(a).split())
    nb = set(normalize_address(b).split())
    if not na or not nb:
        return False
    return (len(na & nb) / len(na | nb)) >= threshold


def score_candidate(input_name, candidate, input_implies_oos=None):
    """Score a TX result row candidate (only has name + taxpayer# + zip at this point)."""
    score = 0.0
    sim = name_similarity(input_name, candidate.get("name") or "")
    score += sim * 100
    score += 50  # all returned rows are real TX entities; baseline > zero
    return score


# ---------------------------------------------------------------------------
# Detail-page parsing
# ---------------------------------------------------------------------------

def parse_detail_page(text):
    """Detail page text from comptroller.texas.gov/.../search/<taxpayer_number>.
    Labels are followed by a newline and then the value; addresses span multiple lines
    until the next label. Stop at known boilerplate ("Public Information Report",
    "Right to Transact", etc.)."""
    out = {}

    # Known label terminators — stop multiline capture when we see any of these
    STOP_LABELS = (
        "Taxpayer Number", "Mailing Address", "Right to Transact",
        "State of Formation", "SOS Registration Status", "Effective SOS Registration",
        "Texas SOS File Number", "Registered Agent Name", "Registered Office",
        "Public Information Report", "Public Information Reports", "Officer",
        "Director", "Required Applications",
    )
    stop_pat = "|".join(re.escape(l) for l in STOP_LABELS)

    def field(label_pat):
        """Single-line field: label, then next non-blank line is the value."""
        m = re.search(rf"{label_pat}\s*\n+([^\n]+)", text)
        return m.group(1).strip() if m else ""

    def field_multiline(label_pat, max_lines=4):
        """Multi-line value: take up to max_lines until we hit a known stop label."""
        m = re.search(rf"{label_pat}\s*\n+", text)
        if not m:
            return ""
        rest = text[m.end():]
        lines = []
        for line in rest.split("\n"):
            line = line.strip()
            if not line:
                if lines:
                    break
                continue
            if re.match(rf"({stop_pat})", line, re.IGNORECASE):
                break
            lines.append(line)
            if len(lines) >= max_lines:
                break
        return ", ".join(lines)

    out["taxpayer_number"] = field(r"Taxpayer Number:")
    out["mailing_address"] = field_multiline(r"Mailing Address:")
    out["right_to_transact"] = field(r"Right to Transact Business in Texas:")
    out["jurisdiction"] = field(r"State of Formation:")
    # SOS Registration Status label is followed by a parenthetical "(SOS status updated each business day):"
    # then the value. Skip the parenthetical line.
    m = re.search(r"SOS Registration Status[^\n]*\n(?:\([^)]*\)[^\n]*\n)?\s*([^\n]+)", text)
    out["sos_registration_status"] = m.group(1).strip() if m else ""
    out["formation_date"] = field(r"Effective SOS Registration Date:")
    out["sos_file_number"] = field(r"Texas SOS File Number:")
    out["agent_name"] = field(r"Registered Agent Name:")
    out["agent_address"] = field_multiline(r"Registered Office Street Address:")

    return out


# ---------------------------------------------------------------------------
# Derived intelligence (state-agnostic — same shape as CA/NY/CO)
# ---------------------------------------------------------------------------

def derive_intelligence(row):
    out = {}
    agent_name = (row.get(PREFIX + "agent_name") or "").strip()
    agent_addr = (row.get(PREFIX + "agent_address") or "").strip()
    princ_addr = (row.get(PREFIX + "principal_address") or row.get(PREFIX + "mailing_address") or "").strip()

    # Detect agent_is_individual heuristically: corporate-sounding suffix → not individual
    is_corporate_agent = bool(re.search(
        r"\b(llc|inc|corp|service|registered|agent|company|co\.?|csc|corporation)\b",
        agent_name, re.IGNORECASE))
    agent_is_individual = bool(agent_name) and not is_corporate_agent

    addr_match = bool(agent_addr and princ_addr and addresses_match(agent_addr, princ_addr))

    if agent_addr and princ_addr:
        out[PREFIX + "agent_address_matches_principal"] = "True" if addr_match else "False"
    else:
        out[PREFIX + "agent_address_matches_principal"] = ""

    out[PREFIX + "agent_is_individual"] = "True" if agent_is_individual else "False"
    out[PREFIX + "agent_type"] = "Individual" if agent_is_individual else ("Corporation" if agent_name else "")

    likely_owner = ""
    if agent_is_individual:
        if addr_match:
            likely_owner = agent_name
        elif not re.search(r"\b(esq|attorney|law)\b", agent_name, re.IGNORECASE):
            likely_owner = f"{agent_name} (offsite agent)"
    out[PREFIX + "likely_owner_name"] = likely_owner

    # Operator pattern
    if re.search(r"\b(esq|attorney|law)\b", agent_name, re.IGNORECASE):
        pattern = "attorney_agent"
    elif (row.get(PREFIX + "jurisdiction") or "").upper() not in ("TX", "TEXAS", ""):
        pattern = "out_of_state_holdco"
    elif agent_is_individual and addr_match:
        pattern = "owner_operator"
    elif agent_is_individual:
        pattern = "individual_agent_offsite"
    elif agent_name:
        pattern = "service_agent"
    else:
        pattern = "unknown"
    out[PREFIX + "operator_pattern"] = pattern
    return out


def cross_row_pass(rows):
    addr_counter = Counter()
    owner_counter = Counter()
    for r in rows:
        pa = normalize_address(r.get(PREFIX + "principal_address") or r.get(PREFIX + "mailing_address") or "")
        if pa:
            addr_counter[pa] += 1
        owner = (r.get(PREFIX + "likely_owner_name") or "").strip().lower()
        if owner:
            owner_counter[owner] += 1
    for r in rows:
        pa = normalize_address(r.get(PREFIX + "principal_address") or r.get(PREFIX + "mailing_address") or "")
        owner = (r.get(PREFIX + "likely_owner_name") or "").strip().lower()
        is_multi = (pa and addr_counter[pa] > 1) or (owner and owner_counter[owner] > 1)
        r[PREFIX + "multi_entity_operator"] = "True" if is_multi else ("False" if (pa or owner) else "")


# ---------------------------------------------------------------------------
# Playwright search + extraction
# ---------------------------------------------------------------------------

async def _do_search(page, query, debug_dir):
    """Run a single TX Comptroller search. Returns list of (name, taxpayer_number, zip) rows."""
    candidates = []
    try:
        await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_selector("#name", timeout=10000, state="visible")
        await page.fill("#name", "")
        await page.fill("#name", query)
        await page.click("#submitBtn")
        # Wait for the results table to populate
        try:
            await page.wait_for_selector("#resultTable tbody tr", timeout=10000)
        except PWTimeout:
            return candidates

        rows = await page.query_selector_all("#resultTable tbody tr")
        for r in rows:
            tds = await r.query_selector_all("td")
            if len(tds) < 3:
                continue
            link_el = await tds[0].query_selector("a")
            name = (await tds[0].inner_text()).strip()
            href = await link_el.get_attribute("href") if link_el else ""
            taxpayer_match = re.search(r"/(\d{11})$", href or "")
            taxpayer_number = taxpayer_match.group(1) if taxpayer_match else (await tds[1].inner_text()).strip()
            zip_ = (await tds[2].inner_text()).strip()
            if name and taxpayer_number:
                candidates.append({"name": name, "taxpayer_number": taxpayer_number, "zip": zip_, "href": href})
    except Exception as e:
        try:
            debug_dir.mkdir(exist_ok=True)
            safe = re.sub(r"[^a-zA-Z0-9]+", "_", query)[:50]
            ts = int(datetime.now().timestamp())
            await page.screenshot(path=str(debug_dir / f"err_{safe}_{ts}.png"), full_page=True)
            html = await page.content()
            (debug_dir / f"err_{safe}_{ts}.html").write_text(html, encoding="utf-8")
        except Exception:
            pass
        raise
    return candidates


async def _fetch_detail(page, taxpayer_number, debug_dir):
    """Fetch and parse the entity detail page."""
    url = DETAIL_URL.format(taxpayer_number)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1500)
        body_text = await page.evaluate("() => document.body.innerText")
        return body_text, parse_detail_page(body_text)
    except Exception as e:
        try:
            debug_dir.mkdir(exist_ok=True)
            ts = int(datetime.now().timestamp())
            await page.screenshot(path=str(debug_dir / f"detail_err_{taxpayer_number}_{ts}.png"), full_page=True)
        except Exception:
            pass
        return "", {"error": f"{type(e).__name__}: {str(e)[:200]}"}


async def process_one(page, entity_name, debug_dir):
    fetched_at = datetime.now(timezone.utc).isoformat()
    queries = [entity_name] + name_variants(entity_name)
    attempts = []
    candidates = []
    best_query = entity_name

    for q in queries:
        attempts.append(q)
        try:
            cands = await _do_search(page, q, debug_dir)
        except Exception as e:
            out = blank_row()
            out[PREFIX + "fetched_at"] = fetched_at
            out[PREFIX + "query_attempts"] = " | ".join(attempts)
            out[PREFIX + "query_used"] = q
            out[PREFIX + "error"] = f"{type(e).__name__}: {str(e)[:200]}"
            return out
        if cands:
            candidates = cands
            best_query = q
            break

    out = blank_row()
    out[PREFIX + "fetched_at"] = fetched_at
    out[PREFIX + "query_used"] = best_query
    out[PREFIX + "query_attempts"] = " | ".join(attempts)
    out[PREFIX + "match_count"] = len(candidates)

    if not candidates:
        return out

    # Score and pick best
    input_implies_oos = bool(OUT_OF_STATE_HINTS.search(entity_name))
    scored = sorted(
        ((score_candidate(entity_name, c, input_implies_oos), c) for c in candidates),
        key=lambda x: -x[0],
    )
    best_score, best = scored[0]
    out[PREFIX + "first_result_name"] = best["name"]
    out[PREFIX + "taxpayer_number"] = best["taxpayer_number"]
    out[PREFIX + "entity_number"] = best["taxpayer_number"]
    out[PREFIX + "zip"] = best["zip"]
    out[PREFIX + "score"] = f"{best_score:.2f}"
    out[PREFIX + "name_similarity"] = f"{name_similarity(entity_name, best['name']):.3f}"
    out[PREFIX + "alternates"] = json.dumps([
        {"name": c["name"], "num": c["taxpayer_number"], "score": round(s, 2)}
        for s, c in scored[1:6]
    ], ensure_ascii=False) if len(scored) > 1 else ""

    # Fetch the detail page for the best match
    body_text, parsed = await _fetch_detail(page, best["taxpayer_number"], debug_dir)
    if parsed.get("error"):
        out[PREFIX + "error"] = parsed["error"]
        return out

    out[PREFIX + "principal_address"] = parsed.get("mailing_address", "")
    out[PREFIX + "mailing_address"] = parsed.get("mailing_address", "")
    out[PREFIX + "right_to_transact"] = parsed.get("right_to_transact", "")
    out[PREFIX + "sos_registration_status"] = parsed.get("sos_registration_status", "")
    out[PREFIX + "status"] = parsed.get("right_to_transact", "")
    out[PREFIX + "formation_date"] = parsed.get("formation_date", "")
    out[PREFIX + "sos_file_number"] = parsed.get("sos_file_number", "")
    out[PREFIX + "jurisdiction"] = parsed.get("jurisdiction", "")
    out[PREFIX + "agent_name"] = parsed.get("agent_name", "")
    out[PREFIX + "agent_address"] = parsed.get("agent_address", "")

    out.update(derive_intelligence(out))
    return out


# ---------------------------------------------------------------------------
# CSV I/O + main
# ---------------------------------------------------------------------------

def load_input(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return rows, list(reader.fieldnames or [])


def load_existing_output(path):
    if not Path(path).exists():
        return set()
    done = set()
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = (row.get("entity_name") or "").strip().lower()
            if name and (row.get(PREFIX + "fetched_at") or "").strip():
                done.add(name)
    return done


def rescore_only(output_path, log=print):
    if not Path(output_path).exists():
        sys.exit(f"ERROR: --rescore-only requires existing output at {output_path}")
    with open(output_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    for r in rows:
        if int(r.get(PREFIX + "match_count") or 0) > 0:
            r.update(derive_intelligence(r))
    cross_row_pass(rows)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    log(f"Rescored {len(rows)} rows.")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input_csv")
    ap.add_argument("output_csv")
    ap.add_argument("--show", action="store_true", help="Run with visible browser")
    ap.add_argument("--limit", type=int, default=0, help="Cap rows processed this run (0 = no cap)")
    ap.add_argument("--delay", type=float, default=1.5, help="Base seconds between rows (±0.5s jitter)")
    ap.add_argument("--rescore-only", action="store_true", help="Re-derive intelligence + cross-row from existing output")
    args = ap.parse_args()

    if args.rescore_only:
        rescore_only(args.output_csv)
        return

    log = _make_logger(Path(__file__).parent / "enrich.log")
    log(f"Run started — input={args.input_csv}, output={args.output_csv}, show={args.show}, limit={args.limit}")

    input_rows, input_fields = load_input(args.input_csv)
    if "entity_name" not in input_fields:
        sys.exit("ERROR: input CSV must have a column named 'entity_name'")
    log(f"Loaded {len(input_rows)} input rows")

    done_names = load_existing_output(args.output_csv)
    if done_names:
        log(f"Resuming: {len(done_names)} rows already complete")
    output_fields = list(input_fields) + [c for c in COLS if c not in input_fields]

    output_path = Path(args.output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_exists = output_path.exists()
    fout = open(output_path, "a" if output_exists else "w", newline="", encoding="utf-8")
    writer = csv.DictWriter(fout, fieldnames=output_fields, extrasaction="ignore")
    if not output_exists:
        writer.writeheader()
        fout.flush()

    debug_dir = Path(__file__).parent / "debug"
    processed = skipped = 0
    start_time = datetime.now()

    async with Stealth().use_async(async_playwright()) as pw:
        browser = await pw.chromium.launch(headless=not args.show)
        ctx = await browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="America/Chicago",
        )
        page = await ctx.new_page()
        try:
            for i, row in enumerate(input_rows):
                if args.limit and processed >= args.limit:
                    break
                name = (row.get("entity_name") or "").strip()
                if not name:
                    continue
                if name.lower() in done_names:
                    skipped += 1
                    continue

                # Skip person-type rows (TX Comptroller indexes business entities, not individuals)
                etype = (row.get("entity_type") or "").lower()
                if etype == "person":
                    out = blank_row()
                    out[PREFIX + "fetched_at"] = datetime.now(timezone.utc).isoformat()
                    out[PREFIX + "match_count"] = 0
                    out[PREFIX + "query_used"] = name
                    out[PREFIX + "query_attempts"] = "(skipped: input is person, not entity)"
                    writer.writerow({**row, **out})
                    fout.flush()
                    processed += 1
                    log(f"[{i+1}/{len(input_rows)}] {name[:60]} -> SKIPPED (person)")
                    continue

                result = await process_one(page, name, debug_dir)
                writer.writerow({**row, **result})
                fout.flush()
                processed += 1

                status = f"matches={result[PREFIX + 'match_count']}"
                if result.get(PREFIX + "first_result_name"):
                    status += f" | {result[PREFIX + 'first_result_name'][:50]}"
                if result.get(PREFIX + "likely_owner_name"):
                    status += f" | owner: {result[PREFIX + 'likely_owner_name']}"
                if result.get(PREFIX + "error"):
                    status += f" | ERROR: {result[PREFIX + 'error'][:80]}"
                log(f"[{i+1}/{len(input_rows)}] {name[:60]} -> {status}")

                if processed % 10 == 0 and processed > 0:
                    elapsed = (datetime.now() - start_time).total_seconds()
                    rate = processed / elapsed * 60 if elapsed > 0 else 0
                    remaining = sum(1 for r in input_rows if (r.get("entity_name") or "").strip() and (r.get("entity_name") or "").strip().lower() not in done_names) - processed
                    eta = remaining / max(rate / 60, 0.001) / 60
                    log(f"  rolling: {rate:.1f} rows/min — ETA {eta:.1f}min for remaining {remaining}")

                await asyncio.sleep(max(0.3, args.delay + random.uniform(-0.5, 0.5)))
        finally:
            await browser.close()
            fout.close()

    log(f"Match phase done: processed={processed}, skipped={skipped}")
    log("Running cross-row analysis ...")
    rescore_only(args.output_csv, log=log)
    log(f"Output: {output_path.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
