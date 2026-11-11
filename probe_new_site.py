#!/usr/bin/env python3
"""
Read-only probe for the new Crystal Sports booking site (live since
2026-11-11; the old api_helper.php API now redirects to /).

Answers one question: with the owner's logged-in cookies, can an
automated client still see the availability grid? Run it from the Mac
and from GitHub Actions (probe.yml) to compare the two.

  Reads cookies from:  CS_AUTH, CS_DID, CS_MEMBER env vars
                       (sent as cs_auth, cs.did, cs.member)
  Requests:            GET /booking?date=<tomorrow>&loc=<LOC>&time=0&sel=
                       for LOC001 and LOC002, redirects NOT followed
  Exit code:           0 if the slot grid parsed for both locations,
                       1 if not, 2 if a cookie env var is missing

Hard rules, enforced in probe_get() rather than just by convention: GET
only, path /booking only (never /booking/review or /booking/confirm),
never follow a redirect, never print a cookie value. Every line of
output goes through say(), which redacts the cookie values (raw and
URL-encoded) in case the server ever echoes one back. Each cookie's
length and the first 8 hex chars of its SHA-256 are printed instead, so
a Mac run and a GitHub Actions run can be checked for using the same
values without revealing them.
"""

import hashlib
import os
import re
import sys
from datetime import datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

import requests

BASE = "https://crystalsports-booking.kegroup.co.th"
BOOKING_PATH = "/booking"
FORBIDDEN_PATH_FRAGMENTS = ("/booking/review", "/booking/confirm")
LOCATIONS = {"LOC001": "Crystal Sports", "LOC002": "Crystal Sports G"}
TZ = ZoneInfo("Asia/Bangkok")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
COOKIE_ENV = {"cs_auth": "CS_AUTH", "cs.did": "CS_DID", "cs.member": "CS_MEMBER"}

# Calibrated against the live site on 2026-11-11: with no cookies,
# /booking 302s to /account/login?ReturnUrl=... (served by IIS, no
# Cloudflare headers seen from a residential IP at the time).
LOGIN_REDIRECT_MARKERS = ("/account/login", "login", "signin", "sign-in")
LOGIN_BODY_MARKERS = ('type="password"', "/account/login", "เข้าสู่ระบบ", "otp")
CHALLENGE_BODY_MARKERS = (
    "just a moment", "challenge-platform", "cf-turnstile",
    "challenges.cloudflare.com/turnstile",
)

_secrets = []


def redact(text):
    text = str(text)
    for value in _secrets:
        text = text.replace(value, "<redacted>")
        text = text.replace(quote(value, safe=""), "<redacted>")
    return text


def say(text=""):
    print(redact(text), flush=True)


class PageParser(HTMLParser):
    """Collects <title> and every <a class="slot ..."> with the text of its
    span.slot-time / span.slot-meta children."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.slots = []
        self._in_title = False
        self._slot = None
        self._slot_depth = 0
        self._field = None
        self._field_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in self.VOID:
            return
        classes = (dict(attrs).get("class") or "").split()
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


def free_count(meta):
    meta = meta.strip()
    if meta.lower() == "full":
        return 0
    m = re.search(r"(\d+)\s*free", meta, re.IGNORECASE)
    return int(m.group(1)) if m else None


def probe_get(params, cookies):
    url = BASE + BOOKING_PATH
    path = urlsplit(url).path
    if path != BOOKING_PATH or any(f in url for f in FORBIDDEN_PATH_FRAGMENTS):
        raise RuntimeError(f"refusing to request {path!r} — probe is read-only, /booking only")
    return requests.get(
        url,
        params=params,
        cookies=cookies,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,th;q=0.8",
        },
        allow_redirects=False,
        timeout=20,
    )


def probe_location(loc, date, cookies):
    params = {"date": date, "loc": loc, "time": "0", "sel": ""}
    say(f"=== {loc} ({LOCATIONS[loc]}) — GET {BOOKING_PATH}?date={date}&loc={loc}&time=0&sel=")

    try:
        resp = probe_get(params, cookies)
    except requests.RequestException as e:
        say(f"  network error: {e}")
        return False

    headers = resp.headers
    body = resp.text or ""
    body_lower = body.lower()
    location = headers.get("Location", "")

    say(f"  status:        {resp.status_code}")
    if location:
        say(f"  location:      {location}")
    say(f"  size:          {len(resp.content)} bytes")
    say(f"  server:        {headers.get('Server', '(none)')}")
    say(f"  cloudflare:    cf-ray {'present' if 'CF-RAY' in headers else 'absent'}, "
        f"cf-mitigated={headers.get('cf-mitigated', '(none)')}")
    if resp.cookies:
        say(f"  set-cookie:    names only: {', '.join(sorted(c.name for c in resp.cookies))}")

    challenge_hits = [m for m in CHALLENGE_BODY_MARKERS if m in body_lower]
    if headers.get("cf-mitigated"):
        challenge_hits.insert(0, f"cf-mitigated: {headers['cf-mitigated']}")
    login_hits = []
    if 300 <= resp.status_code < 400 and any(m in location.lower() for m in LOGIN_REDIRECT_MARKERS):
        login_hits.append("redirected to a login URL")
    login_hits += [m for m in LOGIN_BODY_MARKERS if m in body_lower]

    parser = PageParser()
    if resp.status_code == 200:
        parser.feed(body)
    if parser.title.strip():
        say(f"  title:         {' '.join(parser.title.split())}")

    if resp.status_code == 200 and parser.slots:
        say(f"  verdict:       BOOKING PAGE — {len(parser.slots)} slot(s) parsed")
        for s in parser.slots:
            time_text = " ".join(s["time"].split()) or "?"
            meta_text = " ".join(s["meta"].split())
            free = free_count(meta_text)
            disabled = "is-disabled" in s["classes"]
            say(f"    {time_text:>6}  free={'?' if free is None else free:<3} "
                f"disabled={'yes' if disabled else 'no ':<3}  (meta: {meta_text!r})")
        return True

    if challenge_hits:
        say(f"  verdict:       CLOUDFLARE CHALLENGE — {', '.join(challenge_hits)}")
    elif login_hits:
        say(f"  verdict:       LOGIN PAGE — {', '.join(login_hits)} (cookies missing, expired, or rejected)")
    elif resp.status_code == 200:
        say("  verdict:       200 but no <a class=\"slot\"> elements found — page markup may differ from expected")
    else:
        say(f"  verdict:       UNEXPECTED — status {resp.status_code}, no grid")
    return False


def main():
    values = {name: os.environ.get(env, "").strip() for name, env in COOKIE_ENV.items()}
    _secrets.extend(v for v in values.values() if v)

    say("Cookie env vars (values never printed — length and SHA-256 prefix only):")
    for name, env in COOKIE_ENV.items():
        value = values[name]
        if value:
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
            say(f"  {name} len={len(value)} sha256={digest}  (from {env})")
        else:
            say(f"  {name} MISSING  (from {env})")
    missing = [COOKIE_ENV[n] for n, v in values.items() if not v]
    if missing:
        say(f"ERROR: missing {', '.join(missing)} — no requests sent.")
        return 2

    date = (datetime.now(TZ) + timedelta(days=1)).date().isoformat()
    say(f"Probing tomorrow (Asia/Bangkok): {date}")
    say()

    results = {loc: probe_location(loc, date, values) for loc in LOCATIONS}
    say()
    say("Summary: " + ", ".join(f"{loc}={'grid parsed' if ok else 'NO GRID'}" for loc, ok in results.items()))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
