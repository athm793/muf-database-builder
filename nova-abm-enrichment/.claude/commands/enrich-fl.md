Run the Florida Sunbiz bulk-match enrichment script.

Input file: $ARGUMENTS (path to CSV with entity_name column, defaults to states/fl/input/franchisees.csv)

Steps:
1. Activate the venv: `source .venv/Scripts/activate` (Windows) or `source .venv/bin/activate` (Mac/Linux)
2. Output goes to states/fl/output/enriched.csv (per common naming convention).
3. Check whether the bulk cordata file exists at states/fl/bulk/cordata.txt:
   - If missing or older than 90 days, the script auto-downloads via SFTP (1.74 GB zipped, takes a few minutes). Confirm with me before kicking off if it'll be a fresh download.
   - If a cordata.txt is present locally, use --bulk-file to point to it.
4. Run `python states/fl/enrich.py states/fl/input/franchisees.csv states/fl/output/enriched.csv --limit 5` first as a smoke test.
5. When it finishes, show me:
   - How many of the 5 rows matched
   - One sample row with all fl_* fields — call out fl_likely_owner_name and fl_operator_pattern in particular
   - Any rows where fl_name_similarity is below 0.75 (suspect matches)
6. Ask if I want to proceed with the full batch (no --limit).
7. If yes, re-run. Same script, resumable — it'll skip the 5 already done and run cross-row analysis on the full set at the end.
