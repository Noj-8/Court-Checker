# Test fixtures (scrubbed)

Scrubbed copies of five pages saved from the live Crystal Sports site on
2026-11-13. The raw pages live in `fixtures_raw/`, which is gitignored and must
never be committed. Structure, class names, form fields and URLs are
unchanged. Only the values below were replaced with obvious fakes.

| File | Page |
|---|---|
| `1_booking_time.html` | `/booking?date=…&loc=LOC002&time=18&sel=`: a time chosen, slot grid and court tiles |
| `2_review.html` | `/booking/review?sel=…`: the confirm form (terms checkbox, optional coach) |
| `3_bookings_upcoming.html` | `/bookings?tab=upcoming`: empty |
| `4_topup.html` | `/wallet/topup/<payment ref>?then=<booking ref>`: where a parked booking lands |
| `5_bookings_awaiting.html` | `/bookings?tab=pending`: one booking awaiting payment |

## What was scrubbed

| Kind | Replaced with |
|---|---|
| Member's name (menu greeting "Hi …") | `Hi Test Member` |
| Anti-forgery tokens (`__RequestVerificationToken`, 5 distinct) | `FAKE-ANTIFORGERY-TOKEN-01` … `-05` |
| Booking ref (`SPORTS-…`) | `SPORTS-900000000001` (consistent across the top-up URL and the pending row) |
| Payment / top-up ref (18 digits, in URLs, `data-ref`, the QR download filename) | `100000000000000001` |
| Thai QR payment image (base64 SVG: payee, amount, payment ref) | a small placeholder SVG reading "FAKE QR (scrubbed)" |
| Coach names (real people) | `C.Alpha`, `C.Bravo` |
| Coach ids | `1001`, `1002` |

Checked and **not present** in any of the five pages, so nothing to scrub:
- your phone number
- your email address
- member and LINE ids

Each file starts with a `<!-- SCRUBBED copy … -->` line.
`test_autobook.TestFixturesAreScrubbed` fails if:
- a real-looking token, a long base64 blob, a non-fake booking ref, or the name
  reappears
- a file is missing that line

## What the tests make up

`test_autobook.py` builds these in code and marks them `SYNTHETIC` or
`DERIVED`. **None of the SYNTHETIC pages has been seen on the live site yet.**

| What | Marked | Notes |
|---|---|---|
| An Upcoming list with a booking | SYNTHETIC | Paid status, a played date and time |
| The review form re-rendered with an error | SYNTHETIC | A court taken between review and confirm |
| The POST responses | SYNTHETIC | Apart from the parked redirect, whose Location comes from the real top-up page |
| An Awaiting-payment list with its row removed | DERIVED | |
| A review page for the other free court | DERIVED | |
| Crystal Sports (LOC001) versions of the G pages | DERIVED | |
