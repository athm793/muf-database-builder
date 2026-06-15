#!/usr/bin/env python3
"""
FETCH FDDs BY BRAND NAME  (Apify → state franchise registers)
=============================================================
Given one or more franchise brand names, fetch each brand's Franchise Disclosure
Document (FDD) PDF from a state franchise register via Apify, and save the PDFs
into Data/QSR/ ready for the normal "add FDD" extraction pipeline.

Sources are tried in order until one yields a downloadable FDD:
  1. WI — Wisconsin DFI            (parseforge/wisconsin-franchise-search-scraper)
  2. MN — Minnesota CARDS          (parseforge/mn-franchise-registrations-scraper)
  3. CA — California DFPI          (parseforge/california-franchise-scraper)

Each state's actor has a DIFFERENT input/output shape, handled by a per-source
adapter below:
  WI input {searchTerm, maxItems, downloadPdfs}  → output fddPdfUrl (signed)
  MN input {franchiseName, maxItems}             → output documentUrl + documentType
  CA input {searchQuery, maxItems, includeDetails}→ output: metadata (often NO PDF)

It only FETCHES PDFs — extraction/matching happens afterward in add_fdd.py.

Credentials / actor ids come from FDD Parser/.env:
    APIFY_TOKEN=apify_api_xxx
    APIFY_ACTOR_ID=DcUptfu6v2Y8wCbGY        # WI
    APIFY_ACTOR_ID_MN=dnfUyPVabAz3oj2pE     # MN
    APIFY_ACTOR_ID_CA=H82SbZK5RUog0mBaB     # CA

Usage (CLI):
    python fetch_fdds.py "KFC" "Taco Bell" "Wingstop"
    python fetch_fdds.py --names-file brands.txt
Each result prints a machine-readable line the GUI parses:
    FETCH_RESULT|<brand>|<status>|<file_or_blank>|<note>
status is one of: found | not_found | error
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

# Windows console: avoid cp1252 crashes on emoji.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE     = Path(__file__).resolve().parent
ENV_FILE = HERE.parent / ".env"
DATA_DIR = HERE.parent / "Data" / "QSR"
APIFY_BASE = "https://api.apify.com/v2"

POLL_SECS    = 5
MAX_RUN_WAIT = 900
DEFAULT_MAX  = 10

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/121.0 Safari/537.36")

# Minnesota document types that are actual FDDs, best-first.
MN_FDD_TYPES = ["Clean FDD", "Final FDD", "Marked FDD"]


def log(msg: str = ""):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}" if msg else "", flush=True)


class PaymentRequired(Exception):
    """Raised when Apify returns 402 — the actor is paid and credits are needed."""


# ── CONFIG ───────────────────────────────────────────────────────────────────

def load_env() -> dict:
    cfg = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    for k in ("APIFY_TOKEN", "APIFY_ACTOR_ID", "APIFY_ACTOR_ID_MN", "APIFY_ACTOR_ID_CA"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


# ── APIFY API ────────────────────────────────────────────────────────────────

def _post(path: str, payload: dict, token: str, timeout: int = 60):
    url = f"{APIFY_BASE}{path}?token={token}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _get(path: str, token: str, timeout: int = 120, **params):
    params["token"] = token
    url = f"{APIFY_BASE}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def run_actor(actor: str, token: str, input_payload: dict) -> list[dict]:
    """Start an actor run, wait for it, return dataset items.
    Raises PaymentRequired on HTTP 402 (paid actor, no credits)."""
    try:
        run = _post(f"/acts/{actor}/runs", input_payload, token, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 402:
            raise PaymentRequired(actor)
        raise
    data = run.get("data", {})
    run_id, ds_id = data.get("id"), data.get("defaultDatasetId")
    if not run_id or not ds_id:
        raise RuntimeError(f"unexpected run response: {json.dumps(run)[:160]}")

    waited = 0
    while True:
        time.sleep(POLL_SECS)
        waited += POLL_SECS
        try:
            status = _get(f"/actor-runs/{run_id}", token).get("data", {}).get("status")
        except urllib.error.HTTPError as e:
            if e.code == 402:
                raise PaymentRequired(actor)
            raise
        if status == "SUCCEEDED":
            break
        if status in ("FAILED", "ABORTED", "TIMED-OUT", "TIMING-OUT"):
            raise RuntimeError(f"actor run ended with status {status}")
        if waited >= MAX_RUN_WAIT:
            raise RuntimeError(f"actor run timed out after {MAX_RUN_WAIT}s (status {status})")

    items = _get(f"/datasets/{ds_id}/items", token, clean="true", limit="1000")
    return items if isinstance(items, list) else []


# ── SHARED HELPERS ───────────────────────────────────────────────────────────

def _name_score(requested: str, candidate: str) -> float:
    req, cand = requested.lower().strip(), (candidate or "").lower().strip()
    if not cand:
        return 0.0
    if req in cand or cand in req:
        return 1.0
    return difflib.SequenceMatcher(None, req, cand).ratio()


def _parse_date(raw: str):
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime((raw or "").strip(), fmt)
        except ValueError:
            continue
    return datetime.min


def safe_brand(name: str) -> str:
    return re.sub(r"[^\w\-]+", "_", name.strip()).strip("_") or "brand"


def download_pdf(url: str, dest: Path, timeout: int = 180) -> bool:
    """Download a PDF with browser headers (gov sites 403 the default UA).
    Brace characters in some gov URLs are percent-encoded."""
    safe_url = url.replace("{", "%7B").replace("}", "%7D")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(safe_url, headers={
        "User-Agent": BROWSER_UA, "Accept": "application/pdf,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
            first = r.read(8)
            if not first.startswith(b"%PDF"):
                ctype = r.headers.get("content-type", "")
                log(f"    ⚠️  link was not a PDF (content-type={ctype})")
                tmp.unlink(missing_ok=True)
                return False
            f.write(first)
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


# ── PER-SOURCE ADAPTERS ──────────────────────────────────────────────────────
# Each select() returns {"url","label","file_id"} for the best FDD, or None.

def wi_input(term, n):
    return {"searchTerm": term, "maxItems": n, "downloadPdfs": True}


def wi_select(items, requested):
    usable = [it for it in items if it.get("fddAvailable") and it.get("fddPdfUrl")]
    if not usable:
        return None
    usable.sort(key=lambda it: (
        _name_score(requested, f"{it.get('tradeName','')} {it.get('legalName','')}"),
        1 if (it.get("status") or "").lower() == "registered" else 0,
        _parse_date(it.get("effectiveDate")),
    ), reverse=True)
    b = usable[0]
    return {"url": b["fddPdfUrl"], "label": b.get("legalName", ""),
            "file_id": b.get("fileNumber", "")}


def mn_input(term, n):
    # Pull plenty of docs so the FDD is among them (a franchisor has many filings).
    return {"franchiseName": term, "maxItems": max(n, 50)}


def mn_select(items, requested):
    real = [it for it in items if isinstance(it, dict) and "error" not in it and it.get("documentUrl")]
    fdds = [it for it in real if it.get("documentType") in MN_FDD_TYPES]
    if not fdds:
        return None

    def rank(it):
        type_pri = len(MN_FDD_TYPES) - MN_FDD_TYPES.index(it["documentType"])  # Clean>Final>Marked
        return (
            _name_score(requested, f"{it.get('franchiseName','')} {it.get('franchisor','')}"),
            type_pri,
            _parse_date(it.get("receivedDate")),
            str(it.get("year", "")),
        )

    fdds.sort(key=rank, reverse=True)
    b = fdds[0]
    return {"url": b["documentUrl"],
            "label": f"{b.get('franchisor') or b.get('franchiseName','')} [{b.get('documentType')}]",
            "file_id": b.get("fileNumber", "")}


def ca_input(term, n):
    return {"searchQuery": term, "maxItems": n, "includeDetails": True}


def ca_select(items, requested):
    # California's actor returns filing metadata; it may not expose a PDF link.
    # Be defensive: use any field that looks like a downloadable document URL.
    for it in items:
        if not isinstance(it, dict):
            continue
        for k, v in it.items():
            if isinstance(v, str) and v.lower().startswith("http") and \
               (v.lower().endswith(".pdf") or "pdf" in k.lower()
                or "document" in k.lower() or "fdd" in k.lower()):
                label = it.get("franchisor") or it.get("franchiseName") or it.get("name") or requested
                return {"url": v, "label": str(label), "file_id": str(it.get("applicationId", ""))}
    return None


SOURCES = [
    {"key": "WI", "name": "Wisconsin DFI",  "env": "APIFY_ACTOR_ID",
     "default": "DcUptfu6v2Y8wCbGY", "build": wi_input, "select": wi_select},
    {"key": "MN", "name": "Minnesota CARDS", "env": "APIFY_ACTOR_ID_MN",
     "default": "dnfUyPVabAz3oj2pE", "build": mn_input, "select": mn_select},
    {"key": "CA", "name": "California DFPI", "env": "APIFY_ACTOR_ID_CA",
     "default": "H82SbZK5RUog0mBaB", "build": ca_input, "select": ca_select},
]


# ── FETCH ONE BRAND (fallback chain) ─────────────────────────────────────────

def fetch_one(brand: str, cfg: dict, max_items: int = DEFAULT_MAX) -> dict:
    """Try each state source in order until one yields a downloadable FDD.
    Returns {brand, status, file, legalName, note}."""
    token = cfg.get("APIFY_TOKEN")
    notes = []
    for src in SOURCES:
        actor = cfg.get(src["env"], src["default"])
        if not actor:
            notes.append(f"{src['key']}: no actor id")
            continue
        log(f"🔎 [{src['key']}] {src['name']} — searching for: {brand}")
        try:
            items = run_actor(actor, token, src["build"](brand, max_items))
        except PaymentRequired:
            log(f"    💳 {src['key']}: Apify says payment required (add credits)")
            notes.append(f"{src['key']}: payment required")
            continue
        except Exception as e:
            log(f"    ⚠️  {src['key']}: {e}")
            notes.append(f"{src['key']}: {str(e)[:60]}")
            continue

        if not items:
            log(f"    — {src['key']}: no results")
            notes.append(f"{src['key']}: no registration")
            continue

        pick = src["select"](items, brand)
        if not pick:
            log(f"    — {src['key']}: {len(items)} record(s), but no downloadable FDD")
            notes.append(f"{src['key']}: no FDD pdf")
            continue

        dest = DATA_DIR / f"{safe_brand(brand)}_{src['key']}_{pick['file_id'] or 'fdd'}.pdf"
        log(f"    ✓ {src['key']} match: {pick['label']}")
        log(f"    ⬇️  Downloading FDD → {dest.name}")
        if download_pdf(pick["url"], dest):
            return {"brand": brand, "status": "found", "file": str(dest),
                    "legalName": pick["label"],
                    "note": f"{src['key']}: {pick['label']}; file {pick['file_id']}"}
        notes.append(f"{src['key']}: download failed")

    log(f"    ✗ no FDD obtained for '{brand}'")
    # If every source said payment required, surface that prominently.
    if notes and all("payment required" in n for n in notes):
        return {"brand": brand, "status": "error", "file": "", "legalName": "",
                "note": "Apify payment required — add credits to your Apify account"}
    return {"brand": brand, "status": "not_found", "file": "", "legalName": "",
            "note": "; ".join(notes) or "not found"}


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Fetch FDD PDFs by brand name via Apify (WI/MN/CA).")
    ap.add_argument("names", nargs="*", help="Brand names, e.g. KFC \"Taco Bell\"")
    ap.add_argument("--names-file", help="Text file with one brand name per line.")
    ap.add_argument("--max-items", type=int, default=DEFAULT_MAX)
    args = ap.parse_args()

    names = list(args.names)
    if args.names_file:
        p = Path(args.names_file)
        if p.exists():
            names += [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    seen = set()
    names = [n for n in names if not (n.lower() in seen or seen.add(n.lower()))]
    if not names:
        log("No brand names provided.")
        return 1

    cfg = load_env()
    if not cfg.get("APIFY_TOKEN"):
        log("❌ APIFY_TOKEN not set. Add it to FDD Parser/.env (APIFY_TOKEN=apify_api_...).")
        return 1

    log(f"🚀 Fetching {len(names)} brand(s) — sources: WI → MN → CA")
    results = [fetch_one(b, cfg, args.max_items) for b in names]

    log("")
    log("═" * 55)
    found = [r for r in results if r["status"] == "found"]
    log(f"✅ Fetched {len(found)}/{len(names)} FDDs")
    for r in results:
        if r["status"] != "found":
            log(f"   ⚠️  {r['brand']}: {r['status']} ({r['note']})")

    for r in results:
        print(f"FETCH_RESULT|{r['brand']}|{r['status']}|{r['file']}|{r['note']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
