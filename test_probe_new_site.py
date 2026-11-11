#!/usr/bin/env python3
"""Tests for probe_new_site.py's dedicated-session cookie jar. Run with:
python3 -m unittest test_probe_new_site -v

Responses come from a scripted transport adapter mounted on a real
requests.Session, so requests' own Set-Cookie parsing and Cookie-header
generation run for real — only the network is replaced. Every test uses
a throwaway temp dir, never the real .bot_session.json.
"""
import contextlib
import http.client
import importlib.util
import io
import json
import os
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import requests

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


def make_response(request, status, headers, body):
    raw_headers = "".join(f"{k}: {v}\r\n" for k, v in headers).encode("latin-1") + b"\r\n"
    msg = http.client.parse_headers(io.BytesIO(raw_headers))
    raw = types.SimpleNamespace(
        _original_response=types.SimpleNamespace(msg=msg),
        close=lambda: None, release_conn=lambda: None, read=lambda *a, **k: b"",
    )
    resp = requests.Response()
    resp.status_code = status
    for k, v in headers:
        resp.headers[k] = v if k not in resp.headers else resp.headers[k] + ", " + v
    resp._content = body.encode("utf-8")
    resp._content_consumed = True
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
        status, headers, body = self.script.pop(0)
        return make_response(request, status, headers, body)

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

    def tearDown(self):
        self._tmp.cleanup()

    def run_probe(self, script, env=None, repeat=1, on_send=None):
        adapter = ScriptedAdapter(script, on_send=on_send)
        session = requests.Session()
        session.mount("https://", adapter)
        out = io.StringIO()
        with mock.patch.dict(os.environ, env or {}, clear=True), \
             mock.patch.object(probe.time, "sleep") as sleep, \
             contextlib.redirect_stdout(out):
            code = probe.run(repeat=repeat, interval=0, jar_path=self.jar, session=session)
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


class TestSeedingRules(ProbeTestCase):
    def test_missing_seed_env_sends_nothing_and_creates_nothing(self):
        code, out, adapter, _ = self.run_probe([ok()], env={"CS_AUTH": SEED["CS_AUTH"]})
        self.assertEqual(code, probe.EXIT_NO_SEED)
        self.assertEqual(adapter.sent, [])
        self.assertFalse(self.jar.exists())

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
