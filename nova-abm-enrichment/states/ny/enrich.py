"""
states/ny/enrich.py — match franchisee LLCs against NY State's Active Corporations dataset

Bulk source: data.ny.gov (Socrata Open Data portal)
Dataset:     "Active Corporations: Beginning 1800" — dataset ID n9v6-gdp6
URL:         https://data.ny.gov/api/views/n9v6-gdp6/rows.csv?accessType=DOWNLOAD
Format:      CSV with quoted fields
Refresh:     monthly (per data.ny.gov)
Records:     ~4.2 million active corporations as of dataset publish date

Why bulk over scrape: NY's Active Corporations dataset is HTTPS-published, much
faster than FL's throttled SFTP. Includes registered_agent + chairman + location
fields directly. When chairman_name is filled in (subset of records), we have a
direct owner identity for free — comparable signal quality to FL Sunbiz officers.

Install (once, in project venv):
    pip install requests

Run (auto-download bulk on first use, then match):
    python states/ny/enrich.py states/ny/input/franchisees.csv states/ny/output/enriched.csv

Run with a manually-downloaded bulk CSV (skip HTTPS):
    python states/ny/enrich.py states/ny/input/franchisees.csv states/ny/output/enriched.csv --bulk-file path/to/active_corps.csv

Re-derive intelligence columns without re-matching:
    python states/ny/enrich.py states/ny/input/franchisees.csv states/ny/output/enriched.csv --rescore-only

Notes:
    * Resumable: rows where ny_fetched_at is filled are skipped on re-run.
    * Bulk file cached at states/ny/bulk/active_corps.csv; refreshed if older than 30 days.
    * Output schema follows the universal contract (see ~/.claude/skills/build-state-sos-scraper/SKILL.md).
    * Status field: implicit "Active" (this is the active corporations dataset; inactive entities aren't included).
"""
import argparse
import csv
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PREFIX = "ny_"

DOWNLOAD_URL = "https://data.ny.gov/api/views/n9v6-gdp6/rows.csv?accessType=DOWNLOAD"
BULK_CACHE_DIR = Path(__file__).parent / "bulk"
BULK_CSV_PATH = BULK_CACHE_DIR / "active_corps.csv"
LOG_PATH = BULK_CACHE_DIR / "download.log"
REFRESH_AFTER_DAYS = 30  # NY publishes monthly

OUT_OF_STATE_HINTS = re.compile(
    r"\b(colorado|dakot|rocky\s*mountain|new\s*england|texas|california|florida|nevada|"
    r"arizona|oregon|washington|illinois|midwest|northwest|southeast|northeast|"
    r"hawaii|atlantic|rockies|northern|southern)\b",
    re.IGNORECASE,
)

UNIVERSAL = [
    "match_count", "first_result_name", "entity_number", "status", "entity_type",
    "formation_date", "jurisdiction", "agent_name", "agent_type", "agent_is_individual",
    "agent_address", "principal_address", "mailing_address",
    "statement_due_date", "inactive_date",
    "agent_address_matches_principal", "likely_owner_name", "operator_pattern",
    "multi_entity_operator", "query_used", "query_attempts", "name_similarity",
    "score", "alternates", "fetched_at", "error",
]
NY_EXTRAS = [
    "county",                 # NY-specific: county of registration
    "ceo_name",               # NY-specific: CEO if filed (NY's owner-equivalent field)
    "ceo_address",
    "dos_process_name",       # process server name
    "location_address",       # actual business location
]
COLS = [PREFIX + c for c in UNIVERSAL] + [PREFIX + c for c in NY_EXTRAS]


def blank_row():
    return {k: "" for k in COLS}


# ---------------------------------------------------------------------------
# Logger (write to stdout + bulk/download.log per skill convention)
# ---------------------------------------------------------------------------

_LOG_FILE = None

def _make_logger(log_path=LOG_PATH):
    global _LOG_FILE
    BULK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _LOG_FILE = open(log_path, "a", encoding="utf-8", buffering=1)

    def log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        _LOG_FILE.write(line + "\n")
        _LOG_FILE.flush()
    return log


# ---------------------------------------------------------------------------
# Name matching helpers (same shape as FL/CA — kept inline per "self-contained
# state artifact" convention; resist the urge to extract to shared/ until 3+ states agree)
# ---------------------------------------------------------------------------

def normalize_for_similarity(name):
    s = (name or "").lower()
    s = re.sub(r"\([^)]*\)", " ", s)
    # Treat "&" and "and" identically — different state filings encode them differently
    # (NY dataset has "&", FDD parser inputs often have "And" or "and").
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


# ---------------------------------------------------------------------------
# Resilient HTTPS download (skill convention: resume / retry / fsync / log)
# ---------------------------------------------------------------------------

def need_refresh(path, max_age_days=REFRESH_AFTER_DAYS):
    if not path.exists():
        return True
    age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    return age > timedelta(days=max_age_days)


def download_bulk_via_https(log=None):
    """Download NY active-corporations CSV with resume + retry + fsync.
    Mirrors the FL SFTP downloader's resilience pattern but over HTTPS."""
    if log is None:
        log = _make_logger()

    BULK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    MAX_ATTEMPTS = 50
    attempt = 0
    last_progress_bytes = -1
    stuck_attempts = 0

    while True:
        attempt += 1
        existing = BULK_CSV_PATH.stat().st_size if BULK_CSV_PATH.exists() else 0

        if existing == last_progress_bytes:
            stuck_attempts += 1
            if stuck_attempts >= 5:
                log(f"FATAL: 5 consecutive zero-progress retries. Aborting at {existing/1e9:.2f} GB.")
                sys.exit(1)
            backoff = min(60, 2 ** stuck_attempts)
            log(f"No progress; backing off {backoff}s before retry {attempt} ...")
            time.sleep(backoff)
        else:
            stuck_attempts = 0
        last_progress_bytes = existing

        log(f"Attempt {attempt}: GET {DOWNLOAD_URL}")
        log(f"  existing on disk: {existing/1e9:.2f} GB")

        headers = {"Range": f"bytes={existing}-"} if existing else {}
        try:
            r = requests.get(DOWNLOAD_URL, headers=headers, stream=True, timeout=60)
        except requests.RequestException as e:
            log(f"  connection failed: {type(e).__name__}: {e}")
            continue

        if r.status_code == 206:
            cl = int(r.headers.get("Content-Length", 0))
            remote_size = existing + cl
            mode = "ab"
            log(f"  206 Partial Content; remote total ~{remote_size/1e9:.2f} GB; need {cl/1e9:.2f} GB more")
        elif r.status_code == 200:
            cl = int(r.headers.get("Content-Length", 0))
            remote_size = cl if cl else 0
            if existing and r.headers.get("Accept-Ranges", "").lower() != "bytes":
                log(f"  200 OK (server doesn't honor Range); restarting from byte 0")
                # Server doesn't support resume; truncate and restart
                BULK_CSV_PATH.unlink(missing_ok=True)
                existing = 0
            mode = "wb" if existing == 0 else "ab"
            log(f"  200 OK; remote total ~{remote_size/1e9:.2f} GB" if remote_size else "  200 OK; size unknown (chunked)")
        else:
            log(f"  unexpected status {r.status_code}; aborting attempt")
            r.close()
            continue

        bytes_read = existing
        chunk_count = 0
        last_log_bytes = existing
        last_log_time = datetime.now()
        start_time = datetime.now()
        start_bytes = existing
        clean_finish = False

        try:
            with open(BULK_CSV_PATH, mode) as dst:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    dst.write(chunk)
                    chunk_count += 1
                    bytes_read += len(chunk)
                    if chunk_count % 8 == 0:
                        dst.flush()
                        try: os.fsync(dst.fileno())
                        except Exception: pass
                    now = datetime.now()
                    if (bytes_read - last_log_bytes >= 25 * 1024 * 1024
                            or (now - last_log_time).total_seconds() >= 15):
                        elapsed = (now - start_time).total_seconds() or 0.001
                        kbps = (bytes_read - start_bytes) / 1024 / elapsed
                        if remote_size:
                            pct = bytes_read * 100 / remote_size
                            eta_sec = (remote_size - bytes_read) / max(kbps * 1024, 1)
                            eta_min = int(eta_sec // 60)
                            log(f"  {bytes_read/1e9:.2f}/{remote_size/1e9:.2f} GB ({pct:.1f}%) — {kbps:.0f} KB/s — ETA {eta_min}m")
                        else:
                            log(f"  {bytes_read/1e9:.2f} GB — {kbps:.0f} KB/s")
                        last_log_bytes = bytes_read
                        last_log_time = now
                clean_finish = True
        except (requests.RequestException, OSError, ConnectionError) as e:
            log(f"  connection dropped after {(bytes_read-existing)/1e6:.1f} MB this attempt: {type(e).__name__}: {e}")
        finally:
            r.close()

        on_disk = BULK_CSV_PATH.stat().st_size if BULK_CSV_PATH.exists() else 0
        log(f"  saved to disk: {on_disk/1e9:.2f} GB")
        if clean_finish and (not remote_size or on_disk >= remote_size):
            log(f"Download complete: {on_disk/1e9:.2f} GB")
            return
        if attempt >= MAX_ATTEMPTS:
            log(f"FATAL: hit max attempts ({MAX_ATTEMPTS}). Aborting.")
            sys.exit(1)
        log("Will retry from current offset ...")


def ensure_bulk_file(bulk_file_arg, log=print):
    if bulk_file_arg:
        p = Path(bulk_file_arg)
        if not p.exists():
            sys.exit(f"ERROR: --bulk-file path does not exist: {p}")
        return p
    if need_refresh(BULK_CSV_PATH):
        log("Bulk cache stale or missing — downloading via HTTPS")
        download_bulk_via_https(log=log)
    return BULK_CSV_PATH


# ---------------------------------------------------------------------------
# Index build from CSV
# ---------------------------------------------------------------------------

def _normalize_header(h):
    """data.ny.gov bulk export uses Title Case Spaced headers (e.g., 'Current Entity Name'),
    while the SODA API uses snake_case field IDs (e.g., 'current_entity_name'). Normalize
    both to the same lowercased-underscored shape so the rest of the code is source-agnostic."""
    return re.sub(r"[^\w]+", "_", (h or "").strip().lower()).strip("_")


def stream_match_all(input_rows, bulk_path, log=print):
    """Single-pass stream over the bulk CSV. Returns dict: input_name -> list of (record, query_used).

    NY's active corps dataset is 4.2M records × 30 fields ≈ 6 GB if held in memory as a dict,
    which crashes with MemoryError. Instead, build a lookup of normalized input-names + variants
    once (small, O(inputs)), then scan the CSV once and only KEEP records that match the lookup.
    Memory bounded by matches found, not by the dataset size.
    """
    lookup = {}  # normalized_name -> (original_input_name, query_that_matched)
    for r in input_rows:
        name = (r.get("entity_name") or "").strip()
        if not name:
            continue
        for q in [name] + name_variants(name):
            norm = normalize_for_similarity(q)
            if norm and norm not in lookup:
                lookup[norm] = (name, q)
    log(f"Built lookup table: {len(lookup)} normalized queries from {len(input_rows)} input rows")

    matches = defaultdict(list)  # input_name -> [(record_dict, query_used)]
    n_records = 0
    log(f"Streaming {bulk_path} ({bulk_path.stat().st_size/1e9:.2f} GB) ...")
    with open(bulk_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        try:
            raw_headers = next(reader)
        except StopIteration:
            log("  empty CSV!")
            return {}
        headers = [_normalize_header(h) for h in raw_headers]
        if "current_entity_name" not in headers:
            log(f"  WARNING: 'current_entity_name' missing from headers; got: {headers[:8]}...")
        cname_idx = headers.index("current_entity_name") if "current_entity_name" in headers else -1
        for raw_row in reader:
            n_records += 1
            if cname_idx >= 0 and cname_idx < len(raw_row):
                cname = (raw_row[cname_idx] or "").strip()
                norm = normalize_for_similarity(cname)
                if norm and norm in lookup:
                    input_name, query = lookup[norm]
                    matches[input_name].append((dict(zip(headers, raw_row)), query))
            if n_records % 500_000 == 0:
                log(f"  ... {n_records:,} records scanned, {sum(len(v) for v in matches.values())} candidate matches so far")
    total_candidates = sum(len(v) for v in matches.values())
    log(f"Scan complete: {n_records:,} records, {total_candidates} candidate matches across {len(matches)} inputs")
    return matches


# ---------------------------------------------------------------------------
# Match logic
# ---------------------------------------------------------------------------

def score_record(input_name, rec, input_implies_oos=None):
    score = 100.0  # all rows are active by definition (Active Corporations dataset)

    sim = name_similarity(input_name, rec.get("current_entity_name") or "")
    score += sim * 100

    # Foreign / out-of-state entities (jurisdiction != "New York")
    jur = (rec.get("jurisdiction") or "").lower()
    is_oos = jur and "new york" not in jur
    if input_implies_oos is None:
        input_implies_oos = bool(OUT_OF_STATE_HINTS.search(input_name))
    if is_oos and not input_implies_oos:
        score -= 30

    # Newer entities slight preference
    fd = rec.get("initial_dos_filing_date") or ""
    if fd:
        try:
            dt = datetime.strptime(fd[:10], "%Y-%m-%d")
            score += (dt - datetime(2000, 1, 1)).days * 0.001
        except Exception:
            pass

    return score


def pick_best_match(input_name, candidates_list):
    """Given the (record, query_used) pairs from stream_match_all for one input,
    score them and return (best_record, best_score, alternates, query_used, attempts).
    candidates_list: list of (record_dict, query_that_matched)."""
    if not candidates_list:
        return None, 0.0, [], input_name, [input_name]

    input_implies_oos = bool(OUT_OF_STATE_HINTS.search(input_name))
    scored = sorted(
        ((score_record(input_name, rec, input_implies_oos), rec, q)
         for rec, q in candidates_list),
        key=lambda x: -x[0],
    )
    best_score, best, best_query = scored[0]
    alternates = [
        {"name": r.get("current_entity_name", ""),
         "num": r.get("dos_id", ""),
         "score": round(s, 2)}
        for s, r, _ in scored[1:6]
    ]
    attempts = list(dict.fromkeys([q for _, _, q in scored]))  # unique queries that produced hits
    return best, best_score, alternates, best_query, attempts


# ---------------------------------------------------------------------------
# Build output row from a matched NY record
# ---------------------------------------------------------------------------

def _addr_join(*parts):
    return ", ".join(p.strip() for p in parts if p and p.strip())


def record_to_row(rec, score, alternates, query_used, query_attempts, input_name):
    out = blank_row()
    if rec is None:
        out[PREFIX + "match_count"] = 0
        out[PREFIX + "query_used"] = query_used
        out[PREFIX + "query_attempts"] = " | ".join(query_attempts)
        return out

    name = rec.get("current_entity_name", "")
    fd = (rec.get("initial_dos_filing_date") or "")[:10]
    formation_date = ""
    if fd:
        try:
            dt = datetime.strptime(fd, "%Y-%m-%d")
            formation_date = dt.strftime("%m/%d/%Y")
        except Exception:
            formation_date = fd

    ceo_name = (rec.get("ceo_name") or "").strip()
    ceo_addr = _addr_join(
        rec.get("ceo_address_1"), rec.get("ceo_address_2"),
        rec.get("ceo_city"), rec.get("ceo_state"), rec.get("ceo_zip"),
    )
    agent_name = (rec.get("registered_agent_name") or "").strip()
    agent_addr = _addr_join(
        rec.get("registered_agent_address_1"), rec.get("registered_agent_address_2"),
        rec.get("registered_agent_city"), rec.get("registered_agent_state"), rec.get("registered_agent_zip"),
    )
    location_addr = _addr_join(
        rec.get("location_address_1"), rec.get("location_address_2"),
        rec.get("location_city"), rec.get("location_state"), rec.get("location_zip"),
    )
    process_name = (rec.get("dos_process_name") or "").strip()
    process_addr = _addr_join(
        rec.get("dos_process_address_1"), rec.get("dos_process_address_2"),
        rec.get("dos_process_city"), rec.get("dos_process_state"), rec.get("dos_process_zip"),
    )

    # Principal address: prefer location_address (actual business location); fall back to dos_process_address.
    principal_addr = location_addr or process_addr
    # Mailing: NY doesn't expose a separate mailing field; leave blank unless we have process_addr distinct from location.
    mailing_addr = process_addr if (process_addr and process_addr != principal_addr) else ""

    # If we have an agent, prefer that as agent_*. Otherwise dos_process is the de-facto agent.
    if not agent_name:
        agent_name = process_name
    if not agent_addr:
        agent_addr = process_addr

    addr_match_signal = bool(agent_addr and principal_addr and addresses_match(agent_addr, principal_addr))

    # Likely owner: ceo_name takes priority (it's the named owner/officer in NY filings).
    # Otherwise, if agent appears to be an individual (heuristic: not LLC/Inc/Corp suffix) and address matches principal, use agent.
    likely_owner = ""
    agent_is_individual = False
    if ceo_name:
        likely_owner = ceo_name
    elif agent_name and not re.search(r"\b(llc|inc|corp|service|registered|agent|company|co\.?)\b", agent_name, re.IGNORECASE):
        agent_is_individual = True
        if addr_match_signal:
            likely_owner = agent_name

    # Operator pattern
    if re.search(r"\b(esq|attorney|law)\b", agent_name, re.IGNORECASE):
        pattern = "attorney_agent"
    elif "FOREIGN" in (rec.get("entity_type") or "").upper():
        pattern = "out_of_state_holdco"
    elif ceo_name:
        pattern = "owner_operator"
    elif agent_is_individual and addr_match_signal:
        pattern = "owner_operator"
    elif agent_is_individual:
        pattern = "individual_agent_offsite"
    elif agent_name:
        pattern = "service_agent"
    else:
        pattern = "unknown"

    out[PREFIX + "match_count"] = 1
    out[PREFIX + "first_result_name"] = name
    out[PREFIX + "entity_number"] = rec.get("dos_id", "")
    out[PREFIX + "status"] = "Active"  # implicit — this is the active dataset
    out[PREFIX + "entity_type"] = rec.get("entity_type", "")
    out[PREFIX + "formation_date"] = formation_date
    out[PREFIX + "jurisdiction"] = rec.get("jurisdiction", "")
    out[PREFIX + "agent_name"] = agent_name
    out[PREFIX + "agent_type"] = "Individual" if agent_is_individual else ""
    out[PREFIX + "agent_is_individual"] = "True" if agent_is_individual else "False"
    out[PREFIX + "agent_address"] = agent_addr
    out[PREFIX + "principal_address"] = principal_addr
    out[PREFIX + "mailing_address"] = mailing_addr
    out[PREFIX + "agent_address_matches_principal"] = "True" if addr_match_signal else ("False" if (agent_addr and principal_addr) else "")
    out[PREFIX + "likely_owner_name"] = likely_owner
    out[PREFIX + "operator_pattern"] = pattern
    out[PREFIX + "score"] = f"{score:.2f}"
    out[PREFIX + "name_similarity"] = f"{name_similarity(input_name, name):.3f}"
    out[PREFIX + "alternates"] = json.dumps(alternates, ensure_ascii=False) if alternates else ""
    out[PREFIX + "query_used"] = query_used
    out[PREFIX + "query_attempts"] = " | ".join(query_attempts)
    out[PREFIX + "county"] = rec.get("county", "")
    out[PREFIX + "ceo_name"] = ceo_name
    out[PREFIX + "ceo_address"] = ceo_addr
    out[PREFIX + "dos_process_name"] = process_name
    out[PREFIX + "location_address"] = location_addr

    return out


# ---------------------------------------------------------------------------
# Cross-row pass
# ---------------------------------------------------------------------------

def cross_row_pass(rows):
    addr_counter = Counter()
    owner_counter = Counter()
    for r in rows:
        pa = normalize_address(r.get(PREFIX + "principal_address") or "")
        if pa:
            addr_counter[pa] += 1
        owner = (r.get(PREFIX + "likely_owner_name") or "").strip().lower()
        if owner:
            owner_counter[owner] += 1
    for r in rows:
        pa = normalize_address(r.get(PREFIX + "principal_address") or "")
        owner = (r.get(PREFIX + "likely_owner_name") or "").strip().lower()
        is_multi = (pa and addr_counter[pa] > 1) or (owner and owner_counter[owner] > 1)
        r[PREFIX + "multi_entity_operator"] = "True" if is_multi else ("False" if (pa or owner) else "")


# ---------------------------------------------------------------------------
# CSV I/O
# ---------------------------------------------------------------------------

def load_input(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return rows, list(reader.fieldnames or [])


def load_existing_output(path):
    if not Path(path).exists():
        return set(), []
    done = set()
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
            name = (row.get("entity_name") or "").strip().lower()
            if name and (row.get(PREFIX + "fetched_at") or "").strip():
                done.add(name)
    return done, rows


def rescore_only(output_path, log=print):
    if not Path(output_path).exists():
        sys.exit(f"ERROR: --rescore-only requires existing output at {output_path}")
    with open(output_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    cross_row_pass(rows)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    log(f"Rescored {len(rows)} rows.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input_csv")
    ap.add_argument("output_csv")
    ap.add_argument("--bulk-file", help="Path to manually-downloaded NY active_corps.csv (skips HTTPS)")
    ap.add_argument("--limit", type=int, default=0, help="Cap rows processed this run (0 = no cap)")
    ap.add_argument("--rescore-only", action="store_true",
                    help="Re-run cross-row + derived columns without re-matching")
    args = ap.parse_args()

    if args.rescore_only:
        rescore_only(args.output_csv)
        return

    log = _make_logger(LOG_PATH.parent.parent / "enrich.log")
    log(f"Run started — input={args.input_csv}, output={args.output_csv}")

    input_rows, input_fields = load_input(args.input_csv)
    if "entity_name" not in input_fields:
        sys.exit("ERROR: input CSV must have a column named 'entity_name'")
    log(f"Loaded {len(input_rows)} input rows")

    done_names, _existing_rows = load_existing_output(args.output_csv)
    if done_names:
        log(f"Resuming: {len(done_names)} rows already complete")
    output_fields = list(input_fields) + [c for c in COLS if c not in input_fields]

    bulk_path = ensure_bulk_file(args.bulk_file, log=log)

    # Filter to only rows that need processing (so the stream pass only looks for the 554-N already-done)
    rows_todo = [r for r in input_rows
                 if (r.get("entity_name") or "").strip() and
                 (r.get("entity_name") or "").strip().lower() not in done_names]
    if args.limit:
        rows_todo = rows_todo[:args.limit]
    log(f"Will match {len(rows_todo)} rows ({len(input_rows) - len(rows_todo)} already done or empty)")

    # Single-pass stream over the bulk CSV for all inputs at once (memory-bounded by matches found)
    all_matches = stream_match_all(rows_todo, bulk_path, log=log)

    output_path = Path(args.output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_exists = output_path.exists()
    fout = open(output_path, "a" if output_exists else "w", newline="", encoding="utf-8")
    writer = csv.DictWriter(fout, fieldnames=output_fields, extrasaction="ignore")
    if not output_exists:
        writer.writeheader()
        fout.flush()

    processed = 0
    try:
        for row in rows_todo:
            name = (row.get("entity_name") or "").strip()
            candidates = all_matches.get(name, [])
            best, best_score, alternates, query_used, attempts = pick_best_match(name, candidates)
            out = record_to_row(best, best_score, alternates, query_used, attempts, name)
            out[PREFIX + "fetched_at"] = datetime.now(timezone.utc).isoformat()

            merged = {**row, **out}
            writer.writerow(merged)
            fout.flush()
            processed += 1

            status = f"matches={out[PREFIX + 'match_count']}"
            if out.get(PREFIX + "first_result_name"):
                status += f" | {out[PREFIX + 'first_result_name'][:50]}"
            if out.get(PREFIX + "likely_owner_name"):
                status += f" | owner: {out[PREFIX + 'likely_owner_name']}"
            log(f"[{processed}/{len(rows_todo)}] {name[:60]} -> {status}")
    finally:
        fout.close()
    skipped = len(input_rows) - len(rows_todo)

    log(f"Match phase done: processed={processed}, skipped={skipped}")
    log("Running cross-row analysis ...")
    rescore_only(args.output_csv, log=log)
    log(f"Output: {output_path.resolve()}")


if __name__ == "__main__":
    main()
