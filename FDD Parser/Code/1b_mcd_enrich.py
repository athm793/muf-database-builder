#!/usr/bin/env python3
"""
SCRIPT 1b — McDONALD'S WEB-SEARCH ENRICHMENT
==============================================
McDonald's FDD lists individual person-operators without their operating
company/LLC name. Script 1 extracts these as person-only records. This
script runs AFTER script 1 and uses Claude Code CLI with WebSearch to
look up the operating company for each McDonald's operator, then updates
their franchisee_name to "Company Name (Person Name)".

STRICT NO-HALLUCINATION RULES:
  - Only HIGH-confidence matches are applied (requires a cited source URL)
  - No guessing, no inference from domain names, no partial matches
  - Uncertain → leave the record as person-name only

Usage:
    python 1b_mcd_enrich.py              # enrich all McD records
    python 1b_mcd_enrich.py --limit 10   # enrich first 10 only (testing)

Caching:
    Each search result is cached in cache/mcd_enrich_cache.json.
    Re-runs skip already-cached records. Safe to stop mid-run.

Output:
    Overwrites output/raw/<McD>_raw.csv with enriched franchisee_name values.
    Original is backed up once as output/raw/<McD>_raw.original.csv.
"""

import csv, json, re, sys, subprocess, time
from datetime import datetime
from pathlib import Path

# Fix Windows console encoding for emoji output
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── CONFIG ───────────────────────────────────────────────────────────────────
RAW_DIR      = Path(__file__).parent / "output" / "raw"
CACHE_FILE   = Path(__file__).parent / "cache" / "mcd_enrich_cache.json"
LOG_FILE     = Path(__file__).parent / "mcd_enrich.log"

# Which brand's raw CSV to enrich. After extraction, script 1 saves McD
# records to a file whose name depends on the detected brand. We search
# for any CSV whose brand column equals one of these values.
MCD_BRAND_ALIASES = {"mcdonald's", "mcdonalds", "mcdonald"}

CLAUDE_CMD   = "claude.cmd" if sys.platform == "win32" else "claude"
MODEL        = "sonnet"    # high-stakes reasoning + tool use → use sonnet
CLI_TIMEOUT  = 300         # web searches can take a while
MAX_ATTEMPTS = 2           # retry on transient failures
# ─────────────────────────────────────────────────────────────────────────────


# ── LOGGER ───────────────────────────────────────────────────────────────────

_call_count = 0


def log(msg: str = ""):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}" if msg else ""
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── CACHE ────────────────────────────────────────────────────────────────────

def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(cache: dict):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def cache_key(rec: dict) -> str:
    """
    Stable key for caching — tied to the operator (person name + state).
    City and phone are intentionally excluded so all locations of the same
    operator share one lookup. State is kept so that two people with the
    same name operating in different states stay separate.
    """
    parts = [
        rec.get("franchisee_name", "").strip().lower(),
        rec.get("state", "").strip().upper(),
    ]
    return "|".join(parts)


# ── WEB SEARCH AGENT ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a research agent that looks up the operating company \
behind a McDonald's franchise operator. You have access to the WebSearch tool.

STRICT ANTI-HALLUCINATION RULES:
1. You MUST cite at least one specific URL where you found the company name.
2. Only return a company name if you are HIGHLY confident based on the cited source.
3. Do NOT guess, infer from domain names, or use partial matches.
4. If the search returns nothing definitive, or you have any doubt, return NONE.
5. Do not invent company names. Do not hallucinate.
6. Common corporate naming patterns like "Operator Name LLC" are NOT evidence — \
you need a cited source that explicitly links the person to that company.

Return ONLY valid JSON in this exact format, with no markdown or commentary:
{
  "company": "Exact Company Name LLC" or "NONE",
  "confidence": "HIGH" or "LOW",
  "source_url": "https://..." or "",
  "reasoning": "one sentence citing the source"
}

If confidence is not HIGH, the result will be discarded. So only say HIGH \
when a cited source directly confirms the company."""


def run_claude_websearch(prompt: str) -> str:
    """Invoke Claude Code CLI with WebSearch enabled."""
    global _call_count
    _call_count += 1
    full_prompt = f"{SYSTEM_PROMPT}\n\n{prompt}"
    started = time.time()
    try:
        result = subprocess.run(
            [CLAUDE_CMD, "-p", "--model", MODEL, "--allowed-tools", "WebSearch"],
            input=full_prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CLI_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        log(f"    ⚠️  Call #{_call_count} timed out after {CLI_TIMEOUT}s")
        return ""

    elapsed = time.time() - started
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()[:200]
        log(f"    ⚠️  Call #{_call_count} failed rc={result.returncode}: {err}")
        return ""
    log(f"    · call #{_call_count} [sonnet+websearch] {elapsed:.1f}s")
    return result.stdout.strip()


def parse_response(raw: str) -> dict | None:
    """Extract JSON from Claude's response. Returns None on parse failure."""
    if not raw:
        return None
    text = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r'\{[\s\S]*\}', text)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def lookup_company(rec: dict) -> dict:
    """
    Search the web for the operating company of this McDonald's operator.
    Returns:
      {
        "company": "..." or "NONE",
        "confidence": "HIGH"|"LOW",
        "source_url": "...",
        "reasoning": "..."
      }
    """
    person  = rec.get("franchisee_name", "")
    address = rec.get("raw_address", "")
    city    = rec.get("city", "")
    state   = rec.get("state", "")
    phone   = rec.get("restaurant_phone", "")

    query = f"""Find the operating company or LLC name for this McDonald's franchise operator:

Person:  {person}
Address: {address}
City:    {city}
State:   {state}
Phone:   {phone}

Search the web. Look for sources that explicitly name the company this person \
operates McDonald's restaurants through. Return the strict JSON format specified \
in the system prompt."""

    for attempt in range(MAX_ATTEMPTS):
        raw = run_claude_websearch(query)
        parsed = parse_response(raw)
        if parsed is not None:
            return parsed
        if attempt < MAX_ATTEMPTS - 1:
            log(f"    ⚠️  Parse failed on attempt {attempt + 1}, retrying...")
            time.sleep(2)

    return {"company": "NONE", "confidence": "LOW", "source_url": "",
            "reasoning": "parse_failed_after_retries"}


# ── MAIN ─────────────────────────────────────────────────────────────────────

def find_mcd_csv() -> Path | None:
    """Locate the McDonald's raw CSV in output/raw/."""
    for csv_path in sorted(RAW_DIR.glob("*_raw.csv")):
        # Skip files we created via backup
        if ".original" in csv_path.name:
            continue
        try:
            with open(csv_path, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
                if not rows:
                    continue
                brand = rows[0].get("brand", "").strip().lower()
                if brand in MCD_BRAND_ALIASES:
                    return csv_path
        except Exception:
            continue
    return None


def backup_original(csv_path: Path) -> Path:
    """Create a one-time backup of the original CSV before enrichment."""
    backup = csv_path.with_suffix(".original.csv")
    if not backup.exists():
        backup.write_bytes(csv_path.read_bytes())
        log(f"    💾 Backed up original to {backup.name}")
    return backup


def enrich_records(records: list[dict], limit: int | None = None) -> tuple[int, int, int]:
    """
    Enrich records in place.

    Strategy: group records by (name, state) so we only make one API call
    per unique operator. One person usually runs many locations, so this
    cuts API calls by 10-30x vs the naive per-row approach. The answer
    (company name) is backfilled to every location in the group.

    Returns (total_processed, high_conf_matches, skipped).
    """
    cache = load_cache()

    # ── Group records by operator key ─────────────────────────────────────────
    groups: dict[str, list[dict]] = {}
    for rec in records:
        key = cache_key(rec)
        groups.setdefault(key, []).append(rec)

    unique_operators = list(groups.keys())
    if limit is not None:
        unique_operators = unique_operators[:limit]

    total_groups = len(unique_operators)
    total_records_affected = sum(len(groups[k]) for k in unique_operators)

    log()
    log(f"📊 Deduplicated {total_records_affected} records → {total_groups} unique operators")
    log(f"   (one API call per operator — answer is back-filled to all their locations)")

    high_conf_ops = 0      # unique operators successfully enriched
    skipped_ops = 0        # unique operators where lookup failed / low conf
    processed_records = 0  # total rows whose franchisee_name was rewritten

    for idx, key in enumerate(unique_operators):
        group = groups[key]
        representative = group[0]
        person = representative.get("franchisee_name", "")

        log()
        log(f"[{idx + 1}/{total_groups}] {person}  ({len(group)} locations)")

        # Check cache first
        if key in cache:
            result = cache[key]
            log(f"    ✅ cached: {result.get('company', 'NONE')} ({result.get('confidence', 'LOW')})")
        else:
            result = lookup_company(representative)
            cache[key] = result
            save_cache(cache)   # persist after every lookup

        company = result.get("company", "NONE").strip()
        confidence = result.get("confidence", "LOW").strip().upper()
        source = result.get("source_url", "").strip()

        # STRICT: apply only HIGH-confidence matches with a source URL
        if company and company != "NONE" and confidence == "HIGH" and source:
            new_name = f"{company} ({person})"
            for rec in group:
                rec["franchisee_name"] = new_name
                processed_records += 1
            high_conf_ops += 1
            log(f"    ✓ HIGH → {company}  (applied to {len(group)} rows)")
            log(f"       source: {source}")
        else:
            skipped_ops += 1
            if company == "NONE":
                log(f"    ↷ no match found  ({len(group)} rows left unchanged)")
            elif confidence != "HIGH":
                log(f"    ↷ LOW confidence — not applying ({company})")
            elif not source:
                log(f"    ↷ missing source URL — not applying ({company})")
            else:
                log(f"    ↷ skipped")

    return processed_records, high_conf_ops, skipped_ops


def save_enriched(csv_path: Path, records: list[dict]):
    fields = ["brand", "franchisee_name", "raw_address", "city", "state", "zip", "restaurant_phone"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)


def main():
    # Parse --limit N argument
    limit = None
    if "--limit" in sys.argv:
        try:
            i = sys.argv.index("--limit")
            limit = int(sys.argv[i + 1])
        except (ValueError, IndexError):
            log("❌ --limit requires an integer argument")
            return

    # Fresh log
    try:
        LOG_FILE.write_text("", encoding="utf-8")
    except Exception:
        pass

    log("🔎 McDonald's web-search enrichment")
    log(f"   Log file:  {LOG_FILE}")
    log(f"   Cache:     {CACHE_FILE}")
    log(f"   Model:     {MODEL} + WebSearch tool")
    log(f"   Policy:    HIGH-confidence only with cited source URL")
    if limit:
        log(f"   Limit:     first {limit} records (test mode)")

    csv_path = find_mcd_csv()
    if not csv_path:
        log(f"❌ No McDonald's CSV found in {RAW_DIR}")
        log(f"   Run 1_ai_extract.py first to extract McD.pdf")
        return

    log(f"   Target:    {csv_path.name}")

    # Back up the original once
    backup_original(csv_path)

    # Load records
    with open(csv_path, encoding="utf-8") as f:
        records = list(csv.DictReader(f))
    log(f"   Records:   {len(records)}")

    if not records:
        log("⚠️  CSV is empty — nothing to enrich")
        return

    log()
    rows_rewritten, high_conf_ops, skipped_ops = enrich_records(records, limit=limit)

    # Save enriched CSV
    save_enriched(csv_path, records)

    log()
    log("═" * 55)
    log("✅ ENRICHMENT COMPLETE")
    log("─" * 55)
    log(f"   Rows rewritten: {rows_rewritten}")
    log(f"   ✓ Operators enriched (HIGH conf + source): {high_conf_ops}")
    log(f"   ↷ Operators skipped (LOW / no source / no match): {skipped_ops}")
    log(f"   API calls: {_call_count}")
    log()
    log(f"   Enriched CSV: {csv_path.name}")
    log(f"   Original backup: {csv_path.with_suffix('.original.csv').name}")
    log()
    log("  ➡️  Re-run 2_ai_match.py to incorporate enriched names")


if __name__ == "__main__":
    main()
