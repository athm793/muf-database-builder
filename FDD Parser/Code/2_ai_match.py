#!/usr/bin/env python3
"""
SCRIPT 2 — AI CROSS-FDD FRANCHISEE MATCHER
============================================
Loads all extracted raw CSVs, then uses Claude to:
  1. Identify when the same operator appears across multiple brand FDDs
     (e.g. "Mike Retzer" in McDonald's = "Michael L. Retzer Sr." in Burger King)
  2. Build a unified master registry with total unit count across all brands
  3. Flag multi-brand operators explicitly

Why AI and not just fuzzy matching?
  - Fuzzy matching can't reason: "Mike" = "Michael", "Bob" = "Robert"
  - Fuzzy matching misses "J. Smith TX" = "John Smith Texas"
  - Fuzzy matching has high false-positive rate for common surnames
  - Claude reasons about name + state + address context together

Usage:
    python 2_ai_match.py

Output:
    output/master_franchisees.csv
"""

import re, json, csv, time, sys, subprocess
from datetime import datetime
from pathlib import Path
from collections import defaultdict
from thefuzz import fuzz

# Fix Windows console encoding for emoji output
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── CONFIG ───────────────────────────────────────────────────────────────────
RAW_DIR      = Path(__file__).parent / "output" / "raw"
OUTPUT_DIR   = Path(__file__).parent / "output"
MASTER_FILE  = OUTPUT_DIR / "master_franchisees.csv"
MATCH_CACHE  = Path(__file__).parent / "cache" / "match_cache.json"
SKIPPED_FILE = Path(__file__).parent / "cache" / "match_skipped.json"
LOG_FILE     = Path(__file__).parent / "match.log"

# Claude Code CLI — uses Max plan subscription, no API credits needed
CLAUDE_CMD  = "claude.cmd" if sys.platform == "win32" else "claude"
MODEL       = "sonnet"
HAIKU_MODEL = "haiku"      # used for match yes/no decisions
CLI_TIMEOUT = 180

# Pre-filter thresholds before sending to AI.
# Candidates below FUZZY_LOW_* are dropped. Candidates at or above FUZZY_HIGH
# are auto-merged without AI. Everything in between goes to AI for a judgement call.
#
# Same-brand pairs use a stricter threshold because we compare full company
# names — genuine variants of the same operator score well above 80
# (e.g. "Tri-Arc Food Systems, Inc." vs "Tri-Arc Food Systems, Inc. Tommy Haddock"
# scores ~92). Cross-brand uses the looser 60 because person-name variants
# (Mike vs Michael) score lower.
FUZZY_LOW_SAME_BRAND  = 82
FUZZY_LOW_CROSS_BRAND = 60
FUZZY_HIGH            = 96

# Backward-compat alias (referenced in log output)
FUZZY_LOW = FUZZY_LOW_CROSS_BRAND
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


MAX_RETRIES      = 5     # how many times to retry a failed call
RETRY_WAIT_SECS  = 60    # seconds between retries (rate-limit cool-down)


# ── TIMING STATS ──────────────────────────────────────────────────────────────
# These get populated as the run progresses and are printed at the end.
# Use them to judge whether CLI_TIMEOUT / RETRY_WAIT_SECS are appropriate
# for the real-world call profile.
_call_times: dict[str, list[float]] = {"sonnet": [], "haiku": []}
_retry_wait_events: list[dict] = []  # list of {"call_num", "wait_secs", "attempt"}


def _record_call_time(model: str, elapsed: float):
    _call_times.setdefault(model, []).append(elapsed)


def _record_retry_wait(call_num: int, wait_secs: int, attempt: int):
    _retry_wait_events.append({
        "call_num": call_num,
        "wait_secs": wait_secs,
        "attempt": attempt,
    })


def _percentile(values: list[float], p: float) -> float:
    """Simple percentile (no dependencies). p is 0-100."""
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def print_timing_stats():
    """Print timing statistics for tuning CLI_TIMEOUT and RETRY_WAIT_SECS."""
    log()
    log("📊 TIMING STATS")
    log("─" * 55)
    for model, times in _call_times.items():
        if not times:
            continue
        log(f"   {model} — {len(times)} successful calls")
        log(f"      min    = {min(times):6.1f}s")
        log(f"      p50    = {_percentile(times, 50):6.1f}s")
        log(f"      p90    = {_percentile(times, 90):6.1f}s")
        log(f"      p95    = {_percentile(times, 95):6.1f}s")
        log(f"      max    = {max(times):6.1f}s")
        log(f"      avg    = {sum(times)/len(times):6.1f}s")
    if _retry_wait_events:
        total_wait = sum(e["wait_secs"] for e in _retry_wait_events)
        log(f"   retry waits: {len(_retry_wait_events)} events, {total_wait}s total ({total_wait/60:.1f} min)")
    else:
        log(f"   retry waits: 0 (no failures — no rate-limit hits)")
    log(f"   current: CLI_TIMEOUT={CLI_TIMEOUT}s  RETRY_WAIT_SECS={RETRY_WAIT_SECS}s")
    log()
    # Tuning hints
    all_times = _call_times.get("sonnet", []) + _call_times.get("haiku", [])
    if all_times:
        p95 = _percentile(all_times, 95)
        if CLI_TIMEOUT < p95 * 1.5:
            log(f"   💡 Hint: p95 is {p95:.0f}s — consider raising CLI_TIMEOUT to {int(p95*1.5)}s")
        elif CLI_TIMEOUT > p95 * 3:
            log(f"   💡 Hint: p95 is {p95:.0f}s — CLI_TIMEOUT={CLI_TIMEOUT}s is generous, could lower to {int(p95*2)}s")


def _run_claude_cli(prompt: str, system: str, model: str) -> str:
    """
    Invoke the Claude Code CLI as a subprocess. Uses Max plan auth.
    Retries on failure (rc!=0 or timeout) up to MAX_RETRIES times with
    a RETRY_WAIT_SECS pause between attempts to ride out rate limits.
    Returns "" after all retries are exhausted so the caller can treat
    this pair as "unresolved" and move on (the match cache persists
    everything that did succeed, so re-runs pick up where we left off).
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
                _record_retry_wait(_api_call_count, RETRY_WAIT_SECS, attempt)
                time.sleep(RETRY_WAIT_SECS)
                continue
            log(f"    ❌ Call #{_api_call_count} all {MAX_RETRIES} attempts exhausted — skipping pair")
            return ""

        elapsed = time.time() - started

        if result.returncode != 0:
            err_msg = (result.stderr or result.stdout or "").strip()[:300]
            log(f"    ⚠️  Call #{_api_call_count} ({model}) failed rc={result.returncode}  [attempt {attempt}/{MAX_RETRIES}]")
            log(f"       error: {err_msg}")
            if attempt < MAX_RETRIES:
                log(f"    ⏳ Waiting {RETRY_WAIT_SECS}s before retry...")
                _record_retry_wait(_api_call_count, RETRY_WAIT_SECS, attempt)
                time.sleep(RETRY_WAIT_SECS)
                continue
            log(f"    ❌ Call #{_api_call_count} all {MAX_RETRIES} attempts exhausted — skipping pair")
            return ""

        _record_call_time(model, elapsed)
        log(f"    · call #{_api_call_count} [{model}] {elapsed:.1f}s")
        return result.stdout.strip()

    return ""


# ── LOAD DATA ────────────────────────────────────────────────────────────────

def load_all_raw() -> list[dict]:
    """Load every raw CSV from output/raw/"""
    all_records = []
    csv_files = sorted(RAW_DIR.glob("*_raw.csv"))
    if not csv_files:
        log(f"❌ No raw CSVs found. Run 1_ai_extract.py first.")
        return []

    log(f"📂 Loading {len(csv_files)} brand CSVs...")
    for f in csv_files:
        with open(f, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
            all_records.extend(rows)
            log(f"   {f.stem:<40} {len(rows):>5} records")

    log(f"   {'─'*47}")
    log(f"   Total: {len(all_records)} records across all brands")
    return all_records


# ── PRE-GROUPING ─────────────────────────────────────────────────────────────

def build_candidate_pairs(records: list[dict]) -> list[tuple]:
    """
    Find candidate pairs for fuzzy/AI matching.

    Optimized strategy (two key tricks):

      1. Deduplicate by (brand, normalized_name, state) — one representative
         record per unique combo. Step 1 (exact_same_brand_merge) already
         unioned all literal duplicates, so comparing one rep per unique name
         is equivalent to comparing every record, but 3-4x fewer items.

      2. State blocking for cross-brand — only compare records within the
         same state. An operator running McD in TX and BK in OK is rare
         enough that we accept the miss in exchange for 50x speedup
         (1.33B cross-brand pairs → ~25M). Same-brand already state-filtered.

    Same-brand uses token_set_ratio (handles subset matches like
    "Tri-Arc Food Systems" ⊂ "Tri-Arc Food Systems Tommy Haddock"),
    cross-brand uses token_sort_ratio (better for person-name order variants).

    Returns list of (record_a, record_b, fuzzy_score) tuples sorted desc by score.
    Progress is logged periodically so the run is observable.
    """
    candidates = []

    # ── Dedupe: one representative record per (brand, name, state) ───────────
    unique_reps: dict[tuple, dict] = {}
    for rec in records:
        key = (
            rec["brand"],
            rec["franchisee_name"].strip().lower(),
            (rec.get("state") or "").strip().upper(),
        )
        if key not in unique_reps:
            unique_reps[key] = rec
    reps = list(unique_reps.values())
    log(f"   Deduplicated {len(records)} records → {len(reps)} unique (brand, name, state) representatives")

    # Group reps by brand and by state for fast iteration
    by_brand: dict[str, list[dict]] = defaultdict(list)
    by_state: dict[str, list[dict]] = defaultdict(list)
    for rec in reps:
        by_brand[rec["brand"]].append(rec)
        state = (rec.get("state") or "").strip().upper()
        if state:
            by_state[state].append(rec)

    # ── 1. Same-brand pairs (already state-filtered, name dedup does the rest) ──
    log(f"   Step 3a: same-brand fuzzy pass over {len(by_brand)} brands...")
    same_brand_count = 0
    for brand, group in by_brand.items():
        n = len(group)
        for i in range(n):
            a = group[i]
            for j in range(i + 1, n):
                b = group[j]
                # State agreement: if both have state, they must match
                sa = (a.get("state") or "").strip().upper()
                sb = (b.get("state") or "").strip().upper()
                if sa and sb and sa != sb:
                    continue
                score = fuzz.token_set_ratio(a["franchisee_name"], b["franchisee_name"])
                if score < FUZZY_LOW_SAME_BRAND:
                    continue
                candidates.append((a, b, score))
                same_brand_count += 1
        log(f"      {brand:<25s} {n:>5} reps  → {same_brand_count} candidates so far")
    log(f"   Same-brand candidates: {same_brand_count}")

    # ── 2. Cross-brand pairs, BLOCKED by state ───────────────────────────────
    # Within each state, compare every record from different brands. This
    # drops the pair count from ~1.3B to ~25M (50x), at the cost of missing
    # operators who run different brands in different states.
    log(f"   Step 3b: cross-brand fuzzy pass over {len(by_state)} states...")
    cross_brand_count = 0
    states_processed = 0
    total_states = len(by_state)
    # Process states largest first so we get progress signal on the heavy states early
    for state in sorted(by_state.keys(), key=lambda s: -len(by_state[s])):
        state_reps = by_state[state]
        # Group this state's reps by brand
        brands_here: dict[str, list[dict]] = defaultdict(list)
        for rec in state_reps:
            brands_here[rec["brand"]].append(rec)
        brand_list = list(brands_here.keys())

        state_candidates = 0
        for bi in range(len(brand_list)):
            for bj in range(bi + 1, len(brand_list)):
                for a in brands_here[brand_list[bi]]:
                    for b in brands_here[brand_list[bj]]:
                        score = fuzz.token_sort_ratio(a["franchisee_name"], b["franchisee_name"])
                        if score < FUZZY_LOW_CROSS_BRAND:
                            continue
                        candidates.append((a, b, score))
                        state_candidates += 1
        cross_brand_count += state_candidates
        states_processed += 1
        if states_processed % 5 == 0 or states_processed == total_states:
            log(f"      states processed: {states_processed}/{total_states}  cross-brand candidates so far: {cross_brand_count}")
    log(f"   Cross-brand candidates: {cross_brand_count}")

    # Sort by fuzzy score descending (highest confidence first)
    log(f"   Sorting {len(candidates)} total candidates...")
    candidates.sort(key=lambda x: -x[2])
    return candidates


# ── EXACT SAME-BRAND MERGING ─────────────────────────────────────────────────

def exact_same_brand_merge(records: list[dict], find, union) -> int:
    """
    Merge every set of records that share an EXACT (brand, franchisee_name)
    pair. No AI, no fuzzy matching — if the string is literally identical
    within a brand, it is the same operator. This is the primary mechanism
    for counting multi-location operators (e.g. 35 rows of 'Tri-Arc Food
    Systems, Inc.' → one operator with 35 locations).

    Returns: number of records merged into an existing group.
    """
    exact_groups = defaultdict(list)
    for i, rec in enumerate(records):
        key = (rec['brand'], rec['franchisee_name'].strip().lower())
        exact_groups[key].append(i)

    merged = 0
    for indices in exact_groups.values():
        if len(indices) < 2:
            continue
        root = indices[0]
        for idx in indices[1:]:
            if find(root) != find(idx):
                union(root, idx)
                merged += 1
    return merged


# ── SAME-BRAND OWNER GROUPING ────────────────────────────────────────────────

# Corporate suffix tokens — if any of these appear in a name, it's a company not a person
CORPORATE_SIGNALS = {
    'LLC', 'INC', 'CORP', 'CO', 'LTD', 'COMPANY', 'GROUP',
    'HOLDINGS', 'ENTERPRISES', 'RESTAURANTS', 'FOODS', 'VENTURES',
    'PARTNERS', 'PARTNERSHIP', 'ASSOCIATES', 'MANAGEMENT', 'PROPERTIES',
}


def get_owner_key(name: str) -> tuple[str | None, str | None]:
    """
    Return (grouping_key, method) for same-brand dedup, or (None, None) if ungroupable.

    Priority:
      1. Extract owner from parenthetical: 'Rockham LLC (Drew Smith)' → 'drew smith'
      2. Direct person name (no corporate tokens): 'Jason Patel' → 'jason patel'
      3. Corporate name with no owner signal → (None, None) — don't guess
    """
    # 1. Parenthetical pattern
    m = re.search(r'\(([^)]+)\)', name)
    if m:
        candidate = m.group(1).strip()
        words = candidate.split()
        upper_tokens = {w.rstrip('.,').upper() for w in words}
        if len(words) >= 2 and not upper_tokens & CORPORATE_SIGNALS:
            return candidate.lower(), 'parens'

    # 2. Direct person name — no corporate tokens anywhere in the full name
    words = name.split()
    upper_tokens = {w.rstrip('.,').upper() for w in words}
    if len(words) >= 2 and not upper_tokens & CORPORATE_SIGNALS:
        return name.lower().strip(), 'person'

    # 3. Corporate name with no extractable owner — leave ungrouped
    return None, None


def resolve_same_brand(records: list[dict], find, union) -> dict:
    """
    Merge same-brand records that share an identified owner name.
    Uses Union-Find (find/union) from the caller's scope.

    Returns per-brand audit stats dict.
    """
    brand_owner_groups = defaultdict(list)   # (brand, owner_key) → [record indices]
    method_map = {}                           # (brand, owner_key) → method string

    for i, rec in enumerate(records):
        key, method = get_owner_key(rec['franchisee_name'])
        if key:
            group_key = (rec['brand'], key)
            brand_owner_groups[group_key].append(i)
            method_map[group_key] = method

    # Track which record indices were successfully grouped
    grouped_indices = set()

    for (brand, key), indices in brand_owner_groups.items():
        if len(indices) < 2:
            continue  # nothing to merge
        method = method_map[(brand, key)]
        for idx in indices[1:]:
            if find(indices[0]) != find(idx):
                union(indices[0], idx)
        grouped_indices.update(indices)

    # Build per-brand audit stats
    brand_stats = defaultdict(lambda: {
        'records': 0, 'grouped_parens': 0, 'grouped_person': 0, 'ungrouped_llc': 0
    })

    for i, rec in enumerate(records):
        b = rec['brand']
        brand_stats[b]['records'] += 1
        if i in grouped_indices:
            key, method = get_owner_key(rec['franchisee_name'])
            if method == 'parens':
                brand_stats[b]['grouped_parens'] += 1
            else:
                brand_stats[b]['grouped_person'] += 1
        else:
            _, method = get_owner_key(rec['franchisee_name'])
            if method is None:
                brand_stats[b]['ungrouped_llc'] += 1

    return dict(brand_stats)


# ── AI MATCHING ──────────────────────────────────────────────────────────────

def load_match_cache() -> dict:
    if MATCH_CACHE.exists():
        with open(MATCH_CACHE) as f:
            return json.load(f)
    return {}


def save_match_cache(cache: dict):
    MATCH_CACHE.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: write to .tmp, then rename. Protects against
    # mid-write kills/crashes corrupting the cache.
    tmp = MATCH_CACHE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(cache, f, indent=2)
    import os
    os.replace(tmp, MATCH_CACHE)


# Hybrid model threshold: pairs with fuzzy score below this go to Sonnet
# (the harder, more ambiguous cases). Pairs at or above use Haiku (the
# easier "almost certainly same" cases that just need a sanity check).
# Sonnet is slower and uses more credits but reasons better on edge cases
# like "John Smith TX" vs "John A. Smith Jr. OK" where false positives
# would silently merge two real operators into one ghost record.
SONNET_FUZZY_CEILING = 85


# ── SKIPPED PAIRS (exhausted retries) ─────────────────────────────────────────

def load_skipped() -> list[dict]:
    """Load the running list of pairs whose AI call was skipped after all retries."""
    if SKIPPED_FILE.exists():
        try:
            return json.loads(SKIPPED_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_skipped(skipped: list[dict]):
    SKIPPED_FILE.parent.mkdir(parents=True, exist_ok=True)
    SKIPPED_FILE.write_text(json.dumps(skipped, indent=2), encoding="utf-8")


def record_skipped_pair(a: dict, b: dict, fuzzy_score: int, model: str):
    """
    Append a pair to the skipped list and persist immediately. Idempotent —
    won't duplicate a pair that's already in the file. Re-runs will retry
    these pairs automatically (they aren't in match_cache), but this list
    gives you visibility into WHAT got skipped so you can inspect them.
    """
    skipped = load_skipped()
    pair_key = "|".join(sorted([
        f"{a['franchisee_name']}:{a['brand']}:{a['state']}",
        f"{b['franchisee_name']}:{b['brand']}:{b['state']}",
    ]))
    if any(entry.get("pair_key") == pair_key for entry in skipped):
        return
    skipped.append({
        "pair_key":       pair_key,
        "a_name":         a.get("franchisee_name", ""),
        "a_brand":        a.get("brand", ""),
        "a_state":        a.get("state", ""),
        "b_name":         b.get("franchisee_name", ""),
        "b_brand":        b.get("brand", ""),
        "b_state":        b.get("state", ""),
        "fuzzy_score":    fuzzy_score,
        "model_attempted": model,
        "skipped_at":     datetime.now().isoformat(timespec="seconds"),
    })
    save_skipped(skipped)


def ai_is_same_person(a: dict, b: dict, fuzzy_score: int, cache: dict) -> tuple[bool, str]:
    """
    Ask Claude whether two franchisee records refer to the same person.
    Returns (is_match: bool, reasoning: str)

    Hybrid model selection:
      - fuzzy_score < SONNET_FUZZY_CEILING → Sonnet (better judgment, slower)
      - fuzzy_score >= SONNET_FUZZY_CEILING → Haiku (fast sanity check)

    Uses cache to avoid re-asking the same pair.
    """
    cache_key = "|".join(sorted([
        f"{a['franchisee_name']}:{a['brand']}:{a['state']}",
        f"{b['franchisee_name']}:{b['brand']}:{b['state']}",
    ]))

    if cache_key in cache:
        cached = cache[cache_key]
        return cached["match"], cached["reason"]

    prompt = f"""Are these two franchise operator records likely the same real person?

Record A:
  Name:    {a['franchisee_name']}
  Brand:   {a['brand']}
  State:   {a['state']}
  City:    {a.get('city', '')}
  Address: {a.get('raw_address', '')}

Record B:
  Name:    {b['franchisee_name']}
  Brand:   {b['brand']}
  State:   {b['state']}
  City:    {b.get('city', '')}
  Address: {b.get('raw_address', '')}

Consider:
- Are the names the same person? (nicknames: Mike=Michael, Bob=Robert, Bill=William etc.)
- Are they operating in the same state or overlapping geography?
- Is it plausible this person owns multiple franchise brands?
- Middle initials or suffixes shouldn't prevent a match if everything else aligns

Return JSON only:
{{
  "match": true/false,
  "confidence": 0-100,
  "reason": "one sentence explanation"
}}"""

    system = """You are an expert at franchise operator identity resolution.
You determine whether two names in different FDDs refer to the same real-world person.
Return only valid JSON."""

    chosen_model = HAIKU_MODEL if fuzzy_score >= SONNET_FUZZY_CEILING else MODEL
    raw = _run_claude_cli(prompt, system, chosen_model)

    # If the call exhausted all retries and returned nothing, treat this pair
    # as unresolved and DO NOT cache — so the next run retries it rather than
    # locking in a fake "no match" decision. Also record it in the skipped
    # file for later visibility / targeted re-runs.
    if not raw:
        record_skipped_pair(a, b, fuzzy_score, chosen_model)
        return False, "call_failed_not_cached"

    result = None
    try:
        clean = re.sub(r'```(?:json)?', '', raw).strip().rstrip('`').strip()
        result = json.loads(clean)
    except:
        pass

    if result:
        match = bool(result.get("match", False))
        reason = result.get("reason", "")
        confidence = result.get("confidence", 0)

        # Only accept low-confidence matches if confidence is high enough
        if match and confidence < 70:
            match = False
            reason = f"Low confidence ({confidence}) — treating as separate"
    else:
        match = False
        reason = "AI parse failed — treating as separate"

    cache[cache_key] = {"match": match, "reason": reason}
    return match, reason


# Batch size for Haiku sanity-check calls. Haiku handles easy pairs
# (fuzzy >= SONNET_FUZZY_CEILING) where batching risk is low — they're
# "almost certainly same" cases that need quick confirmation. Batching
# amortizes the per-call CLI overhead (Node.js boot, auth, HTTP round-trip)
# and the Max-plan rate-limit cost across N pair decisions. Sonnet (the
# harder ambiguous cases) stays unbatched to preserve full reasoning.
HAIKU_BATCH_SIZE = 5


def _pair_cache_key(a: dict, b: dict) -> str:
    return "|".join(sorted([
        f"{a['franchisee_name']}:{a['brand']}:{a['state']}",
        f"{b['franchisee_name']}:{b['brand']}:{b['state']}",
    ]))


def ai_batch_same_person(pairs_with_scores: list, cache: dict) -> list:
    """
    Batched Haiku call: evaluates multiple "easy" pairs in one CLI invocation.
    Input: list of (a, b, fuzzy_score) — all MUST be Haiku-bound.
    Returns: list of (is_match, reason) in input order.
    Populates the cache for each fresh decision.
    Falls back to individual calls on parse failure or shape mismatch so
    accuracy is never silently degraded.
    """
    if not pairs_with_scores:
        return []

    # If only one pair, skip batching overhead
    if len(pairs_with_scores) == 1:
        a, b, s = pairs_with_scores[0]
        return [ai_is_same_person(a, b, s, cache)]

    lines = ["Evaluate each pair of franchise operator records below. Decide, for each, whether the two records refer to the same real person.\n"]
    for i, (a, b, _) in enumerate(pairs_with_scores, 1):
        lines.append(
            f"Pair {i}:\n"
            f"  A: Name={a['franchisee_name']} | Brand={a['brand']} | State={a['state']} | City={a.get('city','')} | Addr={a.get('raw_address','')}\n"
            f"  B: Name={b['franchisee_name']} | Brand={b['brand']} | State={b['state']} | City={b.get('city','')} | Addr={b.get('raw_address','')}\n"
        )
    lines.append(
        "Consider for each pair independently:\n"
        "- Nicknames (Mike=Michael, Bob=Robert, Bill=William, etc.)\n"
        "- Overlapping geography / same state\n"
        "- Plausibility of multi-brand ownership\n"
        "- Middle initials or suffixes shouldn't prevent a match if everything else aligns\n\n"
        f"Return a JSON array with exactly {len(pairs_with_scores)} entries, one per pair, in the same order:\n"
        "[\n"
        '  {"pair": 1, "match": true/false, "confidence": 0-100, "reason": "one sentence"},\n'
        "  ...\n"
        "]\n"
        "Return only the JSON array, no other text."
    )
    prompt = "\n".join(lines)
    system = """You are an expert at franchise operator identity resolution.
You determine whether two names in different FDDs refer to the same real-world person.
Return only valid JSON."""

    raw = _run_claude_cli(prompt, system, HAIKU_MODEL)

    if not raw:
        # CLI exhausted retries — fall back to individual calls (each will
        # re-attempt and record-skip on its own, preserving existing behavior).
        log(f"    ⚠️  Batch call failed entirely — falling back to individual calls for {len(pairs_with_scores)} pairs")
        return [ai_is_same_person(a, b, s, cache) for a, b, s in pairs_with_scores]

    parsed = None
    try:
        clean = re.sub(r'```(?:json)?', '', raw).strip().rstrip('`').strip()
        parsed = json.loads(clean)
    except Exception:
        pass

    if not (isinstance(parsed, list) and len(parsed) == len(pairs_with_scores)):
        got = type(parsed).__name__ if parsed is not None else "None"
        got_len = len(parsed) if isinstance(parsed, list) else "n/a"
        log(f"    ⚠️  Batch response shape mismatch (expected list of {len(pairs_with_scores)}, got {got} len={got_len}) — falling back to individual calls")
        return [ai_is_same_person(a, b, s, cache) for a, b, s in pairs_with_scores]

    results = []
    for (a, b, fuzzy_score), entry in zip(pairs_with_scores, parsed):
        if not isinstance(entry, dict):
            # Per-entry fallback
            results.append(ai_is_same_person(a, b, fuzzy_score, cache))
            continue
        match = bool(entry.get("match", False))
        reason = entry.get("reason", "")
        confidence = entry.get("confidence", 0)
        if match and confidence < 70:
            match = False
            reason = f"Low confidence ({confidence}) — treating as separate"
        cache[_pair_cache_key(a, b)] = {"match": match, "reason": reason}
        results.append((match, reason))

    return results


# ── IDENTITY RESOLUTION ───────────────────────────────────────────────────────

def resolve_identities(records: list[dict]) -> dict:
    """
    Build a Union-Find structure mapping every record to a canonical identity.
    Returns {record_index: canonical_identity_id}

    Strategy:
    1. Each record starts as its own identity
    2. Same-brand grouping: merge records sharing an owner name (parens or person)
    3. Cross-brand matching: auto-merge obvious pairs, AI resolves ambiguous ones
    4. Merge matched identities using Union-Find
    """
    # Union-Find
    parent = list(range(len(records)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    # Record-identity map by object id — each record object maps to its
    # index in the records list. This survives records sharing (name, brand).
    rec_idx = {id(rec): i for i, rec in enumerate(records)}

    # ── Step 1: Exact same-brand merging ──────────────────────────────────────
    log(f"\n   Step 1 — Exact same-brand merge (identical strings)...")
    exact_merged = exact_same_brand_merge(records, find, union)
    log(f"      merged {exact_merged} duplicate records into existing groups")

    # ── Step 2: Same-brand owner grouping (parens/person pattern) ─────────────
    log(f"\n   Step 2 — Same-brand owner grouping (parenthetical pattern)...")
    brand_audit = resolve_same_brand(records, find, union)
    log(f"   {'Brand':<28} {'Records':>7}  {'Via parens':>10}  {'Via name':>8}  {'Ungrouped LLC':>13}")
    log(f"   {'─'*28} {'─'*7}  {'─'*10}  {'─'*8}  {'─'*13}")
    for brand, s in sorted(brand_audit.items()):
        flagged = "  ⚠️  check" if s['grouped_parens'] == 0 and s['grouped_person'] == 0 else ""
        log(f"   {brand:<28} {s['records']:>7}  {s['grouped_parens']:>10}  {s['grouped_person']:>8}  {s['ungrouped_llc']:>13}{flagged}")

    # ── Step 3: Fuzzy + AI matching (within AND across brands) ────────────────
    candidates = build_candidate_pairs(records)
    log(f"\n   Step 3 — Candidate pairs to evaluate: {len(candidates)}")

    # Load match cache
    match_cache = load_match_cache()
    ai_calls = 0
    cache_hits = 0
    auto_merged = 0
    ai_merged = 0
    rejected = 0
    total = len(candidates)
    PROGRESS_EVERY = 200

    # Buffer for Haiku-bound pairs needing a fresh AI call.
    # Each entry: (idx_a, idx_b, a, b, fuzzy_score)
    haiku_buffer = []

    def flush_haiku_buffer():
        nonlocal ai_calls, ai_merged, rejected
        if not haiku_buffer:
            return
        pairs = [(e[2], e[3], e[4]) for e in haiku_buffer]
        results = ai_batch_same_person(pairs, match_cache)
        for (idx_a, idx_b, a, b, _fs), (is_match, _reason) in zip(haiku_buffer, results):
            ai_calls += 1
            if is_match:
                union(idx_a, idx_b)
                ai_merged += 1
            else:
                rejected += 1
        save_match_cache(match_cache)
        haiku_buffer.clear()
        time.sleep(0.2)  # one rate-limit pause per batch, not per pair

    for i, (a, b, fuzzy_score) in enumerate(candidates, 1):
        idx_a = rec_idx[id(a)]
        idx_b = rec_idx[id(b)]

        # Already in same group
        if find(idx_a) == find(idx_b):
            if i % PROGRESS_EVERY == 0:
                log(f"      progress: {i}/{total}  auto={auto_merged}  ai={ai_merged}  rej={rejected}  hits={cache_hits}  fresh={ai_calls}  buf={len(haiku_buffer)}")
            continue

        if fuzzy_score >= FUZZY_HIGH:
            union(idx_a, idx_b)
            auto_merged += 1
        else:
            cache_key = _pair_cache_key(a, b)
            if cache_key in match_cache:
                # Cache hit — apply decision immediately (no network call)
                cached = match_cache[cache_key]
                cache_hits += 1
                if cached["match"]:
                    union(idx_a, idx_b)
                    ai_merged += 1
                else:
                    rejected += 1
            elif fuzzy_score >= SONNET_FUZZY_CEILING:
                # Easy pair → Haiku → BATCH it. Flush the buffer before any
                # Sonnet call below to keep call ordering deterministic.
                haiku_buffer.append((idx_a, idx_b, a, b, fuzzy_score))
                if len(haiku_buffer) >= HAIKU_BATCH_SIZE:
                    flush_haiku_buffer()
            else:
                # Hard pair → Sonnet → single call (unbatched for accuracy)
                # Flush any pending Haiku batch first so decisions are applied
                # in the same order pairs were encountered.
                flush_haiku_buffer()
                is_match, _reason = ai_is_same_person(a, b, fuzzy_score, match_cache)
                ai_calls += 1
                if is_match:
                    union(idx_a, idx_b)
                    ai_merged += 1
                else:
                    rejected += 1
                save_match_cache(match_cache)
                time.sleep(0.2)

        if i % PROGRESS_EVERY == 0:
            log(f"      progress: {i}/{total}  auto={auto_merged}  ai={ai_merged}  rej={rejected}  hits={cache_hits}  fresh={ai_calls}  buf={len(haiku_buffer)}")

    # Flush any remaining Haiku pairs
    flush_haiku_buffer()

    log(f"   Auto-merged (fuzzy ≥{FUZZY_HIGH}): {auto_merged}")
    log(f"   AI-merged (reasoned):        {ai_merged}  ({ai_calls} fresh AI calls, {cache_hits} cache hits)")
    log(f"   Rejected:                    {rejected}")

    skipped = load_skipped()
    if skipped:
        log(f"   ⚠️  Skipped (call failed):     {len(skipped)}  → see {SKIPPED_FILE.name}")
        log(f"       (re-run the script to retry these; they are NOT in match_cache)")

    return {i: find(i) for i in range(len(records))}


# ── AGGREGATE ─────────────────────────────────────────────────────────────────

def build_master(records: list[dict], identity_map: dict) -> list[dict]:
    """
    Aggregate records by identity group into one row per operator.
    Picks the most complete name as canonical.
    """
    groups = defaultdict(list)
    for i, rec in enumerate(records):
        group_id = identity_map[i]
        groups[group_id].append(rec)

    operators = []
    for group_id, recs in groups.items():
        # Canonical name = longest name in the group (most complete)
        canonical_name = max((r["franchisee_name"] for r in recs), key=len)

        brands = sorted(set(r["brand"] for r in recs if r["brand"]))
        states = sorted(set(r["state"] for r in recs if r["state"]))
        total_units = len(recs)

        # Primary state = most frequent
        state_freq = defaultdict(int)
        for r in recs:
            if r["state"]:
                state_freq[r["state"]] += 1
        primary_state = max(state_freq, key=state_freq.get) if state_freq else ""

        # All name variants (useful for Apollo search)
        name_variants = sorted(set(r["franchisee_name"] for r in recs))
        name_variants_str = " / ".join(name_variants) if len(name_variants) > 1 else ""

        phone = next((r["restaurant_phone"] for r in recs if r.get("restaurant_phone")), "")
        sample_addr = next((r["raw_address"] for r in recs if r.get("raw_address")), "")

        operators.append({
            "franchisee_name":   canonical_name,
            "name_variants":     name_variants_str,
            "total_units":       total_units,
            "brands_operated":   " | ".join(brands),
            "brand_count":       len(brands),
            "is_multi_brand":    "Yes" if len(brands) > 1 else "No",
            "primary_state":     primary_state,
            "all_states":        " | ".join(states),
            "state_count":       len(states),
            "restaurant_phone":  phone,
            "sample_address":    sample_addr,
            # Apollo fields — filled by you
            "apollo_company":    "",
            "apollo_email":      "",
            "apollo_linkedin":   "",
            "apollo_title":      "",
        })

    operators.sort(key=lambda x: -x["total_units"])
    return operators


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Start a fresh log for this run
    try:
        LOG_FILE.write_text("", encoding="utf-8")
    except Exception:
        pass

    log(f"🔗 Identity matching via Claude Code CLI (Max plan)")
    log(f"   Log file: {LOG_FILE}")

    records = load_all_raw()
    if not records:
        return

    log()
    log(f"🔗 Resolving cross-brand identities...")
    identity_map = resolve_identities(records)

    log()
    log(f"📊 Building master registry...")
    operators = build_master(records, identity_map)

    # Write master CSV
    fields = [
        "franchisee_name", "name_variants", "total_units",
        "brands_operated", "brand_count", "is_multi_brand",
        "primary_state", "all_states", "state_count",
        "restaurant_phone", "sample_address",
        "apollo_company", "apollo_email", "apollo_linkedin", "apollo_title",
    ]
    with open(MASTER_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(operators)

    # Stats
    multi = [o for o in operators if o["is_multi_brand"] == "Yes"]
    tiers = {
        "< 5 units":       [o for o in operators if o["total_units"] < 5],
        "5–15 (Tier B)":   [o for o in operators if 5   <= o["total_units"] <= 15],
        "16–200 (Tier A)": [o for o in operators if 16  <= o["total_units"] <= 200],
        "201+ units":      [o for o in operators if o["total_units"] > 200],
    }

    log()
    log("═" * 55)
    log(f"✅ MASTER REGISTRY: {MASTER_FILE.name}")
    log("─" * 55)
    log(f"   Total unique operators : {len(operators)}")
    log(f"   Multi-brand operators  : {len(multi)}  ← highest priority")
    log(f"\n   Distribution:")
    for label, group in tiers.items():
        bar = "█" * min(len(group) // 5, 35)
        log(f"   {label:<18} {len(group):>5}  {bar}")

    if multi:
        log(f"\n   Top multi-brand operators:")
        log(f"   {'Name':<35} {'Units':>5}  Brands")
        log(f"   {'─'*35} {'─'*5}  {'─'*30}")
        for op in [o for o in operators if o["is_multi_brand"] == "Yes"][:10]:
            log(f"   {op['franchisee_name']:<35} {op['total_units']:>5}  {op['brands_operated'][:35]}")

    log(f"\n  ➡️  Run 3_filter_export.py next")

    # Print call-timing stats so CLI_TIMEOUT / RETRY_WAIT_SECS can be tuned
    print_timing_stats()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # Still print stats on Ctrl-C so partial runs are diagnosable
        log("\n⚠️  Interrupted — printing partial timing stats")
        print_timing_stats()
        raise
