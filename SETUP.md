# One-Time Setup (for whoever prepares the employee's machine)

Works on **Windows and macOS**. Do this once per machine. Afterward, the
employee just double-clicks the launcher for their OS:

- **Windows:** `Add FDD to Database.bat`
- **macOS:** `Add FDD to Database.command`

The tool reads FDDs using the **Claude CLI**, which requires a signed-in
**Claude Max** plan on that machine.

---

## 1. Install the prerequisites

| Tool | Windows | macOS |
|------|---------|-------|
| **Python 3.11+** | [python.org](https://www.python.org/downloads/) — tick **"Add python.exe to PATH"** | [python.org](https://www.python.org/downloads/), or `brew install python python-tk` |
| **Git** | [git-scm.com](https://git-scm.com/download/win) | `xcode-select --install`, or `brew install git` |
| **Node.js LTS** | [nodejs.org](https://nodejs.org/) | [nodejs.org](https://nodejs.org/), or `brew install node` |

> **macOS note:** if you install Python via Homebrew, also install the window
> toolkit: `brew install python-tk`. The python.org installer already includes it.

## 2. Install the Claude CLI and sign in

In a terminal (**Windows:** PowerShell · **macOS:** Terminal):

```
npm install -g @anthropic-ai/claude-code
claude            # opens the CLI — sign in with the Claude Max account
```

Sign in once so it's remembered. You can close the CLI afterward.

## 3. Get the project onto the machine

```
git clone https://github.com/Vatsal-Kumar-Singh/muf-database-builder.git
cd muf-database-builder
```

> The clone already contains the existing dataset (all the `output/` CSVs and
> per-brand `cache/` files), so the tool works **without** the original source
> PDFs on disk.

## 4. Run the setup script

- **Windows:** double-click **`setup.bat`**
- **macOS:** double-click **`setup.command`**. If macOS blocks it, right-click
  the file → **Open** → **Open**. You may first need to make the launchers
  runnable — in Terminal, from the project folder:
  ```
  chmod +x "Add FDD to Database.command" setup.command
  ```

It installs the Python packages and checks that Python, Git, and the Claude CLI
are present. Fix anything it marks `[X]`.

## 5. Let the machine push to GitHub

The tool auto-commits and pushes the updated dataset. Set the git identity:

```
git config --global user.name  "Employee Name"
git config --global user.email "employee@company.com"
```

The first push prompts for GitHub sign-in (**Windows:** Git Credential Manager
browser window · **macOS:** keychain/browser). The account needs **write
access** to the `muf-database-builder` repository.

## 6. (Optional) Enable "Fetch by brand name"

The **Fetch by brand name** tab downloads FDDs from the Wisconsin franchise
register using an Apify actor, so the employee can just type brand names instead
of finding PDFs. To enable it, add an Apify API token to the env file.

Create or edit **`FDD Parser/.env`** and add these lines (the file is gitignored,
so the token is never committed):

```
APIFY_TOKEN=apify_api_xxxxxxxxxxxxxxxxxxxx
APIFY_ACTOR_ID=DcUptfu6v2Y8wCbGY        # Wisconsin DFI
APIFY_ACTOR_ID_MN=dnfUyPVabAz3oj2pE     # Minnesota CARDS
APIFY_ACTOR_ID_CA=H82SbZK5RUog0mBaB     # California DFPI
```

- `APIFY_TOKEN` — from your Apify account → **Settings → Integrations → API token**.
- The three actor ids are the `parseforge` state scrapers (Wisconsin, Minnesota,
  California). Only `APIFY_ACTOR_ID` (Wisconsin) is required; MN/CA are optional
  fallbacks.

If the token is missing, the tool still works — the "Fetch by brand name" tab is
simply disabled, and the employee uses "Add a PDF file" instead.

> **Coverage:** fetch-by-name tries **Wisconsin → Minnesota → California** in
> order and stops at the first source that has a downloadable FDD. A brand that
> isn't in any of them (or whose only hit is California, which often exposes
> filing *metadata* but not the FDD PDF) comes back "not found" — add those via
> "Add a PDF file".

> **💳 Apify credits required.** The MN and CA actors are paid Apify actors.
> If your account is out of credits, those sources return *"payment required"*
> and only Wisconsin (or whichever source has credit) will work. Top up at
> Apify → **Billing** to enable all sources.

> **Cost:** each brand name triggers up to three Apify actor runs (one per
> source, stopping at the first hit). Keep an eye on Apify usage for long lists.

## 7. Make it easy to launch

- **Windows:** right-click **`Add FDD to Database.bat`** → **Send to → Desktop
  (create shortcut)**.
- **macOS:** drag **`Add FDD to Database.command`** onto the Dock, or right-click
  it → **Make Alias** and move the alias to the Desktop.

---

## Done

The employee now just double-clicks the launcher, picks an FDD PDF (or types
brand names), and the tool does the rest. See **`HOW TO ADD AN FDD.md`** for
their instructions.

## Troubleshooting

- **"Python was not found"** — Python isn't on PATH. Windows: re-run the
  installer and tick *Add python.exe to PATH*. macOS: install from python.org or
  `brew install python`.
- **macOS: window doesn't open / "No module named tkinter"** — install the
  toolkit: `brew install python-tk`, or use the python.org installer.
- **macOS: "cannot be opened because it is from an unidentified developer"** —
  right-click the `.command` file → **Open** → **Open** (only needed once).
- **macOS: ".command" opens in a text editor instead of running** — it isn't
  executable yet. In Terminal: `chmod +x "Add FDD to Database.command"`.
- **Tool reads 0 franchisees** — auto page-detection missed (rare). In the tool,
  open **Advanced options** and type the franchisee-list page range from the
  FDD's table of contents (e.g. `230-309`).
- **"Saved locally but NOT pushed to GitHub"** — git isn't signed in or the
  account lacks write access. The data is safe locally; fix git access and the
  next run will push.
- **It runs for a long time** — adding a brand-new brand re-matches every
  operator against the whole database. This is normal; leave it running.
