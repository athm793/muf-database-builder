#!/bin/bash
# ============================================================
#  One-time setup for the Add-FDD tool on macOS.
#  Double-click this file. It installs the Python packages and
#  checks that the other required tools are present.
# ============================================================
cd "$(dirname "$0")" || exit 1
echo "============================================================"
echo "  MUF Database Builder - one-time setup (macOS)"
echo "============================================================"
echo

# ---- 1. Python 3 ----------------------------------------------
if command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v python >/dev/null 2>&1; then PY=python
else
    echo "[X] Python 3 is NOT installed."
    echo "    Install from https://www.python.org/downloads/ (recommended -"
    echo "    it includes the tkinter window toolkit), or:  brew install python python-tk"
    read -n 1 -s -r -p "Press any key to close."
    exit 1
fi
echo "[OK] Python: $($PY --version 2>&1)"
echo

# ---- 2. tkinter (the window toolkit) --------------------------
if $PY -c "import tkinter" >/dev/null 2>&1; then
    echo "[OK] tkinter present"
else
    echo "[X] tkinter is missing (needed to show the window)."
    echo "    Homebrew users:  brew install python-tk"
    echo "    Or install Python from python.org (includes tkinter)."
fi
echo

# ---- 3. Python packages ---------------------------------------
echo "Installing required Python packages..."
$PY -m pip install --upgrade pip
if $PY -m pip install -r requirements.txt; then
    echo "[OK] Python packages installed."
else
    echo "[X] Package install failed. You may need Apple's command-line tools:"
    echo "    xcode-select --install"
fi
echo

# ---- 4. git ----------------------------------------------------
if command -v git >/dev/null 2>&1; then
    echo "[OK] git found."
else
    echo "[X] git is missing (needed to sync to GitHub). Install with:"
    echo "    xcode-select --install"
fi
echo

# ---- 5. Claude CLI --------------------------------------------
if command -v claude >/dev/null 2>&1; then
    echo "[OK] Claude CLI found. Sign in once with:  claude"
else
    echo "[X] The Claude CLI is NOT installed (the tool needs it to read FDDs)."
    echo "    1. Install Node.js LTS from https://nodejs.org/  (or: brew install node)"
    echo "    2. npm install -g @anthropic-ai/claude-code"
    echo "    3. Run:  claude   and sign in with your Claude Max plan."
fi
echo

echo "============================================================"
echo "  Setup checks complete. Fix anything marked [X] above."
echo "  When everything is [OK], double-click:"
echo "      'Add FDD to Database.command'"
echo "  (If macOS blocks it: right-click the file -> Open -> Open.)"
echo "============================================================"
read -n 1 -s -r -p "Press any key to close."
