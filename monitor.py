#!/usr/bin/env python3
"""
Crystal Sports Tennis Court Monitor — cloud edition, rebuilt for the new
booking site (live since 2026-11-11; the old api_helper.php API is gone).

Auto-book (stage 3) is controlled by AUTOBOOK_MODE (the vars.AUTOBOOK_MODE
repository variable, passed in by monitor.yml): "off" (default when unset —
notify only, starred slots get the normal email plus a note to book
manually, and the monitor never sends a POST), "dry_run" or "live". See
autobook.py for the flow and its rules; the only POST anywhere is
bot_session.post_booking_confirm(), and only in "live".

Runs N check passes (LOOP_ITERATIONS, default 4), LOOP_INTERVAL_SECONDS
apart (default 60), then exits. Designed for GitHub Actions cron */5.

  Reads config:   config.json — unchanged schema: targets[{name, date,
                  locations, times[{time, autoBook}]}], optional email_to.
  Secrets (env):  BOT_JAR_KEY (decrypts bot_session.enc, the bot's own login
                  session — see bot_session.py), EMAIL_USER,
                  EMAIL_APP_PASSWORD.
  Reads/writes:   state.json, bot_session.enc (rewritten after every
                  response that changes a cookie).
  Availability:   GET /booking?date=YYYY-MM-DD&loc=LOC00x&time=0&sel= per
                  (date, location), strictly sequential, through
                  bot_session.fetch(). Grid first: if slot links are
                  parsed they decide (the court map's is-locked never
                  matters); a slot is open iff it lacks "is-disabled" and
                  its free count isn't 0. No slots + a booking opens-at
                  phrase ("Booking opens 21:00") = "not open yet" (counts
                  as a read; known_open for it is kept as-is); no slots and
                  no phrase = unexpected page: logged, skipped, no emails,
                  not a session failure.
  Unreadable:     last_check (the dashboard's "active") only moves when a
                  pass read at least one page. After 30 min of passes that
                  read nothing, ONE "Monitor can't read the booking site"
                  email, cleared by the next good read.
  State:          known_open["date|loc|any|HH:MM"] dedupes "slot open"
                  emails exactly as before (the grid has no per-court
                  detail, so the court part of the key is "any").
                  session_alert dedupes the session email: sent once per
                  dead jar, cleared by the next successful check.
  Exit code:      0  ok
                  1  setup error (missing env var, no/unreadable jar), or
                     the jar couldn't be saved but no cookie had changed
                     (transient: the copy on disk is current; next run retries),
                     or state.json couldn't be written on an otherwise normal run.
                     A failing state.json write never changes codes 0/3/4 below
                     on the session paths, and never stops their emails.
                  3  bot session expired (login redirect) — the run that
                     detects it. Later runs on the same dead jar send
                     nothing and exit 0 (the dashboard shows "stale").
                  4  a rotated cookie couldn't be saved — session likely lost

  python monitor.py --alert-session-lost "REASON"
                  sends the "session likely lost" email (used by
                  monitor.yml when bot_session.enc couldn't be pushed) and
                  exits 4.
"""

import argparse
import contextlib
import json
import os
import smtplib
import sys
import tempfile
import time
import traceback
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import requests

import bot_session
from bot_session import BASE, BOOKING_PATH, TZ
import autobook
from booking_page import is_login_page, parse_page, slot_time

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"
STATE_FILE = ROOT / "state.json"

AUTOBOOK_PAUSED_NOTE = "Auto-book is off (AUTOBOOK_MODE) — book manually."
AUTOBOOK_NOTES = {
    "off": AUTOBOOK_PAUSED_NOTE,
    "dry_run": "Auto-book is in DRY RUN: it only reports what it would book — book manually.",
    "live": "Auto-book is on for this time but has no result yet — book manually to be sure.",
}

BOOKING_URL = BASE + BOOKING_PATH
ANY_COURT = "any"  # the court part of a known_open key — the grid is per time, not per court

LOCATIONS = {
    "LOC001": "Crystal Sports",
    "LOC002": "Crystal Sports G",
}

EXIT_OK, EXIT_SETUP, EXIT_SESSION, EXIT_JAR = 0, 1, 3, 4
EXIT_ERROR = 1  # a failed run that needs no reseed
UNREADABLE_ALERT_AFTER_MINUTES = 30
# A streak of unreadable passes is only "consecutive" if they keep coming:
# a longer gap since the last one (runs that exited early, a paused cron,
# a dead session) starts a new streak instead of resuming a stale one.
UNREADABLE_STREAK_GAP_MINUTES = 15
UNREADABLE_STREAK_KEYS = ("unreadable_since", "unreadable_last", "unreadable_passes")

RESEED_STEPS = """To reseed (on the Mac, in court-checker-live):
  1. Open an Incognito window, log in to {base}, copy cs.did then
     cs.member (last). Close the window WITHOUT logging out.
  2. rm -f .bot_session.json.handed-off-*  &&  git pull --rebase  &&  git rm -q bot_session.enc
  3. export CS_DID="$(pbpaste)"; export CS_MEMBER="$(pbpaste)"   (copy each first)
     python3 probe_new_site.py; unset CS_DID CS_MEMBER
  4. python3 seal_session.py            (new key goes to the clipboard)
  5. Update the BOT_JAR_KEY secret:  pbpaste | gh secret set BOT_JAR_KEY; pbcopy </dev/null
     (or GitHub → Settings → Secrets and variables → Actions → BOT_JAR_KEY)
  6. git add bot_session.enc && git commit -m "Reseed bot session" && git push
The next scheduled run picks it up.""".format(base=BASE)


def log(msg):
    bot_session.say(f"[{datetime.now(TZ):%Y-%m-%d %H:%M:%S}] {msg}")


def where(e):
    """An exception for the (public) log: its type and the innermost
    file:line — never its message, which may carry page text or a URL."""
    tb = traceback.extract_tb(e.__traceback__)
    return f"{type(e).__name__} at {Path(tb[-1].filename).name}:{tb[-1].lineno}" if tb else type(e).__name__


def normalize_times(times):
    """Accept either a legacy plain time string or a {time, autoBook} object
    per entry, and always return the latter. Lets old and new config.json
    shapes coexist indefinitely — targets aren't rewritten until edited
    through the dashboard, and plain strings just mean autoBook=False."""
    normalized = []
    for t in times:
        if isinstance(t, str):
            normalized.append({"time": t, "autoBook": False})
        else:
            normalized.append({"time": t["time"], "autoBook": bool(t.get("autoBook", False))})
    return normalized


def load_config():
    if not CONFIG_FILE.exists():
        log(f"ERROR: {CONFIG_FILE.name} not found")
        sys.exit(EXIT_SETUP)
    config = json.loads(CONFIG_FILE.read_text())
    for target in config.get("targets", []):
        target["times"] = normalize_times(target["times"])
    return config


class StateUnreadable(Exception):
    """state.json is missing, unreadable, or not the shape we write."""


# The shape monitor.py writes for every key that decides a booking or an
# email: key -> what its value must be. Keys not listed are left alone; a
# listed key of the wrong shape means the file can't be trusted.
def _is_dict_of(check):
    return lambda v: isinstance(v, dict) and all(isinstance(k, str) and check(x) for k, x in v.items())


def _is_record(r):
    return (isinstance(r, dict) and isinstance(r.get("status", ""), str)
            and isinstance(r.get("attempts", []), list) and all(isinstance(a, dict) for a in r.get("attempts", [])))


STATE_SHAPE = {
    "known_open": _is_dict_of(lambda x: x is True),
    "autobook": _is_dict_of(_is_record),
    "autobook_dry_run": _is_dict_of(_is_record),
    "autobook_retry": _is_dict_of(lambda x: isinstance(x, dict)),
    "autobook_attempts": lambda v: isinstance(v, dict),  # old site's, only ever pruned
    "session_alert": lambda v: isinstance(v, dict),
    "read_alert": lambda v: isinstance(v, dict),
}
# Not checked: last_check, currently_open_count, autobook_mode and the
# unreadable-streak keys are rewritten every run, never decide a booking, and
# are already read defensively (a garbage timestamp just restarts the streak).
# autobook_halted is not checked either: its PRESENCE is the halt, whatever
# its value (see is_halted), so an odd value never makes the file "wrong
# shape" and replaced — that could only ever lose a halt.


def shape_problems(state):
    """[] if `state` is the shape monitor.py writes, else what's wrong.
    Also used by restore_state.py to pick a good version from git history."""
    if not isinstance(state, dict):
        return [f"top level is {type(state).__name__}, not an object"]
    return [f"unexpected shape: {k}" for k, ok in sorted(STATE_SHAPE.items()) if k in state and not ok(state[k])]


def is_halted(state):
    """Halted = the "autobook_halted" KEY is present, whatever its value
    ({} and null included). The only way to clear it is to delete the key
    (restore_state.py does that)."""
    return "autobook_halted" in state


def load_state():
    """state.json as a dict. Raises StateUnreadable if it is missing (it's
    tracked in git, so missing means something went wrong), unreadable, or
    the wrong shape: the caller must fail closed — it holds the auto-book
    records that stop a slot being booked twice."""
    if not STATE_FILE.exists():
        raise StateUnreadable("missing")
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        raise StateUnreadable(type(e).__name__) from None
    problems = shape_problems(state)
    if problems:
        raise StateUnreadable("; ".join(problems))
    return state


def open_state():
    """load_state(), or — if that fails — the halted fresh state, already on disk."""
    try:
        return load_state()
    except StateUnreadable as e:
        return recover_unreadable_state(str(e))


def save_state(state, path=None):
    """Atomic: written to a temp file in the same folder, fsynced, then
    renamed over state.json (or `path`). A crash at any point leaves either
    the old file or the new one, never a half-written one."""
    target = Path(path) if path is not None else STATE_FILE
    data = json.dumps(state, indent=2)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):  # make the rename itself durable (not possible everywhere)
        dfd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def try_save_state(state):
    """save_state that never raises: a failing state write must not stop
    an email from going out or change the exit code a caller intends.
    Returns False (and logs) on failure."""
    try:
        save_state(state)
        return True
    except Exception as e:
        log(f"⚠ couldn't write {STATE_FILE.name}: {type(e).__name__}"
            + (f" ({e.strerror})" if getattr(e, "strerror", None) else ""))
        return False


def clear_unreadable_streak(state):
    for k in UNREADABLE_STREAK_KEYS:
        state.pop(k, None)


def send_email(subject, body, smtp_user, smtp_pass, to_addr):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = to_addr
    msg.set_content(body)
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.send_message(msg)


def slot_key(date, loc_id, court, time_name):
    return f"{date}|{loc_id}|{court}|{time_name}"


def booking_link(date, loc_id):
    return f"{BOOKING_URL}?date={date}&loc={loc_id}"


def fetch_availability(jar, date, loc_id):
    """One GET of the booking grid. Returns (kind, open_times, info):
      "ok"            open_times = {"HH:MM": free count or None}
      "not_open_yet"  open_times = {}; info = opening time if shown
      "login"         the session is no longer valid — caller must stop
      "error"         open_times = None; info = reason (network, http, no grid)
    plus links = {"HH:MM": the open slot's own href} for "ok" (empty otherwise).
    Raises bot_session.JarSaveError if a response couldn't be persisted."""
    params = {"date": date, "loc": loc_id, "time": "0", "sel": ""}
    try:
        fetched = bot_session.fetch(jar, BOOKING_PATH, params)
    except requests.RequestException as e:
        return "error", None, f"network: {type(e).__name__}", {}
    resp = fetched.resp
    if fetched.login:
        return "login", None, resp.headers.get("Location", ""), {}
    if resp.status_code != 200:
        return "error", None, f"http {resp.status_code}", {}
    body = resp.text or ""
    page = parse_page(body)
    if not page["slots"]:
        if is_login_page(body, resp.url):
            # A login page served as a 200 instead of the usual redirect.
            return "login", None, "login page (200)", {}
        if page["not_open_yet"]:
            return "not_open_yet", {}, page["opens_at"], {}
        return "error", None, (f"unexpected page — no slot grid and no opens-at marker "
                               f"({len(body)} bytes); skipped"), {}
    times = [slot_time(s["time"]) for s in page["slots"]]
    unreadable = [s["time"] for s, t in zip(page["slots"], times) if t is None]
    if unreadable:
        return "error", None, (f"unexpected page — {len(unreadable)} slot time(s) not readable "
                               "unambiguously; skipped"), {}
    if len(set(times)) != len(times):
        return "error", None, "unexpected page — the same slot time appears twice; skipped", {}
    open_times, links = {}, {}
    for s, t in zip(page["slots"], times):
        if s["available"] and s["free"] != 0:
            open_times[t] = s["free"]
            if s["href"].startswith(BOOKING_PATH + "?"):  # the slot's own /booking?... link, nothing else
                links[t] = s["href"]
    return "ok", open_times, None, links


def session_alert_email(kind, detail):
    if kind == "lost":
        subject = "🔴 Bot session likely lost — reseed needed"
        lead = ("A cookie rotated but the bot couldn't save it"
                + (f" ({detail})" if detail else "")
                + ", so its session is likely lost: the copy it holds is superseded, and the\n"
                  "site rejects superseded cookies.")
    else:
        subject = "🔴 Bot session expired — reseed needed"
        lead = "The booking site sent the bot to its login page, so its session is no longer valid."
    body = (f"{lead}\n\nCourt availability checks are stopped until you reseed — no more emails\n"
            f"about this until then.\n\n{RESEED_STEPS}\n")
    return subject, body


def alert_session(state, kind, detail, jar_fp, email):
    """Send the session email once per dead jar: not again while the same
    jar file is on disk. Recorded only if the send succeeds, so a failed
    send is retried next run."""
    prev = state.get("session_alert")
    if prev and prev.get("jar_sha256") == jar_fp:
        log(f"  session email already sent at {prev.get('sent_at')} for this jar — not resending")
        return
    subject, body = session_alert_email(kind, detail)
    try:
        send_email(subject, body, *email)
    except Exception as e:
        log(f"⚠ session email failed: {type(e).__name__}")
        return
    state["session_alert"] = {"kind": kind, "sent_at": datetime.now(TZ).isoformat(), "jar_sha256": jar_fp}
    log("✉ session email sent")


def format_free(free):
    return "free count not shown" if free is None else f"{free} free"


def run_check_pass(state, config, jar, email, mode="off"):
    """One full check. Mutates `state` in place. Returns "ok", or "login" if
    the session is no longer valid (stopped at that request, nothing else
    sent). Raises bot_session.JarSaveError if a response couldn't be saved.
    With mode "dry_run"/"live", starred times that just opened go through
    autobook.process() before the "slot open" email."""
    today_str = datetime.now(TZ).date().isoformat()
    active = [t for t in config.get("targets", []) if t["date"] >= today_str]
    if not active:
        log("  no active (future) targets — nothing to check")
        clear_unreadable_streak(state)  # not an unreadable pass: the streak is broken
        return "ok"

    fetch_keys = sorted({(t["date"], loc) for t in active for loc in t["locations"]})
    cache = {}  # (date, loc) -> {"HH:MM": free}; only for grids that were read
    slot_links = {}  # (date, loc) -> {"HH:MM": the open slot's own href}
    pages_read, problems = 0, []
    for date, loc in fetch_keys:
        log(f"  → {LOCATIONS.get(loc, loc)} / {date}")
        kind, open_times, info, links = fetch_availability(jar, date, loc)
        if kind == "login":
            log(f"      🔴 login redirect ({info}) — bot session expired, stopping")
            clear_unreadable_streak(state)  # the session email takes over from here
            return "login"
        if kind == "error":
            log(f"      ⚠ {info}")
            problems.append(f"{LOCATIONS.get(loc, loc)} / {date}: {info}")
            continue
        pages_read += 1
        state.pop("session_alert", None)  # the session works: a future failure may email again
        if kind == "not_open_yet":
            # Not in `cache`: a date that isn't bookable yet says nothing
            # about what was open, so its known_open entries stay as they are.
            log("      not open yet" + (f" (opens {info})" if info else ""))
            continue
        shown = " ".join(f"{t}({'?' if f is None else f})" for t, f in sorted(open_times.items()))
        log(f"      open: {shown or 'none'}")
        cache[(date, loc)] = open_times
        slot_links[(date, loc)] = links

    if pages_read == 0:
        note_unreadable_pass(state, problems, email)
        return "ok"  # last_check, currently_open_count and known_open untouched
    clear_unreadable_streak(state)
    state.pop("read_alert", None)

    known_open = {k: v for k, v in state.get("known_open", {}).items() if k.split("|", 1)[0] >= today_str}
    new_alerts = []
    currently_open = set()
    for target in active:
        starred = {t["time"] for t in target["times"] if t["autoBook"]}
        wanted = {t["time"] for t in target["times"]}
        for loc in target["locations"]:
            open_times = cache.get((target["date"], loc)) or {}
            for t in sorted(wanted & open_times.keys()):
                k = slot_key(target["date"], loc, ANY_COURT, t)
                currently_open.add(k)
                if k not in known_open:
                    # Added to known_open only once its email has gone out (or
                    # an auto-book result email replaced it): see below.
                    new_alerts.append({
                        "target": target["name"], "date": target["date"], "loc_id": loc,
                        "loc": LOCATIONS.get(loc, loc), "time": t, "free": open_times[t],
                        "starred": t in starred, "href": slot_links[(target["date"], loc)].get(t),
                    })

    # A slot that's no longer open (on a page we did read) can alert again
    # if it reopens. Pages that errored keep their entries untouched.
    for k in list(known_open):
        d, l = k.split("|", 2)[:2]
        if (d, l) in cache and k not in currently_open:
            del known_open[k]

    state["known_open"] = known_open
    # autobook_attempts belongs to the old site's booking code: never written
    # now, only pruned by date so it drains away.
    if "autobook_attempts" in state:
        state["autobook_attempts"] = {k: v for k, v in state["autobook_attempts"].items()
                                      if k.split("|", 1)[0] >= today_str}
    for name in (autobook.LIVE_KEY, autobook.DRY_KEY, autobook.RETRY_KEY):
        if name in state:
            state[name] = {k: v for k, v in state[name].items() if k.split("|", 1)[0] >= today_str}
    state["last_check"] = datetime.now(TZ).isoformat()
    state["currently_open_count"] = len(currently_open)
    log(f"  currently open across watchlist: {len(currently_open)}")

    status, emailed, jar_error = "ok", {}, None
    if mode in ("dry_run", "live"):
        ctx = None
        try:
            new_keys = {slot_key(a["date"], a["loc_id"], ANY_COURT, a["time"]) for a in new_alerts}
            candidates = []
            for target in active:
                for loc in target["locations"]:
                    links = slot_links.get((target["date"], loc), {})
                    for t in sorted(x["time"] for x in target["times"] if x["autoBook"]):
                        if t in (cache.get((target["date"], loc)) or {}) and links.get(t):
                            candidates.append({"date": target["date"], "time": t, "loc": loc,
                                               "loc_name": LOCATIONS.get(loc, loc), "href": links[t],
                                               "newly_open": slot_key(target["date"], loc, ANY_COURT, t) in new_keys})
            ctx = autobook.Context(jar, state, mode, send=lambda subj, body: send_email(subj, body, *email),
                                   save_state=save_state, try_save_state=try_save_state, log=log, locations=LOCATIONS)
            autobook.process(ctx, candidates)
        except autobook.SessionStopped:
            log("      🔴 login redirect during auto-book — bot session expired, stopping")
            status = "login"
        except bot_session.JarSaveError as e:
            jar_error = e  # re-raised after the slot-open email below
        except Exception as e:
            # A bug in auto-book must never cost the slot-open email.
            log(f"      ⚠ auto-book failed ({where(e)}) — the slot-open email still goes out")
        # In live mode a starred opening whose result email went out (for the
        # location actually attempted) isn't repeated in the "slot open" email.
        # The same time at the other location is always alerted normally.
        if mode == "live" and ctx is not None:
            emailed = ctx.emailed_slots

    def replaced(a):
        return a["starred"] and (autobook.key_for(a["date"], a["time"]), a["loc_id"]) in emailed
    for a in new_alerts:
        if replaced(a):
            known_open[slot_key(a["date"], a["loc_id"], ANY_COURT, a["time"])] = True  # its result email went out
    new_alerts = [a for a in new_alerts if not replaced(a)]
    for a in new_alerts:
        a["note"] = autobook_note(a, mode, emailed, state)

    if not new_alerts:
        log("  no new openings this iteration")
    else:
        send_slot_alert(new_alerts, known_open, email, mode)
    if jar_error is not None:
        raise jar_error
    return status


def autobook_note(a, mode, emailed, state):
    """The starred line's note. In live mode, if this date+time was already
    auto-booked (or tried) at the OTHER location — this pass or earlier —
    say so and point at that email instead of "no result yet"."""
    if not a["starred"] or mode != "live":
        return AUTOBOOK_NOTES[mode]
    key = autobook.key_for(a["date"], a["time"])
    other = next(((loc, emoji) for (k, loc), emoji in emailed.items() if k == key and loc != a["loc_id"]), None)
    if other is None:
        rec = state.get(autobook.LIVE_KEY, {}).get(key)
        if rec and rec.get("loc") and rec["loc"] != a["loc_id"]:
            other = (rec["loc"], autobook.RESULT_EMOJI.get(rec.get("status"), "?"))
    if other is None:
        return AUTOBOOK_NOTES[mode]
    return f"auto-book already attempted at {LOCATIONS.get(other[0], other[0])} (result: {other[1]}) — see that email"


def send_slot_alert(new_alerts, known_open, email, mode):
    """The 🎾 email. Only if it was sent do its openings go into known_open
    (so a failed send alerts again on the next pass instead of never)."""
    log(f"🎾 {len(new_alerts)} NEW opening(s) detected")
    lines = [f"🎾 {len(new_alerts)} tennis slot(s) just opened up:\n"]
    by_target = {}
    for a in new_alerts:
        by_target.setdefault(a["target"], []).append(a)
    for tname, items in by_target.items():
        lines.append(f"• {tname}")
        for a in items:
            lines.append(f"    {a['date']} {a['time']} — {a['loc']} ({format_free(a['free'])})"
                         + ("  ★" if a["starred"] else ""))
            if a["starred"]:
                lines.append(f"      {a.get('note') or AUTOBOOK_NOTES[mode]}")
            lines.append(f"      {BASE + a['href'] if a['href'] else booking_link(a['date'], a['loc_id'])}")
        lines.append("")
    lines.append(f"Book here: {BOOKING_URL}")
    try:
        send_email(f"🎾 {len(new_alerts)} tennis slot(s) open", "\n".join(lines), *email)
    except Exception as e:
        log(f"⚠ alert email failed: {type(e).__name__} — these openings will alert again next pass")
        return False
    for a in new_alerts:
        known_open[slot_key(a["date"], a["loc_id"], ANY_COURT, a["time"])] = True
    log("✉ alert email sent")
    return True


def note_unreadable_pass(state, problems, email):
    """A pass that read no page at all. After UNREADABLE_ALERT_AFTER_MINUTES
    of these in a row, send ONE email; the next good read clears it."""
    now = datetime.now(TZ)
    last = _parse_time(state.get("unreadable_last"))
    since = _parse_time(state.get("unreadable_since"))
    if since is None or last is None or (now - last).total_seconds() > UNREADABLE_STREAK_GAP_MINUTES * 60:
        clear_unreadable_streak(state)  # stale or partial: start a new streak now
        since = now
        state["unreadable_since"] = now.isoformat()
    state["unreadable_last"] = now.isoformat()
    state["unreadable_passes"] = state.get("unreadable_passes", 0) + 1
    minutes = int((now - since).total_seconds() // 60)
    log(f"  ⚠ no page readable this pass ({state['unreadable_passes']} pass(es) in a row, {minutes} min)")
    if "read_alert" in state or minutes < UNREADABLE_ALERT_AFTER_MINUTES:
        return
    body = (f"The monitor hasn't been able to read a single booking page for {minutes} minutes\n"
            f"({state['unreadable_passes']} check passes in a row). The bot session itself still looks\n"
            "valid — this is the site or the network, or the page changed shape.\n\n"
            "Last problems:\n" + "\n".join(f"  • {p}" for p in problems[:10]) + "\n\n"
            "No slot emails can go out until it reads a page again. Nothing to do if it\n"
            "recovers; you'll get no further email about this until then.\n")
    try:
        send_email("🔴 Monitor can't read the booking site", body, *email)
    except Exception as e:
        log(f"⚠ unreadable-site email failed: {type(e).__name__}")
        return
    state["read_alert"] = {"sent_at": now.isoformat()}
    log("✉ unreadable-site email sent")


def _parse_time(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def email_settings(config=None):
    user = os.environ.get("EMAIL_USER", "").strip()
    password = os.environ.get("EMAIL_APP_PASSWORD", "").strip()
    missing = [k for k, v in {"EMAIL_USER": user, "EMAIL_APP_PASSWORD": password}.items() if not v]
    if missing:
        log(f"ERROR: missing env vars: {', '.join(missing)}")
        return None
    return user, password, (config or {}).get("email_to") or user


def current_jar_path():
    return bot_session.ENC_FILE if os.environ.get(bot_session.JAR_KEY_ENV, "").strip() else bot_session.JAR_FILE


STATE_UNREADABLE_SUBJECT = "🔴 state.json unreadable — auto-book halted"

# The restore steps, in the halt email and (verbatim, a test checks) in
# README.md. Plain commands only: the same in bash and zsh.
HALT_RESTORE_STEPS = """\
To restore and resume (on the Mac, in court-checker-live):

1. Pause the monitor: pause the cron-job.org job, so it doesn't commit
   state.json while you work.
2. Get the latest state:
    git pull --rebase
3. Restore the auto-book records from the newest good state.json in git
   history (valid, not halted, the right shape). It only edits state.json,
   prints what it restored (counts only), and never commits:
    python3 restore_state.py
4. Check My bookings for anything booked since that version, in Awaiting
   payment and Upcoming. A booking made in that gap isn't in the restored
   records (the bot still reads My bookings before booking, so it blocks the
   same time):
    https://crystalsports-booking.kegroup.co.th/bookings?tab=pending
    https://crystalsports-booking.kegroup.co.th/bookings
5. Commit and push:
    git commit -am "Restore state after halt" && git push
6. Resume the cron-job.org job. Auto-book resumes from the next run.
"""


def recover_unreadable_state(reason):
    """state.json exists but can't be read. It holds the auto-book records
    ("never book this date+time twice"), so auto-book is halted — and stays
    halted until the owner removes "autobook_halted" from state.json — while
    notify-only carries on from a fresh state. The unreadable file is kept
    beside it (on the runner; the workflow commits only state.json)."""
    now = datetime.now(TZ)
    kept = STATE_FILE.with_name(f"{STATE_FILE.name}.unreadable-{now:%Y%m%d-%H%M%S}")
    kept_name = None
    if STATE_FILE.exists():
        try:
            os.replace(STATE_FILE, kept)
            kept_name = kept.name
        except OSError:
            pass
    log(f"🔴 {STATE_FILE.name} unreadable ({reason}) — auto-book HALTED; notify-only continues from a fresh state")
    state = {"autobook_halted": {"reason": "state_unreadable", "detail": reason, "since": now.isoformat(),
                                 "kept_as": kept_name}}
    # On disk NOW, before anything (config, email settings, the jar) can make
    # the run return early. If even this fails there is no state.json, and
    # the next run halts again: fail closed either way.
    try_save_state(state)
    return state


def email_halt_once(state, email):
    """The one 🔴 email for a halt (retried on later runs until it's sent).
    A halt with an odd value (set by hand, {} or null) stays a halt: the
    value is replaced by one we can note the email in, the key stays."""
    halted = state["autobook_halted"]
    if not isinstance(halted, dict):
        halted = state["autobook_halted"] = {"reason": "set_in_state_json", "value_was": type(halted).__name__}
    if halted.get("emailed_at"):
        return
    if halted.get("reason") == "state_unreadable":
        why = (f"{STATE_FILE.name} couldn't be used ({halted.get('detail')}). It holds the auto-book records\n"
               "that stop a date+time being booked twice, so auto-book is HALTED")
        kept = ("The unreadable file was kept as " + halted["kept_as"] + " on the runner (not committed).\n"
                if halted.get("kept_as") else "")
    else:
        why = f'{STATE_FILE.name} has an "autobook_halted" entry, so auto-book is HALTED'
        kept = ""
    body = (f"{why}\nsince {halted.get('since', '(unknown)')}: no booking requests at all, whatever AUTOBOOK_MODE says.\n\n"
            "Slot-open emails carry on (you may get one repeat for slots that are already open).\n"
            f"{kept}\n" + HALT_RESTORE_STEPS)
    try:
        send_email(STATE_UNREADABLE_SUBJECT, body, *email)
    except Exception as e:
        log(f"⚠ state-unreadable email failed: {type(e).__name__}")
        return
    halted["emailed_at"] = datetime.now(TZ).isoformat()
    log("✉ state-unreadable email sent")
    try_save_state(state)  # the dedupe mark, on disk before any early return


def run(iterations=4, interval=60, jar_path=bot_session.JAR_FILE, enc_path=bot_session.ENC_FILE, session=None):
    mode = autobook.mode_from_env(log)
    state = open_state()  # first: a halt is on disk before any early return below
    config = load_config()
    email = email_settings(config)
    if email is None:
        return EXIT_SETUP
    if is_halted(state):
        email_halt_once(state, email)
        if mode != "off":
            log('🔴 auto-book HALTED ("autobook_halted" is in state.json) — running notify-only. '
                "Run restore_state.py to resume (see the email).")
            mode = "off"
    if push_failure_block(state) and mode != "off":
        log(f"🔴 auto-book PAUSED: an earlier run's state.json wasn't saved "
            f"({state['autobook_blocked']['reason']}) — running notify-only until a later push succeeds.")
        mode = "off"
    state["autobook_mode"] = mode  # what the dashboard shows: the mode this run actually uses
    state.pop("last_session_alert", None)  # the PHPSESSID-era throttle, replaced by session_alert

    try:
        jar = bot_session.open_jar(jar_path, enc_path, session=session, allow_seed=False)
    except bot_session.SessionOpenError as e:
        log(f"ERROR: couldn't open the bot session ({e.kind}) — no requests sent.")
        return EXIT_SETUP

    autobook.notify_interrupted(autobook.Context(
        jar, state, mode, send=lambda subj, body: send_email(subj, body, *email),
        save_state=save_state, try_save_state=try_save_state, log=log, locations=LOCATIONS))

    prev = state.get("session_alert")
    if prev and prev.get("jar_sha256") == bot_session.file_fingerprint(jar.path):
        log(f"🔴 bot session marked {prev.get('kind')} at {prev.get('sent_at')} and {jar.path.name} hasn't "
            "changed since — not sending any requests until it's reseeded (already emailed; exit 0).")
        try_save_state(state)
        return EXIT_OK

    log(f"Running {iterations} check pass(es), {interval}s apart — auto-book mode: {mode}")
    state_write_failed = False
    for i in range(iterations):
        log(f"--- iteration {i + 1}/{iterations} ---")
        try:
            status = run_check_pass(state, config, jar, email, mode)
        except bot_session.JarSaveError as e:
            try_save_state(state)
            if not e.changes:
                log(f"⚠ couldn't save {jar.path.name} ({e.error}), but no cookie had changed — the copy on "
                    "disk is still current. Failing this run; the next one retries.")
                return EXIT_ERROR
            log(f"🔴 couldn't save {jar.path.name} ({e.error}) — a rotated cookie was NOT saved; "
                "the session is likely lost")
            alert_session(state, "lost", f"saving {jar.path.name} failed: {e.error}",
                          bot_session.file_fingerprint(jar.path), email)
            try_save_state(state)
            return EXIT_JAR
        except Exception as e:
            log(f"⚠ iteration {i + 1} failed: {where(e)}")
            status = "ok"
        state_saved = try_save_state(state)
        if status == "login":
            alert_session(state, "expired", None, bot_session.file_fingerprint(jar.path), email)
            try_save_state(state)
            return EXIT_SESSION
        if not state_saved:
            state_write_failed = True
        if i < iterations - 1:
            log(f"  sleeping {interval}s before next iteration...")
            time.sleep(interval)

    if state_write_failed:
        log(f"Done, but {STATE_FILE.name} couldn't be written — failing the run (dedupe may repeat an email).")
        return EXIT_ERROR
    log("Done.")
    return EXIT_OK


def alert_session_lost(reason):
    """Called by monitor.yml when bot_session.enc couldn't be pushed after
    a cookie rotated."""
    state = open_state()
    email = email_settings(load_config())
    if email is None:
        return EXIT_SETUP
    if is_halted(state):
        email_halt_once(state, email)
    alert_session(state, "lost", reason, bot_session.file_fingerprint(current_jar_path()), email)
    try_save_state(state)
    return EXIT_JAR


STATE_NOT_SAVED_SUBJECT = "🔴 state not saved — check before trusting auto-book"
# Kept in the Actions cache by monitor.yml (never committed): written when a
# run's state.json couldn't be pushed, with the blob id of the repo's
# state.json at that moment. While the repo's state.json is still that blob,
# no push has succeeded since, so auto-book stays off.
STATE_ALERT_MARKER = ROOT / ".state_alert.json"
PUSH_MARKER_STATUS_ENV = "PUSH_MARKER_STATUS"  # the cache-restore step's outcome, set by monitor.yml
REPO_STATE_BLOB_ENV = "REPO_STATE_BLOB"        # blob id of state.json in the checked-out commit
UNKNOWN_BASELINE_MINUTES = 60  # a marker with no blob id pauses auto-book for this long


def read_push_marker():
    """(marker or None, problem or None)."""
    status = os.environ.get(PUSH_MARKER_STATUS_ENV)
    if status is not None and status.strip() != "success":
        return None, f"the marker cache couldn't be read ({status.strip() or 'no outcome'})"
    try:
        marker = json.loads(STATE_ALERT_MARKER.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as e:
        return None, f"the marker couldn't be read ({type(e).__name__})"
    if not isinstance(marker, dict):
        return None, "the marker isn't an object"
    return marker, None


def push_failure_block(state):
    """True (and state["autobook_blocked"] set) while an earlier run's failed
    state push hasn't been followed by a successful one, or if that can't be
    told (assume the worst). Clears itself once the repo's state.json changes."""
    marker, problem = read_push_marker()
    reason = None
    if problem:
        reason = problem
    elif marker is not None:
        baseline = marker.get("remote_blob")
        current = os.environ.get(REPO_STATE_BLOB_ENV, "").strip()
        if baseline and baseline != "unknown":
            if not current or current == "unknown":
                reason = "can't tell whether state.json has been saved since"
            elif current == baseline:
                reason = "state_push_failed"
        else:
            since = _parse_time(marker.get("sent_at"))
            if since is None or (datetime.now(TZ) - since).total_seconds() < UNKNOWN_BASELINE_MINUTES * 60:
                reason = "state_push_failed (no baseline)"
    if reason:
        state["autobook_blocked"] = {"reason": reason, "since": (marker or {}).get("sent_at") or datetime.now(TZ).isoformat()}
        return True
    state.pop("autobook_blocked", None)
    return False


def alert_state_not_saved(reason, remote_blob=None):
    """Called by monitor.yml when this run's state.json couldn't be pushed:
    the next run starts from the older state.json in the repo, which may
    lack the auto-book records written in this run. Writes the marker that
    keeps auto-book off until a push succeeds (even if the email fails), and
    emails once per episode: not again while the repo's state.json
    (remote_blob, its git blob id) is the one already emailed about."""
    blob = remote_blob if remote_blob and remote_blob != "unknown" else (
        os.environ.get(REPO_STATE_BLOB_ENV, "").strip() or None)
    if blob == "unknown":
        blob = None
    marker, _ = read_push_marker()
    same_episode = bool(blob) and isinstance(marker, dict) and marker.get("remote_blob") == blob
    new_marker = {"remote_blob": blob,
                  "sent_at": marker.get("sent_at") if same_episode else datetime.now(TZ).isoformat(),
                  "emailed": bool(same_episode and marker.get("emailed"))}

    def write_marker():
        try:
            STATE_ALERT_MARKER.write_text(json.dumps(new_marker))
        except OSError as e:
            log(f"⚠ couldn't write {STATE_ALERT_MARKER.name}: {type(e).__name__}")

    write_marker()  # first: the pause must not depend on the email or the config
    if new_marker["emailed"]:
        log(f"state-not-saved email already sent for this episode (since {new_marker['sent_at']}) — not resending")
        return EXIT_OK
    email = email_settings(load_config())
    if email is None:
        return EXIT_SETUP
    try:
        records = {k: v.get("status") for k, v in load_state().get(autobook.LIVE_KEY, {}).items()}
    except StateUnreadable:
        records = None
    lines = ([f"  {k}: {v}" for k, v in sorted(records.items())] or ["  (none)"]) if records is not None \
        else ["  (this run's state.json couldn't be read either)"]
    body = (f"This run's {STATE_FILE.name} couldn't be saved to the repo ({reason}).\n\n"
            "The next runs start from the older state.json in the repo, which may not know what this\n"
            "run auto-booked. So auto-book is PAUSED (notify-only) until a later run saves\n"
            "state.json; it then resumes by itself. Check My bookings now:\n\n"
            f"  {BASE}/bookings?tab=pending\n  {BASE}/bookings\n\n"
            "Auto-book records this run held (date|time: status):\n" + "\n".join(lines) + "\n\n"
            "Then check the Actions log for why the push failed.\n\n"
            "You get this email once per episode.\n")
    try:
        send_email(STATE_NOT_SAVED_SUBJECT, body, *email)
    except Exception as e:
        log(f"⚠ state-not-saved email failed: {type(e).__name__}")
        return EXIT_ERROR
    log("✉ state-not-saved email sent")
    new_marker["emailed"] = True
    write_marker()
    return EXIT_OK


def main(argv=None):
    ap = argparse.ArgumentParser(description="Crystal Sports court monitor (notify-only).")
    ap.add_argument("--alert-session-lost", metavar="REASON",
                    help="send the 'session likely lost' email and exit 4")
    ap.add_argument("--alert-state-not-saved", metavar="REASON",
                    help="send the 'state not saved' email (deduped by $REMOTE_STATE_BLOB)")
    args = ap.parse_args(argv)
    if args.alert_session_lost is not None:
        return alert_session_lost(args.alert_session_lost)
    if args.alert_state_not_saved is not None:
        return alert_state_not_saved(args.alert_state_not_saved, os.environ.get("REMOTE_STATE_BLOB", "").strip() or None)
    return run(iterations=int(os.environ.get("LOOP_ITERATIONS", "4")),
               interval=int(os.environ.get("LOOP_INTERVAL_SECONDS", "60")))


if __name__ == "__main__":
    sys.exit(main())
