"""
The bot's own Crystal Sports login session, shared by monitor.py and
probe_new_site.py.

The site rotates cs.member via Set-Cookie on authenticated responses, and
presenting a superseded cs.member kills the whole login session. So:

  * The session lives in a cookie jar that is rewritten atomically (temp
    file, fsync, rename) right after EVERY response, before anything else
    looks at that response.
  * With BOT_JAR_KEY set (GitHub Actions) the jar is bot_session.enc —
    Fernet-encrypted in memory, ciphertext-only on disk, rewritten only when
    a cookie actually changed. Without it, the plaintext .bot_session.json
    (gitignored, mode 600) — and never in Actions.
  * When the server reissues a cookie under a different domain/path, the
    stale copy is dropped so it can't be sent alongside the new one.
  * Requests are strictly sequential (a concurrent call raises) and
    redirects are never followed. Every request goes through _send(), which
    allows exactly: GET /booking, GET /booking/review, GET /bookings, and
    POST /booking/confirm — the last only via post_booking_confirm(), and
    only when the caller passes mode == "live". A login redirect is
    reported to the caller, who must stop — replaying older cookies is
    exactly what kills a session.
  * Cookie values are never printed: only names and fingerprints (length +
    first 8 hex chars of SHA-256), and every line goes through redact().
"""

import hashlib
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

import requests
from requests.cookies import create_cookie

HOST = "crystalsports-booking.kegroup.co.th"
BASE = f"https://{HOST}"
BOOKING_PATH = "/booking"
REVIEW_PATH = "/booking/review"
BOOKINGS_PATH = "/bookings"
CONFIRM_PATH = "/booking/confirm"
ALLOWED_GET_PATHS = frozenset({BOOKING_PATH, REVIEW_PATH, BOOKINGS_PATH})
ALLOWED_REQUESTS = frozenset({("GET", p) for p in ALLOWED_GET_PATHS} | {("POST", CONFIRM_PATH)})
TZ = ZoneInfo("Asia/Bangkok")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,th;q=0.8",
}
ROOT = Path(__file__).resolve().parent
JAR_FILE = ROOT / ".bot_session.json"
ENC_FILE = ROOT / "bot_session.enc"
JAR_KEY_ENV = "BOT_JAR_KEY"
JAR_VERSION = 1
SEED_ENV = {"cs_auth": "CS_AUTH", "cs.did": "CS_DID", "cs.member": "CS_MEMBER"}
OPTIONAL_SEED = {"cs_auth"}
CULTURE_COOKIE = {".AspNetCore.Culture": "c=en|uic=en"}

# Calibrated against the live site on 2026-11-11: with no cookies,
# /booking 302s to /account/login?ReturnUrl=...
LOGIN_REDIRECT_MARKERS = ("/account/login", "login", "signin", "sign-in")

_secrets = set()
_request_lock = threading.Lock()


# --- output & secrets --------------------------------------------------------

def stamp():
    return datetime.now(TZ).isoformat(timespec="seconds")


def fingerprint(value):
    return f"len={len(value)} sha256={hashlib.sha256(value.encode('utf-8')).hexdigest()[:8]}"


def file_fingerprint(path):
    """sha256 prefix of a jar FILE's bytes (ciphertext for bot_session.enc) —
    identifies which jar is on disk, reveals nothing. None if missing."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    except OSError:
        return None


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


# --- the jar file ------------------------------------------------------------

class JarError(Exception):
    pass


class SessionOpenError(Exception):
    """The jar couldn't be opened. kind: "jar" (unreadable/unwritable, bad
    key, Actions without a key), "no_seed", "no_jar", "handed_off"."""

    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


class JarSaveError(Exception):
    """A response was received but the jar couldn't be written. If
    `changes` is non-empty the copy on disk is now superseded."""

    def __init__(self, error, changes):
        super().__init__(error)
        self.error = error
        self.changes = changes


def jar_records(session):
    return [
        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
         "secure": bool(c.secure), "expires": c.expires}
        for c in session.cookies
    ]


def make_fernet(key):
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        raise JarError(f"{JAR_KEY_ENV} is set but the cryptography package isn't installed") from None
    try:
        return Fernet(key.strip().encode("ascii"))
    except (ValueError, TypeError, UnicodeEncodeError):  # binascii.Error is a ValueError
        raise JarError(f"{JAR_KEY_ENV} isn't a valid Fernet key (expected 44 url-safe base64 chars)") from None


def handed_off_files(jar_path):
    jar_path = Path(jar_path)
    return sorted(jar_path.parent.glob(f"{jar_path.name}.handed-off-*"))


def load_jar(path, session, fernet=None):
    try:
        raw = Path(path).read_bytes()
        if fernet is not None:
            try:
                raw = fernet.decrypt(raw)
            except Exception:  # cryptography's InvalidToken carries no message
                raise JarError(f"can't decrypt — wrong {JAR_KEY_ENV}, or the file is corrupted") from None
        data = json.loads(raw)
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


def save_jar(path, session, fernet=None):
    """Write the jar atomically: a crash or failed write at any point leaves
    the previous file intact, never a half-written one. With `fernet`, the
    bytes written are ciphertext only, and an unchanged jar isn't rewritten
    (returns False) so bot_session.enc changes in git only on a real cookie
    change."""
    path = Path(path)
    records = jar_records(session)
    if fernet is not None and path.exists():
        try:
            if json.loads(fernet.decrypt(path.read_bytes()))["cookies"] == records:
                return False
        except Exception:  # unreadable with this key: fall through and rewrite it
            pass
    data = (json.dumps({
        "version": JAR_VERSION, "host": HOST, "updated_at": stamp(), "cookies": records,
    }, indent=2) + "\n").encode("utf-8")
    if fernet is not None:
        data = fernet.encrypt(data)  # in memory; only the ciphertext reaches the disk
    # Same temp prefix for both files, so .gitignore's .bot_session.*.tmp covers it.
    fd, tmp = tempfile.mkstemp(prefix=".bot_session.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(data)
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
    return True


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


# --- cookie bookkeeping ------------------------------------------------------

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
    when = _expiry_datetime(expires)
    if when is None:
        return "expires=far future (9999-12-31 sentinel or beyond)"
    text = f"expires={when.isoformat(timespec='seconds')} (in {fmt_duration(expires - time.time())}"
    if previous is not None and _expiry_datetime(previous) is not None:
        delta = expires - previous
        text += f"; {'+' if delta >= 0 else '-'}{fmt_duration(abs(delta))} vs previous"
    return text + ")"


def _expiry_datetime(expires):
    """The expiry as a Bangkok datetime, or None for "far future": the
    9999-12-31 "never expires" sentinel (which overflows once shifted to
    +07:00) and anything else out of range. Never raises."""
    try:
        when = datetime.fromtimestamp(expires, TZ)
    except (OverflowError, OSError, ValueError, TypeError):
        return None
    return None if when.year >= 9999 else when


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


# --- opening the session -----------------------------------------------------

@dataclass
class Jar:
    session: requests.Session
    path: Path
    fernet: object = None


def open_jar(jar_path=JAR_FILE, enc_path=ENC_FILE, session=None, allow_seed=True):
    """Load the bot's session (or seed it once from env vars, if allowed).
    Prints what it did, fingerprints only. Raises SessionOpenError — and
    sends nothing — if there is no usable jar."""
    session = session or requests.Session()
    session.headers.update(BROWSER_HEADERS)

    key = os.environ.get(JAR_KEY_ENV, "").strip()
    fernet = None
    if key:
        remember_secret(key)
        try:
            fernet = make_fernet(key)
        except JarError as e:
            say(f"ERROR: {e}. No requests sent.")
            raise SessionOpenError("jar") from None
        path = Path(enc_path)
        say(f"{JAR_KEY_ENV} set ({fingerprint(key)}): encrypted jar {path.name}, never written in plaintext.")
    elif os.environ.get("GITHUB_ACTIONS") == "true":
        say(f"ERROR: running in GitHub Actions without {JAR_KEY_ENV} — refusing, so the session is never")
        say("written to the runner's disk in plaintext. No requests sent.")
        raise SessionOpenError("jar")
    else:
        path = Path(jar_path)
        handed = handed_off_files(path)
        if handed and not path.exists():
            say(f"ERROR: no {path.name}, only {handed[-1].name} — this session was handed off to")
            say("GitHub Actions (bot_session.enc). Running it here too would present a superseded cs.member")
            say("and kill it. No requests sent. For a separate Mac session: log in fresh in a private window,")
            say("move the handed-off file out of this folder, and reseed.")
            raise SessionOpenError("handed_off")

    if path.exists():
        try:
            load_jar(path, session, fernet)
        except JarError as e:
            say(f"ERROR: {path.name} exists but couldn't be read ({e}).")
            say("Not falling back to CS_AUTH/CS_DID/CS_MEMBER — they may be superseded cookies,")
            say("and presenting one kills the session. Fix or delete the file, then reseed from a fresh login.")
            raise SessionOpenError("jar") from None
        say(f"Using existing jar {path.name} (mode {oct(path.stat().st_mode & 0o777)}).")
        set_env = [env for env in SEED_ENV.values() if os.environ.get(env)]
        if set_env:
            say(f"  {', '.join(set_env)} set but IGNORED — the jar exists, so it is the only source of cookies.")
    elif not allow_seed:
        say(f"ERROR: no {path.name} — nothing to run on (this caller never seeds). No requests sent.")
        raise SessionOpenError("no_jar")
    else:
        missing, absent = seed_session(session)
        if missing:
            say(f"ERROR: no {path.name} yet and {', '.join(missing)} missing — nothing seeded, no requests sent.")
            raise SessionOpenError("no_seed")
        try:
            save_jar(path, session, fernet)
        except OSError as e:
            say(f"ERROR: couldn't create {path.name} ({type(e).__name__}: {e}) — no requests sent.")
            raise SessionOpenError("jar") from None
        say(f"Seeded {path.name} from env vars (mode 0o600). From now on the env vars are ignored.")
        for name in absent:
            say(f"  {name} absent (not seeded)")

    say("Cookies in jar (values never printed):")
    names, _ = cookie_values(session)
    for name in sorted(names):
        say(f"  {name} {fingerprint(names[name])}")
    return Jar(session=session, path=path, fernet=fernet)


# --- the one way to talk to the site ----------------------------------------

@dataclass
class Fetched:
    resp: requests.Response
    changes: list = field(default_factory=list)
    login: bool = False


def is_login_redirect(resp):
    location = resp.headers.get("Location", "")
    return 300 <= resp.status_code < 400 and any(m in location.lower() for m in LOGIN_REDIRECT_MARKERS)


def fetch(jar, path, params):
    """GET one allowed page (ALLOWED_GET_PATHS) on the bot's session."""
    return _send(jar, "GET", path, params=params)


def post_booking_confirm(jar, fields, mode, referer):
    """THE ONLY function that POSTs, ever: to /booking/confirm, with the
    given form fields (a list of (name, value) pairs), and only when mode ==
    "live". Redirects are not followed; the jar is saved after the response
    exactly as for GETs."""
    if mode != "live":
        raise RuntimeError(f"refusing to POST {CONFIRM_PATH} in mode {mode!r} — only 'live' may book")
    return _send(jar, "POST", CONFIRM_PATH, data=list(fields),
                 headers={"Origin": BASE, "Referer": referer,
                          "Content-Type": "application/x-www-form-urlencoded"})


def _send(jar, method, path, params=None, data=None, headers=None):
    """One request on the bot's session. The jar is saved before
    this returns — before the caller parses or prints anything about the
    response — and it is saved even when the request or the body read
    raises, so a Set-Cookie that arrived is never lost: the headers (and
    cookies) are taken with stream=True, the jar is saved, and only then is
    the body read. Raises JarSaveError if the jar can't be saved (stop: the
    copy on disk may be superseded), requests.RequestException on network
    errors (including a body that fails mid-read — the cookies from its
    headers are already on disk). Fetched.login=True means the session is
    no longer valid: stop."""
    url = BASE + path
    if (method, path) not in ALLOWED_REQUESTS or urlsplit(url).path != path:
        raise RuntimeError(f"refusing to {method} {path!r} — not an allowed request on the bot session")
    if (method == "POST") != (data is not None) or (method == "GET" and data is not None):
        raise RuntimeError("only the booking POST may carry a body")
    if not _request_lock.acquire(blocking=False):
        raise RuntimeError("concurrent request on the bot session — requests must be strictly sequential")
    try:
        before, _ = cookie_values(jar.session)
        before_exp = cookie_expiries(jar.session)
        resp, request_error = None, None
        try:
            # The culture cookie goes per-request so it is sent but never
            # stored in the jar — the jar holds only what the server issued.
            # stream=True: return as soon as the headers (and Set-Cookie)
            # are in, so the jar is saved before the body is read.
            resp = jar.session.request(method, url, params=params, data=data, headers=headers,
                                       cookies=CULTURE_COOKIE, allow_redirects=False,
                                       timeout=20, stream=True)
            merge_set_cookies(jar.session, resp)
        except BaseException as e:  # saved below regardless, then re-raised
            request_error = e

        # Persist first — whatever the session now holds, even if the
        # request raised — before anything else looks at the response.
        after, dupes = cookie_values(jar.session)
        after_exp = cookie_expiries(jar.session)
        changes = diff_cookies(before, after)
        try:
            save_jar(jar.path, jar.session, jar.fernet)
        except OSError as e:
            raise JarSaveError(f"{type(e).__name__}: {e}", changes) from request_error
        if request_error is not None:
            raise request_error
        log_cookie_changes(changes, dupes, before_exp, after_exp)
        try:
            resp.content  # the body, read only now that the jar is safe on disk
        finally:
            resp.close()
    finally:
        _request_lock.release()
    return Fetched(resp=resp, changes=changes, login=is_login_redirect(resp))


def log_cookie_changes(changes, dupes, before_exp, after_exp):
    for kind, name, old, new in changes:
        if kind == "ROTATED":
            say(f"[{stamp()}] ROTATED {name}: {fingerprint(old)} -> {fingerprint(new)} | "
                f"{describe_expiry(after_exp.get(name), before_exp.get(name))}")
        elif kind == "NEW":
            say(f"[{stamp()}] NEW {name}: {fingerprint(new)} | {describe_expiry(after_exp.get(name))}")
        else:
            say(f"[{stamp()}] REMOVED {name}: was {fingerprint(old)}")
    for name in sorted(dupes):
        say(f"[{stamp()}] WARNING: jar holds more than one {name} cookie")
