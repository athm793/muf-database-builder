# Nova ABM Enrichment

Purpose: Enrich the 934-franchisee list (extracted from FDDs) with contact
intelligence for Nova outbound. State SOS lookups → LinkedIn → email waterfall.

## Project context
- Part of Peoplebox Nova GTM (AI interviewer for franchise operators)
- Source data: QSR franchisees extracted from FDDs via the FDD pipeline
- Target output: franchisee LLC → owner name → LinkedIn URL → email/phone
- Downstream consumer: Instantly.ai sequences + LinkedIn outreach

## Folder structure
Each state is a self-contained artifact under `states/<state>/`. Common naming convention across all states: every state has the same file/folder names so working with one state translates directly to working with another.

```
nova-abm-enrichment/
├── states/
│   ├── ca/
│   │   ├── enrich.py            # state-specific enrichment script (scrape or bulk-match)
│   │   ├── input/
│   │   │   ├── franchisees.csv         # input CSV with entity_name column
│   │   │   └── sample_franchisees.csv  # tiny sample for testing
│   │   ├── output/
│   │   │   ├── enriched.csv            # enriched output
│   │   │   └── drawer_text/            # per-entity drawer text dumps (scrape only)
│   │   ├── debug/               # error screenshots + HTML on failures
│   │   └── bulk/                # cached bulk dataset (only present for bulk-match states)
│   ├── fl/
│   │   ├── enrich.py            # bulk-match against Sunbiz cordata.zip (multi-shard Deflate64)
│   │   ├── input/franchisees.csv
│   │   ├── output/enriched.csv
│   │   └── bulk/cordata.zip     # downloaded via SFTP, NOT extracted — read directly
│   ├── ny/                      # data.ny.gov Active Corporations bulk
│   ├── co/                      # data.colorado.gov Business Entities bulk
│   ├── tx/                      # TX Comptroller Playwright scrape
│   └── ...                      # one folder per state, same shape
├── .claude/
│   └── commands/                # /enrich-<state> per state
├── .venv/
└── CLAUDE.md
```

**Naming rule of thumb**: every file/folder name inside `states/<state>/` is identical across states. State context comes from the path, not the filename. (Exception: `bulk/<dataset>.txt` keeps the dataset's real name like `cordata.txt` since it's the canonical file from the source.)

## Pipeline stages
1. **SOS enrichment** — state Secretary of State lookup for each LLC
   - California: `states/ca/enrich.py` (Playwright + stealth, Imperva-protected, scrape path) — DONE (46/53, 31 owners)
   - Florida: `states/fl/enrich.py` (bulk-match against Sunbiz cordata, SFTP throttled to 140 KB/s; multi-shard Deflate64 zip via inflate64 monkey-patch) — DONE (69/125, 62 owners)
   - New York: `states/ny/enrich.py` (bulk-match against data.ny.gov Active Corporations CSV, HTTPS Socrata) — DONE (407/554, 230 owners)
   - Colorado: `states/co/enrich.py` (bulk-match against data.colorado.gov Business Entities CSV, HTTPS Socrata) — DONE (150/208, 92 owners)
   - Texas: `states/tx/enrich.py` (Playwright headless scrape against TX Comptroller, no anti-bot) — DONE (92/101 companies, 60 owners)
   - **5-state total: 1,143 inputs → 764 matches (67%) → 475 likely owners**
   - Other states: TODO. Bulk path exhausted (WA/AZ/KY/MN all paid for commercial). Remaining states are scrape-only — see skill `build-state-sos-scraper` for priority order.
2. **LinkedIn match** — principal name + company → LinkedIn URL (TODO)
3. **Email waterfall** — free-tier rotation across Hunter/Snov/Apollo (TODO)
4. **Phone fallback** — TruePeopleSearch for Tier 1 only (TODO)

## Design principles
- Free-tier only. No paid APIs.
- Every script resumable (Ctrl+C safe, skips completed rows on re-run)
- Incremental CSV writes — never hold the full dataset in memory
- Rate-limit with jitter to stay polite with public SOS sites
- On error: save screenshot + HTML to `states/<state>/debug/`. Don't die silently.
- Each state is a separate artifact — no cross-state file dependencies. Helpers may be duplicated across state scripts (refactor to `shared/` only when 3+ states share identical code).

## Data conventions
- Input CSVs live in `states/<state>/input/`, output in `states/<state>/output/`
- Required input column: `entity_name`
- Output columns prefixed by source: `bf_*` for CA bizfile, `sz_*` for Sunbiz, etc.
- Universal schema (must be consistent across states for orchestrator merge): see `~/.claude/skills/build-state-sos-scraper/SKILL.md` "Universal output schema" section
- Never commit anything under `states/*/input/` (except sample_input.csv) or `states/*/output/` or `states/*/bulk/` — may contain PII or be large

## Running a state scraper
```bash
source .venv/Scripts/activate   # Windows; .venv/bin/activate on Mac/Linux
python states/ca/enrich.py states/ca/input/franchisees.csv states/ca/output/enriched.csv --show --limit 5
```

Or use the slash command: `/enrich-ca`

## Building scrapers for new states
Use the skill `build-state-sos-scraper` (auto-routes between bulk-match and Playwright scrape). State priority and routing is documented there.

## Useful context for Claude Code
- I (the user) work on GTM at Peoplebox, not engineering. Prefer clear commands over abstract refactors.
- Always show me the command before running it. Confirm before touching files in `states/*/input/`.
- When a scraper fails on a row, check `states/<state>/debug/` first — there will be a screenshot and HTML of the failure state.
