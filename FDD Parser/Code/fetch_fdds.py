#!/usr/bin/env python3
"""
FETCH FDDs BY BRAND NAME  (Apify → Wisconsin DFI)
=================================================
Given one or more franchise brand names, this fetches each brand's Franchise
Disclosure Document (FDD) PDF from the Wisconsin DFI franchise register using
the Apify actor `parseforge/wisconsin-franchise-search-scraper`, and saves the
PDFs into Data/QSR/ ready for the normal "add FDD" extraction pipeline.

It only FETCHES PDFs — it does not run extraction/matching. The GUI (add_fdd.py)
calls this, then runs 1_ai_extract --single on each fetched PDF, then matches.

Credentials come from FDD Parser/.env:
    APIFY_TOKEN=apify_api_xxx
    APIFY_ACTOR_ID=DcUptfu6v2Y8wCbGY      (parseforge/wisconsin-franchise-search-scraper)

Actor input  : {searchTerm, maxItems, downloadPdfs}
Actor output : dataset items with legalName, tradeName, statesFiled, status,
               effectiveDate, fddAvailable, fddPdfUrl (a durable signed PDF URL).

Usage (CLI):
    python fetch_fdds.py "KFC" "Taco Bell" "Wingstop"
    python fetch_fdds.py --names-file brands.txt
Each result is printed as a machine-readable line for the GUI:
    FETCH_RESULT|<brand>|<status>|<file_or_blank>|<note>
where status is one of: found | not_found | error
"""

import os
import re
import sys
import json
import time
import difflib
import argparse
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

# Fix Windows console encoding so emoji/log output doesn't crash on cp1252.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE     = Path(__file__).resolve().parent
ENV_FILE = HERE.parent / ".env"
DATA_DIR = HERE.parent / "Data" / "QSR"
APIFY_BASE = "https://api.apify.com/v2"

# Default actor id (Wisconsin). Overridable via .env / env var.
DEFAULT_ACTOR = "DcUptfu6v2Y8wCbGY"

POLL_SECS      = 5      # how often to poll a running actor
MAX_RUN_WAIT   = 900    # give up on a single run after 15 min
DEFAULT_MAX    = 10     # maxItems per search (brands rarely have more)


def log(msg: str = ""):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}" if msg else "", flush=True)


# ── CONFIG ───────────────────────────────────────────────────────────────────

def load_env() -> dict:
    cfg = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    # Real environment variables win over the .env file.
    for k in ("APIFY_TOKEN", "APIFY_ACTOR_ID"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


# ── APIFY API ────────────────────────────────────────────────────────────────

def _post(path: str, payload: dict, token: str, timeout: int = 60):
    url = f"{APIFY_BASE}{path}?token={token}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _get(path: str, token: str, timeout: int = 120, **params):
    params["token"] = token
    url = f"{APIFY_BASE}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def run_search(actor: str, token: str, search_term: str,
               max_items: int = DEFAULT_MAX, download_pdfs: bool = True) -> list[dict]:
    """Start an actor run for one brand, wait for it, return dataset items."""
    payload = {"searchTerm": search_term, "maxItems": max_items,
               "downloadPdfs": download_pdfs}
    run = _post(f"/acts/{actor}/runs", payload, token, timeout=60)
    data = run.get("data", {})
    run_id = data.get("id")
    ds_id = data.get("defaultDatasetId")
    if not run_id or not ds_id:
        raise RuntimeError(f"unexpected run response: {json.dumps(run)[:200]}")

    waited = 0
    while True:
        time.sleep(POLL_SECS)
        waited += POLL_SECS
        status = _get(f"/actor-runs/{run_id}", token).get("data", {}).get("status")
        if status == "SUCCEEDED":
            break
        if status in ("FAILED", "ABORTED", "TIMED-OUT", "TIMING-OUT"):
            raise RuntimeError(f"actor run ended with status {status}")
        if waited >= MAX_RUN_WAIT:
            raise RuntimeError(f"actor run timed out after {MAX_RUN_WAIT}s (status {status})")

    items = _get(f"/datasets/{ds_id}/items", token, clean="true", limit="1000")
    return items if isinstance(items, list) else []


# ── SELECTION + DOWNLOAD ─────────────────────────────────────────────────────

def _name_score(requested: str, item: dict) -> float:
    """0..1 similarity of the requested brand to this registration's names."""
    req = requested.lower().strip()
    cand = " ".join(filter(None, [item.get("tradeName", ""), item.get("legalName", "")])).lower()
    if not cand:
        return 0.0
    # Reward substring containment strongly; otherwise fuzzy ratio.
    if req in cand:
        return 1.0
    return difflib.SequenceMatcher(None, req, cand).ratio()


def _eff_date(item: dict):
    raw = item.get("effectiveDate") or ""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return datetime.min


def pick_best(items: list[dict], requested: str) -> dict | None:
    """
    Choose the best single registration to download for this brand:
    must have a downloadable FDD; prefer Registered status, closest name
    match, then most recent effective date.
    """
    usable = [it for it in items if it.get("fddAvailable") and it.get("fddPdfUrl")]
    if not usable:
        return None

    def rank(it):
        return (
            _name_score(requested, it),
            1 if (it.get("status") or "").lower() == "registered" else 0,
            _eff_date(it),
        )

    usable.sort(key=rank, reverse=True)
    return usable[0]


def safe_brand(name: str) -> str:
    return re.sub(r"[^\w\-]+", "_", name.strip()).strip("_") or "brand"


def download_pdf(url: str, dest: Path, timeout: int = 180) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r, open(tmp, "wb") as f:
            ctype = r.headers.get("content-type", "")
            chunk = r.read(8)
            if not chunk.startswith(b"%PDF"):
                # Not a PDF (maybe an error/HTML page)
                log(f"    ⚠️  download did not look like a PDF (content-type={ctype})")
                tmp.unlink(missing_ok=True)
                return False
            f.write(chunk)
            while True:
                buf = r.read(65536)
                if not buf:
                    break
                f.write(buf)
        tmp.replace(dest)
        return True
    except Exception as e:
        log(f"    ⚠️  download failed: {e}")
        tmp.unlink(missing_ok=True)
        return False


# ── FETCH ONE BRAND ──────────────────────────────────────────────────────────

def fetch_one(brand: str, actor: str, token: str,
              max_items: int = DEFAULT_MAX) -> dict:
    """
    Returns {brand, status, file, legalName, note}.
    status: found | not_found | error
    """
    log(f"🔎 Searching Wisconsin DFI for: {brand}")
    try:
        items = run_search(actor, token, brand, max_items=max_items, download_pdfs=True)
    except Exception as e:
        log(f"    ❌ search error: {e}")
        return {"brand": brand, "status": "error", "file": "", "legalName": "",
                "note": str(e)[:200]}

    if not items:
        log(f"    — no Wisconsin registration found for '{brand}'")
        return {"brand": brand, "status": "not_found", "file": "", "legalName": "",
                "note": "no WI registration"}

    best = pick_best(items, brand)
    if best is None:
        log(f"    — found {len(items)} record(s) but none had a downloadable FDD")
        return {"brand": brand, "status": "not_found", "file": "", "legalName": "",
                "note": f"{len(items)} record(s), no FDD PDF"}

    others = max(0, len({i.get('fileNumber') for i in items if i.get('fddPdfUrl')}) - 1)
    legal = best.get("legalName", "")
    fnum = best.get("fileNumber", "")
    dest = DATA_DIR / f"{safe_brand(brand)}_WI_{fnum}.pdf"
    log(f"    ✓ Match: {legal} (file {fnum}, eff {best.get('effectiveDate')})"
        + (f"  [+{others} other registration(s) not downloaded]" if others else ""))
    log(f"    ⬇️  Downloading FDD → {dest.name}")
    ok = download_pdf(best["fddPdfUrl"], dest)
    if not ok:
        return {"brand": brand, "status": "error", "file": "", "legalName": legal,
                "note": "PDF download failed"}

    note = f"{legal}; file {fnum}"
    if others:
        note += f"; +{others} other registration(s)"
    return {"brand": brand, "status": "found", "file": str(dest),
            "legalName": legal, "note": note}


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Fetch FDD PDFs by brand name via Apify (Wisconsin DFI).")
    ap.add_argument("names", nargs="*", help="Brand names, e.g. KFC \"Taco Bell\"")
    ap.add_argument("--names-file", help="Text file with one brand name per line.")
    ap.add_argument("--max-items", type=int, default=DEFAULT_MAX)
    args = ap.parse_args()

    names = list(args.names)
    if args.names_file:
        p = Path(args.names_file)
        if p.exists():
            names += [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    # De-dupe, preserve order
    seen = set()
    names = [n for n in names if not (n.lower() in seen or seen.add(n.lower()))]

    if not names:
        log("No brand names provided.")
        return 1

    cfg = load_env()
    token = cfg.get("APIFY_TOKEN")
    actor = cfg.get("APIFY_ACTOR_ID", DEFAULT_ACTOR)
    if not token:
        log("❌ APIFY_TOKEN not set. Add it to FDD Parser/.env (APIFY_TOKEN=apify_api_...).")
        return 1

    log(f"🚀 Fetching {len(names)} brand(s) from Wisconsin DFI via Apify")
    results = []
    for brand in names:
        results.append(fetch_one(brand, actor, token, max_items=args.max_items))

    log("")
    log("═" * 55)
    found = [r for r in results if r["status"] == "found"]
    missed = [r for r in results if r["status"] != "found"]
    log(f"✅ Fetched {len(found)}/{len(names)} FDDs")
    for r in missed:
        log(f"   ⚠️  {r['brand']}: {r['status']} ({r['note']})")

    # Machine-readable lines for the GUI
    for r in results:
        print(f"FETCH_RESULT|{r['brand']}|{r['status']}|{r['file']}|{r['note']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
