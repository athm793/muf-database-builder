#!/bin/bash
# ============================================================
#  Add FDD to Database  --  double-click this file on macOS.
#  Opens the point-and-click tool for adding an FDD PDF.
# ============================================================
cd "$(dirname "$0")/FDD Parser/Code" || { echo "Could not find the project folder."; exit 1; }

if command -v python3 >/dev/null 2>&1; then
    exec python3 add_fdd.py
elif command -v python >/dev/null 2>&1; then
    exec python add_fdd.py
else
    echo
    echo "  Python 3 was not found on this Mac."
    echo "  Please run the one-time setup first  --  see SETUP.md"
    echo
    read -n 1 -s -r -p "Press any key to close."
    exit 1
fi
