# Court Checker — tennis court monitor for Crystal Sports Bangkok

Watches the Crystal Sports booking site for the court times you care about and
emails you the moment one opens up. Runs entirely in the cloud: a Python
monitor on GitHub Actions, plus a small web dashboard on Vercel for editing
targets (see [DEPLOY.md](DEPLOY.md)).

Crystal Sports replaced its booking site on **11 Nov 2026** with a
server-rendered ASP.NET Core site. Everything below describes the new setup;
the old `api_helper.php` / `PHPSESSID` flow is gone.

> **Auto-book is paused.** `AUTOBOOK_ENABLED = False` in `monitor.py` while it's
> rebuilt for the new site. Starred times (★ in the dashboard) still get the
> normal "slot open" email, with a note to book manually.

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

Each 🔴 email is sent **once**. After a session email, the monitor sends no
requests until the jar changes, so later runs are quiet. They exit 0, and the
dashboard shows the session as "Needs reseed".

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
- `autoBook` is the ★ star. It's kept for when auto-book returns, and for now
  just adds the "book manually" note to the email.

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
| `bot_session.py` | The bot's session: encrypted jar, cookie rotation, the single guarded GET |
| `booking_page.py` | Grid parser (Thai and English) and login-page detector |
| `probe_new_site.py` | Read-only diagnostic probe |
| `seal_session.py` | One-time Mac → Actions handoff (encrypts the jar, makes `BOT_JAR_KEY`) |
| `bot_session.enc` | The encrypted bot session (committed by the workflow after rotations) |
| `config.json` | Your targets |
| `state.json` | Monitor state: open slots, last check, session and site-read alerts |
| `.github/workflows/monitor.yml` | The monitor job |
| `.github/workflows/probe.yml` | The manual probe job |
| `test_*.py` | Tests: `python3 -m unittest test_monitor test_probe_new_site test_seal_session` |
| `app/`, `lib/` | The dashboard (Next.js, see [DEPLOY.md](DEPLOY.md)) |
| `WALLET_GUARD_SAFETY.md` | History: the old site's auto-book wallet guard |
