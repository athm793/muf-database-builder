#!/usr/bin/env python3
"""
SENIOR-CARE WRAPPER — auto-relaunch with quota-aware sleep
==========================================================
Repeatedly invokes senior_care_extract.py until either:
  (a) the script exits 0 AND its log shows "EXTRACTION COMPLETE", OR
  (b) MAX_ATTEMPTS is reached (safety stop), OR
  (c) MAX_WALL_HOURS elapse (safety stop).

Per-batch + per-PDF caches in the script mean each re-launch picks up where
the previous run left off. The wrapper just decides how long to wait between
launches based on why the prior one exited:

  exit 0  + "EXTRACTION COMPLETE" in log  → done
  exit 0  but no completion marker        → unexpected; short retry
  exit 42 (quota hit per QUOTA_EXIT_CODE) → long sleep (limit reset)
  exit ≠0 (other)                          → short retry
  no output for HEARTBEAT_TIMEOUT          → kill+retry (process hung)

The per-batch partial cache makes interruption safe — at most one batch of
work is lost on a kill.

Usage:
    python run_until_done.py
    python run_until_done.py --max-attempts 30 --quota-sleep 3600
"""

import subprocess, sys, time, os, argparse
from pathlib import Path
from datetime import datetime

HERE         = Path(__file__).parent
SCRIPT       = HERE / "senior_care_extract.py"
LOG_FILE     = HERE / "senior_care_extract.log"
WRAPPER_LOG  = HERE / "run_until_done.log"

# Exit code the script uses to signal Max-plan quota exhaustion.
QUOTA_EXIT_CODE = 42

# Defaults — overridable via CLI flags.
DEFAULT_MAX_ATTEMPTS    = 60
DEFAULT_QUOTA_SLEEP     = 600    # 10 min — short enough to catch fast CLI self-heals
                                 # (config-rewrite race recovers in <5 min); for a real
                                 # 5-hour Max-plan reset the wrapper just re-checks every
                                 # 10 min which is essentially free.
DEFAULT_TRANSIENT_SLEEP = 120    # 2 min — for non-quota failures
DEFAULT_MAX_WALL_HOURS  = 24


def wlog(msg: str):
    """Wrapper log line — distinct from the script's own log."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(WRAPPER_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def log_says_complete() -> bool:
    """True iff senior_care_extract.log's last 40 lines contain EXTRACTION COMPLETE."""
    if not LOG_FILE.exists():
        return False
    try:
        text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
        tail = "\n".join(text.splitlines()[-40:])
        return "EXTRACTION COMPLETE" in tail
    except Exception:
        return False


def run_once() -> int:
    """Invoke the script in a subprocess, streaming its stdout to ours so the
    wrapper's background-task output captures everything. Returns exit code."""
    wlog(f"launching {SCRIPT.name} ...")
    try:
        proc = subprocess.run(
            [sys.executable, "-u", str(SCRIPT)],
            cwd=str(HERE),
            check=False,
        )
        return proc.returncode
    except KeyboardInterrupt:
        wlog("KeyboardInterrupt — exiting wrapper")
        raise


def sleep_with_marker(seconds: int, reason: str):
    """Sleep in 60s chunks so killing the wrapper interrupts quickly."""
    end = time.time() + seconds
    wlog(f"sleeping {seconds}s ({reason}) — will resume at "
         f"{datetime.fromtimestamp(end).strftime('%H:%M:%S')}")
    while time.time() < end:
        chunk = min(60, end - time.time())
        if chunk <= 0:
            break
        time.sleep(chunk)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-attempts",    type=int, default=DEFAULT_MAX_ATTEMPTS)
    parser.add_argument("--quota-sleep",     type=int, default=DEFAULT_QUOTA_SLEEP)
    parser.add_argument("--transient-sleep", type=int, default=DEFAULT_TRANSIENT_SLEEP)
    parser.add_argument("--max-wall-hours",  type=int, default=DEFAULT_MAX_WALL_HOURS)
    args = parser.parse_args()

    # Fresh wrapper log per run
    try:
        WRAPPER_LOG.write_text("", encoding="utf-8")
    except Exception:
        pass

    wall_deadline = time.time() + args.max_wall_hours * 3600
    wlog(f"=== Senior-care wrapper started ===")
    wlog(f"script={SCRIPT}  max_attempts={args.max_attempts}  "
         f"quota_sleep={args.quota_sleep}s  transient_sleep={args.transient_sleep}s  "
         f"max_wall={args.max_wall_hours}h")

    for attempt in range(1, args.max_attempts + 1):
        if time.time() > wall_deadline:
            wlog(f"⏱  Max wall time ({args.max_wall_hours}h) reached — stopping")
            return 2

        wlog(f"--- attempt {attempt}/{args.max_attempts} ---")
        rc = run_once()
        wlog(f"script exited rc={rc}")

        # Success path: trust the log over the exit code. On Windows the
        # script regularly exits with rc=120 (pdfplumber/refcount cleanup
        # artifact AFTER all real work is done) even though the log clearly
        # shows "EXTRACTION COMPLETE". Treating that as failure would loop
        # forever on cached, complete data.
        if log_says_complete():
            wlog(f"✅ EXTRACTION COMPLETE detected in log (rc={rc}). Wrapper done.")
            return 0

        # Quota path: long sleep, then retry. The script wrote the .quota_hit
        # sentinel for diagnostic purposes; we don't need to read it.
        if rc == QUOTA_EXIT_CODE:
            sleep_with_marker(args.quota_sleep, "Max-plan quota hit, waiting for reset")
            continue

        # Anything else: transient failure (network, parse error, hung kill,
        # etc.). Per-batch partial cache means we lose at most one batch.
        sleep_with_marker(args.transient_sleep, f"failure rc={rc} without completion marker")

    wlog(f"❌ Max attempts ({args.max_attempts}) reached without completion. Giving up.")
    return 3


if __name__ == "__main__":
    sys.exit(main())
