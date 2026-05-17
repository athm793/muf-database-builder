"""
states/fl/enrich.py — match franchisee LLCs against Florida Sunbiz bulk corporate data

Bulk source: SFTP sftp.floridados.gov  (Public / PubAccess1845!)
Path on server: /Public/doc/Quarterly/Cor/cordata.zip  (~1.7 GB zipped)
Format: fixed-width text, 1440 chars/record. Schema: https://dos.sunbiz.org/data-definitions/cor.html
Refresh: quarterly (Jan/Apr/Jul/Oct), free.

Why bulk over scrape: Sunbiz officer/director names + addresses are EMBEDDED in the
bulk record (up to 6 officers per entity). For franchise-operator outreach this
is gold — we get a likely owner name directly without drilling into a detail page.

Install (once, in project venv):
    pip install paramiko

Run (auto-download bulk on first use, then match):
    python states/fl/enrich.py states/fl/input/franchisees.csv states/fl/output/enriched.csv

Run with a manually-downloaded bulk file (skip SFTP):
    python states/fl/enrich.py states/fl/input/franchisees.csv states/fl/output/enriched.csv --bulk-file path/to/cordata.txt

Re-derive intelligence columns without re-matching:
    python states/fl/enrich.py states/fl/input/franchisees.csv states/fl/output/enriched.csv --rescore-only

Notes:
    * Resumable: rows where fl_fetched_at is filled are skipped on re-run.
    * Bulk file cached at states/fl/bulk/cordata.txt; refreshed if older than 90 days.
    * Output schema follows the universal contract documented in
      ~/.claude/skills/build-state-sos-scraper/SKILL.md.
"""
import argparse
import csv
import json
import re
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PREFIX = "fl_"

SFTP_HOST = "sftp.floridados.gov"
SFTP_USER = "Public"
SFTP_PASS = "PubAccess1845!"
SFTP_PATH = "/Public/doc/Quarterly/Cor/cordata.zip"

BULK_CACHE_DIR = Path(__file__).parent / "bulk"
BULK_TXT_PATH = BULK_CACHE_DIR / "cordata.txt"
BULK_ZIP_PATH = BULK_CACHE_DIR / "cordata.zip"
REFRESH_AFTER_DAYS = 90  # quarterly cadence

OUT_OF_STATE_HINTS = re.compile(
    r"\b(colorado|dakot|rocky\s*mountain|new\s*england|texas|california|nevada|"
    r"arizona|oregon|washington|illinois|midwest|northwest|southeast|northeast|"
    r"hawaii|atlantic|rockies|northern|southern)\b",
    re.IGNORECASE,
)

# Universal output schema (must stay aligned with skill's contract)
UNIVERSAL = [
    "match_count", "first_result_name", "entity_number", "status", "entity_type",
    "formation_date", "jurisdiction", "agent_name", "agent_type", "agent_is_individual",
    "agent_address", "principal_address", "mailing_address",
    "statement_due_date", "inactive_date",
    "agent_address_matches_principal", "likely_owner_name", "operator_pattern",
    "multi_entity_operator", "query_used", "query_attempts", "name_similarity",
    "score", "alternates", "fetched_at", "error",
]
# FL-specific extras (after universal columns)
FL_EXTRAS = [
    "officers_json",      # JSON list of all officers in the record
    "fei_number",         # Federal EIN
    "filing_type",        # DOMP / DOMNP / FORP / etc.
]
COLS = [PREFIX + c for c in UNIVERSAL] + [PREFIX + c for c in FL_EXTRAS]


def blank_row():
    return {k: "" for k in COLS}


# ---------------------------------------------------------------------------
# Name matching helpers (adapted from CA bizfile_enrich.py — kept inline so this
# state's script is self-contained per project convention)
# ---------------------------------------------------------------------------

def normalize_for_similarity(name):
    s = (name or "").lower()
    s = re.sub(r"\([^)]*\)", " ", s)
    # Treat "&" and "and" identically — different sources encode them differently
    # (NY dataset has "&", FDD parser inputs often have "And").
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
# Cordata fixed-width record parsing
# ---------------------------------------------------------------------------
# All offsets are 0-indexed Python slices, derived from the 1-indexed schema at
# https://dos.sunbiz.org/data-definitions/cor.html. Record length is 1440 chars.

OFFICER_TITLE_MAP = {
    "P": "President",
    "T": "Treasurer",
    "C": "Chairman",
    "V": "Vice President",
    "S": "Secretary",
    "D": "Director",
}

# Officer slot is 128 chars: 4 (title) + 1 (type) + 42 (name) + 42 (addr) + 28 (city) + 2 (state) + 9 (zip+4)
OFFICER_START_POSITIONS = [668, 796, 924, 1052, 1180, 1308]  # 0-indexed starts of each officer slot


def parse_cordata_record(line):
    """Parse one 1440-char cordata record. Returns dict or None if malformed."""
    if len(line) < 1300:  # truncated/blank line
        return None

    def f(start, length):
        return line[start:start + length].strip()

    rec = {
        "entity_number": f(0, 12),
        "name": f(12, 192),
        "status": "Active" if f(204, 1) == "A" else "Inactive",
        "filing_type": f(205, 15),
        "principal_addr_1": f(220, 42),
        "principal_addr_2": f(262, 42),
        "principal_city": f(304, 28),
        "principal_state": f(332, 2),
        "principal_zip": f(334, 10),
        "principal_country": f(344, 2),
        "mail_addr_1": f(346, 42),
        "mail_addr_2": f(388, 42),
        "mail_city": f(430, 28),
        "mail_state": f(458, 2),
        "mail_zip": f(460, 10),
        "mail_country": f(470, 2),
        "file_date": f(472, 8),       # CCYYMMDD
        "fei_number": f(480, 14),
        "more_than_six_officers": f(494, 1) == "Y",
        "last_transaction_date": f(495, 8),
        "agent_name": f(544, 42),
        "agent_type": f(586, 1),       # P=Person, C=Corporation
        "agent_addr": f(587, 42),
        "agent_city": f(629, 28),
        "agent_state": f(657, 2),
        "agent_zip": f(659, 9),
    }

    officers = []
    for start in OFFICER_START_POSITIONS:
        title_code = line[start:start + 4].strip()
        type_code = line[start + 4:start + 5].strip()
        name = line[start + 5:start + 47].strip()
        if not name:
            continue
        officers.append({
            "title_code": title_code,
            "title": OFFICER_TITLE_MAP.get(title_code, title_code),
            "type": "Person" if type_code == "P" else ("Corporation" if type_code == "C" else type_code),
            "name": name,
            "addr": line[start + 47:start + 89].strip(),
            "city": line[start + 89:start + 117].strip(),
            "state": line[start + 117:start + 119].strip(),
            "zip": line[start + 119:start + 128].strip(),
        })
    rec["officers"] = officers
    return rec


def format_address_line(addr1, addr2, city, state, zip_):
    parts = []
    if addr1:
        parts.append(addr1)
    if addr2:
        parts.append(addr2)
    csz = ", ".join(p for p in (city, state) if p)
    if csz:
        if zip_:
            csz = f"{csz} {zip_}"
        parts.append(csz)
    return ", ".join(parts)


def format_file_date(s):
    """Sunbiz cordata file_date is MMDDCCYY (verified empirically: '01271984' for
    Sailormen Inc. founded 1/27/1984; '12022020' for entities formed in 2020).
    The schema PDF doesn't specify the order, but actual data is MMDDCCYY.
    Convert to MM/DD/YYYY (universal schema convention)."""
    if not s or len(s) != 8 or not s.isdigit():
        return ""
    return f"{s[0:2]}/{s[2:4]}/{s[4:8]}"


# ---------------------------------------------------------------------------
# Build search index from cordata
# ---------------------------------------------------------------------------

def stream_match_all(input_rows, bulk_zip_path, log=print):
    """Stream-match across ALL cordata shards in the zip without ever holding the full
    dataset in memory. cordata.zip contains 10+ shards (cordata0.txt..cordata9.txt),
    each ~1.8 GB uncompressed. Total ~18 GB — would OOM if we built a Python dict.

    Returns: dict {input_name: [(record_dict, query_used), ...]}.
    """
    _patch_zipfile_for_deflate64()

    # Build small lookup of normalized input names + variants
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
    n_active = 0
    with zipfile.ZipFile(bulk_zip_path) as zf:
        shards = sorted([n for n in zf.namelist() if n.lower().endswith(".txt")])
        log(f"Streaming {len(shards)} shards from {bulk_zip_path} ...")
        for shard in shards:
            log(f"  Reading {shard} ...")
            shard_records = 0
            with zf.open(shard) as f:
                # The cordata file is fixed-width 1440 chars/record.
                # Read line-by-line via TextIOWrapper for clean record splits.
                import io
                text_io = io.TextIOWrapper(f, encoding="latin-1", newline="")
                for raw_line in text_io:
                    line = raw_line.rstrip("\n").rstrip("\r")
                    rec = parse_cordata_record(line)
                    if rec is None:
                        continue
                    n_records += 1
                    shard_records += 1
                    if rec["status"] == "Active":
                        n_active += 1
                    norm = normalize_for_similarity(rec["name"])
                    if norm and norm in lookup:
                        input_name, query = lookup[norm]
                        matches[input_name].append((rec, query))
                    if n_records % 500_000 == 0:
                        log(f"    ... {n_records:,} records scanned, "
                            f"{sum(len(v) for v in matches.values())} candidate matches")
            log(f"  {shard}: {shard_records:,} records, "
                f"{sum(len(v) for v in matches.values())} cumulative matches")
    log(f"Total: {n_records:,} records ({n_active:,} active); "
        f"{sum(len(v) for v in matches.values())} candidate matches across {len(matches)} inputs")
    return matches


def pick_best_match(input_name, candidates_list):
    """Score (record, query) pairs from stream_match_all and pick best."""
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
        {"name": r["name"], "num": r["entity_number"],
         "status": r["status"], "score": round(s, 2)}
        for s, r, _ in scored[1:6]
    ]
    attempts = list(dict.fromkeys([q for _, _, q in scored]))
    return best, best_score, alternates, best_query, attempts


# ---------------------------------------------------------------------------
# Match logic
# ---------------------------------------------------------------------------

def score_record(input_name, rec, input_implies_oos=None):
    score = 0.0
    if rec["status"] == "Active":
        score += 100
    else:
        score -= 50  # FL bulk only marks A/I — be conservative on Inactive

    sim = name_similarity(input_name, rec["name"])
    score += sim * 100

    # Foreign filing types start with FOR (FORP, FORNP, FORLP, FORL)
    is_oos = rec["filing_type"].startswith("FOR")
    if input_implies_oos is None:
        input_implies_oos = bool(OUT_OF_STATE_HINTS.search(input_name))
    if is_oos and not input_implies_oos:
        score -= 30

    # Newer entities slight preference (recency tiebreaker)
    fd = rec.get("file_date") or ""
    if fd and len(fd) == 8 and fd.isdigit():
        try:
            dt = datetime.strptime(fd, "%Y%m%d")
            score += (dt - datetime(2000, 1, 1)).days * 0.001
        except Exception:
            pass

    return score


def match_one(input_name, index, all_normalized_names, fuzzy_threshold=0.5):
    """Return (best_record, alternates_list, query_used, query_attempts)."""
    queries = [input_name] + name_variants(input_name)
    attempts = []
    input_implies_oos = bool(OUT_OF_STATE_HINTS.search(input_name))

    for q in queries:
        attempts.append(q)
        norm = normalize_for_similarity(q)
        if not norm:
            continue
        candidates = index.get(norm, [])
        if candidates:
            scored = sorted(
                ((score_record(input_name, r, input_implies_oos), r) for r in candidates),
                key=lambda x: -x[0],
            )
            best_score, best = scored[0]
            alternates = [
                {"name": r["name"], "num": r["entity_number"],
                 "status": r["status"], "score": round(s, 2)}
                for s, r in scored[1:6]
            ]
            return best, best_score, alternates, q, attempts

    # Fuzzy fallback: candidates whose first token matches the input's first token
    input_norm = normalize_for_similarity(input_name)
    input_tokens = input_norm.split()
    if not input_tokens:
        return None, 0.0, [], queries[0], attempts

    first_tok = input_tokens[0]
    candidates = []
    for norm in all_normalized_names:
        if not norm.startswith(first_tok):
            continue
        sim = name_similarity(input_name, norm)
        if sim >= fuzzy_threshold:
            for r in index.get(norm, []):
                candidates.append(r)
    if not candidates:
        return None, 0.0, [], queries[0], attempts

    scored = sorted(
        ((score_record(input_name, r, input_implies_oos), r) for r in candidates),
        key=lambda x: -x[0],
    )
    best_score, best = scored[0]
    alternates = [
        {"name": r["name"], "num": r["entity_number"],
         "status": r["status"], "score": round(s, 2)}
        for s, r in scored[1:6]
    ]
    return best, best_score, alternates, "FUZZY:" + first_tok, attempts


# ---------------------------------------------------------------------------
# Build output row from a matched record
# ---------------------------------------------------------------------------

def record_to_row(rec, score, alternates, query_used, query_attempts, input_name):
    out = blank_row()
    if rec is None:
        out[PREFIX + "match_count"] = 0
        out[PREFIX + "query_used"] = query_used
        out[PREFIX + "query_attempts"] = " | ".join(query_attempts)
        return out

    # Pick "likely owner" officer: prefer Person President > Director > Manager > others
    person_officers = [o for o in rec["officers"] if o["type"] == "Person"]
    likely_owner = ""
    title_priority = {"P": 0, "D": 1, "C": 2, "V": 3, "T": 4, "S": 5}
    if person_officers:
        person_officers.sort(key=lambda o: title_priority.get(o["title_code"], 99))
        likely_owner = person_officers[0]["name"]

    principal_full = format_address_line(
        rec["principal_addr_1"], rec["principal_addr_2"],
        rec["principal_city"], rec["principal_state"], rec["principal_zip"],
    )
    mailing_full = format_address_line(
        rec["mail_addr_1"], rec["mail_addr_2"],
        rec["mail_city"], rec["mail_state"], rec["mail_zip"],
    )
    agent_full = format_address_line(
        rec["agent_addr"], "", rec["agent_city"], rec["agent_state"], rec["agent_zip"],
    )

    agent_is_individual = rec["agent_type"] == "P"
    addr_match = bool(agent_full and principal_full and addresses_match(agent_full, principal_full))

    out[PREFIX + "match_count"] = 1
    out[PREFIX + "first_result_name"] = rec["name"]
    out[PREFIX + "entity_number"] = rec["entity_number"]
    out[PREFIX + "status"] = rec["status"]
    out[PREFIX + "entity_type"] = rec["filing_type"]
    out[PREFIX + "formation_date"] = format_file_date(rec["file_date"])
    out[PREFIX + "jurisdiction"] = "Florida" if not rec["filing_type"].startswith("FOR") else "Out-of-state"
    out[PREFIX + "agent_name"] = rec["agent_name"]
    out[PREFIX + "agent_type"] = "Individual" if agent_is_individual else ("Corporation" if rec["agent_type"] == "C" else "")
    out[PREFIX + "agent_is_individual"] = "True" if agent_is_individual else "False"
    out[PREFIX + "agent_address"] = agent_full
    out[PREFIX + "principal_address"] = principal_full
    out[PREFIX + "mailing_address"] = mailing_full
    out[PREFIX + "agent_address_matches_principal"] = "True" if addr_match else ("False" if (agent_full and principal_full) else "")
    out[PREFIX + "score"] = f"{score:.2f}"
    out[PREFIX + "name_similarity"] = f"{name_similarity(input_name, rec['name']):.3f}"
    out[PREFIX + "alternates"] = json.dumps(alternates, ensure_ascii=False) if alternates else ""
    out[PREFIX + "query_used"] = query_used
    out[PREFIX + "query_attempts"] = " | ".join(query_attempts)
    out[PREFIX + "officers_json"] = json.dumps(rec["officers"], ensure_ascii=False) if rec["officers"] else ""
    out[PREFIX + "fei_number"] = rec["fei_number"]
    out[PREFIX + "filing_type"] = rec["filing_type"]
    out[PREFIX + "likely_owner_name"] = likely_owner

    # Operator pattern derivation
    agent_name = rec["agent_name"]
    if re.search(r"\b(esq|attorney|law)\b", agent_name, re.IGNORECASE):
        pattern = "attorney_agent"
    elif rec["filing_type"].startswith("FOR"):
        pattern = "out_of_state_holdco"
    elif likely_owner:
        pattern = "owner_operator" if addr_match or person_officers else "individual_agent_offsite"
    elif agent_name and not agent_is_individual:
        pattern = "service_agent"
    else:
        pattern = "unknown"
    out[PREFIX + "operator_pattern"] = pattern

    return out


# ---------------------------------------------------------------------------
# Cross-row pass: detect operators with multiple LLCs (shared address or owner)
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
# Bulk file management (SFTP download + cache)
# ---------------------------------------------------------------------------

def need_refresh(path, max_age_days=REFRESH_AFTER_DAYS):
    if not path.exists():
        return True
    age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    return age > timedelta(days=max_age_days)


def _make_logger():
    """Print to stdout AND append to states/fl/bulk/download.log so the user can
    `tail -f` the file or open it in the IDE while the download runs."""
    BULK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    log_path = BULK_CACHE_DIR / "download.log"
    log_file = open(log_path, "a", encoding="utf-8", buffering=1)  # line-buffered

    def log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        log_file.write(line + "\n")
        log_file.flush()
    return log, log_file


def download_bulk_via_sftp(log=None):
    if log is None:
        log, _log_file = _make_logger()
    try:
        import paramiko
    except ImportError:
        sys.exit("ERROR: paramiko not installed. Run `pip install paramiko` or use --bulk-file.")

    BULK_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Outer retry loop: server frequently drops the connection on this large file.
    # Resume from the current byte offset on disk each time we reconnect.
    MAX_ATTEMPTS = 50
    attempt = 0
    last_progress_bytes = -1
    stuck_attempts = 0
    while True:
        attempt += 1
        existing = BULK_ZIP_PATH.stat().st_size if BULK_ZIP_PATH.exists() else 0

        # Detect "stuck" state: if we made zero progress between two retries, back off.
        if existing == last_progress_bytes:
            stuck_attempts += 1
            if stuck_attempts >= 5:
                log(f"FATAL: 5 consecutive retries with zero progress. Aborting at {existing/1e9:.2f} GB.")
                sys.exit(1)
            backoff = min(60, 2 ** stuck_attempts)
            log(f"No progress since last attempt; backing off {backoff}s before retry {attempt} ...")
            import time
            time.sleep(backoff)
        else:
            stuck_attempts = 0
        last_progress_bytes = existing

        log(f"Attempt {attempt}: connecting to {SFTP_HOST} (existing local: {existing/1e9:.2f} GB)")
        try:
            # Socket-level read timeout so a silent server stall raises rather than hangs forever.
            import socket
            sock = socket.create_connection((SFTP_HOST, 22), timeout=30)
            sock.settimeout(60)  # any read with no bytes for 60s -> timeout exception -> retry loop
            transport = paramiko.Transport(sock)
            transport.connect(username=SFTP_USER, password=SFTP_PASS)
            transport.set_keepalive(30)  # ping every 30s so idle connections don't get NAT-killed
            sftp = paramiko.SFTPClient.from_transport(transport)
            sftp.get_channel().settimeout(60)
        except Exception as e:
            log(f"  connection failed: {type(e).__name__}: {e}. Retrying ...")
            continue

        try:
            remote_size = sftp.stat(SFTP_PATH).st_size
        except Exception as e:
            log(f"  stat failed: {type(e).__name__}: {e}. Retrying ...")
            try: sftp.close(); transport.close()
            except Exception: pass
            continue

        if existing >= remote_size:
            log(f"Already complete: {existing/1e9:.2f} GB on disk == remote size.")
            try: sftp.close(); transport.close()
            except Exception: pass
            break

        log(f"  remote {remote_size/1e9:.2f} GB; need {(remote_size-existing)/1e9:.2f} GB more")

        bytes_read = existing
        chunk_count = 0
        last_log_bytes = existing
        last_log_time = datetime.now()
        start_time = datetime.now()
        start_bytes = existing
        clean_finish = False
        try:
            with sftp.open(SFTP_PATH, "rb") as src:
                src.set_pipelined(True)
                src.seek(existing)
                with open(BULK_ZIP_PATH, "ab") as dst:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            clean_finish = True
                            break
                        dst.write(chunk)
                        # Flush + fsync every 8 MB so an interruption never loses
                        # more than ~8 MB of work.
                        chunk_count += 1
                        bytes_read += len(chunk)
                        if chunk_count % 8 == 0:
                            dst.flush()
                            try:
                                import os
                                os.fsync(dst.fileno())
                            except Exception:
                                pass
                        # Log every 25 MB or every 15 seconds, whichever first.
                        now = datetime.now()
                        if (bytes_read - last_log_bytes >= 25 * 1024 * 1024
                                or (now - last_log_time).total_seconds() >= 15):
                            elapsed = (now - start_time).total_seconds() or 0.001
                            kbps = (bytes_read - start_bytes) / 1024 / elapsed
                            pct = bytes_read * 100 / remote_size
                            eta_sec = (remote_size - bytes_read) / max(kbps * 1024, 1)
                            eta_min = int(eta_sec // 60)
                            log(f"  {bytes_read/1e9:.2f}/{remote_size/1e9:.2f} GB ({pct:.1f}%) — {kbps:.0f} KB/s — ETA {eta_min}m")
                            last_log_bytes = bytes_read
                            last_log_time = now
        except (paramiko.SSHException, EOFError, OSError) as e:
            log(f"  connection dropped after {(bytes_read-existing)/1e6:.1f} MB this attempt: {type(e).__name__}: {e}")
        except Exception as e:
            log(f"  unexpected error: {type(e).__name__}: {e}")
        finally:
            try: sftp.close()
            except Exception: pass
            try: transport.close()
            except Exception: pass

        # Verify what we have on disk before deciding whether to retry.
        on_disk = BULK_ZIP_PATH.stat().st_size if BULK_ZIP_PATH.exists() else 0
        log(f"  saved to disk: {on_disk/1e9:.2f} GB")
        if clean_finish and on_disk >= remote_size:
            log("Download complete.")
            break
        if attempt >= MAX_ATTEMPTS:
            log(f"FATAL: hit max attempts ({MAX_ATTEMPTS}). Aborting at {on_disk/1e9:.2f} GB.")
            sys.exit(1)
        log("Will retry from current offset ...")

    # NOTE: Sunbiz cordata.zip uses Deflate64 (compress_type=9) which Python's stdlib
    # zipfile doesn't decompress. We don't extract here — the matching code reads the
    # zip directly via a monkey-patched zipfile (see _patch_zipfile_for_deflate64()).
    log(f"Download stays as zip; will stream-read at match time.")


def ensure_bulk_file(bulk_file_arg, log=print):
    """Returns path to the cordata.zip (NOT the extracted txt). Caller reads the zip
    directly via the deflate64-monkey-patched zipfile."""
    if bulk_file_arg:
        p = Path(bulk_file_arg)
        if not p.exists():
            sys.exit(f"ERROR: --bulk-file path does not exist: {p}")
        return p
    if need_refresh(BULK_ZIP_PATH):
        log(f"Bulk zip stale or missing — refreshing from Sunbiz SFTP")
        download_bulk_via_sftp(log=log)
    return BULK_ZIP_PATH


# ---------------------------------------------------------------------------
# Deflate64 support: Sunbiz cordata.zip uses ZIP method 9 (Deflate64) which
# Python's stdlib zipfile cannot decompress. inflate64 provides the primitive;
# we monkey-patch zipfile to recognize it.
# ---------------------------------------------------------------------------

def _patch_zipfile_for_deflate64():
    import inflate64
    class _Deflate64Decompressor:
        def __init__(self):
            self._inflater = inflate64.Inflater()
            self._eof = False
        def decompress(self, data, max_length=0):
            out = self._inflater.inflate(data)
            if not out and not data:
                self._eof = True
            return out
        @property
        def eof(self):
            return self._eof
        @property
        def unused_data(self):
            return b''
        def flush(self):
            return b''

    _orig_check = zipfile._check_compression
    def _patched_check(ct):
        if ct == 9:
            return
        return _orig_check(ct)
    zipfile._check_compression = _patched_check

    _orig_get = zipfile._get_decompressor if hasattr(zipfile, '_get_decompressor') else None
    def _patched_get_decompressor(ct):
        if ct == 9:
            return _Deflate64Decompressor()
        return _orig_get(ct)
    zipfile._get_decompressor = _patched_get_decompressor


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


# ---------------------------------------------------------------------------
# Rescore-only mode: re-run cross-row + derived columns from existing CSV
# ---------------------------------------------------------------------------

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
    ap.add_argument("--bulk-file", help="Path to a manually-downloaded cordata.txt (skips SFTP)")
    ap.add_argument("--limit", type=int, default=0, help="Cap rows processed this run (0 = no cap)")
    ap.add_argument("--rescore-only", action="store_true",
                    help="Re-run cross-row + derived columns from existing output, skip matching")
    args = ap.parse_args()

    if args.rescore_only:
        rescore_only(args.output_csv)
        return

    log = _make_logger() if not args.bulk_file else _make_logger(BULK_CACHE_DIR / "enrich.log") if False else _make_logger()[0] if False else (lambda msg: print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True))
    # Note: _make_logger returns (log, file). For simplicity, just use simple stdout logging here.
    def log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)

    input_rows, input_fields = load_input(args.input_csv)
    if "entity_name" not in input_fields:
        sys.exit("ERROR: input CSV must have a column named 'entity_name'")
    log(f"Loaded {len(input_rows)} input rows")

    done_names, existing_rows = load_existing_output(args.output_csv)
    if done_names:
        log(f"Resuming: {len(done_names)} rows already complete")
    output_fields = list(input_fields) + [c for c in COLS if c not in input_fields]

    bulk_path = ensure_bulk_file(args.bulk_file, log=log)

    # Filter to rows that need processing (so we don't waste a stream pass on done rows)
    rows_todo = [r for r in input_rows
                 if (r.get("entity_name") or "").strip()
                 and (r.get("entity_name") or "").strip().lower() not in done_names]
    if args.limit:
        rows_todo = rows_todo[:args.limit]
    log(f"Will match {len(rows_todo)} rows ({len(input_rows) - len(rows_todo)} already done or empty)")

    # Single-pass stream over the bulk zip for all inputs at once
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
