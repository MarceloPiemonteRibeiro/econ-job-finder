#!/usr/bin/env python3
"""publish.py — copy dashboard.html into the GitHub Pages repo and push.

The daily EconJobWatcher task runs `watcher.py` then this script. Site lives at
https://marcelopiemonteribeiro.github.io/econ-job-finder/ and refreshes every morning.

Reads the token from credentials.local ([github] section) so no browser auth is needed.
"""
import shutil, subprocess, sys, urllib.request
from pathlib import Path
from datetime import datetime

BASE = Path(__file__).resolve().parent
PAGES_REPO = Path.home() / "econ-job-finder"
SRC = BASE / "dashboard.html"


def run(*args):
    r = subprocess.run(args, cwd=PAGES_REPO, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode:
        print(f"$ {' '.join(args)}\n{r.stderr}", file=sys.stderr)
        sys.exit(1)
    return r.stdout


if not PAGES_REPO.exists():
    sys.exit(f"publish.py: repo not found at {PAGES_REPO}. Clone it first.")
if not SRC.exists():
    sys.exit(f"publish.py: {SRC} missing — run watcher.py first.")

shutil.copy2(SRC, PAGES_REPO / "index.html")
if not run("git", "status", "--porcelain").strip():
    print("publish.py: no change since the last push."); sys.exit(0)
run("git", "add", "index.html")
run("git", "commit", "-m", f"update dashboard {datetime.now():%Y-%m-%d %H:%M}")
run("git", "push")
print("publish.py: dashboard pushed to https://marcelopiemonteribeiro.github.io/econ-job-finder/")
