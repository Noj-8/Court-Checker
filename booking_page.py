"""
Parsing of the new Crystal Sports /booking page (stdlib only), shared by
monitor.py and probe_new_site.py.

Language-independent by design — nothing depends on English or Thai text:
  * a slot is <a class="slot ...">; it is available iff its class list has
    neither "is-disabled" nor "is-locked";
  * its free count is the first integer in its span.slot-meta, so "2 free",
    "ว่าง 2" and "ว่าง ๒" all give 2;
  * GRID FIRST: if slot links are parsed, they are the answer. The court
    map is "court-map is-slate is-locked" on every page fetched with time=0
    (no time selected), including normal open dates, so "is-locked" outside
    a slot never decides anything — it is reported as `locked` for
    diagnostics only.
  * "Not open yet" needs BOTH no slot links AND a booking-specific
    opens-at phrase in the page text ("Booking opens 21:00", "Opens for
    booking at 21:00", "เปิดจอง 21:00 น."). Generic text such as "Opening
    hours 06:00-22:00" is not one. Neither = an unexpected page, which
    callers must log and skip — not a closed date, not a dead session.
  * is_login_page() is the one login-page detector, shared by monitor.py
    and probe_new_site.py.

The opens-at phrase and the login text are the only text-based rules (the
site gives no class for either).
"""

import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

THAI_CHARS = re.compile(f"[{chr(0x0E00)}-{chr(0x0E7F)}]")  # the Thai Unicode block
FIRST_INT = re.compile(r"\d+")  # str pattern: also matches Thai digits, which int() accepts
TIME_RE = re.compile(r"(\d{1,2})[:.](\d{2})")
# A clock time: H:MM / H.MM with an optional AM/PM, or a bare hour WITH AM/PM.
# The trailing (?!\w) stops "8 pmx" or "8:00 amount" being read as a marker.
CLOCK_RE = re.compile(
    r"(?<![\d:.])(\d{1,2})(?:[:.](\d{2}))?\s*([ap])\.?\s?m\.?(?!\w)"
    r"|(?<![\d:.])(\d{1,2})[:.](\d{2})(?![\d:.])",
    re.IGNORECASE)
AMPM_RE = re.compile(r"(?<![a-z])[ap]\.?\s?m\.?(?![a-z])", re.IGNORECASE)
OPENS_AT_RE = re.compile(
    r"(?:\bbookings?\s+(?:opens?|will\s+open)\b"        # "Booking opens", "Bookings will open"
    r"|\bopens?\s+for\s+bookings?\b"                    # "Opens for booking"
    r"|เปิด(?:ให้)?(?:การ)?จอง)"                           # "เปิดจอง", "เปิดให้จอง", "เปิดการจอง"
    r"[^0-9\n]{0,24}(\d{1,2})[:.](\d{2})",
    re.IGNORECASE)

# Login-page detection. Strong markers alone are enough; login TEXT only
# counts on a page that also has a <form>, so an ordinary page with a
# "Log in" link is never mistaken for one.
LOGIN_URL_PATH = "/account/login"
LOGIN_STRONG_MARKERS = {
    "password input": re.compile(r"<input[^>]*\btype\s*=\s*[\"']?password\b", re.IGNORECASE),
    "OTP input": re.compile(r"<input[^>]*(?:autocomplete\s*=\s*[\"']?one-time-code"
                            r"|\bname\s*=\s*[\"']?[\w.-]*otp)", re.IGNORECASE),
    "login form": re.compile(r"<form[^>]*\baction\s*=\s*[\"']?[^\"'>]*/account/login", re.IGNORECASE),
}
LOGIN_TEXT = re.compile(r"เข้าสู่ระบบ|\bsign[\s-]?in\b|\blog[\s-]?in\b", re.IGNORECASE)


class PageParser(HTMLParser):
    """Collects <html lang>, <title>, every <a class="slot ..."> with the
    text of its span.slot-time / span.slot-meta children, the page's
    visible text (for the opens-at marker), and whether an "is-locked"
    element appears outside the slots (diagnostics only)."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lang = ""
        self.title = ""
        self.slots = []
        self.locked = False
        self.text = []
        self._in_script = False
        self._in_title = False
        self._slot = None
        self._slot_depth = 0
        self._field = None
        self._field_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "html":
            self.lang = attrs.get("lang") or ""
        if tag == "title":
            self._in_title = True
        if tag in ("script", "style"):
            self._in_script = True
        classes = (attrs.get("class") or "").split()
        is_slot = tag == "a" and "slot" in classes
        if "is-locked" in classes and not is_slot and self._slot is None:
            self.locked = True
        if tag in self.VOID:
            return
        if self._slot is None:
            if is_slot:
                self._slot = {"classes": classes, "time": "", "meta": "", "href": attrs.get("href") or ""}
                self._slot_depth = 1
            return
        self._slot_depth += 1
        if self._field is None and tag == "span":
            if "slot-time" in classes:
                self._field, self._field_depth = "time", self._slot_depth
            elif "slot-meta" in classes:
                self._field, self._field_depth = "meta", self._slot_depth

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in ("script", "style"):
            self._in_script = False
        if tag in self.VOID or self._slot is None:
            return
        if self._field is not None and self._slot_depth == self._field_depth:
            self._field = None
        self._slot_depth -= 1
        if self._slot_depth == 0:
            self.slots.append(self._slot)
            self._slot = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._slot is not None and self._field:
            self._slot[self._field] += data
        if not self._in_script:
            self.text.append(data)


def login_page_markers(html, url=""):
    """Names of the login-page markers present (empty = not a login page)."""
    hits = []
    if LOGIN_URL_PATH in urlsplit(url or "").path.lower():
        hits.append("login URL")
    html = html or ""
    hits += [name for name, rx in LOGIN_STRONG_MARKERS.items() if rx.search(html)]
    if not hits and LOGIN_TEXT.search(html) and re.search(r"<form\b", html, re.IGNORECASE):
        hits.append("login text + form")
    return hits


def is_login_page(html, url=""):
    return bool(login_page_markers(html, url))


def to_24h(hour, minute, ampm=None):
    """'HH:MM', or None if out of range. ampm is 'a', 'p' or None (24-hour)."""
    hour, minute = int(hour), int(minute or 0)
    if not 0 <= minute <= 59:
        return None
    if ampm:
        if not 1 <= hour <= 12:
            return None  # "13:00 PM", "0:30 AM": no unambiguous reading
        hour = hour % 12 + (12 if ampm.lower() == "p" else 0)
    elif not 0 <= hour <= 23:
        return None
    return f"{hour:02d}:{minute:02d}"


def slot_time(text):
    """The slot's START time as 24-hour 'HH:MM', or None when it can't be
    read unambiguously — callers must treat None as an unexpected page,
    never guess.
      '20:00', '8:00', '20.00', '๒๐:๐๐', '20:00 - 21:00'  -> 24-hour as written
      '8:00 PM', '8 pm', '8:00 p.m.', '12:00 AM'          -> converted (20:00, 00:00)
      '8:00 - 9:00 PM'  -> None: the AM/PM isn't on the start time, so its
                           half of the day is a guess
      '25:00', '13:00 PM', 'Full', ''                     -> None"""
    text = text or ""
    m = CLOCK_RE.search(text)
    if not m:
        return None
    if m.group(3):  # the start time carries its own AM/PM
        return to_24h(m.group(1), m.group(2), m.group(3))
    if AMPM_RE.search(text):
        return None  # an AM/PM elsewhere in the text: ambiguous
    return to_24h(m.group(4), m.group(5))


def parse_page(html):
    p = PageParser()
    p.feed(html)
    slots = []
    for s in p.slots:
        meta = " ".join(s["meta"].split())
        m = FIRST_INT.search(meta)
        slots.append({
            "time": " ".join(s["time"].split()) or "?",
            "available": "is-disabled" not in s["classes"] and "is-locked" not in s["classes"],
            "free": int(m.group()) if m else None,
            "meta": meta,
            "href": s["href"],            # the slot's own link (only on available slots)
        })
    thai = any(THAI_CHARS.search(s["meta"]) for s in slots) or bool(THAI_CHARS.search(p.title))
    opens = None if slots else OPENS_AT_RE.search(" ".join(" ".join(p.text).split()))
    opens_at = to_24h(opens.group(1), opens.group(2)) if opens else None
    return {"slots": slots, "lang": p.lang, "thai": thai, "title": " ".join(p.title.split()),
            "locked": p.locked,           # the court map's is-locked: diagnostics only
            "opens_at": opens_at,         # only looked for when there are no slots
            "not_open_yet": not slots and opens_at is not None}


# ---------------------------------------------------------------------------
# Auto-book pages: a minimal DOM, the slot page (with a time selected), the
# review/confirm form, and the My bookings lists. Built from pages saved from
# the live site on 2026-11-13 (scrubbed copies in tests/fixtures/).
# ---------------------------------------------------------------------------

class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.children, self.parent = tag, attrs, [], parent

    @property
    def classes(self):
        return (self.attrs.get("class") or "").split()

    def text(self):
        out = []
        for c in self.children:
            out.append(c if isinstance(c, str) else ("" if c.tag in ("script", "style") else c.text()))
        return " ".join(" ".join(out).split())

    def iter(self):
        for c in self.children:
            if isinstance(c, Node):
                yield c
                yield from c.iter()

    def find_all(self, tag=None, cls=None, **attrs):
        return [n for n in self.iter()
                if (tag is None or n.tag == tag) and (cls is None or cls in n.classes)
                and all(n.attrs.get(k) == v for k, v in attrs.items())]

    def find(self, tag=None, cls=None, **attrs):
        found = self.find_all(tag, cls, **attrs)
        return found[0] if found else None


class _TreeBuilder(HTMLParser):
    VOID = PageParser.VOID

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, None)
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in self.VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not self.root and n.tag != tag:  # tolerate unclosed tags
            n = n.parent
        if n is not self.root:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def dom(html):
    b = _TreeBuilder()
    b.feed(html or "")
    b.close()
    return b.root


def query_of(href):
    """The query of a relative link as a dict (blank values kept), plus its path."""
    from urllib.parse import parse_qsl
    parts = urlsplit(href or "")
    return parts.path, dict(parse_qsl(parts.query, keep_blank_values=True))


def parse_slot_page(html):
    """/booking?date=..&loc=..&time=N&sel= — the page after a time is chosen.
    Returns what is selected (date label, branch, time) and the court tiles,
    in site order: [{name, sel, available}]."""
    root = dom(html)
    date_chip = next((n for n in root.find_all("a", "date-chip") if "is-selected" in n.classes), None)
    branch = next((n for n in root.find_all("a", "choice-card") if "is-selected" in n.classes), None)
    slot = next((n for n in root.find_all("a", "slot") if "is-selected" in n.classes), None)
    courts = []
    for tile in root.find_all("a", "court-tile"):
        name_el = tile.find("span", "court-name")
        _, q = query_of(tile.attrs.get("href", ""))
        available = "is-disabled" not in tile.classes and tile.attrs.get("aria-disabled") != "true" and bool(q.get("sel"))
        courts.append({"name": name_el.text() if name_el else "", "sel": q.get("sel", ""), "available": available})
    return {
        "date_label": date_chip.attrs.get("aria-label", "") if date_chip else "",
        "branch": (branch.find("span", "choice-title").text() if branch and branch.find("span", "choice-title") else ""),
        "time": slot_time(slot.find("span", "slot-time").text()) if slot and slot.find("span", "slot-time") else None,
        "courts": courts,
    }


PRICE_RE = re.compile(r"฿\s*([\d,]+(?:\.\d+)?)")


def _field_info(n):
    a = n.attrs
    info = {"tag": n.tag, "name": a.get("name", ""), "type": (a.get("type") or ("text" if n.tag == "input" else n.tag)).lower(),
            "value": a.get("value", ""), "required": "required" in a, "disabled": "disabled" in a,
            "checked": "checked" in a}
    if n.tag == "select":
        opts = n.find_all("option")
        chosen = next((o for o in opts if "selected" in o.attrs), opts[0] if opts else None)
        info["value"] = chosen.attrs.get("value", "") if chosen else ""
    if n.tag == "textarea":
        info["value"] = n.text()
    return info


def parse_review(html):
    """/booking/review?sel=.. — the confirm form. Returns the booking forms
    (action /booking/confirm), and for the first one: method, every named
    field (inputs, selects, textareas, named buttons — including any outside
    the form that point at it with form="id"), and the booked item cards
    (title "Branch · Court", date/time line, price)."""
    root = dom(html)
    forms = [f for f in root.find_all("form") if urlsplit(f.attrs.get("action", "")).path == "/booking/confirm"]
    out = {"form_count": len(forms), "method": "", "fields": [], "items": [], "total": None, "page_date": ""}
    h1 = root.find("h1", "page-title")
    if h1 is not None:
        sib = h1.parent.find("p", "text-muted") if h1.parent else None
        out["page_date"] = sib.text() if sib else ""
    if not forms:
        return out
    form = forms[0]
    out["method"] = (form.attrs.get("method") or "get").lower()
    fid = form.attrs.get("id")
    members = [n for n in form.iter() if n.tag in ("input", "select", "textarea", "button")]
    if fid:
        members += [n for n in root.iter() if n.tag in ("input", "select", "textarea", "button")
                    and n.attrs.get("form") == fid and n not in members]
    out["fields"] = [_field_info(n) for n in members if n.attrs.get("name")]
    for card in form.find_all("div", "card"):
        title = card.find("b")
        sub = card.find("div", "text-muted")
        bolds = card.find_all("b")
        price = next((PRICE_RE.search(b.text()) for b in bolds if PRICE_RE.search(b.text())), None)
        if title is not None and sub is not None and " · " in title.text():
            out["items"].append({"title": title.text(), "when": sub.text(),
                                 "price": price.group(1).replace(",", "") if price else None})
    total = root.find("b", **{"data-rv-total": ""})
    m = PRICE_RE.search(total.text()) if total is not None else None
    out["total"] = m.group(1).replace(",", "") if m else None
    return out


ERROR_CLASSES = ("alert-danger", "alert-error", "validation-summary-errors", "field-validation-error",
                 "toast-error", "is-error")


def page_error_messages(html):
    """Text of explicit error elements on a page (a re-rendered form that
    refused the booking). Empty list = no explicit error shown."""
    root = dom(html)
    return [n.text() for n in root.iter() if any(c in n.classes for c in ERROR_CLASSES) and n.text()]


ROW_RANGE_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[–—-]\s*(\d{1,2}):(\d{2})")
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
DATE_RES = (
    (re.compile(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?\s+(\d{4})"), lambda m: (int(m[3]), MONTHS.get(m[2].lower()), int(m[1]))),
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})"), lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})"), lambda m: (int(m[3]), int(m[2]), int(m[1]))),
)


def _range_of(text):
    """('HH:MM', 'HH:MM') start and end of the first time range in the text."""
    r = ROW_RANGE_RE.search(text or "")
    return (f"{int(r[1]):02d}:{r[2]}", f"{int(r[3]):02d}:{r[4]}") if r else None


def _play_slot(text):
    """('YYYY-MM-DD', 'HH:MM' start, 'HH:MM' end) if the text shows a played
    date AND a time RANGE ("Sun 15 Nov 2026 · 23:00 – 24:00"), else None. A
    bare time such as the "Booked on 13 Nov 2026 11:23" cell is not a
    played slot."""
    rng = _range_of(text)
    if not rng:
        return None
    for rx, conv in DATE_RES:
        for m in rx.finditer(text):
            y, mo, d = conv(m)
            if mo:
                return (f"{y:04d}-{mo:02d}-{d:02d}",) + rng
    return None


BOOKING_HREF_RE = re.compile(r"/bookings/(SPORTS-\d{1,20})")


def bangkok_today():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Bangkok")).date()


def infer_play_date(day, month, today):
    """A list row shows "15 · Nov" with no year: the next date with that day
    and month on or after `today` (Asia/Bangkok), so a "2 Jan" row seen in
    December is next year. None if no such date (e.g. 31 Feb)."""
    from datetime import date
    for year in range(today.year, today.year + 5):  # 29 Feb may be up to 4 years out
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        if d >= today:
            return d
    return None


def parse_bookings_list(html, today=None):
    """/bookings (Upcoming) or /bookings?tab=pending (Awaiting payment).
    Returns {"tab", "rows": [{ref, status, text, slot, loc_name, court}],
    "empty", "recognised"}. slot is ('YYYY-MM-DD', 'HH:MM' start, 'HH:MM'
    end) when the row shows the played day and time range:
      - Upcoming rows (seen live 2026-11-13): a.booking-item with
        <div class="booking-date"><b>15</b><span>Nov</span></div> (no year:
        see infer_play_date), "23:00–24:00 · G South-1" and
        "Crystal Sports G · SPORTS-…".
      - Awaiting-payment rows (a.bk-row) show only ref, booked-on time and
        status: slot is None, and the caller reads the booking's own page.
    A row is any link in #bookingList to /bookings/SPORTS-<digits>."""
    today = today or bangkok_today()
    root = dom(html)
    active = next((n for n in root.find_all("a", "tab") if "active" in n.classes), None)
    tab = query_of(active.attrs.get("href", ""))[1].get("tab", "") if active is not None else ""
    lst = root.find(id="bookingList")
    rows = []
    for row in (lst.iter() if lst is not None else ()):
        if row.tag != "a":
            continue
        m = BOOKING_HREF_RE.fullmatch(urlsplit(row.attrs.get("href", "")).path)
        ref = row.attrs.get("data-ref") or (m.group(1) if m else "")
        if not ref:
            continue
        badge = row.find("span", "badge")
        text = row.text()
        slot, loc_name, court = None, "", ""
        date_box = row.find("div", "booking-date")
        if date_box is not None:
            day_el, mon_el = date_box.find("b"), date_box.find("span")
            day = FIRST_INT.search(day_el.text()) if day_el is not None else None
            month = MONTHS.get((mon_el.text() if mon_el is not None else "")[:3].lower())
            head = row.find("div", "fw-700")
            rng = _range_of(head.text() if head is not None else "")
            played = infer_play_date(int(day.group(0)), month, today) if day and month else None
            if played and rng:
                slot = (played.isoformat(),) + rng
            if head is not None and " · " in head.text():
                court = head.text().split(" · ", 1)[1].strip()
            sub = row.find("div", "text-muted")
            if sub is not None and " · " in sub.text():
                loc_name = sub.text().rsplit(" · ", 1)[0].strip()
        else:
            slot = _play_slot(text)
        rows.append({"ref": ref, "status": badge.text() if badge is not None else "", "text": text,
                     "slot": slot, "loc_name": loc_name, "court": court})
    empty = root.find("div", "empty-state") is not None
    return {"tab": tab, "rows": rows, "empty": empty and not rows, "recognised": lst is not None}


def parse_booking_detail(html):
    """/bookings/SPORTS-<digits> — one booking's own page (seen live
    2026-11-13). Returns {"ref", "status", "lines": [{loc_name, court,
    slot}]} where each card.bk-group is one court booked: its title
    "Crystal Sports G · G South-1" and "Sun 15 Nov 2026 · 23:00–24:00"
    (slot = ('2026-11-15', '23:00', '24:00'), None if unreadable). The page also
    has a cancel form; nothing here ever submits anything."""
    root = dom(html)
    h1 = root.find("h1", "section-title")
    ref = h1.text() if h1 is not None else ""
    badge = h1.parent.find("span", "badge") if h1 is not None and h1.parent is not None else None
    lines = []
    for card in root.find_all("div", "bk-group"):
        title = card.find("b")
        when = card.find("div", "text-muted")
        loc_name, court = "", ""
        if title is not None and " · " in title.text():
            loc_name, court = (x.strip() for x in title.text().split(" · ", 1))
        lines.append({"loc_name": loc_name, "court": court,
                      "slot": _play_slot(when.text()) if when is not None else None})
    return {"ref": ref, "status": badge.text() if badge is not None else "", "lines": lines}
