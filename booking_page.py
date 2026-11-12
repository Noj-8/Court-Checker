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
  * "Not open yet" needs BOTH no slot links AND an opens-at marker in the
    page text ("opens 21:00", "เปิดจอง 21:00 น."). Neither = an unexpected
    page, which callers must log and skip — not a closed date, not a dead
    session.

The opens-at marker is the one text-based rule (the site gives no class for
it): an English or Thai "open" word followed by an HH:MM within a few
characters.
"""

import re
from html.parser import HTMLParser

THAI_CHARS = re.compile(f"[{chr(0x0E00)}-{chr(0x0E7F)}]")  # the Thai Unicode block
FIRST_INT = re.compile(r"\d+")  # str pattern: also matches Thai digits, which int() accepts
TIME_RE = re.compile(r"(\d{1,2})[:.](\d{2})")
OPENS_AT_RE = re.compile(r"(?:\bopen(?:s|ing)?\b|เปิด)[^0-9\n]{0,24}(\d{1,2})[:.](\d{2})", re.IGNORECASE)


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
                self._slot = {"classes": classes, "time": "", "meta": ""}
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


def slot_time(text):
    """'20:00', '8:00', '20.00', '20:00 - 21:00' -> 'HH:MM' (the start); None if no time."""
    m = TIME_RE.search(text or "")
    return f"{int(m.group(1)):02d}:{int(m.group(2)):02d}" if m else None


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
        })
    thai = any(THAI_CHARS.search(s["meta"]) for s in slots) or bool(THAI_CHARS.search(p.title))
    opens = None if slots else OPENS_AT_RE.search(" ".join(" ".join(p.text).split()))
    opens_at = f"{int(opens.group(1)):02d}:{int(opens.group(2)):02d}" if opens else None
    return {"slots": slots, "lang": p.lang, "thai": thai, "title": " ".join(p.title.split()),
            "locked": p.locked,           # the court map's is-locked: diagnostics only
            "opens_at": opens_at,         # only looked for when there are no slots
            "not_open_yet": not slots and opens_at is not None}
