# MUF Database Builder

Turns Franchise Disclosure Documents (FDDs) into a clean, deduplicated database
of franchise operators (the ICP list used for outreach). A non-technical user
adds FDDs through a simple point-and-click window — no terminal required.

## What the tool does

Pick (or fetch) an FDD → it extracts the franchisee list → re-matches operators
across every brand → rebuilds the ICP exports → pushes the updated dataset to
GitHub. Two ways to add FDDs:

- **Add a PDF file** — choose an FDD PDF you already have.
- **Fetch by brand name** — type brand names (KFC, Taco Bell, …) and it downloads
  their FDDs from the **Wisconsin** franchise register (via Apify) and adds them.

Main outputs live in `FDD Parser/Code/output/` — start with `icp_combined.csv`.

## Quick start

1. **Set up the machine once** — follow **[SETUP.md](SETUP.md)** (installs Python,
   Git, Node + the Claude CLI; signs into Claude Max; optional Apify token).
2. **Launch the tool** by double-clicking the launcher for your OS:
   - **Windows:** `Add FDD to Database.bat`
   - **macOS:** `Add FDD to Database.command`
3. **Use it** — see **[HOW TO ADD AN FDD.md](HOW TO ADD AN FDD.md)**.

> The repo ships with the existing dataset (the `output/` CSVs and per-brand
> `cache/` files), so the tool works on a fresh clone **without** the original
> source PDFs.

## Requirements

- **Python 3.11+** (with Tkinter) — the window toolkit.
- **Claude CLI** signed in to a **Claude Max** plan — used to read the FDDs.
  (No `ANTHROPIC_API_KEY` needed; it uses the CLI subscription.)
- **Git** with push access to this repo — the tool auto-syncs the dataset.
- **(Optional) Apify token** in `FDD Parser/.env` — enables "Fetch by brand name".

Python packages: `pip install -r requirements.txt`.

---

## ⚠️ macOS — known gotchas

The tool is cross-platform, but macOS has a few rough edges worth knowing:

1. **Window won't open / "No module named '\_tkinter'"** — Homebrew's Python
   doesn't bundle Tkinter. Install it:
   ```
   brew install python-tk
   ```
   (The python.org installer already includes Tkinter — easiest option.)

2. **Launcher opens in a text editor instead of running** — the `.command`
   file lost its executable bit (can happen after unzip/copy). Fix it once:
   ```
   chmod +x "Add FDD to Database.command" setup.command
   ```
   (It's committed executable, so a normal `git clone` should be fine.)

3. **"…cannot be opened because it is from an unidentified developer"** —
   Gatekeeper. Right-click the `.command` → **Open** → **Open** (one time only).

4. **`python-Levenshtein` fails to install** — it compiles C and needs Apple's
   command-line tools:
   ```
   xcode-select --install
   ```
   Without it the tool still runs (it falls back to pure-Python fuzzy matching),
   just slower on the matching step.

5. **`python` not found** — modern macOS only ships `python3`. The launcher tries
   both; if neither exists, install Python from python.org or `brew install python`.

6. **Claude CLI not found** — install Node (`brew install node`), then
   `npm install -g @anthropic-ai/claude-code`, then run `claude` once to sign in.

7. **First GitHub push asks for sign-in** — macOS stores it in the Keychain after
   the first time. The account needs write access to this repo.

8. **Don't re-save the `.command`/`.sh` files with Windows line endings.** A
   `.gitattributes` keeps them as LF; CRLF would break them with
   `bad interpreter: /bin/bash^M`.

If the window never appears and there's no error, run the tool from a terminal to
see the message: `cd "FDD Parser/Code" && python3 add_fdd.py`.

---

## Project layout

```
Add FDD to Database.bat / .command   ← double-click launchers (Win / mac)
setup.bat / setup.command            ← one-time setup (Win / mac)
requirements.txt
SETUP.md                             ← provisioning guide
HOW TO ADD AN FDD.md                 ← end-user guide
FDD Parser/
  .env                               ← secrets (gitignored): Claude / Apify
  Data/QSR/                          ← FDD PDFs (gitignored; not needed on clone)
  Code/
    add_fdd.py                       ← the desktop tool (GUI)
    fetch_fdds.py                    ← fetch FDDs by name via Apify (Wisconsin)
    1_ai_extract.py                  ← extract franchisee lists from a PDF
    2_ai_match.py                    ← cross-brand identity matching
    3_filter_export.py               ← ICP tiering + export
    4_classify_entity.py             ← person vs company
    cache/  output/                  ← committed dataset + caches
```
