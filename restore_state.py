#!/usr/bin/env python3
"""
Restore auto-book records after a halt ("🔴 state.json unreadable — auto-book
halted"). Run on the Mac, in court-checker-live, after `git pull --rebase`:

    python3 restore_state.py

It walks the git history of state.json, newest first, and picks the newest
version that is valid JSON, NOT halted, and passes the same shape check the
monitor uses (monitor.shape_problems). From that version it merges
autobook, autobook_dry_run and autobook_retry into the current state.json
(where both have the same date|time, the current value wins), removes
"autobook_halted", writes the file atomically, and prints what it restored
(counts only — never refs or record contents). It never commits or pushes:
check My bookings, then commit and push yourself.

Exit status: 0 restored (or nothing to do), 1 nothing usable found / the
current state.json can't be read (state.json is left untouched).
"""

import json
import subprocess
import sys
from pathlib import Path

import monitor

STORES = ("autobook", "autobook_dry_run", "autobook_retry")
MAX_VERSIONS = 500


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def history(repo):
    """Commits that touched state.json, newest first: [(hash, date)]."""
    r = git(repo, "log", f"-{MAX_VERSIONS}", "--format=%H %cI", "--", "state.json")
    if r.returncode != 0:
        raise SystemExit(f"ERROR: git log failed — run this inside court-checker-live ({r.stderr.strip()[:200]})")
    return [tuple(line.split(" ", 1)) for line in r.stdout.splitlines() if line.strip()]


def usable(text):
    """The parsed state if it's valid, not halted and the right shape, else None."""
    try:
        state = json.loads(text)
    except ValueError:
        return None
    if monitor.shape_problems(state) or monitor.is_halted(state):
        return None
    return state


def find_last_good(repo):
    skipped = 0
    for commit, when in history(repo):
        r = git(repo, "show", f"{commit}:state.json")
        state = usable(r.stdout) if r.returncode == 0 else None
        if state is not None:
            return commit, when, state, skipped
        skipped += 1
    return None, None, None, skipped


def main(repo=None):
    repo = Path(repo or Path.cwd())
    path = repo / "state.json"
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"ERROR: the current state.json can't be read ({type(e).__name__}). Did you git pull? Nothing changed.")
        return 1
    if not isinstance(current, dict):
        print("ERROR: the current state.json isn't a JSON object. Nothing changed.")
        return 1
    if not monitor.is_halted(current):
        print('state.json has no "autobook_halted" entry: auto-book isn\'t halted. Nothing to do.')
        return 0

    commit, when, good, skipped = find_last_good(repo)
    if good is None:
        print(f"ERROR: no usable state.json in the last {MAX_VERSIONS} versions ({skipped} checked). Nothing changed.")
        return 1

    merged = dict(current)
    counts = {}
    for k in STORES:
        good_part = good.get(k, {})
        cur_part = current.get(k, {}) if isinstance(current.get(k), dict) else {}
        merged[k] = {**good_part, **cur_part}  # the current value wins on the same date|time
        counts[k] = (len(good_part), len(merged[k]))
    merged.pop("autobook_halted", None)
    problems = monitor.shape_problems(merged)
    if problems:
        print(f"ERROR: the merged state would have the wrong shape ({'; '.join(problems)}). Nothing changed.")
        return 1
    monitor.save_state(merged, path)

    print(f"Restored from {commit[:10]} ({when}); skipped {skipped} newer version(s) "
          "(halted, invalid or the wrong shape).")
    for k, (from_good, total) in counts.items():
        print(f"  {k}: {from_good} record(s) in that version -> {total} now")
    print('Removed "autobook_halted". Nothing was committed or pushed.')
    print("Next: check My bookings, then  git commit -am \"Restore state after halt\" && git push")
    return 0


if __name__ == "__main__":
    sys.exit(main())
