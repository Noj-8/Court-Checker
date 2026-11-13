"""
Auto-book for starred times on the new Crystal Sports site (stage 3).

REAL MONEY. Read this before changing anything.

Mode comes from the AUTOBOOK_MODE repository variable (monitor.yml passes
vars.AUTOBOOK_MODE): "off" (default — the monitor's notify-only
behaviour, nothing here runs), "dry_run" (every check up to and including
the verified review form, then a 🧪 email instead of booking), "live".

The flow for a starred time that is newly open (or was deferred earlier
without ever POSTing):
  1. Key = date|HH:MM. At most ONE booking per key across both locations,
     ever: the attempt is recorded in state before any booking request.
  2. Read /bookings?tab=pending and /bookings. A listed booking showing this
     date+time -> skip (one ℹ️ email). A listed booking whose played
     date/time the list doesn't show (the live Awaiting-payment rows show
     only ref, booked-on time and status) -> can't be sure: no booking,
     one ℹ️ email per key, retried on a later pass. Bookings this bot made
     itself are recognised by their ref fingerprint and ignored.
  3. GET the slot's own link from the grid; check the page has that date,
     branch and time selected; take the first free court tile (site order).
  4. GET /booking/review?sel=..; verify branch, court, date, time, price, and
     that the form posts to /booking/confirm with exactly: sel, a method
     radio offering WALLET, __RequestVerificationToken, the terms checkbox,
     and the optional coach selects (left empty). Anything else, anything
     required or pre-filled -> 🔴 "booking form changed / page mismatch",
     no POST.
  5. dry_run: 🧪 "Would have booked ..." once per key. No POST.
  6. live: write-ahead (status in_progress) saved to state.json on disk,
     then the ONE POST (bot_session.post_booking_confirm) with method=WALLET,
     acceptTerms ticked and no coach fields; redirect not followed.
  7. Verify via both lists: our ref under Awaiting payment -> 🟡 Parked;
     under Upcoming -> 🟢 Paid; clearly refused (an error shown on the
     re-rendered form) AND not listed -> ONE other free court at the same
     time (back to 4), else 🔴 Failed; anything else -> 🟠 Unclear, never
     Failed.
  8. A key left in_progress by a run that died is never retried: 🟠 once.

state.json and the Actions logs are public, so booking/payment refs are
never stored or logged — only a short fingerprint. Emails carry them.
"""

import hashlib
import os
import re
from datetime import date as _date, datetime
from urllib.parse import parse_qs, urlsplit

import requests

import booking_page
import bot_session
from bot_session import BASE, BOOKINGS_PATH, BOOKING_PATH, REVIEW_PATH, TZ

MODES = ("off", "dry_run", "live")
LIVE_KEY, DRY_KEY, RETRY_KEY = "autobook", "autobook_dry_run", "autobook_retry"
PENDING_URL = f"{BASE}{BOOKINGS_PATH}?tab=pending"
BOOKINGS_URL = f"{BASE}{BOOKINGS_PATH}"
REF_RE = re.compile(r"SPORTS-\d+")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
TERMINAL = {"skipped_existing", "aborted_mismatch", "dry_run", "in_progress", "interrupted",
            "paid", "parked", "failed", "unclear", "not_recorded"}


class SessionStopped(Exception):
    """A login redirect: the session is gone; the pass must stop."""


class Deferred(Exception):
    """Nothing was booked and it's safe to try again on a later pass."""


def mode_from_env(log):
    raw = os.environ.get("AUTOBOOK_MODE", "").strip().lower()
    if raw in ("", "off"):
        return "off"
    if raw in MODES:
        return raw
    log(f"⚠ AUTOBOOK_MODE={raw!r} isn't one of {', '.join(MODES)} — treating it as 'off'")
    return "off"


def key_for(date_iso, hhmm):
    return f"{date_iso}|{hhmm}"


def date_label(date_iso):
    """'2026-11-15' -> 'Sun 15 Nov 2026', as the site writes it (English)."""
    d = _date.fromisoformat(date_iso)
    return f"{WEEKDAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]} {d.year}"


def ref_fp(ref):
    return hashlib.sha256(ref.encode()).hexdigest()[:12]


def now_iso():
    return datetime.now(TZ).isoformat(timespec="seconds")


class Context:
    """What the flow needs from monitor.py (passed in to avoid a circular import)."""

    def __init__(self, jar, state, mode, send, save_state, try_save_state, log, locations):
        self.jar, self.state, self.mode = jar, state, mode
        self.send, self.save_state, self.try_save_state, self.log = send, save_state, try_save_state, log
        self.locations = locations
        self.emailed_keys = set()

    def records(self):
        return self.state.setdefault(LIVE_KEY if self.mode == "live" else DRY_KEY, {})

    def retries(self):
        return self.state.setdefault(RETRY_KEY, {})

    def own_ref_fps(self):
        fps = set()
        for rec in self.state.get(LIVE_KEY, {}).values():
            fps.update(a.get("ref_fp") for a in rec.get("attempts", []) if a.get("ref_fp"))
        return fps

    def email(self, key, subject, body):
        self.emailed_keys.add(key)
        try:
            self.send(subject, body)
            self.log(f"✉ {subject.split(' ', 1)[0]} email sent for {key}")
        except Exception as e:
            self.log(f"⚠ auto-book email failed for {key}: {e}")


# --- requests (all through bot_session) -----------------------------------------

def _get(ctx, path, params):
    try:
        f = bot_session.fetch(ctx.jar, path, params)
    except requests.RequestException as e:
        raise Deferred(f"network error on {path}: {e}") from None
    if f.login:
        raise SessionStopped()
    if f.resp.status_code != 200:
        raise Deferred(f"http {f.resp.status_code} on {path}")
    body = f.resp.text or ""
    if booking_page.is_login_page(body, f.resp.url):
        raise SessionStopped()
    return body


def read_lists(ctx):
    """Both lists, as {"pending": [...rows], "upcoming": [...rows]}."""
    out = {}
    for name, params in (("pending", {"tab": "pending"}), ("upcoming", {})):
        page = booking_page.parse_bookings_list(_get(ctx, BOOKINGS_PATH, params))
        if not page["recognised"]:
            raise Deferred(f"the My bookings page ({name}) didn't look as expected")
        out[name] = page["rows"]
    return out


def slot_page(ctx, href):
    path, query = booking_page.query_of(href)
    if path != BOOKING_PATH:
        raise RuntimeError(f"slot link {href!r} isn't a /booking link")
    return booking_page.parse_slot_page(_get(ctx, BOOKING_PATH, query)), query


# --- verification ---------------------------------------------------------------

def check_slot_page(page, cand):
    problems = []
    want = date_label(cand["date"])
    if page["date_label"] != want:
        problems.append(f"selected date is {page['date_label']!r}, expected {want!r}")
    if page["branch"] != cand["loc_name"]:
        problems.append(f"selected branch is {page['branch']!r}, expected {cand['loc_name']!r}")
    if page["time"] != cand["time"]:
        problems.append(f"selected time is {page['time']!r}, expected {cand['time']!r}")
    return problems


def check_review(review, cand, court):
    """Every way the review page must look before we may POST. Returns
    (problems, post_fields, price)."""
    sel = court["sel"]
    problems = []
    if review["form_count"] != 1:
        problems.append(f"{review['form_count']} forms post to /booking/confirm (expected 1)")
    if review["form_count"] and review["method"] != "post":
        problems.append(f"the form method is {review['method']!r}, not post")

    items = review["items"]
    want_title = f"{cand['loc_name']} · {court['name']}"
    want_when = f"{date_label(cand['date'])} · {cand['time']}"
    price = None
    if len(items) != 1:
        problems.append(f"{len(items)} items in the booking (expected exactly 1)")
    else:
        item = items[0]
        if item["title"] != want_title:
            problems.append(f"booking item is {item['title']!r}, expected {want_title!r}")
        if not item["when"].startswith(want_when):
            problems.append(f"booking time is {item['when']!r}, expected it to start {want_when!r}")
        if not item["price"]:
            problems.append("no price shown for the court")
        price = item["price"]
        if review["total"] is not None and price and review["total"] != price:
            problems.append(f"total ฿{review['total']} differs from the court price ฿{price}")
    if review["page_date"] and review["page_date"] != date_label(cand["date"]):
        problems.append(f"page date is {review['page_date']!r}, expected {date_label(cand['date'])!r}")

    allowed = {"sel", "method", "acceptTerms", "__RequestVerificationToken", f"coach_{sel}", f"type_{sel}"}
    fields = review["fields"]
    by_name = {}
    for f in fields:
        by_name.setdefault(f["name"], []).append(f)
        if f["name"] not in allowed:
            problems.append(f"unexpected field {f['name']!r} ({f['type']}"
                            + (", required" if f["required"] else "")
                            + (f", pre-filled {f['value']!r}" if f["value"] else "") + ")")
        elif f["required"] and f["name"] != "acceptTerms" and not f["disabled"]:
            problems.append(f"field {f['name']!r} is now required")

    sels = by_name.get("sel", [])
    if len(sels) != 1 or sels[0]["type"] != "hidden" or sels[0]["value"] != sel:
        problems.append(f"sel field is {[(s['type'], s['value']) for s in sels]}, expected one hidden {sel!r}")
    methods = by_name.get("method", [])
    if not any(m["type"] == "radio" and m["value"] == "WALLET" for m in methods):
        problems.append("no WALLET payment-method radio")
    if any(m["type"] != "radio" for m in methods):
        problems.append("a non-radio 'method' field")
    terms = by_name.get("acceptTerms", [])
    if len(terms) != 1 or terms[0]["type"] != "checkbox" or not terms[0]["value"]:
        problems.append(f"terms checkbox missing or changed ({[(t['type'], t['value']) for t in terms]})")
    tokens = by_name.get("__RequestVerificationToken", [])
    if len(tokens) != 1 or tokens[0]["type"] != "hidden" or not tokens[0]["value"]:
        problems.append("anti-forgery token missing or changed")
    for opt in (f"coach_{sel}", f"type_{sel}"):
        for f in by_name.get(opt, []):
            if f["value"]:
                problems.append(f"{opt} is pre-filled with {f['value']!r}")

    if problems:
        return problems, None, price
    # Exactly what a browser sends, in document order, with Wallet chosen and
    # the terms box ticked — minus the coach selects (owner's rule: never
    # send coach fields; a browser would send coach_<sel>="" for "No coach").
    post, method_done = [], False
    for f in fields:
        if f["name"] == "sel":
            post.append(("sel", f["value"]))
        elif f["name"] == "method" and not method_done:
            post.append(("method", "WALLET"))
            method_done = True
        elif f["name"] == "acceptTerms":
            post.append(("acceptTerms", f["value"]))
        elif f["name"] == "__RequestVerificationToken":
            post.append((f["name"], f["value"]))
    return [], post, price


# --- the flow -------------------------------------------------------------------

def notify_interrupted(ctx):
    """Step 8: a key a previous run left in_progress (it died after the
    write-ahead, maybe after the POST) is never retried: one 🟠 email."""
    for key, rec in ctx.state.get(LIVE_KEY, {}).items():
        if rec.get("status") == "in_progress":
            rec["status"] = "interrupted"
            rec["result_at"] = now_iso()
            ctx.email(key, f"🟠 Unclear — check My bookings: {_what(rec)}",
                      "A run stopped in the middle of auto-booking this slot (after recording the attempt,\n"
                      "possibly after the booking request). It will NOT be retried.\n\n"
                      f"  {_what(rec)}\n\nCheck whether it's booked:\n  {PENDING_URL}\n  {BOOKINGS_URL}\n")
            ctx.try_save_state(ctx.state)


def _what(rec):
    court = f" {rec['court']}" if rec.get("court") else ""
    price = f" ฿{rec['price']}" if rec.get("price") else ""
    return f"{rec.get('loc_name', rec.get('loc', ''))}{court} {rec.get('date', '')} {rec.get('time', '')}{price}".strip()


def process(ctx, candidates):
    """Run the flow for each candidate {date, time, loc, loc_name, href,
    newly_open}. Raises SessionStopped on a login redirect. Never raises for
    anything else: every failure ends in a recorded outcome or a deferral."""
    if ctx.mode not in ("dry_run", "live"):
        return
    seen = set()
    for cand in candidates:
        key = key_for(cand["date"], cand["time"])
        if key in seen:
            continue  # one per date+time across both locations, even within a pass
        seen.add(key)
        if key in ctx.records():
            continue
        if not cand["newly_open"] and key not in ctx.retries():
            continue
        _process_one(ctx, key, cand)


def _process_one(ctx, key, cand):
    records = ctx.records()
    rec = records[key] = {"status": "started", "mode": ctx.mode, "loc": cand["loc"], "loc_name": cand["loc_name"],
                          "date": cand["date"], "time": cand["time"], "started_at": now_iso(), "attempts": []}
    ctx.log(f"  ★ auto-book ({ctx.mode}) {key} at {cand['loc_name']}")
    try:
        _flow(ctx, key, cand, rec)
    except SessionStopped:
        _after_error(ctx, key, rec, "the session ended", retry_reason="session expired")
        raise
    except Deferred as e:
        _after_error(ctx, key, rec, f"couldn't continue: {e}", retry_reason=str(e))
    except bot_session.JarSaveError:
        _after_error(ctx, key, rec, "the session jar couldn't be saved", retry_reason=None)
        raise
    except Exception as e:  # a bug: never make a POSTed key retryable, never book twice
        _after_error(ctx, key, rec, f"internal error {type(e).__name__}: {e}", retry_reason="internal error")
    if rec.get("status") in TERMINAL:
        ctx.retries().pop(key, None)


def _after_error(ctx, key, rec, what, retry_reason):
    """An exception interrupted the flow. Only a key that never had a POST
    sent may be retried; anything else gets a final outcome now."""
    if rec["status"] == "in_progress":
        _finish(ctx, key, rec, "unclear", f"after the booking request, {what}")
    elif rec["attempts"]:
        _finish(ctx, key, rec, "failed", f"the first court was refused and then {what}")
    elif rec["status"] == "started":
        del ctx.records()[key]
        if retry_reason:
            ctx.retries().setdefault(key, {"since": now_iso()})["reason"] = retry_reason
        ctx.log(f"    deferred ({what}) — nothing was submitted; will retry")


def _flow(ctx, key, cand, rec):
    # 2. existing bookings
    lists = read_lists(ctx)
    own = ctx.own_ref_fps()
    others = [r for r in lists["pending"] + lists["upcoming"] if ref_fp(r["ref"]) not in own]
    if any(r["slot"] == (cand["date"], cand["time"]) for r in others):
        rec["status"] = "skipped_existing"
        rec["result_at"] = now_iso()
        ctx.email(key, f"ℹ️ Already booked: {cand['date']} {cand['time']} — not auto-booking",
                  f"A starred time opened ({cand['loc_name']} {cand['date']} {cand['time']}), but My bookings already\n"
                  f"shows a booking at that date and time, so I didn't book another.\n\n  {BOOKINGS_URL}\n")
        return
    unreadable = [r for r in others if r["slot"] is None]
    if unreadable:
        retry = ctx.retries().setdefault(key, {"since": now_iso()})
        retry["reason"] = f"{len(unreadable)} existing booking(s) without a readable date/time"
        if not retry.get("notified"):
            retry["notified"] = now_iso()
            ctx.email(key, f"ℹ️ Not auto-booking {cand['date']} {cand['time']} — can't check your bookings",
                      f"A starred time opened ({cand['loc_name']} {cand['date']} {cand['time']}), but My bookings lists\n"
                      f"{len(unreadable)} booking(s) whose court date/time the list doesn't show, so I can't rule out\n"
                      "that you already have this slot. Not booking — book manually if you want it:\n\n"
                      f"  {BASE}{cand['href']}\n  {BOOKINGS_URL}\n\nI'll try again while it stays open; no more emails about it.\n")
        del ctx.records()[key]
        ctx.log(f"    not booking: {retry['reason']}")
        return

    tried = set()
    for attempt in (1, 2):
        # 3. the slot page and its free courts
        page, _ = slot_page(ctx, cand["href"])
        problems = check_slot_page(page, cand)
        if problems:
            return _mismatch(ctx, key, rec, cand, problems)
        free = [c for c in page["courts"] if c["available"] and c["sel"] not in tried]
        if not free:
            if attempt == 1:
                raise Deferred("no free court left on the slot page")
            return _finish(ctx, key, rec, "failed", "the court was refused and no other court was free")
        court = free[0]
        tried.add(court["sel"])
        result = _book_court(ctx, key, cand, rec, court, lists)
        if result != "refused":
            return
        ctx.log("    refused and not listed — trying one other court")
    _finish(ctx, key, rec, "failed", "two courts were refused")


def _book_court(ctx, key, cand, rec, court, lists_before):
    # 4. review + verify
    review_html = _get(ctx, REVIEW_PATH, {"sel": court["sel"]})
    review = booking_page.parse_review(review_html)
    problems, post, price = check_review(review, cand, court)
    rec["court"], rec["price"] = court["name"], price
    if problems:
        _mismatch(ctx, key, rec, cand, problems)
        return "stop"
    # 5. dry run stops here
    if ctx.mode == "dry_run":
        rec["status"] = "dry_run"
        rec["result_at"] = now_iso()
        ctx.email(key, f"🧪 Would have booked {cand['loc_name']} {court['name']} {cand['date']} {cand['time']} ฿{price}",
                  f"Dry run: every check passed and the booking form looks as expected. In live mode I would\n"
                  f"now book {cand['loc_name']} {court['name']} {cand['date']} {cand['time']} for ฿{price},\n"
                  "paying from the Wallet (method=WALLET, terms accepted, no coach).\n\nNothing was booked. "
                  f"Book manually if you want it:\n  {BASE}{cand['href']}\n")
        return "stop"
    # 6. live: write-ahead on disk, then the one POST
    attempt = {"court": court["name"], "sel": court["sel"], "at": now_iso()}
    rec["attempts"].append(attempt)
    rec["status"] = "in_progress"
    try:
        ctx.save_state(ctx.state)
    except Exception as e:
        rec["status"] = "not_recorded"
        rec["result_at"] = now_iso()
        ctx.email(key, f"🔴 Failed: {_what(rec)}",
                  f"I couldn't record the attempt in state.json ({type(e).__name__}: {e}), so I did NOT send the\n"
                  f"booking request. Book manually if you want it:\n  {BASE}{cand['href']}\n")
        return "stop"
    review_url = f"{BASE}{REVIEW_PATH}?sel={court['sel']}"
    hint, location, refused_msgs = "none", "", []
    try:
        f = bot_session.post_booking_confirm(ctx.jar, post, ctx.mode, referer=review_url)
        attempt["http"] = f.resp.status_code
        location = f.resp.headers.get("Location", "")
        if f.login:
            hint = "login"
        elif 300 <= f.resp.status_code < 400 and urlsplit(location).path.startswith("/wallet/topup/"):
            hint = "parked"
        elif f.resp.status_code == 200:
            refused_msgs = booking_page.page_error_messages(f.resp.text or "")
            hint = "refused" if refused_msgs else "re-rendered"
        else:
            hint = "other"
    except requests.RequestException as e:
        hint = f"network: {type(e).__name__}"
    attempt["hint"] = hint
    ref_match = REF_RE.search(" ".join(parse_qs(urlsplit(location).query).get("then", [])) or location)
    ref = ref_match.group(0) if ref_match else None
    if ref:
        attempt["ref_fp"] = ref_fp(ref)
    ctx.log(f"    POST /booking/confirm -> {attempt.get('http', '-')} ({hint})")
    ctx.try_save_state(ctx.state)
    if hint == "login":
        raise SessionStopped()

    # 7. verify
    lists = read_lists(ctx)
    before = {r["ref"] for r in lists_before["pending"] + lists_before["upcoming"]}
    pending = {r["ref"]: r for r in lists["pending"]}
    upcoming = {r["ref"]: r for r in lists["upcoming"]}
    if not ref:
        new = [r for r in list(pending) + list(upcoming) if r not in before and ref_fp(r) not in ctx.own_ref_fps()]
        if len(set(new)) == 1:
            ref = new[0]
            attempt["ref_fp"] = ref_fp(ref)
    row = pending.get(ref) or upcoming.get(ref) if ref else None
    if row is not None and row["slot"] not in (None, (cand["date"], cand["time"])):
        return _finish(ctx, key, rec, "unclear", f"the new booking is listed for {row['slot']}, not "
                       f"{cand['date']} {cand['time']}", ref=ref, location=location)
    if ref and ref in pending:
        return _finish(ctx, key, rec, "parked", None, ref=ref, location=location)
    if ref and ref in upcoming:
        return _finish(ctx, key, rec, "paid", None, ref=ref, location=location)
    if hint == "refused" and not ref:
        rec["status"] = "refused"  # nothing booked; the caller may try one other court
        attempt["refused"] = "; ".join(refused_msgs)[:200]
        return "refused"
    return _finish(ctx, key, rec, "unclear",
                   f"after the booking request the site answered {attempt.get('http', 'nothing')} ({hint}) and the "
                   "booking isn't clearly listed", ref=ref, location=location)


def _mismatch(ctx, key, rec, cand, problems):
    rec["status"] = "aborted_mismatch"
    rec["result_at"] = now_iso()
    rec["problems"] = problems[:10]
    ctx.email(key, f"🔴 Failed: booking form changed / page mismatch — {cand['date']} {cand['time']}",
              f"I stopped before booking {cand['loc_name']} {cand['date']} {cand['time']} because the pages didn't\n"
              "look exactly as expected. Nothing was submitted. What differed:\n\n"
              + "\n".join(f"  • {p}" for p in problems)
              + f"\n\nBook manually if you want it:\n  {BASE}{cand['href']}\n")


def _finish(ctx, key, rec, status, reason, ref=None, location=""):
    rec["status"] = status
    rec["result_at"] = now_iso()
    if reason:
        rec["reason"] = reason
    what = _what(rec)
    ref_line = f"Booking ref: {ref}\n" if ref else ""
    if status == "paid":
        ctx.email(key, f"🟢 Paid: {what}",
                  f"Booked and paid from the Wallet:\n\n  {what}\n{ref_line}\n  {BOOKINGS_URL}\n")
    elif status == "parked":
        topup = f"  Top up / pay: {BASE}{location}\n" if location.startswith("/wallet/topup/") else ""
        ctx.email(key, f"🟡 Parked — pay within 15 min: {what}",
                  f"Booked, awaiting payment — the Wallet didn't cover it. PAY WITHIN 15 MINUTES or the\n"
                  f"booking lapses:\n\n  {what}\n{ref_line}\n{topup}  Awaiting payment: {PENDING_URL}\n")
    elif status == "failed":
        ctx.email(key, f"🔴 Failed: {what}",
                  f"Auto-book didn't get this slot: {reason}.\nNothing is booked (checked My bookings).\n\n"
                  f"  {what}\n  {BOOKINGS_URL}\n")
    else:
        ctx.email(key, f"🟠 Unclear — check My bookings: {what}",
                  f"I sent the booking request but can't confirm the result: {reason}.\n"
                  "It will NOT be retried. Please check:\n\n"
                  f"  {what}\n{ref_line}  {PENDING_URL}\n  {BOOKINGS_URL}\n")
    ctx.try_save_state(ctx.state)
    return "stop"
