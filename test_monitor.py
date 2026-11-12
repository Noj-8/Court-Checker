#!/usr/bin/env python3
"""Tests for monitor.py on the new booking site (notify-only). Run with:
python3 -m unittest test_monitor -v

Responses come from the same scripted transport adapter as the probe tests,
mounted on a real requests.Session, so bot_session's real cookie handling
and jar writes run — only the network is replaced. Every date is relative
to today in Bangkok, so nothing here goes stale. Every test uses a
throwaway temp dir for config.json, state.json and the jar.
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import requests

import booking_page
import bot_session
import monitor
from test_probe_new_site import GRID_EN, GRID_TH, MEMBER_NEW, ScriptedAdapter, login_redirect, ok

try:
    from cryptography.fernet import Fernet
except ImportError:
    Fernet = None

COOKIES = {"cs.did": "DID_monitor_value_1111", "cs.member": "MEMBER_monitor_value_2222"}
EMAIL_ENV = {"EMAIL_USER": "bot@example.com", "EMAIL_APP_PASSWORD": "app-password"}

LOCKED_EN = """<html lang="en"><head><title>Book a court</title></head><body>
<div class="court-map is-locked"><p>Booking opens 21:00</p></div>
</body></html>"""
LOCKED_TH = """<html lang="th"><head><title>จองสนาม</title></head><body>
<div class="court-map is-locked"><p>เปิดจอง 21:00 น.</p></div>
</body></html>"""
NO_GRID = "<html><body><p>Something went wrong</p></body></html>"
# What the real site serves for a normal OPEN date with time=0: the court map
# is locked (no time selected) AND the full 18-slot grid (06:00-23:00) is there.
REAL_OPEN_DAY = (
    '<html lang="en"><head><title>Book a court</title></head><body>'
    '<div class="court-map is-slate is-locked"><p>Pick a time to see courts</p></div>'
    + "".join(
        f'<a class="slot{"" if h in (20, 22) else " is-disabled"}" href="#">'
        f'<span class="slot-time">{h:02d}:00</span>'
        f'<span class="slot-meta">{"2 free" if h == 20 else "1 free" if h == 22 else "Full"}</span></a>'
        for h in range(6, 24))
    + "</body></html>")
# No slots, the same locked court map, and no opens-at marker: unexpected.
LOCKED_MAP_NO_GRID = ('<html><body><div class="court-map is-slate is-locked">'
                      '<p>Pick a time to see courts</p></div></body></html>')


def day(offset):
    return (datetime.now(monitor.TZ).date() + timedelta(days=offset)).isoformat()


def grid(*slots, lang="en"):
    """slots: (time, meta, disabled) tuples."""
    rows = "\n".join(
        f'<a class="slot{" is-disabled" if disabled else ""}" href="#">'
        f'<span class="slot-time">{t}</span><span class="slot-meta">{meta}</span></a>'
        for t, meta, disabled in slots)
    return f'<html lang="{lang}"><head><title>Book</title></head><body>{rows}</body></html>'


def target(date, times, locations=("LOC001",), name="Fri"):
    return {"name": name, "date": date, "locations": list(locations),
            "times": [{"time": t, "autoBook": star} for t, star in times]}


class MonitorTestCase(unittest.TestCase):
    def setUp(self):
        bot_session._secrets.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.jar = self.dir / ".bot_session.json"
        self.enc = self.dir / "bot_session.enc"
        self.config_file = self.dir / "config.json"
        self.state_file = self.dir / "state.json"
        self.write_jar(COOKIES)
        self.date = day(1)
        self.write_config([target(self.date, [("20:00", False), ("22:00", False)])])

    def tearDown(self):
        self._tmp.cleanup()

    def write_jar(self, cookies, path=None, fernet=None):
        s = requests.Session()
        for name, value in cookies.items():
            s.cookies.set_cookie(bot_session.create_cookie(name, value, domain=bot_session.HOST, path="/", secure=True))
        bot_session.save_jar(path or self.jar, s, fernet)

    def write_config(self, targets):
        self.config_file.write_text(json.dumps({"targets": targets}))

    def state(self):
        return json.loads(self.state_file.read_text()) if self.state_file.exists() else {}

    def run_monitor(self, script, iterations=1, env=None, argv=None, send_fails=False):
        """Runs monitor.run() (or main(argv)) end to end. Any attempt to send
        a non-GET request — through the session or the requests module —
        fails the test."""
        adapter = ScriptedAdapter(script)
        session = requests.Session()
        session.mount("https://", adapter)
        sent = []

        def fake_send(subject, body, *_):
            if send_fails:
                raise OSError("smtp down")
            sent.append({"subject": subject, "body": body})

        def no_post(*a, **k):
            raise AssertionError("monitor must never send a POST/PUT/DELETE")

        out = io.StringIO()
        with mock.patch.dict(os.environ, {**EMAIL_ENV, **(env or {})}, clear=True), \
             mock.patch.object(monitor, "CONFIG_FILE", self.config_file), \
             mock.patch.object(monitor, "STATE_FILE", self.state_file), \
             mock.patch.object(monitor, "send_email", side_effect=fake_send), \
             mock.patch.object(monitor.time, "sleep"), \
             mock.patch.object(requests, "post", side_effect=no_post), \
             mock.patch.object(requests, "put", side_effect=no_post), \
             mock.patch.object(requests.Session, "post", side_effect=no_post), \
             mock.patch.object(bot_session, "ENC_FILE", self.enc), \
             mock.patch.object(bot_session, "JAR_FILE", self.jar), \
             contextlib.redirect_stdout(out):
            if argv is not None:
                code = monitor.main(argv)
            else:
                code = monitor.run(iterations=iterations, interval=0, jar_path=self.jar,
                                   enc_path=self.enc, session=session)
        self.assertTrue(all(r["method"] == "GET" for r in adapter.sent), adapter.sent)
        return code, out.getvalue(), adapter, sent


class TestGridParsing(unittest.TestCase):
    def test_thai_and_english_grids_give_the_same_availability(self):
        en, th = booking_page.parse_page(GRID_EN), booking_page.parse_page(GRID_TH)
        pick = lambda p: [(booking_page.slot_time(s["time"]), s["available"], s["free"]) for s in p["slots"]]
        self.assertEqual(pick(en), [("20:00", True, 2), ("21:00", False, None), ("22:00", True, 1)])
        self.assertEqual(pick(th), pick(en))
        self.assertFalse(en["locked"] or th["locked"])

    def test_not_open_yet_needs_no_slots_and_an_opens_at_marker(self):
        for html in (LOCKED_EN, LOCKED_TH):
            with self.subTest(lang=html[12:14]):
                page = booking_page.parse_page(html)
                self.assertTrue(page["not_open_yet"])
                self.assertEqual(page["opens_at"], "21:00")
                self.assertEqual(page["slots"], [])

    def test_locked_court_map_never_overrides_a_parsed_grid(self):
        page = booking_page.parse_page(REAL_OPEN_DAY)
        self.assertTrue(page["locked"], "fixture must carry the real locked court map")
        self.assertFalse(page["not_open_yet"])
        self.assertEqual(len(page["slots"]), 18)
        self.assertEqual([booking_page.slot_time(s["time"]) for s in page["slots"] if s["available"]],
                         ["20:00", "22:00"])

    def test_locked_court_map_without_marker_is_not_not_open_yet(self):
        page = booking_page.parse_page(LOCKED_MAP_NO_GRID)
        self.assertTrue(page["locked"])
        self.assertFalse(page["not_open_yet"])
        self.assertIsNone(page["opens_at"])

    def test_times_that_are_not_an_opens_marker_are_ignored(self):
        page = booking_page.parse_page("<p>Last updated 21:00</p><script>var open = '10:00';</script>")
        self.assertFalse(page["not_open_yet"])

    def test_is_locked_on_a_single_slot_only_closes_that_slot(self):
        page = booking_page.parse_page(
            '<a class="slot is-locked"><span class="slot-time">20:00</span><span class="slot-meta">1</span></a>'
            '<a class="slot"><span class="slot-time">21:00</span><span class="slot-meta">1</span></a>')
        self.assertFalse(page["locked"])
        self.assertEqual([s["available"] for s in page["slots"]], [False, True])

    def test_slot_time_normalisation(self):
        for text, want in [("20:00", "20:00"), ("8:00", "08:00"), ("20.00", "20:00"),
                           ("20:00 - 21:00", "20:00"), ("Full", None)]:
            self.assertEqual(booking_page.slot_time(text), want, text)


class TestCheckPass(MonitorTestCase):
    def test_real_open_day_with_locked_court_map_reports_open_slots(self):
        code, out, _, sent = self.run_monitor([ok(REAL_OPEN_DAY)])
        self.assertEqual(code, monitor.EXIT_OK)
        self.assertNotIn("not open yet", out)
        self.assertEqual(len(sent), 1)
        self.assertIn(f"{self.date} 20:00 — Crystal Sports (2 free)", sent[0]["body"])
        self.assertIn(f"{self.date} 22:00 — Crystal Sports (1 free)", sent[0]["body"])
        self.assertEqual(self.state()["currently_open_count"], 2)

    def test_unexpected_page_is_logged_and_skipped_not_a_session_failure(self):
        self.write_config([target(self.date, [("20:00", False)], locations=("LOC001", "LOC002"))])
        self.state_file.write_text(json.dumps({"known_open": {f"{self.date}|LOC001|any|20:00": True}}))
        code, out, adapter, sent = self.run_monitor([ok(LOCKED_MAP_NO_GRID), ok(REAL_OPEN_DAY)])
        self.assertEqual(code, monitor.EXIT_OK)
        self.assertEqual(len(adapter.sent), 2, "the run carries on to the next page")
        self.assertIn("unexpected page — no slot grid and no opens-at marker", out)
        self.assertNotIn("session_alert", self.state())
        self.assertIn(f"{self.date}|LOC001|any|20:00", self.state()["known_open"], "unexpected page keeps its entries")
        self.assertEqual(len(sent), 1)
        self.assertIn(f"{self.date} 20:00 — Crystal Sports G (2 free)", sent[0]["body"])
        self.assertNotIn("— Crystal Sports (", sent[0]["body"], "no slot line from the unexpected LOC001 page")

    def test_open_slot_emails_once_with_free_count_and_link(self):
        code, out, adapter, sent = self.run_monitor([ok(GRID_EN)])
        self.assertEqual(code, monitor.EXIT_OK)
        self.assertEqual(len(sent), 1)
        self.assertIn("2 tennis slot(s) open", sent[0]["subject"])
        self.assertIn(f"{self.date} 20:00 — Crystal Sports (2 free)", sent[0]["body"])
        self.assertIn(f"{self.date} 22:00 — Crystal Sports (1 free)", sent[0]["body"])
        self.assertIn(f"/booking?date={self.date}&loc=LOC001", sent[0]["body"])
        self.assertNotIn("21:00", sent[0]["body"], "is-disabled slot must not be reported")
        state = self.state()
        self.assertEqual(set(state["known_open"]), {f"{self.date}|LOC001|any|20:00", f"{self.date}|LOC001|any|22:00"})
        self.assertEqual(state["currently_open_count"], 2)
        self.assertIn("last_check", state)
        self.assertEqual(adapter.sent[0]["url"], f"{bot_session.BASE}/booking?date={self.date}&loc=LOC001&time=0&sel=")

    def test_dedupe_across_runs_and_realert_after_closing(self):
        self.run_monitor([ok(GRID_EN)])
        _, _, _, sent = self.run_monitor([ok(GRID_TH)])  # same availability, other language
        self.assertEqual(sent, [], "an already-known open slot must not email again")
        _, _, _, sent = self.run_monitor([ok(grid(("20:00", "Full", True), ("22:00", "1 free", False)))])
        self.assertEqual(sent, [])
        self.assertNotIn(f"{self.date}|LOC001|any|20:00", self.state()["known_open"])
        _, _, _, sent = self.run_monitor([ok(GRID_EN)])
        self.assertEqual(len(sent), 1)
        self.assertIn("1 tennis slot(s) open", sent[0]["subject"])
        self.assertIn("20:00", sent[0]["body"])

    def test_locked_date_is_not_open_yet_not_an_error(self):
        self.state_file.write_text(json.dumps({"known_open": {f"{self.date}|LOC001|any|20:00": True}}))
        for page in (LOCKED_EN, LOCKED_TH):
            with self.subTest(lang=page[12:14]):
                code, out, adapter, sent = self.run_monitor([ok(page)], iterations=1)
                self.assertEqual(code, monitor.EXIT_OK)
                self.assertEqual(sent, [])
                self.assertIn("not open yet (opens 21:00)", out)
                self.assertNotIn("session_alert", self.state())
                self.assertEqual(self.state()["known_open"], {})

    def test_zero_free_unwatched_times_and_legacy_config_strings(self):
        cfg = {"targets": [{"name": "Old", "date": self.date, "locations": ["LOC002"], "times": ["20:00", "21:00"]}]}
        self.config_file.write_text(json.dumps(cfg))
        page = grid(("20:00", "0 free", False), ("21:00", "ว่าง ๓", False), ("23:00", "4 free", False), lang="th")
        _, _, adapter, sent = self.run_monitor([ok(page)])
        self.assertEqual(len(sent), 1)
        self.assertIn(f"{self.date} 21:00 — Crystal Sports G (3 free)", sent[0]["body"])
        self.assertNotIn("20:00", sent[0]["body"])
        self.assertNotIn("23:00", sent[0]["body"])
        self.assertIn("loc=LOC002", adapter.sent[0]["url"])

    def test_page_errors_keep_known_open_and_old_format_keys_are_pruned(self):
        self.write_config([target(self.date, [("20:00", False)], locations=("LOC001", "LOC002"))])
        self.state_file.write_text(json.dumps({"known_open": {
            f"{self.date}|LOC001|North-2|20:00": True,   # old per-court key, page read OK
            f"{self.date}|LOC002|any|20:00": True,       # page errors -> must be kept
            f"{day(-1)}|LOC001|any|20:00": True,         # past date -> pruned
        }, "last_session_alert": 1794330325.6}))
        code, out, _, sent = self.run_monitor([ok(grid(("20:00", "Full", True))), ok(NO_GRID)])
        self.assertEqual(code, monitor.EXIT_OK)
        self.assertIn("unexpected page", out)
        state = self.state()
        self.assertEqual(state["known_open"], {f"{self.date}|LOC002|any|20:00": True})
        self.assertNotIn("last_session_alert", state, "PHPSESSID-era field must be dropped")

    def test_past_targets_send_nothing(self):
        self.write_config([target(day(-1), [("20:00", False)])])
        code, _, adapter, sent = self.run_monitor([])
        self.assertEqual((code, adapter.sent, sent), (monitor.EXIT_OK, [], []))


class TestNoPost(MonitorTestCase):
    def test_full_run_with_starred_open_slot_never_posts(self):
        self.write_config([target(self.date, [("20:00", True), ("22:00", False)], locations=("LOC001", "LOC002"))])
        script = [ok(GRID_EN), ok(GRID_TH), ok(GRID_EN), ok(GRID_TH)]
        code, out, adapter, sent = self.run_monitor(script, iterations=2)
        self.assertEqual(code, monitor.EXIT_OK)
        self.assertEqual(len(adapter.sent), 4)
        self.assertEqual({r["method"] for r in adapter.sent}, {"GET"})
        self.assertTrue(all(r["url"].startswith(f"{bot_session.BASE}/booking?") for r in adapter.sent))
        self.assertFalse(any(f in r["url"] for r in adapter.sent for f in bot_session.FORBIDDEN_PATH_FRAGMENTS))
        self.assertEqual(len(sent), 1, "one email for the pass that found the openings, none on the repeat")
        body = sent[0]["body"]
        self.assertEqual(body.count(monitor.AUTOBOOK_PAUSED_NOTE), 2, "once under each location's starred 20:00")
        self.assertIn("20:00 — Crystal Sports (2 free)  ★", body)
        self.assertNotIn("autobook_attempts", self.state())

    def test_autobook_cannot_be_switched_on_without_code(self):
        with mock.patch.object(monitor, "AUTOBOOK_ENABLED", True):
            code, _, adapter, _ = self.run_monitor([ok()])
        self.assertEqual((code, adapter.sent), (monitor.EXIT_SETUP, []))

    def test_fetch_refuses_anything_but_get_booking(self):
        jar = bot_session.Jar(session=requests.Session(), path=self.jar)
        for path in ("/booking/review", "/booking/confirm", "/account/login", "/booking/../booking/confirm"):
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                bot_session.fetch(jar, path, {})


class TestSessionFailures(MonitorTestCase):
    def test_login_redirect_emails_once_then_waits_for_reseed(self):
        code, out, adapter, sent = self.run_monitor([login_redirect(), ok()], iterations=3)
        self.assertEqual(code, monitor.EXIT_SESSION)
        self.assertEqual(len(adapter.sent), 1, "must stop at the first login redirect")
        self.assertEqual([m["subject"] for m in sent], ["🔴 Bot session expired — reseed needed"])
        for step in ("Incognito", "seal_session.py", "BOT_JAR_KEY", "git add bot_session.enc"):
            self.assertIn(step, sent[0]["body"])
        self.assertEqual(self.state()["session_alert"]["kind"], "expired")

        # Next scheduled runs on the same dead jar: no requests, no email, exit 0
        # (only the detecting run fails, so GitHub emails once).
        for _ in range(2):
            code, out, adapter, sent = self.run_monitor([ok()])
            self.assertEqual((code, adapter.sent, sent), (monitor.EXIT_OK, [], []))
            self.assertIn("not sending any requests", out)

    def test_reseed_clears_the_alert_and_a_later_expiry_emails_again(self):
        self.run_monitor([login_redirect()])
        self.write_jar({"cs.did": "DID_reseeded_3333", "cs.member": "MEMBER_reseeded_4444"})
        code, _, adapter, sent = self.run_monitor([ok(GRID_EN)])
        self.assertEqual((code, len(adapter.sent)), (monitor.EXIT_OK, 1))
        self.assertNotIn("session_alert", self.state())
        code, _, _, sent = self.run_monitor([login_redirect()])
        self.assertEqual(code, monitor.EXIT_SESSION)
        self.assertEqual([m["subject"] for m in sent], ["🔴 Bot session expired — reseed needed"])

    def test_failed_email_is_retried_next_run(self):
        code, _, _, sent = self.run_monitor([login_redirect()], send_fails=True)
        self.assertEqual((code, sent), (monitor.EXIT_SESSION, []))
        self.assertNotIn("session_alert", self.state())
        _, _, adapter, sent = self.run_monitor([login_redirect()])
        self.assertEqual(len(adapter.sent), 1)
        self.assertEqual(len(sent), 1)

    def test_login_form_served_as_200_counts_as_expired(self):
        page = '<html><body><form action="/account/login"><input type="password"></form></body></html>'
        code, _, _, sent = self.run_monitor([ok(page)])
        self.assertEqual(code, monitor.EXIT_SESSION)
        self.assertEqual(len(sent), 1)

    def test_rotation_that_cannot_be_saved_is_session_likely_lost(self):
        rotate = f"cs.member={MEMBER_NEW}; path=/; secure"
        with mock.patch.object(bot_session.os, "replace", side_effect=OSError("disk full")):
            code, out, adapter, sent = self.run_monitor([ok(GRID_EN, set_cookies=[rotate]), ok()], iterations=2)
        self.assertEqual(code, monitor.EXIT_JAR)
        self.assertEqual(len(adapter.sent), 1, "nothing may be sent after a failed save")
        self.assertEqual([m["subject"] for m in sent], ["🔴 Bot session likely lost — reseed needed"])
        self.assertIn("disk full", sent[0]["body"])
        self.assertNotIn(MEMBER_NEW, out + sent[0]["body"])
        self.assertEqual(self.state()["session_alert"]["kind"], "lost")

    def test_alert_session_lost_cli_for_a_failed_push(self):
        code, _, adapter, sent = self.run_monitor([], argv=["--alert-session-lost", "bot_session.enc could not be pushed"])
        self.assertEqual((code, adapter.sent), (monitor.EXIT_JAR, []))
        self.assertEqual([m["subject"] for m in sent], ["🔴 Bot session likely lost — reseed needed"])
        self.assertIn("could not be pushed", sent[0]["body"])
        _, _, _, sent = self.run_monitor([], argv=["--alert-session-lost", "again"])
        self.assertEqual(sent, [], "same jar on disk -> no second email")

    def test_missing_jar_is_a_setup_error_and_never_seeds(self):
        self.jar.unlink()
        code, _, adapter, sent = self.run_monitor([ok()], env={"CS_DID": "DID_x_12345678", "CS_MEMBER": "MEMBER_x_12345678"})
        self.assertEqual((code, adapter.sent, sent), (monitor.EXIT_SETUP, [], []))
        self.assertFalse(self.jar.exists())


@unittest.skipIf(Fernet is None, "cryptography not installed")
class TestEncryptedSession(MonitorTestCase):
    def test_rotation_is_persisted_to_the_encrypted_jar_and_used_next(self):
        key = Fernet.generate_key().decode()
        self.write_jar(COOKIES, path=self.enc, fernet=Fernet(key.encode()))
        self.jar.unlink()
        rotate = f"cs.member={MEMBER_NEW}; path=/; secure"
        code, out, adapter, _ = self.run_monitor([ok(GRID_EN, set_cookies=[rotate]), ok(GRID_EN)],
                                                 iterations=2, env={"BOT_JAR_KEY": key})
        self.assertEqual(code, monitor.EXIT_OK)
        self.assertIn(("cs.member", MEMBER_NEW), adapter.sent[1]["cookies"])
        s = requests.Session()
        bot_session.load_jar(self.enc, s, Fernet(key.encode()))
        self.assertEqual({c.name: c.value for c in s.cookies}["cs.member"], MEMBER_NEW)
        self.assertNotIn(MEMBER_NEW, out)
        self.assertNotIn(key, out)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["bot_session.enc", "config.json", "state.json"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
