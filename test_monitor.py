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
        self.state_file.write_text("{}")  # tracked in git: always there (missing = halt, see test_autobook)
        marker = mock.patch.object(monitor, "STATE_ALERT_MARKER", self.dir / ".state_alert.json")
        marker.start()
        self.addCleanup(marker.stop)
        run_file = mock.patch.object(monitor, "RUN_FILE", self.dir / ".autobook_run.json")
        run_file.start()
        self.addCleanup(run_file.stop)
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
        for text, want in [
            ("20:00", "20:00"), ("8:00", "08:00"), ("20.00", "20:00"), ("๒๐:๐๐", "20:00"),
            ("20:00 - 21:00", "20:00"), ("20:00 น.", "20:00"),
            ("8:00 PM", "20:00"), ("8 pm", "20:00"), ("8:00 p.m.", "20:00"), ("08:30PM", "20:30"),
            ("12:00 AM", "00:00"), ("12:30 pm", "12:30"), ("6:00 AM - 7:00 AM", "06:00"),
            ("8:00 - 9:00 PM", None),     # whose PM? never guessed
            ("13:00 PM", None), ("0:30 AM", None), ("25:00", None), ("20:75", None),
            ("Full", None), ("", None), ("Evening", None), ("8", None),
        ]:
            with self.subTest(text=text):
                self.assertEqual(booking_page.slot_time(text), want)

    def test_opens_at_uses_the_same_24_hour_conversion(self):
        self.assertEqual(booking_page.parse_page("<p>Booking opens 21:00</p>")["opens_at"], "21:00")


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

    def test_not_open_yet_is_a_read_not_an_error_and_keeps_known_open(self):
        known = {f"{self.date}|LOC001|any|20:00": True}
        self.state_file.write_text(json.dumps({"known_open": known}))
        for page in (LOCKED_EN, LOCKED_TH):
            with self.subTest(lang=page[12:14]):
                code, out, adapter, sent = self.run_monitor([ok(page)], iterations=1)
                self.assertEqual(code, monitor.EXIT_OK)
                self.assertEqual(sent, [])
                self.assertIn("not open yet (opens 21:00)", out)
                state = self.state()
                self.assertNotIn("session_alert", state)
                self.assertEqual(state["known_open"], known, "not-open-yet must leave known_open as-is")
                self.assertIn("last_check", state, "a not-open-yet page counts as a read")
                self.assertNotIn("unreadable_passes", state)

    def test_opening_hours_text_without_a_grid_is_an_unexpected_page(self):
        page = "<html><body><p>Opening hours 06:00-22:00</p><p>เวลาเปิด 06:00 - 22:00 น.</p></body></html>"
        self.assertFalse(booking_page.parse_page(page)["not_open_yet"])
        self.assertIsNone(booking_page.parse_page(page)["opens_at"])
        code, out, _, sent = self.run_monitor([ok(page)])
        self.assertEqual((code, sent), (monitor.EXIT_OK, []))
        self.assertIn("unexpected page", out)
        self.assertNotIn("not open yet", out)

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

    def test_12_hour_grid_is_converted(self):
        page = grid(("8:00 PM", "2 free", False), ("9:00 PM", "Full", True), ("10:00 PM", "1 free", False))
        _, _, _, sent = self.run_monitor([ok(page)])
        self.assertEqual(len(sent), 1)
        self.assertIn(f"{self.date} 20:00 — Crystal Sports (2 free)", sent[0]["body"])
        self.assertIn(f"{self.date} 22:00 — Crystal Sports (1 free)", sent[0]["body"])

    def test_unreadable_or_duplicate_slot_times_make_the_page_unexpected(self):
        for label, page in [
            ("ambiguous AM/PM", grid(("8:00 - 9:00 PM", "2 free", False), ("22:00", "1 free", False))),
            ("no time at all", grid(("Evening", "2 free", False), ("22:00", "1 free", False))),
            ("same time twice", grid(("8:00", "2 free", False), ("8:00", "1 free", False), ("22:00", "1 free", False))),
        ]:
            with self.subTest(label):
                self.state_file.write_text("{}")
                code, out, _, sent = self.run_monitor([ok(page)])
                self.assertEqual(code, monitor.EXIT_OK)
                self.assertEqual(sent, [], "no slot email from a page whose times can't be trusted")
                self.assertIn("unexpected page", out)
                self.assertNotIn("known_open", self.state())

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


class TestUnreadableSite(MonitorTestCase):
    def test_unreadable_pass_leaves_last_check_and_counts(self):
        before = {"last_check": "2026-01-01T00:00:00+07:00", "currently_open_count": 3,
                  "known_open": {f"{self.date}|LOC001|any|20:00": True}}
        self.state_file.write_text(json.dumps(before))
        code, out, _, sent = self.run_monitor([ok(NO_GRID), (503, [], "")], iterations=2)
        self.assertEqual((code, sent), (monitor.EXIT_OK, []))
        state = self.state()
        for k, v in before.items():
            self.assertEqual(state[k], v, f"{k} must not move when no page was read")
        self.assertEqual(state["unreadable_passes"], 2)
        self.assertIn("unreadable_since", state)

    def test_one_email_after_30_minutes_then_cleared_by_a_good_read(self):
        now = datetime.now(monitor.TZ)
        self.state_file.write_text(json.dumps({
            "unreadable_since": (now - timedelta(minutes=31)).isoformat(),
            "unreadable_last": (now - timedelta(minutes=2)).isoformat(),  # a live streak
            "unreadable_passes": 7}))
        _, _, _, sent = self.run_monitor([ok(NO_GRID)])
        self.assertEqual([m["subject"] for m in sent], ["🔴 Monitor can't read the booking site"])
        self.assertIn("31 minutes", sent[0]["body"])
        self.assertIn("unexpected page", sent[0]["body"])
        _, _, _, sent = self.run_monitor([(503, [], ""), (503, [], "")], iterations=2)
        self.assertEqual(sent, [], "deduped until a good read")
        _, _, _, sent = self.run_monitor([ok(grid(("20:00", "Full", True)))])
        self.assertEqual(sent, [])
        state = self.state()
        for k in ("unreadable_since", "unreadable_passes", "read_alert"):
            self.assertNotIn(k, state)
        self.assertIn("last_check", state)

    def test_no_email_before_30_minutes(self):
        now = datetime.now(monitor.TZ)
        self.state_file.write_text(json.dumps({"unreadable_since": (now - timedelta(minutes=29)).isoformat(),
                                               "unreadable_last": (now - timedelta(minutes=2)).isoformat()}))
        _, _, _, sent = self.run_monitor([ok(NO_GRID)])
        self.assertEqual(sent, [])

    def test_stale_or_partial_streak_starts_over_instead_of_emailing(self):
        now = datetime.now(monitor.TZ)
        two_days = (now - timedelta(days=2)).isoformat()
        for label, planted in [
            ("streak whose last pass was long ago", {"unreadable_since": two_days, "unreadable_last": two_days}),
            ("since without a last pass (written by the previous version)", {"unreadable_since": two_days}),
            ("garbage timestamps", {"unreadable_since": "yesterday", "unreadable_last": 12}),
        ]:
            with self.subTest(label):
                self.state_file.write_text(json.dumps({**planted, "unreadable_passes": 40}))
                _, out, _, sent = self.run_monitor([ok(NO_GRID)])
                self.assertEqual(sent, [], "a stale streak must not trigger the email at once")
                state = self.state()
                self.assertEqual(state["unreadable_passes"], 1)
                self.assertLess((datetime.now(monitor.TZ) - datetime.fromisoformat(state["unreadable_since"])).total_seconds(), 60)
                self.assertIn("0 min", out)

    def test_early_exit_passes_break_the_streak(self):
        now = datetime.now(monitor.TZ)
        live = {"unreadable_since": (now - timedelta(minutes=29)).isoformat(),
                "unreadable_last": (now - timedelta(minutes=1)).isoformat(), "unreadable_passes": 9}
        # A pass with nothing to check, then an unreadable pass: a new streak, no email.
        self.state_file.write_text(json.dumps(live))
        self.write_config([target(day(-1), [("20:00", False)])])
        self.run_monitor([])
        self.assertFalse(set(monitor.UNREADABLE_STREAK_KEYS) & set(self.state()))
        # A login exit clears it too (the session email takes over).
        self.state_file.write_text(json.dumps(live))
        self.write_config([target(self.date, [("20:00", False)])])
        self.run_monitor([login_redirect()])
        self.assertFalse(set(monitor.UNREADABLE_STREAK_KEYS) & set(self.state()))

    def test_one_readable_page_counts_as_a_read(self):
        self.write_config([target(self.date, [("20:00", False)], locations=("LOC001", "LOC002"))])
        self.state_file.write_text(json.dumps({"unreadable_since": day(-1) + "T00:00:00+07:00"}))
        _, _, _, sent = self.run_monitor([ok(NO_GRID), ok(LOCKED_EN)])
        self.assertEqual(sent, [])
        self.assertNotIn("unreadable_since", self.state())


class TestStateWriteFailures(MonitorTestCase):
    """A failing state.json write must never stop a session email or change
    the exit code a path intends."""

    def broken_state(self):
        return mock.patch.object(monitor, "save_state", side_effect=OSError("read-only file system"))

    def test_expired_email_and_exit_3_survive_a_failing_state_write(self):
        with self.broken_state():
            code, out, _, sent = self.run_monitor([login_redirect()])
        self.assertEqual(code, monitor.EXIT_SESSION)
        self.assertEqual([m["subject"] for m in sent], ["🔴 Bot session expired — reseed needed"])
        self.assertIn("couldn't write state.json", out)

    def test_lost_email_and_exit_4_survive_a_failing_state_write(self):
        rotate = f"cs.member={MEMBER_NEW}; path=/; secure"
        with self.broken_state(), mock.patch.object(bot_session.os, "replace", side_effect=OSError("disk full")):
            code, _, _, sent = self.run_monitor([ok(GRID_EN, set_cookies=[rotate])])
        self.assertEqual(code, monitor.EXIT_JAR)
        self.assertEqual([m["subject"] for m in sent], ["🔴 Bot session likely lost — reseed needed"])

    def test_transient_jar_failure_stays_exit_1(self):
        with self.broken_state(), mock.patch.object(bot_session.os, "replace", side_effect=OSError("disk full")):
            code, _, _, sent = self.run_monitor([ok(GRID_EN)])
        self.assertEqual((code, sent), (monitor.EXIT_ERROR, []))

    def test_dead_session_repeat_run_stays_exit_0(self):
        self.run_monitor([login_redirect()])
        with self.broken_state():
            code, _, adapter, sent = self.run_monitor([ok()])
        self.assertEqual((code, adapter.sent, sent), (monitor.EXIT_OK, [], []))

    def test_alert_cli_still_emails_and_exits_4(self):
        with self.broken_state():
            code, _, _, sent = self.run_monitor([], argv=["--alert-session-lost", "push failed"])
        self.assertEqual(code, monitor.EXIT_JAR)
        self.assertEqual(len(sent), 1)

    def test_normal_run_still_emails_then_fails_the_run(self):
        with self.broken_state():
            code, out, _, sent = self.run_monitor([ok(GRID_EN), ok(GRID_EN)], iterations=2)
        self.assertEqual(code, monitor.EXIT_ERROR)
        self.assertEqual(len(sent), 1, "in-memory dedupe still works within the run")
        self.assertIn("couldn't be written — failing the run", out)


class TestLoginDetector(unittest.TestCase):
    def test_login_pages(self):
        for label, html, url in [
            ("password input", '<form><input type="password" name="p"></form>', ""),
            ("OTP input", '<input autocomplete="one-time-code">', ""),
            ("OTP by name", "<input name='otpCode'>", ""),
            ("login form action", '<form method="post" action="/account/login?ReturnUrl=%2F">', ""),
            ("login URL", "<p>hello</p>", f"{bot_session.BASE}/account/login?ReturnUrl=%2Fbooking"),
            ("Thai text + form", "<h1>เข้าสู่ระบบ</h1><form><input name='mobile'></form>", ""),
            ("English text + form", "<h1>Sign in</h1><form><input name='mobile'></form>", ""),
        ]:
            with self.subTest(label):
                self.assertTrue(booking_page.is_login_page(html, url))

    def test_not_login_pages(self):
        for label, html in [
            ("a Log in link, no form", '<a href="/account/login">Log in</a><p>Server busy</p>'),
            ("logged-in nav", '<a href="/account/logout">Log out</a><form action="/booking/search"><input name="q"></form>'),
            ("the grid", GRID_EN),
            ("not open yet", LOCKED_TH),
        ]:
            with self.subTest(label):
                self.assertFalse(booking_page.is_login_page(html, f"{bot_session.BASE}/booking"))


class TestNoPost(MonitorTestCase):
    def test_full_run_with_starred_open_slot_never_posts(self):
        # Mode off (unset or explicit). dry_run's no-POST run is in
        # test_autobook.TestModes, which has the pages auto-book reads.
        for env in ({}, {"AUTOBOOK_MODE": "off"}, {"AUTOBOOK_MODE": "not-a-mode"}):
            with self.subTest(env=env):
                self.state_file.write_text("{}")
                self._full_run_never_posts(env)

    def _full_run_never_posts(self, env):
        self.write_config([target(self.date, [("20:00", True), ("22:00", False)], locations=("LOC001", "LOC002"))])
        script = [ok(GRID_EN), ok(GRID_TH), ok(GRID_EN), ok(GRID_TH)]
        code, out, adapter, sent = self.run_monitor(script, iterations=2, env=env)
        self.assertEqual(code, monitor.EXIT_OK)
        self.assertEqual(len(adapter.sent), 4)
        self.assertEqual({r["method"] for r in adapter.sent}, {"GET"})
        self.assertTrue(all(r["url"].startswith(f"{bot_session.BASE}/booking?") for r in adapter.sent))
        self.assertFalse(any(f in r["url"] for r in adapter.sent for f in ("/booking/review", "/booking/confirm", "/bookings")))
        self.assertEqual(len(sent), 1, "one email for the pass that found the openings, none on the repeat")
        body = sent[0]["body"]
        self.assertEqual(body.count(monitor.AUTOBOOK_PAUSED_NOTE), 2, "once under each location's starred 20:00")
        self.assertIn("20:00 — Crystal Sports (2 free)  ★", body)
        self.assertNotIn("autobook_attempts", self.state())

    def test_fetch_refuses_anything_but_the_allowed_gets(self):
        jar = bot_session.Jar(session=requests.Session(), path=self.jar)
        for path in ("/booking/confirm", "/account/login", "/account/logout", "/wallet/topup/1",
                     "/booking/../booking/confirm", "/bookings/SPORTS-1/sheet"):
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

    def test_login_or_otp_page_served_as_200_counts_as_expired(self):
        for page in ('<html><body><form action="/account/login"><input type="password"></form></body></html>',
                     '<html><body><h1>ยืนยัน OTP</h1><input autocomplete="one-time-code"></body></html>'):
            with self.subTest(page=page[12:40]):
                self.state_file.write_text("{}")
                code, _, _, sent = self.run_monitor([ok(page)])
                self.assertEqual(code, monitor.EXIT_SESSION)
                self.assertEqual(len(sent), 1)

    def test_save_failure_without_cookie_changes_is_transient(self):
        with mock.patch.object(bot_session.os, "replace", side_effect=OSError("disk full")):
            code, out, adapter, sent = self.run_monitor([ok(GRID_EN), ok()], iterations=2)
        self.assertEqual(code, monitor.EXIT_ERROR)
        self.assertEqual(len(adapter.sent), 1)
        self.assertEqual(sent, [], "no session email: nothing was lost")
        self.assertNotIn("session_alert", self.state())
        self.assertIn("no cookie had changed", out)
        # No permanent halt: the next run sends requests and works normally.
        code, _, adapter, sent = self.run_monitor([ok(GRID_EN)])
        self.assertEqual((code, len(adapter.sent), len(sent)), (monitor.EXIT_OK, 1, 1))

    def test_body_read_failure_after_rotation_still_persists_the_cookie(self):
        rotate = f"cs.member={MEMBER_NEW}; path=/; secure"
        broken = ok(GRID_EN, set_cookies=[rotate]) + (requests.exceptions.ChunkedEncodingError("connection reset mid-body"),)
        code, out, adapter, sent = self.run_monitor([broken, ok(GRID_EN)], iterations=2)
        self.assertEqual(code, monitor.EXIT_OK)
        self.assertIn("network: ChunkedEncodingError", out)
        self.assertNotIn("connection reset mid-body", out, "exception messages (may carry a URL) aren't logged")
        on_disk = {r["name"]: r["value"] for r in json.loads(self.jar.read_text())["cookies"]}
        self.assertEqual(on_disk["cs.member"], MEMBER_NEW)
        self.assertIn(("cs.member", MEMBER_NEW), adapter.sent[1]["cookies"])
        self.assertNotIn(("cs.member", COOKIES["cs.member"]), adapter.sent[1]["cookies"])
        self.assertIn("ROTATED cs.member", out)
        self.assertNotIn(MEMBER_NEW, out)

    def test_jar_is_saved_even_if_handling_the_response_raises(self):
        # requests has applied the Set-Cookie by the time get() returns; if
        # anything after that raises, the rotation must still reach disk.
        session = requests.Session()
        adapter = ScriptedAdapter([ok(GRID_EN, set_cookies=[f"cs.member={MEMBER_NEW}; path=/; secure"])])
        session.mount("https://", adapter)
        for name, value in COOKIES.items():
            session.cookies.set_cookie(bot_session.create_cookie(name, value, domain=bot_session.HOST, path="/", secure=True))
        jar = bot_session.Jar(session=session, path=self.jar)
        with mock.patch.object(bot_session, "merge_set_cookies", side_effect=RuntimeError("boom")), \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            bot_session.fetch(jar, bot_session.BOOKING_PATH, {})
        on_disk = {r["name"]: r["value"] for r in json.loads(self.jar.read_text())["cookies"]}
        self.assertEqual(on_disk["cs.member"], MEMBER_NEW)

    def test_rotation_that_cannot_be_saved_is_session_likely_lost(self):
        rotate = f"cs.member={MEMBER_NEW}; path=/; secure"
        real_replace = os.replace

        def replace(src, dst):  # only the jar's disk is full: state.json (also atomic now) still saves
            if Path(dst) == self.jar:
                raise OSError("disk full")
            return real_replace(src, dst)
        with mock.patch.object(bot_session.os, "replace", side_effect=replace):
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
        # .autobook_run.json holds only this run's auto-book date|time keys (no secrets).
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         [".autobook_run.json", "bot_session.enc", "config.json", "state.json"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
