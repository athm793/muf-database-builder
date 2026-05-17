Run the California bizfile enrichment script on an input CSV.

Input file: $ARGUMENTS (path to CSV with entity_name column, defaults to states/ca/input/franchisees.csv)

Steps:
1. Activate the venv: `source .venv/Scripts/activate` (Windows) or `source .venv/bin/activate` (Mac/Linux)
2. Output goes to states/ca/output/enriched.csv (per common naming convention).
3. Run `python states/ca/enrich.py states/ca/input/franchisees.csv states/ca/output/enriched.csv --show --limit 5` first.
4. When it finishes, show me:
   - How many of the 5 rows got a match (`bf_match_count > 0`)
   - One sample row with all bf_* fields — call out bf_likely_owner_name and bf_operator_pattern in particular
   - Any rows where bf_name_similarity is below 0.75 (suspect matches)
   - Whether any errors landed in states/ca/debug/
5. Ask if I want to proceed with the full batch (no --show, no --limit).
6. If yes, re-run. Same script, resumable — it'll skip the 5 already done.
