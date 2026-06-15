#!/usr/bin/env python3
"""
SCRIPT 1 — AI-POWERED FDD EXTRACTOR
=====================================
Uses Claude API to:
  1. Detect the brand name from the cover page
  2. Find the franchisee list section (regardless of label —
     "Exhibit R", "Schedule A", "Attachment 1", "List of Outlets", etc.)
  3. Extract every franchisee record as structured JSON

Handles format variation across brands automatically.
Caches results so re-running is fast and doesn't waste API calls.

Usage:
    python 1_ai_extract.py

Folder structure:
    FDD Parser/
        Data/               ← FDD PDFs live here
        Code/
            cache/          ← per-PDF JSON cache (auto-created)
            output/raw/     ← per-brand structured CSVs (auto-created)
"""

import re, json, csv, time, sys, subprocess, hashlib
from datetime import datetime
from pathlib import Path
import pdfplumber

# Fix Windows console encoding for emoji output
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── CONFIG ───────────────────────────────────────────────────────────────────
PDF_DIR    = Path(__file__).parent.parent / "Data" / "QSR"
CACHE_DIR  = Path(__file__).parent / "cache"
OUTPUT_DIR = Path(__file__).parent / "output" / "raw"
LOG_FILE   = Path(__file__).parent / "extract.log"

# Claude Code CLI — uses Max plan subscription, no API credits needed
CLAUDE_CMD  = "claude.cmd" if sys.platform == "win32" else "claude"
MODEL       = "sonnet"   # for record extraction & nuanced reasoning
HAIKU_MODEL = "haiku"    # for cheap binary classification tasks
CLI_TIMEOUT = 600        # seconds per call — large extraction batches can be slow

# How many lines of text to send per chunk when scanning for the section
CHUNK_SIZE = 800
# Max lines to scan before giving up looking for franchisee section.
# Some FDDs (McDonald's, Burger King) place the franchisee list near the
# end of 50k+ line documents, so we scan the whole thing.
MAX_SCAN_LINES = 100000
# Minimum confidence required for a chunk to be accepted as the franchisee
# section — raised from 70 to 85 to avoid false positives from TOC/cover
# pages that mention addresses or franchisee lists in passing.
CHUNK_CONFIDENCE_THRESHOLD = 85

# ── MANUAL PAGE RANGES ───────────────────────────────────────────────────────
# If a PDF filename is listed here, we skip auto-detection entirely and
# extract records only from these pages (1-indexed, inclusive).
# This is dramatically faster AND more accurate than automatic section
# detection. Add entries as: "filename.pdf": (start_page, end_page)
# (start_page, end_page, brand_name)
# Brand name avoids wasting an API call on detection from franchisee-list pages
# which don't contain cover page text.
PAGE_RANGES: dict[str, tuple[int, int, str]] = {
    "5guys.pdf":           (232, 309, "Five Guys"),
    "applebees.pdf":       (333, 360, "Applebee's"),
    "Bojangles.pdf":       (430, 451, "Bojangles"),
    "burger king fdd.pdf": (549, 619, "Burger King"),
    "BWW1.pdf":            (210, 231, "Buffalo Wild Wings"),
    "BWW2.pdf":            (209, 219, "Buffalo Wild Wings"),
    "cinnabon.pdf":        (350, 419, "Cinnabon"),
    "dairy queen 1.pdf":   (314, 346, "Dairy Queen"),
    "dairy_queen2.pdf":    (372, 440, "Dairy Queen"),
    "Domino's Pizza.pdf":  (100, 282, "Domino's"),
    "ihop1.pdf":           (71,  75,  "IHOP"),
    "ihop2.pdf":           (85,  122, "IHOP"),
    "jamba.pdf":            (311, 350, "Jamba"),
    "Jimmy Johns.pdf":     (222, 284, "Jimmy John's"),
    "KFC1.pdf":            (93,  94,  "KFC"),
    "KFC2.pdf":            (152, 242, "KFC"),
    "Little Ceasar.pdf":   (177, 234, "Little Caesars"),
    "McD.pdf":             (237, 382, "McDonald's"),
    "PIZZA HUT 1.pdf":     (304, 373, "Pizza Hut"),
    "PIZZA HUT 2.pdf":     (158, 201, "Pizza Hut"),
    "popeyes.pdf":         (334, 397, "Popeyes"),
    "Zaxbys.pdf":          (207, 235, "Zaxby's"),
}
# ─────────────────────────────────────────────────────────────────────────────

# ── PER-PDF EXTRACTION HINTS ─────────────────────────────────────────────────
# Extra instructions appended to the extraction system prompt for specific
# PDFs whose list format diverges from the typical "Company — Address" pattern.
# McDonald's is handled separately via 1b_mcd_enrich.py (web-search enrichment),
# not via an inline hint — the McD FDD contains no company names in the source.
PDF_EXTRACTION_HINTS: dict[str, str] = {}
# ─────────────────────────────────────────────────────────────────────────────

# ── REAL-TIME LOGGER ─────────────────────────────────────────────────────────

_api_call_count = 0
_run_started_at = datetime.now()


def log(msg: str = ""):
    """Write a timestamped line to both stdout (flushed) and a log file."""
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}" if msg else ""
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── HELPERS ──────────────────────────────────────────────────────────────────

def extract_layout_text(pdf_path: Path) -> list[str]:
    """Use pdfplumber to extract text, preserving layout. Cross-platform."""
    all_lines = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text(layout=True)
                if text:
                    all_lines.extend(text.split("\n"))
    except Exception as e:
        log(f"    ❌ pdfplumber error: {e}")
        return []
    return all_lines


def extract_layout_text_pages(pdf_path: Path, start_page: int, end_page: int) -> list[str]:
    """
    Extract text from a specific inclusive 1-indexed page range only.
    Much faster than reading the whole PDF when we know exactly which
    pages contain the franchisee list.
    """
    all_lines = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            total = len(pdf.pages)
            # Clamp to valid range
            s = max(1, start_page)
            e = min(total, end_page)
            for page_num in range(s - 1, e):  # 1-indexed → 0-indexed
                text = pdf.pages[page_num].extract_text(layout=True)
                if text:
                    all_lines.extend(text.split("\n"))
    except Exception as e:
        log(f"    ❌ pdfplumber error: {e}")
        return []
    return all_lines


# ── AUTO PAGE-RANGE DETECTION (for FDDs not in PAGE_RANGES) ───────────────────
# A new FDD uploaded by an employee won't have a manual page range. Rather than
# scan the whole document line-by-line with the model (slow + unreliable), we
# score every page by how "address-dense" it is. The franchisee/outlet list is
# always the largest block of pages packed with ZIP codes and phone numbers,
# while disclosure / legal / financial pages have almost none. This needs zero
# API calls and reliably isolates the list section on standard FDDs.

ZIP_RE   = re.compile(r'\b\d{5}(?:-\d{4})?\b')
PHONE_RE = re.compile(r'\(?\b\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b')


def _page_address_score(text: str) -> int:
    """Higher = more like a page of the franchisee/outlet list."""
    if not text:
        return 0
    # Phones are weighted higher — every outlet row tends to carry one, and they
    # almost never appear on disclosure/financial pages.
    return len(ZIP_RE.findall(text)) + 2 * len(PHONE_RE.findall(text))


def find_franchisee_pages(pdf_path: Path,
                          threshold: int = 5,
                          max_gap: int = 2,
                          pad: int = 1,
                          min_total: int = 20) -> tuple[int, int] | None:
    """
    Heuristically locate the franchisee-list pages by address density.

    Returns (start_page, end_page) 1-indexed inclusive, or None if no
    address-dense block was found (caller then falls back to the slow scan).
    """
    scores: list[int] = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            total = len(pdf.pages)
            for page in pdf.pages:
                try:
                    txt = page.extract_text() or ""
                except Exception:
                    txt = ""
                scores.append(_page_address_score(txt))
    except Exception as e:
        log(f"    ⚠️  density scan failed: {e}")
        return None

    if not scores:
        return None

    # Merge high-scoring pages into runs, tolerating up to `max_gap` lean pages
    # (page breaks, section headers, blank pages) inside a single list section.
    runs: list[tuple[int, int]] = []
    start = end = None
    gap = 0
    for i, s in enumerate(scores):
        if s >= threshold:
            if start is None:
                start = i
            end = i
            gap = 0
        elif start is not None:
            gap += 1
            if gap > max_gap:
                runs.append((start, end))
                start = end = None
                gap = 0
    if start is not None:
        runs.append((start, end))

    if not runs:
        return None

    # The franchisee list is the run with the greatest cumulative density.
    a, b = max(runs, key=lambda r: sum(scores[r[0]:r[1] + 1]))
    if sum(scores[a:b + 1]) < min_total:
        return None

    return (max(1, a + 1 - pad), min(total, b + 1 + pad))


MAX_RETRIES      = 5     # how many times to retry a failed call
RETRY_WAIT_SECS  = 300   # 5 minutes between retries (rate-limit cool-down)


def _run_claude_cli(prompt: str, system: str, model: str) -> str:
    """
    Invoke the Claude Code CLI as a subprocess. Uses Max plan auth.
    On failure (rc!=0 or timeout), retries up to MAX_RETRIES times
    with a RETRY_WAIT_SECS pause between attempts to ride out rate limits.
    Returns "" only after all retries are exhausted.
    """
    global _api_call_count
    _api_call_count += 1
    full_prompt = f"{system}\n\n{prompt}" if system else prompt

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
            )
        except subprocess.TimeoutExpired:
            log(f"    ⚠️  Call #{_api_call_count} ({model}) timed out after {CLI_TIMEOUT}s  [attempt {attempt}/{MAX_RETRIES}]")
            if attempt < MAX_RETRIES:
                log(f"    ⏳ Waiting {RETRY_WAIT_SECS}s before retry...")
                time.sleep(RETRY_WAIT_SECS)
                continue
            log(f"    ❌ Call #{_api_call_count} all {MAX_RETRIES} attempts exhausted — skipping")
            return ""

        elapsed = time.time() - started

        if result.returncode != 0:
            err_msg = (result.stderr or result.stdout or "").strip()[:300]
            log(f"    ⚠️  Call #{_api_call_count} ({model}) failed rc={result.returncode}  [attempt {attempt}/{MAX_RETRIES}]")
            log(f"       error: {err_msg}")
            if attempt < MAX_RETRIES:
                log(f"    ⏳ Waiting {RETRY_WAIT_SECS}s before retry...")
                time.sleep(RETRY_WAIT_SECS)
                continue
            log(f"    ❌ Call #{_api_call_count} all {MAX_RETRIES} attempts exhausted — skipping")
            return ""

        # Success
        log(f"    · call #{_api_call_count} [{model}] {elapsed:.1f}s")
        return result.stdout.strip()

    return ""


def call_claude(prompt: str, system: str = "", max_tokens: int = 4096) -> str:
    """Sonnet call via Claude Code CLI. (max_tokens kept for signature compatibility.)"""
    return _run_claude_cli(prompt, system, MODEL)


def call_haiku(prompt: str, system: str = "", max_tokens: int = 200) -> str:
    """Haiku call via Claude Code CLI for cheap binary/classification tasks."""
    return _run_claude_cli(prompt, system, HAIKU_MODEL)


def safe_json(text: str) -> dict | list | None:
    """Extract and parse JSON from Claude's response (strips markdown fences)."""
    text = re.sub(r"```(?:json)?", "", text).strip().rstrip("`").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find a JSON block within the text
        m = re.search(r'(\{[\s\S]*\}|\[[\s\S]*\])', text)
        if m:
            try:
                return json.loads(m.group(1))
            except:
                pass
    return None


# ── STEP 1: DETECT BRAND ─────────────────────────────────────────────────────

def detect_brand(lines: list[str]) -> str:
    """Ask Claude to identify the franchisor brand from the first 60 lines."""
    cover = "\n".join(lines[:60])
    prompt = f"""This is the cover page of a Franchise Disclosure Document (FDD).
What is the franchisor brand name? Return ONLY the brand name as a short string,
e.g. "McDonald's", "Burger King", "Taco Bell".
Do not include LLC, Corp, USA etc. Just the consumer-facing brand name.

Cover page text:
{cover}"""
    brand = call_claude(prompt, max_tokens=50).strip().strip('"').strip("'")
    return brand


# ── STEP 2: FIND FRANCHISEE LIST SECTION ─────────────────────────────────────

def find_franchisee_section(lines: list[str]) -> tuple[int, int]:
    """
    Scan the PDF in chunks, asking Claude to identify where the franchisee
    list begins and ends.

    The section could be called anything:
    - Exhibit R / Exhibit A / Exhibit S
    - Schedule A / Attachment 1
    - "List of Franchised Restaurants/Outlets/Locations"
    - "Current Franchisees" / "Franchisee Directory"
    - Item 20 tables (some FDDs embed the list in Item 20 directly)

    Returns (start_line, end_line).
    """
    system = """You are a legal document analyst specialising in Franchise Disclosure Documents (FDDs).
Your job is to locate the section that lists current franchisee names and their restaurant locations.
This section may be labelled anything — Exhibit R, Schedule A, Attachment, List of Outlets, etc.
Respond only with valid JSON."""

    # First pass: scan TOC / first 500 lines for structural hints
    toc_text = "\n".join(lines[:500])
    toc_prompt = f"""Look at this text from a Franchise Disclosure Document.
Find any mention of where the list of current franchisees/outlets is located.
This could be an exhibit, attachment, schedule, or section with any name.

Return JSON:
{{
  "found_hint": true/false,
  "section_name": "e.g. Exhibit R / Schedule A / List of Franchised Restaurants",
  "likely_keyword": "the exact text that marks the start of that section"
}}

Text:
{toc_text}"""

    hint_raw = call_claude(toc_prompt, system=system, max_tokens=200)
    hint = safe_json(hint_raw) or {}
    keyword = hint.get("likely_keyword", "")

    # If we got a keyword hint, search for it directly
    if keyword and len(keyword) > 3:
        for i, line in enumerate(lines):
            if keyword.upper() in line.upper() and len(line.strip()) < 100:
                # Verify this is actually the start of the franchisee list
                sample = "\n".join(lines[i:i+80])
                verify_prompt = f"""Does this text appear to be the START of a list of franchisee names
and their restaurant addresses? Answer with JSON: {{"is_franchisee_list": true/false}}

Text:
{sample}"""
                verify = safe_json(call_haiku(verify_prompt, system=system, max_tokens=60))
                if verify and verify.get("is_franchisee_list"):
                    # Find the end: scan forward for next major section
                    end = find_section_end(lines, i)
                    return i, end

    # Fallback: chunk scan — look for a block that looks like a franchisee list
    log("     No TOC hint found — scanning document in chunks...")
    for chunk_start in range(0, min(len(lines), MAX_SCAN_LINES), CHUNK_SIZE):
        chunk = "\n".join(lines[chunk_start: chunk_start + CHUNK_SIZE])

        scan_prompt = f"""Does this chunk of text appear to contain a list of franchisee names
and their restaurant addresses (part of the franchisee outlet list in an FDD)?
Look for patterns like:
- Rows of NAME + ADDRESS + PHONE NUMBER
- Multiple people's names with street addresses
- A table or columnar list of franchise locations

Return JSON: {{"contains_franchisee_list": true/false, "confidence": 0-100}}

Chunk (lines {chunk_start}–{chunk_start+CHUNK_SIZE}):
{chunk[:2000]}"""

        result = safe_json(call_haiku(scan_prompt, system=system, max_tokens=80))
        if result and result.get("contains_franchisee_list") and result.get("confidence", 0) >= CHUNK_CONFIDENCE_THRESHOLD:
            # Back up a bit to catch the section header
            start = max(0, chunk_start - 20)
            end = find_section_end(lines, chunk_start)
            log(f"     Found franchisee section at line ~{start}")
            return start, end

        # Rate limit guard
        time.sleep(0.3)

    return -1, -1


def find_section_end(lines: list[str], start: int) -> int:
    """
    Scan forward from start to find where the franchisee list ends.
    Only stops on EXPLICIT end-of-section markers — never on blank-line runs,
    because FDD page breaks routinely produce long blank gaps mid-section.
    """
    end_patterns = [
        r'EXHIBIT\s+[S-Z]\b',
        r'SCHEDULE\s+[B-Z]\b',
        r'ATTACHMENT\s+[2-9]\b',
        r'^ITEM\s+\d+\b',
        r'FRANCHISE AGREEMENT',
        r'OPERATIONS MANUAL',
        r'FINANCIAL STATEMENTS',
    ]

    # Scan up to 40,000 lines forward (covers even the largest FDDs)
    limit = min(start + 40000, len(lines))
    for i in range(start + 10, limit):
        line = lines[i].strip().upper()
        if not line:
            continue
        # Explicit end-section markers only — and only if the line is short
        # (otherwise we might match a sentence that happens to contain the word)
        if len(lines[i].strip()) < 80:
            for pattern in end_patterns:
                if re.search(pattern, line):
                    return i
    return limit


def _looks_like_franchisee_list(section_lines: list[str]) -> bool:
    """
    Quick sanity check (one Haiku call) that an address-dense page block is
    actually a list of franchisee/operator names with addresses — not a decoy
    such as an Item 20 unit-count table, a state-agent exhibit, or financial
    statements. Returns True on a clear yes OR if the check itself fails
    (we don't want a flaky call to throw away a good detection).
    """
    sample = "\n".join(section_lines[:60])
    prompt = f"""Below is the start of a section from a Franchise Disclosure Document.
Is this a list of individual franchisee/operator names together with their
restaurant locations (addresses)? Answer JSON: {{"is_franchisee_list": true/false}}

Text:
{sample}"""
    verdict = safe_json(call_haiku(prompt, max_tokens=60))
    if not isinstance(verdict, dict):
        return True  # inconclusive — trust the density heuristic
    return bool(verdict.get("is_franchisee_list", True))


# ── STEP 3: EXTRACT RECORDS ───────────────────────────────────────────────────

def extract_records_from_section(lines: list[str], brand: str, pdf_path: Path, extra_hint: str = "") -> list[dict]:
    """
    Send the franchisee section to Claude in batches.
    Claude returns structured JSON for each record:
    {name, address, city, state, zip, phone}

    We batch 250 lines at a time to stay within token limits.

    Writes a partial cache after every batch so work survives crashes/kills.
    On re-entry, resumes from the saved next_batch_start if the content
    hash of `lines` matches what was saved.

    Args:
      extra_hint: per-PDF extra instructions appended to the system prompt.
    """
    BATCH = 250
    content_hash = hashlib.md5("\n".join(lines).encode("utf-8")).hexdigest()

    # Dedupe on (name, address) so the same operator at different locations
    # is preserved. Only true duplicates (same name + same address from
    # overlapping batch reads) are dropped.
    all_records: list[dict] = []
    seen_location_keys: set = set()
    resume_from = 0

    partial = load_partial(pdf_path, content_hash)
    if partial is not None:
        all_records, resume_from, seen_location_keys = partial
        log(f"    🔁 Resuming from batch offset {resume_from} ({len(all_records)} records already saved)")

    system = f"""You are extracting franchisee data from a {brand} Franchise Disclosure Document.
Extract EVERY individual franchise LOCATION listed in the text, even when the same
franchisee name appears multiple times with different addresses — each location is
a separate record and MUST be returned as its own object.
Return ONLY a JSON array. No explanation. No markdown.
Each object must have these exact keys:
  name, address, city, state, zip, phone
Use empty string "" for any missing field.
Do not include header rows, page numbers, or section titles as records.{extra_hint}"""

    for batch_start in range(resume_from, len(lines), BATCH):
        batch_lines = lines[batch_start: batch_start + BATCH]
        chunk_text = "\n".join(batch_lines)

        # Skip clearly empty or header-only chunks
        if len(chunk_text.strip()) < 50:
            # Still advance the partial cursor so we don't re-scan this on resume
            save_partial(pdf_path, content_hash, brand, all_records,
                         batch_start + BATCH, seen_location_keys)
            continue

        prompt = f"""Extract EVERY franchise location record from this text chunk.
If the same franchisee name owns multiple locations (different addresses), return
one object per location — DO NOT collapse duplicates.
Return a JSON array of objects with keys: name, address, city, state, zip, phone.

Text:
{chunk_text}"""

        raw = call_claude(prompt, system=system, max_tokens=4096)
        records = safe_json(raw)


        if isinstance(records, list):
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                name = rec.get("name", "").strip()
                # Skip if empty name or header-like content
                if not name or len(name) < 3:
                    continue
                if any(skip in name.upper() for skip in ["NAME", "FRANCHISEE", "EXHIBIT", "SCHEDULE"]):
                    continue
                # Dedupe on (name + address) to keep multi-location operators
                addr = rec.get("address", "").strip()
                loc_key = (name.lower(), addr.lower())
                if loc_key in seen_location_keys:
                    continue
                seen_location_keys.add(loc_key)
                all_records.append({
                    "brand":           brand,
                    "franchisee_name": normalize_name(name),
                    "raw_address":     addr,
                    "city":            rec.get("city", "").title(),
                    "state":           rec.get("state", "").upper(),
                    "zip":             rec.get("zip", ""),
                    "restaurant_phone": rec.get("phone", ""),
                })

        # Persist progress after every batch — atomic write via tmp+replace.
        save_partial(pdf_path, content_hash, brand, all_records,
                     batch_start + BATCH, seen_location_keys)

        time.sleep(0.5)  # Rate limit guard

    return all_records


def normalize_name(name: str) -> str:
    """Normalize to Title Case, strip suffixes."""
    name = re.sub(r'\s+', ' ', name).strip()
    name = re.sub(r',?\s*(JR\.?|SR\.?|III|II|IV|ESQ\.?)$', '', name, flags=re.IGNORECASE).strip()
    return name.title()


# ── CACHING ───────────────────────────────────────────────────────────────────

def cache_path(pdf_path: Path) -> Path:
    return CACHE_DIR / f"{pdf_path.stem}.json"


def load_cache(pdf_path: Path) -> dict | None:
    cp = cache_path(pdf_path)
    if cp.exists():
        with open(cp) as f:
            return json.load(f)
    return None


def save_cache(pdf_path: Path, data: dict):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(cache_path(pdf_path), "w") as f:
        json.dump(data, f, indent=2)


# ── PARTIAL (per-batch) CACHE — resume-safe ──────────────────────────────────
# Written after every batch so that a crash, kill, or power loss loses at most
# one batch of work instead of the entire PDF. The content_hash guards against
# resuming into a different section if the auto-detection drifts between runs.

def partial_path(pdf_path: Path) -> Path:
    return CACHE_DIR / f"{pdf_path.stem}.partial.json"


def load_partial(pdf_path: Path, content_hash: str) -> tuple[list[dict], int, set] | None:
    pp = partial_path(pdf_path)
    if not pp.exists():
        return None
    try:
        with open(pp, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if data.get("content_hash") != content_hash:
        return None
    records = data.get("records", []) or []
    next_start = int(data.get("next_batch_start", 0) or 0)
    seen = {tuple(k) for k in data.get("seen_location_keys", []) if isinstance(k, (list, tuple)) and len(k) == 2}
    return records, next_start, seen


def save_partial(pdf_path: Path, content_hash: str, brand: str,
                 records: list[dict], next_batch_start: int,
                 seen_location_keys: set):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    pp = partial_path(pdf_path)
    tmp = pp.with_suffix(".json.tmp")
    payload = {
        "brand": brand,
        "content_hash": content_hash,
        "next_batch_start": next_batch_start,
        "records": records,
        "seen_location_keys": [list(k) for k in seen_location_keys],
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    tmp.replace(pp)


def clear_partial(pdf_path: Path):
    pp = partial_path(pdf_path)
    if pp.exists():
        try:
            pp.unlink()
        except Exception:
            pass


# ── MAIN ─────────────────────────────────────────────────────────────────────

def process_pdf(pdf_path: Path,
                brand_hint: str | None = None,
                pages_hint: tuple[int, int] | None = None) -> tuple[str, list[dict]]:
    """
    Extract franchisee records from one FDD PDF.

    brand_hint / pages_hint let a caller (e.g. the single-file "add FDD" tool)
    override auto-detection when the employee already knows the brand or the
    franchisee-list page range. Both are optional — when absent, the brand is
    read from the cover page and the page range is found by address-density.
    """
    log()
    log("─" * 55)
    log(f"📄  {pdf_path.name}")
    pdf_started = time.time()
    calls_before = _api_call_count

    # Check cache first
    cached = load_cache(pdf_path)
    if cached:
        log(f"    ✅ Loaded from cache ({len(cached['records'])} records)")
        return cached["brand"], cached["records"]

    # Per-PDF custom extraction instructions (empty string for most PDFs)
    extra_hint = PDF_EXTRACTION_HINTS.get(pdf_path.name, "")
    if extra_hint:
        log(f"    🧩 Using custom extraction hint for this PDF")

    # ── Fast path: known page range ───────────────────────────────────────
    # Either a built-in PAGE_RANGES entry, or a caller-supplied pages_hint.
    manual_range = None
    if pdf_path.name in PAGE_RANGES:
        s, e, b = PAGE_RANGES[pdf_path.name]
        manual_range = (s, e, b)
    elif pages_hint is not None:
        b = brand_hint or detect_brand(extract_layout_text_pages(pdf_path, 1, 6))
        manual_range = (pages_hint[0], pages_hint[1], b)

    if manual_range is not None:
        start_page, end_page, brand = manual_range
        log(f"    🎯 Using manual page range: pages {start_page}–{end_page}")
        log(f"    ✓ Brand: {brand} (from config)")
        section_lines = extract_layout_text_pages(pdf_path, start_page, end_page)
        if not section_lines:
            log(f"    ❌ pdfplumber returned no text for those pages — skipping")
            return "Unknown", []
        log(f"    📖 Read {len(section_lines)} lines from pages {start_page}–{end_page}")

        # Skip section detection entirely — go straight to extraction
        log(f"    📋 Extracting records with AI...")
        records = extract_records_from_section(section_lines, brand, pdf_path, extra_hint)
        calls_used = _api_call_count - calls_before
        elapsed = time.time() - pdf_started
        log(f"    ✓ Extracted: {len(records)} records  ({calls_used} API calls, {elapsed:.0f}s)")
        save_cache(pdf_path, {"brand": brand, "records": records})
        clear_partial(pdf_path)
        return brand, records

    # ── Auto path: detect brand + franchisee pages ────────────────────────
    # Step 1: Brand detection (from cover page only — cheap, one API call)
    if brand_hint:
        brand = brand_hint
        log(f"    ✓ Brand: {brand} (provided)")
    else:
        log(f"    🔍 Detecting brand...")
        brand = detect_brand(extract_layout_text_pages(pdf_path, 1, 6))
        log(f"    ✓ Brand: {brand}")

    # Step 2a: Fast heuristic — find the address-dense franchisee-list pages.
    # No API calls; reliably isolates the list on standard FDD layouts.
    log(f"    🔍 Locating franchisee list pages (address-density scan)...")
    page_range = find_franchisee_pages(pdf_path)
    if page_range is not None:
        s, e = page_range
        log(f"    ✓ Detected franchisee list: pages {s}–{e}")
        section_lines = extract_layout_text_pages(pdf_path, s, e)
        # Confirm the dense block really is a franchisee/outlet list and not a
        # decoy (Item 20 unit-count table, state-agent exhibit, financials).
        # One cheap Haiku call; on rejection we fall through to the full scan.
        if section_lines and not _looks_like_franchisee_list(section_lines):
            log(f"    ⚠️  Dense block didn't look like a franchisee list — falling back to full scan")
            section_lines = []
        if section_lines:
            log(f"    📋 Extracting records with AI...")
            records = extract_records_from_section(section_lines, brand, pdf_path, extra_hint)
            if records:
                calls_used = _api_call_count - calls_before
                elapsed = time.time() - pdf_started
                log(f"    ✓ Extracted: {len(records)} records  ({calls_used} API calls, {elapsed:.0f}s)")
                save_cache(pdf_path, {"brand": brand, "records": records})
                clear_partial(pdf_path)
                return brand, records
            log(f"    ⚠️  Density-detected pages yielded 0 records — falling back to full scan")

    # Step 2b: Fallback — slow line-by-line scan of the whole document.
    log(f"    🔍 Density scan inconclusive — scanning full document...")
    lines = extract_layout_text(pdf_path)
    if not lines:
        log(f"    ❌ pdfplumber failed — skipping")
        return brand, []
    log(f"    📖 Read {len(lines)} lines from PDF")

    start, end = find_franchisee_section(lines)
    if start == -1:
        log(f"    ❌ Franchisee section not found")
        save_cache(pdf_path, {"brand": brand, "records": [], "error": "section_not_found"})
        return brand, []

    section_lines = lines[start:end]
    log(f"    ✓ Section: lines {start}–{end} ({len(section_lines)} lines)")

    log(f"    📋 Extracting records with AI...")
    records = extract_records_from_section(section_lines, brand, pdf_path, extra_hint)
    calls_used = _api_call_count - calls_before
    elapsed = time.time() - pdf_started
    log(f"    ✓ Extracted: {len(records)} records  ({calls_used} API calls, {elapsed:.0f}s)")

    save_cache(pdf_path, {"brand": brand, "records": records})
    clear_partial(pdf_path)
    return brand, records


def save_raw_csv(brand: str, records: list[dict], output_dir: Path):
    """
    Append records for a brand to its raw CSV. Multiple PDFs can share a
    brand (e.g. PIZZA HUT 1.pdf + PIZZA HUT 2.pdf both map to 'Pizza Hut'),
    so we must append rather than overwrite. Header is written only when
    the file doesn't exist yet.
    """
    if not records:
        return
    safe = re.sub(r'[^\w\-]', '_', brand)
    path = output_dir / f"{safe}_raw.csv"
    fields = ["brand","franchisee_name","raw_address","city","state","zip","restaurant_phone"]
    file_exists = path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not file_exists:
            writer.writeheader()
        writer.writerows(records)
    print(f"    💾 Appended: {path.name}")


RAW_FIELDS = ["brand", "franchisee_name", "raw_address", "city",
              "state", "zip", "restaurant_phone"]


def append_and_dedupe_raw(brand: str, records: list[dict], output_dir: Path) -> int:
    """
    Add one PDF's records to its brand raw CSV, then remove duplicate rows.

    Used by single-file mode. Unlike save_raw_csv (which blindly appends),
    this re-reads the brand CSV, merges the new records, and de-duplicates on
    the full identity (name + address + city + state + zip). That makes adding
    the SAME FDD twice idempotent — re-uploads can't double a brand's unit
    counts — while still keeping every distinct location.

    Returns the number of genuinely new rows added.
    """
    if not records:
        return 0
    output_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[^\w\-]', '_', brand)
    path = output_dir / f"{safe}_raw.csv"

    existing: list[dict] = []
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            existing = list(csv.DictReader(f))
    before = len(existing)

    def row_key(r: dict) -> tuple:
        return tuple((str(r.get(k, "")) or "").strip().lower()
                     for k in ("franchisee_name", "raw_address", "city", "state", "zip"))

    seen: set = set()
    merged: list[dict] = []
    for r in existing + records:
        k = row_key(r)
        if k in seen:
            continue
        seen.add(k)
        # Normalise to the canonical column set
        merged.append({fld: r.get(fld, "") for fld in RAW_FIELDS})

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RAW_FIELDS)
        writer.writeheader()
        writer.writerows(merged)

    net_new = len(merged) - before
    print(f"    💾 {path.name}: {len(merged)} rows total ({net_new:+d} new)")
    return net_new


def single_main(pdf_path_str: str, brand: str | None = None,
                pages: str | None = None) -> int:
    """
    Add exactly ONE FDD PDF to the dataset (used by the GUI 'Add FDD' tool).

    Extracts just this PDF, then appends + de-dupes its brand raw CSV WITHOUT
    touching any other brand's data. (The full main() globs the disk and
    rebuilds every raw CSV — unsafe here, since a fresh clone won't have the
    other 21 source PDFs on disk, only their committed caches/CSVs.)

    Prints a final machine-readable line the GUI parses:
        SINGLE_RESULT brand=<...> extracted=<n> net_new=<n>
    Returns process exit code (0 ok, 2 = nothing extracted).
    """
    pdf_path = Path(pdf_path_str)
    if not pdf_path.exists():
        log(f"❌ File not found: {pdf_path}")
        print("SINGLE_RESULT|||0|0", flush=True)
        return 1

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    pages_hint = None
    if pages:
        m = re.match(r'^\s*(\d+)\s*[-–]\s*(\d+)\s*$', pages)
        if m:
            pages_hint = (int(m.group(1)), int(m.group(2)))
            log(f"    🎯 Using provided page range: {pages_hint[0]}–{pages_hint[1]}")
        else:
            log(f"    ⚠️  Could not parse page range '{pages}' — ignoring, will auto-detect")

    log(f"🚀 Adding single FDD: {pdf_path.name}")
    detected_brand, records = process_pdf(pdf_path, brand_hint=brand, pages_hint=pages_hint)
    net_new = append_and_dedupe_raw(detected_brand, records, OUTPUT_DIR)

    log()
    log("═" * 55)
    if records:
        log(f"✅ {pdf_path.name}: extracted {len(records)} locations "
            f"for {detected_brand} ({net_new} new)")
    else:
        log(f"⚠️  {pdf_path.name}: extracted 0 records. The franchisee list "
            f"page range may need to be supplied manually.")
    # Machine-readable summary line for the GUI to parse:
    #   SINGLE_RESULT|<brand>|<extracted>|<net_new>
    print(f"SINGLE_RESULT|{detected_brand}|{len(records)}|{net_new}", flush=True)
    return 0 if records else 2


def main():
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Clear previous raw CSVs — save_raw_csv appends, so stale files would
    # cause duplication when re-running with cached PDFs. Per-PDF JSON caches
    # in cache/ are left alone (those are the source of truth for records).
    for csv_file in OUTPUT_DIR.glob("*_raw.csv"):
        try:
            csv_file.unlink()
        except Exception:
            pass

    # Start a fresh log for this run
    try:
        LOG_FILE.write_text("", encoding="utf-8")
    except Exception:
        pass

    pdfs = sorted(PDF_DIR.glob("*.pdf"))
    if not pdfs:
        log(f"❌ No PDFs found in {PDF_DIR.resolve()}")
        return

    log(f"🚀 Processing {len(pdfs)} FDD PDFs via Claude Code CLI (Max plan)")
    log(f"   Log file: {LOG_FILE}")
    log(f"   Cache is per-PDF — re-running is safe and resumes where left off")

    summary = []
    for idx, pdf in enumerate(pdfs, start=1):
        log()
        log(f"[{idx}/{len(pdfs)}] starting {pdf.name}")
        brand, records = process_pdf(pdf)
        save_raw_csv(brand, records, OUTPUT_DIR)
        summary.append((brand, pdf.name, len(records)))

    total_elapsed = (datetime.now() - _run_started_at).total_seconds()
    log()
    log("═" * 55)
    log(f"✅ EXTRACTION COMPLETE — {_api_call_count} API calls in {total_elapsed:.0f}s")
    log("─" * 55)
    for brand, fname, count in summary:
        status = "✅" if count > 0 else "⚠️ "
        log(f"  {status}  {brand:<25} {count:>5} records  ({fname})")

    total = sum(c for _, _, c in summary)
    log("─" * 55)
    log(f"     Total records: {total}")
    log()
    log("  ➡️  Run 2_ai_match.py next")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Extract franchisee records from FDD PDFs.")
    ap.add_argument("--single", metavar="PDF",
                    help="Add a single PDF to the dataset (append + dedupe its brand CSV "
                         "without rebuilding others). Used by the Add-FDD tool.")
    ap.add_argument("--brand", default=None,
                    help="Override the brand name (single mode only).")
    ap.add_argument("--pages", default=None,
                    help="Franchisee-list page range, e.g. 230-309 (single mode only). "
                         "Omit to auto-detect.")
    args, _ = ap.parse_known_args()

    if args.single:
        sys.exit(single_main(args.single, args.brand, args.pages))
    else:
        main()
