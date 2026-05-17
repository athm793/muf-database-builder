#!/usr/bin/env python3
"""
PREVIEW — Snapshot of stage 2's current match cache as master + tier CSVs.
===========================================================================
Reads:
  - output/raw/*_raw.csv (same as 2_ai_match.py)
  - cache/match_cache.json (partial decisions from in-progress stage 2)

Writes to output/preview/ (separate from final outputs so the running
stage 2 is not disturbed):
  - master_franchisees.csv
  - icp_tier_a.csv
  - icp_tier_b.csv
  - icp_combined.csv
  - excluded_large.csv
  - excluded_small.csv

Safe to run while 2_ai_match.py is still executing — reads are atomic
relative to the cache's save-after-every-call behavior. Regenerate anytime
to see updated progress.

Usage:
    python preview_current_state.py
"""

import sys
from pathlib import Path

# Reuse the live logic from 2_ai_match.py and 3_filter_export.py
sys.path.insert(0, str(Path(__file__).parent))

from importlib import import_module
ai_match = import_module("2_ai_match")
filter_export = import_module("3_filter_export")

import csv, json
from collections import defaultdict

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


PREVIEW_DIR = Path(__file__).parent / "output" / "preview"
PREVIEW_DIR.mkdir(parents=True, exist_ok=True)


def build_identity_map_from_cache(records: list[dict]) -> dict:
    """
    Replay the deterministic steps (exact merge + owner grouping) plus
    the cached AI decisions, without making any new API calls.
    Returns {record_index: canonical_identity_id}.
    """
    n = len(records)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    # Step 1 — Exact same-brand merge
    print("[preview] Step 1: exact same-brand merge")
    exact_merged = ai_match.exact_same_brand_merge(records, find, union)
    print(f"          merged {exact_merged} exact duplicates")

    # Step 2 — Parens/person-name grouping
    print("[preview] Step 2: same-brand owner grouping")
    ai_match.resolve_same_brand(records, find, union)

    # Step 4 — Replay cached AI decisions (no new API calls)
    cache = ai_match.load_match_cache()
    print(f"[preview] Step 4: replaying {len(cache)} cached AI decisions")

    # Build a lookup from (franchisee_name, brand, state) → list of indices
    by_key = defaultdict(list)
    for i, rec in enumerate(records):
        key = f"{rec['franchisee_name']}:{rec['brand']}:{rec['state']}"
        by_key[key].append(i)

    applied = 0
    missing = 0
    for cache_key, decision in cache.items():
        if not decision.get("match"):
            continue
        # Cache keys are sorted pairs of "name:brand:state" joined by "|"
        try:
            left, right = cache_key.split("|", 1)
        except ValueError:
            continue
        left_indices = by_key.get(left, [])
        right_indices = by_key.get(right, [])
        if not left_indices or not right_indices:
            missing += 1
            continue
        union(left_indices[0], right_indices[0])
        applied += 1

    print(f"          applied {applied} cached matches ({missing} had no matching records)")

    return {i: find(i) for i in range(n)}


def main():
    print(f"[preview] Loading raw records...")
    records = ai_match.load_all_raw()
    if not records:
        print("[preview] No raw records — run 1_ai_extract.py first.")
        return

    identity_map = build_identity_map_from_cache(records)

    print(f"[preview] Building master registry...")
    operators = ai_match.build_master(records, identity_map)

    # Write preview master
    master_path = PREVIEW_DIR / "master_franchisees.csv"
    fields = [
        "franchisee_name", "name_variants", "total_units",
        "brands_operated", "brand_count", "is_multi_brand",
        "primary_state", "all_states", "state_count",
        "restaurant_phone", "sample_address",
        "apollo_company", "apollo_email", "apollo_linkedin", "apollo_title",
    ]
    with open(master_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(operators)
    print(f"[preview] Wrote {master_path}  ({len(operators)} operators)")

    # Run stage 3 logic against preview master
    print(f"[preview] Running tier split...")
    tier_a, tier_b, large, small = [], [], [], []
    for row in operators:
        try:
            u = int(row.get("total_units", 0))
        except ValueError:
            continue
        if   filter_export.TIER_A[0] <= u <= filter_export.TIER_A[1]:
            tier_a.append(filter_export.enrich(row, "A"))
        elif filter_export.TIER_B[0] <= u <= filter_export.TIER_B[1]:
            tier_b.append(filter_export.enrich(row, "B"))
        elif u > filter_export.TIER_A[1]:
            large.append(row)
        else:
            small.append(row)
    combined = sorted(tier_a + tier_b, key=lambda x: -int(x["total_units"]))

    def write_csv(rows, path):
        if not rows:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"          {path.name:<30}  {len(rows):>5} rows")

    write_csv(tier_a,   PREVIEW_DIR / "icp_tier_a.csv")
    write_csv(tier_b,   PREVIEW_DIR / "icp_tier_b.csv")
    write_csv(combined, PREVIEW_DIR / "icp_combined.csv")
    write_csv(large,    PREVIEW_DIR / "excluded_large.csv")
    write_csv(small,    PREVIEW_DIR / "excluded_small.csv")

    multi = [o for o in operators if o["is_multi_brand"] == "Yes"]
    print()
    print("═" * 55)
    print("PREVIEW COMPLETE")
    print("─" * 55)
    print(f"  Total unique operators : {len(operators)}")
    print(f"  Multi-brand operators  : {len(multi)}")
    print(f"  Tier A (16-200 units)  : {len(tier_a)}")
    print(f"  Tier B (5-15 units)    : {len(tier_b)}")
    print(f"  Total ICP              : {len(combined)}")
    print()
    print(f"  Outputs in: {PREVIEW_DIR}")
    print(f"  (Re-run this script anytime to refresh the preview)")


if __name__ == "__main__":
    main()
