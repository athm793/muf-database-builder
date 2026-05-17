# FDD Pipeline v2 — AI-Powered

Uses Claude API for every hard part:
- Finding the franchisee list regardless of what it's called
- Extracting records from any format
- Matching the same operator across different brand FDDs

---

## Setup

```bash
# Dependencies
pip install anthropic thefuzz python-Levenshtein

# pdftotext (Mac)
brew install poppler

# pdftotext (Ubuntu)
sudo apt-get install poppler-utils

# Set your API key
export ANTHROPIC_API_KEY=sk-ant-...
```

---

## Folder Structure

```
fdd_pipeline_v2/
    pdfs/                ← DROP ALL FDD PDFs HERE
    cache/               ← API response cache (auto-created)
    output/
        raw/             ← per-brand CSVs (auto-created)
    1_ai_extract.py
    2_ai_match.py
    3_filter_export.py
    README.md
```

---

## Run

```bash
python 1_ai_extract.py    # Extract from all PDFs
python 2_ai_match.py      # Match identities across brands
python 3_filter_export.py # Filter ICP + export
```

---

## Output Files

All outputs are written to `Code/output/`:

| File | Script | Contents |
|---|---|---|
| `output/raw/<Brand>_raw.csv` | Script 1 | Raw extracted franchisee records per brand |
| `output/master_franchisees.csv` | Script 2 | Every unique operator with unit counts, brand info, and multi-brand flags |
| `output/icp_tier_a.csv` | Script 3 | Tier A prospects (16–50 units) — Apollo-ready |
| `output/icp_tier_b.csv` | Script 3 | Tier B prospects (5–15 units) — Apollo-ready |
| `output/icp_combined.csv` | Script 3 | Tier A + B combined, sorted by total units — main working file |
| `output/excluded_large.csv` | Script 3 | Operators with 51+ units (too large for ICP) |
| `output/excluded_small.csv` | Script 3 | Operators with fewer than 5 units (not ICP) |

> **Start with `icp_combined.csv`** — open in Excel and work top-down by `total_units`. Multi-brand operators (`is_multi_brand = Yes`) are highest priority.

---

## Caching

All Claude API calls are cached in `/cache/`.
Re-running scripts after adding new PDFs only calls the API for new files.
Safe to re-run at any time.

---

## What "match" means

Script 2 asks Claude to resolve whether two names in different FDDs
are the same real person, considering:
- Name variants (Mike = Michael, Bill = William)
- Same state / overlapping geography
- Plausibility of owning multiple brands

The `name_variants` column in the master CSV shows all name spellings
found for that operator — useful context for Apollo searches.

---

## Apollo Workflow

1. Open `icp_tier_a.csv` in Google Sheets
2. Column `apollo_search_hint` is pre-filled — paste directly into Apollo
3. Fill: `apollo_company`, `apollo_email`, `apollo_linkedin`, `apollo_title`
4. Priority order:
   - `is_multi_brand = Yes` (highest — cross-brand operators)
   - Tier A sorted by `total_units` descending
   - Then Tier B

---

## Troubleshooting

**Brand shows 0 records after extraction:**
Check `cache/<filename>.json` — if it has `"error": "section_not_found"`,
the AI couldn't locate the franchisee section. This can happen with:
- Scanned PDFs (no extractable text)
- Very non-standard FDD formats
Delete the cache file, re-run, and inspect the output logs.

**Fuzzy threshold tuning (2_ai_match.py):**
- `FUZZY_LOW = 60`  — raise to 70 if getting too many AI calls
- `FUZZY_HIGH = 96` — lower to 90 if obvious matches aren't auto-merging

**API rate limits:**
Scripts include sleep() calls. If you hit rate limits on large batches,
increase the sleep values in 1_ai_extract.py (line ~150) and
2_ai_match.py (line ~170).
