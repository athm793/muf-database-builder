#!/usr/bin/env python3
"""
FETCH FDDs DIRECTLY — Playwright scraper (no Apify)
====================================================
Replaces fetch_fdds.py.  Same CLI interface and FETCH_RESULT output format.
Sources tried in order: WI DFI → MN CARDS

Adaptive selector strategy: tries multiple CSS/text selectors in priority order
so it degrades gracefully if a site updates its markup.  Use --debug to save
screenshots and HTML snapshots for troubleshooting.

Usage:
    py fetch_fdds_direct.py "KFC" "Taco Bell"
    py fetch_fdds_direct.py --names-file brands.txt
    py fetch_fdds_direct.py "Wingstop" --debug

Output (one line per brand, parsed by add_fdd.py):
    FETCH_RESULT|<brand>|found|<file_path>|<note>
    FETCH_RESULT|<brand>|not_found||<reason>
    FETCH_RESULT|<brand>|error||<reason>
"""

import re, sys, json, time, asyncio, argparse, difflib
import urllib.request, urllib.error
from datetime import datetime
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

HERE      = Path(__file__).resolve().parent
DATA_DIR  = HERE.parent / "Data" / "QSR"
DEBUG_DIR = HERE / "debug_fetch"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/121.0 Safari/537.36")

MN_FDD_PRIORITY = ["Clean FDD", "Final FDD", "Marked FDD"]


def log(msg=""):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}" if msg else "", flush=True)


def name_score(requested: str, candidate: str) -> float:
    req  = requested.lower().strip()
    cand = (candidate or "").lower().strip()
    if not cand:
        return 0.0
    if req == cand:
        return 1.0
    if req in cand or cand in req:
        return 0.9
    return difflib.SequenceMatcher(None, req, cand).ratio()


def safe_fname(name: str) -> str:
    return re.sub(r"[^\w\-]+", "_", name.strip()).strip("_") or "brand"


def download_pdf(url: str, dest: Path, timeout: int = 180) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    safe_url = url.replace("{", "%7B").replace("}", "%7D")
    req = urllib.request.Request(
        safe_url, headers={"User-Agent": UA, "Accept": "application/pdf,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
            first = r.read(8)
            if not first.startswith(b"%PDF"):
                log(f"    URL did not return a PDF (content-type: {r.headers.get('content-type', '?')})")
                tmp.unlink(missing_ok=True)
                return False
            f.write(first)
            while chunk := r.read(65536):
                f.write(chunk)
        tmp.replace(dest)
        return True
    except Exception as e:
        log(f"    Download error: {e}")
        tmp.unlink(missing_ok=True)
        return False


async def _save_debug(page, label: str):
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(DEBUG_DIR / f"{label}.png"), full_page=True)
    (DEBUG_DIR / f"{label}.html").write_text(
        await page.content(), encoding="utf-8", errors="replace")
    log(f"    Debug saved: debug_fetch/{label}.png")


async def _first_visible(page, selectors: list[str], timeout_ms: int = 2000):
    """Return the first locator from the list that is visible, or None."""
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=timeout_ms):
                return el
        except Exception:
            continue
    return None


# ── WI DFI ───────────────────────────────────────────────────────────────────

async def fetch_wi(page, brand: str, debug: bool = False) -> dict | None:
    """
    Search Wisconsin DFI Franchise Search for a brand.
    Returns {"url": ..., "label": ..., "source": "WI"} or None.

    WI DFI is an ASP.NET WebForms app at:
      https://www.wdfi.org/apps/FranchiseSearch/search.aspx
    Results table columns: Trade Name | Legal Name | Status | Effective Date | File Number
    FDD links appear either inline in the results or on a detail page.
    """
    log(f"  [WI] Searching Wisconsin DFI: {brand!r}")
    try:
        await page.goto("https://www.wdfi.org/apps/FranchiseSearch/search.aspx",
                        wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        log(f"    [WI] Navigation failed: {e}")
        return None

    if debug:
        await _save_debug(page, f"wi_loaded_{safe_fname(brand)}")

    # Find search input
    search_box = await _first_visible(page, [
        "input[type='text']",
        "input[name*='Franchisor' i]",
        "input[name*='Search' i]",
        "input[id*='Franchisor' i]",
        "input[id*='txtSearch' i]",
        "#txtFranchisor",
        "input[placeholder*='name' i]",
    ])
    if not search_box:
        log("    [WI] Could not locate search input")
        if debug:
            await _save_debug(page, f"wi_no_input_{safe_fname(brand)}")
        return None

    await search_box.fill(brand)

    # Submit
    submit_btn = await _first_visible(page, [
        "input[type='submit']",
        "button[type='submit']",
        "button:has-text('Search')",
        "input[value='Search']",
        "input[value='Go']",
        "a:has-text('Search')",
    ])
    if submit_btn:
        await submit_btn.click()
    else:
        await search_box.press("Enter")

    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass

    if debug:
        await _save_debug(page, f"wi_results_{safe_fname(brand)}")

    # Parse result rows
    best: dict = {"score": 0.0, "url": None, "label": ""}

    rows = await page.query_selector_all("table tr")
    for row in rows:
        cells = await row.query_selector_all("td")
        if not cells:
            continue
        trade  = (await cells[0].inner_text()).strip()
        legal  = (await cells[1].inner_text()).strip() if len(cells) > 1 else ""
        score  = max(name_score(brand, trade), name_score(brand, legal))
        if score < 0.35:
            continue

        label = trade or legal
        row_links = await row.query_selector_all("a[href]")

        for link in row_links:
            href = await link.get_attribute("href") or ""
            href_lower = href.lower()
            # Direct PDF link
            if href_lower.endswith(".pdf") or "fddpdf" in href_lower or "download" in href_lower:
                full = href if href.startswith("http") else f"https://www.wdfi.org{href}"
                if score > best["score"]:
                    best = {"score": score, "url": full, "label": label, "source": "WI"}
            # Detail page — navigate to find PDF there
            elif any(x in href_lower for x in ["detail", "view", "filing", "document", "fdd"]):
                if score > best["score"]:
                    detail_url = href if href.startswith("http") else f"https://www.wdfi.org{href}"
                    pdf_url = await _wi_detail_pdf(page, detail_url, debug)
                    if pdf_url:
                        best = {"score": score, "url": pdf_url, "label": label, "source": "WI"}

    if best["url"]:
        log(f"    [WI] Match: {best['label']!r} (score {best['score']:.2f})")
        return best
    log(f"    [WI] No match (best score: {best['score']:.2f})")
    return None


async def _wi_detail_pdf(page, detail_url: str, debug: bool) -> str | None:
    """Navigate to a WI DFI detail page and return the first FDD PDF link."""
    try:
        await page.goto(detail_url, wait_until="domcontentloaded", timeout=20000)
        if debug:
            await _save_debug(page, "wi_detail")
        links = await page.query_selector_all("a[href]")
        for link in links:
            href = (await link.get_attribute("href") or "").lower()
            text = (await link.inner_text()).strip().lower()
            if href.endswith(".pdf") or "fdd" in text or "disclosure" in text or "download" in text:
                raw = await link.get_attribute("href")
                return raw if (raw or "").startswith("http") else f"https://www.wdfi.org{raw}"
    except Exception as e:
        log(f"    [WI] Detail page error: {e}")
    return None


# ── MN CARDS ─────────────────────────────────────────────────────────────────

async def fetch_mn(page, brand: str, debug: bool = False) -> dict | None:
    """
    Search Minnesota CARDS for a brand's FDD.

    MN CARDS is a JSF app. Flow:
      1. Accept disclaimer at /CARDS/security/disclaimer.faces
      2. Search franchise by name
      3. Find best FDD document (Clean FDD > Final FDD > Marked FDD)

    Returns {"url": ..., "label": ..., "source": "MN"} or None.
    """
    log(f"  [MN] Searching Minnesota CARDS: {brand!r}")

    # Step 1: disclaimer
    try:
        await page.goto(
            "https://www.cards.commerce.state.mn.us/CARDS/security/disclaimer.faces",
            wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        log(f"    [MN] Navigation failed: {e}")
        return None

    if debug:
        await _save_debug(page, f"mn_disclaimer_{safe_fname(brand)}")

    # Click agree button
    agree_btn = await _first_visible(page, [
        "input[value*='agree' i]",
        "input[value*='Accept' i]",
        "button:has-text('agree')",
        "button:has-text('Accept')",
        "a:has-text('agree')",
        "input[type='submit']",
    ])
    if agree_btn:
        await agree_btn.click()
    else:
        # Fall back: click any submit/button on disclaimer
        btns = await page.query_selector_all("input[type='submit'], button")
        for btn in btns:
            text = (await btn.inner_text()).strip().lower()
            if any(w in text for w in ["agree", "accept", "continue", "proceed", "ok"]):
                await btn.click()
                break

    try:
        await page.wait_for_load_state("networkidle", timeout=12000)
    except PWTimeout:
        pass

    if debug:
        await _save_debug(page, f"mn_post_disclaimer_{safe_fname(brand)}")

    # Step 2: franchise search
    search_box = await _first_visible(page, [
        "input[type='text']:visible",
        "input[id*='franchise' i]",
        "input[name*='franchise' i]",
        "input[id*='name' i]",
        "input[type='text']",
    ])
    if not search_box:
        log("    [MN] No search box found after disclaimer")
        return None

    await search_box.fill(brand)

    submit_btn = await _first_visible(page, [
        "input[type='submit']",
        "button[type='submit']",
        "button:has-text('Search')",
        "input[value='Search']",
    ])
    if submit_btn:
        await submit_btn.click()
    else:
        await search_box.press("Enter")

    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass

    if debug:
        await _save_debug(page, f"mn_results_{safe_fname(brand)}")

    # Step 3: pick best FDD document from results
    best: dict = {"score": 0.0, "url": None, "label": "", "type_pri": -1}

    rows = await page.query_selector_all("table tr")
    for row in rows:
        cells = await row.query_selector_all("td")
        if not cells:
            continue
        texts = [(await c.inner_text()).strip() for c in cells]
        full_text = " ".join(texts)

        score = name_score(brand, full_text)
        if score < 0.25:
            continue

        doc_type  = next((t for t in texts if any(ft in t for ft in MN_FDD_PRIORITY)), "")
        type_pri  = (len(MN_FDD_PRIORITY) - MN_FDD_PRIORITY.index(doc_type)
                     if doc_type else 0)

        row_links = await row.query_selector_all("a[href]")
        for link in row_links:
            href = await link.get_attribute("href") or ""
            if not href:
                continue
            is_doc = (href.lower().endswith(".pdf") or
                      any(x in href.lower() for x in ["document", "file", "download", "view"]))
            if is_doc and (score > best["score"] or
                           (score == best["score"] and type_pri > best["type_pri"])):
                full = href if href.startswith("http") else \
                    f"https://www.cards.commerce.state.mn.us{href}"
                best = {"score": score, "url": full,
                        "label": full_text[:80], "type_pri": type_pri,
                        "source": "MN", "doc_type": doc_type}

    if best["url"]:
        log(f"    [MN] Match: {best.get('doc_type','FDD')!r} "
            f"score {best['score']:.2f}")
        return best
    log("    [MN] No match")
    return None


# ── ORCHESTRATOR ──────────────────────────────────────────────────────────────

async def fetch_one_async(brand: str, debug: bool = False) -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not debug)
        ctx = await browser.new_context(user_agent=UA)
        page = await ctx.new_page()

        for source_fn in (fetch_wi, fetch_mn):
            try:
                hit = await source_fn(page, brand, debug=debug)
            except Exception as e:
                source_name = source_fn.__name__.split("_")[1].upper()
                log(f"    [{source_name}] Unexpected error: {e}")
                continue

            if not hit:
                continue

            src = hit.get("source", "?")
            dest = DATA_DIR / f"{safe_fname(brand)}_{src}_fdd.pdf"
            log(f"    Downloading → {dest.name}")
            if download_pdf(hit["url"], dest):
                await browser.close()
                return {
                    "brand": brand, "status": "found",
                    "file":  str(dest),
                    "note":  f"{src}: {hit.get('label', '')[:80]}",
                }
            log(f"    Download failed for {hit['url'][:100]}")

        await browser.close()

    return {
        "brand": brand, "status": "not_found", "file": "",
        "note": "Not found in WI DFI or MN CARDS — try adding the PDF manually",
    }


def fetch_one(brand: str, debug: bool = False) -> dict:
    return asyncio.run(fetch_one_async(brand, debug))


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Fetch FDD PDFs directly via Playwright (WI DFI + MN CARDS)")
    ap.add_argument("names", nargs="*", metavar="BRAND",
                    help="One or more brand names")
    ap.add_argument("--names-file", metavar="FILE",
                    help="Text file with one brand name per line")
    ap.add_argument("--debug", action="store_true",
                    help="Save screenshots + HTML to debug_fetch/ on failure")
    args = ap.parse_args()

    names: list[str] = list(args.names)
    if args.names_file:
        p = Path(args.names_file)
        if p.exists():
            names += [ln.strip() for ln in
                      p.read_text(encoding="utf-8").splitlines() if ln.strip()]

    seen: set[str] = set()
    names = [n for n in names if not (n.lower() in seen or seen.add(n.lower()))]

    if not names:
        log("No brand names provided.")
        return 1

    log(f"Fetching {len(names)} brand(s)  —  WI DFI → MN CARDS")
    results = [fetch_one(b, debug=args.debug) for b in names]

    found = sum(1 for r in results if r["status"] == "found")
    log(f"\nResult: {found}/{len(names)} FDDs obtained")
    for r in results:
        if r["status"] != "found":
            log(f"  Not found: {r['brand']} — {r['note']}")

    for r in results:
        print(
            f"FETCH_RESULT|{r['brand']}|{r['status']}|{r['file']}|{r['note']}",
            flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
