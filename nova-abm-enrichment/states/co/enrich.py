"""
states/co/enrich.py — match franchisee LLCs against Colorado's Business Entities dataset

Bulk source: data.colorado.gov (Socrata Open Data portal)
Dataset:     "Business Entities in Colorado" — dataset ID 4ykn-tg5h
URL:         https://data.colorado.gov/api/views/4ykn-tg5h/rows.csv?accessType=DOWNLOAD
Format:      CSV with quoted fields
Records:     ~3 million entities (active + inactive)

Why bulk over scrape: HTTPS, free, no anti-bot fight. CO's data is structured
better than NY's — agent name is broken into first/middle/last/suffix, and
status is a real field ("Good Standing"/"Delinquent"/"Voluntarily Dissolved").

Install: pip install requests

Run:    python states/co/enrich.py states/co/input/franchisees.csv states/co/output/enriched.csv
Rescore: same with --rescore-only

Notes:
    * Resumable via co_fetched_at on existing output rows.
    * Bulk file cached at states/co/bulk/business_entities.csv; refresh after 30 days.
    * CO entityname can have status appended ("ACME LLC, Delinquent May 1, 2016") —
      strip the trailing ", <Status> <Date>" before similarity matching.
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

PREFIX = "co_"

DOWNLOAD_URL = "https://data.colorado.gov/api/views/4ykn-tg5h/rows.csv?accessType=DOWNLOAD"
BULK_CACHE_DIR = Path(__file__).parent / "bulk"
BULK_CSV_PATH = BULK_CACHE_DIR / "business_entities.csv"
LOG_PATH = BULK_CACHE_DIR / "download.log"
REFRESH_AFTER_DAYS = 30

# Out-of-state hints for THIS state's matcher: don't include "colorado" itself,
# since input names containing "Colorado" are perfectly compatible with CO data.
OUT_OF_STATE_HINTS = re.compile(
    r"\b(dakot|rocky\s*mountain|new\s*england|texas|california|florida|nevada|"
    r"arizona|oregon|washington|illinois|midwest|northwest|southeast|northeast|"
    r"hawaii|atlantic|rockies)\b",
    re.IGNORECASE,
)

# CO entityname sometimes has status appended (e.g. "ACME LLC, Delinquent May 1, 2016")
CO_NAME_STATUS_SUFFIX = re.compile(
    r",\s*(Delinquent|Dissolved|Voluntarily Dissolved|Administratively Dissolved|Withdrawn|Merged|Expired)(\s+[A-Za-z]+\s+\d+,?\s+\d{4})?\s*$",
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
CO_EXTRAS = [
    "agent_first_name",         # CO publishes structured agent names
    "agent_middle_name",
    "agent_last_name",
    "agent_organization_name",  # filled when agent is a corporation
]
COLS = [PREFIX + c for c in UNIVERSAL] + [PREFIX + c for c in CO_EXTRAS]


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
    while the SODA API uses snake_case field IDs (e.g., 'entityname'). Normalize
    both to the same lowercased-underscored shape so the rest of the code is source-agnostic."""
    return re.sub(r"[^\w]+", "_", (h or "").strip().lower()).strip("_")


def _strip_co_status_suffix(name):
    """CO entityname can have ', Delinquent May 1 2016' style suffix appended.
    Strip it before normalization so similarity matching isn't poisoned."""
    if not name:
        return name
    return CO_NAME_STATUS_SUFFIX.sub("", name).strip()


def stream_match_all(input_rows, bulk_path, log=print):
    """Single-pass stream over the bulk CSV. See NY's enrich.py for rationale on why
    we build a small lookup of inputs and scan once instead of indexing the whole dataset."""
    lookup = {}
    for r in input_rows:
        name = (r.get("entity_name") or "").strip()
        if not name:
            continue
        for q in [name] + name_variants(name):
            norm = normalize_for_similarity(q)
            if norm and norm not in lookup:
                lookup[norm] = (name, q)
    log(f"Built lookup table: {len(lookup)} normalized queries from {len(input_rows)} input rows")

    matches = defaultdict(list)
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
        if "entityname" not in headers:
            log(f"  WARNING: 'entityname' missing from headers; got: {headers[:8]}...")
        cname_idx = headers.index("entityname") if "entityname" in headers else -1
        for raw_row in reader:
            n_records += 1
            if cname_idx >= 0 and cname_idx < len(raw_row):
                cname = _strip_co_status_suffix((raw_row[cname_idx] or "").strip())
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
    """CO has a real status field (entitystatus). Score:
        Good Standing: +100
        Compliant / Active: +80
        Delinquent: 0
        Voluntarily Dissolved / Withdrawn / Expired: -50
        Administratively Dissolved: -75
    """
    status = (rec.get("entitystatus") or "").lower()
    if "good standing" in status:
        score = 100.0
    elif "compliant" in status or "active" in status:
        score = 80.0
    elif "delinquent" in status:
        score = 0.0
    elif "administratively" in status:
        score = -75.0
    elif any(w in status for w in ("dissolved", "withdrawn", "expired", "merged")):
        score = -50.0
    else:
        score = 50.0  # unknown status — neutral

    # Use the cleaned entityname (without status suffix) for similarity
    clean_name = _strip_co_status_suffix(rec.get("entityname") or "")
    sim = name_similarity(input_name, clean_name)
    score += sim * 100

    # Foreign / out-of-state: CO entitytype is FLLC/FPC/etc. for foreign, DLLC/DPC for domestic
    etype = (rec.get("entitytype") or "").upper()
    is_oos = etype.startswith("F")
    if input_implies_oos is None:
        input_implies_oos = bool(OUT_OF_STATE_HINTS.search(input_name))
    if is_oos and not input_implies_oos:
        score -= 30

    # Newer entities slight preference
    fd = rec.get("entityformdate") or ""
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
        {"name": r.get("entityname", ""),
         "num": r.get("entityid", ""),
         "score": round(s, 2)}
        for s, r, _ in scored[1:6]
    ]
    attempts = list(dict.fromkeys([q for _, _, q in scored]))  # unique queries that produced hits
    return best, best_score, alternates, best_query, attempts


# ---------------------------------------------------------------------------
# Build output row from a matched CO record
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

    raw_name = rec.get("entityname", "")
    name = _strip_co_status_suffix(raw_name)  # removes ", Delinquent May 1, 2016" style suffix
    fd = (rec.get("entityformdate") or "")[:10]
    formation_date = ""
    if fd:
        try:
            dt = datetime.strptime(fd, "%Y-%m-%d")
            formation_date = dt.strftime("%m/%d/%Y")
        except Exception:
            formation_date = fd

    # CO has structured agent name (first/middle/last/suffix) for individual agents,
    # OR an organization name when the agent is a corporation.
    agent_first = (rec.get("agentfirstname") or "").strip()
    agent_middle = (rec.get("agentmiddlename") or "").strip()
    agent_last = (rec.get("agentlastname") or "").strip()
    agent_suffix = (rec.get("agentsuffix") or "").strip()
    agent_org = (rec.get("agentorganizationname") or "").strip()

    individual_name_parts = [p for p in (agent_first, agent_middle, agent_last) if p]
    if agent_suffix:
        individual_name_parts.append(agent_suffix)
    individual_agent_name = " ".join(individual_name_parts)

    agent_is_individual = bool(individual_agent_name) and not agent_org
    agent_name = individual_agent_name if agent_is_individual else agent_org

    agent_addr = _addr_join(
        rec.get("agentprincipaladdress1"), rec.get("agentprincipaladdress2"),
        rec.get("agentprincipalcity"), rec.get("agentprincipalstate"), rec.get("agentprincipalzipcode"),
    )

    principal_addr = _addr_join(
        rec.get("principaladdress1"), rec.get("principaladdress2"),
        rec.get("principalcity"), rec.get("principalstate"), rec.get("principalzipcode"),
    )
    mailing_addr = _addr_join(
        rec.get("mailingaddress1"), rec.get("mailingaddress2"),
        rec.get("mailingcity"), rec.get("mailingstate"), rec.get("mailingzipcode"),
    )

    addr_match_signal = bool(agent_addr and principal_addr and addresses_match(agent_addr, principal_addr))

    # Likely owner: when agent is a NAMED individual, that's our best owner signal.
    # CO doesn't have a separate CEO/officer field in this dataset, so individual agents
    # are the primary lead — especially when their address matches the principal address.
    likely_owner = ""
    if agent_is_individual:
        if addr_match_signal:
            likely_owner = individual_agent_name
        elif not re.search(r"\b(esq|attorney|law)\b", individual_agent_name, re.IGNORECASE):
            # Off-site individual agent — probably the owner OR a family member, less certain
            likely_owner = individual_agent_name + " (offsite agent)"

    # Operator pattern
    etype = (rec.get("entitytype") or "").upper()
    if re.search(r"\b(esq|attorney|law)\b", agent_name, re.IGNORECASE):
        pattern = "attorney_agent"
    elif etype.startswith("F"):  # Foreign LLC/Corp/etc. = registered out-of-state
        pattern = "out_of_state_holdco"
    elif agent_is_individual and addr_match_signal:
        pattern = "owner_operator"
    elif agent_is_individual:
        pattern = "individual_agent_offsite"
    elif agent_name:
        pattern = "service_agent"
    else:
        pattern = "unknown"

    # Map CO entitytype codes to readable values for the output column.
    ETYPE_DESC = {
        "DLLC": "Domestic LLC", "FLLC": "Foreign LLC",
        "DPC": "Domestic Profit Corp", "FPC": "Foreign Profit Corp",
        "DNP": "Domestic Nonprofit", "FNP": "Foreign Nonprofit",
        "DLP": "Domestic LP", "FLP": "Foreign LP",
        "DLLP": "Domestic LLP", "FLLP": "Foreign LLP",
    }
    entity_type_readable = ETYPE_DESC.get(etype, etype)

    out[PREFIX + "match_count"] = 1
    out[PREFIX + "first_result_name"] = name
    out[PREFIX + "entity_number"] = rec.get("entityid", "")
    out[PREFIX + "status"] = rec.get("entitystatus", "")
    out[PREFIX + "entity_type"] = entity_type_readable
    out[PREFIX + "formation_date"] = formation_date
    out[PREFIX + "jurisdiction"] = rec.get("jurisdictonofformation", "")
    out[PREFIX + "agent_name"] = agent_name
    out[PREFIX + "agent_type"] = "Individual" if agent_is_individual else ("Corporation" if agent_org else "")
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
    out[PREFIX + "agent_first_name"] = agent_first
    out[PREFIX + "agent_middle_name"] = agent_middle
    out[PREFIX + "agent_last_name"] = agent_last
    out[PREFIX + "agent_organization_name"] = agent_org

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
