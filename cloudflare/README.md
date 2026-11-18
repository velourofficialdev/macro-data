# Hourly "fast" refresh (Cloudflare Worker)

A Cloudflare Worker with a Cron Trigger starts the GitHub `update` workflow in fast mode
(news, Fed odds, options, prices). The daily full run stays as the backup. The Worker has a
`scheduled` handler only (no web routes). It sends one POST and logs only the HTTP status:

    POST https://api.github.com/repos/velourofficialdev/macro-data/actions/workflows/update.yml/dispatches
    {"ref":"main","inputs":{"mode":"fast"}}

The token is read only from `env.GITHUB_TOKEN` and is never logged. No secret is stored in this repo.

## Owner steps (human-only)

1. Edit `.github/workflows/update.yml`. Replace `workflow_dispatch: {}` with:

```
  workflow_dispatch:
    inputs:
      mode:
        description: "full = everything, fast = news, Fed odds, options, prices only"
        type: choice
        options: [full, fast]
        default: full
```

   In the "Download latest data" step add under `env:` this line:

```
          FAST_RUN: ${{ github.event.inputs.mode == 'fast' && '1' || '' }}
```

   and keep the run line as `python update_data.py` (the script reads `FAST_RUN`; scheduled and
   push runs leave it empty, so they stay full). Recommended: before `git push` in
   "Commit refreshed data" add `git pull --rebase origin main`.

2. Create a fine-grained GitHub personal access token: resource owner `velourofficialdev`,
   repository `macro-data` only, permission Actions: Read and write (nothing else). Set an
   expiry and put the renewal date in your calendar.

3. Deploy to Cloudflare (paste the token only at the prompt, never into a file or chat):

```
cd cloudflare
npx wrangler login
npx wrangler secret put GITHUB_TOKEN
npx wrangler deploy
```

   Confirm the cron shows in the dashboard: Workers > macro-data-fast-cron > Triggers.

4. Decide the window. Default is hourly 12:05-21:05 UTC, Monday to Friday. Widen with
   `crons = ["5 * * * 1-5"]` in `wrangler.toml`. Each fast run commits data and republishes
   the page, so about 10 extra commits a day; decide if that is acceptable.

## Test locally

Put a throwaway token in `cloudflare/.dev.vars` (git-ignored) as `GITHUB_TOKEN=...`, then:

```
npx wrangler dev --test-scheduled
curl "http://localhost:8787/__scheduled"
```

Check the Actions tab for a new `update` run with mode `fast`. The Worker log shows
`dispatch ok: 204`; any other status is logged as `dispatch failed: HTTP <code>`.

## Rotate the token

1. Create a new token as in step 2.
2. `cd cloudflare && npx wrangler secret put GITHUB_TOKEN` (paste the new one).
3. Delete the old token in GitHub settings.
