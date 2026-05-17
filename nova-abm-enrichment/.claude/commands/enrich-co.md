Run the Colorado Business Entities bulk-match enrichment script.

Input file: $ARGUMENTS (path to CSV with entity_name column, defaults to states/co/input/franchisees.csv)

Steps:
1. Activate the venv: `source .venv/Scripts/activate` (Windows) or `source .venv/bin/activate` (Mac/Linux)
2. Output goes to states/co/output/enriched.csv (per common naming convention).
3. Check whether the bulk business_entities.csv exists at states/co/bulk/business_entities.csv:
   - If missing or older than 30 days, the script auto-downloads via HTTPS from data.colorado.gov (resilient with auto-retry + resume + fsync). Confirm with me before kicking off a fresh download (~700 MB).
   - If a CSV is present locally, use --bulk-file to point to it.
4. Run `python states/co/enrich.py states/co/input/franchisees.csv states/co/output/enriched.csv --limit 5` first as a smoke test.
5. When it finishes, show me:
   - How many of the 5 rows matched
   - One sample row with all co_* fields — call out co_likely_owner_name, co_status, and co_operator_pattern in particular
   - Any rows where co_name_similarity is below 0.75 (suspect matches)
   - Counts of "Good Standing" vs "Delinquent" vs "Voluntarily Dissolved" entities matched
6. Ask if I want to proceed with the full batch (no --limit).
7. If yes, re-run. Resumable — it'll skip the 5 already done and run cross-row analysis on the full set at the end.
