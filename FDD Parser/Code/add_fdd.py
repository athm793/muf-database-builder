#!/usr/bin/env python3
"""
ADD FDD TO DATABASE — friendly desktop tool
===========================================
A point-and-click window for non-technical users. Two ways to add FDDs:

  • "Add a PDF file"      — pick an FDD PDF you already have.
  • "Fetch by brand name" — type brand names (KFC, Taco Bell, …) and the tool
                            downloads their FDDs from Wisconsin DFI (via Apify),
                            then adds them.

Either way it then: extracts the franchisee list, re-matches operators across
all brands, rebuilds the ICP exports, and pushes the updated dataset to GitHub.

Launch it by double-clicking the launcher for your OS:
  • Windows:  "Add FDD to Database.bat"   (runs pythonw add_fdd.py)
  • macOS:    "Add FDD to Database.command" (runs python3 add_fdd.py)
Or directly:  python add_fdd.py
"""

import os
import sys
import csv
import queue
import shutil
import threading
import subprocess
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

# ── PATHS ────────────────────────────────────────────────────────────────────
HERE       = Path(__file__).resolve().parent              # .../FDD Parser/Code
REPO_ROOT  = HERE.parent.parent                           # .../<repo root>
DATA_DIR   = HERE.parent / "Data" / "QSR"                 # where PDFs are dropped
ENV_FILE   = HERE.parent / ".env"                         # Apify creds live here
OUTPUT_DIR = HERE / "output"
MASTER_CSV = OUTPUT_DIR / "master_franchisees.csv"
ICP_CSV    = OUTPUT_DIR / "icp_combined.csv"

PYTHON = sys.executable
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Paths the GitHub sync stages (relative to repo root) — regenerated data only.
GIT_PATHS = ["FDD Parser/Code/output", "FDD Parser/Code/cache"]


# ── SMALL HELPERS ────────────────────────────────────────────────────────────

def count_data_rows(csv_path: Path) -> int:
    if not csv_path.exists():
        return 0
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            return max(0, sum(1 for _ in f) - 1)
    except Exception:
        return 0


def apify_configured() -> bool:
    """True if an APIFY_TOKEN with a value is present in .env or the environment."""
    if os.environ.get("APIFY_TOKEN"):
        return True
    if not ENV_FILE.exists():
        return False
    try:
        for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s.startswith("APIFY_TOKEN=") and len(s) > len("APIFY_TOKEN="):
                return True
    except Exception:
        pass
    return False


def parse_names(raw: str) -> list[str]:
    """Split a textbox of brand names on newlines/commas; de-dupe, keep order."""
    parts = []
    for line in raw.splitlines():
        for piece in line.split(","):
            piece = piece.strip()
            if piece:
                parts.append(piece)
    seen, out = set(), []
    for p in parts:
        if p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


# ── THE APP ──────────────────────────────────────────────────────────────────

class AddFddApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.pdf_path: Path | None = None
        self.msg_q: "queue.Queue[tuple]" = queue.Queue()
        self.worker: threading.Thread | None = None

        root.title("MUF Database Builder — Add an FDD")
        root.geometry("800x600")
        root.minsize(700, 520)

        self._build_ui()
        self.root.after(100, self._drain_queue)

    # ---- UI construction ----
    def _build_ui(self):
        ttk.Label(self.root,
                  text="Add Franchise Disclosure Documents (FDDs) to the database",
                  font=("Segoe UI", 13, "bold")).pack(anchor="w", padx=12, pady=(12, 2))

        nb = ttk.Notebook(self.root)
        nb.pack(fill="x", padx=12, pady=6)
        self._build_tab_file(nb)
        self._build_tab_fetch(nb)

        # Shared controls
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=12, pady=(2, 0))
        self.open_btn = ttk.Button(bar, text="Open output folder", command=self.open_output)
        self.open_btn.pack(side="left")

        self.progress = ttk.Progressbar(self.root, mode="indeterminate")
        self.progress.pack(fill="x", padx=12, pady=(6, 4))

        self.status = ttk.Label(self.root, text="Ready.", foreground="#333")
        self.status.pack(anchor="w", padx=12)

        self.log = scrolledtext.ScrolledText(self.root, height=18, wrap="word",
                                             font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, padx=12, pady=8)
        self.log.configure(state="disabled")

    def _build_tab_file(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="  Add a PDF file  ")

        ttk.Label(tab, text="Choose an FDD PDF you already have, then click "
                            "“Add to Database”.", foreground="#555").pack(anchor="w", pady=(8, 4))

        row = ttk.Frame(tab); row.pack(fill="x", pady=4)
        self.choose_btn = ttk.Button(row, text="Choose FDD PDF…", command=self.choose_file)
        self.choose_btn.pack(side="left")
        self.file_label = ttk.Label(row, text="No file selected", foreground="#777")
        self.file_label.pack(side="left", padx=10)

        self.adv_open = tk.BooleanVar(value=False)
        ttk.Checkbutton(tab, text="Advanced options (optional — usually leave blank)",
                        variable=self.adv_open, command=self._toggle_advanced).pack(anchor="w")
        self.adv_frame = ttk.Frame(tab)
        ttk.Label(self.adv_frame, text="Brand name:").grid(row=0, column=0, sticky="w")
        self.brand_var = tk.StringVar()
        ttk.Entry(self.adv_frame, textvariable=self.brand_var, width=26).grid(
            row=0, column=1, sticky="w", padx=6, pady=3)
        ttk.Label(self.adv_frame, text="(blank = auto-detect)", foreground="#888").grid(
            row=0, column=2, sticky="w")
        ttk.Label(self.adv_frame, text="Franchisee-list pages:").grid(row=1, column=0, sticky="w")
        self.pages_var = tk.StringVar()
        ttk.Entry(self.adv_frame, textvariable=self.pages_var, width=26).grid(
            row=1, column=1, sticky="w", padx=6, pady=3)
        ttk.Label(self.adv_frame, text="e.g. 230-309 (blank = auto-detect)",
                  foreground="#888").grid(row=1, column=2, sticky="w")

        self.run_btn = ttk.Button(tab, text="Add to Database",
                                  command=self.start_single, state="disabled")
        self.run_btn.pack(anchor="w", pady=8)

    def _build_tab_fetch(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="  Fetch by brand name  ")

        ttk.Label(tab, text="Type franchise brand names (one per line, or comma-separated). "
                            "The tool downloads each FDD from Wisconsin DFI and adds it.",
                  foreground="#555", wraplength=720, justify="left").pack(anchor="w", pady=(8, 4))

        self.names_text = scrolledtext.ScrolledText(tab, height=6, wrap="word",
                                                    font=("Consolas", 10))
        self.names_text.pack(fill="x", pady=4)
        self.names_text.insert("1.0", "KFC\nTaco Bell\nWingstop")

        self.fetch_btn = ttk.Button(tab, text="Fetch & Add", command=self.start_fetch)
        self.fetch_btn.pack(anchor="w", pady=8)

        if not apify_configured():
            self.fetch_btn.config(state="disabled")
            ttk.Label(tab, foreground="#b00",
                      text="⚠  Apify is not configured. Add APIFY_TOKEN to FDD Parser/.env "
                           "to enable fetching (see SETUP.md).",
                      wraplength=720, justify="left").pack(anchor="w")

    def _toggle_advanced(self):
        if self.adv_open.get():
            self.adv_frame.pack(fill="x", padx=20, pady=4)
        else:
            self.adv_frame.pack_forget()

    # ---- actions ----
    def choose_file(self):
        path = filedialog.askopenfilename(
            title="Choose an FDD PDF",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")])
        if not path:
            return
        self.pdf_path = Path(path)
        self.file_label.config(text=self.pdf_path.name, foreground="#0a0")
        self.run_btn.config(state="normal")

    def open_output(self):
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(str(OUTPUT_DIR))  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.run(["open", str(OUTPUT_DIR)])
            else:
                subprocess.run(["xdg-open", str(OUTPUT_DIR)])
        except Exception as e:
            messagebox.showinfo("Output folder", f"Output is at:\n{OUTPUT_DIR}\n\n({e})")

    def start_single(self):
        if not self.pdf_path or not self.pdf_path.exists():
            messagebox.showwarning("No file", "Please choose an FDD PDF first.")
            return
        if self._busy():
            return
        self._lock()
        brand = self.brand_var.get().strip() or None
        pages = self.pages_var.get().strip() or None
        self._start(self._run_single, self.pdf_path, brand, pages)

    def start_fetch(self):
        names = parse_names(self.names_text.get("1.0", "end"))
        if not names:
            messagebox.showwarning("No names", "Please enter at least one brand name.")
            return
        if self._busy():
            return
        self._lock()
        self._start(self._run_fetch_batch, names)

    # ---- worker lifecycle ----
    def _busy(self):
        return self.worker is not None and self.worker.is_alive()

    def _lock(self):
        self.choose_btn.config(state="disabled")
        self.run_btn.config(state="disabled")
        self.fetch_btn.config(state="disabled")
        self.progress.start(12)
        self._set_status("Working… you can leave this window open.")

    def _start(self, target, *args):
        self.worker = threading.Thread(target=target, args=args, daemon=True)
        self.worker.start()

    # ---- single-PDF flow ----
    def _run_single(self, pdf_path: Path, brand, pages):
        try:
            self._q("log", f"================ Adding: {pdf_path.name} ================\n")
            self._q("log", "This can take a few minutes — longer for a brand-new brand,\n"
                           "since every operator is re-matched against the whole database.\n\n")
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            dest = DATA_DIR / pdf_path.name
            if pdf_path.resolve() != dest.resolve():
                shutil.copy2(pdf_path, dest)
                self._q("log", f"Copied into data folder: {dest}\n\n")

            self._q("status", "Step 1/5 — Reading the FDD…")
            self._q("log", "----- Step 1/5: Reading the FDD -----\n")
            brand_found, extracted, net_new = self._extract_single(dest, brand, pages)
            if extracted == 0:
                self._q("error",
                         "No franchisees could be read from this PDF.\n\n"
                         "Open “Advanced options” and enter the franchisee-list page "
                         "range from the FDD's table of contents (e.g. 230-309), then "
                         "try again.")
                return

            self._pipeline_tail()
            synced, git_msg = self._git_sync(f"{brand_found} (+{net_new} locations)")
            self._finish_summary(
                headline=f"✅ Added {brand_found}: {net_new} new locations "
                         f"({extracted} read from the PDF).",
                synced=synced, git_msg=git_msg)
        except Exception as e:
            self._q("error", f"Something went wrong:\n\n{e}")

    # ---- fetch-by-name batch flow ----
    def _run_fetch_batch(self, names: list[str]):
        try:
            self._q("log", f"========== Fetching {len(names)} brand(s) from Wisconsin DFI ==========\n")
            self._q("status", "Step 1 — Downloading FDDs from Wisconsin (Apify)…")
            rc, lines = self._stream([PYTHON, "-u", "fetch_fdds.py", *names],
                                     capture_prefix="FETCH_RESULT|")
            results = []
            for ln in lines:
                p = ln.split("|")
                if len(p) >= 4:
                    results.append({"brand": p[0], "status": p[1], "file": p[2], "note": p[3]})
            found = [r for r in results if r["status"] == "found" and r["file"]]
            missed = [r for r in results if r["status"] != "found"]

            if not found:
                miss_txt = "\n".join(f"  • {r['brand']} — {r['note']}" for r in missed) or "  (none)"
                self._q("error",
                         "Couldn't fetch any FDDs from Wisconsin.\n\n"
                         "Not found / no FDD:\n" + miss_txt +
                         "\n\nThese brands may not be registered in Wisconsin. You can add "
                         "their FDD manually using the “Add a PDF file” tab.")
                return

            # Extract each fetched PDF (append + dedupe; matching happens once after).
            for i, r in enumerate(found, 1):
                self._q("status", f"Step 2 — Reading FDD {i}/{len(found)}: {r['brand']}…")
                self._q("log", f"\n----- Reading {r['brand']} -----\n")
                self._extract_single(Path(r["file"]), r["brand"], None)

            self._pipeline_tail()

            brands_str = ", ".join(r["brand"] for r in found)
            synced, git_msg = self._git_sync(f"{len(found)} brand(s): {brands_str}")

            lines_out = [f"✅ Fetched & added {len(found)} brand(s): {brands_str}."]
            if missed:
                lines_out.append("⚠️ Not found in Wisconsin (add manually if needed): "
                                 + ", ".join(r["brand"] for r in missed))
            self._finish_summary(headline="\n".join(lines_out), synced=synced, git_msg=git_msg)
        except Exception as e:
            self._q("error", f"Something went wrong:\n\n{e}")

    # ---- shared pipeline steps ----
    def _extract_single(self, dest: Path, brand, pages):
        cmd = [PYTHON, "-u", "1_ai_extract.py", "--single", str(dest)]
        if brand:
            cmd += ["--brand", brand]
        if pages:
            cmd += ["--pages", pages]
        rc, caps = self._stream(cmd, capture_prefix="SINGLE_RESULT|")
        brand_found, extracted, net_new = (brand or ""), 0, 0
        if caps:
            parts = caps[-1].split("|")
            if len(parts) >= 3:
                brand_found = parts[0] or brand_found
                extracted = int(parts[1] or 0)
                net_new = int(parts[2] or 0)
        self._q("log", f"\n{brand_found}: {extracted} locations read ({net_new} new).\n")
        return brand_found, extracted, net_new

    def _pipeline_tail(self):
        self._q("status", "Matching operators across all brands…")
        self._q("log", "\n----- Building the master registry -----\n")
        self._stream([PYTHON, "-u", "2_ai_match.py"])
        self._q("status", "Exporting the ICP lists…")
        self._q("log", "\n----- Exporting ICP tiers -----\n")
        self._stream([PYTHON, "-u", "3_filter_export.py"])
        self._q("status", "Classifying operators…")
        self._q("log", "\n----- Classifying person vs company -----\n")
        self._stream([PYTHON, "-u", "4_classify_entity.py"])

    def _finish_summary(self, headline: str, synced: bool, git_msg: str):
        total_ops = count_data_rows(MASTER_CSV)
        total_icp = count_data_rows(ICP_CSV)
        sync_line = ("✅ Saved to GitHub." if synced
                     else f"⚠️ Saved locally but NOT pushed to GitHub:\n{git_msg}")
        summary = (f"{headline}\n\n"
                   f"Database now holds:\n"
                   f"   • {total_ops:,} unique operators\n"
                   f"   • {total_icp:,} in the ICP list (icp_combined.csv)\n\n"
                   f"{sync_line}")
        self._q("done", {"summary": summary, "synced": synced})

    # ---- subprocess streaming ----
    def _stream(self, cmd: list[str], capture_prefix: str | None = None):
        """Run cmd (cwd=Code), stream stdout to the log. Lines starting with
        capture_prefix are captured (prefix stripped) and returned as a list."""
        captured = []
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(HERE),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                bufsize=1, creationflags=CREATE_NO_WINDOW)
        except Exception as e:
            self._q("log", f"[error] could not start: {' '.join(cmd)}\n{e}\n")
            return 1, captured
        for line in proc.stdout:
            if capture_prefix and line.startswith(capture_prefix):
                captured.append(line[len(capture_prefix):].strip())
            else:
                self._q("log", line)
        proc.wait()
        if proc.returncode not in (0, None):
            self._q("log", f"[exited with code {proc.returncode}]\n")
        return proc.returncode, captured

    # ---- git sync ----
    def _git(self, *args):
        try:
            r = subprocess.run(["git", *args], cwd=str(REPO_ROOT),
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", creationflags=CREATE_NO_WINDOW)
            return r.returncode, (r.stdout or "") + (r.stderr or "")
        except FileNotFoundError:
            return 127, "git is not installed / not on PATH"

    def _git_sync(self, commit_subject: str):
        self._q("status", "Saving to GitHub…")
        self._q("log", "\n----- Saving the updated dataset to GitHub -----\n")
        rc, _ = self._git("rev-parse", "--is-inside-work-tree")
        if rc != 0:
            self._q("log", "Not a git repository — files saved locally only.\n")
            return False, "not a git repository"
        for p in GIT_PATHS:
            self._git("add", "--", p)
        rc, out = self._git("commit", "-m", f"Add FDD: {commit_subject}")
        self._q("log", out + "\n")
        if rc != 0 and "nothing to commit" in out.lower():
            self._q("log", "No data changes to push.\n")
            return True, "no changes"
        if rc != 0:
            return False, "commit failed — is git configured (user.name / user.email)?\n" + out
        rc, out = self._git("pull", "--rebase")
        self._q("log", out + "\n")
        if rc != 0:
            self._git("rebase", "--abort")
            return False, ("couldn't merge the latest from GitHub. Your changes are "
                           "committed locally; ask an admin to push.")
        rc, out = self._git("push")
        self._q("log", out + "\n")
        if rc != 0:
            return False, ("push failed — check internet and GitHub access on this PC.\n" + out)
        return True, "pushed"

    # ---- queue + UI updates ----
    def _q(self, kind, payload):
        self.msg_q.put((kind, payload))

    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.msg_q.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "status":
                    self._set_status(payload)
                elif kind == "done":
                    self._on_done(payload)
                elif kind == "error":
                    self._on_error(payload)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_queue)

    def _append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_status(self, text):
        self.status.config(text=text)

    def _unlock(self):
        self.progress.stop()
        self.choose_btn.config(state="normal")
        self.run_btn.config(state="normal" if self.pdf_path else "disabled")
        if apify_configured():
            self.fetch_btn.config(state="normal")

    def _on_done(self, info):
        self._unlock()
        self._set_status("Done.")
        self._append_log("\n" + info["summary"] + "\n")
        if info["synced"]:
            messagebox.showinfo("Done", info["summary"])
        else:
            messagebox.showwarning("Done (not synced)", info["summary"])

    def _on_error(self, text):
        self._unlock()
        self._set_status("Stopped.")
        self._append_log("\n[stopped] " + text + "\n")
        messagebox.showerror("Couldn't finish", text)


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if sys.platform == "win32" else "clam")
    except Exception:
        pass
    AddFddApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
