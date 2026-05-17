#!/usr/bin/env python3
"""
SCRIPT 3 — FILTER + APOLLO EXPORT
====================================
Reads master_franchisees.csv → applies ICP filter (5-50 units)
→ segments into Tier A / Tier B → outputs Apollo-ready CSVs

Usage:
    python 3_filter_export.py

Outputs:
    output/icp_tier_a.csv      ← 16-50 units
    output/icp_tier_b.csv      ← 5-15 units
    output/icp_combined.csv    ← all ICP
    output/excluded_large.csv
    output/excluded_small.csv
"""

import csv, re, sys
from pathlib import Path

# Fix Windows console encoding for emoji output
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUTPUT_DIR  = Path(__file__).parent / "output"
MASTER_FILE = OUTPUT_DIR / "master_franchisees.csv"

TIER_A = (16, 200)   # larger multi-unit operators with HR/Ops layers
TIER_B = (5, 15)     # founder-led smaller operators

SEQUENCES = {"A": "nova_qsr_tier_a", "B": "nova_qsr_tier_b"}

MESSAGING = {
    "A": (
        "Has HR/Ops layer. Pain: no centralized hiring system across 16-200 locations. "
        "Pitch: Nova as command center for all-location hiring — one dashboard, "
        "AI screening, consistent process."
    ),
    "B": (
        "Founder-led. Owner personally reviews candidates. "
        "Pain: no bandwidth for structured interviews at scale. "
        "Pitch: Nova as their first hire — automates screening so they stay in operations."
    ),
}


def load_master():
    if not MASTER_FILE.exists():
        print(f"❌ {MASTER_FILE.name} not found. Run 2_ai_match.py first.")
        return []
    with open(MASTER_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def extract_first_name(name: str) -> str:
    """
    Try to extract a person's first name from the canonical franchisee name.
    Prefers the owner name inside parentheses: 'Rockham LLC (Drew Smith)' → 'Drew'
    Falls back to the first word of the name if no parenthetical exists.
    """
    m = re.search(r'\(([^)]+)\)', name)
    if m:
        owner = m.group(1).strip()
        return owner.split()[0] if owner.split() else name.split()[0]
    return name.split()[0] if name.split() else ""


def enrich(row: dict, tier: str) -> dict:
    name = row.get("franchisee_name", "")
    first = extract_first_name(name)
    units = int(row.get("total_units", 0))

    # Apollo search hint — use owner name from parens if available, else full name
    m = re.search(r'\(([^)]+)\)', name)
    search_name = m.group(1).strip() if m else name
    state = row.get("primary_state", "")
    brands = row.get("brands_operated", "").split(" | ")
    primary_brand = brands[0] if brands else ""
    apollo_hint = f'{search_name} {primary_brand} franchisee {state}'

    return {
        "franchisee_name":    name,
        "name_variants":      row.get("name_variants", ""),
        "first_name":         first,
        "total_units":        units,
        "brands_operated":    row.get("brands_operated", ""),
        "brand_count":        row.get("brand_count", ""),
        "is_multi_brand":     row.get("is_multi_brand", ""),
        "primary_state":      state,
        "all_states":         row.get("all_states", ""),
        "tier":               tier,
        "instantly_sequence": SEQUENCES[tier],
        "messaging_note":     MESSAGING[tier],
        "apollo_search_hint": apollo_hint,
        # You fill these in Apollo
        "apollo_company":     "",
        "apollo_email":       "",
        "apollo_linkedin":    "",
        "apollo_title":       "",
        "restaurant_phone":   row.get("restaurant_phone", ""),
        "sample_address":     row.get("sample_address", ""),
    }


def write_csv(rows, path):
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"   ✅ {path.name:<40} {len(rows):>5} rows")


def main():
    operators = load_master()
    if not operators:
        return

    tier_a, tier_b, large, small = [], [], [], []

    for row in operators:
        try:
            u = int(row.get("total_units", 0))
        except ValueError:
            continue
        if   TIER_A[0] <= u <= TIER_A[1]: tier_a.append(enrich(row, "A"))
        elif TIER_B[0] <= u <= TIER_B[1]: tier_b.append(enrich(row, "B"))
        elif u > TIER_A[1]:               large.append(row)
        else:                             small.append(row)

    combined = sorted(tier_a + tier_b, key=lambda x: -int(x["total_units"]))

    print("💾 Writing exports...")
    write_csv(tier_a,    OUTPUT_DIR / "icp_tier_a.csv")
    write_csv(tier_b,    OUTPUT_DIR / "icp_tier_b.csv")
    write_csv(combined,  OUTPUT_DIR / "icp_combined.csv")
    write_csv(large,     OUTPUT_DIR / "excluded_large.csv")
    write_csv(small,     OUTPUT_DIR / "excluded_small.csv")

    multi = [r for r in combined if r["is_multi_brand"] == "Yes"]

    print(f"\n{'═'*55}")
    print(f"✅ EXPORT COMPLETE")
    print(f"{'─'*55}")
    print(f"   Tier A  (16-200 units) : {len(tier_a):>4}  → nova_qsr_tier_a")
    print(f"   Tier B  (5-15 units)   : {len(tier_b):>4}  → nova_qsr_tier_b")
    print(f"   Total ICP              : {len(combined):>4}")
    print(f"   Multi-brand in ICP     : {len(multi):>4}  ← start here")
    print(f"{'─'*55}")
    print(f"\n📌 APOLLO WORKFLOW")
    print(f"   Each row has an 'apollo_search_hint' column.")
    print(f"   Copy that string directly into Apollo's search bar.")
    print(f"   Fill: apollo_company, apollo_email, apollo_linkedin, apollo_title")
    print(f"   Start with multi-brand operators (is_multi_brand = Yes)")
    print(f"   Then Tier A sorted by total_units descending")


if __name__ == "__main__":
    main()
