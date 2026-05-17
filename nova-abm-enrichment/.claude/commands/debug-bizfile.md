Inspect the latest bizfile_debug artifacts and propose a fix.

Steps:
1. List files in bizfile_debug/ sorted by modification time (most recent first).
2. For the most recent error pair (.png + .html with the same timestamp):
   - Show me the screenshot path so I can open it
   - Grep the HTML for: <title>, any text matching /error|not found|failed|blocked/i, the current value of the search input
   - Tell me what state bizfile was in when the error fired (e.g., stuck on search page, no results rendered, detail drawer failed to open)
3. Cross-reference with scripts/bizfile_enrich.py to identify which selector or flow step broke.
4. Propose the minimal fix. Wait for my approval before editing the script.
5. After editing, suggest I re-run the specific row(s) that failed.
