#!/usr/bin/env python3
"""
SCRIPT 4 — ENTITY CLASSIFIER (PERSON vs COMPANY)
==================================================
Reads icp_combined.csv → classifies each franchisee_name as:
  - person              (e.g. "Drew Smith")
  - company             (e.g. "Ambrosia QSR Washington, LLC")
  - company_with_owner  (e.g. "Rockham LLC (Drew Smith)")

Hybrid strategy:
  1. Regex pass first — covers ~95% of cases for free
     (entity suffixes like LLC/Inc/Corp, parentheticals, pure-name patterns)
  2. Sonnet fallback for ambiguous names

Output columns (added to icp_combined):
  - entity_type          : person | company | company_with_owner
  - classification_method: regex | ai
  - search_target        : the person OR company name to paste into Apollo

Usage:
    python 4_classify_entity.py

Output:
    output/icp_combined_classified.csv
"""

import re, json, csv, time, sys, subprocess
from datetime import datetime
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── CONFIG ───────────────────────────────────────────────────────────────────
OUTPUT_DIR     = Path(__file__).parent / "output"
INPUT_FILE     = OUTPUT_DIR / "icp_combined.csv"
OUTPUT_FILE    = OUTPUT_DIR / "icp_combined_classified.csv"
CLASSIFY_CACHE = Path(__file__).parent / "cache" / "entity_classify_cache.json"
LOG_FILE       = Path(__file__).parent / "classify.log"

CLAUDE_CMD  = "claude.cmd" if sys.platform == "win32" else "claude"
MODEL       = "sonnet"
CLI_TIMEOUT = 180

MAX_RETRIES     = 5
RETRY_WAIT_SECS = 60

# Strong company indicators — if any appear as a word/token, it's a company.
COMPANY_SUFFIXES = [
    r"\bL\.?L\.?C\.?\b",
    r"\bL\.?L\.?P\.?\b",
    r"\bL\.?P\.?\b",
    r"\bP\.?L\.?L\.?C\.?\b",
    r"\bInc\.?\b",
    r"\bIncorporated\b",
    r"\bCorp\.?\b",
    r"\bCorporation\b",
    r"\bCo\.?\b",
    r"\bCompany\b",
    r"\bLtd\.?\b",
    r"\bLimited\b",
    r"\bGmbH\b",
    r"\bP\.?A\.?\b",
    r"\bN\.?A\.?\b",
]

# Words that strongly imply a business entity even without a legal suffix.
COMPANY_KEYWORDS = [
    r"\bHoldings?\b", r"\bGroup\b", r"\bEnterprises?\b", r"\bVentures?\b",
    r"\bInvestments?\b", r"\bPartners?\b", r"\bRestaurants?\b", r"\bFoods?\b",
    r"\bHospitality\b", r"\bManagement\b", r"\bStores?\b", r"\bBrands?\b",
    r"\bProperties\b", r"\bConcepts?\b", r"\bSystems?\b", r"\bServices?\b",
    r"\bDining\b", r"\bFranchise[es]?\b", r"\bOperating\b", r"\bDivision\b",
    r"\bAssociates?\b", r"\bIndustries\b", r"\bCorp\b", r"\bTrust\b",
]

COMPANY_SUFFIX_RE  = re.compile("|".join(COMPANY_SUFFIXES), re.IGNORECASE)
COMPANY_KEYWORD_RE = re.compile("|".join(COMPANY_KEYWORDS), re.IGNORECASE)
PAREN_RE           = re.compile(r"\(([^)]+)\)")

# A "pure person" name: 2-4 capitalized words, optionally with middle initials,
# optionally with a suffix like Jr/Sr/II/III. No digits, no commas (commas
# almost always indicate "LastName, FirstName" or a legal entity).
PERSON_NAME_RE = re.compile(
    r"^[A-Z][a-zA-Z'\-]+"                      # First name
    r"(?:\s+[A-Z]\.?)*"                         # Optional middle initial(s)
    r"(?:\s+[A-Z][a-zA-Z'\-]+){1,2}"            # Last name (and optional middle word)
    r"(?:\s+(?:Jr\.?|Sr\.?|II|III|IV))?$"       # Optional generational suffix
)


# ── LOGGER ───────────────────────────────────────────────────────────────────
_api_call_count = 0


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
    if CLASSIFY_CACHE.exists():
        try:
            return json.loads(CLASSIFY_CACHE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(cache: dict):
    CLASSIFY_CACHE.parent.mkdir(parents=True, exist_ok=True)
    CLASSIFY_CACHE.write_text(json.dumps(cache, indent=2), encoding="utf-8")


# ── REGEX PASS ───────────────────────────────────────────────────────────────
def classify_regex(name: str) -> tuple[str, str] | None:
    """
    Returns (entity_type, search_target) if the name can be classified
    confidently by regex alone, else None (caller falls back to AI).
    """
    if not name or not name.strip():
        return ("company", name)

    n = name.strip()

    # Parenthetical owner: "Rockham LLC (Drew Smith)" → company_with_owner,
    # search by the person inside the parens.
    paren = PAREN_RE.search(n)
    if paren:
        owner = paren.group(1).strip()
        # Strip leading "DBA " or "d/b/a " patterns — those are tradenames,
        # not owners (e.g. "Fqsr, Llc (Dba Kbp Foods)" is still just a company).
        if re.match(r"^(?:dba|d/?b/?a)\b", owner, re.IGNORECASE):
            outer = PAREN_RE.sub("", n).strip(" ,")
            if COMPANY_SUFFIX_RE.search(outer) or COMPANY_KEYWORD_RE.search(outer):
                return ("company", outer)
            return None  # ambiguous → AI
        # Strip a trailing "(1)" / "(2)" disambiguator
        if re.match(r"^\d+$", owner):
            outer = PAREN_RE.sub("", n).strip(" ,")
            if COMPANY_SUFFIX_RE.search(outer) or COMPANY_KEYWORD_RE.search(outer):
                return ("company", outer)
            if PERSON_NAME_RE.match(outer):
                return ("person", outer)
            return None
        # Otherwise treat the parenthetical as an owner name
        return ("company_with_owner", owner)

    # Strong legal suffix → company
    if COMPANY_SUFFIX_RE.search(n):
        return ("company", n)

    # Business keyword → company
    if COMPANY_KEYWORD_RE.search(n):
        return ("company", n)

    # Pure person-name pattern → person
    # Reject if it contains digits, commas, slashes, or ampersands.
    if any(c in n for c in ",/&@#$%"):
        return None
    if re.search(r"\d", n):
        return None
    if PERSON_NAME_RE.match(n):
        return ("person", n)

    return None  # ambiguous → AI


# ── AI FALLBACK ──────────────────────────────────────────────────────────────
SYSTEM_PROMPT = (
    "You classify franchisee names from US restaurant FDD filings. "
    "Reply with ONLY a single JSON object — no prose, no markdown fences. "
    "Schema: {\"entity_type\": \"person|company|company_with_owner\", "
    "\"search_target\": \"the name a salesperson would paste into Apollo to "
    "find this operator's contact info\"}. "
    "Rules: "
    "- 'person' = a real human being's name with no business entity wrapper. "
    "- 'company' = a registered business entity with no extractable owner name. "
    "- 'company_with_owner' = a company name that ALSO contains a clearly "
    "  identifiable individual owner (e.g. 'Smith Holdings (John Smith)'). "
    "- search_target for 'person' = the person's name. "
    "- search_target for 'company' = the company name. "
    "- search_target for 'company_with_owner' = the owner's name (the human)."
)


def classify_ai(name: str) -> tuple[str, str]:
    """Fallback for ambiguous names. Returns (entity_type, search_target)."""
    global _api_call_count
    _api_call_count += 1
    prompt = f"Franchisee name: {name}\n\nClassify per the schema."

    for attempt in range(1, MAX_RETRIES + 1):
        started = time.time()
        try:
            result = subprocess.run(
                [CLAUDE_CMD, "-p", "--model", MODEL],
                input=f"{SYSTEM_PROMPT}\n\n{prompt}",
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=CLI_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            log(f"    ⚠️  call #{_api_call_count} timed out [attempt {attempt}/{MAX_RETRIES}]")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_WAIT_SECS); continue
            return ("company", name)

        elapsed = time.time() - started

        if result.returncode != 0:
            log(f"    ⚠️  call #{_api_call_count} failed rc={result.returncode} [attempt {attempt}/{MAX_RETRIES}]")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_WAIT_SECS); continue
            return ("company", name)

        text = result.stdout.strip()
        log(f"    · call #{_api_call_count} [{MODEL}] {elapsed:.1f}s")

        # Pull the first {...} block out
        m = re.search(r"\{.*?\}", text, re.DOTALL)
        if not m:
            log(f"       ⚠️  no JSON in response, defaulting to company. raw={text[:120]}")
            return ("company", name)
        try:
            obj = json.loads(m.group(0))
            etype  = obj.get("entity_type", "company").strip().lower()
            target = obj.get("search_target", name).strip()
            if etype not in {"person", "company", "company_with_owner"}:
                etype = "company"
            return (etype, target or name)
        except json.JSONDecodeError:
            log(f"       ⚠️  bad JSON, defaulting to company. raw={text[:120]}")
            return ("company", name)

    return ("company", name)


# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    if not INPUT_FILE.exists():
        log(f"❌ {INPUT_FILE.name} not found. Run 3_filter_export.py first.")
        return

    # Reset log
    LOG_FILE.write_text("", encoding="utf-8")

    log(f"🏷️  Entity classifier — hybrid regex + Sonnet fallback")
    log(f"   Input : {INPUT_FILE.name}")
    log(f"   Output: {OUTPUT_FILE.name}")
    log(f"   Cache : {CLASSIFY_CACHE.name}")
    log("")

    cache = load_cache()
    log(f"📦 Loaded cache with {len(cache)} prior classifications")

    with open(INPUT_FILE, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    log(f"📂 Loaded {len(rows)} ICP rows")

    counts = {"regex": 0, "ai": 0, "cache": 0}
    type_counts = {"person": 0, "company": 0, "company_with_owner": 0}

    enriched = []
    for i, row in enumerate(rows, 1):
        name = row.get("franchisee_name", "")
        cache_key = name.strip().lower()

        if cache_key in cache:
            etype, target, method = cache[cache_key]["entity_type"], \
                                    cache[cache_key]["search_target"], \
                                    cache[cache_key]["method"]
            counts["cache"] += 1
        else:
            regex_result = classify_regex(name)
            if regex_result is not None:
                etype, target = regex_result
                method = "regex"
                counts["regex"] += 1
            else:
                etype, target = classify_ai(name)
                method = "ai"
                counts["ai"] += 1

            cache[cache_key] = {
                "entity_type":   etype,
                "search_target": target,
                "method":        method,
            }
            # Persist after each AI call so an interrupted run is resumable
            if method == "ai":
                save_cache(cache)

        type_counts[etype] += 1

        new_row = dict(row)
        new_row["entity_type"]           = etype
        new_row["classification_method"] = method
        new_row["search_target"]         = target
        enriched.append(new_row)

        if i % 200 == 0:
            log(f"   progress {i}/{len(rows)}  regex={counts['regex']}  ai={counts['ai']}  cache={counts['cache']}")

    save_cache(cache)

    # Write output
    fieldnames = list(enriched[0].keys())
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(enriched)

    log("")
    log("═" * 55)
    log(f"✅ CLASSIFIED {len(enriched)} rows → {OUTPUT_FILE.name}")
    log("─" * 55)
    log(f"   By method:")
    log(f"     regex (free)        : {counts['regex']:>5}")
    log(f"     ai (sonnet)         : {counts['ai']:>5}")
    log(f"     cache hits          : {counts['cache']:>5}")
    log(f"   By entity type:")
    log(f"     person              : {type_counts['person']:>5}")
    log(f"     company             : {type_counts['company']:>5}")
    log(f"     company_with_owner  : {type_counts['company_with_owner']:>5}")
    log("")
    log("➡️  Next: filter by entity_type in Apollo. Use 'search_target'")
    log("    column directly in Apollo's people/company search bar.")


if __name__ == "__main__":
    main()
