#!/usr/bin/env python3
"""Tests for stage 3 auto-book (autobook.py + the guarded POST in
bot_session.py). Run with: python3 -m unittest test_autobook -v

Pages come from tests/fixtures/ — scrubbed copies of pages saved from the
live site on 2026-11-13 — moved to a date relative to today so nothing goes
stale. Pages changed from a real one are marked DERIVED (e.g. the filled
Upcoming list and a booking's own page for another date or time). Anything
we have NOT seen on the live site is built in this file and marked
SYNTHETIC (a re-rendered form refusing a taken court, the POST responses'
exact shape apart from the parked redirects, whose Locations are the two
real ones: Nov 11's /wallet/topup/…?then= and Nov 13's
/payment/SPORTS-…/topup-shortfall).

The fake site routes by method + path + query and records every request,
including POST bodies, at the transport level — so "no POST" here means no
POST left the process, whatever code path tried.
"""
import contextlib
import io
import json
import os
import re
import shutil
import smtplib
import subprocess
import sys
import tempfile
import unittest
from datetime import date as _date, timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qsl, urlsplit

import requests

import autobook
import booking_page
import bot_session
import monitor
from test_monitor import COOKIES, EMAIL_ENV, MonitorTestCase, target
from test_probe_new_site import login_redirect, make_response

FIX = Path(__file__).resolve().parent / "tests" / "fixtures"
FIXTURE_DATE = _date(2026, 11, 15)  # the date the live pages were saved for (a Sunday)
REAL_SEL, OTHER_SEL = "270", "306"  # the two free G South courts on the saved page
TOKEN = "FAKE-ANTIFORGERY-TOKEN-02"
REF = "SPORTS-900000000001"          # scrubbed ref: the real top-up URL and pending row agree on it
TOPUP_NOV11 = "/wallet/topup/100000000000000001?then=" + REF   # the real Nov 11 shape (from 4_topup.html)
TOPUP = f"/payment/{REF}/topup-shortfall"                         # the real Nov 13 shape
REF2 = "SPORTS-900000000002"         # scrubbed ref of the real filled Upcoming row and its own page
MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def fixture(name):
    return (FIX / name).read_text(encoding="utf-8")


class Pages:
    """The fixtures, moved from 15 Nov 2026 to `d`."""

    def __init__(self, d):
        self.d = d
        self.iso, self.ymd, self.label = d.isoformat(), d.strftime("%Y%m%d"), autobook.date_label(d.isoformat())
        self.sel = f"{self.ymd}-{REAL_SEL}"
        self.sel2 = f"{self.ymd}-{OTHER_SEL}"

    def move(self, html):
        return (html.replace("2026-11-15", self.iso).replace("20261115", self.ymd)
                .replace(autobook.date_label("2026-11-15"), self.label))

    # --- real pages (scrubbed) ---
    def grid(self):            # /booking?..&time=0 — the slot grid (+ court map)
        return self.move(fixture("1_booking_time.html"))

    def slot(self):            # /booking?..&time=18 — the time selected, court tiles
        return self.move(fixture("1_booking_time.html"))

    def review(self, sel=None):
        html = self.move(fixture("2_review.html"))
        if sel == self.sel2:   # DERIVED: the same review page for the other free court
            html = html.replace(self.sel, self.sel2).replace("G South-1", "G South-3")
        return html

    def pending_with_ref(self, ref=REF):     # real Awaiting-payment page with one row
        return fixture("5_bookings_awaiting.html").replace(REF, ref)

    def upcoming_empty(self):                # real Upcoming page, empty
        return fixture("3_bookings_upcoming.html")

    # --- derived / synthetic ---
    def pending_empty(self):
        """DERIVED: the real Awaiting-payment page with its one row removed."""
        return re.sub(r'<a class="bk-row" role="row".*?</a>', "", fixture("5_bookings_awaiting.html"), flags=re.S)

    def upcoming_filled(self, ref=REF2, d=None, time="23:00", loc_name="Crystal Sports G", court="G South-1",
                        status="Paid", end=None):
        """The REAL filled Upcoming page (one Paid row: 15 Nov, 23:00–24:00,
        G South-1, Crystal Sports G). With arguments it is DERIVED: the same
        row moved to another ref/day/time/location/court/status."""
        d = d or self.d
        end = end or f"{int(time[:2]) + 1:02d}:{time[3:]}"
        return (fixture("6_bookings_upcoming_filled.html")
                .replace(REF2, ref)
                .replace("<b>15</b><span>Nov</span>", f"<b>{d.day}</b><span>{MONTH_ABBR[d.month - 1]}</span>")
                .replace("23:00–24:00 · G South-1", f"{time}–{end} · {court}")
                .replace(f"Crystal Sports G · {ref}", f"{loc_name} · {ref}")
                .replace('badge-dot">Paid<', f'badge-dot">{status}<'))

    def detail(self, ref, d=None, time="23:00", loc_name="Crystal Sports G", court="G South-1", end=None):
        """A booking's own page /bookings/SPORTS-… — the REAL page (15 Nov,
        23:00–24:00, G South-1), DERIVED to another ref/date/time/court."""
        d = d or self.d
        end = end or f"{int(time[:2]) + 1:02d}:{time[3:]}"
        return (fixture("7_booking_detail.html").replace(REF2, ref)
                .replace(autobook.date_label("2026-11-15") + " · 23:00–24:00", f"{autobook.date_label(d.isoformat())} · {time}–{end}")
                .replace("Crystal Sports G · G South-1", f"{loc_name} · {court}"))

    def grid_with_2200_open(self):
        """DERIVED: the real grid with 22:00 open as well (1 free)."""
        html = self.grid().replace(
            '<a class="slot is-disabled" aria-disabled="true">\n                        <span class="slot-time">22:00</span>\n                        <span class="slot-meta">Full</span>',
            '<a class="slot" href="/booking?date=' + self.iso + '&amp;loc=LOC002&amp;time=17&amp;sel=#step-court">\n'
            '                        <span class="slot-time">22:00</span>\n                        <span class="slot-meta">1 free</span>')
        assert html != self.grid()
        return html

    def grid_with_2300_closed(self):
        """DERIVED: the real grid with 23:00 full."""
        html = re.sub(r'<a class="slot is-selected"\s+href="[^"]*time=18[^"]*">\s*<span class="slot-time">23:00</span>\s*'
                      r'<span class="slot-meta">2 free</span>\s*</a>',
                      '<a class="slot is-disabled" aria-disabled="true"><span class="slot-time">23:00</span>'
                      '<span class="slot-meta">Full</span></a>', self.grid())
        assert html != self.grid()
        return html

    def refused(self, sel=None):
        """SYNTHETIC: the review form re-rendered with an error (court taken)."""
        return self.review(sel).replace('<div class="flow" style="--flow:16px">',
                                        '<div class="flow" style="--flow:16px"><div class="alert alert-danger">'
                                        'Sorry, this court was just booked by someone else.</div>', 1)


class FakeSite(requests.adapters.BaseAdapter):
    """Routes requests to handlers; records everything that was sent."""

    def __init__(self, pages):
        super().__init__()
        self.p = pages
        self.sent = []
        self.grid = {"LOC001": pages.grid(), "LOC002": pages.grid()}
        self.slot_pages = [pages.slot()]
        self.reviews = {}
        self.pending = [pages.pending_empty()]       # served in order; the last one repeats
        self.upcoming = [pages.upcoming_empty()]
        self.posts = []                              # POST responses in order
        # ref -> a booking's own page (or a response tuple). The bot's own new
        # booking (REF) is at the test slot: DERIVED from the real page 7.
        self.details = {REF: pages.detail(REF)}
        self.on_post = None

    @staticmethod
    def _next(queue):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def send(self, request, **kwargs):
        u = urlsplit(request.url)
        q = dict(parse_qsl(u.query, keep_blank_values=True))
        body = request.body.decode() if isinstance(request.body, bytes) else (request.body or "")
        self.sent.append({"method": request.method, "path": u.path, "query": q, "url": request.url,
                          "form": parse_qsl(body, keep_blank_values=True) if body else []})
        if request.method == "POST":
            if self.on_post:
                self.on_post(len([s for s in self.sent if s["method"] == "POST"]))
            if not self.posts:
                raise AssertionError("unexpected POST")
            r = self.posts.pop(0)
            if isinstance(r, BaseException):
                raise r
            return make_response(request, *r)
        if u.path == "/booking" and q.get("time") == "0":
            return make_response(request, 200, [("Content-Type", "text/html")], self.grid[q["loc"]])
        if u.path == "/booking":
            return make_response(request, 200, [("Content-Type", "text/html")], self._next(self.slot_pages))
        if u.path == "/booking/review":
            html = self.reviews.get(q["sel"]) or self.p.review(q["sel"])
            return make_response(request, 200, [("Content-Type", "text/html")], html)
        m = re.fullmatch(r"/bookings/(SPORTS-\d+)", u.path)
        if m and m.group(1) in self.details:
            page = self.details[m.group(1)]
            return make_response(request, *(page if isinstance(page, tuple) else (200, [("Content-Type", "text/html")], page)))
        if u.path == "/bookings":
            queue = self.pending if q.get("tab") == "pending" else self.upcoming
            page = self._next(queue)
            return make_response(request, *(page if isinstance(page, tuple) else (200, [("Content-Type", "text/html")], page)))
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    def close(self):
        pass

    def methods(self):
        return [s["method"] for s in self.sent]

    def posts_sent(self):
        return [s for s in self.sent if s["method"] == "POST"]


def parked_redirect(location=TOPUP):
    return (302, [("Location", location)], "")


def refused_page(html):
    return (200, [("Content-Type", "text/html")], html)


class AutobookTestCase(MonitorTestCase):
    def setUp(self):
        super().setUp()
        self.d = _date.fromisoformat(monitor.datetime.now(monitor.TZ).date().isoformat()) + timedelta(days=2)
        self.p = Pages(self.d)
        self.date = self.p.iso
        self.key = autobook.key_for(self.date, "23:00")
        self.write_config([target(self.date, [("23:00", True)], locations=("LOC002",))])
        self.site = FakeSite(self.p)

    def run_mode(self, mode, iterations=1, raise_ok=False, fail_subjects=(), env=None):
        session = requests.Session()
        session.mount("https://", self.site)
        sent = []
        out = io.StringIO()
        env = {**EMAIL_ENV, **({"AUTOBOOK_MODE": mode} if mode is not None else {}), **(env or {})}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(monitor, "CONFIG_FILE", self.config_file), \
             mock.patch.object(monitor, "STATE_FILE", self.state_file), \
             mock.patch.object(monitor, "send_email", side_effect=lambda s, b, *a: self._send(sent, fail_subjects, s, b)), \
             mock.patch.object(monitor.time, "sleep"), \
             contextlib.redirect_stdout(out):
            code = monitor.run(iterations=iterations, interval=0, jar_path=self.jar, enc_path=self.enc, session=session)
        self.out = out.getvalue()
        return code, sent

    @staticmethod
    def _send(sent, fail_subjects, subject, body):
        if subject.startswith(tuple(fail_subjects)) if fail_subjects else False:
            raise smtplib.SMTPServerDisconnected("smtp down (test) for someone@example.com")
        sent.append({"subject": subject, "body": body})

    def subjects(self, sent):
        return [m["subject"] for m in sent]

    def record(self, key=None, store=autobook.LIVE_KEY):
        return self.state().get(store, {}).get(key or self.key)

    def assert_private(self, sent=()):
        """Booking/payment refs never reach the public state.json or the logs."""
        public = self.state_file.read_text() + self.out
        for secret in (REF, "100000000000000001"):
            self.assertNotIn(secret, public)


class TestParkedEndToEnd(AutobookTestCase):
    def test_parked_path_both_real_redirect_shapes(self):
        for location in (TOPUP, TOPUP_NOV11, bot_session.BASE + TOPUP):
            with self.subTest(location=location):
                self.setUp()
                self._parked(location)

    def _parked(self, location):
        self.site.posts = [parked_redirect(location)]
        self.site.pending = [self.p.pending_empty(), self.p.pending_with_ref()]  # before / after the POST
        code, sent = self.run_mode("live")
        self.assertEqual(code, monitor.EXIT_OK)

        # Exactly one POST, to /booking/confirm, with exactly the body a real
        # browser sent on Nov 13, in that order: "No coach", Wallet, terms ticked.
        posts = self.site.posts_sent()
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["path"], "/booking/confirm")
        self.assertEqual(posts[0]["form"], [("sel", self.p.sel), (f"coach_{self.p.sel}", ""), ("method", "WALLET"),
                                            ("acceptTerms", "true"), ("__RequestVerificationToken", TOKEN)])
        self.assertFalse(any(n.startswith("type_") for n, _ in posts[0]["form"]), "the disabled coach-type select is never sent")

        # The order: lists, slot page, review, POST, lists again — all GETs on
        # allowed paths. The redirect is never followed (no /payment or /wallet GET).
        order = [(s["method"], s["path"], s["query"].get("tab", s["query"].get("time", ""))) for s in self.site.sent]
        self.assertEqual(order, [("GET", "/booking", "0"), ("GET", "/bookings", "pending"), ("GET", "/bookings", ""),
                                 ("GET", "/booking", "18"), ("GET", "/booking/review", ""),
                                 ("POST", "/booking/confirm", ""), ("GET", "/bookings", "pending"), ("GET", "/bookings", ""),
                                 ("GET", f"/bookings/{REF}", "")], "the new pending booking's own page, read before 🟡")

        self.assertEqual(self.subjects(sent), [f"🟡 Parked — pay within 15 min: Crystal Sports G G South-1 {self.date} 23:00 ฿500"])
        body = sent[0]["body"]
        self.assertIn(f"Top up / pay: {location if location.startswith('https://') else bot_session.BASE + location}\n", body,
                      "the parked link is the actual Location")
        self.assertIn(f"{bot_session.BASE}/bookings?tab=pending", body)
        self.assertIn(REF, body)
        rec = self.record()
        self.assertEqual(rec["status"], "parked")
        self.assertEqual(rec["attempts"][0]["hint"], "parked")
        self.assertEqual(rec["attempts"][0]["ref_fp"], autobook.ref_fp(REF))
        self.assert_private()

        # Never again for this key.
        self.state_file.write_text(json.dumps({**self.state(), "known_open": {}}))  # the slot "reopens"
        self.site.sent.clear()
        code, sent = self.run_mode("live")
        self.assertEqual(self.site.posts_sent(), [])
        self.assertNotIn("/booking/review", [s["path"] for s in self.site.sent])


class TestOutcomes(AutobookTestCase):
    def test_paid_path(self):
        # The new booking shows up as the REAL Paid Upcoming row (moved to this
        # slot). The paid redirect itself is SYNTHETIC: we haven't seen it, so
        # here it carries no ref and the ref comes from the new row.
        self.site.posts = [(302, [("Location", "/bookings?tab=upcoming")], "")]
        self.site.upcoming = [self.p.upcoming_empty(), self.p.upcoming_filled("SPORTS-900000000077")]
        code, sent = self.run_mode("live")
        self.assertEqual(self.subjects(sent), [f"🟢 Paid: Crystal Sports G G South-1 {self.date} 23:00 ฿500"])
        self.assertEqual(self.record()["status"], "paid")

    def test_court_taken_tries_one_other_court(self):
        self.site.posts = [refused_page(self.p.refused()), parked_redirect()]
        self.site.pending = [self.p.pending_empty(), self.p.pending_empty(), self.p.pending_with_ref()]
        self.site.upcoming = [self.p.upcoming_empty()]
        code, sent = self.run_mode("live")
        posts = self.site.posts_sent()
        self.assertEqual([dict(p["form"])["sel"] for p in posts], [self.p.sel, self.p.sel2])
        self.assertEqual(self.subjects(sent), [f"🟡 Parked — pay within 15 min: Crystal Sports G G South-3 {self.date} 23:00 ฿500"])
        rec = self.record()
        self.assertEqual([(a["n"], a["court_index"]) for a in rec["attempts"]], [(1, 7), (2, 9)],
                         "the courts' positions in site order (G South-1, G South-3), not their names")

    def test_both_courts_taken_is_failed(self):
        self.site.posts = [refused_page(self.p.refused()), refused_page(self.p.refused(self.p.sel2))]
        code, sent = self.run_mode("live")
        self.assertEqual(len(self.site.posts_sent()), 2, "at most one retry")
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0]["subject"].startswith("🔴 Failed: "))
        self.assertEqual(self.record()["status"], "failed")

    def test_never_more_than_two_posts_even_with_more_free_courts(self):
        three_free = self.p.slot().replace(  # DERIVED: G South-2 free as well
            '<a class="court-tile is-disabled" aria-disabled="true" aria-pressed="false"\n                           aria-label="G South-2 booked">',
            f'<a class="court-tile" href="/booking?date={self.date}&amp;loc=LOC002&amp;time=18&amp;sel={self.p.ymd}-299#step-court" aria-pressed="false"\n                           aria-label="G South-2">')
        self.assertNotEqual(three_free, self.p.slot())
        self.site.slot_pages = [three_free]
        self.site.reviews[f"{self.p.ymd}-299"] = self.p.review().replace(self.p.sel, f"{self.p.ymd}-299").replace("G South-1", "G South-2")
        self.site.posts = [refused_page(self.p.refused()), refused_page(self.p.refused(self.p.sel2)), parked_redirect()]
        code, sent = self.run_mode("live")
        self.assertEqual(len(self.site.posts_sent()), 2)
        self.assertTrue(sent[0]["subject"].startswith("🔴 Failed: "))

    def test_refused_and_no_other_court_is_failed_after_one_post(self):
        only_one_free = self.p.slot().replace(f'sel={self.p.sel2}#step-court', '#step-court"  aria-disabled="true')
        self.site.slot_pages = [self.p.slot(), only_one_free]
        self.site.posts = [refused_page(self.p.refused())]
        code, sent = self.run_mode("live")
        self.assertEqual(len(self.site.posts_sent()), 1)
        self.assertTrue(sent[0]["subject"].startswith("🔴 Failed: "))

    def test_unclear_outcomes_are_never_failed(self):
        cases = {
            "parked redirect but not listed": [parked_redirect()],
            "network error after sending": [requests.exceptions.ConnectionError("reset")],
            "server error": [(500, [], "oops")],
            "re-rendered without an error": [refused_page(self.p.review())],
            "redirect somewhere unexpected": [(302, [("Location", "/booking")], "")],
        }
        for label, posts in cases.items():
            with self.subTest(label):
                self.setUp()
                self.site.posts = list(posts)
                code, sent = self.run_mode("live")
                self.assertEqual(len(self.site.posts_sent()), 1, "never a second POST when unclear")
                self.assertEqual(len(sent), 1)
                self.assertTrue(sent[0]["subject"].startswith("🟠 Unclear — check My bookings: "), sent[0]["subject"])
                self.assertIn("NOT be retried", sent[0]["body"])
                self.assertEqual(self.record()["status"], "unclear")


class TestNoPostWhenThePageIsWrong(AutobookTestCase):
    def assert_mismatch(self, review_html, *expected):
        self.site.reviews[self.p.sel] = review_html
        code, sent = self.run_mode("live")
        self.assertEqual(self.site.posts_sent(), [], "no POST when the review page is wrong")
        self.assertEqual(len(sent), 1)
        self.assertIn("booking form changed / page mismatch", sent[0]["subject"])
        for e in expected:
            self.assertIn(e, sent[0]["body"])
        self.assertEqual(self.record()["status"], "aborted_mismatch")

    def test_extra_required_field(self):
        self.assert_mismatch(self.p.review().replace(
            '<input type="hidden" name="sel"', '<input type="text" name="phone" required /><input type="hidden" name="sel"'),
            "unexpected field 'phone'")

    def test_extra_prefilled_hidden_field(self):
        self.assert_mismatch(self.p.review().replace(
            '<input type="hidden" name="sel"', '<input type="hidden" name="promo" value="X1" /><input type="hidden" name="sel"'),
            "unexpected field 'promo'", "pre-filled 'X1'")

    def test_form_changes(self):
        r = self.p.review()
        variants = {
            "coach pre-selected": (r.replace('<option value="1001"', '<option selected value="1001"'), "is pre-selected"),
            "coach field missing": (re.sub(r'<select class="input" name="coach_[^"]*">.*?</select>', "", r, flags=re.S), "coach field"),
            "coach field renamed": (r.replace(f'name="coach_{self.p.sel}"', 'name="coach"'), "coach field"),
            "coach field disabled": (r.replace(f'name="coach_{self.p.sel}"', f'name="coach_{self.p.sel}" disabled'), "coach field"),
            "coach-type select enabled": (r.replace(f'name="type_{self.p.sel}" disabled', f'name="type_{self.p.sel}"'), "a browser would send it"),
            "terms value changed": (r.replace('name="acceptTerms" value="true"', 'name="acceptTerms" value="on"'), "terms checkbox"),
            "terms checkbox gone": (r.replace('name="acceptTerms"', 'name="accept"'), "terms checkbox"),
            "no Wallet option": (r.replace('value="WALLET"', 'value="WALLET2"'), "no WALLET"),
            "posts somewhere else": (r.replace('action="/booking/confirm"', 'action="/booking/confirm2"'), "0 forms"),
            "token missing": (re.sub(r'<input name="__RequestVerificationToken"[^>]*/></form>', "</form>", r), "anti-forgery"),
            "coach now required": (r.replace(f'name="coach_{self.p.sel}"', f'name="coach_{self.p.sel}" required'), "now required"),
            "a second court in the cart": (r.replace('<div class="card">\n            <div class="card-title">Payment method',
                                                     '<div class="card"><div><b>Crystal Sports G · G South-3</b>'
                                                     f'<div class="text-muted text-sm">{self.p.label} · 23:00 – 24:00</div></div>'
                                                     '<b>฿500</b></div><div class="card">\n            <div class="card-title">Payment method'),
                                           "2 items"),
            "no price": (r.replace("<b>฿500</b></div>", "<b>free?</b></div>", 1), "no price"),
        }
        for label, (html, expected) in variants.items():
            with self.subTest(label):
                self.assertNotEqual(html, r, "the variant must actually change the page")
                self.setUp()
                self.assert_mismatch(html, expected)

    def test_review_for_a_different_slot(self):
        r = self.p.review()
        other_day = self.p.label.replace(str(self.d.day), str((self.d + timedelta(days=1)).day), 1)
        variants = {
            "different date": r.replace(f"{self.p.label} · 23:00", f"{other_day} · 23:00"),
            "different time": r.replace(f"{self.p.label} · 23:00 – 24:00", f"{self.p.label} · 22:00 – 23:00"),
            "different branch": r.replace("Crystal Sports G · G South-1", "Crystal Sports · G South-1"),
            "different court": r.replace("Crystal Sports G · G South-1", "Crystal Sports G · G South-2"),
            "different sel": r.replace(f'name="sel" value="{self.p.sel}"', f'name="sel" value="{self.p.sel2}"'),
        }
        for label, html in variants.items():
            with self.subTest(label):
                self.assertNotEqual(html, r)
                self.setUp()
                self.assert_mismatch(html)

    def test_slot_page_for_a_different_time(self):
        self.site.slot_pages = [self.p.slot().replace('<span class="slot-time">23:00</span>\n                        <span class="slot-meta">2 free',
                                                      '<span class="slot-time">22:00</span>\n                        <span class="slot-meta">2 free')]
        self.site.grid["LOC002"] = self.p.grid()
        code, sent = self.run_mode("live")
        self.assertEqual(self.site.posts_sent(), [])
        self.assertNotIn("/booking/review", [s["path"] for s in self.site.sent])
        self.assertIn("selected time is '22:00'", sent[0]["body"])


class TestExistingBookings(AutobookTestCase):
    def assert_skipped(self, sent):
        self.assertEqual(self.site.posts_sent(), [])
        self.assertNotIn("/booking/review", [s["path"] for s in self.site.sent])
        self.assertEqual(self.subjects(sent), [f"ℹ️ Already booked: {self.date} 23:00 — not auto-booking"])
        self.assertEqual(self.record()["status"], "skipped_existing")

    def assert_booked(self):
        self.assertEqual(len(self.site.posts_sent()), 1)
        self.assertEqual(self.record()["status"], "parked")

    def book_ok(self):
        self.site.posts = [parked_redirect()]
        self.site.pending = [self.p.pending_empty(), self.p.pending_with_ref()]

    def detail_gets(self):
        return [s["path"] for s in self.site.sent if s["path"].startswith("/bookings/")]

    def test_upcoming_booking_at_that_time_skips_once(self):
        self.site.upcoming = [self.p.upcoming_filled()]  # the REAL row, moved to this date
        code, sent = self.run_mode("live")
        self.assert_skipped(sent)
        self.assertEqual(self.detail_gets(), [], "the Upcoming row shows the date: no detail page needed")
        self.state_file.write_text(json.dumps({**self.state(), "known_open": {}}))
        code, sent = self.run_mode("live")
        self.assertEqual(sent and [m for m in sent if "ℹ️" in m["subject"]], [])

    def test_upcoming_booking_at_that_time_at_the_other_location_or_court_skips(self):
        for loc_name, court in (("Crystal Sports", "Court 4"), ("Crystal Sports G", "G North-2")):
            with self.subTest(loc=loc_name, court=court):
                self.setUp()
                self.site.upcoming = [self.p.upcoming_filled(loc_name=loc_name, court=court)]
                code, sent = self.run_mode("live")
                self.assert_skipped(sent)

    def test_my_own_booking_at_another_time_or_date_does_not_block(self):
        for label, kw in (("another time", {"time": "21:00"}), ("another date", {"d": self.d + timedelta(days=1)})):
            with self.subTest(label):
                self.setUp()
                self.site.upcoming = [self.p.upcoming_filled(**kw)]
                self.book_ok()
                code, sent = self.run_mode("live")
                self.assert_booked()
                self.assertEqual([m for m in self.subjects(sent) if m.startswith("ℹ️")], [])

    def test_pending_booking_is_read_from_its_own_page(self):
        other = "SPORTS-900000000099"
        cases = {"same time": (self.p.detail(other), "skip"),
                 "same time, other location": (self.p.detail(other, loc_name="Crystal Sports", court="Court 4"), "skip"),
                 "another time": (self.p.detail(other, time="20:00"), "book"),
                 "another date": (self.p.detail(other, d=self.d + timedelta(days=3)), "book")}
        for label, (page, expect) in cases.items():
            with self.subTest(label):
                self.setUp()
                self.site.details[other] = page
                if expect == "skip":
                    self.site.pending = [self.p.pending_with_ref(other)]
                    code, sent = self.run_mode("live")
                    self.assert_skipped(sent)
                else:
                    self.site.posts = [parked_redirect()]
                    # before: someone else's pending booking; after: that one plus ours
                    both = self.p.pending_with_ref(other).replace(
                        '<a class="bk-row"', self.p.pending_with_ref().split('<div id="bookingList">')[1].split("</div>")[0]
                        + '<a class="bk-row"', 1)
                    self.site.pending = [self.p.pending_with_ref(other), both]
                    code, sent = self.run_mode("live")
                    self.assert_booked()
                self.assertEqual(self.detail_gets()[:1], [f"/bookings/{other}"], "exactly the real detail path")
                self.assertTrue(all(s["method"] == "GET" for s in self.site.sent if s["path"].startswith("/bookings/")))

    def test_unreadable_listed_booking_blocks_and_retries(self):
        # The REAL Awaiting-payment row shows no court date/time, and its own page can't be read either.
        other = "SPORTS-900000000099"
        unreadable = {
            "server error": (500, [], "oops"),
            "page without a date": self.p.detail(other).replace(f"{self.p.label} · 23:00–24:00", "—"),
            "page for a different booking": self.p.detail("SPORTS-900000000098"),
            "unexpected page": "<html><body><p>Hello</p></body></html>",
        }
        for label, page in unreadable.items():
            with self.subTest(label):
                self.setUp()
                self.site.details[other] = page
                self.site.pending = [self.p.pending_with_ref(other)]
                code, sent = self.run_mode("live", iterations=2)
                self.assertEqual(self.site.posts_sent(), [])
                self.assertNotIn("/booking/review", [s["path"] for s in self.site.sent])
                self.assertEqual(len([m for m in sent if m["subject"].startswith("ℹ️ Not auto-booking")]), 1)
                self.assertIsNone(self.record(), "not a final outcome — nothing was submitted")
                self.assertIn(self.key, self.state()[autobook.RETRY_KEY])
                self.assertEqual([s["path"] for s in self.site.sent].count("/bookings"), 4, "re-checked on the second pass")
        # Once the list is clear, the deferred key is booked.
        self.site.pending = [self.p.pending_empty(), self.p.pending_with_ref()]
        self.site.posts = [parked_redirect()]
        code, sent = self.run_mode("live")
        self.assert_booked()
        self.assertNotIn(self.key, self.state().get(autobook.RETRY_KEY, {}))

    def test_the_bots_own_booking_is_not_looked_up_and_never_blocks(self):
        # The bot parked 22:00 earlier (its ref fingerprint is in state); that
        # row is on the Awaiting-payment list without a date/time.
        own = {autobook.key_for(self.date, "22:00"): {"status": "parked", "date": self.date, "time": "22:00",
                                                    "attempts": [{"ref_fp": autobook.ref_fp(REF)}]}}
        self.state_file.write_text(json.dumps({autobook.LIVE_KEY: own}))
        self.site.pending = [self.p.pending_with_ref(REF)]
        code, sent = self.run_mode("dry_run")
        self.assertEqual([m for m in self.subjects(sent) if m.startswith("ℹ️")], [])
        self.assertEqual(self.detail_gets(), [])
        self.assertIn(f"🧪 Would have booked Crystal Sports G G South-1 {self.date} 23:00 ฿500", self.subjects(sent))


# SYNTHETIC: a 200 page that is none of the site's pages (a verification /
# interstitial page). It has a form and a hidden input, but no login form.
INTERSTITIAL = ('<html lang="en"><head><title>One moment</title></head><body><h1>Checking your browser</h1>'
                '<form action="/verify" method="post"><input type="hidden" name="t" value="x" />'
                '<button type="submit">Continue</button></form></body></html>')


class TestUnrecognisedPagesFailClosed(AutobookTestCase):
    """Any 200 page that isn't the page asked for is UNREADABLE: no booking
    that pass, never "you have no bookings"."""

    def assert_not_booked_info_once(self, reason):
        code, sent = self.run_mode("live", iterations=2)
        self.assertNotEqual(code, monitor.EXIT_SESSION, "not a login page")
        self.assertEqual(self.site.posts_sent(), [])
        self.assertNotIn("/booking/review", [s["path"] for s in self.site.sent])
        info = [m for m in sent if m["subject"].startswith("ℹ️ Not auto-booking")]
        self.assertEqual([m["subject"] for m in info],
                         [f"ℹ️ Not auto-booking {self.date} 23:00 — can't check your bookings"], "ℹ️ exactly once")
        self.assertIn("didn't look as expected", info[0]["body"])
        self.assertNotIn("Checking your browser", info[0]["body"])
        self.assertIsNone(self.record(), "not a final outcome — nothing was submitted")
        self.assertEqual(self.state()[autobook.RETRY_KEY][self.key]["reason"], reason)
        self.assertGreaterEqual([s["path"] for s in self.site.sent].count("/bookings"), 2, "re-checked on the second pass")
        self.assertIn(f"not booking: {reason}", self.out)

    def test_pending_list_is_an_unrelated_page(self):
        self.site.pending = [INTERSTITIAL]
        self.assert_not_booked_info_once("bookings_page_unrecognised:pending")

    def test_upcoming_list_is_an_unrelated_page(self):
        self.site.upcoming = [INTERSTITIAL]
        self.assert_not_booked_info_once("bookings_page_unrecognised:upcoming")

    def test_pending_request_answered_with_the_upcoming_tab(self):
        self.site.pending = [self.p.upcoming_empty()]   # a real list, but not the one asked for
        self.assert_not_booked_info_once("bookings_page_unrecognised:pending")

    def test_upcoming_request_answered_with_the_pending_tab(self):
        self.site.upcoming = [self.p.pending_empty()]
        self.assert_not_booked_info_once("bookings_page_unrecognised:upcoming")

    def test_booked_once_the_lists_are_readable_again(self):
        self.site.pending = [INTERSTITIAL]
        self.run_mode("live")
        self.assertIn(self.key, self.state()[autobook.RETRY_KEY])
        self.site = FakeSite(self.p)
        self.site.posts = [parked_redirect()]
        self.site.pending = [self.p.pending_empty(), self.p.pending_with_ref()]
        self.run_mode("live")
        self.assertEqual(len(self.site.posts_sent()), 1)
        self.assertEqual(self.record()["status"], "parked")

    def test_review_page_is_an_unrelated_page(self):
        self.site.reviews[self.p.sel] = INTERSTITIAL
        code, sent = self.run_mode("live")
        self.assertEqual(self.site.posts_sent(), [], "no POST")
        self.assertEqual(self.record()["status"], "aborted_mismatch")
        self.assertIn("booking form changed / page mismatch", sent[0]["subject"])

    def test_slot_page_is_an_unrelated_page(self):
        self.site.slot_pages = [INTERSTITIAL]
        code, sent = self.run_mode("live")
        self.assertEqual(self.site.posts_sent(), [], "no POST")
        self.assertNotIn("/booking/review", [s["path"] for s in self.site.sent])
        self.assertNotEqual(code, monitor.EXIT_SESSION)
        self.assertIn(self.record()["status"], autobook.TERMINAL)

    def test_lists_unrelated_after_the_post_is_unclear_never_retried(self):
        self.site.posts = [parked_redirect()]
        self.site.pending = [self.p.pending_empty(), INTERSTITIAL]
        code, sent = self.run_mode("live", iterations=2)
        self.assertEqual(len(self.site.posts_sent()), 1, "never a second POST")
        self.assertEqual(self.record()["status"], "unclear")
        self.assertNotIn(self.key, self.state().get(autobook.RETRY_KEY, {}))


class TestListParsing(unittest.TestCase):
    """The real filled Upcoming row and booking page, parsed directly."""

    def test_real_upcoming_row(self):
        page = booking_page.parse_bookings_list(fixture("6_bookings_upcoming_filled.html"), today=_date(2026, 11, 13))
        self.assertEqual(page["tab"], "upcoming")
        self.assertEqual(page["rows"], [{
            "ref": REF2, "status": "Paid", "slot": ("2026-11-15", "23:00", "24:00"), "loc_name": "Crystal Sports G",
            "court": "G South-1", "text": page["rows"][0]["text"]}])

    def test_the_year_is_the_next_occurrence_on_or_after_today(self):
        html = fixture("6_bookings_upcoming_filled.html")
        cases = [(_date(2026, 11, 15), "2026-11-15"),   # on the day itself: this year
                 (_date(2026, 11, 16), "2027-11-15"),   # the day after: next year
                 (_date(2025, 12, 31), "2026-11-15")]
        for today, want in cases:
            with self.subTest(today=today):
                self.assertEqual(booking_page.parse_bookings_list(html, today=today)["rows"][0]["slot"][0], want)
        jan = html.replace("<b>15</b><span>Nov</span>", "<b>2</b><span>Jan</span>")
        self.assertEqual(booking_page.parse_bookings_list(jan, today=_date(2026, 12, 30))["rows"][0]["slot"],
                         ("2027-01-02", "23:00", "24:00"), "Dec -> Jan")
        self.assertEqual(booking_page.infer_play_date(29, 2, _date(2026, 3, 1)), _date(2028, 2, 29))
        self.assertIsNone(booking_page.infer_play_date(31, 2, _date(2026, 3, 1)))

    def test_bangkok_today_is_the_default(self):
        with mock.patch.object(booking_page, "bangkok_today", return_value=_date(2026, 11, 16)):
            row = booking_page.parse_bookings_list(fixture("6_bookings_upcoming_filled.html"))["rows"][0]
        self.assertEqual(row["slot"], ("2027-11-15", "23:00", "24:00"))

    def test_real_awaiting_payment_row_has_no_slot(self):
        rows = booking_page.parse_bookings_list(fixture("5_bookings_awaiting.html"))["rows"]
        self.assertEqual([(r["ref"], r["status"], r["slot"]) for r in rows], [(REF, "Awaiting payment", None)])

    def test_real_booking_page(self):
        page = booking_page.parse_booking_detail(fixture("7_booking_detail.html"))
        self.assertEqual(page, {"ref": REF2, "status": "Paid", "lines": [
            {"loc_name": "Crystal Sports G", "court": "G South-1", "slot": ("2026-11-15", "23:00", "24:00")}]})


class TestRedirectHint(AutobookTestCase):
    def test_parked_location_shapes(self):
        yes = [TOPUP, TOPUP_NOV11, bot_session.BASE + TOPUP, "/payment/SPORTS-1/TopUp-Shortfall"]
        no = ["/bookings?tab=pending", "/payment/SPORTS-1/receipt", "/wallet/topup/123", "", None]
        for loc in yes:
            self.assertTrue(autobook.is_parked_location(loc), loc)
        for loc in no:
            self.assertFalse(autobook.is_parked_location(loc), loc)

    def test_the_lists_decide_not_the_redirect(self):
        # A topup redirect without a ref is not the parked hint, but the new
        # booking under Awaiting payment still makes it 🟡 (ref from the list).
        self.site.posts = [parked_redirect("/wallet/topup/100000000000000001")]
        self.site.pending = [self.p.pending_empty(), self.p.pending_with_ref()]
        code, sent = self.run_mode("live")
        self.assertEqual(self.record()["attempts"][0]["hint"], "other")
        self.assertTrue(sent[0]["subject"].startswith("🟡 Parked"))
        self.assertNotIn("Top up / pay:", sent[0]["body"])
        self.assertIn(REF, sent[0]["body"])


class TestOncePerDateTime(AutobookTestCase):
    def test_both_locations_open_books_only_one(self):
        self.write_config([target(self.date, [("23:00", True)], locations=("LOC001", "LOC002"))])
        # LOC001 is checked first, so it's the one booked: give it Crystal Sports pages (DERIVED from the G ones).
        loc1_card = f'<a class="choice-card" href="/booking?date={self.date}&amp;loc=LOC001'
        self.site.slot_pages = [self.p.slot().replace('class="choice-card is-selected"', 'class="choice-card"')
                                .replace(loc1_card, loc1_card.replace('"choice-card"', '"choice-card is-selected"'))]
        self.site.reviews[self.p.sel] = self.p.review().replace("Crystal Sports G · G South-1", "Crystal Sports · G South-1")
        self.site.posts = [parked_redirect()]
        self.site.pending = [self.p.pending_empty(), self.p.pending_with_ref()]
        code, sent = self.run_mode("live", iterations=2)
        self.assertEqual(len(self.site.posts_sent()), 1)
        self.assertEqual(len([s for s in self.site.sent if s["path"] == "/booking/review"]), 1)
        self.assertEqual(self.record()["loc"], "LOC001")
        self.assertEqual(self.record()["status"], "parked")
        # And a later run, with both still open and "reopened", doesn't book the other location.
        self.state_file.write_text(json.dumps({**self.state(), "known_open": {}}))
        self.site.sent.clear()
        self.run_mode("live")
        self.assertEqual(self.site.posts_sent(), [])


class TestCrashAfterWriteAhead(AutobookTestCase):
    def test_a_run_that_dies_mid_booking_is_never_retried(self):
        def die(_n):
            on_disk = json.loads(self.state_file.read_text())
            self.assertEqual(on_disk[autobook.LIVE_KEY][self.key]["status"], "in_progress",
                             "the write-ahead record must be on disk BEFORE the POST")
            raise KeyboardInterrupt("runner killed")
        self.site.on_post = die
        self.site.posts = [parked_redirect()]
        with self.assertRaises(KeyboardInterrupt):
            self.run_mode("live")
        self.assertEqual(self.record()["status"], "in_progress")

        self.site.on_post = None
        self.site.sent.clear()
        self.state_file.write_text(json.dumps({**self.state(), "known_open": {}}))
        code, sent = self.run_mode("live")
        self.assertEqual(self.site.posts_sent(), [])
        self.assertEqual(self.subjects(sent)[0], f"🟠 Unclear — check My bookings: Crystal Sports G {self.date} 23:00")
        self.assertEqual(len([m for m in sent if m["subject"].startswith("🟠")]), 1)
        self.assertEqual(self.record()["status"], "interrupted")

        code, sent = self.run_mode("live")
        self.assertEqual(self.site.posts_sent(), [])
        self.assertEqual([m for m in sent if m["subject"].startswith("🟠")], [], "🟠 only once")

    def test_interrupted_email_that_fails_is_sent_on_a_later_run(self):
        self.site.on_post = lambda n: (_ for _ in ()).throw(KeyboardInterrupt())
        self.site.posts = [parked_redirect()]
        with self.assertRaises(KeyboardInterrupt):
            self.run_mode("live")
        self.site.on_post = None
        self.site.sent.clear()
        code, sent = self.run_mode("live", fail_subjects=("🟠",))
        self.assertEqual(self.record()["status"], "in_progress", "not marked interrupted: nobody was told yet")
        self.assertEqual(self.site.posts_sent(), [], "still never retried")
        code, sent = self.run_mode("live")
        self.assertEqual(len([m for m in sent if m["subject"].startswith("🟠")]), 1)
        self.assertEqual(self.record()["status"], "interrupted")

    def test_write_ahead_that_cannot_be_saved_means_no_post(self):
        real_save = monitor.save_state
        calls = {"n": 0}

        def save(state, *a, **kw):
            if state.get(autobook.LIVE_KEY, {}).get(self.key, {}).get("status") == "in_progress":
                raise OSError("disk full")
            return real_save(state, *a, **kw)
        with mock.patch.object(monitor, "save_state", side_effect=save):
            code, sent = self.run_mode("live")
        self.assertEqual(self.site.posts_sent(), [])
        self.assertTrue(sent[0]["subject"].startswith("🔴 Failed: "))
        self.assertIn("did NOT send", sent[0]["body"])


class TestModes(AutobookTestCase):
    def test_mode_off_is_todays_behaviour(self):
        for mode in (None, "", "off", "OFF", "nonsense"):
            with self.subTest(mode=mode):
                self.setUp()
                code, sent = self.run_mode(mode)
                self.assertEqual(self.site.methods(), ["GET"], "only the grid page — no auto-book requests at all")
                self.assertEqual(len(sent), 1)
                self.assertIn(monitor.AUTOBOOK_PAUSED_NOTE, sent[0]["body"])
                self.assertFalse({autobook.LIVE_KEY, autobook.DRY_KEY, autobook.RETRY_KEY} & set(self.state()))

    def test_dry_run_checks_everything_but_never_posts(self):
        code, sent = self.run_mode("dry_run", iterations=2)
        self.assertNotIn("POST", self.site.methods())
        self.assertIn("/booking/review", [s["path"] for s in self.site.sent])
        subjects = self.subjects(sent)
        self.assertIn(f"🧪 Would have booked Crystal Sports G G South-1 {self.date} 23:00 ฿500", subjects)
        self.assertEqual(len([s for s in subjects if s.startswith("🧪")]), 1)
        self.assertTrue(any(s.startswith("🎾") for s in subjects), "dry run still sends the slot-open email")
        self.assertEqual(self.record(store=autobook.DRY_KEY)["status"], "dry_run")
        self.assertIsNone(self.record(), "a dry run never blocks a later live booking")
        # A later run doesn't repeat the 🧪 email.
        self.state_file.write_text(json.dumps({**self.state(), "known_open": {}}))
        code, sent = self.run_mode("dry_run")
        self.assertFalse(any(s.startswith("🧪") for s in self.subjects(sent)))

    def test_dry_run_mismatch_still_reports_and_never_posts(self):
        self.site.reviews[self.p.sel] = self.p.review().replace('name="acceptTerms"', 'name="accept"')
        code, sent = self.run_mode("dry_run")
        self.assertNotIn("POST", self.site.methods())
        self.assertTrue(any("booking form changed" in s for s in self.subjects(sent)))

    def test_unstarred_slots_never_trigger_auto_book(self):
        self.write_config([target(self.date, [("23:00", False)], locations=("LOC002",))])
        for mode in ("dry_run", "live"):
            with self.subTest(mode=mode):
                self.site = FakeSite(self.p)
                self.state_file.write_text("{}")
                code, sent = self.run_mode(mode)
                self.assertEqual(self.site.methods(), ["GET"])
                self.assertEqual([s["path"] for s in self.site.sent], ["/booking"])

    def test_live_slot_open_email_omits_a_slot_that_got_its_own_result_email(self):
        self.write_config([target(self.date, [("23:00", True), ("22:00", False)], locations=("LOC002",))])
        self.site.grid["LOC002"] = self.p.grid().replace(
            '<a class="slot is-disabled" aria-disabled="true">\n                        <span class="slot-time">22:00</span>\n                        <span class="slot-meta">Full</span>',
            '<a class="slot" href="/booking?date=' + self.date + '&amp;loc=LOC002&amp;time=17&amp;sel=#step-court">\n'
            '                        <span class="slot-time">22:00</span>\n                        <span class="slot-meta">1 free</span>')
        self.site.posts = [parked_redirect()]
        self.site.pending = [self.p.pending_empty(), self.p.pending_with_ref()]
        code, sent = self.run_mode("live")
        slot_email = [m for m in sent if m["subject"].startswith("🎾")]
        self.assertEqual(len(slot_email), 1)
        self.assertIn("22:00", slot_email[0]["body"])
        self.assertNotIn("23:00", slot_email[0]["body"])
        self.assertIn(f"{bot_session.BASE}/booking?date={self.date}&loc=LOC002&time=17&sel=#step-court", slot_email[0]["body"],
                      "deep link: the slot's own href")


# --- third review ------------------------------------------------------------------

THAI = re.compile("[฀-๿]")
OTHER = "SPORTS-900000000099"


def page_texts():
    """Every text node and attribute value of 8+ characters in the scrubbed
    fixtures — "page text" that must never end up in state.json."""
    out = set()
    for f in FIX.glob("*.html"):
        for n in booking_page.dom(f.read_text(encoding="utf-8")).iter():
            for c in n.children:
                if isinstance(c, str) and len(" ".join(c.split())) >= 8:
                    out.add(" ".join(c.split()))
            if n.tag not in ("script", "style"):
                out.update(v for k, v in n.attrs.items() if k not in ("class", "style") and v and len(v) >= 8)
    ours = set(monitor.LOCATIONS.values())  # our own constants (the site shows them too)
    return {t for t in out if t not in ours}


def strings_in(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from strings_in(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from strings_in(v)
    elif isinstance(obj, str):
        yield obj


class TestPrivacy(AutobookTestCase):
    def test_logs_carry_no_refs_or_ref_paths(self):
        self.site.details[OTHER] = self.p.detail(OTHER, time="20:00")   # someone else's booking, other time
        both = self.p.pending_with_ref(OTHER).replace(
            '<a class="bk-row"', self.p.pending_with_ref().split('<div id="bookingList">')[1].split("</div>")[0]
            + '<a class="bk-row"', 1)
        self.site.pending = [self.p.pending_with_ref(OTHER), both]
        self.site.posts = [parked_redirect(TOPUP_NOV11)]
        code, sent = self.run_mode("live")
        self.assertTrue(sent[0]["subject"].startswith("🟡"))
        for secret in (REF, OTHER, "/bookings/SPORTS", "100000000000000001", "SPORTS-"):
            self.assertNotIn(secret, self.out)
        self.assertIn(f"booking ref:{autobook.ref_fp(OTHER)}: http 200", self.out)
        self.assertIn(REF, sent[0]["body"], "the email (private) still carries the ref")

    def test_a_failing_booking_page_is_logged_by_fingerprint_and_status_only(self):
        for label, page, want in (("http error", (503, [], "busy"), "http_503"),
                                  ("network error", requests.exceptions.ConnectionError(
                                      f"Max retries exceeded with url: /bookings/{OTHER} (someone@example.com)"),
                                   "network_error")):
            with self.subTest(label):
                self.setUp()
                if isinstance(page, BaseException):
                    original = self.site.send

                    def send(request, _orig=original, _exc=page, **kw):
                        if request.url.endswith(f"/bookings/{OTHER}"):
                            self.site.sent.append({"method": "GET", "path": f"/bookings/{OTHER}", "query": {}, "url": "", "form": []})
                            raise _exc
                        return _orig(request, **kw)
                    self.site.send = send
                else:
                    self.site.details[OTHER] = page
                self.site.pending = [self.p.pending_with_ref(OTHER)]
                code, sent = self.run_mode("live")
                self.assertIn(f"booking ref:{autobook.ref_fp(OTHER)}: {want}", self.out)
                for secret in (OTHER, "/bookings/SPORTS", "Max retries", "example.com", "busy"):
                    self.assertNotIn(secret, self.out)

    def test_redact_fingerprints_booking_and_payment_refs(self):
        text = bot_session.redact(f"GET /bookings/{REF} then {bot_session.BASE}{TOPUP_NOV11}")
        self.assertNotIn("SPORTS-", text)
        self.assertNotIn("100000000000000001", text)
        self.assertIn(f"ref:{autobook.ref_fp(REF)}", text)

    def test_no_log_line_prints_an_exception_message_or_page_text(self):
        # Static: log()/say() calls and the probe's diagnostic lines never
        # interpolate a bare exception ({e}) or a page title.
        root = Path(__file__).resolve().parent
        offenders = []
        for name in ("monitor.py", "autobook.py", "booking_page.py", "probe_new_site.py"):
            for i, line in enumerate((root / name).read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"\b(log|say|diag\.append)\(f?['\"].*(\{e\}|\{e!r\}|\{e!s\}|\['title'\]\}|\[\"title\"\]\})", line):
                    offenders.append(f"{name}:{i}: {line.strip()}")
        self.assertEqual(offenders, [])

    def test_an_unexpected_page_is_logged_without_its_text(self):
        self.site.grid["LOC002"] = "<html><head><title>Hello Test Member SECRET-TITLE</title></head><body>?</body></html>"
        self.run_mode("off")
        self.assertIn("unexpected page", self.out)
        self.assertNotIn("SECRET-TITLE", self.out)

    def test_email_failure_is_logged_without_the_smtp_message(self):
        code, sent = self.run_mode("off", fail_subjects=("🎾",))
        self.assertIn("alert email failed: SMTPServerDisconnected", self.out)
        self.assertNotIn("example.com", self.out)


class TestStateHoldsNoSiteText(AutobookTestCase):
    """state.json is public: our own values and fixed codes only."""

    def assert_state_clean(self, label):
        texts = page_texts()
        state = self.state()
        for s in strings_in(state):
            with self.subTest(label, value=s[:60]):
                self.assertIsNone(re.search(r"SPORTS-\d+", s), "a booking ref in state")
                self.assertIsNone(THAI.search(s), "Thai page text in state")
                self.assertNotIn("100000000000000001", s)
                for t in texts:
                    self.assertNotIn(t, s, "page text in state")
                for injected in ("PREFILLED-SECRET-VALUE", "Sorry, this court", "someone@example.com", "FAKE-ANTIFORGERY"):
                    self.assertNotIn(injected, s)
        return state

    def test_attempts_hold_only_our_own_keys(self):
        self.site.posts = [refused_page(self.p.refused()), parked_redirect()]
        self.site.pending = [self.p.pending_empty(), self.p.pending_empty(), self.p.pending_with_ref()]
        self.run_mode("live")
        attempts = self.record()["attempts"]
        self.assertEqual(len(attempts), 2)
        for a in attempts:
            self.assertLessEqual(set(a), {"n", "court_index", "at", "http", "hint", "ref_fp", "refused"})
        for v in strings_in(self.state()):
            self.assertIsNone(re.fullmatch(r"\d{8}-\d{1,6}", v), f"a court's sel in state: {v}")
            self.assertNotIn(self.p.sel, v)
            self.assertNotIn(self.p.sel2, v)

    def test_page_texts_are_actually_collected(self):
        texts = page_texts()
        for expected in ("Hi Test Member", "Awaiting payment", "I have read and accept the terms of service"):
            self.assertTrue(any(expected in t for t in texts), expected)

    def test_every_outcome_writes_only_codes(self):
        r = self.p.review()
        scenarios = {
            "parked": lambda: (setattr(self.site, "posts", [parked_redirect()]),
                               setattr(self.site, "pending", [self.p.pending_empty(), self.p.pending_with_ref()])),
            "refused twice": lambda: setattr(self.site, "posts", [refused_page(self.p.refused()), refused_page(self.p.refused(self.p.sel2))]),
            "unclear (network)": lambda: setattr(self.site, "posts", [requests.exceptions.ConnectionError(f"reset /bookings/{REF}")]),
            "unexpected pre-filled field": lambda: self.site.reviews.__setitem__(self.p.sel, r.replace(
                '<input type="hidden" name="sel"', '<input type="hidden" name="promo code!" value="PREFILLED-SECRET-VALUE" />'
                                                   '<input type="hidden" name="sel"')),
            "coach pre-selected": lambda: self.site.reviews.__setitem__(self.p.sel, r.replace('<option value="1001"', '<option selected value="1001"')),
            "different date": lambda: self.site.reviews.__setitem__(self.p.sel, r.replace(f"{self.p.label} · 23:00", "Mon 1 Jan 2029 · 23:00")),
            "existing booking": lambda: setattr(self.site, "upcoming", [self.p.upcoming_filled()]),
            "unreadable booking": lambda: (self.site.details.__setitem__(OTHER, (500, [], "x")),
                                           setattr(self.site, "pending", [self.p.pending_with_ref(OTHER)])),
            "paid": lambda: (setattr(self.site, "posts", [(302, [("Location", "/bookings")], "")]),
                             setattr(self.site, "upcoming", [self.p.upcoming_empty(), self.p.upcoming_filled("SPORTS-900000000077")])),
        }
        codes = {}
        for label, arrange in scenarios.items():
            self.setUp()
            arrange()
            self.run_mode("live")
            state = self.assert_state_clean(label)
            rec = state.get(autobook.LIVE_KEY, {}).get(self.key) or state.get(autobook.RETRY_KEY, {}).get(self.key)
            codes[label] = (rec.get("status"), rec.get("reason"), rec.get("problems"))
        self.assertEqual(codes["unexpected pre-filled field"], ("aborted_mismatch", None, ["unexpected_field"]))
        self.assertEqual(codes["coach pre-selected"], ("aborted_mismatch", None, ["coach_preselected"]))
        self.assertEqual(codes["different date"][2], ["page_mismatch:time"])
        self.assertEqual(codes["unclear (network)"][:2], ("unclear", "not_listed_after:network_error"))
        self.assertEqual(codes["refused twice"][:2], ("failed", "two_courts_refused"))
        self.assertEqual(codes["unreadable booking"][:2], (None, "existing_booking_unreadable"))

    def test_dry_run_and_interrupted_records_are_clean_too(self):
        self.run_mode("dry_run")
        self.assert_state_clean("dry run")
        self.setUp()
        self.site.on_post = lambda n: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.run_mode("live")
        self.site.on_post = None
        self.state_file.write_text(json.dumps({**self.state(), "known_open": {}}))
        self.run_mode("live")
        self.assertEqual(self.assert_state_clean("interrupted")[autobook.LIVE_KEY][self.key]["status"], "interrupted")

class TestDashboardMode(AutobookTestCase):
    def test_every_run_writes_the_mode_it_uses(self):
        for mode, want in ((None, "off"), ("off", "off"), ("dry_run", "dry_run"), ("live", "live"), ("bogus", "off")):
            with self.subTest(mode=mode):
                self.setUp()
                self.run_mode(mode)
                self.assertEqual(self.state()["autobook_mode"], want)

    def test_a_run_that_stops_early_still_writes_it(self):
        self.write_config([])
        self.run_mode("live")
        self.assertEqual(self.state()["autobook_mode"], "live")


class TestDashboardWording(unittest.TestCase):
    """No JS test runner here: pin the dashboard's per-mode wording to the
    mode values the monitor writes (npm run build type-checks the rest)."""

    def test_each_mode_has_its_wording_and_unknown_is_cautious(self):
        src = (Path(__file__).resolve().parent / "app" / "Dashboard.tsx").read_text(encoding="utf-8")
        for text in ("starred — notify only (auto-book off)",
                     "starred — dry run (emails what it would book, books nothing)",
                     "AUTO-BOOK — books with Wallet when it opens"):
            self.assertIn(text, src)
        for stale in ("auto-book paused — notify only", "paused while it's rebuilt"):
            self.assertNotIn(stale, src, "no fixed 'paused' text left")
        self.assertIn('"autobook_halted" in state', src, "halted = the key is present, whatever its value")
        self.assertIn("state.autobook_blocked", src)
        view = src[src.index("function modeView"):src.index("const DANGER")]
        self.assertIn('return m === "off" || m === "dry_run" || m === "live" ? m : "unknown"', view)
        default = src[src.index("    default:", src.index("function modeText")):]
        self.assertIn("bannerClass: DANGER", default[:800], "unknown mode looks like live")
        live = src[src.index('    case "live":'):src.index("    default:", src.index("function modeText"))]
        self.assertIn("DANGER", live)
        types = (Path(__file__).resolve().parent / "lib" / "types.ts").read_text(encoding="utf-8")
        self.assertIn('export type AutobookMode = "off" | "dry_run" | "live"', types)
        self.assertEqual(set(autobook.MODES), {"off", "dry_run", "live"})


class TestStateFile(AutobookTestCase):
    def test_save_is_atomic_when_interrupted(self):
        self.state_file.write_text(json.dumps({"known_open": {"old": True}}))
        for where_ in ("fsync", "replace"):
            with self.subTest(where_), mock.patch.object(monitor.os, where_,
                                                         side_effect=KeyboardInterrupt("killed")), \
                 mock.patch.object(monitor, "STATE_FILE", self.state_file), self.assertRaises(KeyboardInterrupt):
                monitor.save_state({"known_open": {"new": True}, "big": "x" * 100000})
            self.assertEqual(json.loads(self.state_file.read_text()), {"known_open": {"old": True}})
            self.assertEqual([p.name for p in self.dir.glob(".state.json.*")], [], "temp file cleaned up")

    def test_kill_9_mid_write_leaves_a_readable_state(self):
        # A real process killed (os._exit: no cleanup at all) between writing
        # the temp file and the rename. state.json must still be the old one.
        self.state_file.write_text(json.dumps({"known_open": {"old": True}}))
        script = (
            "import os, sys, json; sys.path.insert(0, sys.argv[1]); import monitor; from pathlib import Path\n"
            "monitor.STATE_FILE = Path(sys.argv[2])\n"
            "real_fsync = os.fsync\n"
            "def die(fd):\n    real_fsync(fd); os._exit(9)\n"
            "os.fsync = die\n"
            "monitor.save_state({'known_open': {'new': True}, 'big': 'x' * 1000000})\n")
        r = subprocess.run([sys.executable, "-c", script, str(Path(__file__).resolve().parent), str(self.state_file)],
                           capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 9)
        self.assertEqual(monitor.json.loads(self.state_file.read_text()), {"known_open": {"old": True}})
        with mock.patch.object(monitor, "STATE_FILE", self.state_file):
            self.assertEqual(monitor.load_state(), {"known_open": {"old": True}})

    def test_unreadable_state_halts_auto_book_but_keeps_notifying(self):
        for broken in ("{not json", "[1, 2]", "\x00\x01garbage"):
            with self.subTest(broken=broken):
                self.setUp()
                self.state_file.write_text(broken)
                code, sent = self.run_mode("live")
                self.assertEqual([s["path"] for s in self.site.sent], ["/booking"], "no auto-book requests at all")
                self.assertEqual(self.subjects(sent)[0], monitor.STATE_UNREADABLE_SUBJECT)
                self.assertTrue(self.subjects(sent)[1].startswith("🎾"), "notify-only still works")
                self.assertIn(monitor.AUTOBOOK_PAUSED_NOTE, sent[1]["body"])
                state = self.state()
                self.assertEqual(state["autobook_halted"]["reason"], "state_unreadable")
                self.assertEqual(state["autobook_mode"], "off")
                self.assertTrue(list(self.dir.glob("state.json.unreadable-*")), "the unreadable file is kept")
                # Later runs: still halted, no second 🔴 email, still no auto-book requests.
                self.site.sent.clear()
                self.state_file.write_text(json.dumps({**self.state(), "known_open": {}}))
                code, sent = self.run_mode("live")
                self.assertEqual([s["path"] for s in self.site.sent], ["/booking"])
                self.assertNotIn(monitor.STATE_UNREADABLE_SUBJECT, self.subjects(sent))

    def test_the_session_lost_cli_also_survives_an_unreadable_state(self):
        self.state_file.write_text("{oops")
        sent = []
        with mock.patch.dict(os.environ, EMAIL_ENV, clear=True), \
             mock.patch.object(monitor, "CONFIG_FILE", self.config_file), \
             mock.patch.object(monitor, "STATE_FILE", self.state_file), \
             mock.patch.object(monitor, "send_email", side_effect=lambda s, b, *a: sent.append(s)), \
             contextlib.redirect_stdout(io.StringIO()):
            code = monitor.main(["--alert-session-lost", "push failed"])
        self.assertEqual(code, monitor.EXIT_JAR)
        self.assertEqual(sent, [monitor.STATE_UNREADABLE_SUBJECT, "🔴 Bot session likely lost — reseed needed"])
        self.assertIn("autobook_halted", self.state())

    def test_halt_email_is_retried_until_it_is_sent(self):
        self.state_file.write_text("{oops")
        code, sent = self.run_mode("live", fail_subjects=("🔴 state.json",))
        self.assertNotIn(monitor.STATE_UNREADABLE_SUBJECT, self.subjects(sent))
        code, sent = self.run_mode("live")
        self.assertEqual(self.subjects(sent).count(monitor.STATE_UNREADABLE_SUBJECT), 1)
        code, sent = self.run_mode("live")
        self.assertNotIn(monitor.STATE_UNREADABLE_SUBJECT, self.subjects(sent))

    def test_removing_the_halt_resumes_auto_book(self):
        self.state_file.write_text("{oops")
        self.run_mode("live")
        state = self.state()
        del state["autobook_halted"]
        state["known_open"] = {}
        self.state_file.write_text(json.dumps(state))
        self.site.posts = [parked_redirect()]
        self.site.pending = [self.p.pending_empty(), self.p.pending_with_ref()]
        self.run_mode("live")
        self.assertEqual(len(self.site.posts_sent()), 1)

class TestClashUsesTimeRanges(AutobookTestCase):
    def test_overlaps(self):
        d = "2026-11-15"
        yes = [((d, "22:00", "24:00"), (d, "23:00", "24:00")), ((d, "23:30", "00:30"), (d, "23:00", "24:00")),
               ((d, "21:00", "23:30"), (d, "23:00", "24:00")), ((d, "23:00", "24:00"), (d, "23:00", "24:00"))]
        no = [((d, "22:00", "23:00"), (d, "23:00", "24:00")), (("2026-11-16", "23:00", "24:00"), (d, "23:00", "24:00")),
              ((d, "06:00", "08:00"), (d, "23:00", "24:00"))]
        for a, b in yes:
            self.assertTrue(autobook.overlaps(a, b) and autobook.overlaps(b, a), (a, b))
        for a, b in no:
            self.assertFalse(autobook.overlaps(a, b) or autobook.overlaps(b, a), (a, b))

    def test_a_two_hour_booking_covering_the_slot_blocks_it(self):
        for label, arrange in (
                ("upcoming 22:00-24:00", lambda: setattr(self.site, "upcoming", [self.p.upcoming_filled(time="22:00", end="24:00")])),
                ("pending, own page 22:00-24:00", lambda: (self.site.details.__setitem__(OTHER, self.p.detail(OTHER, time="22:00", end="24:00")),
                                                           setattr(self.site, "pending", [self.p.pending_with_ref(OTHER)])))):
            with self.subTest(label):
                self.setUp()
                arrange()
                code, sent = self.run_mode("live")
                self.assertEqual(self.site.posts_sent(), [])
                self.assertEqual(self.subjects(sent), [f"ℹ️ Already booked: {self.date} 23:00 — not auto-booking"])

    def test_an_adjacent_booking_does_not_block(self):
        self.site.upcoming = [self.p.upcoming_filled(time="21:00", end="23:00")]
        self.site.posts = [parked_redirect()]
        self.site.pending = [self.p.pending_empty(), self.p.pending_with_ref()]
        code, sent = self.run_mode("live")
        self.assertEqual(len(self.site.posts_sent()), 1)


class TestErrorsNeverDropAnything(AutobookTestCase):
    def test_result_email_that_fails_does_not_hide_the_slot_open_email(self):
        self.site.posts = [parked_redirect()]
        self.site.pending = [self.p.pending_empty(), self.p.pending_with_ref()]
        code, sent = self.run_mode("live", fail_subjects=("🟡",))
        self.assertEqual(len(self.site.posts_sent()), 1)
        slot = [m for m in sent if m["subject"].startswith("🎾")]
        self.assertEqual(len(slot), 1, "the result email failed, so the opening is still announced")
        self.assertIn("23:00", slot[0]["body"])

    def test_context_email_counts_only_sent_mail(self):
        ctx = autobook.Context(None, {}, "live", send=mock.Mock(side_effect=OSError("smtp")), save_state=None,
                               try_save_state=None, log=lambda m: None, locations={})
        self.assertFalse(ctx.email("k", "🟢 x", "y"))
        self.assertEqual(ctx.emailed_slots, {})

    def test_a_crashing_auto_book_still_sends_the_slot_open_email(self):
        for exc in (RuntimeError("bug"), KeyError("x"), bot_session.JarSaveError("disk full", [])):
            with self.subTest(exc=type(exc).__name__):
                self.setUp()
                with mock.patch.object(autobook, "process", side_effect=exc):
                    code, sent = self.run_mode("live")
                self.assertEqual([m["subject"][:1] for m in sent], ["🎾"])
                self.assertIn("23:00", sent[0]["body"])
                known = self.state().get("known_open", {})
                self.assertIn(monitor.slot_key(self.date, "LOC002", monitor.ANY_COURT, "23:00"), known)
                if isinstance(exc, bot_session.JarSaveError):
                    self.assertEqual(code, monitor.EXIT_ERROR, "the jar error still fails the run, after the email")

    def test_an_opening_whose_email_failed_alerts_again(self):
        code, sent = self.run_mode("off", fail_subjects=("🎾",))
        self.assertEqual(self.state().get("known_open", {}), {}, "not marked known: its email didn't go out")
        code, sent = self.run_mode("off")
        self.assertEqual(len([m for m in sent if m["subject"].startswith("🎾")]), 1)
        code, sent = self.run_mode("off")
        self.assertEqual(sent, [], "and only once it went out is it known")

    def test_write_ahead_does_not_persist_an_unannounced_opening(self):
        k = monitor.slot_key(self.date, "LOC002", monitor.ANY_COURT, "23:00")

        def die(_n):
            self.assertNotIn(k, json.loads(self.state_file.read_text()).get("known_open", {}))
            raise KeyboardInterrupt()
        self.site.on_post = die
        with self.assertRaises(KeyboardInterrupt):
            self.run_mode("live")

    def test_a_skipped_candidate_at_one_location_does_not_block_the_other(self):
        # 23:00 at Crystal Sports was already open (and alerted) before auto-book
        # was switched on; now 23:00 opens at Crystal Sports G.
        self.write_config([target(self.date, [("23:00", True)], locations=("LOC001", "LOC002"))])
        self.state_file.write_text(json.dumps({"known_open": {
            monitor.slot_key(self.date, "LOC001", monitor.ANY_COURT, "23:00"): True}}))
        self.site.posts = [parked_redirect()]
        self.site.pending = [self.p.pending_empty(), self.p.pending_with_ref()]
        code, sent = self.run_mode("live")
        self.assertEqual(len(self.site.posts_sent()), 1)
        self.assertEqual(self.record()["loc"], "LOC002")

    def two_starred(self):
        self.write_config([target(self.date, [("22:00", True), ("23:00", True)], locations=("LOC002",))])
        self.site.grid["LOC002"] = self.p.grid_with_2200_open()
        return autobook.key_for(self.date, "22:00"), self.key

    def test_session_stop_puts_every_remaining_candidate_into_retries(self):
        k22, k23 = self.two_starred()
        self.site.pending = [login_redirect()]
        code, sent = self.run_mode("live")
        self.assertEqual(code, monitor.EXIT_SESSION)
        retries = self.state()[autobook.RETRY_KEY]
        self.assertEqual({k: v["reason"] for k, v in retries.items()}, {k22: "session_stopped", k23: "session_stopped"})
        self.assertEqual(self.state().get(autobook.LIVE_KEY, {}), {})

    def test_jar_save_error_before_any_post_is_retried_next_run(self):
        k22, k23 = self.two_starred()
        real_save = bot_session.save_jar
        calls = {"n": 0}

        def save(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 3:   # grid, (pending) -> fails on the auto-book's first list read
                raise OSError("disk full")
            return real_save(*a, **kw)
        with mock.patch.object(bot_session, "save_jar", side_effect=save):
            code, sent = self.run_mode("live")
        self.assertEqual(code, monitor.EXIT_ERROR)
        self.assertEqual(self.site.posts_sent(), [])
        retries = self.state()[autobook.RETRY_KEY]
        self.assertEqual({k: v["reason"] for k, v in retries.items()}, {k22: "jar_save_error", k23: "jar_save_error"})
        self.assertEqual(self.state().get(autobook.LIVE_KEY, {}), {}, "no final outcome: nothing was submitted")
        self.assertTrue(any(m["subject"].startswith("🎾") for m in sent), "the slot-open email still went out")
        # Next run (the jar saves again): the retried keys are booked although no longer newly open.
        self.site.sent.clear()
        # (The fake site serves only the 23:00 slot page, so 22:00 stops at the
        # slot-page check; what matters is that both are attempted again.)
        self.site.posts = [parked_redirect()]
        self.site.pending = [self.p.pending_empty(), self.p.pending_empty(), self.p.pending_with_ref()]
        code, sent = self.run_mode("live")
        self.assertEqual(len(self.site.posts_sent()), 1)
        self.assertEqual(self.record(k23)["status"], "parked")
        self.assertEqual(self.record(k22)["status"], "aborted_mismatch")
        self.assertEqual(self.state().get(autobook.RETRY_KEY, {}), {})

    def test_jar_save_error_after_the_post_is_unclear_and_never_retried(self):
        real_save = bot_session.save_jar
        posted = {"yes": False}

        def on_post(_n):
            posted["yes"] = True
        self.site.on_post = on_post
        self.site.posts = [parked_redirect()]

        def save(*a, **kw):
            if posted["yes"]:
                raise OSError("disk full")
            return real_save(*a, **kw)
        with mock.patch.object(bot_session, "save_jar", side_effect=save):
            code, sent = self.run_mode("live")
        self.assertEqual(len(self.site.posts_sent()), 1)
        self.assertEqual(self.record()["status"], "unclear")
        self.assertNotIn(self.key, self.state().get(autobook.RETRY_KEY, {}))
        self.assertTrue(any(m["subject"].startswith("🟠") for m in sent))
        self.site.sent.clear()
        self.state_file.write_text(json.dumps({**self.state(), "known_open": {}}))
        self.run_mode("live")
        self.assertEqual(self.site.posts_sent(), [])



# --- independent review ----------------------------------------------------------

class TestHaltIsDurable(AutobookTestCase):
    BAD = {
        "missing": None,
        "not json": "{oops",
        "autobook is a list": json.dumps({"autobook": []}),
        "a record's attempts aren't a list": json.dumps({"autobook": {"2026-11-15|23:00": {"status": "parked", "attempts": "x"}}}),
        "a record isn't an object": json.dumps({"autobook_dry_run": {"2026-11-15|23:00": "dry_run"}}),
        "retry entry isn't an object": json.dumps({"autobook_retry": {"2026-11-15|23:00": "x"}}),
        "known_open value isn't true": json.dumps({"known_open": {"2026-11-15|LOC002|any|23:00": 1}}),
        "session_alert isn't an object": json.dumps({"session_alert": "expired"}),
        "halt isn't an object": json.dumps({"autobook_halted": True}),
    }

    def plant(self, content):
        if content is None:
            self.state_file.unlink()
        else:
            self.state_file.write_text(content)

    def test_every_bad_state_halts_and_notify_only_carries_on(self):
        for label, content in self.BAD.items():
            with self.subTest(label):
                self.setUp()
                self.plant(content)
                code, sent = self.run_mode("live")
                self.assertEqual([s["path"] for s in self.site.sent], ["/booking"], "no auto-book requests")
                self.assertEqual(self.subjects(sent)[0], monitor.STATE_UNREADABLE_SUBJECT)
                self.assertTrue(self.subjects(sent)[1].startswith("🎾"), "notify-only from a fresh state")
                self.assertIn("autobook_halted", self.state())
                code, sent = self.run_mode("live")
                self.assertNotIn(monitor.STATE_UNREADABLE_SUBJECT, self.subjects(sent), "deduped")
                self.assertEqual([s["path"] for s in self.site.sent].count("/bookings"), 0)

    def test_garbage_in_informational_keys_does_not_halt(self):
        self.state_file.write_text(json.dumps({"last_check": 5, "unreadable_last": 12, "currently_open_count": "x"}))
        self.site.posts = [parked_redirect()]
        self.site.pending = [self.p.pending_empty(), self.p.pending_with_ref()]
        self.run_mode("live")
        self.assertNotIn("autobook_halted", self.state())
        self.assertEqual(len(self.site.posts_sent()), 1)

    def test_the_halt_is_on_disk_before_any_early_return(self):
        early_returns = {
            "email settings missing": lambda: self._run_patched(env={"EMAIL_USER": "", "EMAIL_APP_PASSWORD": ""}),
            "config.json missing": lambda: (self.config_file.unlink(), self._run_patched(expect=SystemExit)),
            "config.json unreadable": lambda: (self.config_file.write_text("{x"), self._run_patched(expect=ValueError)),
            "bot session can't be opened": lambda: (self.jar.unlink(), self._run_patched()),
            "bot session already marked dead": lambda: self._run_patched(dead_session=True),
            "session-lost CLI": lambda: self._run_patched(argv=["--alert-session-lost", "x"]),
            "session-lost CLI, email settings missing": lambda: self._run_patched(
                argv=["--alert-session-lost", "x"], env={"EMAIL_USER": "", "EMAIL_APP_PASSWORD": ""}),
        }
        for bad_label in ("missing", "not json", "autobook is a list"):
            for label, go in early_returns.items():
                with self.subTest(state=bad_label, early_return=label):
                    self.setUp()
                    self.plant(self.BAD[bad_label])
                    go()
                    halted = self.state().get("autobook_halted")
                    self.assertIsNotNone(halted, "the halt must be on disk")
                    self.assertEqual(halted["reason"], "state_unreadable")

    def _run_patched(self, env=None, expect=None, dead_session=False, argv=None):
        sent = []
        full_env = {**EMAIL_ENV, "AUTOBOOK_MODE": "live", **(env or {})}
        session = requests.Session()
        session.mount("https://", self.site)
        with mock.patch.dict(os.environ, full_env, clear=True), \
             mock.patch.object(monitor, "CONFIG_FILE", self.config_file), \
             mock.patch.object(monitor, "STATE_FILE", self.state_file), \
             mock.patch.object(monitor, "send_email", side_effect=lambda s, b, *a: sent.append(s)), \
             mock.patch.object(monitor.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            if dead_session:
                real_open = monitor.open_state

                def open_and_mark():
                    st = real_open()
                    st["session_alert"] = {"kind": "expired", "sent_at": "x",
                                           "jar_sha256": bot_session.file_fingerprint(self.jar)}
                    return st
                with mock.patch.object(monitor, "open_state", side_effect=open_and_mark):
                    monitor.run(iterations=1, interval=0, jar_path=self.jar, enc_path=self.enc, session=session)
            elif argv:
                with mock.patch.object(monitor, "current_jar_path", return_value=self.jar):
                    monitor.main(argv)
            elif expect:
                with self.assertRaises(expect):
                    monitor.run(iterations=1, interval=0, jar_path=self.jar, enc_path=self.enc, session=session)
            else:
                monitor.run(iterations=1, interval=0, jar_path=self.jar, enc_path=self.enc, session=session)
        self.assertEqual(self.site.posts_sent(), [])
        return sent

    def test_the_halt_emails_dedupe_mark_survives_an_early_return(self):
        self.state_file.write_text("{oops")
        self.jar.unlink()  # every run returns early: the bot session can't be opened
        first = self._run_patched()
        second = self._run_patched()
        self.assertEqual(first, [monitor.STATE_UNREADABLE_SUBJECT])
        self.assertEqual(second, [], "sent once, although no run got past opening the jar")

    def test_the_halt_email_carries_the_restore_steps(self):
        self.state_file.write_text("{oops")
        code, sent = self.run_mode("live")
        body = sent[0]["body"]
        self.assertIn(monitor.HALT_RESTORE_STEPS, body)
        for must in ("cron-job.org", "git pull --rebase", "python3 restore_state.py", "bookings?tab=pending",
                     'git commit -am "Restore state after halt" && git push'):
            self.assertIn(must, body)

    def test_readme_has_the_same_restore_steps(self):
        readme = (Path(__file__).resolve().parent / "README.md").read_text(encoding="utf-8")
        self.assertIn(monitor.HALT_RESTORE_STEPS, readme)

    def test_the_steps_are_plain_commands(self):
        commands = restore_commands()
        self.assertEqual(commands, ["git pull --rebase", "python3 restore_state.py",
                                    'git commit -am "Restore state after halt" && git push'])
        for c in commands:  # nothing that differs between bash and zsh
            for shell_feature in ("$(", "`", "<<", "for ", "{ ", "[[", "!", "*", "~", ";"):
                self.assertNotIn(shell_feature, c)


def restore_commands():
    return [l.strip() for l in monitor.HALT_RESTORE_STEPS.splitlines()
            if l.startswith("    ") and l.strip().startswith(("git ", "python3 "))]


class TestRestoreState(unittest.TestCase):
    """restore_state.py and the halt email's steps, run for real in a throwaway
    repo (with a bare "origin"), in bash and in zsh."""
    CODE = ("restore_state.py", "monitor.py", "autobook.py", "booking_page.py", "bot_session.py")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def git(self, repo, *a):
        return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout

    def make_history(self):
        origin, repo = self.root / "origin.git", self.root / "repo"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True, capture_output=True)
        self.git(repo, "config", "user.email", "t@example.invalid")
        self.git(repo, "config", "user.name", "t")
        self.git(repo, "checkout", "-q", "-b", "main")
        self.good = {"autobook": {"2026-11-15|23:00": {"status": "parked", "attempts": [{"n": 1, "ref_fp": "abcdef123456"}]},
                                  "2026-11-20|20:00": {"status": "failed", "attempts": []}},
                     "autobook_dry_run": {"2026-11-16|20:00": {"status": "dry_run"}},
                     "autobook_retry": {"2026-11-17|21:00": {"reason": "no_free_court"}}, "known_open": {}}
        versions = [
            ("older good", {"autobook": {"2026-11-01|20:00": {"status": "paid", "attempts": []}}}),
            ("good", self.good),
            ("wrong shape", {"autobook": [], "known_open": {}}),
            ("not json", "{broken"),
            ("halted by hand, null", {**self.good, "autobook_halted": None}),
            ("wrong-shape halt (monitor's fresh state)", {"autobook_halted": {"reason": "state_unreadable"},
                                                          "known_open": {"k": True},
                                                          "autobook": {"2026-11-20|20:00": {"status": "paid", "attempts": []}}}),
        ]
        for msg, content in versions:
            (repo / "state.json").write_text(content if isinstance(content, str) else json.dumps(content))
            self.git(repo, "add", "state.json")
            self.git(repo, "commit", "-qm", msg)
        self.git(repo, "push", "-q", "-u", "origin", "main")
        src = Path(__file__).resolve().parent
        for f in self.CODE:  # the code, untracked, as in the real working copy
            shutil.copy(src / f, repo / f)
        return origin, repo

    def test_the_halt_steps_work_in_bash_and_zsh(self):
        for shell in ("bash", "zsh"):
            with self.subTest(shell=shell):
                if not shutil.which(shell):
                    self.skipTest(f"{shell} not installed")
                self.setUp()
                origin, repo = self.make_history()
                outputs = []
                for cmd in restore_commands():
                    r = subprocess.run([shell, "-c", cmd], cwd=repo, capture_output=True, text=True)
                    self.assertEqual(r.returncode, 0, f"{cmd}: {r.stdout} {r.stderr}")
                    outputs.append(r.stdout)
                pushed = json.loads(self.git(origin, "show", "main:state.json"))
                self.assertNotIn("autobook_halted", pushed)
                self.assertEqual(pushed["autobook_dry_run"], self.good["autobook_dry_run"])
                self.assertEqual(pushed["autobook_retry"], self.good["autobook_retry"])
                self.assertEqual(pushed["autobook"]["2026-11-15|23:00"], self.good["autobook"]["2026-11-15|23:00"])
                self.assertEqual(pushed["autobook"]["2026-11-20|20:00"]["status"], "paid", "the current value wins")
                self.assertNotIn("2026-11-01|20:00", pushed["autobook"], "the NEWEST good version, not an older one")
                self.assertEqual(pushed["known_open"], {"k": True}, "the rest of the current state is kept")
                self.assertEqual(monitor.shape_problems(pushed), [])
                summary = outputs[1]
                self.assertIn("skipped 4 newer version(s)", summary,
                              "wrong shape, not JSON, halted with null, the monitor's halted state")
                self.assertIn("autobook: 2 record(s) in that version -> 2 now", summary)
                self.assertNotIn("abcdef123456", summary, "counts only, no record contents")
                self.assertNotIn("SPORTS-", summary)

    def run_restore(self, repo):
        return subprocess.run([sys.executable, "restore_state.py"], cwd=repo, capture_output=True, text=True)

    def test_it_never_commits_or_pushes(self):
        origin, repo = self.make_history()
        head = self.git(repo, "rev-parse", "HEAD")
        r = self.run_restore(repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.git(repo, "rev-parse", "HEAD"), head)
        self.assertEqual(self.git(origin, "rev-parse", "main"), head)
        self.assertIn("state.json", self.git(repo, "status", "--porcelain"))

    def test_nothing_usable_leaves_state_untouched(self):
        repo = self.root / "bare-history"
        repo.mkdir()
        self.git(repo, "init", "-q")
        self.git(repo, "config", "user.email", "t@example.invalid")
        self.git(repo, "config", "user.name", "t")
        for v in ("{bad", json.dumps({"autobook": "x"}), json.dumps({"autobook_halted": {}})):
            (repo / "state.json").write_text(v)
            self.git(repo, "add", "state.json")
            self.git(repo, "commit", "-qm", "v")
        for f in self.CODE:
            shutil.copy(Path(__file__).resolve().parent / f, repo / f)
        before = (repo / "state.json").read_text()
        r = self.run_restore(repo)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("no usable state.json", r.stdout)
        self.assertEqual((repo / "state.json").read_text(), before)

    def test_not_halted_is_a_no_op(self):
        origin, repo = self.make_history()
        (repo / "state.json").write_text(json.dumps({"known_open": {}}))
        r = self.run_restore(repo)
        self.assertEqual(r.returncode, 0)
        self.assertIn("Nothing to do", r.stdout)
        self.assertEqual(json.loads((repo / "state.json").read_text()), {"known_open": {}})

    def test_it_works_on_its_own_folder_from_anywhere(self):
        origin, repo = self.make_history()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        r = subprocess.run([sys.executable, str(repo / "restore_state.py")], cwd=elsewhere, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("autobook_halted", json.loads((repo / "state.json").read_text()))
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_outside_a_git_checkout_it_says_so(self):
        plain = self.root / "not-a-repo"
        plain.mkdir()
        for f in self.CODE:
            shutil.copy(Path(__file__).resolve().parent / f, plain / f)
        (plain / "state.json").write_text(json.dumps({"autobook_halted": {}}))
        r = subprocess.run([sys.executable, str(plain / "restore_state.py")], cwd=self.root, capture_output=True, text=True,
                           env={**os.environ, "GIT_CEILING_DIRECTORIES": str(self.root)})
        self.assertEqual(r.returncode, 1)
        self.assertIn("isn't a git repository", r.stdout)
        self.assertEqual(json.loads((plain / "state.json").read_text()), {"autobook_halted": {}})

    def test_a_checkout_without_state_json_says_so(self):
        origin, repo = self.make_history()
        (repo / "state.json").unlink()
        r = self.run_restore(repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn("there's no state.json", r.stdout)

    def test_an_unreadable_current_file_is_left_alone(self):
        origin, repo = self.make_history()
        (repo / "state.json").write_text("{nope")
        r = self.run_restore(repo)
        self.assertEqual(r.returncode, 1)
        self.assertEqual((repo / "state.json").read_text(), "{nope")


class TestKeyedFingerprint(unittest.TestCase):
    def test_fingerprint_is_a_keyed_hmac_from_bot_jar_key(self):
        import hashlib, hmac as _hmac
        plain = hashlib.sha256(REF.encode()).hexdigest()[:12]
        with mock.patch.dict(os.environ, {"BOT_JAR_KEY": "A" * 44}):
            a = autobook.ref_fp(REF)
            self.assertEqual(a, autobook.ref_fp(REF), "stable for one key")
            key = _hmac.new(b"A" * 44, b"court-checker ref fingerprint v1", hashlib.sha256).digest()
            self.assertEqual(a, _hmac.new(key, REF.encode(), hashlib.sha256).hexdigest()[:12])
            self.assertIn(f"ref:{a}", bot_session.redact(REF))
            self.assertIn(f"payref:{autobook.ref_fp('100000000000000001')}", bot_session.redact("100000000000000001"))
        with mock.patch.dict(os.environ, {"BOT_JAR_KEY": "B" * 44}):
            b = autobook.ref_fp(REF)
        with mock.patch.dict(os.environ, {}, clear=True):
            none = autobook.ref_fp(REF)
        self.assertEqual(len({a, b, none, plain}), 4, "the old plain SHA-256 fingerprint no longer matches")
        self.assertNotEqual(a, plain)
        self.assertNotIn(plain, bot_session.redact(REF))


class TestAlertOnlyTheAttemptedLocation(AutobookTestCase):
    def test_other_locations_opening_is_still_alerted(self):
        self.write_config([target(self.date, [("23:00", True)], locations=("LOC001", "LOC002"))])
        # The pages are Crystal Sports G's, so the attempt at LOC001 (tried
        # first) ends 🔴 page mismatch.
        code, sent = self.run_mode("live")
        subjects = self.subjects(sent)
        self.assertTrue(any(s.startswith("🔴 Failed: booking form changed") for s in subjects), subjects)
        self.assertEqual(self.record()["loc"], "LOC001")
        slot = [m for m in sent if m["subject"].startswith("🎾")]
        self.assertEqual(len(slot), 1)
        self.assertIn("— Crystal Sports G (", slot[0]["body"], "LOC002's opening is alerted")
        self.assertNotIn("— Crystal Sports (", slot[0]["body"], "LOC001's was replaced by its result email")
        known = self.state()["known_open"]
        for loc in ("LOC001", "LOC002"):
            self.assertIn(monitor.slot_key(self.date, loc, monitor.ANY_COURT, "23:00"), known)


class TestStateNotSaved(MonitorTestCase):
    def call(self, blob, fail=False, run_info=None, extra_env=None):
        if run_info is not None:
            (self.dir / ".autobook_run.json").write_text(json.dumps(run_info))
        sent = []

        def send(s, b, *a):
            if fail:
                raise smtplib.SMTPException("down")
            sent.append({"subject": s, "body": b})
        env = {**EMAIL_ENV, **({"REMOTE_STATE_BLOB": blob} if blob is not None else {}), **(extra_env or {})}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(monitor, "CONFIG_FILE", self.config_file), \
             mock.patch.object(monitor, "STATE_FILE", self.state_file), \
             mock.patch.object(monitor, "send_email", side_effect=send), contextlib.redirect_stdout(io.StringIO()):
            code = monitor.main(["--alert-state-not-saved", "push failed"])
        return code, sent

    def test_no_known_blob_at_all_always_emails(self):
        unknown = {"REPO_STATE_BLOB": "unknown"}
        self.assertEqual(len(self.call("unknown", run_info=self.NO_ATTEMPT, extra_env=unknown)[1]), 1)
        self.assertEqual(len(self.call("unknown", run_info=self.NO_ATTEMPT, extra_env=unknown)[1]), 1,
                         "can't tell episodes apart: always email")
        self.assertIsNone(json.loads((self.dir / ".state_alert.json").read_text())["remote_blob"])

    def test_the_checkout_blob_is_the_fallback_episode_id(self):
        b1 = {"REPO_STATE_BLOB": "B1"}
        self.assertEqual(len(self.call("unknown", run_info=self.NO_ATTEMPT, extra_env=b1)[1]), 1)
        self.assertEqual(self.call("unknown", run_info=self.NO_ATTEMPT, extra_env=b1)[1], [], "same episode")

    NO_ATTEMPT = {"attempt_keys": [], "carry_keys": [], "carry_attempted": False}

    def test_without_attempts_one_email_per_episode(self):
        code, sent = self.call("blob1", run_info=self.NO_ATTEMPT)
        self.assertEqual(code, monitor.EXIT_OK)
        self.assertEqual([m["subject"] for m in sent], [monitor.STATE_NOT_SAVED_SUBJECT])
        self.assertEqual(self.call("blob1", run_info=self.NO_ATTEMPT)[1], [], "same repo state.json: same episode")
        self.assertEqual(len(self.call("blob2", run_info=self.NO_ATTEMPT)[1]), 1, "the repo moved on: a new episode")
        self.assertEqual(len(self.call("unknown", run_info=self.NO_ATTEMPT)[1]), 1, "can't tell: always email")
        self.assertEqual(len(self.call("unknown", run_info=self.NO_ATTEMPT)[1]), 1)

    def test_with_attempts_the_email_lists_them_and_the_marker_records_them(self):
        code, sent = self.call("blob1", run_info={"attempt_keys": ["2026-11-15|23:00"], "carry_keys": [], "carry_attempted": False})
        self.assertIn("2026-11-15|23:00", sent[0]["body"])
        self.assertIn("halts auto-book", sent[0]["body"])
        marker = json.loads((self.dir / ".state_alert.json").read_text())
        self.assertEqual((marker["autobook_attempted"], marker["autobook_keys"]), (True, ["2026-11-15|23:00"]))
        self.assertNotIn("SPORTS-", (self.dir / ".state_alert.json").read_text())

    def test_a_failed_send_still_writes_the_marker_and_retries_the_email(self):
        code, sent = self.call("blob1", fail=True, run_info=self.NO_ATTEMPT)
        self.assertEqual(code, monitor.EXIT_ERROR)
        marker = json.loads((self.dir / ".state_alert.json").read_text())
        self.assertEqual((marker["remote_blob"], marker["emailed"]), ("blob1", False))
        self.assertEqual(len(self.call("blob1", run_info=self.NO_ATTEMPT)[1]), 1, "not emailed yet: sent next time")
        self.assertEqual(self.call("blob1", run_info=self.NO_ATTEMPT)[1], [])

    def test_workflow_sends_it_when_the_push_fails(self):
        wf = (Path(__file__).resolve().parent / ".github" / "workflows" / "monitor.yml").read_text(encoding="utf-8")
        self.assertIn('echo "state_not_saved=true" >> "$GITHUB_OUTPUT"', wf)
        self.assertIn("python monitor.py --alert-state-not-saved", wf)
        self.assertIn("if: always() && steps.save.outputs.state_not_saved == 'true'", wf)
        self.assertIn("REMOTE_STATE_BLOB=", wf)


class TestSecondCourtOnlyIfNothingNew(AutobookTestCase):
    def test_refused_but_a_new_booking_appeared_is_unclear(self):
        two = self.p.pending_with_ref("SPORTS-900000000071").replace(
            '<a class="bk-row"', self.p.pending_with_ref("SPORTS-900000000072").split('<div id="bookingList">')[1]
            .split("</div>")[0] + '<a class="bk-row"', 1)
        for label, after in (("one new booking", self.p.pending_with_ref("SPORTS-900000000071")), ("two new bookings", two)):
            with self.subTest(label):
                self.setUp()
                self.site.details["SPORTS-900000000071"] = self.p.detail("SPORTS-900000000071", time="10:00")
                self.site.posts = [refused_page(self.p.refused()), parked_redirect()]
                self.site.pending = [self.p.pending_empty(), after]
                code, sent = self.run_mode("live")
                self.assertEqual(len(self.site.posts_sent()), 1, "no second court")
                self.assertEqual(len(sent), 1)
                self.assertTrue(sent[0]["subject"].startswith("🟠 Unclear"), sent[0]["subject"])
                self.assertEqual(self.record()["reason"], "refused_but_new_booking_listed")

    def test_refused_and_nothing_new_still_tries_one_other_court(self):
        self.site.posts = [refused_page(self.p.refused()), parked_redirect()]
        self.site.pending = [self.p.pending_empty(), self.p.pending_empty(), self.p.pending_with_ref()]
        code, sent = self.run_mode("live")
        self.assertEqual(len(self.site.posts_sent()), 2)


class TestNoSiteFieldNamesInState(AutobookTestCase):
    def test_field_names_stay_in_the_email(self):
        for name, extra, code_ in (("promo", 'value="X"', "unexpected_field"), ("phone", "required", "unexpected_field")):
            with self.subTest(name):
                self.setUp()
                self.site.reviews[self.p.sel] = self.p.review().replace(
                    '<input type="hidden" name="sel"', f'<input type="text" name="{name}" {extra} /><input type="hidden" name="sel"')
                code, sent = self.run_mode("live")
                self.assertEqual(self.record()["problems"], [code_])
                self.assertNotIn(name, self.state_file.read_text())
                self.assertIn(f"unexpected field {name!r}", sent[0]["body"])

    def test_a_required_known_field_is_a_code_without_its_name(self):
        self.site.reviews[self.p.sel] = self.p.review().replace(
            f'name="coach_{self.p.sel}"', f'name="coach_{self.p.sel}" required')
        self.run_mode("live")
        self.assertEqual(self.record()["problems"], ["field_required"])
        self.assertNotIn("coach_", self.state_file.read_text())



# --- second independent review ---------------------------------------------------

class TestHaltIsTheKey(AutobookTestCase):
    def test_any_value_halts_and_never_replaces_the_file(self):
        for value in ({}, None, [], "yes", 0, False, {"reason": "manual"}):
            with self.subTest(value=value):
                self.setUp()
                self.state_file.write_text(json.dumps({"autobook_halted": value, "known_open": {}}))
                code, sent = self.run_mode("live")
                self.assertEqual([s["path"] for s in self.site.sent], ["/booking"], "no auto-book requests")
                self.assertEqual(list(self.dir.glob("state.json.unreadable-*")), [],
                                 "an odd value is not 'wrong shape': the file isn't replaced")
                self.assertEqual(self.subjects(sent)[0], monitor.STATE_UNREADABLE_SUBJECT)
                self.assertIn('has an "autobook_halted" entry', sent[0]["body"])
                self.assertIn("autobook_halted", self.state(), "still halted")
                code, sent = self.run_mode("live")
                self.assertEqual([s["path"] for s in self.site.sent].count("/bookings"), 0)
                self.assertNotIn(monitor.STATE_UNREADABLE_SUBJECT, self.subjects(sent), "emailed once")

    def test_odd_halt_value_is_not_a_shape_problem(self):
        for value in (None, [], "x", 3):
            self.assertEqual(monitor.shape_problems({"autobook_halted": value}), [])
            self.assertTrue(monitor.is_halted({"autobook_halted": value}))
        self.assertFalse(monitor.is_halted({}))

    def test_only_deleting_the_key_resumes(self):
        self.state_file.write_text(json.dumps({"autobook_halted": None}))
        self.run_mode("live")
        state = self.state()
        state["autobook_halted"] = {}
        self.state_file.write_text(json.dumps({**state, "known_open": {}}))
        self.run_mode("live")
        self.assertEqual(self.site.posts_sent(), [], "an emptied value is still a halt")
        state = self.state()
        del state["autobook_halted"]
        self.state_file.write_text(json.dumps({**state, "known_open": {}}))
        self.site.posts = [parked_redirect()]
        self.site.pending = [self.p.pending_empty(), self.p.pending_with_ref()]
        self.run_mode("live")
        self.assertEqual(len(self.site.posts_sent()), 1)


class TestPushFailurePause(AutobookTestCase):
    """A failed state push: harmless without auto-book attempts; after one,
    a durable halt in state.json that only deleting the key clears."""

    def fail_push(self, blob="B1"):
        """What monitor.yml does after this run's push failed (the run file
        is the one the last run_mode() left behind)."""
        sent = []
        with mock.patch.dict(os.environ, {**EMAIL_ENV, "REMOTE_STATE_BLOB": blob}, clear=True), \
             mock.patch.object(monitor, "CONFIG_FILE", self.config_file), \
             mock.patch.object(monitor, "STATE_FILE", self.state_file), \
             mock.patch.object(monitor, "send_email", side_effect=lambda s, b, *a: sent.append({"subject": s, "body": b})), \
             contextlib.redirect_stdout(io.StringIO()):
            monitor.main(["--alert-state-not-saved", "push failed"])
        return sent

    def marker(self):
        return json.loads((self.dir / ".state_alert.json").read_text())

    def live(self, status="success", repo_state=None):
        """The next run. repo_state: what the repo's state.json is (a failed
        push means the run's own state never got there)."""
        self.site.sent.clear()
        if repo_state is not None:
            self.state_file.write_text(json.dumps(repo_state))
        else:
            self.state_file.write_text(json.dumps({**self.state(), "known_open": {}}))
        return self.run_mode("live", env={"PUSH_MARKER_STATUS": status, "REPO_STATE_BLOB": "B1"})

    def book_ok(self):
        self.site.posts = [parked_redirect()]
        self.site.pending = [self.p.pending_empty(), self.p.pending_with_ref()]

    def attempt_then_fail_push(self):
        before = self.state()
        self.book_ok()
        self.run_mode("live")
        self.assertEqual(len(self.site.posts_sent()), 1)
        sent = self.fail_push()
        return before, sent

    def test_a_failed_push_without_an_attempt_pauses_nothing(self):
        self.write_config([target(self.date, [("23:00", False)], locations=("LOC002",))])
        self.run_mode("live")
        sent = self.fail_push()
        self.assertEqual([m["subject"] for m in sent], [monitor.STATE_NOT_SAVED_SUBJECT], "still one email")
        self.assertIn("made no auto-book attempt", sent[0]["body"])
        self.assertFalse(self.marker()["autobook_attempted"])
        self.assertEqual(self.fail_push(), [], "once per episode")
        self.write_config([target(self.date, [("23:00", True)], locations=("LOC002",))])
        self.book_ok()
        self.live(repo_state={})
        self.assertEqual(len(self.site.posts_sent()), 1, "no pause")
        self.assertNotIn("autobook_halted", self.state())

    def test_a_failed_push_after_an_attempt_halts_durably(self):
        before, sent = self.attempt_then_fail_push()
        self.assertEqual(self.marker()["autobook_keys"], [self.key])
        self.assertTrue(self.marker()["autobook_attempted"])
        self.assertIn(self.key, sent[0]["body"])
        # The next run starts from the repo's state.json, which lacks this run's records.
        code, sent = self.live(repo_state=before)
        self.assertEqual(self.site.posts_sent(), [])
        self.assertNotIn("/bookings", [x["path"] for x in self.site.sent], "no auto-book requests")
        halt = self.state()["autobook_halted"]
        self.assertEqual((halt["reason"], halt["keys"]), (monitor.PUSH_HALT_REASON, [self.key]))
        self.assertNotIn("SPORTS-", self.state_file.read_text(), "no refs")
        halt_mail = [m for m in sent if m["subject"] == monitor.STATE_UNREADABLE_SUBJECT]
        self.assertEqual(len(halt_mail), 1)
        self.assertIn(self.key, halt_mail[0]["body"])
        self.assertIn(monitor.HALT_RESTORE_STEPS, halt_mail[0]["body"])
        self.assertTrue(any(m["subject"].startswith("🎾") for m in sent), "notify-only carries on")
        # Later runs (their pushes succeed): still halted, no repeat email, the marker isn't re-applied.
        for _ in range(3):
            code, sent = self.live()
            self.assertEqual(self.site.posts_sent(), [])
            self.assertIn("autobook_halted", self.state())
            self.assertNotIn(monitor.STATE_UNREADABLE_SUBJECT, self.subjects(sent))
        # Only deleting the key clears it — and the marker (still in the cache)
        # isn't applied again. The lost booking is still on My bookings, so the
        # existing-bookings check stops a second one.
        state = self.state()
        del state["autobook_halted"]
        self.site.pending = [self.p.pending_with_ref()]
        code, sent = self.live(repo_state={**state, "known_open": {}})
        self.assertNotIn("autobook_halted", self.state())
        self.assertEqual(self.site.posts_sent(), [])
        self.assertIn(f"ℹ️ Already booked: {self.date} 23:00 — not auto-booking", self.subjects(sent))

    def test_the_halt_is_carried_through_another_failed_push(self):
        before, _ = self.attempt_then_fail_push()
        self.live(repo_state=before)  # writes the halt... but this push fails too
        self.fail_push()
        self.assertTrue(self.marker()["autobook_attempted"], "carried into the next marker")
        self.assertEqual(self.marker()["autobook_keys"], [self.key])
        self.live(repo_state=before)  # the repo still has neither
        self.assertEqual(self.state()["autobook_halted"]["keys"], [self.key])
        self.assertEqual(self.site.posts_sent(), [])

    def test_a_halt_already_there_gets_the_keys_added(self):
        before, _ = self.attempt_then_fail_push()
        self.live(repo_state={**before, "autobook_halted": None})
        halt = self.state()["autobook_halted"]
        self.assertEqual(halt["push_failed_keys"], [self.key])

    def test_an_unreadable_cache_blocks_only_that_run(self):
        self.book_ok()
        self.live(status="failure")
        self.assertEqual(self.site.posts_sent(), [])
        self.assertIn("autobook_blocked", self.state())
        self.assertNotIn("autobook_halted", self.state())
        self.book_ok()
        self.live()
        self.assertEqual(len(self.site.posts_sent()), 1)
        self.assertNotIn("autobook_blocked", self.state())

    def test_an_unreadable_marker_is_a_halt_with_unknown_keys(self):
        for raw in ("{x", "[1]", json.dumps({"no": "episode"})):
            with self.subTest(raw=raw):
                self.setUp()
                (self.dir / ".state_alert.json").write_text(raw)
                self.book_ok()
                self.live()
                self.assertEqual(self.site.posts_sent(), [])
                halt = self.state()["autobook_halted"]
                self.assertEqual((halt["reason"], halt["keys_unknown"]), (monitor.PUSH_HALT_REASON, True))
                state = self.state()
                del state["autobook_halted"]
                self.book_ok()
                self.live(repo_state={**state, "known_open": {}})
                self.assertEqual(len(self.site.posts_sent()), 1, "handled once; deleting the key resumes")

    def test_a_missing_run_file_at_the_alert_is_assumed_to_have_attempts(self):
        self.run_mode("live")
        (self.dir / ".autobook_run.json").unlink()
        sent = self.fail_push()
        self.assertTrue(self.marker()["autobook_attempted"])
        self.assertTrue(self.marker()["keys_unknown"])
        self.live(repo_state={})
        self.assertTrue(self.state()["autobook_halted"]["keys_unknown"])

    def test_no_run_file_means_no_auto_book(self):
        self.book_ok()
        with mock.patch.object(monitor, "write_run_file", side_effect=OSError("disk")):
            self.live()
        self.assertEqual(self.site.posts_sent(), [])
        self.assertEqual([x["path"] for x in self.site.sent], ["/booking"], "no auto-book requests at all")

    def test_the_push_failure_halt_is_on_disk_before_an_early_return(self):
        before, _ = self.attempt_then_fail_push()
        self.state_file.write_text(json.dumps(before))
        with mock.patch.dict(os.environ, {"PUSH_MARKER_STATUS": "success", "EMAIL_USER": "", "EMAIL_APP_PASSWORD": ""}, clear=True), \
             mock.patch.object(monitor, "CONFIG_FILE", self.config_file), \
             mock.patch.object(monitor, "STATE_FILE", self.state_file), contextlib.redirect_stdout(io.StringIO()):
            code = monitor.run(iterations=1, interval=0, jar_path=self.jar, enc_path=self.enc)
        self.assertEqual(code, monitor.EXIT_SETUP, "returned early: no email settings")
        self.assertEqual(self.state()["autobook_halted"]["reason"], monitor.PUSH_HALT_REASON)

    def test_an_attempt_that_cant_be_noted_is_never_posted(self):
        self.book_ok()
        with mock.patch.object(monitor, "note_run_attempt", side_effect=OSError("disk")):
            code, sent = self.live()
        self.assertEqual(self.site.posts_sent(), [])
        self.assertTrue(sent[0]["subject"].startswith("🔴 Failed: "))
        self.assertIn("did NOT send", sent[0]["body"])

    def test_the_run_file_lists_this_runs_attempts_before_the_post(self):
        def check(_n):
            info = json.loads((self.dir / ".autobook_run.json").read_text())
            self.assertEqual(info["attempt_keys"], [self.key], "noted before the write-ahead and the POST")
        self.site.on_post = check
        self.book_ok()
        self.run_mode("live")
        self.assertEqual(len(self.site.posts_sent()), 1)

    def test_workflow_restores_the_marker_before_the_monitor_runs(self):
        wf = (Path(__file__).resolve().parent / ".github" / "workflows" / "monitor.yml").read_text(encoding="utf-8")
        restore = wf.index("id: push_marker")
        run = wf.index("- name: Run monitor")
        self.assertLess(restore, run)
        self.assertIn("continue-on-error: true", wf[restore - 300:run])
        self.assertIn("PUSH_MARKER_STATUS: ${{ steps.push_marker.outcome }}", wf)


class TestOtherLocationWording(AutobookTestCase):
    def test_same_pass_attempt_at_the_other_location_is_named(self):
        self.write_config([target(self.date, [("23:00", True)], locations=("LOC001", "LOC002"))])
        code, sent = self.run_mode("live")  # LOC001 (tried first) ends 🔴: the pages are G's
        slot = next(m for m in sent if m["subject"].startswith("🎾"))
        self.assertIn("auto-book already attempted at Crystal Sports (result: 🔴) — see that email", slot["body"])
        self.assertNotIn("no result yet", slot["body"])

    def test_an_earlier_attempt_at_the_other_location_is_named(self):
        self.write_config([target(self.date, [("23:00", True)], locations=("LOC001", "LOC002"))])
        self.state_file.write_text(json.dumps({
            "known_open": {monitor.slot_key(self.date, "LOC001", monitor.ANY_COURT, "23:00"): True},
            autobook.LIVE_KEY: {self.key: {"status": "parked", "loc": "LOC001", "date": self.date, "time": "23:00",
                                           "attempts": [{"n": 1}]}}}))
        code, sent = self.run_mode("live")
        self.assertEqual(self.site.posts_sent(), [])
        slot = next(m for m in sent if m["subject"].startswith("🎾"))
        self.assertIn("— Crystal Sports G (", slot["body"])
        self.assertIn("auto-book already attempted at Crystal Sports (result: 🟡 booked, awaiting payment) — see that email",
                      slot["body"])

    def test_every_status_has_wording_never_a_question_mark(self):
        statuses = set(autobook.TERMINAL) | {"refused", "started", "dry_run", "something new", ""}
        for status in sorted(statuses):
            with self.subTest(status=status):
                a = {"starred": True, "date": self.date, "time": "23:00", "loc_id": "LOC002"}
                state = {autobook.LIVE_KEY: {self.key: {"status": status, "loc": "LOC001"}}}
                note = monitor.autobook_note(a, "live", {}, state)
                self.assertIn("auto-book already attempted at Crystal Sports (result: ", note)
                self.assertNotIn("?", note)
                if status in autobook.RESULT_TEXT:
                    self.assertIn(autobook.RESULT_TEXT[status], note)
                else:
                    self.assertIn(autobook.RESULT_UNKNOWN, note)
        for status in ("refused", "started", "dry_run"):
            self.assertIn(status, autobook.RESULT_TEXT)
        for status in autobook.TERMINAL:
            self.assertIn(status, autobook.RESULT_TEXT, "every stored status is worded")

    def test_no_attempt_elsewhere_keeps_the_usual_note(self):
        self.write_config([target(self.date, [("23:00", True), ("22:00", False)], locations=("LOC002",))])
        self.site.grid["LOC002"] = self.p.grid_with_2200_open()
        self.site.pending = [self.p.pending_with_ref(OTHER)]  # unreadable booking: no attempt, no result email
        self.site.details[OTHER] = (500, [], "x")
        self.state_file.write_text(json.dumps({autobook.RETRY_KEY: {self.key: {"notified": "x", "reason": "r"}}}))
        code, sent = self.run_mode("live")
        slot = next(m for m in sent if m["subject"].startswith("🎾"))
        self.assertIn(monitor.AUTOBOOK_NOTES["live"], slot["body"])


class TestNewPendingBookingIsReadBeforeParked(AutobookTestCase):
    def test_its_own_page_must_show_this_slot(self):
        cases = {
            "another time": (self.p.detail(REF, time="21:00"), "listed_other_slot"),
            "another date": (self.p.detail(REF, d=self.d + timedelta(days=1)), "listed_other_slot"),
            "page unreadable": ((500, [], "x"), "new_booking_unreadable"),
            "page for another booking": (self.p.detail("SPORTS-900000000098"), "new_booking_unreadable"),
        }
        for label, (page, reason) in cases.items():
            with self.subTest(label):
                self.setUp()
                self.site.details[REF] = page
                self.site.posts = [parked_redirect()]
                self.site.pending = [self.p.pending_empty(), self.p.pending_with_ref()]
                code, sent = self.run_mode("live")
                self.assertEqual(len(self.site.posts_sent()), 1, "never a second court")
                self.assertEqual(len(sent), 1)
                self.assertTrue(sent[0]["subject"].startswith("🟠 Unclear"), sent[0]["subject"])
                self.assertEqual(self.record()["reason"], reason)
                self.assertIn(f"/bookings/{REF}", [x["path"] for x in self.site.sent])

    def test_a_full_pre_check_budget_never_hides_the_real_booking(self):
        # Ten pending bookings you made yourself (at other times) use up the
        # whole pre-booking read budget; the bot's new booking is still read
        # from its own page on the post-POST budget, and is 🟡.
        others = [f"SPORTS-9000000001{i:02d}" for i in range(autobook.MAX_DETAIL_READS)]
        rows = "".join(self.p.pending_with_ref(r).split('<div id="bookingList">')[1].split("</div>")[0] for r in others)
        page = self.p.pending_empty().replace('<div id="bookingList">', '<div id="bookingList">' + rows, 1)
        after = page.replace('<div id="bookingList">', '<div id="bookingList">' + self.p.pending_with_ref()
                             .split('<div id="bookingList">')[1].split("</div>")[0], 1)
        for i, r in enumerate(others):
            self.site.details[r] = self.p.detail(r, time=f"{8 + i:02d}:00")
        self.site.posts = [parked_redirect()]
        self.site.pending = [page, after]
        code, sent = self.run_mode("live")
        reads = [x["path"] for x in self.site.sent if x["path"].startswith("/bookings/")]
        self.assertEqual(len(reads), autobook.MAX_DETAIL_READS + 1, "the cap was used up, then one post-POST read")
        self.assertEqual(reads[-1], f"/bookings/{REF}")
        self.assertTrue(sent[0]["subject"].startswith("🟡"), sent[0]["subject"])

    def test_matching_page_is_parked(self):
        self.site.posts = [parked_redirect()]
        self.site.pending = [self.p.pending_empty(), self.p.pending_with_ref()]
        code, sent = self.run_mode("live")
        self.assertTrue(sent[0]["subject"].startswith("🟡"))



class TestWhenItFires(AutobookTestCase):
    """The old monitor's trigger (attempt_autobook() only inside `if k not in
    known_open:`): a starred time is attempted on the check where it NEWLY
    opens. Plus stage 3's rule: an attempt that submitted nothing is retried
    while the time stays open; anything submitted never is."""

    def book_ok(self):
        self.site.posts = [parked_redirect()]
        self.site.pending = [self.p.pending_empty(), self.p.pending_with_ref()]

    def autobook_requests(self):
        return [x for x in self.site.sent if x["path"] != "/booking" or x["query"].get("time") != "0"]

    def run_again(self, grid=None):
        self.site.sent.clear()
        self.site.grid["LOC002"] = grid or self.p.grid()
        return self.run_mode("live")

    def test_starring_a_time_that_is_already_open_does_nothing_until_it_reopens(self):
        self.write_config([target(self.date, [("23:00", False)], locations=("LOC002",))])
        code, sent = self.run_mode("live")
        self.assertEqual([m["subject"][:1] for m in sent], ["🎾"], "alerted while unstarred")
        self.write_config([target(self.date, [("23:00", True)], locations=("LOC002",))])  # now starred
        self.book_ok()
        for _ in range(3):  # still open: not newly open
            code, sent = self.run_again()
            self.assertEqual(self.autobook_requests(), [], "no auto-book requests at all")
            self.assertEqual(sent, [], "and no email")
        code, sent = self.run_again(grid=self.p.grid_with_2300_closed())  # it closes
        self.assertEqual(self.autobook_requests(), [])
        code, sent = self.run_again()  # and opens again: newly open
        self.assertEqual(len(self.site.posts_sent()), 1)
        self.assertEqual(self.record()["status"], "parked")

    def test_a_newly_open_starred_time_is_attempted_on_that_check(self):
        self.book_ok()
        code, sent = self.run_mode("live")
        self.assertEqual(len(self.site.posts_sent()), 1)

    def test_an_attempt_that_submitted_nothing_is_retried_while_open(self):
        for label, first in (("site error", (503, [], "busy")),
                             ("network error", requests.exceptions.ConnectionError("reset"))):
            with self.subTest(label):
                self.setUp()
                if isinstance(first, BaseException):
                    calls = {"n": 0}
                    original = self.site.send

                    def send(request, _orig=original, **kw):
                        if urlsplit(request.url).path == "/bookings" and calls["n"] == 0:
                            calls["n"] += 1
                            raise first
                        return _orig(request, **kw)
                    self.site.send = send
                    self.site.pending = [self.p.pending_empty(), self.p.pending_with_ref()]
                else:
                    self.site.pending = [first, self.p.pending_empty(), self.p.pending_with_ref()]
                self.site.posts = [parked_redirect()]
                code, sent = self.run_mode("live")  # deferred: nothing submitted
                self.assertEqual(self.site.posts_sent(), [])
                self.assertIn(self.key, self.state()[autobook.RETRY_KEY])
                code, sent = self.run_again()       # still open, not newly open: retried
                self.assertEqual(len(self.site.posts_sent()), 1)
                self.assertEqual(self.record()["status"], "parked")

    def test_anything_submitted_or_finished_is_never_tried_again_even_after_reopening(self):
        outcomes = {
            "unclear (submitted)": lambda: setattr(self.site, "posts", [(500, [], "oops")]),
            "parked (submitted)": self.book_ok,
            "form changed (finished, nothing submitted)": lambda: self.site.reviews.__setitem__(
                self.p.sel, self.p.review().replace('name="acceptTerms"', 'name="accept"')),
            "already booked (finished)": lambda: setattr(self.site, "upcoming", [self.p.upcoming_filled()]),
        }
        for label, arrange in outcomes.items():
            with self.subTest(label):
                self.setUp()
                arrange()
                self.run_mode("live")
                status = self.record()["status"]
                self.assertNotIn(self.key, self.state().get(autobook.RETRY_KEY, {}))
                self.book_ok()
                self.run_again()                                   # still open
                self.run_again(grid=self.p.grid_with_2300_closed())  # closes
                code, sent = self.run_again()                      # reopens: newly open
                self.assertEqual(self.site.posts_sent(), [])
                self.assertNotIn("/booking/review", [x["path"] for x in self.site.sent])
                self.assertEqual(self.record()["status"], status)
                self.assertTrue(any(m["subject"].startswith("🎾") for m in sent), "the reopening is still alerted")


LOGIN_PAGE_200 = '<html><body><form action="/account/login"><input type="password"></form></body></html>'


class TestLoginRedirectNamesThePage(AutobookTestCase):
    """When a login redirect ends a run, the log names the page TYPE that
    redirected (grid / slot page / bookings / booking detail / review /
    confirm) — never a ref or URL."""

    OTHER = "SPORTS-900000000099"

    def answer_with_login(self, matches, response=None):
        """Serve `response` (default: a 302 to the login page) for the first
        request `matches(method, path, query)` accepts; the rest as normal."""
        real, hit = self.site.send, {"n": 0}

        def send(request, **kw):
            u = urlsplit(request.url)
            q = dict(parse_qsl(u.query, keep_blank_values=True))
            if not hit["n"] and matches(request.method, u.path, q):
                hit["n"] += 1
                self.site.sent.append({"method": request.method, "path": u.path, "query": q,
                                       "url": request.url, "form": []})
                return make_response(request, *(response or login_redirect()))
            return real(request, **kw)
        self.site.send = send
        return hit

    CASES = {
        "grid": lambda m, p, q: p == "/booking" and q.get("time") == "0",
        "bookings (pending)": lambda m, p, q: p == "/bookings" and q.get("tab") == "pending",
        "bookings": lambda m, p, q: p == "/bookings" and "tab" not in q,
        "booking detail": lambda m, p, q: p.startswith("/bookings/SPORTS-"),
        "slot page": lambda m, p, q: p == "/booking" and q.get("time") not in (None, "0"),
        "review": lambda m, p, q: p == "/booking/review",
        "confirm": lambda m, p, q: m == "POST",
    }

    def run_case(self, page, response=None):
        self.setUp()
        if page == "booking detail":
            self.site.pending = [self.p.pending_with_ref(self.OTHER)]
            self.site.details[self.OTHER] = self.p.detail(self.OTHER, time="20:00")
        hit = self.answer_with_login(self.CASES[page], response)
        code, sent = self.run_mode("live")
        self.assertEqual(hit["n"], 1, "the redirect was served where intended")
        self.assertEqual(len(self.site.posts_sent()), int(page == "confirm"),
                         "no POST unless the redirect answered the POST itself")
        self.assertEqual(code, monitor.EXIT_SESSION)
        return self.out

    def test_each_page_type_that_redirects_is_named_in_the_log(self):
        for page in self.CASES:
            with self.subTest(page):
                out = self.run_case(page)
                if page == "grid":
                    self.assertIn("🔴 login redirect on the grid page (", out)
                else:
                    self.assertIn(f"🔴 login redirect during auto-book on the {page} page (redirect)", out)
                lines = [l for l in out.splitlines() if "🔴 login redirect" in l]
                self.assertEqual(len(lines), 1, lines)
                for secret in (REF, self.OTHER, *COOKIES.values()):
                    self.assertNotIn(secret, out)

    def test_a_login_page_served_as_200_is_named_too(self):
        for page in ("bookings (pending)", "slot page", "review"):
            with self.subTest(page):
                out = self.run_case(page, (200, [("Content-Type", "text/html")], LOGIN_PAGE_200))
                self.assertIn(f"🔴 login redirect during auto-book on the {page} page (login page (200))", out)

    def test_page_type_classifies_every_allowed_request(self):
        pt = bot_session.page_type
        self.assertEqual(pt("GET", "/booking", {"date": "x", "loc": "LOC001", "time": "0", "sel": ""}), "grid")
        self.assertEqual(pt("GET", "/booking", {"date": "x", "loc": "LOC001", "time": "23"}), "slot page")
        self.assertEqual(pt("GET", "/booking/review", {"sel": "abc"}), "review")
        self.assertEqual(pt("GET", "/bookings", {}), "bookings")
        self.assertEqual(pt("GET", "/bookings", {"tab": "pending"}), "bookings (pending)")
        self.assertEqual(pt("GET", f"/bookings/{REF}", {}), "booking detail")
        self.assertEqual(pt("POST", "/booking/confirm"), "confirm")
        self.assertEqual(pt("GET", "/somewhere-else"), "unknown")


class TestPostIsImpossibleElsewhere(unittest.TestCase):
    def setUp(self):
        self.jar = bot_session.Jar(session=requests.Session(), path=Path("/nonexistent/never-written"))

    def test_post_only_to_confirm_and_only_live(self):
        for mode in ("off", "dry_run", "", None, "LIVE"):
            with self.subTest(mode=mode), self.assertRaises(RuntimeError):
                bot_session.post_booking_confirm(self.jar, [("sel", "x")], mode, referer="x")
        for path in ("/booking", "/booking/review", "/bookings", "/wallet/topup/1", "/account/logout", "/booking/confirm/x"):
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                bot_session._send(self.jar, "POST", path, data=[("a", "b")])
        for method in ("PUT", "DELETE", "PATCH"):
            with self.subTest(method=method), self.assertRaises(RuntimeError):
                bot_session._send(self.jar, method, "/booking/confirm", data=[("a", "b")])
        with self.assertRaises(RuntimeError):
            bot_session._send(self.jar, "GET", "/booking/confirm")
        with self.assertRaises(RuntimeError):
            bot_session._send(self.jar, "GET", "/booking", data=[("a", "b")])

    def test_booking_pages_are_get_only_and_exactly_that_shape(self):
        refused_gets = ["/bookings/SPORTS-1/sheet", "/bookings/SPORTS-1/cancel", "/bookings/lines/1/cancel",
                        "/bookings/SPORTS-", "/bookings/SPORTS-1x", "/bookings/sports-1", "/bookings/SPORTS-1/",
                        "/bookings/SPORTS-1?x=1", "/bookings/SPORTS-1#x", "/bookings/SPORTS-" + "1" * 21,
                        "/bookings/../booking/confirm", "/payment/SPORTS-1/topup-shortfall", "/wallet/topup/1"]
        for path in refused_gets:
            with self.subTest(get=path), self.assertRaises(RuntimeError):
                bot_session._send(self.jar, "GET", path)
        for method in ("POST", "PUT", "DELETE"):
            with self.subTest(method=method), self.assertRaises(RuntimeError):
                bot_session._send(self.jar, method, "/bookings/SPORTS-1", data=[("bookingRef", "SPORTS-1")])
        # And the one allowed shape does reach the network layer (a fake one here).
        site = FakeSite(Pages(FIXTURE_DATE))
        site.details["SPORTS-123"] = "<html></html>"
        self.jar.session.mount("https://", site)
        with mock.patch.object(bot_session, "save_jar"):
            bot_session.fetch(self.jar, "/bookings/SPORTS-123", {})
        self.assertEqual([(x["method"], x["path"]) for x in site.sent], [("GET", "/bookings/SPORTS-123")])

    def test_only_one_place_in_the_code_can_post(self):
        root = Path(__file__).resolve().parent
        offenders = []
        for f in root.glob("*.py"):
            if f.name.startswith("test_"):
                continue
            src = f.read_text(encoding="utf-8")
            for m in re.finditer(r'\.post\(|"POST"|\'POST\'|method="post"|requests\.request\(', src):
                offenders.append(f"{f.name}:{src[:m.start()].count(chr(10)) + 1}:{m.group(0)}")
        self.assertEqual(sorted(offenders), sorted([
            "bot_session.py:" + str(i) + ':"POST"' for i in self._post_lines(root / "bot_session.py")]),
            "only bot_session.py may mention POST")

    @staticmethod
    def _post_lines(path):
        return [i for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) if '"POST"' in line]

    def test_bot_session_post_lines_are_the_guard_and_the_one_post(self):
        lines = [l.strip() for l in (Path(__file__).resolve().parent / "bot_session.py").read_text().splitlines() if '"POST"' in l]
        self.assertEqual(len(lines), 3, lines)  # the allow-list, the one POST call, the body rule


class TestFixturesAreScrubbed(unittest.TestCase):
    def test_no_tokens_refs_or_names_from_the_live_pages(self):
        for f in FIX.glob("*.html"):
            t = f.read_text(encoding="utf-8")
            with self.subTest(f.name):
                self.assertTrue(t.startswith("<!-- SCRUBBED copy"), "fixtures must say they're scrubbed")
                self.assertIsNone(re.search(r"CfDJ8[\w-]{20,}", t), "real anti-forgery token")
                self.assertIsNone(re.search(r"[A-Za-z0-9+/=]{300,}", t), "embedded image/QR data")
                for ref in re.findall(r"SPORTS-\d+", t):
                    self.assertTrue(ref.startswith("SPORTS-9000000000"), ref)
                self.assertNotIn("นจ", t)
                for line_id in re.findall(r"/bookings/lines/(\d+)", t):
                    self.assertTrue(line_id.startswith("9000"), "real booking line id")
        self.assertEqual(sorted(f.name for f in FIX.glob("*.html"))[-4:],
                         ["6_bookings_upcoming_filled.html", "7_booking_detail.html", "8_login.html", "8_login_en.html"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
