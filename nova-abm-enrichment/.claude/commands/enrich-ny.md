Run the NY State Active Corporations bulk-match enrichment script.

Input file: $ARGUMENTS (path to CSV with entity_name column, defaults to states/ny/input/franchisees.csv)

Steps:
1. Activate the venv: `source .venv/Scripts/activate` (Windows) or `source .venv/bin/activate` (Mac/Linux)
2. Output goes to states/ny/output/enriched.csv (per common naming convention).
3. Check whether the bulk active_corps.csv exists at states/ny/bulk/active_corps.csv:
   - If missing or older than 30 days, the script auto-downloads via HTTPS from data.ny.gov (resilient with auto-retry + resume + fsync). Confirm with me before kicking off a fresh download.
   - If a CSV is present locally, use --bulk-file to point to it.
4. Run `python states/ny/enrich.py states/ny/input/franchisees.csv states/ny/output/enriched.csv --limit 5` first as a smoke test.
5. When it finishes, show me:
   - How many of the 5 rows matched
   - One sample row with all ny_* fields — call out ny_likely_owner_name (driven by chairman_name when filed) and ny_operator_pattern in particular
   - Any rows where ny_name_similarity is below 0.75 (suspect matches)
   - Counts of ny_chairman_name populated vs blank (chairman is gold but optional in NY filings)
6. Ask if I want to proceed with the full batch (no --limit).
7. If yes, re-run. Resumable — it'll skip the 5 already done and run cross-row analysis on the full set at the end.
