Run the Texas Comptroller Taxable Entity Search scraper.

Input file: $ARGUMENTS (path to CSV with entity_name column, defaults to states/tx/input/franchisees.csv)

Steps:
1. Activate the venv: `source .venv/Scripts/activate` (Windows) or `source .venv/bin/activate` (Mac/Linux)
2. Output goes to states/tx/output/enriched.csv (per common naming convention).
3. TX Comptroller has no anti-bot — headless is fine. Use --show only if behavior changes.
4. Run `python states/tx/enrich.py states/tx/input/franchisees.csv states/tx/output/enriched.csv --limit 5` first as a smoke test.
5. When it finishes, show me:
   - How many of the 5 rows matched (companies — person rows are auto-skipped because TX Comptroller indexes business entities)
   - One sample row with all tx_* fields — call out tx_likely_owner_name, tx_status, tx_sos_file_number, and tx_operator_pattern
   - Counts of "ACTIVE" status entities matched
   - Whether any rows show errors (check states/tx/debug/ if so)
6. Ask if I want to proceed with the full batch (no --limit).
7. If yes, re-run. Resumable — it'll skip the 5 already done and run cross-row analysis on the full set at the end.

Notes:
- Person-typed input rows are auto-skipped (TX Comptroller indexes entities, not individuals).
- Detail page reached via direct URL: https://comptroller.texas.gov/taxes/franchise/account-status/search/<taxpayer_number> (no clicking needed once we have the taxpayer number).
- TX agent is usually a corporate registered agent (CSC, Corporate Creations) — pattern correctly classified as service_agent. Owner extraction via "individual_agent_offsite" works when the agent is a named individual.
