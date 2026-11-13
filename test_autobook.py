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
                        status="Paid"):
        """The REAL filled Upcoming page (one Paid row: 15 Nov, 23:00–24:00,
        G South-1, Crystal Sports G). With arguments it is DERIVED: the same
        row moved to another ref/day/time/location/court/status."""
        d = d or self.d
        end = f"{int(time[:2]) + 1:02d}:{time[3:]}"
        return (fixture("6_bookings_upcoming_filled.html")
                .replace(REF2, ref)
                .replace("<b>15</b><span>Nov</span>", f"<b>{d.day}</b><span>{MONTH_ABBR[d.month - 1]}</span>")
                .replace("23:00–24:00 · G South-1", f"{time}–{end} · {court}")
                .replace(f"Crystal Sports G · {ref}", f"{loc_name} · {ref}")
                .replace('badge-dot">Paid<', f'badge-dot">{status}<'))

    def detail(self, ref, d=None, time="23:00", loc_name="Crystal Sports G", court="G South-1"):
        """A booking's own page /bookings/SPORTS-… — the REAL page (15 Nov,
        23:00–24:00, G South-1), DERIVED to another ref/date/time/court."""
        d = d or self.d
        end = f"{int(time[:2]) + 1:02d}:{time[3:]}"
        return (fixture("7_booking_detail.html").replace(REF2, ref)
                .replace(autobook.date_label("2026-11-15") + " · 23:00–24:00", f"{autobook.date_label(d.isoformat())} · {time}–{end}")
                .replace("Crystal Sports G · G South-1", f"{loc_name} · {court}"))

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
        self.details = {}                            # ref -> a booking's own page (or a response tuple)
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
            return make_response(request, 200, [("Content-Type", "text/html")], self._next(queue))
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

    def run_mode(self, mode, iterations=1, raise_ok=False):
        session = requests.Session()
        session.mount("https://", self.site)
        sent = []
        out = io.StringIO()
        env = {**EMAIL_ENV, **({"AUTOBOOK_MODE": mode} if mode is not None else {})}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(monitor, "CONFIG_FILE", self.config_file), \
             mock.patch.object(monitor, "STATE_FILE", self.state_file), \
             mock.patch.object(monitor, "send_email", side_effect=lambda s, b, *a: sent.append({"subject": s, "body": b})), \
             mock.patch.object(monitor.time, "sleep"), \
             contextlib.redirect_stdout(out):
            code = monitor.run(iterations=iterations, interval=0, jar_path=self.jar, enc_path=self.enc, session=session)
        self.out = out.getvalue()
        return code, sent

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
                                 ("POST", "/booking/confirm", ""), ("GET", "/bookings", "pending"), ("GET", "/bookings", "")])

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
        self.assertEqual([a["court"] for a in rec["attempts"]], ["G South-1", "G South-3"])

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


class TestListParsing(unittest.TestCase):
    """The real filled Upcoming row and booking page, parsed directly."""

    def test_real_upcoming_row(self):
        page = booking_page.parse_bookings_list(fixture("6_bookings_upcoming_filled.html"), today=_date(2026, 11, 13))
        self.assertEqual(page["tab"], "upcoming")
        self.assertEqual(page["rows"], [{
            "ref": REF2, "status": "Paid", "slot": ("2026-11-15", "23:00"), "loc_name": "Crystal Sports G",
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
                         ("2027-01-02", "23:00"), "Dec -> Jan")
        self.assertEqual(booking_page.infer_play_date(29, 2, _date(2026, 3, 1)), _date(2028, 2, 29))
        self.assertIsNone(booking_page.infer_play_date(31, 2, _date(2026, 3, 1)))

    def test_bangkok_today_is_the_default(self):
        with mock.patch.object(booking_page, "bangkok_today", return_value=_date(2026, 11, 16)):
            row = booking_page.parse_bookings_list(fixture("6_bookings_upcoming_filled.html"))["rows"][0]
        self.assertEqual(row["slot"], ("2027-11-15", "23:00"))

    def test_real_awaiting_payment_row_has_no_slot(self):
        rows = booking_page.parse_bookings_list(fixture("5_bookings_awaiting.html"))["rows"]
        self.assertEqual([(r["ref"], r["status"], r["slot"]) for r in rows], [(REF, "Awaiting payment", None)])

    def test_real_booking_page(self):
        page = booking_page.parse_booking_detail(fixture("7_booking_detail.html"))
        self.assertEqual(page, {"ref": REF2, "status": "Paid", "lines": [
            {"loc_name": "Crystal Sports G", "court": "G South-1", "slot": ("2026-11-15", "23:00")}]})


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
        self.assertEqual(self.subjects(sent)[0], f"🟠 Unclear — check My bookings: Crystal Sports G G South-1 {self.date} 23:00 ฿500")
        self.assertEqual(len([m for m in sent if m["subject"].startswith("🟠")]), 1)
        self.assertEqual(self.record()["status"], "interrupted")

        code, sent = self.run_mode("live")
        self.assertEqual(self.site.posts_sent(), [])
        self.assertEqual([m for m in sent if m["subject"].startswith("🟠")], [], "🟠 only once")

    def test_write_ahead_that_cannot_be_saved_means_no_post(self):
        real_save = monitor.save_state
        calls = {"n": 0}

        def save(state):
            if state.get(autobook.LIVE_KEY, {}).get(self.key, {}).get("status") == "in_progress":
                raise OSError("disk full")
            return real_save(state)
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
                self.state_file.unlink(missing_ok=True)
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
        self.assertEqual(sorted(f.name for f in FIX.glob("*.html"))[-2:],
                         ["6_bookings_upcoming_filled.html", "7_booking_detail.html"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
