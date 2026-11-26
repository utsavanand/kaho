# Bug reports: how Report a Bug works

**Report a Bug…** (in the menu bar menu and the Dock menu) sends a report
straight to the developer. It shipped in 2.4.10 (#33).

## The flow

```
Kaho (Report a Bug… form)
  │  POST JSON {version, description, email, diagnostics, log?}
  ▼
https://kaho.utsava.xyz/api/kaho-report                    ← tried first
https://my-digital-card-three.vercel.app/api/kaho-report   ← fallback
  │  Vercel serverless function (my-digital-card repo, api/kaho-report.js)
  │  uses KAHO_REPORTS_TOKEN (Vercel env var, Production)
  ▼
GitHub API → new issue in utsavanand/kaho-reports (private)
  ▼
GitHub emails the owner (repo owners watch their repos by default)
```

If sending fails, Kaho offers the old Mail draft instead.

Why two addresses: Spectrum's "Security Shield" filter (run by CUJO)
blocks all of `utsava.xyz` for its customers, redirecting to
`block.charter-prod.hosted.cujo.io`. Vercel's own `.vercel.app` name for
the site gets through.

## How it was set up

1. **A private repo for the reports**

   ```
   gh repo create utsavanand/kaho-reports --private
   ```

   It's private because reports can carry a user's email and, if they
   choose, a log with their dictations. The `kaho` repo is public.

2. **The endpoint**: `api/kaho-report.js` in `my-digital-card`.
   - Vercel turns any file in the root `api/` folder into a function, even
     on this Astro site.
   - Only POST is accepted. A report needs a description of at least 3
     characters.
   - Fields are capped: description 5,000 characters, log 40,000.
   - Each address can send 5 reports an hour (counted per running
     instance, so it's a best effort).
   - GitHub's error replies are never passed back to Kaho, because they can
     describe the token.
   - The log goes in a collapsible block, fenced so its contents can't break
     out of it.

3. **The token**: a fine-grained personal access token, made at
   https://github.com/settings/personal-access-tokens/new.
   - Repository access: **Only select repositories → kaho-reports**.
   - Permissions: **+ Add permissions**, search **Issues**, set
     **Read and write**. Searching "read" only finds "Repository creation".
   - Expiration: as long as GitHub allows.

4. **Storing the token on Vercel** without it ever appearing on screen. Copy
   it from GitHub, then:

   ```
   pbpaste | vercel env add KAHO_REPORTS_TOKEN production --cwd <my-digital-card checkout>
   ```

5. **Redeploy** so the function picks up the variable:

   ```
   vercel deploy --prod --yes
   ```

6. **Kaho's side**, in `kaho.py`:
   - `REPORT_URLS` lists the two addresses.
   - `send_bug_report()` tries them in order. A network error moves on to
     the next address; a refusal from the service (400 or 429) is shown and
     isn't retried.
   - `bug_diagnostics()` sends the version, settings, providers and models,
     but never keys, the dictionary or transcripts.
   - The log (last `REPORT_LOG_LINES`, 300 lines) is attached only if the
     user ticks the box, which is off by default.
   - `email_bug_report()` is the Mail fallback.

## Checking it

```
curl -X POST https://my-digital-card-three.vercel.app/api/kaho-report \
  -H 'content-type: application/json' \
  -d '{"version":"test","description":"test report"}'
# → {"ok":true,"id":N}
gh issue list -R utsavanand/kaho-reports
```

| Reply | What it means |
|---|---|
| `{"ok":true,"id":N}` | Filed as issue #N. |
| `503 "Reports aren't set up yet."` | `KAHO_REPORTS_TOKEN` is missing. Add it (step 4) and redeploy (step 5). |
| `502 "Couldn't file the report."` | GitHub rejected the token, probably expired or revoked. Check the Vercel function logs. |
| `429` | The rate limit was hit. |
| `400` | The description was empty. |

## Upkeep

- **When the token expires:** make a new one with the same settings (step 3),
  then repeat steps 4 and 5. Until then, Kaho falls back to offering a Mail
  draft, so reports aren't lost.
- **To stop getting the emails:** change the Watch setting on
  `kaho-reports`.
- **If the Spectrum block is lifted:** nothing changes. The first address
  just starts working for those users.
