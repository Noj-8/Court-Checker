#!/usr/bin/env python3
"""Tests for probe_new_site.py's dedicated-session cookie jar. Run with:
python3 -m unittest test_probe_new_site -v

Responses come from a scripted transport adapter mounted on a real
requests.Session, so requests' own Set-Cookie parsing and Cookie-header
generation run for real — only the network is replaced. Every test uses
a throwaway temp dir, never the real .bot_session.json.
"""
import contextlib
import email.utils
import http.client
import importlib.util
import io
import json
import os
import stat
import tempfile
import time
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

import requests

try:
    from cryptography.fernet import Fernet
except ImportError:  # only the encrypted-jar tests need it
    Fernet = None

REPO = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("probe_new_site", REPO / "probe_new_site.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

SEED = {
    "CS_AUTH": "AUTH_seed_value_1111",
    "CS_DID": "DID_seed_value_2222",
    "CS_MEMBER": "MEMBER_old_value_3333",
}
MEMBER_NEW = "MEMBER_new_value_4444"

GRID_EN = """<html lang="en"><head><title>Book a court</title></head><body>
<a class="slot" href="#"><span class="slot-time">20:00</span><span class="slot-meta">2 free</span></a>
<a class="slot is-disabled" href="#"><span class="slot-time">21:00</span><span class="slot-meta">Full</span></a>
<a class="slot" href="#"><span class="slot-time">22:00</span><span class="slot-meta">1 free</span></a>
</body></html>"""

GRID_TH = """<html lang="th"><head><title>จองสนาม</title></head><body>
<a class="slot" href="#"><span class="slot-time">20:00</span><span class="slot-meta">ว่าง 2</span></a>
<a class="slot is-disabled" href="#"><span class="slot-time">21:00</span><span class="slot-meta">เต็ม</span></a>
<a class="slot" href="#"><span class="slot-time">22:00</span><span class="slot-meta">ว่าง ๑</span></a>
</body></html>"""


def make_response(request, status, headers, body, body_error=None):
    """body_error: an exception raised when the body is read (the headers,
    and so the Set-Cookie, have already arrived)."""
    raw_headers = "".join(f"{k}: {v}\r\n" for k, v in headers).encode("latin-1") + b"\r\n"
    msg = http.client.parse_headers(io.BytesIO(raw_headers))

    def read(*a, **k):
        if body_error is not None:
            raise body_error
        return b""

    raw = types.SimpleNamespace(
        _original_response=types.SimpleNamespace(msg=msg),
        close=lambda: None, release_conn=lambda: None, read=read,
    )
    resp = requests.Response()
    resp.status_code = status
    for k, v in headers:
        resp.headers[k] = v if k not in resp.headers else resp.headers[k] + ", " + v
    resp._content = False if body_error is not None else body.encode("utf-8")
    resp._content_consumed = body_error is None
    resp.encoding = "utf-8"
    resp.url = request.url
    resp.request = request
    resp.raw = raw
    requests.cookies.extract_cookies_to_jar(resp.cookies, request, raw)
    return resp


class ScriptedAdapter(requests.adapters.BaseAdapter):
    def __init__(self, script, on_send=None):
        super().__init__()
        self.script = list(script)
        self.sent = []
        self.on_send = on_send

    def send(self, request, **kwargs):
        self.sent.append({"method": request.method, "url": request.url,
                          "cookies": parse_cookie_header(request.headers.get("Cookie", ""))})
        if self.on_send:
            self.on_send(len(self.sent))
        if not self.script:
            raise AssertionError(f"unexpected extra request: {request.method} {request.url}")
        status, headers, body, *body_error = self.script.pop(0)
        return make_response(request, status, headers, body, *body_error)

    def close(self):
        pass


def parse_cookie_header(header):
    pairs = [p.partition("=") for p in header.split("; ") if p]
    return [(name, value) for name, _, value in pairs]


def ok(body=GRID_EN, set_cookies=()):
    return (200, [("Content-Type", "text/html; charset=utf-8"), ("Server", "Microsoft-IIS/10.0")]
            + [("Set-Cookie", c) for c in set_cookies], body)


def login_redirect(set_cookies=()):
    return (302, [("Location", f"{probe.BASE}/account/login?ReturnUrl=%2Fbooking"), ("Server", "Microsoft-IIS/10.0")]
            + [("Set-Cookie", c) for c in set_cookies], "")


class ProbeTestCase(unittest.TestCase):
    def setUp(self):
        probe._secrets.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.jar = self.dir / ".bot_session.json"
        self.enc = self.dir / "bot_session.enc"

    def tearDown(self):
        self._tmp.cleanup()

    def run_probe(self, script, env=None, repeat=1, on_send=None, check_account=False):
        adapter = ScriptedAdapter(script, on_send=on_send)
        session = requests.Session()
        session.mount("https://", adapter)
        out = io.StringIO()
        with mock.patch.dict(os.environ, env or {}, clear=True), \
             mock.patch.object(probe.time, "sleep") as sleep, \
             contextlib.redirect_stdout(out):
            code = probe.run(repeat=repeat, interval=0, jar_path=self.jar, session=session,
                             enc_path=self.enc, check_account_pages=check_account)
        return code, out.getvalue(), adapter, sleep

    def jar_cookies(self):
        return {c["name"]: c["value"] for c in json.loads(self.jar.read_text())["cookies"]}

    def assert_no_secret_leak(self, out, *extra):
        for value in list(SEED.values()) + list(extra):
            self.assertNotIn(value, out, "a cookie value was printed")

    def assert_no_temp_files(self):
        self.assertEqual(sorted(p.name for p in self.dir.glob(".bot_session.*.tmp")), [])


class TestRotationPersists(ProbeTestCase):
    """(a) A rotated cookie is persisted immediately, and the NEXT request
    sends the new value — never the superseded one."""

    def test_rotated_cookie_is_saved_before_the_next_request_and_sent_on_it(self):
        on_disk_at_request = {}

        def on_send(n):
            on_disk_at_request[n] = self.jar_cookies() if self.jar.exists() else None

        rotate = f"cs.member={MEMBER_NEW}; path=/; secure; httponly; samesite=lax"
        code, out, adapter, _ = self.run_probe([ok(set_cookies=[rotate]), ok()], env=SEED, on_send=on_send)

        self.assertEqual(code, probe.EXIT_OK)
        # Jar was seeded to disk before the very first request.
        self.assertEqual(on_disk_at_request[1]["cs.member"], SEED["CS_MEMBER"])
        # The rotation was on disk BEFORE request 2 went out, not saved at the end.
        self.assertEqual(on_disk_at_request[2]["cs.member"], MEMBER_NEW)

        first, second = adapter.sent[0]["cookies"], adapter.sent[1]["cookies"]
        self.assertIn(("cs.member", SEED["CS_MEMBER"]), first)
        self.assertEqual([v for n, v in second if n == "cs.member"], [MEMBER_NEW])

        self.assertEqual(self.jar_cookies()["cs.member"], MEMBER_NEW)
        self.assertEqual(stat.S_IMODE(self.jar.stat().st_mode), 0o600)
        self.assertIn(f"ROTATED cs.member: {probe.fingerprint(SEED['CS_MEMBER'])} -> {probe.fingerprint(MEMBER_NEW)}", out)
        self.assertIn("expires=session (no Expires/Max-Age)", out)
        self.assertIn("rotations=1", out)
        self.assert_no_secret_leak(out, MEMBER_NEW)
        self.assert_no_temp_files()

    def test_rotation_under_a_different_domain_replaces_rather_than_duplicates(self):
        # If the server reissues the cookie with Domain=, the old host-only
        # copy must not keep being sent alongside the new one.
        rotate = f"cs.member={MEMBER_NEW}; domain=.kegroup.co.th; path=/; secure; httponly"
        code, out, adapter, _ = self.run_probe([ok(set_cookies=[rotate]), ok()], env=SEED)

        self.assertEqual(code, probe.EXIT_OK)
        self.assertEqual([v for n, v in adapter.sent[1]["cookies"] if n == "cs.member"], [MEMBER_NEW])
        stored = [c for c in json.loads(self.jar.read_text())["cookies"] if c["name"] == "cs.member"]
        self.assertEqual(len(stored), 1)
        self.assertNotIn("WARNING: jar holds more than one", out)

    def test_read_only_and_culture_cookie_on_every_request(self):
        code, out, adapter, _ = self.run_probe([ok(), ok()], env=SEED)
        self.assertEqual(code, probe.EXIT_OK)
        for sent in adapter.sent:
            self.assertEqual(sent["method"], "GET")
            self.assertEqual(requests.utils.urlparse(sent["url"]).path, "/booking")
            self.assertIn((".AspNetCore.Culture", "c=en|uic=en"), sent["cookies"])
        self.assertNotIn(".AspNetCore.Culture", self.jar_cookies(), "culture cookie must not be stored in the jar")
        self.assertIn("lang=en thai=no", out)
        self.assertIn("open: 20:00(2) 22:00(1)", out)


class TestRotationExpiry(ProbeTestCase):
    """Each rotation logs the new cookie's expiry (never its value), and
    once a previous expiry is known, how far the rotation moved it."""

    def test_expires_logged_on_each_rotation_with_delta_once_known(self):
        # Relative to now, never hardcoded — a fixed date would eventually
        # pass and http.cookiejar would silently treat the cookie as deleted.
        now = int(time.time())
        exp1, exp2 = now + 30 * 86400, now + 31 * 86400
        second_value = "MEMBER_newer_value_5555"
        script = [
            ok(set_cookies=[f"cs.member={MEMBER_NEW}; expires={email.utils.formatdate(exp1, usegmt=True)}; path=/; secure"]),
            ok(set_cookies=[f"cs.member={second_value}; expires={email.utils.formatdate(exp2, usegmt=True)}; path=/; secure"]),
        ]
        code, out, adapter, _ = self.run_probe(script, env=SEED)
        self.assertEqual(code, probe.EXIT_OK)

        rotations = [line for line in out.splitlines() if "ROTATED cs.member" in line]
        self.assertEqual(len(rotations), 2)
        iso = lambda ts: datetime.fromtimestamp(ts, probe.TZ).isoformat(timespec="seconds")
        # First rotation replaces a seeded cookie: its old expiry was never
        # known, so no "vs previous" is claimed.
        self.assertIn(f"expires={iso(exp1)}", rotations[0])
        self.assertNotIn("vs previous", rotations[0])
        # Second rotation: lifetime moved forward by exactly a day.
        self.assertIn(f"expires={iso(exp2)}", rotations[1])
        self.assertIn("+1d 0h 0m vs previous", rotations[1])
        stored = {c["name"]: c["expires"] for c in json.loads(self.jar.read_text())["cookies"]}
        self.assertEqual(stored["cs.member"], exp2, "the new expiry must be persisted in the jar too")
        self.assert_no_secret_leak(out, MEMBER_NEW, second_value)

    def test_max_age_is_reported_as_an_expiry_time(self):
        code, out, adapter, _ = self.run_probe(
            [ok(set_cookies=[f"cs.member={MEMBER_NEW}; max-age=3600; path=/; secure"]), ok()], env=SEED)
        self.assertEqual(code, probe.EXIT_OK)
        rotation = next(line for line in out.splitlines() if "ROTATED cs.member" in line)
        self.assertRegex(rotation, r"expires=\S+ \(in (0h 59m|1h 0m)\)")


class TestExpiryIsTotal(unittest.TestCase):
    def test_sentinel_and_overflow_are_far_future(self):
        for value in (253402300799, 253402300799.0, 10 ** 20, float("inf"), float("nan")):
            with self.subTest(value=value):
                self.assertEqual(probe.describe_expiry(value), "expires=far future (9999-12-31 sentinel or beyond)")
        normal = time.time() + 3600
        self.assertNotIn("vs previous", probe.describe_expiry(normal, 253402300799),
                         "no delta against a far-future previous")
        self.assertIn("far future", probe.describe_expiry(253402300799, normal))


class TestFarFutureRotation(ProbeTestCase):
    def test_rotation_to_a_never_expiring_cookie_is_logged_not_a_crash(self):
        rotate = f"cs.member={MEMBER_NEW}; expires=Fri, 31 Dec 9999 23:59:59 GMT; path=/; secure"
        code, out, adapter, _ = self.run_probe([ok(set_cookies=[rotate]), ok()], env=SEED)
        self.assertEqual(code, probe.EXIT_OK)
        self.assertEqual(len(adapter.sent), 2)
        self.assertIn("ROTATED cs.member", out)
        self.assertIn("expires=far future", out)
        self.assertEqual(self.jar_cookies()["cs.member"], MEMBER_NEW)


class TestAtomicWrite(ProbeTestCase):
    """(b) A failed write leaves the previous jar file intact."""

    def _session_with(self, member):
        s = requests.Session()
        s.cookies.set_cookie(probe.create_cookie("cs.member", member, domain=probe.HOST, path="/", secure=True))
        return s

    def test_failure_during_fsync_leaves_old_file_intact(self):
        probe.save_jar(self.jar, self._session_with(SEED["CS_MEMBER"]))
        before = self.jar.read_bytes()
        with mock.patch.object(probe.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                probe.save_jar(self.jar, self._session_with(MEMBER_NEW))
        self.assertEqual(self.jar.read_bytes(), before)
        self.assert_no_temp_files()

    def test_failure_during_rename_leaves_old_file_intact(self):
        probe.save_jar(self.jar, self._session_with(SEED["CS_MEMBER"]))
        before = self.jar.read_bytes()
        with mock.patch.object(probe.os, "replace", side_effect=OSError("rename failed")):
            with self.assertRaises(OSError):
                probe.save_jar(self.jar, self._session_with(MEMBER_NEW))
        self.assertEqual(self.jar.read_bytes(), before)
        self.assert_no_temp_files()

    def test_run_stops_when_a_rotation_cannot_be_saved(self):
        rotate = f"cs.member={MEMBER_NEW}; path=/; secure"
        real_replace = os.replace
        calls = {"n": 0}

        def replace_fails_after_seed(src, dst):
            calls["n"] += 1
            if calls["n"] > 1:  # seeding succeeds; the post-response save fails
                raise OSError("rename failed")
            return real_replace(src, dst)

        with mock.patch.object(probe.os, "replace", side_effect=replace_fails_after_seed):
            code, out, adapter, _ = self.run_probe([ok(set_cookies=[rotate]), ok()], env=SEED)

        self.assertEqual(code, probe.EXIT_JAR)
        self.assertEqual(len(adapter.sent), 1, "must not send another request after a failed save")
        self.assertEqual(self.jar_cookies()["cs.member"], SEED["CS_MEMBER"])
        self.assertIn("NOT saved", out)
        self.assert_no_secret_leak(out, MEMBER_NEW)
        self.assert_no_temp_files()


class TestLanguageIndependentParsing(unittest.TestCase):
    """(c) Thai and English pages parse to the same availability."""

    @staticmethod
    def availability(html):
        return [(s["time"], s["available"], s["free"]) for s in probe.parse_page(html)["slots"]]

    def test_thai_and_english_parse_the_same(self):
        expected = [("20:00", True, 2), ("21:00", False, None), ("22:00", True, 1)]
        self.assertEqual(self.availability(GRID_EN), expected)
        self.assertEqual(self.availability(GRID_TH), expected)  # includes Thai digit ๑ -> 1

    def test_language_is_reported_not_relied_on(self):
        en, th = probe.parse_page(GRID_EN), probe.parse_page(GRID_TH)
        self.assertEqual((en["lang"], en["thai"]), ("en", False))
        self.assertEqual((th["lang"], th["thai"]), ("th", True))

    def test_availability_comes_from_the_class_not_the_text(self):
        html = ('<a class="slot is-disabled"><span class="slot-time">18:00</span>'
                '<span class="slot-meta">ว่าง 3</span></a>'
                '<a class="slot"><span class="slot-time">19:00</span><span class="slot-meta">Open</span></a>')
        self.assertEqual(self.availability(html), [("18:00", False, 3), ("19:00", True, None)])


class TestStopsOnLoginRedirect(ProbeTestCase):
    """(d) Stops immediately on the first login redirect; never retries."""

    def test_stops_on_first_login_redirect_and_persists_the_deletion(self):
        expire_auth = "cs_auth=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/; secure"
        script = [ok(), ok(), login_redirect(set_cookies=[expire_auth]), ok(), ok(), ok()]
        code, out, adapter, sleep = self.run_probe(script, env=SEED, repeat=3)

        self.assertEqual(code, probe.EXIT_LOGIN)
        self.assertEqual(len(adapter.sent), 3, "round 2 LOC002 and round 3 must never be requested")
        self.assertEqual(sleep.call_count, 1)
        self.assertIn("STOPPED at round 2/3, LOC001: login redirect", out)
        self.assertNotIn("cs_auth", self.jar_cookies(), "the server's deletion must be persisted")
        self.assertIn("REMOVED cs_auth", out)
        self.assert_no_secret_leak(out)


ACCOUNT_REF = "SPORTS-202611219999"
ACCOUNT_PAGE = (f'<html><body><h1>My bookings</h1><a class="bk-row" href="/bookings/{ACCOUNT_REF}">'
                'Private Member Name</a></body></html>')


def account_ok():
    return ok(body=ACCOUNT_PAGE)


def account_login_redirect():
    return (302, [("Location", f"{probe.BASE}/account/login?ReturnUrl=%2Fbookings")], "")


class TestCheckAccount(ProbeTestCase):
    """--check-account: after the first round's grids, GET /bookings then
    /bookings?tab=pending and print status + ok/login redirect — never page
    text or refs; stop at the first login."""

    def account_lines(self, out):
        return [l.split("] ", 1)[1] for l in out.splitlines() if "] account " in l]

    def requests_made(self, adapter):
        return [(r["method"], urlsplit(r["url"]).path, urlsplit(r["url"]).query) for r in adapter.sent]

    def test_both_ok(self):
        code, out, adapter, _ = self.run_probe([ok(), ok(), account_ok(), account_ok()], env=SEED,
                                               check_account=True)
        self.assertEqual(code, probe.EXIT_OK)
        self.assertEqual(self.requests_made(adapter)[2:], [("GET", "/bookings", ""), ("GET", "/bookings", "tab=pending")])
        self.assertEqual(self.account_lines(out), ["account /bookings: status=200 ok",
                                                   "account /bookings?tab=pending: status=200 ok"])
        self.assertIn("Account pages: both ok.", out)
        self.assertNotIn(ACCOUNT_REF, out)
        self.assertNotIn("Private Member Name", out)
        self.assertNotIn("My bookings", out)
        self.assert_no_secret_leak(out)

    def test_login_redirect_on_bookings_stops_before_the_pending_tab(self):
        code, out, adapter, _ = self.run_probe([ok(), ok(), account_login_redirect()], env=SEED,
                                               repeat=2, check_account=True)
        self.assertEqual(code, probe.EXIT_LOGIN)
        self.assertEqual(len(adapter.sent), 3, "no pending tab, no round 2")
        self.assertEqual(self.account_lines(out), ["account /bookings: status=302 login redirect"])
        self.assertIn("STOPPED at account page /bookings: login redirect.", out)
        self.assertIn("is cs_auth there?", out)
        self.assertNotIn("ReturnUrl", out, "the Location isn't printed")
        self.assert_no_secret_leak(out)

    def test_login_redirect_on_the_pending_tab(self):
        code, out, adapter, _ = self.run_probe([ok(), ok(), account_ok(), account_login_redirect()], env=SEED,
                                               check_account=True)
        self.assertEqual(code, probe.EXIT_LOGIN)
        self.assertEqual(self.account_lines(out), ["account /bookings: status=200 ok",
                                                   "account /bookings?tab=pending: status=302 login redirect"])

    def test_login_page_served_as_200(self):
        login = ok(body='<html><body><form action="/account/login"><input type="password"></form></body></html>')
        code, out, adapter, _ = self.run_probe([ok(), ok(), login], env=SEED, check_account=True)
        self.assertEqual(code, probe.EXIT_LOGIN)
        self.assertEqual(self.account_lines(out), ["account /bookings: status=200 login page (200: login form)"])

    def test_not_ok_status_is_reported_and_the_other_page_still_checked(self):
        err = (500, [("Content-Type", "text/html")], f"<html>error for {ACCOUNT_REF}</html>")
        code, out, adapter, _ = self.run_probe([ok(), ok(), err, account_ok()], env=SEED, check_account=True)
        self.assertEqual(code, probe.EXIT_NO_GRID)
        self.assertEqual(self.account_lines(out), ["account /bookings: status=500 not ok (unexpected status)",
                                                   "account /bookings?tab=pending: status=200 ok"])
        self.assertIn("Account pages: at least one was NOT ok", out)
        self.assertNotIn(ACCOUNT_REF, out)

    def test_network_error(self):
        code, out, adapter, _ = self.run_probe([ok(), ok(), (200, [], "", requests.ConnectionError("boom")),
                                                account_ok()], env=SEED, check_account=True)
        self.assertEqual(code, probe.EXIT_NO_GRID)
        self.assertEqual(self.account_lines(out)[0], "account /bookings: status=ERR network error (ConnectionError)")

    def test_runs_once_after_round_one_only(self):
        code, out, adapter, _ = self.run_probe([ok(), ok(), account_ok(), account_ok(), ok(), ok()], env=SEED,
                                               repeat=2, check_account=True)
        self.assertEqual(code, probe.EXIT_OK)
        self.assertEqual([p for _, p, _ in self.requests_made(adapter)],
                         ["/booking", "/booking", "/bookings", "/bookings", "/booking", "/booking"])

    def test_skipped_when_the_grid_redirects(self):
        code, out, adapter, _ = self.run_probe([login_redirect()], env=SEED, check_account=True)
        self.assertEqual(code, probe.EXIT_LOGIN)
        self.assertEqual(len(adapter.sent), 1)
        self.assertEqual(self.account_lines(out), [])

    def test_off_by_default_and_only_gets(self):
        code, out, adapter, _ = self.run_probe([ok(), ok()], env=SEED)
        self.assertEqual(code, probe.EXIT_OK)
        self.assertEqual({p for _, p, _ in self.requests_made(adapter)}, {"/booking"})
        code, out, adapter, _ = self.run_probe([ok(), ok(), account_ok(), account_ok()], env=SEED, check_account=True)
        self.assertEqual({m for m, _, _ in self.requests_made(adapter)}, {"GET"})

    def test_jar_save_failure_on_an_account_page_stops(self):
        real, calls = probe.bot_session.save_jar, {"n": 0}

        def save(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 4:   # seed, grid, grid, then /bookings
                raise OSError("disk full")
            return real(*a, **kw)
        with mock.patch.object(probe.bot_session, "save_jar", side_effect=save):
            code, out, adapter, _ = self.run_probe([ok(), ok(), account_ok(), account_ok()], env=SEED,
                                                   check_account=True)
        self.assertEqual(code, probe.EXIT_JAR)
        self.assertEqual(len(adapter.sent), 3, "nothing after the failed save")
        self.assertIn("account pages: FAILED TO WRITE", out)

    def test_cli_flag(self):
        with mock.patch.object(probe, "run", return_value=0) as run:
            probe.main(["--check-account"])
            self.assertTrue(run.call_args.kwargs["check_account_pages"])
            probe.main([])
            self.assertFalse(run.call_args.kwargs["check_account_pages"])

    def test_works_without_cs_auth_seeded(self):
        env = {"CS_DID": SEED["CS_DID"], "CS_MEMBER": SEED["CS_MEMBER"]}
        code, out, adapter, _ = self.run_probe([ok(), ok(), account_login_redirect()], env=env, check_account=True)
        self.assertEqual(code, probe.EXIT_LOGIN)
        self.assertIn("cs_auth absent (not seeded)", out)
        self.assertNotIn("cs_auth", [n for n, _ in adapter.sent[2]["cookies"]])


class TestSeedingRules(ProbeTestCase):
    def test_missing_required_seed_env_sends_nothing_and_creates_nothing(self):
        for env, should_name in [({"CS_AUTH": SEED["CS_AUTH"]}, ["CS_DID", "CS_MEMBER"]),
                                 ({"CS_AUTH": SEED["CS_AUTH"], "CS_DID": SEED["CS_DID"]}, ["CS_MEMBER"]),
                                 ({"CS_AUTH": SEED["CS_AUTH"], "CS_MEMBER": SEED["CS_MEMBER"]}, ["CS_DID"])]:
            with self.subTest(env=sorted(env)):
                code, out, adapter, _ = self.run_probe([ok()], env=env)
                self.assertEqual(code, probe.EXIT_NO_SEED)
                self.assertEqual(adapter.sent, [])
                self.assertFalse(self.jar.exists())
                error = next(line for line in out.splitlines() if line.startswith("ERROR"))
                for name in should_name:
                    self.assertIn(name, error)
                self.assertNotIn("CS_AUTH", error, "CS_AUTH is optional and must never be reported missing")

    def test_cs_auth_is_optional(self):
        env = {"CS_DID": SEED["CS_DID"], "CS_MEMBER": SEED["CS_MEMBER"]}
        code, out, adapter, _ = self.run_probe([ok(), ok()], env=env)
        self.assertEqual(code, probe.EXIT_OK)
        self.assertIn("cs_auth absent (not seeded)", out)
        self.assertNotIn("cs_auth", self.jar_cookies())
        self.assertEqual(set(self.jar_cookies()), {"cs.did", "cs.member"})
        sent_names = [n for n, _ in adapter.sent[0]["cookies"]]
        self.assertNotIn("cs_auth", sent_names)
        self.assertIn("cs.did", sent_names)
        self.assertIn("cs.member", sent_names)
        self.assert_no_secret_leak(out)

    def test_cs_auth_seeded_when_present(self):
        code, out, adapter, _ = self.run_probe([ok(), ok()], env=SEED)
        self.assertEqual(code, probe.EXIT_OK)
        self.assertNotIn("absent (not seeded)", out)
        self.assertEqual(self.jar_cookies()["cs_auth"], SEED["CS_AUTH"])

    def test_existing_jar_wins_and_env_vars_are_ignored(self):
        s = requests.Session()
        for name, value in [("cs_auth", "AUTH_from_jar_5555"), ("cs.did", "DID_from_jar_6666"),
                            ("cs.member", "MEMBER_from_jar_7777")]:
            s.cookies.set_cookie(probe.create_cookie(name, value, domain=probe.HOST, path="/", secure=True))
        probe.save_jar(self.jar, s)

        code, out, adapter, _ = self.run_probe([ok(), ok()], env=SEED)
        self.assertEqual(code, probe.EXIT_OK)
        self.assertIn(("cs.member", "MEMBER_from_jar_7777"), adapter.sent[0]["cookies"])
        self.assertNotIn(("cs.member", SEED["CS_MEMBER"]), adapter.sent[0]["cookies"])
        self.assertIn("IGNORED", out)
        self.assert_no_secret_leak(out, "AUTH_from_jar_5555", "MEMBER_from_jar_7777")

    def test_unreadable_jar_never_falls_back_to_env_vars(self):
        self.jar.write_text("{not json")
        code, out, adapter, _ = self.run_probe([ok()], env=SEED)
        self.assertEqual(code, probe.EXIT_JAR)
        self.assertEqual(adapter.sent, [])
        self.assertEqual(self.jar.read_text(), "{not json")


def session_with(cookies):
    s = requests.Session()
    for name, value in cookies.items():
        s.cookies.set_cookie(probe.create_cookie(name, value, domain=probe.HOST, path="/", secure=True))
    return s


SEEDED = {"cs.did": SEED["CS_DID"], "cs.member": SEED["CS_MEMBER"]}
ROTATE = f"cs.member={MEMBER_NEW}; path=/; secure; httponly"


@unittest.skipIf(Fernet is None, "cryptography not installed")
class TestEncryptedJar(ProbeTestCase):
    """BOT_JAR_KEY set: the jar is bot_session.enc, ciphertext only."""

    def setUp(self):
        super().setUp()
        self.key = Fernet.generate_key().decode()

    def enc_cookies(self, key=None):
        s = requests.Session()
        probe.load_jar(self.enc, s, Fernet((key or self.key).encode()))
        return {c.name: c.value for c in s.cookies}

    def test_round_trip_and_rotation_persist_encrypted(self):
        probe.save_jar(self.enc, session_with(SEEDED), Fernet(self.key.encode()))
        raw = self.enc.read_bytes()
        for needle in list(SEEDED.values()) + ["cs.member", "cookies"]:
            self.assertNotIn(needle.encode(), raw)
        self.assertEqual(self.enc_cookies(), SEEDED)

        code, out, adapter, _ = self.run_probe([ok(set_cookies=[ROTATE]), ok()],
                                               env={"BOT_JAR_KEY": self.key})
        self.assertEqual(code, probe.EXIT_OK)
        self.assertIn(("cs.member", MEMBER_NEW), adapter.sent[1]["cookies"])
        self.assertEqual(self.enc_cookies()["cs.member"], MEMBER_NEW)
        self.assertIn(f"BOT_JAR_KEY set ({probe.fingerprint(self.key)})", out)
        self.assertIn("ROTATED cs.member", out)
        self.assert_no_secret_leak(out, MEMBER_NEW, self.key)
        self.assertFalse(self.jar.exists())
        self.assert_no_temp_files()

    def test_wrong_or_invalid_key_fails_cleanly_without_overwriting(self):
        probe.save_jar(self.enc, session_with(SEEDED), Fernet(self.key.encode()))
        before = self.enc.read_bytes()
        for label, key in [("wrong key", Fernet.generate_key().decode()), ("not a key", "not-a-fernet-key")]:
            with self.subTest(label):
                code, out, adapter, _ = self.run_probe([ok()], env={**SEED, "BOT_JAR_KEY": key})
                self.assertEqual(code, probe.EXIT_JAR)
                self.assertEqual(adapter.sent, [], "no request may be sent without a readable jar")
                self.assertEqual(self.enc.read_bytes(), before)
                self.assertFalse(self.jar.exists(), "must not fall back to seeding a plaintext jar")
                self.assertIn("BOT_JAR_KEY", out)
                self.assert_no_secret_leak(out, key)
                self.assert_no_temp_files()

    def test_no_plaintext_ever_written_when_key_is_set(self):
        written = []
        real_fdopen = os.fdopen

        class Recorder:
            def __init__(self, f):
                self._f = f

            def write(self, data):
                written.append(bytes(data))
                return self._f.write(data)

            def __getattr__(self, name):
                return getattr(self._f, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return self._f.__exit__(*exc)

        with mock.patch.object(probe.os, "fdopen", lambda *a, **k: Recorder(real_fdopen(*a, **k))):
            # Seeds from env (write 1), then a rotation (write 2), then no change (no write).
            code, out, _, _ = self.run_probe([ok(set_cookies=[ROTATE]), ok()],
                                             env={**SEED, "BOT_JAR_KEY": self.key})
        self.assertEqual(code, probe.EXIT_OK)
        self.assertEqual(len(written), 2, "expected exactly the seed write and the rotation write")
        for data in written:
            for needle in list(SEED.values()) + [MEMBER_NEW, "cs.member", '"cookies"']:
                self.assertNotIn(needle.encode(), data, "plaintext reached the disk")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["bot_session.enc"])
        self.assertEqual(self.enc_cookies()["cs.member"], MEMBER_NEW)

    def test_unchanged_cookies_leave_the_file_byte_identical(self):
        # So "bot_session.enc changed in git" means "a cookie changed".
        probe.save_jar(self.enc, session_with(SEEDED), Fernet(self.key.encode()))
        before = self.enc.read_bytes()
        code, _, _, _ = self.run_probe([ok(), ok()], env={"BOT_JAR_KEY": self.key})
        self.assertEqual(code, probe.EXIT_OK)
        self.assertEqual(self.enc.read_bytes(), before)

    def test_actions_without_key_refuses_and_writes_nothing(self):
        code, out, adapter, _ = self.run_probe([ok()], env={**SEED, "GITHUB_ACTIONS": "true"})
        self.assertEqual(code, probe.EXIT_JAR)
        self.assertEqual(adapter.sent, [])
        self.assertEqual(list(self.dir.iterdir()), [])


class TestHandedOff(ProbeTestCase):
    def setUp(self):
        super().setUp()
        self.handed = self.dir / ".bot_session.json.handed-off-20261112-230000"
        self.handed.write_text("{}")

    def test_handed_off_file_blocks_the_mac_run(self):
        code, out, adapter, _ = self.run_probe([ok()], env=SEED)
        self.assertEqual(code, probe.EXIT_HANDED_OFF)
        self.assertEqual(adapter.sent, [])
        self.assertFalse(self.jar.exists(), "must not seed a new jar over a handed-off session")
        self.assertIn("handed off", out)
        self.assertIn(self.handed.name, out)

    def test_a_fresh_jar_beside_it_runs_normally(self):
        probe.save_jar(self.jar, session_with(SEEDED))
        code, _, adapter, _ = self.run_probe([ok(), ok()], env={})
        self.assertEqual(code, probe.EXIT_OK)
        self.assertEqual(len(adapter.sent), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
