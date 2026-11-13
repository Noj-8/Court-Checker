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
  2. Read /bookings?tab=pending and /bookings. Upcoming rows show the
     played day and time (the year is inferred); Awaiting-payment rows
     don't, so for each such booking (not the bot's own, recognised by its
     ref fingerprint) its own page /bookings/SPORTS-… is read. A booking at
     a time range overlapping this slot (same date), any location or court
     -> skip (one ℹ️ email). A booking at another date/time doesn't block. One whose date/time
     can't be read anywhere -> no booking this pass, one ℹ️ email per key,
     retried on a later pass.
  3. GET the slot's own link from the grid; check the page has that date,
     branch and time selected; take the first free court tile (site order).
  4. GET /booking/review?sel=..; verify branch, court, date, time, price, and
     that the form posts to /booking/confirm with exactly: sel, the
     coach_<sel> select on "No coach", a method radio offering WALLET, the
     terms checkbox (value "true"), __RequestVerificationToken, and the
     disabled type_<sel>. Anything else, anything required, pre-filled or
     pre-selected -> 🔴 "booking form changed / page mismatch", no POST.
  5. dry_run: 🧪 "Would have booked ..." once per key. No POST.
  6. live: write-ahead (status in_progress) saved to state.json on disk,
     then the ONE POST (bot_session.post_booking_confirm) with exactly the
     body a browser sent on 2026-11-13: sel, coach_<sel>=, method=WALLET,
     acceptTerms=true, __RequestVerificationToken. Redirect not followed;
     a Location with "topup" and SPORTS-<digits> is the parked hint.
  7. Verify via both lists: our ref under Awaiting payment -> 🟡 Parked
     (the email links the actual Location); under Upcoming -> 🟢 Paid;
     clearly refused (an error shown on the re-rendered form) AND not
     listed -> ONE other free court at the same time (back to 4), else
     🔴 Failed; anything else -> 🟠 Unclear, never Failed.
  8. A key left in_progress by a run that died is never retried: 🟠 once.

state.json and the Actions logs are public. state.json holds only our own
values and fixed reason codes (never site text, field names or values, refs
or personal data); refs appear only as a short keyed fingerprint, in state
and in the log. Emails carry the details.
"""

import os
import re
from datetime import date as _date, datetime
from urllib.parse import urljoin

import requests

import booking_page
import bot_session
from bot_session import BASE, BOOKINGS_PATH, BOOKING_PATH, REVIEW_PATH, TZ, ref_fp

MODES = ("off", "dry_run", "live")
LIVE_KEY, DRY_KEY, RETRY_KEY = "autobook", "autobook_dry_run", "autobook_retry"
PENDING_URL = f"{BASE}{BOOKINGS_PATH}?tab=pending"
BOOKINGS_URL = f"{BASE}{BOOKINGS_PATH}"
REF_RE = re.compile(r"SPORTS-\d{1,20}")
SEL_RE = re.compile(r"\d{8}-\d{1,6}")              # a court's sel, e.g. 20261115-270
PRICE_RE = re.compile(r"\d{1,6}(?:\.\d{1,2})?")
SLOT_MINUTES = 60  # every grid slot is one hour
MAX_DETAIL_READS = 10  # per pass: more unknown pending bookings than this -> fail closed
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
RESULT_EMOJI = {"paid": "🟢", "parked": "🟡", "failed": "🔴", "aborted_mismatch": "🔴", "not_recorded": "🔴",
                "unclear": "🟠", "interrupted": "🟠", "in_progress": "🟠", "skipped_existing": "ℹ️"}
TERMINAL = {"skipped_existing", "aborted_mismatch", "dry_run", "in_progress", "interrupted",
            "paid", "parked", "failed", "unclear", "not_recorded"}


class SessionStopped(Exception):
    """A login redirect: the session is gone; the pass must stop."""


class Deferred(Exception):
    """Nothing was booked and it's safe to try again on a later pass.
    `code` is a fixed reason code (safe for state.json); `text` is for the
    email and the log (never a ref, URL with a ref, or page text)."""

    def __init__(self, code, text):
        super().__init__(text)
        self.code, self.text = code, text


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


def safe_price(price):
    return price if price and PRICE_RE.fullmatch(price) else None


def is_parked_location(location):
    """The redirect after a booking the Wallet didn't cover. Seen live:
    /wallet/topup/<id>?then=SPORTS-<ref> (2026-11-11) and
    /payment/SPORTS-<ref>/topup-shortfall (2026-11-13). Any Location with
    "topup" and a booking ref counts. Only a hint: never followed."""
    return "topup" in (location or "").lower() and bool(REF_RE.search(location or ""))


def _minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def overlaps(a, b):
    """Whether two (date, 'HH:MM' start, 'HH:MM' end) ranges overlap. An end
    of 24:00 is midnight; an end at or before the start runs past midnight."""
    if a[0] != b[0]:
        return False
    a0, a1, b0, b1 = _minutes(a[1]), _minutes(a[2]), _minutes(b[1]), _minutes(b[2])
    a1 += 1440 if a1 <= a0 else 0
    b1 += 1440 if b1 <= b0 else 0
    return a0 < b1 and b0 < a1


def slot_range(date_iso, hhmm):
    end = _minutes(hhmm) + SLOT_MINUTES
    return (date_iso, hhmm, f"{end // 60:02d}:{end % 60:02d}")


def now_iso():
    return datetime.now(TZ).isoformat(timespec="seconds")


class Context:
    """What the flow needs from monitor.py (passed in to avoid a circular import)."""

    def __init__(self, jar, state, mode, send, save_state, try_save_state, log, locations):
        self.jar, self.state, self.mode = jar, state, mode
        self.send, self.save_state, self.try_save_state, self.log = send, save_state, try_save_state, log
        self.locations = locations
        # (key, location) -> the result emoji of an email actually SENT this
        # pass: only that location's opening is left out of the slot-open
        # email; the other location's line points at it.
        self.emailed_slots = {}
        self.current_loc = None  # the location being attempted
        self.detail_cache = {}  # ref -> [(date, start, end)] or None (unreadable), for this pass
        # key -> {"court", "price"} as the page showed them: for this pass's
        # emails only, never written to state.json (it's public).
        self.labels = {}

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
        """Send; True if it went out. Only a sent email counts as "this key
        had its result email at this location" (which drops that location's
        opening from the slot-open email)."""
        try:
            self.send(subject, body)
        except Exception as e:
            self.log(f"⚠ auto-book email failed for {key}: {type(e).__name__}")
            return False
        self.emailed_slots[(key, self.current_loc)] = subject.split(" ", 1)[0]
        self.log(f"✉ {subject.split(' ', 1)[0]} email sent for {key}")
        return True

    def defer(self, key, code, **extra):
        retry = self.retries().setdefault(key, {"since": now_iso()})
        retry["reason"] = code
        retry.update(extra)
        return retry


# --- requests (all through bot_session) -----------------------------------------

def _get(ctx, path, params, label=None):
    """GET an allowed page. Errors name `label` (or the path), never a URL
    with a ref in it, and never the exception's message."""
    label = label or path
    try:
        f = bot_session.fetch(ctx.jar, path, params)
    except requests.RequestException as e:
        raise Deferred("network_error", f"network error ({type(e).__name__}) on {label}") from None
    if f.login:
        raise SessionStopped()
    if f.resp.status_code != 200:
        raise Deferred(f"http_{int(f.resp.status_code)}", f"http {f.resp.status_code} on {label}")
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
            raise Deferred(f"bookings_page_unrecognised:{name}", f"the My bookings page ({name}) didn't look as expected")
        out[name] = page["rows"]
    return out


def slot_page(ctx, href):
    path, query = booking_page.query_of(href)
    if path != BOOKING_PATH:
        raise RuntimeError("the slot link isn't a /booking link")
    return booking_page.parse_slot_page(_get(ctx, BOOKING_PATH, query)), query


# --- existing bookings ------------------------------------------------------------

def booking_slots(ctx, ref):
    """The (date, start, end) of every court in booking `ref`, read from its
    own page (/bookings/SPORTS-…): the Awaiting-payment list doesn't show
    them. None if unreadable. Cached for the pass. Logged by fingerprint."""
    if ref in ctx.detail_cache:
        return ctx.detail_cache[ref]
    slots = None
    if REF_RE.fullmatch(ref or "") and len(ctx.detail_cache) < MAX_DETAIL_READS:
        label = f"booking ref:{ref_fp(ref)}"
        try:
            page = booking_page.parse_booking_detail(_get(ctx, f"{BOOKINGS_PATH}/{ref}", {}, label=label))
            ctx.log(f"    {label}: http 200")
        except Deferred as e:
            ctx.log(f"    {label}: {e.code}")
            page = None
        if page and page["ref"] == ref and page["lines"] and all(l["slot"] for l in page["lines"]):
            slots = [l["slot"] for l in page["lines"]]
    ctx.detail_cache[ref] = slots
    return slots


def existing_at(ctx, rows, cand_range):
    """(clash, unreadable): whether any of these bookings (not the bot's own)
    has a time range overlapping `cand_range` = (date, start, end) — any
    location or court — and how many have a date/time that couldn't be read
    from the list or the booking's own page."""
    unreadable = 0
    for r in rows:
        slots = [r["slot"]] if r["slot"] else booking_slots(ctx, r["ref"])
        if slots is None:
            unreadable += 1
        elif any(overlaps(s, cand_range) for s in slots):
            return True, 0
    return False, unreadable


# --- verification ---------------------------------------------------------------
# Each problem is (code, text): the fixed code goes to state.json; the text,
# which may quote the page (field names included), goes only in the email.

def check_slot_page(page, cand):
    problems = []
    want = date_label(cand["date"])
    if page["date_label"] != want:
        problems.append(("page_mismatch:date", f"selected date is {page['date_label']!r}, expected {want!r}"))
    if page["branch"] != cand["loc_name"]:
        problems.append(("page_mismatch:branch", f"selected branch is {page['branch']!r}, expected {cand['loc_name']!r}"))
    if page["time"] != cand["time"]:
        problems.append(("page_mismatch:time", f"selected time is {page['time']!r}, expected {cand['time']!r}"))
    return problems


def check_review(review, cand, court):
    """Every way the review page must look before we may POST. Returns
    (problems, post_fields, price)."""
    sel = court["sel"]
    problems = []
    if not SEL_RE.fullmatch(sel or ""):
        problems.append(("sel_format", f"the court's sel {sel!r} isn't in the expected form"))
    if review["form_count"] != 1:
        problems.append(("form_count", f"{review['form_count']} forms post to /booking/confirm (expected 1)"))
    if review["form_count"] and review["method"] != "post":
        problems.append(("form_method", f"the form method is {review['method']!r}, not post"))

    items = review["items"]
    want_title = f"{cand['loc_name']} · {court['name']}"
    want_when = f"{date_label(cand['date'])} · {cand['time']}"
    price = None
    if len(items) != 1:
        problems.append(("item_count", f"{len(items)} items in the booking (expected exactly 1)"))
    else:
        item = items[0]
        if item["title"] != want_title:
            problems.append(("page_mismatch:branch_court", f"booking item is {item['title']!r}, expected {want_title!r}"))
        if not item["when"].startswith(want_when):
            problems.append(("page_mismatch:time", f"booking time is {item['when']!r}, expected it to start {want_when!r}"))
        price = safe_price(item["price"])
        if not price:
            problems.append(("no_price", "no price shown for the court"))
        if review["total"] is not None and price and review["total"] != price:
            problems.append(("price_total_differs", f"total ฿{review['total']} differs from the court price ฿{price}"))
    if review["page_date"] and review["page_date"] != date_label(cand["date"]):
        problems.append(("page_mismatch:date", f"page date is {review['page_date']!r}, expected {date_label(cand['date'])!r}"))

    coach_name, type_name = f"coach_{sel}", f"type_{sel}"
    allowed = {"sel", "method", "acceptTerms", "__RequestVerificationToken", coach_name, type_name}
    fields = review["fields"]
    by_name = {}
    for f in fields:
        by_name.setdefault(f["name"], []).append(f)
        if f["name"] not in allowed:
            problems.append(("unexpected_field",
                             f"unexpected field {f['name']!r} ({f['type']}"
                             + (", required" if f["required"] else "")
                             + (f", pre-filled {f['value']!r}" if f["value"] else "") + ")"))
        elif f["required"] and f["name"] != "acceptTerms" and not f["disabled"]:
            problems.append(("field_required", f"field {f['name']!r} is now required"))

    sels = by_name.get("sel", [])
    if len(sels) != 1 or sels[0]["type"] != "hidden" or sels[0]["value"] != sel:
        problems.append(("sel_changed", f"sel field is {[(s['type'], s['value']) for s in sels]}, expected one hidden {sel!r}"))
    coaches = by_name.get(coach_name, [])
    if len(coaches) != 1 or coaches[0]["tag"] != "select" or coaches[0]["disabled"]:
        problems.append(("coach_missing", f"coach field {coach_name!r} missing or changed "
                                          f"({[(c['tag'], c['disabled']) for c in coaches]})"))
    elif coaches[0]["value"]:
        problems.append(("coach_preselected", f"{coach_name} is pre-selected ({coaches[0]['value']!r}), expected no coach"))
    for t in by_name.get(type_name, []):
        # A browser leaves the coach-type select out while it's disabled; if it
        # were enabled it would be sent too, and the body would differ.
        if not t["disabled"] or t["value"]:
            problems.append(("coach_type_enabled", f"{type_name} is enabled or pre-filled ({t['value']!r}) — a browser would send it"))
    methods = by_name.get("method", [])
    if not any(m["type"] == "radio" and m["value"] == "WALLET" for m in methods):
        problems.append(("no_wallet", "no WALLET payment-method radio"))
    if any(m["type"] != "radio" for m in methods):
        problems.append(("method_not_radio", "a non-radio 'method' field"))
    terms = by_name.get("acceptTerms", [])
    if len(terms) != 1 or terms[0]["type"] != "checkbox" or terms[0]["value"] != "true":
        problems.append(("terms_changed", f"terms checkbox missing or changed ({[(t['type'], t['value']) for t in terms]})"))
    tokens = by_name.get("__RequestVerificationToken", [])
    if len(tokens) != 1 or tokens[0]["type"] != "hidden" or not tokens[0]["value"]:
        problems.append(("token_missing", "anti-forgery token missing or changed"))

    if problems:
        return problems, None, price
    # Exactly the body a real browser sent on 2026-11-13 (DevTools payload),
    # in that order and nothing else: the court, "No coach", Wallet, the
    # terms box ticked, the anti-forgery token.
    post = [("sel", sel), (coach_name, ""), ("method", "WALLET"), ("acceptTerms", "true"),
            ("__RequestVerificationToken", tokens[0]["value"])]
    return [], post, price


# --- the flow -------------------------------------------------------------------

def notify_interrupted(ctx):
    """Step 8: a key a previous run left in_progress (it died after the
    write-ahead, maybe after the POST) is never retried: one 🟠 email. It
    stays in_progress (and is tried again next run) until that email is sent."""
    for key, rec in ctx.state.get(LIVE_KEY, {}).items():
        if rec.get("status") == "in_progress":
            ctx.current_loc = rec.get("loc")
            sent = ctx.email(key, f"🟠 Unclear — check My bookings: {_what(rec)}",
                             "A run stopped in the middle of auto-booking this slot (after recording the attempt,\n"
                             "possibly after the booking request). It will NOT be retried.\n\n"
                             f"  {_what(rec)}\n\nCheck whether it's booked:\n  {PENDING_URL}\n  {BOOKINGS_URL}\n")
            if sent:
                rec["status"] = "interrupted"
                rec["result_at"] = now_iso()
                ctx.try_save_state(ctx.state)


def _what(rec, label=None):
    label = label or {}
    court = f" {label['court']}" if label.get("court") else ""
    price = f" ฿{label['price']}" if label.get("price") else ""
    return f"{rec.get('loc_name', rec.get('loc', ''))}{court} {rec.get('date', '')} {rec.get('time', '')}{price}".strip()


def _eligible(ctx, key, cand):
    return key not in ctx.records() and (cand["newly_open"] or key in ctx.retries())


def process(ctx, candidates):
    """Run the flow for each candidate {date, time, loc, loc_name, href,
    newly_open}. Raises SessionStopped on a login redirect and JarSaveError
    if the jar couldn't be saved — after putting every candidate it didn't
    get to into the retry list. Never raises for anything else: every
    failure ends in a recorded outcome or a deferral."""
    if ctx.mode not in ("dry_run", "live"):
        return
    attempted = set()  # keys tried this pass (one per date+time, across both locations)
    for i, cand in enumerate(candidates):
        key = key_for(cand["date"], cand["time"])
        if key in attempted or not _eligible(ctx, key, cand):
            continue  # a skipped candidate never blocks the same time at the other location
        attempted.add(key)
        try:
            _process_one(ctx, key, cand)
        except (SessionStopped, bot_session.JarSaveError) as e:
            code = "session_stopped" if isinstance(e, SessionStopped) else "jar_save_error"
            for rest in candidates[i + 1:]:
                k = key_for(rest["date"], rest["time"])
                if k not in attempted and _eligible(ctx, k, rest):
                    ctx.defer(k, code)
            ctx.try_save_state(ctx.state)
            raise


def _process_one(ctx, key, cand):
    records = ctx.records()
    rec = records[key] = {"status": "started", "mode": ctx.mode, "loc": cand["loc"], "loc_name": cand["loc_name"],
                          "date": cand["date"], "time": cand["time"], "started_at": now_iso(), "attempts": []}
    ctx.current_loc = cand["loc"]
    ctx.log(f"  ★ auto-book ({ctx.mode}) {key} at {cand['loc_name']}")
    try:
        _flow(ctx, key, cand, rec)
    except SessionStopped:
        _after_error(ctx, key, rec, "session_stopped", "the session ended", retry=True)
        raise
    except Deferred as e:
        _after_error(ctx, key, rec, e.code, f"couldn't continue: {e.text}", retry=True)
    except bot_session.JarSaveError:
        # Before any POST: nothing was submitted, so it's retried next run.
        _after_error(ctx, key, rec, "jar_save_error", "the session jar couldn't be saved", retry=True)
        raise
    except Exception as e:  # a bug: never make a POSTed key retryable, never book twice
        _after_error(ctx, key, rec, "internal_error", f"internal error ({type(e).__name__})", retry=True)
    if rec.get("status") in TERMINAL:
        ctx.retries().pop(key, None)


def _after_error(ctx, key, rec, code, text, retry):
    """An exception interrupted the flow. Only a key that never had a POST
    sent may be retried; anything else gets a final outcome now."""
    if rec["status"] == "in_progress":
        _finish(ctx, key, rec, "unclear", f"after_post:{code}", f"after the booking request, {text}")
    elif rec["attempts"]:
        _finish(ctx, key, rec, "failed", f"after_refusal:{code}", f"the first court was refused and then {text}")
    elif rec["status"] == "started":
        del ctx.records()[key]
        if retry:
            ctx.defer(key, code)
        ctx.log(f"    deferred ({code}) — nothing was submitted; will retry")


def _flow(ctx, key, cand, rec):
    # 2. existing bookings
    lists = read_lists(ctx)
    own = ctx.own_ref_fps()
    others = [r for r in lists["pending"] + lists["upcoming"] if ref_fp(r["ref"]) not in own]
    clash, unreadable = existing_at(ctx, others, slot_range(cand["date"], cand["time"]))
    if clash:
        rec["status"] = "skipped_existing"
        rec["result_at"] = now_iso()
        ctx.email(key, f"ℹ️ Already booked: {cand['date']} {cand['time']} — not auto-booking",
                  f"A starred time opened ({cand['loc_name']} {cand['date']} {cand['time']}), but My bookings already\n"
                  f"shows a booking overlapping that date and time, so I didn't book another.\n\n  {BOOKINGS_URL}\n")
        return
    if unreadable:
        retry = ctx.defer(key, "existing_booking_unreadable", unreadable=unreadable)
        if not retry.get("notified"):
            if ctx.email(key, f"ℹ️ Not auto-booking {cand['date']} {cand['time']} — can't check your bookings",
                         f"A starred time opened ({cand['loc_name']} {cand['date']} {cand['time']}), but My bookings lists\n"
                         f"{unreadable} booking(s) whose court date/time I couldn't read (neither on the list nor on\n"
                         "the booking's own page), so I can't rule out that you already have this slot.\n"
                         "Not booking — book manually if you want it:\n\n"
                         f"  {BASE}{cand['href']}\n  {BOOKINGS_URL}\n\nI'll try again while it stays open; no more emails about it.\n"):
                retry["notified"] = now_iso()
        del ctx.records()[key]
        ctx.log(f"    not booking: {unreadable} existing booking(s) without a readable date/time")
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
                raise Deferred("no_free_court", "no free court left on the slot page")
            return _finish(ctx, key, rec, "failed", "refused_no_other_court",
                           "the court was refused and no other court was free")
        court = dict(free[0], index=page["courts"].index(free[0]) + 1)  # 1-based, in site order
        tried.add(court["sel"])
        result = _book_court(ctx, key, cand, rec, court, lists)
        if result != "refused":
            return
        ctx.log("    refused and not listed — trying one other court")
    _finish(ctx, key, rec, "failed", "two_courts_refused", "two courts were refused")


def _book_court(ctx, key, cand, rec, court, lists_before):
    # 4. review + verify
    review_html = _get(ctx, REVIEW_PATH, {"sel": court["sel"]})
    review = booking_page.parse_review(review_html)
    problems, post, price = check_review(review, cand, court)
    ctx.labels[key] = {"court": court["name"], "price": price}
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
    # Only our own values: the attempt number and the court's position in
    # site order — never the page's court name, its sel or the price.
    attempt = {"n": len(rec["attempts"]) + 1, "court_index": court["index"], "at": now_iso()}
    rec["attempts"].append(attempt)
    rec["status"] = "in_progress"
    try:
        ctx.save_state(ctx.state)
    except Exception as e:
        rec["status"] = "not_recorded"
        rec["reason"] = "write_ahead_failed"
        rec["result_at"] = now_iso()
        ctx.email(key, f"🔴 Failed: {_what(rec, ctx.labels.get(key))}",
                  f"I couldn't record the attempt in state.json ({type(e).__name__}), so I did NOT send the\n"
                  f"booking request. Book manually if you want it:\n  {BASE}{cand['href']}\n")
        return "stop"
    review_url = f"{BASE}{REVIEW_PATH}?sel={court['sel']}"
    hint, location = "none", ""
    try:
        f = bot_session.post_booking_confirm(ctx.jar, post, ctx.mode, referer=review_url)
        attempt["http"] = int(f.resp.status_code)
        location = f.resp.headers.get("Location", "")
        if f.login:
            hint = "login"
        elif 300 <= f.resp.status_code < 400 and is_parked_location(location):
            hint = "parked"
        elif f.resp.status_code == 200:
            hint = "refused" if booking_page.page_error_messages(f.resp.text or "") else "re-rendered"
        else:
            hint = "other"
    except requests.RequestException:
        hint = "network_error"
    attempt["hint"] = hint
    ref_match = REF_RE.search(location)  # a hint only: the lists decide the outcome
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
    new = sorted({r for r in list(pending) + list(upcoming) if r not in before and ref_fp(r) not in ctx.own_ref_fps()})
    if hint == "refused" and not ref:
        # The site showed an error. Only if NO booking at all appeared is it
        # safe to try another court; any new booking makes it unclear.
        if not new:
            rec["status"] = "refused"  # nothing booked; the caller may try one other court
            attempt["refused"] = True
            return "refused"
        return _finish(ctx, key, rec, "unclear", "refused_but_new_booking_listed",
                       f"the site showed an error, but {len(new)} new booking(s) appeared in My bookings",
                       ref=new[0] if len(new) == 1 else None, location=location)
    if not ref and len(new) == 1:
        ref = new[0]  # the one new booking (still checked against this slot below)
        attempt["ref_fp"] = ref_fp(ref)
    row = pending.get(ref) or upcoming.get(ref) if ref else None
    if ref and ref in pending and row["slot"] is None:
        # Awaiting-payment rows don't show the court time: read the new
        # booking's own page before calling it ours (🟡).
        slots = booking_slots(ctx, ref)
        if slots is None:
            return _finish(ctx, key, rec, "unclear", "new_booking_unreadable",
                           "a new booking is awaiting payment, but its own page couldn't be read to confirm "
                           "its date and time", ref=ref, location=location)
        if any(s[:2] != (cand["date"], cand["time"]) for s in slots):
            other = ", ".join(f"{s[0]} {s[1]}" for s in slots)
            return _finish(ctx, key, rec, "unclear", "listed_other_slot",
                           f"the new booking's own page shows {other}, not {cand['date']} {cand['time']}",
                           ref=ref, location=location)
    if row is not None and row["slot"] is not None and row["slot"][:2] != (cand["date"], cand["time"]):
        return _finish(ctx, key, rec, "unclear", "listed_other_slot",
                       f"the new booking is listed for {row['slot'][0]} {row['slot'][1]}, not {cand['date']} {cand['time']}",
                       ref=ref, location=location)
    if ref and ref in pending:
        return _finish(ctx, key, rec, "parked", None, None, ref=ref, location=location)
    if ref and ref in upcoming:
        return _finish(ctx, key, rec, "paid", None, None, ref=ref, location=location)
    return _finish(ctx, key, rec, "unclear", f"not_listed_after:{hint}",
                   f"after the booking request the site answered {attempt.get('http', 'nothing')} ({hint}) and the "
                   "booking isn't clearly listed", ref=ref, location=location)


def _mismatch(ctx, key, rec, cand, problems):
    rec["status"] = "aborted_mismatch"
    rec["result_at"] = now_iso()
    rec["problems"] = [code for code, _ in problems][:10]
    ctx.email(key, f"🔴 Failed: booking form changed / page mismatch — {cand['date']} {cand['time']}",
              f"I stopped before booking {cand['loc_name']} {cand['date']} {cand['time']} because the pages didn't\n"
              "look exactly as expected. Nothing was submitted. What differed:\n\n"
              + "\n".join(f"  • {text}" for _, text in problems)
              + f"\n\nBook manually if you want it:\n  {BASE}{cand['href']}\n")


def _finish(ctx, key, rec, status, reason_code, reason_text, ref=None, location=""):
    rec["status"] = status
    rec["result_at"] = now_iso()
    if reason_code:
        rec["reason"] = reason_code
    what = _what(rec, ctx.labels.get(key))
    ref_line = f"Booking ref: {ref}\n" if ref else ""
    if status == "paid":
        ctx.email(key, f"🟢 Paid: {what}",
                  f"Booked and paid from the Wallet:\n\n  {what}\n{ref_line}\n  {BOOKINGS_URL}\n")
    elif status == "parked":
        topup = f"  Top up / pay: {urljoin(BASE + '/', location)}\n" if is_parked_location(location) else ""
        ctx.email(key, f"🟡 Parked — pay within 15 min: {what}",
                  f"Booked, awaiting payment — the Wallet didn't cover it. PAY WITHIN 15 MINUTES or the\n"
                  f"booking lapses:\n\n  {what}\n{ref_line}\n{topup}  Awaiting payment: {PENDING_URL}\n")
    elif status == "failed":
        ctx.email(key, f"🔴 Failed: {what}",
                  f"Auto-book didn't get this slot: {reason_text}.\nNothing is booked (checked My bookings).\n\n"
                  f"  {what}\n  {BOOKINGS_URL}\n")
    else:
        ctx.email(key, f"🟠 Unclear — check My bookings: {what}",
                  f"I sent the booking request but can't confirm the result: {reason_text}.\n"
                  "It will NOT be retried. Please check:\n\n"
                  f"  {what}\n{ref_line}  {PENDING_URL}\n  {BOOKINGS_URL}\n")
    ctx.try_save_state(ctx.state)
    return "stop"
