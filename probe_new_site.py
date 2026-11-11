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
  Requests:    each round GETs /booking?date=<tomorrow>&loc=<LOC>&time=0&sel=
               for LOC001 then LOC002, redirects NOT followed. Also sends
               .AspNetCore.Culture=c=en|uic=en and reports whether the page
               came back in English; parsing doesn't depend on it.
  Options:     --repeat N rounds (default 1), --interval SECONDS between
               rounds (default 300). --repeat 12 --interval 300 = one hour.
  Exit code:   0  grid parsed for both locations in every round
               1  at least one location/round returned no grid
               2  no jar file yet and CS_DID or CS_MEMBER is missing
               3  stopped on a login redirect (session no longer valid)
               4  the jar file couldn't be read or written

Hard rules, enforced in code: GET only, /booking only (never
/booking/review or /booking/confirm), never follow a redirect, stop on
the first login redirect and never retry, never print a cookie value —
only names and fingerprints (length + first 8 hex chars of SHA-256).
Every output line also goes through redact() in case the server ever
echoes a value back.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

import requests
from requests.cookies import create_cookie

HOST = "crystalsports-booking.kegroup.co.th"
BASE = f"https://{HOST}"
BOOKING_PATH = "/booking"
FORBIDDEN_PATH_FRAGMENTS = ("/booking/review", "/booking/confirm")
LOCATIONS = {"LOC001": "Crystal Sports", "LOC002": "Crystal Sports G"}
TZ = ZoneInfo("Asia/Bangkok")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
JAR_FILE = Path(__file__).resolve().parent / ".bot_session.json"
JAR_VERSION = 1
SEED_ENV = {"cs_auth": "CS_AUTH", "cs.did": "CS_DID", "cs.member": "CS_MEMBER"}
OPTIONAL_SEED = {"cs_auth"}
CULTURE_COOKIE = {".AspNetCore.Culture": "c=en|uic=en"}

# Calibrated against the live site on 2026-11-11: with no cookies,
# /booking 302s to /account/login?ReturnUrl=... (served by IIS, no
# Cloudflare headers seen from a residential IP at the time).
LOGIN_REDIRECT_MARKERS = ("/account/login", "login", "signin", "sign-in")
LOGIN_BODY_MARKERS = ('type="password"', "/account/login", "เข้าสู่ระบบ", "otp")
CHALLENGE_BODY_MARKERS = (
    "just a moment", "challenge-platform", "cf-turnstile",
    "challenges.cloudflare.com/turnstile",
)
THAI_CHARS = re.compile(f"[{chr(0x0E00)}-{chr(0x0E7F)}]")  # the Thai Unicode block
FIRST_INT = re.compile(r"\d+")  # str pattern: also matches Thai digits, which int() accepts

EXIT_OK, EXIT_NO_GRID, EXIT_NO_SEED, EXIT_LOGIN, EXIT_JAR = 0, 1, 2, 3, 4

_secrets = set()


def stamp():
    return datetime.now(TZ).isoformat(timespec="seconds")


def fingerprint(value):
    return f"len={len(value)} sha256={hashlib.sha256(value.encode('utf-8')).hexdigest()[:8]}"


def remember_secret(value):
    # Very short values would make redaction mangle ordinary output; real
    # session cookies are far longer than this.
    if value and len(value) >= 8:
        _secrets.add(value)


def redact(text):
    text = str(text)
    for value in sorted(_secrets, key=len, reverse=True):
        text = text.replace(value, "<redacted>")
        text = text.replace(quote(value, safe=""), "<redacted>")
    return text


def say(text=""):
    print(redact(text), flush=True)


class JarError(Exception):
    pass


def jar_records(session):
    return [
        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
         "secure": bool(c.secure), "expires": c.expires}
        for c in session.cookies
    ]


def load_jar(path, session):
    try:
        data = json.loads(Path(path).read_text())
        if data.get("version") != JAR_VERSION or not isinstance(data.get("cookies"), list):
            raise JarError(f"unexpected format (version={data.get('version')!r})")
        for r in data["cookies"]:
            session.cookies.set_cookie(create_cookie(
                r["name"], r["value"], domain=r["domain"], path=r["path"],
                secure=r["secure"], expires=r["expires"],
            ))
            remember_secret(r["value"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise JarError(f"{type(e).__name__}: {e}") from None


def save_jar(path, session):
    """Write the jar atomically: a crash or failed write at any point leaves
    the previous file intact, never a half-written one."""
    path = Path(path)
    payload = json.dumps({
        "version": JAR_VERSION, "host": HOST, "updated_at": stamp(),
        "cookies": jar_records(session),
    }, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".bot_session.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def seed_session(session):
    values = {name: os.environ.get(env, "").strip() for name, env in SEED_ENV.items()}
    missing = [env for name, env in SEED_ENV.items() if not values[name] and name not in OPTIONAL_SEED]
    if missing:
        return missing, []
    absent = []
    for name, value in values.items():
        if not value:
            absent.append(name)
            continue
        # Explicit host + "/" so the server's first rotation (a host-only
        # cookie on path /) REPLACES this entry instead of sitting beside it.
        session.cookies.set_cookie(create_cookie(name, value, domain=HOST, path="/", secure=True))
        remember_secret(value)
    return [], absent


def cookie_values(session):
    values, dupes = {}, set()
    for c in session.cookies:
        if c.name in values:
            dupes.add(c.name)
        values[c.name] = c.value
    return values, dupes


def cookie_expiries(session):
    return {c.name: c.expires for c in session.cookies}


def fmt_duration(seconds):
    seconds = int(seconds)
    sign = "-" if seconds < 0 else ""
    days, rem = divmod(abs(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    return f"{sign}{days}d {hours}h {rem // 60}m" if days else f"{sign}{hours}h {rem // 60}m"


def describe_expiry(expires, previous=None):
    """Expiry only, never the value. `expires` is what requests parsed from
    the Set-Cookie (Expires, or Max-Age converted to a time). `previous` is
    omitted when unknown — e.g. a seeded cookie, whose browser-side expiry
    was never copied — rather than shown as if it were a session cookie."""
    if expires is None:
        return "expires=session (no Expires/Max-Age)"
    text = (f"expires={datetime.fromtimestamp(expires, TZ).isoformat(timespec='seconds')} "
            f"(in {fmt_duration(expires - time.time())}")
    if previous is not None:
        delta = expires - previous
        text += f"; {'+' if delta >= 0 else '-'}{fmt_duration(abs(delta))} vs previous"
    return text + ")"


def merge_set_cookies(session, resp):
    """requests has already applied resp's Set-Cookie headers to
    session.cookies (including deletions). If the server just reissued a
    cookie under a different domain/path than the copy we hold, both would
    be sent on the next request — and the old one is exactly the
    superseded value that kills the session. Drop it."""
    just_set = {}
    for c in resp.cookies:
        just_set.setdefault(c.name, set()).add((c.domain, c.path))
    for c in list(session.cookies):
        if c.name in just_set and (c.domain, c.path) not in just_set[c.name]:
            session.cookies.clear(c.domain, c.path, c.name)
    for c in session.cookies:
        remember_secret(c.value)


def diff_cookies(before, after):
    changes = []
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name), after.get(name)
        if old == new:
            continue
        kind = "NEW" if old is None else "REMOVED" if new is None else "ROTATED"
        changes.append((kind, name, old, new))
    return changes


class PageParser(HTMLParser):
    """Collects <html lang>, <title>, and every <a class="slot ..."> with the
    text of its span.slot-time / span.slot-meta children."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lang = ""
        self.title = ""
        self.slots = []
        self._in_title = False
        self._slot = None
        self._slot_depth = 0
        self._field = None
        self._field_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "html":
            self.lang = attrs.get("lang") or ""
        if tag == "title":
            self._in_title = True
        if tag in self.VOID:
            return
        classes = (attrs.get("class") or "").split()
        if self._slot is None:
            if tag == "a" and "slot" in classes:
                self._slot = {"classes": classes, "time": "", "meta": ""}
                self._slot_depth = 1
            return
        self._slot_depth += 1
        if self._field is None and tag == "span":
            if "slot-time" in classes:
                self._field, self._field_depth = "time", self._slot_depth
            elif "slot-meta" in classes:
                self._field, self._field_depth = "meta", self._slot_depth

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self.VOID or self._slot is None:
            return
        if self._field is not None and self._slot_depth == self._field_depth:
            self._field = None
        self._slot_depth -= 1
        if self._slot_depth == 0:
            self.slots.append(self._slot)
            self._slot = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._slot is not None and self._field:
            self._slot[self._field] += data


def parse_page(html):
    """Language-independent: a slot is available iff its class list lacks
    "is-disabled", and its free count is the first integer in slot-meta —
    so "2 free", "ว่าง 2" and "ว่าง ๒" all give 2. Text like "Full" is never
    relied on."""
    p = PageParser()
    p.feed(html)
    slots = []
    for s in p.slots:
        meta = " ".join(s["meta"].split())
        m = FIRST_INT.search(meta)
        slots.append({
            "time": " ".join(s["time"].split()) or "?",
            "available": "is-disabled" not in s["classes"],
            "free": int(m.group()) if m else None,
            "meta": meta,
        })
    thai = any(THAI_CHARS.search(s["meta"]) for s in slots) or bool(THAI_CHARS.search(p.title))
    return {"slots": slots, "lang": p.lang, "thai": thai, "title": " ".join(p.title.split())}


def probe_get(session, params):
    url = BASE + BOOKING_PATH
    path = urlsplit(url).path
    if path != BOOKING_PATH or any(f in url for f in FORBIDDEN_PATH_FRAGMENTS):
        raise RuntimeError(f"refusing to request {path!r} — probe is read-only, /booking only")
    # The culture cookie goes per-request so it is sent but never stored in
    # the jar — the jar holds only what the server itself issued.
    return session.get(url, params=params, cookies=CULTURE_COOKIE, allow_redirects=False, timeout=20)


def probe_once(session, jar_path, loc, date):
    params = {"date": date, "loc": loc, "time": "0", "sel": ""}
    result = {"loc": loc, "status": None, "ok": False, "login": False, "changes": [],
              "jar_error": None, "error": None, "page": None, "diag": []}
    before, _ = cookie_values(session)
    before_exp = cookie_expiries(session)
    try:
        resp = probe_get(session, params)
    except requests.RequestException as e:
        result["error"] = f"network error: {e}"
        return result

    # Persist first, before parsing or printing anything about the response.
    merge_set_cookies(session, resp)
    after, dupes = cookie_values(session)
    after_exp = cookie_expiries(session)
    result["changes"] = diff_cookies(before, after)
    try:
        save_jar(jar_path, session)
    except OSError as e:
        result["jar_error"] = f"{type(e).__name__}: {e}"
        return result

    for kind, name, old, new in result["changes"]:
        if kind == "ROTATED":
            say(f"[{stamp()}] ROTATED {name}: {fingerprint(old)} -> {fingerprint(new)} | "
                f"{describe_expiry(after_exp.get(name), before_exp.get(name))}")
        elif kind == "NEW":
            say(f"[{stamp()}] NEW {name}: {fingerprint(new)} | {describe_expiry(after_exp.get(name))}")
        else:
            say(f"[{stamp()}] REMOVED {name}: was {fingerprint(old)}")
    for name in sorted(dupes):
        say(f"[{stamp()}] WARNING: jar holds more than one {name} cookie")

    result["status"] = resp.status_code
    body = resp.text or ""
    body_lower = body.lower()
    location = resp.headers.get("Location", "")
    result["login"] = (300 <= resp.status_code < 400
                       and any(m in location.lower() for m in LOGIN_REDIRECT_MARKERS))
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
        diag.append(f"title:      {result['page']['title']}")
    challenge_hits = [m for m in CHALLENGE_BODY_MARKERS if m in body_lower]
    if resp.headers.get("cf-mitigated"):
        challenge_hits.insert(0, f"cf-mitigated: {resp.headers['cf-mitigated']}")
    login_hits = [m for m in LOGIN_BODY_MARKERS if m in body_lower]
    if challenge_hits:
        diag.append(f"verdict:    CLOUDFLARE CHALLENGE — {', '.join(challenge_hits)}")
    elif result["login"]:
        diag.append("verdict:    LOGIN REDIRECT")
    elif result["ok"]:
        diag.append(f"verdict:    BOOKING PAGE — {len(result['page']['slots'])} slot(s)")
    elif resp.status_code == 200:
        diag.append("verdict:    200 but no <a class=\"slot\"> elements"
                    + (f" (login-page markers: {', '.join(login_hits)})" if login_hits else ""))
    else:
        diag.append(f"verdict:    UNEXPECTED — status {resp.status_code}, no grid")
    return result


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


def run(repeat=1, interval=300, jar_path=JAR_FILE, session=None):
    session = session or requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,th;q=0.8",
    })
    jar_path = Path(jar_path)

    if jar_path.exists():
        try:
            load_jar(jar_path, session)
        except JarError as e:
            say(f"ERROR: {jar_path.name} exists but couldn't be read ({e}).")
            say("Not falling back to CS_AUTH/CS_DID/CS_MEMBER — they may be superseded cookies,")
            say("and presenting one kills the session. Fix or delete the file, then reseed from a fresh login.")
            return EXIT_JAR
        say(f"Using existing jar {jar_path.name} (mode {oct(jar_path.stat().st_mode & 0o777)}).")
        set_env = [env for env in SEED_ENV.values() if os.environ.get(env)]
        if set_env:
            say(f"  {', '.join(set_env)} set but IGNORED — the jar exists, so it is the only source of cookies.")
    else:
        missing, absent = seed_session(session)
        if missing:
            say(f"ERROR: no {jar_path.name} yet and {', '.join(missing)} missing — nothing seeded, no requests sent.")
            return EXIT_NO_SEED
        try:
            save_jar(jar_path, session)
        except OSError as e:
            say(f"ERROR: couldn't create {jar_path.name} ({type(e).__name__}: {e}) — no requests sent.")
            return EXIT_JAR
        say(f"Seeded {jar_path.name} from env vars (mode 0o600). From now on the env vars are ignored.")
        for name in absent:
            say(f"  {name} absent (not seeded)")

    say("Cookies in jar (values never printed):")
    names, _ = cookie_values(session)
    for name in sorted(names):
        say(f"  {name} {fingerprint(names[name])}")
    say()

    all_ok = True
    for rnd in range(1, repeat + 1):
        date = (datetime.now(TZ) + timedelta(days=1)).date().isoformat()
        for loc in LOCATIONS:
            result = probe_once(session, jar_path, loc, date)
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
        if rnd < repeat:
            say(f"  sleeping {interval:g}s before round {rnd + 1}/{repeat}...")
            time.sleep(interval)

    say()
    say(f"Done: {repeat} round(s) — " + ("grid parsed every time." if all_ok else "at least one location had NO GRID."))
    return EXIT_OK if all_ok else EXIT_NO_GRID


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only probe of the new Crystal Sports booking site.")
    ap.add_argument("--repeat", type=int, default=1, help="number of rounds (default 1)")
    ap.add_argument("--interval", type=float, default=300, help="seconds between rounds (default 300)")
    args = ap.parse_args(argv)
    if args.repeat < 1 or args.interval < 0:
        ap.error("--repeat must be >= 1 and --interval >= 0")
    return run(repeat=args.repeat, interval=args.interval)


if __name__ == "__main__":
    sys.exit(main())
