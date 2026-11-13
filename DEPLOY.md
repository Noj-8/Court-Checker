# Deploying the dashboard

The Court Checker dashboard is a small Next.js app on Vercel. It lets you
manage targets and see the monitor's status from any browser. It reads and
writes two files in the GitHub repo and nothing else:

- `config.json`, your targets (read and write)
- `state.json`, the monitor's status (read only)

It has **no access to secrets and the bot session**. It can't reseed or change
`BOT_JAR_KEY`; the **Bot session** panel only shows status and the reseed steps
(see [README.md](README.md#reseeding-the-bot-session)).

```
 Browser ──HTTPS──▶ Vercel: Next.js dashboard
                      ─ NextAuth → GitHub OAuth (sign-in, allowlisted users)
                      ─ API routes use a GitHub token to read/write
                        config.json and read state.json
                              │ GitHub API (Contents only)
                              ▼
                    GitHub repo Noj-8/Court-Checker
                      ─ config.json, state.json, bot_session.enc
                      ─ Actions: monitor.yml (triggered by cron-job.org)
```

Vercel's automatic deploys from `main` are **off** (`vercel.json`), because
the monitor commits `state.json` every few minutes and each commit would
otherwise trigger a deploy. You deploy by hand with `npx vercel --prod`.

---

## 1. GitHub token for the dashboard (`GITHUB_TOKEN`)

Create a fine-grained personal access token at
<https://github.com/settings/personal-access-tokens/new>:

- **Repository access:** only `Court-Checker`
- **Repository permissions:**
  - **Contents:** Read and write
  - **Metadata:** Read-only (selected automatically)
  - everything else: **No access**

The dashboard no longer writes any secrets, so it needs **no Secrets
permission**. If your current token has *Secrets: Read and write* from the
PHPSESSID days, replace it with a Contents-only token.

## 2. GitHub OAuth app (sign-in)

At <https://github.com/settings/developers>, go to **OAuth Apps → New OAuth
App** and set:

- **Homepage URL:** `https://court-checker.vercel.app`
- **Authorization callback URL:** `https://court-checker.vercel.app/api/auth/callback/github`

Copy the client ID, and generate a client secret.

## 3. Vercel environment variables

Set these in the Vercel project → **Settings → Environment Variables**
(`.env.example` lists the same names):

| Name | Value |
|---|---|
| `GITHUB_OWNER` | `Noj-8` |
| `GITHUB_REPO` | `Court-Checker` |
| `GITHUB_TOKEN` | the token from step 1 |
| `GITHUB_OAUTH_CLIENT_ID` | from step 2 |
| `GITHUB_OAUTH_CLIENT_SECRET` | from step 2 |
| `NEXTAUTH_SECRET` | a random string, e.g. `openssl rand -base64 32` |
| `NEXTAUTH_URL` | `https://court-checker.vercel.app` |
| `ALLOWED_GITHUB_USERS` | `Noj-8`, comma-separated if you add a friend. If it's empty, nobody can sign in. |

## 4. Deploy

From the repo root, on the commit you want live:

```bash
npm install
npm run build          # optional local check: the type check runs as part of the build
npx vercel --prod
```

The first time, `npx vercel` asks you to log in and link the existing
`court-checker` project. After that, `npx vercel --prod` is the whole deploy.

## 5. Check it

Open <https://court-checker.vercel.app> and sign in with GitHub. You should see:

- **Status bar:** "Active" if the monitor read a booking page in the last 10
  minutes, otherwise "Idle".
- **Watch list:** your targets, each showing either "N times open" or
  "Monitoring". Starred times are labelled by the auto-book mode the monitor
  last reported (`autobook_mode` in `state.json`):
  - *starred — notify only (auto-book off)*
  - *starred — dry run (emails what it would book, books nothing)*
  - **AUTO-BOOK — books with Wallet when it opens** (in red)

  The banner above the list says the same. It says *HALTED* while
  `state.json` has an `"autobook_halted"` entry, and *PAUSED* after a failed
  state push, until a later push succeeds. Until a monitor run has reported a
  mode, the dashboard shows "mode unknown" with the cautious, red live-style
  wording.
- **Bot session:** one of
  - **OK**
  - **Needs reseed**, with the time it was detected (the steps open
    automatically)
  - **Stale**, if no page has been read recently

Adding, editing and deleting a target each make a commit "Update config (via
dashboard, by @you)". The next monitor run picks the change up.

---

## Common issues

**"OAuthSignin" / configuration error on sign-in.** The callback URL in the
OAuth app must be exactly
`https://court-checker.vercel.app/api/auth/callback/github`.

**"AccessDenied".** Your GitHub username isn't in `ALLOWED_GITHUB_USERS`.

**"Failed to load data from GitHub".** `GITHUB_TOKEN` is wrong, expired, or not
scoped to `Court-Checker` with Contents access. Make a new one, update it in
Vercel, and deploy again.

**Bot session says "Stale" but there's no email.** Possible causes:

- The cron-job.org job is paused or failing.
- The Actions runs are failing (check the Actions tab).
- The site has been unreadable for under 30 minutes, so the email isn't due yet.

**Bot session says "Needs reseed".** Follow the steps in the panel, or in
[README.md](README.md#reseeding-the-bot-session).

## Auto-book mode

Auto-book is switched by the **`AUTOBOOK_MODE` repository variable** on
GitHub (`off` / `dry_run` / `live`), not by anything in Vercel. The dashboard
can't change it, and needs no extra token permission to show it: it reads the
mode the monitor writes to `state.json`. So a change appears after the next
monitor run. The dashboard only edits which times are starred (★). To check
or change the mode:

```bash
gh variable list
gh variable set AUTOBOOK_MODE --body off     # emergency off: applies from the next run
```

See [README.md](README.md#auto-book-starred-times) for what each mode does and
the emails it sends.

## Adding a friend

Add their GitHub username to `ALLOWED_GITHUB_USERS` in Vercel, run
`npx vercel --prod`, and send them the URL. Everyone shares the same
`config.json`. If two people save at the same moment, the last save wins.
