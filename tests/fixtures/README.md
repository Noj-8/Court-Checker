# Test fixtures (scrubbed)

Scrubbed copies of eight pages saved from the live Crystal Sports site
(1–7 on 2026-11-13, 8 on 2026-11-15). The raw pages live in `fixtures_raw/`, which is gitignored and must
never be committed. Structure, class names, form fields and URLs are
unchanged. Only the values below were replaced with obvious fakes.

| File | Page |
|---|---|
| `1_booking_time.html` | `/booking?date=…&loc=LOC002&time=18&sel=`: a time chosen, slot grid and court tiles |
| `2_review.html` | `/booking/review?sel=…`: the confirm form (terms checkbox, optional coach) |
| `3_bookings_upcoming.html` | `/bookings?tab=upcoming`: empty |
| `4_topup.html` | `/wallet/topup/<payment ref>?then=<booking ref>`: where a parked booking lands |
| `5_bookings_awaiting.html` | `/bookings?tab=pending`: one booking awaiting payment |
| `6_bookings_upcoming_filled.html` | `/bookings?tab=upcoming`: one Paid booking (15 Nov, 23:00–24:00, G South-1, Crystal Sports G) |
| `7_booking_detail.html` | `/bookings/SPORTS-…`: that booking's own page (date, time, court, and a Cancel form the bot never uses) |
| `8_login.html` | `/account/login`: the login page, logged out (one `<form action="/account/login">` with a `mobile` field). The only page the login detector may match |

## What was scrubbed

| Kind | Replaced with |
|---|---|
| Member's name (menu greeting "Hi …") | `Hi Test Member` |
| Anti-forgery tokens (`__RequestVerificationToken`, 7 distinct) | `FAKE-ANTIFORGERY-TOKEN-01` … `-07` |
| Booking refs (`SPORTS-…`) | `SPORTS-900000000001` (consistent across the top-up URL and the pending row), `SPORTS-900000000002` (consistent across the Upcoming row and the booking page) |
| Payment / top-up ref (18 digits, in URLs, `data-ref`, the QR download filename) | `100000000000000001` |
| Thai QR payment image (base64 SVG: payee, amount, payment ref) | a small placeholder SVG reading "FAKE QR (scrubbed)" |
| Coach names (real people) | `C.Alpha`, `C.Bravo` |
| Coach ids | `1001`, `1002` |
| Booking line id (`/bookings/lines/<id>/cancel`) | `900001` |
| Login page: its anti-forgery token, and the 10-digit placeholder on the phone field | `FAKE-ANTIFORGERY-TOKEN-08`, `0000000000` |

Checked and **not present** in any of the eight pages, so nothing to scrub:
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
| The Upcoming row or booking page at another ref, date, time, location, court or status | DERIVED | From pages 6 and 7 |
| The review form re-rendered with an error | SYNTHETIC | A court taken between review and confirm |
| The POST responses | SYNTHETIC | Apart from the two parked redirect Locations, which are real: `/wallet/topup/<id>?then=SPORTS-…` (Nov 11) and `/payment/SPORTS-…/topup-shortfall` (Nov 13) |
| An Awaiting-payment list with its row removed | DERIVED | |
| A review page for the other free court | DERIVED | |
| Crystal Sports (LOC001) versions of the G pages | DERIVED | |
