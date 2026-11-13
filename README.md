# Court Checker — tennis court monitor for Crystal Sports Bangkok

Watches the Crystal Sports booking site for the court times you care about and
emails you the moment one opens up. Runs entirely in the cloud: a Python
monitor on GitHub Actions, plus a small web dashboard on Vercel for editing
targets (see [DEPLOY.md](DEPLOY.md)).

Crystal Sports replaced its booking site on **11 Nov 2026** with a
server-rendered ASP.NET Core site. Everything below describes the new setup;
the old `api_helper.php` / `PHPSESSID` flow is gone.

> **Auto-book** for starred times (★) is controlled by the `AUTOBOOK_MODE`
> repository variable: `off` (the default), `dry_run` or `live`. See
> [Auto-book](#auto-book-starred-times). With it off, starred times just get the
> normal "slot open" email with a note to book manually.

---

## How it works

```
cron-job.org ──(every ~5 min: workflow_dispatch)──▶ GitHub Actions: monitor.yml
                                                        │
                                                        ▼
                                  monitor.py  (4 passes, 60 s apart)
                                    │  GET /booking?date=…&loc=LOC001|LOC002&time=0&sel=
                                    │  on the bot's OWN login session (bot_session.enc)
                                    ▼
                         slot opened? ──▶ email (Gmail)
                                    │
                                    ▼
                commit state.json + bot_session.enc back to the repo (one commit)
```

- **The trigger.** cron-job.org calls GitHub's `workflow_dispatch` API for
  `monitor.yml` roughly every 5 minutes, because GitHub's own schedule is often
  late or skipped. `monitor.yml` also keeps its `*/5` schedule as a fallback.
  Runs never overlap: `monitor.yml` and `probe.yml` share the concurrency group
  `tennis-monitor`.
- **The bot session.** The site rotates the `cs.member` cookie about every 15
  minutes, and presenting a superseded copy kills the login. So the monitor uses
  its own dedicated session, not your browser's. It lives in
  **`bot_session.enc`**, a Fernet-encrypted cookie jar committed to the repo.
  The key is the **`BOT_JAR_KEY`** repo secret. Every response that changes a
  cookie is saved to the jar before anything else happens, and the workflow
  commits and pushes the jar after every run. Plaintext never touches the
  runner's disk.
- **Reading the page.** The page is parsed the same way in Thai and English:
  - A slot is open when its `a.slot` has no `is-disabled` class and its free
    count isn't 0.
  - A date shows as "not open yet" when the page has no slot grid and says when
    booking opens (e.g. "Booking opens 21:00" or "เปิดจอง 21:00 น.").
  - Anything else is logged and skipped as an unexpected page.
- **No double emails.** `state.json` remembers which times are already open
  (`known_open`, keys `date|loc|any|HH:MM`), so you get one email per opening.
  The grid works per time slot, not per court.

## Emails you can get

| Subject | Meaning | What to do |
|---|---|---|
| 🎾 N tennis slot(s) open | A watched time just opened | Book it (link in the email) |
| 🔴 Bot session expired — reseed needed | The site sent the bot to its login page | [Reseed](#reseeding-the-bot-session) |
| 🔴 Bot session likely lost — reseed needed | A rotated cookie couldn't be saved or pushed | [Reseed](#reseeding-the-bot-session) |
| 🔴 Monitor can't read the booking site | No page has been readable for 30 min (site down, network, or the page changed) | Usually nothing; it clears itself on the next good read |

Auto-book emails (only when `AUTOBOOK_MODE` is `dry_run` or `live`) are listed
under [Auto-book](#auto-book-starred-times).

Each 🔴 email is sent **once**. After a session email, the monitor sends no
requests until the jar changes, so later runs are quiet. They exit 0, and the
dashboard shows the session as "Needs reseed".

## Auto-book (starred times)

**Real money.** In `live` mode the bot books a starred time as soon as it
opens and pays from your **Wallet**.

### Modes

| `AUTOBOOK_MODE` | What happens when a starred time opens |
|---|---|
| unset, empty, `off`, or anything else | Nothing new: the normal 🎾 email, with "book manually". No auto-book requests at all. |
| `dry_run` | Every check runs, up to and including reading the booking form, then a 🧪 email says what it **would** have booked. Nothing is submitted. You still get the 🎾 email. |
| `live` | It books. You get one result email (🟢 / 🟡 / 🔴 / 🟠) instead of the 🎾 email for that time. |

`AUTOBOOK_MODE` is a GitHub **repository variable** (not a secret, and not a
Vercel setting). Set it in repo → Settings → Secrets and variables → Actions
→ **Variables** tab, or from the Mac:

```bash
gh variable set AUTOBOOK_MODE --body dry_run    # or: live / off
gh variable list                                # check what it is now
```

Each run reads it once at the start, so a change applies from the next run
(within about 5 minutes). The run's log shows `auto-book mode: …`, and the
run writes the mode it used to `state.json` (`autobook_mode`), which the
dashboard shows.

Start with `dry_run` and read a few 🧪 emails before switching to `live`.

### What it does, in order

1. **One booking per date + time, ever,** across both locations. If 20:00 is
   starred at both Crystal Sports and Crystal Sports G, it books only one.
2. **Checks My bookings first,** both Upcoming and Awaiting payment. If you
   already have a booking whose time overlaps that slot on that date (any
   location, any court; a 22:00–24:00 booking covers 23:00), it doesn't book. Awaiting-payment rows don't show the court time,
   so it opens each one's own page (`/bookings/SPORTS-…`) to read it.
   Bookings at other times never block. If a booking's date/time can't be
   read at all, it plays safe: no booking that pass, one ℹ️ email, and it
   tries again while the time stays open.
3. **Opens the time's own page** and takes the first free court (site order).
4. **Checks the booking form** before submitting: right branch, court, date,
   time, a price, and exactly the expected fields. Anything different, any
   new required field, or a coach already selected → it stops with 🔴
   "booking form changed / page mismatch" and submits nothing.
5. **Records the attempt** in `state.json` before submitting, then submits
   once: **Wallet**, terms accepted, no coach.
6. **Checks My bookings again** to decide the outcome (below). If the court
   was taken in the meantime, it tries **one** other free court at the same
   time, never more.

A run that dies mid-booking is never retried: the next run sends 🟠 once.

### Auto-book emails

| Subject | Meaning | What to do |
|---|---|---|
| 🧪 Would have booked … | Dry run: everything checked out | Nothing (book manually if you want it) |
| 🟢 Paid: … | Booked and paid from the Wallet | Nothing |
| 🟡 Parked — pay within 15 min: … | Booked, but the Wallet didn't cover it. The site holds it as **Awaiting payment** | **Pay within 15 minutes** using the link in the email, or it lapses. |
| 🔴 Failed: … | Not booked: the courts were taken, or the attempt couldn't be recorded (then nothing was sent) | Book manually if anything's left |
| 🔴 Failed: booking form changed / page mismatch | The site's pages changed; nothing was submitted. The email lists what differed | Book manually; the code needs updating |
| 🟠 Unclear — check My bookings | A booking request was sent but the result couldn't be confirmed | **Check My bookings now.** It will not be retried. |
| ℹ️ Already booked: … | You already have a booking at that date and time | Nothing |
| ℹ️ Not auto-booking … — can't check your bookings | A booking's date/time couldn't be read, so it didn't risk a double booking | Book manually if you want it; it keeps trying quietly |
| 🔴 state.json unreadable — auto-book halted | `state.json` (which holds the "already booked" records) couldn't be read. Auto-book is **halted**: no booking requests at all. Slot-open emails carry on | Check My bookings, then [resume](#if-auto-book-is-halted) |

Each result is sent once per date + time. If a result email can't be sent,
that time is included in the normal 🎾 email instead. A failure inside
auto-book never stops the 🎾 email.

`state.json` and the Actions logs are public. They hold only fixed reason
codes, form field names and a short fingerprint of booking refs: no page text,
field values, refs or court names. The full ref, the court and the payment
link are only in your emails.

### If auto-book is halted

When `state.json` can't be read, the monitor keeps the bad copy aside on the
runner, starts a fresh `state.json` with an `"autobook_halted"` entry, and
sends one 🔴 email. Auto-book stays off, whatever `AUTOBOOK_MODE` says, and
the dashboard banner says *HALTED*. To resume, check My bookings, then:

```bash
git pull --rebase
python3 - <<'PY'
import json; s = json.load(open("state.json")); s.pop("autobook_halted", None)
json.dump(s, open("state.json", "w"), indent=2)
PY
git commit -am "Resume auto-book" && git push
```

### Emergency: turn it off

```bash
gh variable set AUTOBOOK_MODE --body off
```

(or delete the variable, or set it to `off` on the Variables tab). The next
run is notify-only. A run that's already going finishes with the mode it
started with (up to about 4 minutes). To stop that too, cancel it in the
Actions tab, and pause the cron-job.org job (see
[Stopping the monitor](#stopping-the-monitor)).

## Secrets (repo → Settings → Secrets and variables → Actions)

| Name | What |
|---|---|
| `BOT_JAR_KEY` | The Fernet key for `bot_session.enc`. It's created by `seal_session.py`, which copies it to the clipboard without ever printing it. |
| `EMAIL_USER` | The Gmail address the alerts are sent from (and to, unless `email_to` is set in `config.json`) |
| `EMAIL_APP_PASSWORD` | A Gmail app password for that address |

`PHPSESSID` and `MEMBER_MOBILE` from the old site aren't used any more and can
be deleted.

## Reseeding the bot session

Do this when you get a "reseed needed" email. The steps are also on the
dashboard under **Bot session**. On the Mac, in `court-checker-live`:

1. Open an **Incognito** window and log in to
   <https://crystalsports-booking.kegroup.co.th>. Don't use your everyday browser
   session: if you share it with the bot, the first rotation kills one of them.
2. Delete the old session:
   ```bash
   git pull --rebase && git rm -q bot_session.enc && rm -f .bot_session.json.handed-off-*
   ```
3. In DevTools → Application → Cookies, copy `cs.did` and run
   `export CS_DID="$(pbpaste)"`. Then copy `cs.member` **last** and run
   `export CS_MEMBER="$(pbpaste)"`. The values never appear in chat or in shell
   history.
4. Close the Incognito window. **Don't log out.**
5. Seed the jar and check it once:
   ```bash
   python3 probe_new_site.py; unset CS_DID CS_MEMBER
   ```
6. `python3 seal_session.py`: encrypts the jar into `bot_session.enc`, puts a
   **new** key on the clipboard, and renames the plaintext jar to
   `.bot_session.json.handed-off-<time>`. After that the Mac refuses to use it.
7. Paste the key into `BOT_JAR_KEY` **before copying anything else**:
   ```bash
   pbpaste | gh secret set BOT_JAR_KEY; pbcopy </dev/null
   ```
   You can also use GitHub → Settings → Secrets and variables → Actions →
   `BOT_JAR_KEY` → Update.
8. ```bash
   git add bot_session.enc && git commit -m "Reseed bot session" && git push
   ```

The next run picks it up. When a run reads a page, the "Needs reseed" status
clears. Once that first run has gone green, delete the
`.bot_session.json.handed-off-*` file. It's a superseded plaintext copy.

`seal_session.py` needs `pip3 install cryptography` on the Mac.

## Editing targets

Use the dashboard (see [DEPLOY.md](DEPLOY.md)), or edit `config.json` directly:

```json
{
  "targets": [
    {
      "name": "Fri evening",
      "date": "2026-11-20",
      "locations": ["LOC001", "LOC002"],
      "times": [{"time": "20:00", "autoBook": false}, {"time": "21:00", "autoBook": true}]
    }
  ]
}
```

- `locations`: `LOC001` = Crystal Sports, `LOC002` = Crystal Sports G.
- `times`: `HH:00`, from 06:00 to 23:00 (the grid's 18 slots). Plain strings
  like `"20:00"` are also accepted.
- `autoBook` is the ★ star: the times auto-book may book, depending on
  [`AUTOBOOK_MODE`](#auto-book-starred-times). With the mode `off` it just adds
  the "book manually" note to the email.

Past dates are ignored automatically.

## The probe (`probe_new_site.py` / `probe.yml`)

The probe is a read-only diagnostic: it makes GET `/booking` requests only,
prints cookie fingerprints and never values, and logs rotations with their
expiry times. **Pause the cron-job.org job before you run `probe.yml`.** It
shares the `tennis-monitor` group, so it would otherwise sit queued behind the
monitor (GitHub can cancel a queued run) and stop monitor checks while it runs.

## Stopping the monitor

Pause the job on cron-job.org, and disable the workflow under Actions → Tennis
monitor → ⋯ → Disable workflow, which also stops the `*/5` fallback schedule.
Emptying the targets list (`{"targets": []}`) also works: the monitor then sends
no requests.

## What's in the repo

| File | What |
|---|---|
| `monitor.py` | The monitor: check passes, emails, state |
| `autobook.py` | Auto-book: the checks, the booking flow and its emails |
| `bot_session.py` | The bot's session: encrypted jar, cookie rotation, and the one guarded request function (a short allow-list of GETs, plus the single POST to `/booking/confirm`, live mode only) |
| `booking_page.py` | Page parsers: the grid, the court and review pages, My bookings (Thai and English), and the login-page detector |
| `probe_new_site.py` | Read-only diagnostic probe |
| `seal_session.py` | One-time Mac → Actions handoff (encrypts the jar, makes `BOT_JAR_KEY`) |
| `bot_session.enc` | The encrypted bot session (committed by the workflow after rotations) |
| `config.json` | Your targets |
| `state.json` | Monitor state: open slots, last check, session and site-read alerts |
| `.github/workflows/monitor.yml` | The monitor job |
| `.github/workflows/probe.yml` | The manual probe job |
| `test_*.py` | Tests: `python3 -m unittest test_monitor test_probe_new_site test_seal_session test_autobook` |
| `tests/fixtures/` | Scrubbed copies of live pages for the tests (see its README). The raw pages go in `fixtures_raw/`, which is gitignored and never committed |
| `app/`, `lib/` | The dashboard (Next.js, see [DEPLOY.md](DEPLOY.md)) |
| `WALLET_GUARD_SAFETY.md` | History: the old site's auto-book wallet guard |
