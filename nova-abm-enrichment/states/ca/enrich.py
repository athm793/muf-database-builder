"""
bizfile_enrich.py — California SOS bizfile scraper for franchise FDD enrichment

Given a CSV of California LLC/Corp names, fetches registration details from
bizfileonline.sos.ca.gov: status, entity number, formation date, registered
agent, principal/mailing/agent addresses, standing flags, and derived
intelligence columns (likely owner, operator pattern, multi-entity flag).

Install (once):
    pip install playwright playwright-stealth
    playwright install chromium

Input CSV must have a column named `entity_name`.

Run (trial):
    python scripts/bizfile_enrich.py data/input/ca_trial.csv data/output/ca_trial_enriched.csv --show --limit 5

Run (full batch):
    python scripts/bizfile_enrich.py data/input/ca_trial.csv data/output/ca_trial_enriched.csv --show

Re-derive intelligence columns without re-scraping:
    python scripts/bizfile_enrich.py data/input/ca_trial.csv data/output/ca_trial_enriched.csv --rescore-only

Notes:
    * Resumable — rows where bf_fetched_at is filled are skipped.
    * Imperva blocks headless Chromium after 1-2 requests; use --show.
    * Drawer text per entity goes to states/ca/output/drawer_text/<entity_number>.txt
      (kept out of the main CSV to prevent bloat). Re-parse from there if needed.
    * On scrape errors, screenshot + HTML lands in states/ca/debug/.
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

SEARCH_URL = "https://bizfileonline.sos.ca.gov/search/business"

OUT_OF_STATE_HINTS = re.compile(
    r"\b(colorado|dakot|rocky\s*mountain|new\s*england|texas|florida|nevada|"
    r"arizona|oregon|washington|illinois|midwest|northwest|southeast|northeast|"
    r"hawaii|atlantic|rockies|northern|southern)\b",
    re.IGNORECASE,
)

BF_COLS = [
    "bf_match_count",
    "bf_first_result_name",
    "bf_entity_number",
    "bf_status",
    "bf_entity_type",
    "bf_formation_date",
    "bf_jurisdiction",
    "bf_agent_name",
    "bf_agent_type",
    "bf_agent_is_individual",
    "bf_agent_address",
    "bf_principal_address",
    "bf_mailing_address",
    "bf_statement_due_date",
    "bf_inactive_date",
    "bf_standing_sos",
    "bf_standing_ftb",
    "bf_standing_agent",
    "bf_standing_vcfcf",
    "bf_agent_address_matches_principal",
    "bf_likely_owner_name",
    "bf_operator_pattern",
    "bf_multi_entity_operator",
    "bf_query_used",
    "bf_query_attempts",
    "bf_name_similarity",
    "bf_score",
    "bf_alternates",
    "bf_fetched_at",
    "bf_error",
]


def blank_row():
    return {k: "" for k in BF_COLS}


def normalize_for_similarity(name):
    s = (name or "").lower()
    s = re.sub(r"\([^)]*\)", " ", s)
    # Treat "&" and "and" identically — sources encode them differently and
    # exact-name lookup misses are easy to miss when one side has & and the other "and".
    s = s.replace("&", " and ")
    s = re.sub(
        r"\b(llc|l\.l\.c\.?|inc|incorporated|limited|ltd|llp|l\.l\.p\.?|l\.p\.?|corp|corporation|company|co)\b\.?",
        " ",
        s,
    )
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
    """Progressive simplifications when a query returns 0 matches."""
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

    # First N distinctive words
    SKIP = {
        "holdings", "group", "management", "ventures", "enterprises", "investments",
        "concepts", "operating", "operations", "the", "a", "an",
    }
    tokens = [t for t in normalize_for_similarity(name).split() if t not in SKIP]
    if len(tokens) >= 3:
        add(" ".join(tokens[:3]))
    if len(tokens) >= 2:
        add(" ".join(tokens[:2]))

    return variants


def score_result(input_name, candidate, input_implies_oos=None):
    """Score a candidate result. Higher = better pick."""
    score = 0.0
    status = (candidate.get("bf_status") or "").lower()
    if "active" in status:
        score += 100
    elif "suspended" in status:
        score += 20
    elif "forfeited" in status:
        score -= 50
    elif any(s in status for s in ("terminated", "dissolved", "cancelled", "canceled", "merged", "surrendered")):
        score -= 100

    sim = name_similarity(input_name, candidate.get("bf_first_result_name") or "")
    score += sim * 100

    entity_type = (candidate.get("bf_entity_type") or "").lower()
    jurisdiction = (candidate.get("bf_jurisdiction") or "").lower()
    is_oos = ("out of state" in entity_type) or (jurisdiction and "california" not in jurisdiction)
    if input_implies_oos is None:
        input_implies_oos = bool(OUT_OF_STATE_HINTS.search(input_name))
    if is_oos and not input_implies_oos:
        score -= 30

    fd = candidate.get("bf_formation_date") or ""
    if fd:
        try:
            dt = datetime.strptime(fd, "%m/%d/%Y")
            score += (dt - datetime(2000, 1, 1)).days * 0.001
        except Exception:
            pass

    return score


async def find_search_input(page, timeout=15000):
    selectors = [
        'input[placeholder*="earch" i]',
        'input[aria-label*="earch" i]',
        'input[type="search"]',
        'input.MuiInputBase-input',
    ]
    for sel in selectors:
        try:
            el = await page.wait_for_selector(sel, timeout=timeout, state="visible")
            if el:
                return el
        except PWTimeout:
            continue
    return None


def parse_result_row(row_text):
    """bizfile result row: ENTITY NAME (entity_number) / Click to expand /
    <filing_date>\\t<status>\\t<entity_type>\\t<jurisdiction>\\t<agent_name>
    """
    out = {}
    lines = [l.strip() for l in row_text.split("\n") if l.strip()]
    for l in lines:
        m = re.match(r"(.+?)\s*\((\d{5,})\)\s*$", l)
        if m:
            out["bf_first_result_name"] = l
            out["bf_entity_number"] = m.group(2)
            break
    for l in lines:
        if "\t" in l:
            cols = [c.strip() for c in l.split("\t") if c.strip()]
            if len(cols) >= 5:
                out["bf_formation_date"] = cols[0]
                out["bf_status"] = cols[1]
                out["bf_entity_type"] = cols[2]
                out["bf_jurisdiction"] = cols[3]
                out["bf_agent_name"] = cols[4]
                break
    return out


def parse_drawer_fields(text):
    """Extract addresses, standings, agent type, dates from the detail drawer."""
    out = {}
    LABELS = [
        "Principal Address",
        "Mailing Address",
        "Statement of Info Due Date",
        "Inactive Date",
        "Agent",
        "Standing - SOS",
        "Standing - FTB",
        "Standing - Agent",
        "Standing - VCFCF",
    ]
    positions = []
    for label in LABELS:
        m = re.search(rf"(?:\n|^){re.escape(label)}\s*\t", text)
        if m:
            positions.append((m.start(), m.end(), label))
    positions.sort()

    sections = {}
    for i, (_, content_start, label) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        sections[label] = text[content_start:end]

    STOP_RE = re.compile(
        r"^(CA Registered|Authorized|Address Type|History|Documents|Statements?|Filed Documents|View History|View Details)",
        re.IGNORECASE,
    )

    def take_lines(content, max_lines=3):
        lines = []
        for raw in content.split("\n"):
            l = raw.strip()
            if not l:
                if lines:
                    break
                continue
            if STOP_RE.match(l):
                break
            lines.append(l)
            if len(lines) >= max_lines:
                break
        return lines

    if "Principal Address" in sections:
        ls = take_lines(sections["Principal Address"], 3)
        if ls:
            out["bf_principal_address"] = ", ".join(ls)
    if "Mailing Address" in sections:
        ls = take_lines(sections["Mailing Address"], 3)
        if ls:
            out["bf_mailing_address"] = ", ".join(ls)

    for csv_key, label in [
        ("bf_statement_due_date", "Statement of Info Due Date"),
        ("bf_inactive_date", "Inactive Date"),
        ("bf_standing_sos", "Standing - SOS"),
        ("bf_standing_ftb", "Standing - FTB"),
        ("bf_standing_agent", "Standing - Agent"),
        ("bf_standing_vcfcf", "Standing - VCFCF"),
    ]:
        if label in sections:
            ls = take_lines(sections[label], 1)
            if ls:
                out[csv_key] = ls[0]

    if "Agent" in sections:
        agent_lines = [l.strip() for l in sections["Agent"].split("\n") if l.strip()]
        if agent_lines:
            agent_type_raw = agent_lines[0].strip()
            out["bf_agent_type"] = agent_type_raw
            is_individual = agent_type_raw.lower().startswith("individual")
            out["bf_agent_is_individual"] = "True" if is_individual else "False"
            if is_individual and len(agent_lines) >= 2:
                addr_lines = []
                for l in agent_lines[2:]:
                    if STOP_RE.match(l):
                        break
                    addr_lines.append(l)
                    if len(addr_lines) >= 3:
                        break
                if addr_lines:
                    out["bf_agent_address"] = ", ".join(addr_lines)
    return out


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


def derive_intelligence(row):
    """Compute derived columns from already-extracted bf_* fields."""
    out = {}
    agent_name = (row.get("bf_agent_name") or "").strip()
    agent_addr = (row.get("bf_agent_address") or "").strip()
    princ_addr = (row.get("bf_principal_address") or "").strip()
    is_individual = (row.get("bf_agent_is_individual") or "").strip().lower() == "true"
    entity_type = (row.get("bf_entity_type") or "").lower()

    addr_match = bool(agent_addr and princ_addr and addresses_match(agent_addr, princ_addr))
    if agent_addr and princ_addr:
        out["bf_agent_address_matches_principal"] = "True" if addr_match else "False"
    else:
        out["bf_agent_address_matches_principal"] = ""

    if is_individual and agent_name:
        if addr_match:
            out["bf_likely_owner_name"] = agent_name
        elif not re.search(r"\b(esq|attorney|law)\b", agent_name, re.IGNORECASE):
            out["bf_likely_owner_name"] = f"{agent_name} (no addr match)"

    if re.search(r"\b(esq|attorney|law)\b", agent_name, re.IGNORECASE):
        pattern = "attorney_agent"
    elif agent_name and not is_individual:
        pattern = "service_agent"
    elif "out of state" in entity_type:
        pattern = "out_of_state_holdco"
    elif is_individual and addr_match:
        pattern = "owner_operator"
    elif is_individual and agent_name:
        pattern = "individual_agent_offsite"
    else:
        pattern = "unknown"
    out["bf_operator_pattern"] = pattern
    return out


async def _do_search(page, query, input_name, debug_dir):
    """Run one search query. Returns (best_result_dict, alternates_list)."""
    out = blank_row()
    alternates = []
    try:
        await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=45000)

        search_input = await find_search_input(page)
        if not search_input:
            try:
                await page.reload(wait_until="domcontentloaded", timeout=45000)
            except PWTimeout:
                pass
            search_input = await find_search_input(page)
        if not search_input:
            raise RuntimeError("Could not locate search input on bizfile page")

        await search_input.click()
        try:
            await search_input.fill("")
        except Exception:
            pass
        await search_input.type(query, delay=25)
        await search_input.press("Enter")

        try:
            await page.wait_for_selector("text=/Results:\\s*\\d+/i", timeout=20000)
        except PWTimeout:
            pass
        await page.wait_for_timeout(1500)

        rows = await page.query_selector_all('table tbody tr')
        if not rows:
            rows = await page.query_selector_all('[role="row"]')

        candidates = []
        for r in rows:
            try:
                txt = (await r.inner_text()).strip()
            except Exception:
                continue
            if not txt or not re.search(r"\(\d{5,}\)", txt):
                continue
            parsed = parse_result_row(txt)
            if not parsed.get("bf_entity_number"):
                continue
            candidates.append((r, parsed))

        out["bf_match_count"] = len(candidates)
        if not candidates:
            return out, alternates

        input_implies_oos = bool(OUT_OF_STATE_HINTS.search(input_name))
        scored = [
            (score_result(input_name, parsed, input_implies_oos), idx, row_el, parsed)
            for idx, (row_el, parsed) in enumerate(candidates)
        ]
        # Highest score wins; ties broken by original order
        scored.sort(key=lambda x: (-x[0], x[1]))

        best_score, _, best_el, best_parsed = scored[0]
        for k, v in best_parsed.items():
            if v:
                out[k] = v
        out["bf_score"] = f"{best_score:.2f}"

        for s, _, _, parsed in scored[1:6]:
            alternates.append({
                "name": parsed.get("bf_first_result_name", ""),
                "num": parsed.get("bf_entity_number", ""),
                "status": parsed.get("bf_status", ""),
                "score": round(s, 2),
            })

        link = None
        try:
            link = await best_el.query_selector("a, button, [role='link'], [role='button']")
        except Exception:
            pass
        try:
            if link:
                await link.click()
            else:
                await best_el.click()
        except Exception:
            pass

        try:
            await page.wait_for_selector(
                "text=/Principal Address|Mailing Address|Agent Information|Address Type/i",
                timeout=15000,
            )
        except PWTimeout:
            pass
        await page.wait_for_timeout(1500)

        full_text = await page.evaluate("() => document.body.innerText")
        out["_drawer_text"] = full_text  # written to per-entity file by caller, not CSV
        for k, v in parse_drawer_fields(full_text).items():
            if v and not out.get(k):
                out[k] = v

    except Exception as e:
        out["bf_error"] = f"{type(e).__name__}: {str(e)[:300]}"
        try:
            debug_dir.mkdir(exist_ok=True)
            safe = re.sub(r"[^a-zA-Z0-9]+", "_", query)[:50]
            ts = int(datetime.now().timestamp())
            await page.screenshot(path=str(debug_dir / f"err_{safe}_{ts}.png"), full_page=True)
            html = await page.content()
            (debug_dir / f"err_{safe}_{ts}.html").write_text(html, encoding="utf-8")
        except Exception:
            pass

    return out, alternates


async def process_one(page, entity_name, debug_dir):
    fetched_at = datetime.now(timezone.utc).isoformat()
    queries = [entity_name] + name_variants(entity_name)
    attempts = []
    best = None
    best_query = entity_name
    best_alternates = []

    for q in queries:
        attempts.append(q)
        result, alternates = await _do_search(page, q, entity_name, debug_dir)
        if result.get("bf_error"):
            best, best_query, best_alternates = result, q, alternates
            break
        if int(result.get("bf_match_count") or 0) > 0:
            best, best_query, best_alternates = result, q, alternates
            break
        if best is None:
            best, best_query, best_alternates = result, q, alternates

    out = best if best is not None else blank_row()
    out["bf_fetched_at"] = fetched_at
    out["bf_query_used"] = best_query
    out["bf_query_attempts"] = " | ".join(attempts)
    if best_alternates:
        out["bf_alternates"] = json.dumps(best_alternates, ensure_ascii=False)

    if out.get("bf_first_result_name"):
        out["bf_name_similarity"] = f"{name_similarity(entity_name, out['bf_first_result_name']):.3f}"

    out.update(derive_intelligence(out))
    return out


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
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("entity_name") or "").strip().lower()
            if name and (row.get("bf_fetched_at") or "").strip():
                done.add(name)
    return done


def write_drawer_file(output_dir, entity_number, drawer_text):
    if not entity_number or not drawer_text:
        return
    drawer_dir = output_dir / "drawer_text"
    drawer_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9]+", "_", str(entity_number))[:40]
    (drawer_dir / f"{safe}.txt").write_text(drawer_text, encoding="utf-8")


def cross_row_pass(rows):
    """Mark rows whose agent or principal address recurs across rows. Mutates rows in place."""
    addr_counter = Counter()
    agent_counter = Counter()
    for r in rows:
        addr = normalize_address(r.get("bf_principal_address") or "")
        if addr:
            addr_counter[addr] += 1
        agent = (r.get("bf_agent_name") or "").strip().lower()
        is_individual = (r.get("bf_agent_is_individual") or "").strip().lower() == "true"
        if agent and is_individual:
            agent_counter[agent] += 1

    for r in rows:
        signals = []
        addr = normalize_address(r.get("bf_principal_address") or "")
        if addr and addr_counter[addr] > 1:
            signals.append(f"shared_principal_address:{addr_counter[addr]}")
        agent = (r.get("bf_agent_name") or "").strip().lower()
        is_individual = (r.get("bf_agent_is_individual") or "").strip().lower() == "true"
        if agent and is_individual and agent_counter[agent] > 1:
            signals.append(f"shared_agent:{agent_counter[agent]}")
        r["bf_multi_entity_operator"] = "; ".join(signals)


def post_process_csv(output_csv_path, output_dir):
    """Re-derive intelligence + cross-row analysis. Idempotent."""
    rows = []
    with open(output_csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        existing_fields = list(reader.fieldnames or [])
        for r in reader:
            rows.append(r)

    drawer_dir = output_dir / "drawer_text"
    for r in rows:
        ent = (r.get("bf_entity_number") or "").strip()
        if ent and drawer_dir.exists():
            safe = re.sub(r"[^a-zA-Z0-9]+", "_", str(ent))[:40]
            f = drawer_dir / f"{safe}.txt"
            if f.exists():
                drawer_text = f.read_text(encoding="utf-8")
                parsed = parse_drawer_fields(drawer_text)
                for k, v in parsed.items():
                    if v and not (r.get(k) or "").strip():
                        r[k] = v
        r.update(derive_intelligence(r))

    cross_row_pass(rows)

    input_fields = [k for k in existing_fields if not k.startswith("bf_") and k != "bf_drawer_text"]
    output_fields = list(input_fields) + [c for c in BF_COLS if c not in input_fields]

    with open(output_csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=output_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input_csv")
    ap.add_argument("output_csv")
    ap.add_argument("--show", action="store_true", help="Visible browser (required to bypass Imperva)")
    ap.add_argument("--limit", type=int, default=0, help="Cap rows processed this run (0 = no cap)")
    ap.add_argument("--delay", type=float, default=2.5, help="Base seconds between searches (±1s jitter)")
    ap.add_argument("--rescore-only", action="store_true", help="Skip scraping; just re-derive columns + cross-row analysis")
    args = ap.parse_args()

    output_path = Path(args.output_csv)
    output_dir = output_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.rescore_only:
        if not output_path.exists():
            sys.exit("ERROR: --rescore-only requires the output CSV to already exist")
        print("Re-running post-processing (no scraping).")
        post_process_csv(output_path, output_dir)
        print(f"Done. Output: {output_path.resolve()}")
        return

    input_rows, input_fields = load_input(args.input_csv)
    if "entity_name" not in input_fields:
        sys.exit("ERROR: input CSV must have a column named 'entity_name'")

    done_names = load_existing_output(args.output_csv)
    output_fields = list(input_fields) + [c for c in BF_COLS if c not in input_fields]

    output_exists = output_path.exists()
    fout = open(output_path, "a" if output_exists else "w", newline="", encoding="utf-8")
    writer = csv.DictWriter(fout, fieldnames=output_fields, extrasaction="ignore")
    if not output_exists:
        writer.writeheader()
        fout.flush()

    debug_dir = Path(__file__).parent / "debug"
    processed = skipped = 0

    async with Stealth().use_async(async_playwright()) as pw:
        browser = await pw.chromium.launch(headless=not args.show)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="America/Los_Angeles",
        )
        page = await context.new_page()

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

                print(f"[{i+1}/{len(input_rows)}] {name}", flush=True)
                result = await process_one(page, name, debug_dir)
                drawer_text = result.pop("_drawer_text", "")
                write_drawer_file(output_dir, result.get("bf_entity_number", ""), drawer_text)
                writer.writerow({**row, **result})
                fout.flush()
                processed += 1

                status = f"  -> matches={result['bf_match_count']}"
                if result["bf_first_result_name"]:
                    status += f" | pick='{result['bf_first_result_name'][:50]}' (sim={result.get('bf_name_similarity', '')}, score={result.get('bf_score', '')})"
                if result["bf_error"]:
                    status += f" | ERROR: {result['bf_error'][:100]}"
                print(status, flush=True)

                await asyncio.sleep(max(0.5, args.delay + random.uniform(-1.0, 1.0)))
        finally:
            await browser.close()
            fout.close()

    print("\nRunning post-processing (cross-row analysis + derived columns)...")
    post_process_csv(output_path, output_dir)

    print(f"\nDone. Processed {processed}, skipped {skipped} already-completed rows.")
    print(f"Output: {output_path.resolve()}")
    if debug_dir.exists() and any(debug_dir.iterdir()):
        print(f"Debug artifacts (errors): {debug_dir.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
