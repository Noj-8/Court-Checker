# Phase 2 — Deploy the dashboard UI

This guide deploys the Court Checker web dashboard to Vercel (free) and wires it
to your existing `Court-Checker` GitHub repo, so you can manage targets,
refresh your cookie, and see live status from any browser.

**Time required:** ~25 minutes, mostly clicking through web forms.

---

## Overview of what we're setting up

```
                                 ┌──────────────┐
                                 │   Browser    │
                                 │  (you / me)  │
                                 └──────┬───────┘
                                        │ HTTPS
                                        ▼
        ┌─────────────────────────────────────────────────┐
        │  Vercel (free Hobby tier)                       │
        │  ─ Next.js dashboard                            │
        │  ─ NextAuth → GitHub OAuth (sign in)            │
        │  ─ API routes use a GitHub PAT to read/write    │
        │    config.json, state.json, and PHPSESSID secret│
        └────────────────┬────────────────────────────────┘
                         │ GitHub API
                         ▼
        ┌─────────────────────────────────────────────────┐
        │  GitHub repo `Court-Checker` (existing)         │
        │  ─ config.json (targets)                        │
        │  ─ state.json (live status)                     │
        │  ─ Repository Secrets (PHPSESSID, etc.)         │
        │  ─ GitHub Actions cron — runs every 5 min       │
        └─────────────────────────────────────────────────┘
```

You'll need accounts on:
- **GitHub** (already have)
- **Vercel** — sign in with GitHub, free, no credit card required

---

## Step 1 — Add the new files to your existing repo

The dashboard files live alongside your existing monitor files in the same
`Court-Checker` repo. (Next.js for the UI, Python for the monitor — they don't
step on each other, and Vercel only deploys the Next.js part.)

### What's new vs. what's already there

Already in your repo (don't re-upload):
- `monitor.py`, `config.json`, `state.json`, `requirements.txt`
- `.github/workflows/monitor.yml`
- `README.md` — though I'll have you update it (see below)

New files to add:
- `package.json`, `tsconfig.json`, `tailwind.config.ts`, `postcss.config.js`
- `next.config.js`, `vercel.json`
- `.gitignore`, `.env.example`
- `app/` folder (entire tree)
- `lib/` folder (entire tree)
- `DEPLOY.md` (this file)

### Upload approach

Same trick as before — drag all the loose files into GitHub's upload page,
and create the nested folders manually if drag-drop loses them.

1. On your `Court-Checker` repo, click **Add file** → **Upload files**
2. Drag in the loose root files: `package.json`, `tsconfig.json`,
   `tailwind.config.ts`, `postcss.config.js`, `next.config.js`, `vercel.json`,
   `.gitignore`, `.env.example`, `DEPLOY.md`
3. Commit them
4. For the `app/` folder: it has nested files. Best path:
   - Click **Add file** → **Create new file**
   - Type filename: `app/layout.tsx` — slashes auto-create the folder
   - Paste the contents of `app/layout.tsx` from your download
   - Commit
   - Repeat for: `app/page.tsx`, `app/Dashboard.tsx`, `app/SignIn.tsx`,
     `app/globals.css`, `app/api/auth/[...nextauth]/route.ts`,
     `app/api/config/route.ts`, `app/api/cookie/route.ts`,
     `app/api/state/route.ts`
5. For `lib/`: same approach, create `lib/auth.ts`, `lib/github.ts`,
   `lib/types.ts`, `lib/tweetnacl-sealedbox-js.d.ts`

> Tedious, I know. The alternative is using Git from your terminal —
> if you're comfortable with that, just clone, copy files in, and push.
> But the web UI works fine if you go file by file.

When you're done, your repo's root should contain:

```
.github/workflows/monitor.yml      ← from Phase 1
.gitignore                          ← new
.env.example                        ← new
DEPLOY.md                           ← new (this file)
README.md                           ← from Phase 1
app/
  Dashboard.tsx
  SignIn.tsx
  api/
    auth/[...nextauth]/route.ts
    config/route.ts
    cookie/route.ts
    state/route.ts
  globals.css
  layout.tsx
  page.tsx
config.json                         ← from Phase 1
lib/
  auth.ts
  github.ts
  tweetnacl-sealedbox-js.d.ts
  types.ts
monitor.py                          ← from Phase 1
next.config.js                      ← new
package.json                        ← new
postcss.config.js                   ← new
requirements.txt                    ← from Phase 1
state.json                          ← from Phase 1
tailwind.config.ts                  ← new
tsconfig.json                       ← new
vercel.json                         ← new
```

**⚠️ Important: don't worry that GitHub Actions might break.** The `monitor.yml`
workflow only runs the Python script and ignores all the Next.js stuff. You
can verify next time the cron triggers (or manually run it from the Actions
tab) — it should still succeed.

---

## Step 2 — Create a GitHub Personal Access Token (PAT)

Vercel's API routes need a token to read/write your repo on your behalf. We'll
create a **fine-grained PAT** scoped to ONLY this repo with the minimum needed
permissions.

1. Go to <https://github.com/settings/personal-access-tokens/new>
2. Fill in:
   - **Token name**: `Court Checker dashboard`
   - **Expiration**: 90 days (or longer; you'll need to refresh later)
   - **Description** (optional): `Used by Vercel-hosted dashboard`
   - **Resource owner**: your username (Noj-8)
   - **Repository access**: select **Only select repositories**, then choose
     `Court-Checker`
3. Under **Permissions** → **Repository permissions**, set:
   - **Contents**: Read and write
   - **Secrets**: Read and write
   - **Metadata**: Read-only (auto-selected)

   Leave everything else at "No access".
4. Click **Generate token**
5. Copy the token (starts with `github_pat_…`). **You only see it once** —
   stash it in a notes app or password manager temporarily.

---

## Step 3 — Create a GitHub OAuth App (for sign-in)

This is what powers the "Sign in with GitHub" button. It's separate from the
PAT above — the OAuth App is for users authenticating to the dashboard;
the PAT is for the dashboard's backend talking to GitHub on your behalf.

1. Go to <https://github.com/settings/developers> → **OAuth Apps** →
   **New OAuth App**
2. Fill in:
   - **Application name**: `Court Checker`
   - **Homepage URL**: `https://example.com` (placeholder — we'll update after
     deployment)
   - **Authorization callback URL**:
     `https://example.com/api/auth/callback/github` (placeholder, same)
   - **Application description** (optional): `Tennis court availability monitor`
3. Click **Register application**
4. On the next page, copy the **Client ID** (visible)
5. Click **Generate a new client secret** → copy the **Client Secret**
   (visible once)

Stash both somewhere temporarily.

We'll come back to update the URLs after Vercel gives us a real domain.

---

## Step 4 — Deploy to Vercel

1. Go to <https://vercel.com/signup> and sign up (or log in) with **GitHub**.
   Click whatever permissions Vercel asks for to access your repos.
2. On the Vercel dashboard, click **Add New…** → **Project**
3. Find your `Court-Checker` repo in the list and click **Import**
4. On the configuration page:
   - **Project Name**: `court-checker` (or whatever you like — this becomes
     part of your URL)
   - **Framework Preset**: should auto-detect as **Next.js**
   - **Root Directory**: `./` (default — leave it)
   - **Build & Output Settings**: leave at defaults
5. Expand the **Environment Variables** section and add these one at a time
   (click "Add" between each):

| Name | Value |
|------|-------|
| `GITHUB_OWNER` | `Noj-8` |
| `GITHUB_REPO` | `Court-Checker` |
| `GITHUB_TOKEN` | the `github_pat_…` from Step 2 |
| `GITHUB_OAUTH_CLIENT_ID` | from Step 3 |
| `GITHUB_OAUTH_CLIENT_SECRET` | from Step 3 |
| `NEXTAUTH_SECRET` | a random string — generate with `openssl rand -base64 32` in Terminal, or use any random 32-character string |
| `ALLOWED_GITHUB_USERS` | `Noj-8` (comma-separate later if you add a friend, e.g. `Noj-8,FriendsUsername`) |

   We'll add `NEXTAUTH_URL` after the first deploy gives us a real domain.

6. Click **Deploy**. Wait ~2 minutes for the build. It'll fail to start
   correctly because `NEXTAUTH_URL` is missing, but that's fine — let it deploy.

7. Once it shows "Congratulations!" or similar, copy your Vercel domain.
   It'll look like `court-checker-xxxx.vercel.app`.

---

## Step 5 — Final wiring

### 5a. Update OAuth callback URLs

Back at <https://github.com/settings/developers> → click your OAuth App:

- **Homepage URL**: `https://YOUR-VERCEL-DOMAIN.vercel.app`
- **Authorization callback URL**:
  `https://YOUR-VERCEL-DOMAIN.vercel.app/api/auth/callback/github`

Click **Update application**.

### 5b. Add `NEXTAUTH_URL` to Vercel

In Vercel: your project → **Settings** → **Environment Variables** →
**Add New**:

| Name | Value |
|------|-------|
| `NEXTAUTH_URL` | `https://YOUR-VERCEL-DOMAIN.vercel.app` |

Then trigger a redeploy: **Deployments** tab → click the latest one → **⋯** →
**Redeploy**.

---

## Step 6 — Try it!

1. Open `https://YOUR-VERCEL-DOMAIN.vercel.app`
2. You should see the Court Checker landing page with a **Sign in with GitHub** button
3. Click it — first time, GitHub will ask you to authorize the OAuth App. Approve.
4. You should land on the dashboard, see your existing Sunday target,
   and a status pill showing how recently the monitor ran

Try the things:
- Click **Add target** → fill in a new target → save → it should appear in the list and within 5 min the cron will start watching it
- Click the edit pencil on a target → change times → save
- Click the trash icon → confirm → target is removed
- Scroll to **Session cookie** → click **Update PHPSESSID** → paste a value → it'll update the GitHub repo secret directly

If anything's broken, the Vercel **Functions** logs (Deployments → latest → Functions tab) will show errors.

---

## Common issues

**"Configuration error" / "OAuthSignin" error on sign-in:**
- The callback URL in your OAuth App doesn't match your Vercel domain.
  Double-check it's exactly `https://YOUR-DOMAIN.vercel.app/api/auth/callback/github`.

**Sign-in returns "AccessDenied":**
- Your GitHub username isn't in `ALLOWED_GITHUB_USERS`. The check is
  case-insensitive but must be the exact username (e.g. `Noj-8`).

**Dashboard loads but shows "Failed to load data from GitHub":**
- The PAT is wrong, expired, or doesn't have the right repo selected.
  Generate a new one at github.com/settings/personal-access-tokens, update
  `GITHUB_TOKEN` in Vercel, redeploy.

**Cookie update returns "failed to update secret":**
- Same root cause as above — the PAT needs `Secrets: Read and write` permission
  on the `Court-Checker` repo specifically.

**The monitor cron is failing now:**
- The Next.js files shouldn't break the Python action, but if they do, check
  the Actions tab → click the failing run → see the error. Most likely cause
  is that I missed something — let me know and we'll fix it.

---

## Adding a friend later

When you want to share with a friend:
1. Get their GitHub username
2. Vercel → your project → Settings → Environment Variables → edit
   `ALLOWED_GITHUB_USERS` → change `Noj-8` to `Noj-8,TheirUsername`
3. Redeploy (Deployments → latest → ⋯ → Redeploy)
4. Send them your dashboard URL — they sign in with their own GitHub

They'll see the same shared dashboard you do. Both of you can edit targets;
last write wins. (No conflict resolution; if you edit at the same time, one of
you might overwrite the other.)
