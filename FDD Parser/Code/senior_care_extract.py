#!/usr/bin/env python3
"""
SENIOR CARE FDD EXTRACTOR
=========================
Input  : Data/senior care/*.pdf
Output : output/senior_care_master.csv

Mirrors the QSR pipeline (1_ai_extract.py) but scoped to senior-care brands
(Home Instead and any future brand PDFs dropped into Data/senior care/).
Uses the Claude Code CLI on the Max plan — no API credits required.

Output columns (exactly, in this order):
  franchisee_name, primary_state, name_variants, total_units,
  brands_operated, brand_count, is_multi_brand, all_states,
  state_count, restaurant_phone, sample_address

Usage:
    python senior_care_extract.py
"""

import re, json, csv, time, sys, subprocess, hashlib, shutil, os
from datetime import datetime
from pathlib import Path
from collections import defaultdict
import pdfplumber

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── PATHS ────────────────────────────────────────────────────────────────────
PDF_DIR    = Path(__file__).parent.parent / "Data" / "senior care"
CACHE_DIR  = Path(__file__).parent / "cache" / "senior_care"
OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_CSV = OUTPUT_DIR / "senior_care_master.csv"
LOG_FILE   = Path(__file__).parent / "senior_care_extract.log"

# Claude Code CLI — uses Max plan subscription, no API credits needed.
# We resolve both `claude.cmd` (the npm shim) AND a directory containing
# `node.exe`. The shim calls `node` internally, so its directory must be on
# PATH or the call dies with "'node' is not recognized". This matters when
# the script runs inside a subprocess whose PATH was captured before Node
# was installed (e.g. a Claude Code IDE session opened before npm install).
def _resolve_claude_cmd() -> str:
    name = "claude.cmd" if sys.platform == "win32" else "claude"
    found = shutil.which(name) or shutil.which("claude")
    if found:
        return found
    if sys.platform == "win32":
        candidates = [
            Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd",
            Path(os.environ.get("USERPROFILE", "")) / "AppData" / "Roaming" / "npm" / "claude.cmd",
        ]
        for c in candidates:
            if c.exists():
                return str(c)
    return name  # last resort — will fail loudly with FileNotFoundError


def _resolve_node_dir() -> str | None:
    """Directory containing node.exe — None if we can't find one. Used to
    augment subprocess PATH so claude.cmd's internal `node` invocation works."""
    if sys.platform != "win32":
        return None
    found = shutil.which("node") or shutil.which("node.exe")
    if found:
        return str(Path(found).parent)
    candidates = [
        Path(r"C:\Program Files\nodejs\node.exe"),
        Path(r"C:\Program Files (x86)\nodejs\node.exe"),
        Path(r"D:\node.exe"),                                            # user installed Node to D:\ root
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "nodejs" / "node.exe",
        Path(os.environ.get("ProgramFiles", "")) / "nodejs" / "node.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c.parent)
    return None


CLAUDE_CMD  = _resolve_claude_cmd()
NODE_DIR    = _resolve_node_dir()
MODEL       = "sonnet"
CLI_TIMEOUT = 600

BATCH_LINES     = 100
# Re-read OVERLAP lines from the prior batch into the current one so rows that
# straddle a batch boundary get a second chance at extraction. Dedup on
# (name, address) drops the duplicates the AI emits from the re-read window.
BATCH_OVERLAP   = 50
MAX_RETRIES     = 5
RETRY_WAIT_SECS = 300

# ── PER-PDF PAGE RANGES + BRAND ─────────────────────────────────────────────
# (start_page, end_page, brand_name) — 1-indexed inclusive. Skips auto-detect
# entirely for known PDFs. Exhibit D for Home Instead = pages 198-219; page
# 220+ is Exhibit E (terminated franchisees) which we deliberately exclude.
PAGE_RANGES: dict[str, tuple[int, int, str]] = {
    "Home Instead.pdf":      (198, 219, "Home Instead"),
    "Right at Home.pdf":     (322, 348, "Right at Home"),
    "Always Best.pdf":       (185, 198, "Always Best Care"),
    "Caring.pdf":            (150, 152, "Caring Senior Service"),
    "Comfort Keepers.pdf":   (346, 357, "Comfort Keepers"),
    "FirstLight.pdf":        (195, 225, "FirstLight Home Care"),
    "Interim 1.pdf":         (233, 247, "Interim HealthCare"),
    "interim 2.pdf":         (206, 232, "Interim HealthCare"),
    "Synergy HomeCare.pdf":  ( 62,  69, "Synergy HomeCare"),
    "Visiting Angels.pdf":   (151, 193, "Visiting Angels"),
    # Griswold.pdf intentionally omitted — 5-page Minnesota state addendum, no franchisee list.
}

# Columns the downstream consumer expects, in order.
FIELDS = [
    "franchisee_name", "primary_state", "name_variants", "total_units",
    "brands_operated", "brand_count", "is_multi_brand", "all_states",
    "state_count", "restaurant_phone", "sample_address",
]

# Skip Exhibit E (terminated/transferred franchisees) — these are NOT active operators
EXHIBIT_E_MARKERS = (
    "TRANSFERRED, TERMINATED",
    "LIST OF FRANCHISEES WHO TRANSFERRED",
    "DID NOT RENEW",
    "TRANSFERS – STILL FRANCHISEES",
    "TRANSFERS - STILL FRANCHISEES",
    "FRANCHISES TERMINATED, CANCELED",       # Interim 1/2
    "FRANCHISED BUSINESS NOT YET OPENED",    # Comfort Keepers
    "LIST OF FRANCHISEES WHO HAVE LEFT",     # Caring
    "OUTLET NOT OPEN AS OF",                 # Interim 1/2 pre-opening section
)

# ── LOGGER ───────────────────────────────────────────────────────────────────
_api_call_count = 0
_run_started_at = datetime.now()


def log(msg: str = ""):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}" if msg else ""
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── CLAUDE CLI ───────────────────────────────────────────────────────────────

# Exit code returned to the caller when we detect a Max-plan quota/session limit.
# A wrapper (run_until_done.py) treats this code as "sleep long, then re-run"
# rather than "transient failure, retry soon" — Max-plan resets typically take
# 1-5 hours so burning the script's 5×300s retry loop would waste 25 min.
QUOTA_EXIT_CODE = 42

# Substrings (lowercased) in the CLI's stderr/stdout that indicate the Max-plan
# usage quota is exhausted. When any is matched, we abort the run immediately so
# the wrapper can sleep until the limit resets. Patterns observed across the
# Anthropic CLI's various phrasings.
QUOTA_MARKERS = (
    "you've hit your limit",
    "you have hit your limit",
    "rate_limit_exceeded",
    "usage limit",
    "credit balance is too low",
    "credit balance",
    "5-hour limit",
    "weekly limit",
    "quota exceeded",
    "quota_exceeded",
    "resets ",                      # bare "resets " — matches "resets 6:50pm" (no "at")
    "out of extra usage",           # actual Max-plan exhaustion phrasing observed in logs
    "limit reached",
    # Max-plan auto-reset writes a stub `.claude.json` with this flag when
    # credits are exhausted. The CLI surfaces it as a config-load error during
    # the rewrite window (sometimes "Unexpected end of JSON input"). Treat all
    # three as quota signals so the wrapper sleeps instead of burning retries.
    "out_of_credits",
    "cachedextrausagedisabledreason",
    "configuration error in",
    "unexpected end of json input",
)


def _looks_like_quota_error(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(m in low for m in QUOTA_MARKERS)


def _abort_on_quota(err_text: str):
    """Persist a sentinel and exit fast so the wrapper can sleep until reset."""
    log(f"    🛑 Quota/session limit detected — aborting to let wrapper sleep+retry")
    log(f"       error fragment: {err_text[:200]}")
    try:
        sentinel = LOG_FILE.parent / "cache" / "senior_care" / ".quota_hit"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
    except Exception:
        pass
    sys.exit(QUOTA_EXIT_CODE)


def _build_subprocess_env() -> dict:
    """Inherit current env, but ensure NODE_DIR is on PATH so claude.cmd's
    internal `node` lookup succeeds even when our shell PATH is stale."""
    env = os.environ.copy()
    if NODE_DIR:
        cur = env.get("PATH", "")
        # Compare as discrete PATH entries — substring match is wrong when
        # NODE_DIR is short like "D:\" (matches D:\Git\..., D:\python..., etc.)
        sep = os.pathsep
        norm = lambda p: p.rstrip("\\/").lower()
        entries = {norm(p) for p in cur.split(sep) if p}
        if norm(NODE_DIR) not in entries:
            env["PATH"] = f"{NODE_DIR}{sep}{cur}" if cur else NODE_DIR
    return env


def _run_claude_cli(prompt: str, system: str, model: str) -> str:
    """Invoke claude CLI as a subprocess with retry-on-failure."""
    global _api_call_count
    _api_call_count += 1
    full_prompt = f"{system}\n\n{prompt}" if system else prompt
    env = _build_subprocess_env()

    for attempt in range(1, MAX_RETRIES + 1):
        started = time.time()
        try:
            result = subprocess.run(
                [CLAUDE_CMD, "-p", "--model", model],
                input=full_prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=CLI_TIMEOUT,
                env=env,
            )
        except subprocess.TimeoutExpired:
            log(f"    ⚠️  Call #{_api_call_count} ({model}) timed out after {CLI_TIMEOUT}s  [attempt {attempt}/{MAX_RETRIES}]")
            if attempt < MAX_RETRIES:
                log(f"    ⏳ Waiting {RETRY_WAIT_SECS}s before retry...")
                time.sleep(RETRY_WAIT_SECS)
                continue
            log(f"    ❌ Call #{_api_call_count} all {MAX_RETRIES} attempts exhausted — skipping")
            return ""

        if result.returncode != 0:
            full_err = (result.stderr or "") + "\n" + (result.stdout or "")
            err = full_err.strip()[:300]
            log(f"    ⚠️  Call #{_api_call_count} ({model}) failed rc={result.returncode}  [attempt {attempt}/{MAX_RETRIES}]")
            log(f"       error: {err}")
            # Quota/session limit: don't burn 25 min of retries — wrapper will
            # sleep until reset and re-launch. Partial cache preserves progress.
            if _looks_like_quota_error(full_err):
                _abort_on_quota(full_err)
            if attempt < MAX_RETRIES:
                log(f"    ⏳ Waiting {RETRY_WAIT_SECS}s before retry...")
                time.sleep(RETRY_WAIT_SECS)
                continue
            log(f"    ❌ Call #{_api_call_count} all {MAX_RETRIES} attempts exhausted — skipping")
            return ""

        log(f"    · call #{_api_call_count} [{model}] {time.time()-started:.1f}s")
        return result.stdout.strip()
    return ""


def safe_json(text: str):
    text = re.sub(r"```(?:json)?", "", text).strip().rstrip("`").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
    return None


# ── PDF READ ─────────────────────────────────────────────────────────────────

def extract_pages_text(pdf_path: Path, start_page: int, end_page: int) -> list[str]:
    lines: list[str] = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            total = len(pdf.pages)
            s = max(1, start_page)
            e = min(total, end_page)
            for i in range(s - 1, e):
                t = pdf.pages[i].extract_text(layout=True)
                if t:
                    lines.extend(t.split("\n"))
    except Exception as e:
        log(f"    [!] pdfplumber error: {e}")
        return []
    return lines


def trim_at_exhibit_e(lines: list[str]) -> list[str]:
    """Stop reading at the first Exhibit-E marker we encounter."""
    out: list[str] = []
    for line in lines:
        up = line.upper()
        if any(marker in up for marker in EXHIBIT_E_MARKERS):
            break
        out.append(line)
    return out


# ── EXTRACTION ───────────────────────────────────────────────────────────────

def extract_records(lines: list[str], brand: str, pdf_path: Path) -> list[dict]:
    """
    Send Exhibit-D text to Claude in batches; parse JSON records.
    Each record: {state, name, address, city, st, zip, phone}
    NOTE: 'state' is the leftmost full-state column (may carry over from a
    prior row); 'st' is the 2-letter state code from the address.
    """
    content_hash = hashlib.md5("\n".join(lines).encode("utf-8")).hexdigest()
    records, resume_from, seen_keys = [], 0, set()

    partial = load_partial(pdf_path, content_hash)
    if partial is not None:
        records, resume_from, seen_keys = partial
        log(f"    -> resuming from line {resume_from} ({len(records)} records cached)")

    system = f"""You are extracting franchisee records from the {brand} Franchise Disclosure Document.
The table layout varies by brand. Common column patterns include:
  - State | Owners | Address | City | State | Zip | Phone
  - Owner Name | Address 1 | Address 2 | City | State | Zip | Center/Territory ID | Phone
If a leftmost "State" (full name) column exists and is blank for continuation
rows, INFER it from the most recent non-empty state value above.
If two address columns exist (Address 1 and Address 2), concatenate them as
"address1, address2" into the single 'address' field, skipping any blank one.
Return ONLY a JSON array (no markdown, no explanation). Each object must have keys:
  state, name, address, city, st, zip, phone
Where 'state' is the full state name and 'st' is the 2-letter code.
Use "" for missing fields. Do NOT include header rows, page headers/footers, page numbers,
exhibit titles, or "List of Franchisees" lines as records."""

    step = max(1, BATCH_LINES - BATCH_OVERLAP)  # advance by `step`, read `BATCH_LINES`
    for batch_start in range(resume_from, len(lines), step):
        batch = lines[batch_start: batch_start + BATCH_LINES]
        text = "\n".join(batch)
        if len(text.strip()) < 50:
            save_partial(pdf_path, content_hash, brand, records, batch_start + step, seen_keys)
            continue

        prompt = f"""Extract EVERY franchisee location row from this Exhibit D text chunk.
Each physical location is one record — if the same owner has multiple addresses, emit one object per address.
Carry forward the leftmost State column for rows where it is blank.

Return a JSON array of {{state, name, address, city, st, zip, phone}}.

Text:
{text}"""

        raw = _run_claude_cli(prompt, system, MODEL)
        parsed = safe_json(raw)
        if isinstance(parsed, list):
            for rec in parsed:
                if not isinstance(rec, dict):
                    continue
                name = (rec.get("name") or "").strip()
                if not name or len(name) < 3:
                    continue
                upper = name.upper()
                if any(s in upper for s in ["NAME", "OWNERS", "EXHIBIT", "LIST OF FRANCHISEES"]):
                    continue
                addr = (rec.get("address") or "").strip()
                key = (name.lower(), addr.lower())
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                records.append({
                    "brand":            brand,
                    "franchisee_name":  normalize_name(name),
                    "state_full":       (rec.get("state") or "").strip().title(),
                    "raw_address":      addr,
                    "city":             (rec.get("city") or "").strip().title(),
                    "state":            (rec.get("st") or "").strip().upper(),
                    "zip":              (rec.get("zip") or "").strip(),
                    "restaurant_phone": format_phone(rec.get("phone", "")),
                })

        save_partial(pdf_path, content_hash, brand, records, batch_start + step, seen_keys)
        time.sleep(0.5)

    return records


def normalize_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip().rstrip(",")
    # Strip trailing asterisks (used in Exhibit E to mark special status)
    name = name.rstrip("*").strip()
    return name


def format_phone(raw: str) -> str:
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return f"({digits[0:3]}) {digits[3:6]}-{digits[6:10]}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"({digits[1:4]}) {digits[4:7]}-{digits[7:11]}"
    return raw.strip()


# ── PER-PDF CACHE ────────────────────────────────────────────────────────────

def cache_path(pdf_path: Path) -> Path:
    return CACHE_DIR / f"{pdf_path.stem}.json"


def partial_path(pdf_path: Path) -> Path:
    return CACHE_DIR / f"{pdf_path.stem}.partial.json"


def load_cache(pdf_path: Path):
    cp = cache_path(pdf_path)
    if cp.exists():
        try:
            return json.loads(cp.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def save_cache(pdf_path: Path, data: dict):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path(pdf_path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_partial(pdf_path: Path, content_hash: str):
    pp = partial_path(pdf_path)
    if not pp.exists():
        return None
    try:
        data = json.loads(pp.read_text(encoding="utf-8"))
    except Exception:
        return None
    if data.get("content_hash") != content_hash:
        return None
    records = data.get("records", []) or []
    next_start = int(data.get("next_batch_start", 0) or 0)
    seen = {tuple(k) for k in data.get("seen_keys", []) if isinstance(k, (list, tuple)) and len(k) == 2}
    return records, next_start, seen


def save_partial(pdf_path: Path, content_hash: str, brand: str,
                 records: list[dict], next_start: int, seen_keys: set):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    pp = partial_path(pdf_path)
    tmp = pp.with_suffix(".json.tmp")
    payload = {
        "brand": brand,
        "content_hash": content_hash,
        "next_batch_start": next_start,
        "records": records,
        "seen_keys": [list(k) for k in seen_keys],
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(pp)


def clear_partial(pdf_path: Path):
    pp = partial_path(pdf_path)
    if pp.exists():
        try:
            pp.unlink()
        except Exception:
            pass


# ── PER-PDF ORCHESTRATION ────────────────────────────────────────────────────

def process_pdf(pdf_path: Path) -> tuple[str, list[dict]]:
    log()
    log("─" * 55)
    log(f"📄  {pdf_path.name}")
    pdf_started = time.time()
    calls_before = _api_call_count

    cached = load_cache(pdf_path)
    if cached:
        log(f"    ✅ Loaded from cache ({len(cached['records'])} records)")
        return cached["brand"], cached["records"]

    if pdf_path.name not in PAGE_RANGES:
        log(f"    ⚠️  No page range configured for {pdf_path.name} — add it to PAGE_RANGES")
        return "Unknown", []

    start_page, end_page, brand = PAGE_RANGES[pdf_path.name]
    log(f"    🎯 Using manual page range: pages {start_page}–{end_page}")
    log(f"    ✓ Brand: {brand} (from config)")

    lines = extract_pages_text(pdf_path, start_page, end_page)
    if not lines:
        log(f"    ❌ pdfplumber returned no text for those pages — skipping")
        return brand, []
    log(f"    📖 Read {len(lines)} lines from pages {start_page}–{end_page}")
    lines = trim_at_exhibit_e(lines)
    log(f"    ✂️  Trimmed to {len(lines)} lines (stopped at Exhibit E if present)")

    log(f"    📋 Extracting records with AI...")
    records = extract_records(lines, brand, pdf_path)
    calls_used = _api_call_count - calls_before
    elapsed = time.time() - pdf_started
    log(f"    ✓ Extracted: {len(records)} records  ({calls_used} API calls, {elapsed:.0f}s)")

    save_cache(pdf_path, {"brand": brand, "records": records})
    clear_partial(pdf_path)
    return brand, records


# ── AGGREGATION (records -> 11-column rows) ─────────────────────────────────

def aggregate(all_records: list[dict]) -> list[dict]:
    """Group records by (brand-agnostic) franchisee name → one master row each."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in all_records:
        key = r["franchisee_name"].strip().lower()
        groups[key].append(r)

    operators: list[dict] = []
    for key, recs in groups.items():
        canonical = max((r["franchisee_name"] for r in recs), key=len)
        brands = sorted({r["brand"] for r in recs if r["brand"]})
        states = sorted({r["state"] for r in recs if r["state"]})

        # Primary state = most frequent
        state_freq = defaultdict(int)
        for r in recs:
            if r["state"]:
                state_freq[r["state"]] += 1
        primary_state = max(state_freq, key=state_freq.get) if state_freq else ""

        name_variants = sorted({r["franchisee_name"] for r in recs})
        name_variants_str = " / ".join(name_variants) if len(name_variants) > 1 else ""

        phone = next((r["restaurant_phone"] for r in recs if r.get("restaurant_phone")), "")
        sample_addr_parts = next(
            ((r.get("raw_address",""), r.get("city",""), r.get("state",""), r.get("zip",""))
             for r in recs if r.get("raw_address")),
            ("", "", "", "")
        )
        line1, city, st, zp = sample_addr_parts
        rest = ", ".join(p for p in [city, st] if p)
        sample_addr = ", ".join(p for p in [line1, rest] if p)
        if zp:
            sample_addr = f"{sample_addr} {zp}".strip()

        operators.append({
            "franchisee_name":  canonical,
            "primary_state":    primary_state,
            "name_variants":    name_variants_str,
            "total_units":      len(recs),
            "brands_operated":  " | ".join(brands),
            "brand_count":      len(brands),
            "is_multi_brand":   "Yes" if len(brands) > 1 else "No",
            "all_states":       " | ".join(states),
            "state_count":      len(states),
            "restaurant_phone": phone,
            "sample_address":   sample_addr,
        })

    operators.sort(key=lambda o: (-o["total_units"], o["franchisee_name"].lower()))
    return operators


# ── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    try:
        LOG_FILE.write_text("", encoding="utf-8")
    except Exception:
        pass

    pdfs = sorted(PDF_DIR.glob("*.pdf"))
    if not pdfs:
        log(f"❌ No PDFs found in {PDF_DIR.resolve()}")
        return

    log(f"🚀 Processing {len(pdfs)} senior-care FDD PDF(s) via Claude Code CLI (Max plan)")
    log(f"   Log file: {LOG_FILE}")
    log(f"   Cache is per-PDF — re-running is safe and resumes where left off")

    all_records: list[dict] = []
    summary = []
    for idx, pdf in enumerate(pdfs, 1):
        log()
        log(f"[{idx}/{len(pdfs)}] starting {pdf.name}")
        brand, recs = process_pdf(pdf)
        all_records.extend(recs)
        summary.append((brand, pdf.name, len(recs)))

    total_elapsed = (datetime.now() - _run_started_at).total_seconds()
    log()
    log("═" * 55)
    log(f"✅ EXTRACTION COMPLETE — {_api_call_count} API calls in {total_elapsed:.0f}s")
    log("─" * 55)
    for brand, fname, count in summary:
        status = "✅" if count > 0 else "⚠️ "
        log(f"  {status}  {brand:<22} {count:>5} records  ({fname})")
    log("─" * 55)
    log(f"     Total raw records: {len(all_records)}")

    operators = aggregate(all_records)
    log()
    log(f"📊 Aggregated → {len(operators)} unique franchisees")
    multi = [o for o in operators if o["is_multi_brand"] == "Yes"]
    if multi:
        log(f"   Multi-brand operators: {len(multi)}")

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(operators)
    log(f"💾 Wrote {OUTPUT_CSV.name} ({len(operators)} rows)")


if __name__ == "__main__":
    main()
