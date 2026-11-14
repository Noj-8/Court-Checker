#!/usr/bin/env python3
"""
Read-only probe for the new Crystal Sports booking site (live since
2026-11-11; the old api_helper.php API now redirects to /).

Proves the bot can keep its OWN login session alive: the site rotates
cs.member via Set-Cookie on authenticated responses, and presenting a
superseded cs.member kills the whole login session. So the session lives
in a cookie jar that is rewritten after every single response, and
requests are strictly sequential.

  Cookie jar:  .bot_session.json next to this script (gitignored, mode
               600). Seeded ONCE from CS_DID / CS_MEMBER (required) and
               CS_AUTH (optional) — sent as cs.did, cs.member, cs_auth — if
               it doesn't exist; once it exists the env vars are ignored.
               Rewritten atomically (temp file, fsync, rename) right after
               every response.
  Encrypted:   if BOT_JAR_KEY is set (GitHub Actions), the jar is
               bot_session.enc instead — the same JSON, Fernet-encrypted
               in memory, so plaintext never touches disk. It is rewritten
               (atomically) only when the cookies actually changed, because
               Fernet output differs on every call and git would otherwise
               see a change every run. In Actions without BOT_JAR_KEY the
               probe refuses to run at all.
  Handed off:  if .bot_session.json is gone but a
               .bot_session.json.handed-off-<ts> exists (seal_session.py
               moved the session to Actions), the Mac probe refuses to run.
  Requests:    each round GETs /booking?date=<tomorrow>&loc=<LOC>&time=0&sel=
               for LOC001 then LOC002, redirects NOT followed. Also sends
               .AspNetCore.Culture=c=en|uic=en and reports whether the page
               came back in English; parsing doesn't depend on it.
  Options:     --repeat N rounds (default 1), --interval SECONDS between
               rounds (default 300). --repeat 12 --interval 300 = one hour.
               --check-account: after the first round's grids, also GET
               /bookings and /bookings?tab=pending (the account pages
               auto-book reads first) and print, for each, the status and
               "ok" or "login redirect" — never page text or refs. Stops
               at the first login redirect, like everything else.
  Exit code:   0  grid parsed for both locations in every round
               1  at least one location/round returned no grid, or an
                  account page (--check-account) wasn't ok
               2  no jar file yet and CS_DID or CS_MEMBER is missing
               3  stopped on a login redirect (session no longer valid —
                  or, with --check-account, an account page refused it)
               4  the jar file couldn't be read or written (incl. a wrong or
                  invalid BOT_JAR_KEY, or Actions without BOT_JAR_KEY)
               5  the session was handed off to GitHub Actions

Hard rules, enforced in code: GET only, /booking only — plus /bookings
(both tabs) with --check-account; never /booking/review, /booking/confirm
or a booking's own page — never follow a redirect, stop on
the first login redirect and never retry, never print a cookie value —
only names and fingerprints (length + first 8 hex chars of SHA-256).
Every output line also goes through redact() in case the server ever
echoes a value back.

The session/jar machinery lives in bot_session.py and the page parsing in
booking_page.py (both shared with monitor.py); the names are re-exported
here so existing callers and tests keep working.
"""


import argparse
import os  # noqa: F401 — tests patch probe.os.* (the shared os module bot_session writes through)
import sys
import time
from datetime import datetime, timedelta

import requests

import bot_session
from booking_page import FIRST_INT, THAI_CHARS, PageParser, is_login_page, login_page_markers, parse_page  # noqa: F401 (re-exported)
from bot_session import (  # noqa: F401 (re-exported)
    BASE, BOOKING_PATH, BOOKINGS_PATH, CULTURE_COOKIE, ENC_FILE, HOST, JAR_FILE,
    JAR_KEY_ENV, JAR_VERSION, LOGIN_REDIRECT_MARKERS, OPTIONAL_SEED, SEED_ENV, TZ, USER_AGENT,
    JarError, JarSaveError, SessionOpenError, _secrets, cookie_expiries, cookie_values,
    create_cookie, describe_expiry, diff_cookies, fingerprint, fmt_duration, handed_off_files,
    jar_records, load_jar, make_fernet, merge_set_cookies, redact, remember_secret, save_jar,
    say, seed_session, stamp,
)

LOCATIONS = {"LOC001": "Crystal Sports", "LOC002": "Crystal Sports G"}
# The probe itself only ever GETs /booking (and /bookings with
# --check-account); it never touches these.
FORBIDDEN_PATH_FRAGMENTS = ("/booking/review", "/booking/confirm")
CHALLENGE_BODY_MARKERS = (
    "just a moment", "challenge-platform", "cf-turnstile",
    "challenges.cloudflare.com/turnstile",
)

EXIT_OK, EXIT_NO_GRID, EXIT_NO_SEED, EXIT_LOGIN, EXIT_JAR, EXIT_HANDED_OFF = 0, 1, 2, 3, 4, 5
OPEN_ERROR_EXIT = {"jar": EXIT_JAR, "no_seed": EXIT_NO_SEED, "no_jar": EXIT_NO_SEED,
                   "handed_off": EXIT_HANDED_OFF}


def probe_once(jar, loc, date):
    params = {"date": date, "loc": loc, "time": "0", "sel": ""}
    result = {"loc": loc, "status": None, "ok": False, "login": False, "changes": [],
              "jar_error": None, "error": None, "page": None, "diag": []}
    try:
        fetched = bot_session.fetch(jar, BOOKING_PATH, params)
    except requests.RequestException as e:
        result["error"] = f"network error: {e}"
        return result
    except JarSaveError as e:
        result["jar_error"], result["changes"] = e.error, e.changes
        return result

    resp = fetched.resp
    result["changes"] = fetched.changes
    result["status"] = resp.status_code
    result["login"] = fetched.login
    body = resp.text or ""
    body_lower = body.lower()
    location = resp.headers.get("Location", "")
    if resp.status_code == 200:
        result["page"] = parse_page(body)
        result["ok"] = bool(result["page"]["slots"])

    diag = result["diag"]
    if location:
        diag.append(f"location:   {location}")
    diag.append(f"size:       {len(resp.content)} bytes")
    diag.append(f"server:     {resp.headers.get('Server', '(none)')}")
    diag.append(f"cloudflare: cf-ray {'present' if 'CF-RAY' in resp.headers else 'absent'}, "
                f"cf-mitigated={resp.headers.get('cf-mitigated', '(none)')}")
    if result["page"] and result["page"]["title"]:
        diag.append(f"title:      {len(result['page']['title'])} chars (text not logged: the log is public)")
    challenge_hits = [m for m in CHALLENGE_BODY_MARKERS if m in body_lower]
    if resp.headers.get("cf-mitigated"):
        challenge_hits.insert(0, f"cf-mitigated: {resp.headers['cf-mitigated']}")
    login_hits = login_page_markers(body, resp.url)
    if challenge_hits:
        diag.append(f"verdict:    CLOUDFLARE CHALLENGE — {', '.join(challenge_hits)}")
    elif result["login"]:
        diag.append("verdict:    LOGIN REDIRECT")
    elif result["ok"]:
        diag.append(f"verdict:    BOOKING PAGE — {len(result['page']['slots'])} slot(s)"
                    + (" (court map is-locked — expected with time=0)" if result["page"]["locked"] else ""))
    elif result["page"] and result["page"]["not_open_yet"]:
        diag.append(f"verdict:    NOT OPEN YET — opens {result['page']['opens_at']}")
    elif resp.status_code == 200:
        diag.append("verdict:    200 but no <a class=\"slot\"> elements"
                    + (f" (login-page markers: {', '.join(login_hits)})" if login_hits else ""))
    else:
        diag.append(f"verdict:    UNEXPECTED — status {resp.status_code}, no grid")
    return result


ACCOUNT_PAGES = (("/bookings", {}), ("/bookings?tab=pending", {"tab": "pending"}))


def check_account_page(jar, params):
    """One GET of /bookings (params {} or {"tab": "pending"}). Returns
    (status or None, verdict): verdict is "ok", "login redirect",
    "login page (200)", or a short reason — never page text or a ref."""
    try:
        fetched = bot_session.fetch(jar, BOOKINGS_PATH, params)
    except requests.RequestException as e:
        return None, f"network error ({type(e).__name__})"
    resp = fetched.resp
    if fetched.login:
        return resp.status_code, "login redirect"
    if resp.status_code != 200:
        return resp.status_code, "not ok (unexpected status)"
    if is_login_page(resp.text or "", resp.url):
        return resp.status_code, "login page (200)"
    return resp.status_code, "ok"


def check_account(jar):
    """--check-account: the account pages, in order, stopping at the first
    login. Returns EXIT_OK, EXIT_NO_GRID (a page wasn't ok) or EXIT_LOGIN.
    Raises JarSaveError like probe_once's fetch would."""
    worst = EXIT_OK
    for label, params in ACCOUNT_PAGES:
        status, verdict = check_account_page(jar, params)
        say(f"[{stamp()}] account {label}: status={'ERR' if status is None else status} {verdict}")
        if verdict.startswith("login"):
            say()
            say(f"STOPPED at account page {label}: {verdict}. The grid read fine on this same session,")
            say("so account pages refuse something the grid doesn't need — e.g. a cookie the jar")
            say("lacks (see the cookie names above: is cs_auth there?). Not retrying.")
            return EXIT_LOGIN
        if verdict != "ok":
            worst = EXIT_NO_GRID
    return worst


def summary_line(rnd, repeat, result):
    head = f"[{stamp()}] round {rnd}/{repeat} {result['loc']}"
    if result["error"]:
        return f"{head} status=ERR {result['error']}"
    page = result["page"] or {"slots": [], "lang": "", "thai": False}
    open_slots = [s for s in page["slots"] if s["available"]]
    open_text = " ".join(f"{s['time']}({'?' if s['free'] is None else s['free']})" for s in open_slots) or "none"
    counts = {k: sum(1 for c in result["changes"] if c[0] == k) for k in ("ROTATED", "NEW", "REMOVED")}
    rot = f"rotations={counts['ROTATED']}"
    if counts["NEW"]:
        rot += f" (+{counts['NEW']} new)"
    if counts["REMOVED"]:
        rot += f" (-{counts['REMOVED']} removed)"
    line = f"{head} status={result['status']} slots={len(page['slots'])} open: {open_text} | {rot}"
    if result["page"]:
        line += f" | lang={page['lang'] or '?'} thai={'yes' if page['thai'] else 'no'}"
    if result["login"]:
        line += " | LOGIN REDIRECT"
    elif not result["ok"]:
        line += " | NO GRID"
    return line


def run(repeat=1, interval=300, jar_path=JAR_FILE, session=None, enc_path=ENC_FILE, check_account_pages=False):
    try:
        jar = bot_session.open_jar(jar_path, enc_path, session=session)
    except SessionOpenError as e:
        return OPEN_ERROR_EXIT[e.kind]
    jar_path = jar.path
    say()

    all_ok, account_ok = True, True
    for rnd in range(1, repeat + 1):
        date = (datetime.now(TZ) + timedelta(days=1)).date().isoformat()
        for loc in LOCATIONS:
            result = probe_once(jar, loc, date)
            if result["jar_error"]:
                say(f"[{stamp()}] round {rnd}/{repeat} {loc}: FAILED TO WRITE {jar_path.name} "
                    f"({result['jar_error']}).")
                if result["changes"]:
                    say(f"  {len(result['changes'])} cookie change(s) from that response were NOT saved — the copy")
                    say("  on disk is now superseded and presenting it will kill the session.")
                say("STOPPED. Delete the jar and reseed from a fresh login before running again.")
                return EXIT_JAR
            say(summary_line(rnd, repeat, result))
            if rnd == 1 or not result["ok"]:
                for line in result["diag"]:
                    say(f"    {line}")
            if result["login"]:
                say()
                say(f"STOPPED at round {rnd}/{repeat}, {loc}: login redirect — the bot's session is no longer valid.")
                say("Not retrying: the only cookies left are the ones just rejected, and replaying an older")
                say("one is exactly what kills a session. To recover, log in fresh in a private window,")
                say(f"delete {jar_path.name}, and reseed.")
                return EXIT_LOGIN
            all_ok = all_ok and result["ok"]
        if check_account_pages and rnd == 1:
            try:
                account = check_account(jar)
            except JarSaveError as e:
                say(f"[{stamp()}] account pages: FAILED TO WRITE {jar_path.name} ({e.error}).")
                if e.changes:
                    say(f"  {len(e.changes)} cookie change(s) from that response were NOT saved — the copy")
                    say("  on disk is now superseded and presenting it will kill the session.")
                say("STOPPED. Delete the jar and reseed from a fresh login before running again.")
                return EXIT_JAR
            if account == EXIT_LOGIN:
                return EXIT_LOGIN
            account_ok = account == EXIT_OK
        if rnd < repeat:
            say(f"  sleeping {interval:g}s before round {rnd + 1}/{repeat}...")
            time.sleep(interval)

    say()
    say(f"Done: {repeat} round(s) — " + ("grid parsed every time." if all_ok else "at least one location had NO GRID."))
    if check_account_pages:
        say("Account pages: " + ("both ok." if account_ok else "at least one was NOT ok (see above)."))
    return EXIT_OK if all_ok and account_ok else EXIT_NO_GRID


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only probe of the new Crystal Sports booking site.")
    ap.add_argument("--repeat", type=int, default=1, help="number of rounds (default 1)")
    ap.add_argument("--interval", type=float, default=300, help="seconds between rounds (default 300)")
    ap.add_argument("--check-account", action="store_true",
                    help="after the first round, also GET /bookings and /bookings?tab=pending")
    args = ap.parse_args(argv)
    if args.repeat < 1 or args.interval < 0:
        ap.error("--repeat must be >= 1 and --interval >= 0")
    return run(repeat=args.repeat, interval=args.interval, check_account_pages=args.check_account)


if __name__ == "__main__":
    sys.exit(main())
