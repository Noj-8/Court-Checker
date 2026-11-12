#!/usr/bin/env python3
"""
Crystal Sports Tennis Court Monitor — cloud edition, rebuilt for the new
booking site (live since 2026-11-11; the old api_helper.php API is gone).

NOTIFY-ONLY: AUTOBOOK_ENABLED is False until auto-book is rebuilt for the
new site (stage 3). Starred slots get the normal "slot open" email plus a
note to book manually. The monitor never sends a POST.

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
                  its free count isn't 0. No slots + an opens-at marker
                  ("opens 21:00") = "not open yet"; no slots and no marker
                  = unexpected page: logged, skipped, no emails, not a
                  session failure.
  State:          known_open["date|loc|any|HH:MM"] dedupes "slot open"
                  emails exactly as before (the grid has no per-court
                  detail, so the court part of the key is "any").
                  session_alert dedupes the session email: sent once per
                  dead jar, cleared by the next successful check.
  Exit code:      0  ok
                  1  setup error (missing env var, no/unreadable jar)
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
import json
import os
import smtplib
import sys
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import requests

import bot_session
from bot_session import BASE, BOOKING_PATH, TZ
from booking_page import parse_page, slot_time

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"
STATE_FILE = ROOT / "state.json"

# Stage 3 rebuilds auto-book for the new site. Until then there is no
# booking code at all: this constant only selects the note in the email,
# and the monitor refuses to start if it's flipped without that code.
AUTOBOOK_ENABLED = False
AUTOBOOK_PAUSED_NOTE = "Auto-book is paused while it's rebuilt for the new site — book manually."

BOOKING_URL = BASE + BOOKING_PATH
ANY_COURT = "any"  # the court part of a known_open key — the grid is per time, not per court

LOCATIONS = {
    "LOC001": "Crystal Sports",
    "LOC002": "Crystal Sports G",
}

EXIT_OK, EXIT_SETUP, EXIT_SESSION, EXIT_JAR = 0, 1, 3, 4

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


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


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
    Raises bot_session.JarSaveError if a response couldn't be persisted."""
    params = {"date": date, "loc": loc_id, "time": "0", "sel": ""}
    try:
        fetched = bot_session.fetch(jar, BOOKING_PATH, params)
    except requests.RequestException as e:
        return "error", None, f"network: {e}"
    resp = fetched.resp
    if fetched.login:
        return "login", None, resp.headers.get("Location", "")
    if resp.status_code != 200:
        return "error", None, f"http {resp.status_code}"
    body = resp.text or ""
    page = parse_page(body)
    if not page["slots"]:
        if 'type="password"' in body.lower():
            # A login form served as a 200 instead of the usual redirect.
            return "login", None, "login form (200)"
        if page["not_open_yet"]:
            return "not_open_yet", {}, page["opens_at"]
        return "error", None, (f"unexpected page — no slot grid and no opens-at marker "
                               f"({len(body)} bytes, title {page['title']!r}); skipped")
    open_times = {}
    for s in page["slots"]:
        t = slot_time(s["time"])
        if t and s["available"] and s["free"] != 0:
            open_times[t] = s["free"]
    return "ok", open_times, None


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
        log(f"⚠ session email failed: {e}")
        return
    state["session_alert"] = {"kind": kind, "sent_at": datetime.now(TZ).isoformat(), "jar_sha256": jar_fp}
    log("✉ session email sent")


def format_free(free):
    return "free count not shown" if free is None else f"{free} free"


def run_check_pass(state, config, jar, email):
    """One full check. Mutates `state` in place. Returns "ok", or "login" if
    the session is no longer valid (stopped at that request, nothing else
    sent). Raises bot_session.JarSaveError if a response couldn't be saved."""
    today_str = datetime.now(TZ).date().isoformat()
    active = [t for t in config.get("targets", []) if t["date"] >= today_str]
    if not active:
        log("  no active (future) targets — nothing to check")
        return "ok"

    fetch_keys = sorted({(t["date"], loc) for t in active for loc in t["locations"]})
    cache = {}  # (date, loc) -> {"HH:MM": free}; only for pages that were read successfully
    for date, loc in fetch_keys:
        log(f"  → {LOCATIONS.get(loc, loc)} / {date}")
        kind, open_times, info = fetch_availability(jar, date, loc)
        if kind == "login":
            log(f"      🔴 login redirect ({info}) — bot session expired, stopping")
            return "login"
        if kind == "error":
            log(f"      ⚠ {info}")
            continue
        state.pop("session_alert", None)  # the session works: a future failure may email again
        if kind == "not_open_yet":
            log("      not open yet" + (f" (opens {info})" if info else ""))
        else:
            shown = " ".join(f"{t}({'?' if f is None else f})" for t, f in sorted(open_times.items()))
            log(f"      open: {shown or 'none'}")
        cache[(date, loc)] = open_times

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
                    known_open[k] = True
                    new_alerts.append({
                        "target": target["name"], "date": target["date"], "loc_id": loc,
                        "loc": LOCATIONS.get(loc, loc), "time": t, "free": open_times[t],
                        "starred": t in starred,
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
    state["last_check"] = datetime.now(TZ).isoformat()
    state["currently_open_count"] = len(currently_open)
    log(f"  currently open across watchlist: {len(currently_open)}")

    if not new_alerts:
        log("  no new openings this iteration")
        return "ok"

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
            if a["starred"] and not AUTOBOOK_ENABLED:
                lines.append(f"      {AUTOBOOK_PAUSED_NOTE}")
            lines.append(f"      {booking_link(a['date'], a['loc_id'])}")
        lines.append("")
    lines.append(f"Book here: {BOOKING_URL}")
    try:
        send_email(f"🎾 {len(new_alerts)} tennis slot(s) open", "\n".join(lines), *email)
        log("✉ alert email sent")
    except Exception as e:
        log(f"⚠ alert email failed: {e}")
    return "ok"


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


def run(iterations=4, interval=60, jar_path=bot_session.JAR_FILE, enc_path=bot_session.ENC_FILE, session=None):
    if AUTOBOOK_ENABLED:
        log("ERROR: AUTOBOOK_ENABLED is True but there is no auto-book code for the new site yet.")
        return EXIT_SETUP
    config = load_config()
    email = email_settings(config)
    if email is None:
        return EXIT_SETUP
    state = load_state()
    state.pop("last_session_alert", None)  # the PHPSESSID-era throttle, replaced by session_alert

    try:
        jar = bot_session.open_jar(jar_path, enc_path, session=session, allow_seed=False)
    except bot_session.SessionOpenError as e:
        log(f"ERROR: couldn't open the bot session ({e.kind}) — no requests sent.")
        return EXIT_SETUP

    prev = state.get("session_alert")
    if prev and prev.get("jar_sha256") == bot_session.file_fingerprint(jar.path):
        log(f"🔴 bot session marked {prev.get('kind')} at {prev.get('sent_at')} and {jar.path.name} hasn't "
            "changed since — not sending any requests until it's reseeded (already emailed; exit 0).")
        save_state(state)
        return EXIT_OK

    log(f"Running {iterations} check pass(es), {interval}s apart — notify-only, auto-book paused")
    for i in range(iterations):
        log(f"--- iteration {i + 1}/{iterations} ---")
        try:
            status = run_check_pass(state, config, jar, email)
        except bot_session.JarSaveError as e:
            log(f"🔴 couldn't save {jar.path.name} ({e.error}) — "
                + ("a rotated cookie was NOT saved; the session is likely lost" if e.changes
                   else "nothing had changed, but stopping rather than risk it"))
            alert_session(state, "lost", f"saving {jar.path.name} failed: {e.error}",
                          bot_session.file_fingerprint(jar.path), email)
            save_state(state)
            return EXIT_JAR
        except Exception as e:
            log(f"⚠ iteration {i + 1} failed: {e}")
            status = "ok"
        save_state(state)
        if status == "login":
            alert_session(state, "expired", None, bot_session.file_fingerprint(jar.path), email)
            save_state(state)
            return EXIT_SESSION
        if i < iterations - 1:
            log(f"  sleeping {interval}s before next iteration...")
            time.sleep(interval)

    log("Done.")
    return EXIT_OK


def alert_session_lost(reason):
    """Called by monitor.yml when bot_session.enc couldn't be pushed after
    a cookie rotated."""
    email = email_settings(load_config())
    if email is None:
        return EXIT_SETUP
    state = load_state()
    alert_session(state, "lost", reason, bot_session.file_fingerprint(current_jar_path()), email)
    save_state(state)
    return EXIT_JAR


def main(argv=None):
    ap = argparse.ArgumentParser(description="Crystal Sports court monitor (notify-only).")
    ap.add_argument("--alert-session-lost", metavar="REASON",
                    help="send the 'session likely lost' email and exit 4")
    args = ap.parse_args(argv)
    if args.alert_session_lost is not None:
        return alert_session_lost(args.alert_session_lost)
    return run(iterations=int(os.environ.get("LOOP_ITERATIONS", "4")),
               interval=int(os.environ.get("LOOP_INTERVAL_SECONDS", "60")))


if __name__ == "__main__":
    sys.exit(main())
