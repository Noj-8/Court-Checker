# Tennis monitor — Phase 1 (cloud setup)

GitHub Actions starts the monitor every 5 minutes; each run polls 4 times at
60-second intervals for effective ~60s polling, so your Mac doesn't need to
be on. Phase 2 will add the pretty web UI on top.

(Why every 5 min + 4 inner polls instead of just `* * * * *`? GitHub Actions has
a documented 5-minute minimum cron interval and skips schedules during high load,
so the loop-inside-each-run pattern is the standard workaround.)

---

## What you'll do (about 10 minutes)

1. Create a GitHub account (skip if you have one)
2. Create a new public repo and upload these files
3. Set 3 secrets in the repo
4. Trigger a test run — verify it works
5. Wait. The cron takes over from there.

---

## Step 1 — GitHub account

If you don't have one, sign up at <https://github.com/signup>. Free is fine.

## Step 2 — Create a public repo and upload files

1. Go to <https://github.com/new>
2. **Repository name**: anything you like (`tennis-monitor` is fine)
3. **Public** (important — public repos get unlimited free Actions minutes; private would burn through the free tier in a week)
4. Don't tick "Add a README" — leave the repo empty
5. Click **Create repository**

On the empty repo page, you'll see a link "uploading an existing file". Click it.

Now drag and drop **all the files in this folder, preserving the folder structure**. Specifically:

- `monitor.py`
- `config.json`
- `state.json`
- `requirements.txt`
- `README.md` (this file)
- `.github/workflows/monitor.yml`

GitHub's drag-and-drop accepts folders, so you can drop the whole `tennis-monitor-cloud` folder contents at once. **It's important that `monitor.yml` ends up at `.github/workflows/monitor.yml` in the repo** — without that exact path, Actions won't see it.

If drag-drop doesn't preserve the `.github/workflows/` folder, do this instead:
1. Upload the loose files (`monitor.py`, `config.json`, etc.) to the root
2. Click **Add file → Create new file**
3. In the filename box, type: `.github/workflows/monitor.yml` — the slashes auto-create folders
4. Paste the contents of `monitor.yml`
5. Click **Commit changes**

Click the green **Commit changes** button to save everything.

## Step 3 — Set 3 secrets

In your repo:

1. Click **Settings** (top of the repo page)
2. In the sidebar: **Secrets and variables → Actions**
3. Click **New repository secret** and add three, one at a time:

| Name | Value |
|------|-------|
| `PHPSESSID` | The long cookie value (currently `68sj0h1vppr8i7qc2tpg915n5f`, but get a fresh one from your browser) |
| `EMAIL_USER` | `nojnoj2000@gmail.com` |
| `EMAIL_APP_PASSWORD` | The 16-character Gmail app password (no spaces) |

These are encrypted at rest and never shown again — that's why GitHub shows them as `***` in logs.

## Step 4 — Run a test

GitHub doesn't run scheduled workflows on a brand-new repo until you manually trigger one. So:

1. Click the **Actions** tab in your repo
2. If GitHub asks "I understand my workflows, go ahead and enable them" — click it
3. In the sidebar, click **Tennis monitor**
4. Click the **Run workflow** dropdown on the right → green **Run workflow** button
5. Wait ~30 seconds, then refresh. You should see a new run with a green checkmark.

   Note: the run takes ~3-4 minutes total (4 polls × 60s sleeps). It's normal
   for the run to be "in progress" for a while — that's the loop, not a hang.

6. Click into the run, expand the "Run monitor" step, you should see something like:

   ```
   [2026-05-08 19:30:00] Running 4 check pass(es), 60s apart
   [2026-05-08 19:30:00] --- iteration 1/4 ---
   [2026-05-08 19:30:00]   → Crystal Sports / 2026-05-10
   [2026-05-08 19:30:01]   → Crystal Sports G / 2026-05-10
   [2026-05-08 19:30:02]   currently open across watchlist: 0
   [2026-05-08 19:30:02]   no new openings this iteration
   [2026-05-08 19:30:02]   sleeping 60s before next iteration...
   [2026-05-08 19:31:02] --- iteration 2/4 ---
   ...
   [2026-05-08 19:33:05] Done.
   ```

If you see ❌ red X instead, click into it — the error will be in the log. Common ones:
- "missing env vars" → a secret name is wrong (must be exact: `PHPSESSID`, `EMAIL_USER`, `EMAIL_APP_PASSWORD`)
- "session expired" email → cookie needs refresh
- "Authentication failed" on email → app password wrong

## Step 5 — That's it

The cron now runs every 5 minutes automatically. You'll get an email when any slot you've configured opens up. Same as your Mac was doing, just without the Mac.

---

## Editing your targets

Targets live in `config.json`. To edit:

1. On GitHub, click `config.json` → click the pencil icon (top right of the file viewer) to edit
2. Change the JSON
3. **Commit changes** at the bottom
4. Next scheduled run (within 5 min) picks up the new config

Each target has:
- `name`: shown in alert emails
- `date`: `"YYYY-MM-DD"`
- `locations`: `["LOC001"]` (Crystal Sports), `["LOC002"]` (Crystal Sports G), or `["LOC001", "LOC002"]` (either)
- `times`: list of strings like `["20:00", "21:00"]` — must be `HH:00` format

Example for adding a new target:

```json
{
  "targets": [
    {
      "name": "Sunday 7am-9pm — Either court",
      "date": "2026-05-10",
      "locations": ["LOC001", "LOC002"],
      "times": ["07:00", "08:00", "09:00", "10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00", "17:00", "18:00", "19:00", "20:00", "21:00"]
    },
    {
      "name": "Tuesday evening",
      "date": "2026-05-12",
      "locations": ["LOC001"],
      "times": ["18:00", "19:00", "20:00"]
    }
  ]
}
```

## When PHPSESSID expires

You'll get an email titled "Tennis monitor: session expired". To refresh:

1. Log in to <https://crystalsports-booking.kegroup.co.th/booking.php>
2. DevTools → Application → Cookies → copy `PHPSESSID` value
3. In your repo: Settings → Secrets and variables → Actions
4. Click `PHPSESSID` → Update → paste new value → Update secret
5. Next scheduled run uses the new cookie

## Stopping the monitor

If you want to pause it (e.g. you've got nothing to watch):

- **Empty the targets list** in `config.json`: `{"targets": []}`. The action keeps running but does nothing.
- **Or** disable the workflow: Actions tab → Tennis monitor → ⋯ menu → Disable workflow.

## What's in the repo

- `monitor.py` — the Python script that does one check pass and exits
- `config.json` — your targets (edit this to add/change watch slots)
- `state.json` — auto-managed; tracks already-alerted slots so you don't get spam
- `requirements.txt` — Python deps (just `requests`)
- `.github/workflows/monitor.yml` — the cron schedule + workflow steps
