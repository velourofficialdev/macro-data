# Macro Dashboard

Treasury curve and spreads (10Y-2Y, 10Y-3M, 20Y-10Y, 30Y-5Y), 10Y real yield, DXY, and CFTC positioning (USD Index and Gold), with a plain-English meaning next to every reading. Replaces the old Excel output.

```
update_data.py      downloads FRED / CFTC / DXY into data/*.csv   (runs in GitHub Actions)
build_dashboard.py  data/*.csv + playbook.json  ->  docs/index.html
playbook.json       thresholds and meanings. Edit this to change what the dashboard says
template.html       page layout
.github/workflows/  daily update, build and hand-off to the public page repo
legacy/             the original notebook, kept for reference
```

## Look and feel

Colors follow the Velour base `#42070E` (CSS variables at the top of `template.html`). The four chart colors were checked for colorblind separation and contrast in both light and dark mode, so change them there only if you re-run the check. The page has an Auto / Light / Dark switch in the top bar (Auto follows the device, and the browser remembers the choice). Charts are drawn by a small script inside `template.html`, so the page needs no internet connection or external libraries.

## How it is hosted

Two repos keep the engine out of sight:

- **macro-data (private, this repo).** Downloads the data, holds the CSVs, `playbook.json`, the build code and the Excel export. A GitHub Action runs every weekday at 22:30 UTC, commits the refreshed data here, builds the page and pushes only `index.html` and `Macro_Dashboard.xlsx` to the public repo.
- **macro-dashboard (public).** Contains only those two files and a small workflow that publishes them with GitHub Pages at `https://velourofficialdev.github.io/macro-dashboard/`.

One-time setup for the hand-off:

1. On GitHub open Settings > Developer settings > Personal access tokens > Fine-grained tokens > Generate new token. Resource owner: velourofficialdev. Repository access: Only select repositories > `macro-dashboard`. Permissions: Contents = Read and write. Set a long expiry and note the renewal date.
2. In this repo open Settings > Secrets and variables > Actions > New repository secret. Name `DASHBOARD_REPO_TOKEN`, value the token.
3. Open the Actions tab, pick **Update data and publish dashboard**, and click **Run workflow**.

A second workflow, **Check publish token**, runs every Monday, tests the token and opens an issue (GitHub emails you) when it is missing, rejected, unable to push, or within 21 days of expiring. GitHub offers no way to renew a personal token automatically, so renewal is still a manual 2-minute step.

If the token expires the workflow still updates the data here and shows a warning, but the public page stops updating until a new token is saved.

## Changing what the dashboard says

Open `playbook.json`. Each reading has bands (`lt` means "below this number"), and each band has a `label`, `meaning` and `so_what`. Commit the change and the page rebuilds. The `cross_signals` section combines readings, for example "real yields up and dollar up".

Suggested workflow so a broken edit never goes live:

```
git checkout -b dev        # edit playbook.json or code here
git push -u origin dev     # opens a build-only check, nothing is deployed
# when it looks right: merge dev into main
```

## Run on your own computer

```
python -m venv .venv && .venv\Scripts\activate      # Windows
pip install -r requirements.txt
python update_data.py       # needs internet
python build_dashboard.py   # writes docs/index.html, open it in a browser
```

## Notes

- Positioning detail uses the same CFTC report for everything: net, net/long/short weekly change, open interest and its change, net as % of open interest (also rescaled 0-100 over 52 reports), plus the Friday-close price and its change from the prior Friday. The last 52 reports are shown in the weekly tables.
- Both COT series use the CFTC Legacy Futures Only report, Non-Commercial group (Gold: COMEX 088691, USD Index: ICE 098662), so they are comparable in kind; still read each against its own history.
- Z-scores use 252 trading days (daily series) and 52 weekly reports (COT). The COT Index is 0 at the 52-week low net position and 100 at the high.
- If a source fails, the old CSV is kept and the failure shows in the **Data status** table on the page.
- Gold price (for the weekly price change next to Gold COT) is the COMEX gold future `GC=F` from Yahoo Finance. DXY comes from Yahoo Finance, with Stooq as a fallback. Cloud servers are sometimes blocked by Yahoo, so check the status table if the DXY date stops moving.
- The rules in `playbook.json` are rules of thumb, not predictions or advice.

- Other-country yields come from official free sources: Bank of Canada (Valet API), Deutsche Bundesbank (Svensson curve), Japan Ministry of Finance (JGB benchmark yields) and Reserve Bank of Australia (table F2, no 30Y published). Saved as `data/YC_<CC>.csv`. France, Italy, China, India and Brazil have no free daily official feed, so they are not included.
