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
from urllib.parse import urlsplit

import requests

import booking_page
import bot_session
import monitor
from test_probe_new_site import GRID_EN, GRID_TH, MEMBER_NEW, ScriptedAdapter, login_redirect, ok

FIXTURES = Path(__file__).resolve().parent / "tests" / "fixtures"

try:
    from cryptography.fernet import Fernet
except ImportError:
    Fernet = None

COOKIES = {"cs.did": "DID_monitor_value_1111", "cs.member": "MEMBER_monitor_value_2222"}
# The culture cookie the site's /lang/en sets. Test jars start with it (the
# steady state after the first run), so /lang/en is only sent where a test
# leaves it out on purpose.
SITE_CULTURE = {".AspNetCore.Culture": "c=en|uic=en"}
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
        self.cache = self.dir / ".jar_cache" / "bot_session.enc"   # the Actions-cache fallback copy
        self.write_jar({**COOKIES, **SITE_CULTURE})
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

    def run_monitor(self, script, iterations=1, env=None, argv=None, send_fails=False, lang_script=None):
        """Runs monitor.run() (or main(argv)) end to end. Any attempt to send
        a non-GET request — through the session or the requests module —
        fails the test."""
        adapter = ScriptedAdapter(script, lang_script=lang_script)
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
                                   enc_path=self.enc, session=session, cache_path=self.cache)
        self.assertTrue(all(r["method"] == "GET" for r in adapter.sent + adapter.lang_sent), adapter.sent)
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
    """Only the real login page counts: its URL, or its form (a <form>
    posting to /account/login). Generic inputs or words never do — account
    pages have forms, tokens and checkboxes too."""

    LOGIN_FIXTURES = ("8_login.html", "8_login_en.html")   # the real login page, Thai and English

    def test_the_real_login_page_is_a_login_page(self):
        for name in self.LOGIN_FIXTURES:
            with self.subTest(name):
                html = (FIXTURES / name).read_text(encoding="utf-8")
                self.assertEqual(booking_page.login_page_markers(html, f"{bot_session.BASE}/bookings"), ["login form"])
                self.assertTrue(booking_page.is_login_page(html))

    def test_every_other_real_page_is_not_a_login_page(self):
        pages = sorted(f for f in FIXTURES.glob("*.html") if f.name not in self.LOGIN_FIXTURES)
        self.assertEqual(len(pages), 7)
        for f in pages:
            with self.subTest(f.name):
                html = f.read_text(encoding="utf-8")
                self.assertEqual(booking_page.login_page_markers(html, f"{bot_session.BASE}/bookings"), [])

    def test_login_pages(self):
        for label, html, url in [
            ("login form action", '<form method="post" action="/account/login?ReturnUrl=%2F">', ""),
            ("login form, absolute action", "<form action='https://x.example/Account/Login/'></form>", ""),
            ("login form, unquoted", "<form method=post action=/account/login>", ""),
            ("login URL", "<p>hello</p>", f"{bot_session.BASE}/account/login?ReturnUrl=%2Fbooking"),
        ]:
            with self.subTest(label):
                self.assertTrue(booking_page.is_login_page(html, url))

    def test_not_login_pages(self):
        for label, html in [
            ("a Log in link, no form", '<a href="/account/login">Log in</a><p>Server busy</p>'),
            ("logged-in nav", '<a href="/account/logout">Log out</a><form action="/booking/search"><input name="q"></form>'),
            ("logout form", '<form action="/account/logout" method="post"><input type="hidden" name="__RequestVerificationToken"></form>'),
            ("password input", '<form><input type="password" name="p"></form>'),
            ("OTP input", '<form><input autocomplete="one-time-code"></form>'),
            ("OTP by name", "<form><input name='otpCode'></form>"),
            ("Thai login text + form", "<h1>เข้าสู่ระบบ</h1><form><input name='mobile'></form>"),
            ("English login text + form", "<h1>Sign in</h1><p>Log in again</p><form action='/bookings/lines/1/cancel'></form>"),
            ("login only in a returnUrl", '<form action="/culture/set?returnUrl=%2Faccount%2Flogin"></form>'),
            ("a different account path", '<form action="/account/login-help"></form>'),
            ("form markup only in a script", "<script>var t = '<form action=\"/account/login\">';</script><p>ok</p>"),
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


class TestLanguageStep(MonitorTestCase):
    """At the start of a run: GET /lang/en?returnUrl=/booking (the site's EN
    button) if the jar has no culture cookie from the site, or a page wasn't
    English last run. Its Set-Cookie is kept in the jar and sent from then
    on. The bot never invents a cookie of its own."""

    def setUp(self):
        super().setUp()
        self.write_jar(COOKIES)           # a jar before its first /lang/en
        self.write_config([target((datetime.now(monitor.TZ) + timedelta(days=2)).date().isoformat(), [("20:00", False)])])

    def test_no_culture_cookie_sets_one_and_it_is_sent(self):
        code, out, adapter, _ = self.run_monitor([ok(GRID_EN)])
        self.assertEqual(code, monitor.EXIT_OK)
        self.assertEqual([urlsplit(r["url"]).query for r in adapter.lang_sent], ["returnUrl=%2Fbooking"])
        self.assertNotIn(".AspNetCore.Culture", [n for n, _ in adapter.lang_sent[0]["cookies"]])
        self.assertIn((".AspNetCore.Culture", "c%3Den%7Cuic%3Den"), adapter.sent[0]["cookies"],
                      "the grid request after it carries the site's cookie")
        self.assertIn(".AspNetCore.Culture", self.jar_names(), "stored in the jar")
        self.assertIn("🌐 /lang/en (no culture cookie from the site yet) -> 302; culture cookie: .AspNetCore.Culture", out)
        self.assertNotIn("c%3Den", out, "cookie values are never printed")

    def test_once_set_it_is_not_requested_again(self):
        self.run_monitor([ok(GRID_EN)])
        code, out, adapter, _ = self.run_monitor([ok(GRID_EN)])
        self.assertEqual(adapter.lang_sent, [])
        self.assertIn((".AspNetCore.Culture", "c%3Den%7Cuic%3Den"), adapter.sent[0]["cookies"])

    def test_once_per_run_not_per_pass(self):
        code, out, adapter, _ = self.run_monitor([ok(GRID_EN), ok(GRID_EN)], iterations=2,
                                                 lang_script=[(302, [("Location", "/booking")], "")] * 2)
        self.assertEqual(len(adapter.lang_sent), 1)

    def test_a_page_not_in_english_last_run_redoes_it(self):
        self.write_jar({**COOKIES, **SITE_CULTURE})
        self.state_file.write_text(json.dumps({"lang_retry": "2026-11-15T00:00:00+07:00"}))
        code, out, adapter, _ = self.run_monitor([ok(GRID_EN)])
        self.assertEqual(len(adapter.lang_sent), 1)
        self.assertIn("a page wasn't English last run", out)
        self.assertNotIn("lang_retry", self.state())

    def test_network_error_keeps_going_and_retries_next_run(self):
        self.state_file.write_text(json.dumps({"lang_retry": "2026-11-15T00:00:00+07:00"}))
        code, out, adapter, _ = self.run_monitor([ok(GRID_EN)], lang_script=[requests.ConnectionError("down")])
        self.assertEqual(code, monitor.EXIT_OK)
        self.assertEqual(len(adapter.sent), 1, "the grid is still checked")
        self.assertIn("/lang/en failed (ConnectionError)", out)
        self.assertIn("lang_retry", self.state(), "not cleared without a response")

    def test_a_login_redirect_on_lang_en_doesnt_stop_the_run(self):
        # The grid decides whether the session is dead, not the EN button.
        code, out, adapter, sent = self.run_monitor([ok(GRID_EN)], lang_script=[login_redirect()])
        self.assertEqual(code, monitor.EXIT_OK)
        self.assertEqual(len(adapter.sent), 1)
        self.assertEqual([m for m in sent if "session" in m["subject"].lower()], [])

    def test_a_jar_save_failure_on_lang_en_is_handled_like_any_other(self):
        real_replace = os.replace

        def replace(src, dst):
            if Path(dst) == self.jar:
                raise OSError("disk full")
            return real_replace(src, dst)
        with mock.patch.object(bot_session.os, "replace", side_effect=replace):
            code, out, adapter, sent = self.run_monitor([ok(GRID_EN)])
        self.assertEqual(code, monitor.EXIT_JAR, "the culture cookie changed and couldn't be saved")
        self.assertEqual(adapter.sent, [], "nothing after a failed save")

    def test_dead_session_sends_nothing_at_all(self):
        jar_fp = bot_session.file_fingerprint(self.jar)
        self.state_file.write_text(json.dumps({"session_alert": {"kind": "expired", "sent_at": "x", "jar_sha256": jar_fp}}))
        code, out, adapter, _ = self.run_monitor([])
        self.assertEqual((adapter.sent, adapter.lang_sent), ([], []))

    def test_lang_retry_is_part_of_the_checked_state_shape(self):
        self.assertEqual(monitor.shape_problems({"lang_retry": "2026-11-15T00:00:00+07:00"}), [])
        self.assertEqual(monitor.shape_problems({"lang_retry": {"x": 1}}), ["unexpected shape: lang_retry"])

    def jar_names(self):
        return {c["name"] for c in json.loads(self.jar.read_text())["cookies"]}


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

    def test_login_page_served_as_200_counts_as_expired(self):
        for page in ('<html><body><form action="/account/login"><input name="mobile"></form></body></html>',
                     (FIXTURES / "8_login.html").read_text(encoding="utf-8"),
                     (FIXTURES / "8_login_en.html").read_text(encoding="utf-8")):
            with self.subTest(page=page[12:40]):
                self.state_file.write_text("{}")
                code, _, _, sent = self.run_monitor([ok(page)])
                self.assertEqual(code, monitor.EXIT_SESSION)
                self.assertEqual(len(sent), 1)

    def test_a_200_page_without_the_login_form_is_not_expired(self):
        page = '<html><body><h1>ยืนยัน OTP</h1><form><input type="password"><input autocomplete="one-time-code"></form></body></html>'
        code, out, _, sent = self.run_monitor([ok(page)])
        self.assertNotEqual(code, monitor.EXIT_SESSION)
        self.assertEqual(sent, [])
        self.assertNotIn("session_alert", self.state())
        self.assertIn("unexpected page", out)

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

    # --- a failed push whose jar IS in the Actions cache: the softer email ----
    def cached_alert(self):
        return self.run_monitor([], argv=["--alert-session-lost", "bot_session.enc could not be pushed",
                                          "--cache-saved"])

    def test_cache_saved_sends_the_softer_email(self):
        code, _, adapter, sent = self.cached_alert()
        self.assertEqual((code, adapter.sent), (monitor.EXIT_JAR, []))
        self.assertEqual([m["subject"] for m in sent], [monitor.SESSION_CACHED_SUBJECT])
        body = sent[0]["body"]
        self.assertNotIn("likely lost", sent[0]["subject"] + body)
        self.assertNotIn("stopped until you reseed", body)
        self.assertIn("could not be pushed", body)
        self.assertIn("The newest cookie was saved to the Actions cache, and the next run will use it", body)
        soft = body.index("You'll only need to reseed if a \"🔴 Bot session expired — reseed needed\" email")
        steps = body.index(monitor.RESEED_STEPS)
        self.assertLess(soft, steps, "reseed steps stay at the bottom, for that case")
        self.assertEqual(body.rstrip("\n"), body[:steps] + monitor.RESEED_STEPS)
        self.assertEqual(self.state()["session_alert"]["kind"], "lost_cached")
        _, _, _, sent = self.cached_alert()
        self.assertEqual(sent, [], "same jar on disk -> no second email")

    def test_without_cache_saved_the_wording_stays_strong(self):
        _, _, _, sent = self.run_monitor([], argv=["--alert-session-lost", "push failed"])
        self.assertEqual([m["subject"] for m in sent], ["🔴 Bot session likely lost — reseed needed"])
        self.assertIn("stopped until you reseed", sent[0]["body"])
        self.assertEqual(self.state()["session_alert"]["kind"], "lost")

    def test_after_the_softer_email_the_next_run_carries_on(self):
        self.cached_alert()
        code, _, adapter, sent = self.run_monitor([ok(GRID_EN)])
        self.assertEqual(code, monitor.EXIT_OK)
        self.assertEqual([m for m in sent if "session" in m["subject"].lower()], [])
        self.assertTrue(adapter.sent, "the cached jar is used: requests go out")
        self.assertNotIn("session_alert", self.state())

    def test_after_the_softer_email_an_expiry_still_emails(self):
        self.cached_alert()
        code, _, _, sent = self.run_monitor([login_redirect()])
        self.assertEqual(code, monitor.EXIT_SESSION)
        self.assertEqual([m["subject"] for m in sent], ["🔴 Bot session expired — reseed needed"])
        self.assertEqual(self.state()["session_alert"]["kind"], "expired")
        _, _, adapter, sent = self.run_monitor([ok(GRID_EN)])
        self.assertEqual((adapter.sent, sent), ([], []), "a real expiry still stops the runs")

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


@unittest.skipIf(Fernet is None, "cryptography not installed")
class TestJarFallback(MonitorTestCase):
    """A run rotates a cookie but can't push bot_session.enc: the Actions
    cache holds the encrypted jar (stage_cache), and the next run uses
    whichever of repo and cache has the higher generation — never a lower
    one, never another session's, and the repo copy if the cache can't be
    read."""

    OLD, NEW = COOKIES["cs.member"], MEMBER_NEW
    ROTATE = f"cs.member={MEMBER_NEW}; path=/; secure"

    def setUp(self):
        super().setUp()
        self.key = Fernet.generate_key().decode()
        self.fernet = Fernet(self.key.encode())
        self.jar.unlink()
        self.write_enc(self.enc, {**COOKIES, **SITE_CULTURE}, 3, "L1")

    def write_enc(self, path, cookies, generation, lineage):
        s = requests.Session()
        for name, value in cookies.items():
            s.cookies.set_cookie(bot_session.create_cookie(name, value, domain=bot_session.HOST, path="/", secure=True))
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        bot_session.save_jar(path, s, self.fernet, generation, lineage)

    def read_enc(self, path):
        gen, lineage, s = bot_session.peek_jar(path, self.fernet)
        return gen, lineage, {c.name: c.value for c in s.cookies}

    def run_enc(self, script, iterations=1):
        return self.run_monitor(script, iterations=iterations, env={"BOT_JAR_KEY": self.key})

    def stage(self):
        with mock.patch.dict(os.environ, {"BOT_JAR_KEY": self.key}, clear=True), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            code = bot_session.stage_cache(self.enc, self.cache)
        return code, out.getvalue()

    def member_sent(self, adapter, i=0):
        return dict(adapter.sent[i]["cookies"]).get("cs.member")

    # --- the scenario from 2026-11-15 ---------------------------------------
    def test_push_fails_after_a_rotation_and_the_next_run_uses_the_cache(self):
        repo_before = self.enc.read_bytes()
        code, out, adapter, _ = self.run_enc([ok(GRID_EN, set_cookies=[self.ROTATE])])
        self.assertEqual(code, monitor.EXIT_OK)
        self.assertEqual(self.read_enc(self.enc)[:2], (4, "L1"), "the rotation bumped the generation")
        self.stage()                                   # the workflow's always() step
        self.assertEqual(self.read_enc(self.cache)[0], 4)
        self.enc.write_bytes(repo_before)              # the push failed: the next checkout has the old copy

        code, out, adapter, _ = self.run_enc([ok(GRID_EN)])
        self.assertEqual(code, monitor.EXIT_OK)
        self.assertEqual(self.member_sent(adapter), self.NEW, "the current cookie, not the superseded one")
        self.assertIn("Jar: CACHE copy, generation 4", out)
        self.assertIn("newer than the repo's generation 3", out)
        self.assertEqual(self.read_enc(self.enc)[:2], (4, "L1"), "written over the repo copy, so it gets committed")
        self.assertNotIn(self.NEW, out)
        self.assertNotIn(self.key, out)

    def test_control_without_the_cache_the_superseded_cookie_would_be_sent(self):
        repo_before = self.enc.read_bytes()
        self.run_enc([ok(GRID_EN, set_cookies=[self.ROTATE])])
        self.enc.write_bytes(repo_before)
        code, out, adapter, _ = self.run_enc([ok(GRID_EN)])
        self.assertEqual(self.member_sent(adapter), self.OLD)
        self.assertIn("Jar: repo copy, generation 3", out)
        self.assertIn("no cache copy", out)

    def test_a_rotation_in_the_cache_run_continues_from_the_cache_generation(self):
        self.write_enc(self.cache, {**COOKIES, **SITE_CULTURE, "cs.member": self.NEW}, 7, "L1")
        rotate2 = "cs.member=MEMBER_third_5555; path=/; secure"
        self.run_enc([ok(GRID_EN, set_cookies=[rotate2])])
        self.assertEqual(self.read_enc(self.enc)[0], 8)

    # --- repo newer / same / cache unusable ---------------------------------
    def test_repo_newer_than_the_cache_uses_the_repo(self):
        self.write_enc(self.enc, {**COOKIES, **SITE_CULTURE, "cs.member": "MEMBER_repo_6666"}, 5, "L1")
        self.write_enc(self.cache, {**COOKIES, **SITE_CULTURE, "cs.member": "MEMBER_cache_7777"}, 4, "L1")
        code, out, adapter, _ = self.run_enc([ok(GRID_EN)])
        self.assertEqual(self.member_sent(adapter), "MEMBER_repo_6666")
        self.assertIn("Jar: repo copy, generation 5", out)
        self.assertIn("cache copy generation 4", out)
        self.assertIn("is older", out)

    def test_same_generation_uses_the_repo(self):
        self.write_enc(self.cache, {**COOKIES, **SITE_CULTURE, "cs.member": "MEMBER_cache_7777"}, 3, "L1")
        code, out, adapter, _ = self.run_enc([ok(GRID_EN)])
        self.assertEqual(self.member_sent(adapter), self.OLD)
        self.assertIn("is the same", out)

    def test_an_unreadable_cache_uses_the_repo(self):
        other = Fernet(Fernet.generate_key())
        s = requests.Session()
        s.cookies.set_cookie(bot_session.create_cookie("cs.member", "MEMBER_x", domain=bot_session.HOST, path="/"))
        cases = {"garbage": b"\x00not a jar", "empty": b"", "truncated": self.enc.read_bytes()[:40],
                 "another key": None}
        for label, data in cases.items():
            with self.subTest(label):
                self.cache.parent.mkdir(parents=True, exist_ok=True)
                if data is None:
                    self.cache.unlink(missing_ok=True)
                    bot_session.save_jar(self.cache, s, other, 99, "L1")
                else:
                    self.cache.write_bytes(data)
                code, out, adapter, _ = self.run_enc([ok(GRID_EN)])
                self.assertEqual(code, monitor.EXIT_OK)
                self.assertEqual(self.member_sent(adapter), self.OLD)
                self.assertIn("the cache copy can't be read", out)

    def test_a_cache_from_another_session_is_ignored_even_if_higher(self):
        self.write_enc(self.cache, {**COOKIES, **SITE_CULTURE, "cs.member": "MEMBER_old_session"}, 50, "OTHER")
        code, out, adapter, _ = self.run_enc([ok(GRID_EN)])
        self.assertEqual(self.member_sent(adapter), self.OLD)
        self.assertIn("from a different session (reseeded?) — ignored", out)

    def test_an_unreadable_repo_jar_still_sends_nothing(self):
        self.write_enc(self.cache, {**COOKIES, **SITE_CULTURE}, 9, "L1")
        self.enc.write_bytes(b"garbage")
        code, out, adapter, _ = self.run_enc([])
        self.assertEqual(code, monitor.EXIT_SETUP)
        self.assertEqual((adapter.sent, adapter.lang_sent), ([], []))

    # --- generations never go backwards -------------------------------------
    def test_generation_counts_rotations_only(self):
        self.run_enc([ok(GRID_EN, set_cookies=[self.ROTATE]), ok(GRID_EN), ok(GRID_EN)], iterations=3)
        self.assertEqual(self.read_enc(self.enc)[0], 4, "one rotation, two unchanged responses")

    def test_save_jar_refuses_a_lower_generation_of_the_same_session(self):
        before = self.enc.read_bytes()
        s = requests.Session()
        with self.assertRaises(OSError):
            bot_session.save_jar(self.enc, s, self.fernet, 2, "L1")
        self.assertEqual(self.enc.read_bytes(), before)
        bot_session.save_jar(self.enc, s, self.fernet, 0, "NEW-SESSION")   # a reseed may start again at 0
        self.assertEqual(self.read_enc(self.enc)[:2], (0, "NEW-SESSION"))

    def test_stage_cache_keeps_a_higher_cache_and_copies_a_newer_repo(self):
        self.write_enc(self.cache, {**COOKIES, "cs.member": "MEMBER_cache_7777"}, 6, "L1")
        cached = self.cache.read_bytes()
        code, out = self.stage()
        self.assertEqual((code, self.cache.read_bytes()), (0, cached), "repo gen 3 never replaces cache gen 6")
        self.assertIn("kept the cache copy, generation 6", out)
        self.write_enc(self.enc, {**COOKIES, "cs.member": "MEMBER_repo_8888"}, 7, "L1")
        code, out = self.stage()
        self.assertEqual(self.cache.read_bytes(), self.enc.read_bytes(), "the repo's ciphertext, byte for byte")
        self.assertIn("staged the repo copy, generation 7", out)

    def test_stage_cache_replaces_another_sessions_or_unreadable_cache(self):
        for label in ("other session", "unreadable"):
            with self.subTest(label):
                if label == "other session":
                    self.write_enc(self.cache, COOKIES, 99, "OTHER")
                else:
                    self.cache.write_bytes(b"junk")
                self.stage()
                self.assertEqual(self.cache.read_bytes(), self.enc.read_bytes())

    def test_stage_cache_writes_ciphertext_only_and_needs_the_key(self):
        self.stage()
        data = self.cache.read_bytes()
        for needle in (self.OLD, COOKIES["cs.did"], "cs.member", "cookies"):
            self.assertNotIn(needle.encode(), data)
        self.cache.unlink()
        with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bot_session.stage_cache(self.enc, self.cache), 1)
        self.assertFalse(self.cache.exists())

    def test_stage_cache_keeps_the_cache_when_the_repo_jar_is_unreadable(self):
        self.write_enc(self.cache, COOKIES, 5, "L1")
        cached = self.cache.read_bytes()
        self.enc.write_bytes(b"garbage")
        code, out = self.stage()
        self.assertEqual((code, self.cache.read_bytes()), (0, cached))

    def test_a_jar_from_before_generations_loads_as_generation_0(self):
        legacy = json.dumps({"version": 1, "host": bot_session.HOST, "updated_at": "x",
                             "cookies": [{"name": "cs.member", "value": "M", "domain": bot_session.HOST,
                                          "path": "/", "secure": True, "expires": None}]}).encode()
        self.enc.write_bytes(self.fernet.encrypt(legacy))
        self.assertEqual(self.read_enc(self.enc)[:2], (0, ""))
        for bad in ({"generation": -1}, {"generation": "3"}, {"generation": True}, {"lineage": 5}):
            with self.subTest(bad=bad):
                d = json.loads(legacy); d.update(bad)
                self.enc.write_bytes(self.fernet.encrypt(json.dumps(d).encode()))
                with self.assertRaises(bot_session.JarError):
                    self.read_enc(self.enc)

    # --- the workflows ------------------------------------------------------
    def test_both_workflows_restore_before_and_save_right_after_the_run(self):
        root = Path(__file__).resolve().parent / ".github" / "workflows"
        for name, run_step in (("monitor.yml", "- name: Run monitor"), ("probe.yml", "- name: Run probe")):
            with self.subTest(name):
                wf = (root / name).read_text(encoding="utf-8")
                restore = wf.index("- name: Restore the bot session fallback")
                run = wf.index(run_step)
                stage = wf.index("- name: Stage the bot session fallback")
                save = wf.index("- name: Save the bot session fallback")
                push = wf.index("git push -q origin HEAD:main")
                self.assertLess(restore, run)
                self.assertLess(run, stage)
                self.assertLess(stage, save)
                self.assertLess(save, push, "cached before any push can fail or hang")
                self.assertIn("restore-keys: bot-jar-", wf[restore:run])
                self.assertIn("continue-on-error: true", wf[restore:run])
                self.assertEqual(wf.count("key: bot-jar-${{ github.run_id }}-${{ github.run_attempt }}"), 3)  # save + check
                self.assertIn("python bot_session.py stage-cache", wf[stage:save])
                self.assertIn("if: always()", wf[stage:save])
                self.assertIn("path: .jar_cache/bot_session.enc", wf[save:push])
                self.assertIn("for delay in 10 20 40 60 90 last; do", wf)
                self.assertIn('sleep "$delay"', wf)

    def test_softer_wording_only_when_the_cache_entry_is_confirmed(self):
        root = Path(__file__).resolve().parent / ".github" / "workflows"
        confirmed = 'if [ "$CACHE_HIT" = "true" ] && cmp -s .jar_cache/bot_session.enc bot_session.enc; then'
        for name in ("monitor.yml", "probe.yml"):
            with self.subTest(name):
                wf = (root / name).read_text(encoding="utf-8")
                save = wf.index("- name: Save the bot session fallback")
                check = wf.index("- name: Check the bot session fallback was saved")
                push = wf.index("git push -q origin HEAD:main")
                self.assertLess(save, check)
                self.assertLess(check, push)
                step = wf[check:wf.index("- name:", check + 1)]
                self.assertIn("id: cache_check", step)
                self.assertIn("lookup-only: true", step)
                self.assertIn("uses: actions/cache/restore@v4", step)
                self.assertIn("key: bot-jar-${{ github.run_id }}-${{ github.run_attempt }}", step)
                self.assertIn("continue-on-error: true", step)
                # every step that branches on CACHE_HIT is handed it in its own env
                for start in [i for i in range(len(wf)) if wf.startswith("      - name:", i)]:
                    st = wf[start:wf.find("      - name:", start + 1) % (len(wf) + 1)]
                    if '"$CACHE_HIT"' in st:
                        self.assertIn("CACHE_HIT: ${{ steps.cache_check.outputs.cache-hit }}", st)
                # the cached annotation sits in the confirmed branch, the strong one in the else
                c = wf.index(confirmed)
                els = wf.index("else", c)
                self.assertLess(c, wf.index("title=Bot session not pushed (cached)"))
                self.assertLess(wf.index("title=Bot session not pushed (cached)"), els)
                self.assertLess(els, wf.index("title=Bot session likely lost"))
        wf = (root / "monitor.yml").read_text(encoding="utf-8")
        email = wf[wf.index("- name: Email if the bot session was lost"):]
        email = email[:email.index("- name:", 5)]
        c = email.index(confirmed)
        els = email.index("else", c)
        self.assertIn('"$CACHE_HIT"', email)
        self.assertEqual(email.count("--cache-saved"), 1)
        self.assertLess(c, email.index("--cache-saved"))
        self.assertLess(email.index("--cache-saved"), els)
        self.assertIn('python monitor.py --alert-session-lost "bot_session.enc could not be pushed after a '
                      'cookie rotated"\n', email[els:])

    def test_the_cache_folder_is_gitignored(self):
        gi = (Path(__file__).resolve().parent / ".gitignore").read_text()
        self.assertIn(".jar_cache/", gi.splitlines())


if __name__ == "__main__":
    unittest.main(verbosity=2)
