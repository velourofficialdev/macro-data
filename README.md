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

Colors come from the design tokens at the top of `template.html` (primary #90BFF9, secondary #B96AC9, accent #787779, background #2E2E2E, surface #35383C, text #F8FAFC, muted #94A3B8, border #334155, plus up/down/warn and a derived light theme). The page has an Auto / Light / Dark switch in the top bar (Auto follows the device, and the browser remembers the choice). Charts are drawn by a small script inside `template.html`, so the page needs no internet connection or external libraries.

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
- Gold price (for the weekly price change next to Gold COT) is the COMEX gold future `GC=F` from Yahoo Finance. DXY is computed from the ECB's official euro reference rates with ICE's published formula (50.14348112 × EURUSD^-0.576 × USDJPY^0.136 × GBPUSD^-0.119 × USDCAD^0.091 × USDSEK^0.042 × USDCHF^0.036), at the 14:15 CET fixing; Yahoo (ICE close) then Stooq are fallbacks. Levels sit within about 0.2 of ICE's close; day-to-day moves differ more because the fixing is taken mid-day. The 7 FX pairs also come from the ECB reference rates (crossed through the euro), with Yahoo then FRED H.10 as fallbacks. Each file keeps a single source, so a fallback replaces the whole history rather than mixing sources.
- The rules in `playbook.json` are rules of thumb, not predictions or advice.

- Other-country yields come from official free sources: Bank of Canada (Valet API), Deutsche Bundesbank (Svensson curve), Japan Ministry of Finance (JGB benchmark yields) and Reserve Bank of Australia (table F2, no 30Y published). Saved as `data/YC_<CC>.csv`. China uses the official ChinaBond government bond yield curve (EastMoney, which republishes it, as fallback). India uses FBIL's daily G-Sec par yield curve (semi-annual YTM), backfilled from Jan 2024 and fetched day by day after that. France and Italy have no free official daily feed, so they come from CNBC's benchmark yields (closes at the European close, about 17:30 CET; weekend bars dropped). Checked when added: FR/IT/IN monthly averages match the official OECD series on FRED within about 1-4 bp, and the same CNBC feed for US 10Y matches FRED DGS10 within about 1 bp a day. Brazil is not included.
