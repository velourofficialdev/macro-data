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

- Other-country yields come from official free sources: Bank of Canada (Valet API), Deutsche Bundesbank (Svensson curve), Japan Ministry of Finance (JGB benchmark yields) and Reserve Bank of Australia (table F2, no 30Y published). Saved as `data/YC_<CC>.csv`. China uses the official ChinaBond government bond yield curve (EastMoney, which republishes it, as fallback). India uses FBIL's daily G-Sec par yield curve (semi-annual YTM), backfilled from Jan 2024 and fetched day by day after that. France and Italy have no free official daily feed, so they come from CNBC's benchmark yields (closes at the European close, about 17:30 CET; weekend bars dropped). Checked when added: FR/IT/IN monthly averages match the official OECD series on FRED within about 1-4 bp, and the same CNBC feed for US 10Y matches FRED DGS10 within about 1 bp a day. Brazil is not included. The UK, Switzerland and New Zealand also come from CNBC benchmark yields (UK 2/5/10/30Y, Switzerland 2Y and 10Y only, New Zealand 5Y and 10Y only; the missing tenors are not published by CNBC and stay empty). Checked when added against OECD monthly 10Y on FRED: UK within about 2-6 bp, New Zealand 3-5 bp, Switzerland about 7 bp lower (different benchmark bond). Central bank policy rates for the US, euro area, UK, Japan, Switzerland, Canada, Australia and New Zealand come from the BIS daily policy-rate series (`data/POLICY_RATES.csv`). Currency COT (EUR, JPY, GBP, CHF, CAD, AUD, NZD) uses the same CFTC Legacy report, Non-Commercial group, for the CME currency futures (`data/COT_<CUR>.csv`). They feed the "Macro drivers" card in the FX section: per pair, four drivers (4-week trend of the 2Y yield gap vs the US, policy rate gap, speculator positioning Z-score, risk appetite from VIX) each marked supports higher, supports lower or neutral. It is context, not a forecast: a backtest of the 7 pairs (Apr 2025 to Oct 2026) found only the yield-gap trend leaned forward, weakly.


## This week strip and calendar
`calendar.json` is a hand-maintained list of upcoming central bank decisions and key US releases (Fed, ECB, BoE, BoJ, BoC, RBA, SNB, US jobs and CPI). Each entry names its source; update it every few months. The FX section's "This week" card shows the next 14 days from it, plus a one-line risk and dollar read. In the Macro drivers rows, heavily one-sided speculator positioning (|Z| of 2 or more) is shown as "crowded" and not counted for or against, and the policy rate row shows each bank's 6-month direction.

## Instruments, expected range and watchlist
The FX section also covers gold and three US indices: ES (S&P 500), NQ (Nasdaq 100), US30 (Dow 30), using the cash index levels from Yahoo Finance (`^GSPC`, `^NDX`, `^DJI`; FRED SP500, NASDAQ100, DJIA as fallback) saved in `data/idx/`, plus CFTC COT for the matching CME and CBOT futures. Each card shows a likely weekly range (+-1 sigma from the last 20 and 60 days of daily moves; in a 2001-2026 test on 7 pairs the weekly move stayed inside it 68.3% of weeks) and the scheduled events in the next 7 days. The macro drivers read is context: it was right about half of weeks in the same test. The risk and volatility rows are shown but not counted. The section uses the same terminal layout as Rates: a watchlist on the left (tick up to 5 rows to chart them as % change), the chart top-right, the Macro Drivers matrix bottom-right and a reading panel for the selected instrument. The watchlist choice is stored in this browser only (`macro-fx-watchlist`).

## Options positioning
`options_levels.py` (called by `update_data.py`) reads Cboe's free delayed option chains for SPX and GLD after the US close and saves `data/options/<KEY>.json`, a daily `<KEY>_history.csv` and the open interest by strike (`<KEY>_oi.csv`, used for next day's change table). It computes net gamma exposure by strike ($ per 1% move, expiries up to 60 days, assuming dealers are long customer calls and short customer puts), the gamma flip (where total gamma crosses zero, re-priced on a ±12% grid), call and put walls (largest call gamma above / put gamma below price), max pain for the nearest expiry, put/call ratios, a 1-day move from ATM implied vol and the front-expiry straddle range. S&P levels are shifted to ES by the closing basis (ES=F minus SPX); gold levels are GLD levels scaled by GC=F ÷ GLD. It is end-of-day positioning, not a live trade tape, and the gamma sign convention is a simplification.

The interactive desk at the top of the Options section (SPX / Gold switch, Gamma · Delta · Vanna · Charm · Theta · Vega tabs, expiry picker) uses `surface` in each `data/options/<KEY>.json`: per-strike exposure for every Greek, all expiries up to 60 days plus the first 12 expiries one by one, the gamma flip per expiry and the implied-vol smile. Views: Spine (profile by strike with walls, flip and price, ES / gold-future prices alongside), Terrain (rotatable 3D strike × expiry relief), Grid (strike × expiry heat table; click to focus an expiry) and Smile (call and put IV with a fitted curve). Every Greek uses the same dealer convention as GEX (calls +, puts −). "Copy levels" puts the current walls and flip on the clipboard. Drawn with the page's own script and colour tokens, no libraries.

Options desk extras: **Traditional / Effective / Shadow** modes (Effective = gamma re-priced at a chosen price move with each option keeping its IV; Shadow = the extra change when IV slides along the smile with price, i.e. sticky-moneyness minus sticky-strike; both for all expiries and the first three, moves −3% to +3% plus ±1σ), a king node (largest gamma strike), a regime label based on distance from the flip in 1σ moves, a **cross-expiry stack** (two configurable views side by side), a guide for each Greek, a **volatility** panel (ATM IV term structure, 25-delta risk reversal, 30-day implied vs 10/20/60-day realised vol and the premium between them, after the FlashAlpha volatility-surface-python definitions), **VIX term structure** (VIX3M ÷ VIX from FRED, with the ludovico-finance study result as context), **Replay** (price vs walls and flip from the daily history) and an **AI prompt** builder (Master / Quick bias / Key levels). The Overview has a **Regime fingerprint**: today's curve, real yield, VIX and 21-day moves in the S&P 500, gold and DXY (z-scored) matched against every past day, with what followed the five closest matches. SP500 and VIXCLS now keep 10 years of FRED history for it.

Instruments on the options desk: S&P 500 (SPX options → ES), Nasdaq 100 (NDX → NQ), Dow (DJX → YM; DJX is the Dow ÷ 100, so YM levels = DJX × the closing ratio), Russell 2000 (RUT → RTY) and gold (GLD → GC). Chains with more than 150 strikes in the window are grouped into wider strike buckets (e.g. 20 points for NDX). Realised vol for each comes from Yahoo index closes (^GSPC, ^NDX, ^DJI, ^RUT, GC=F), falling back to the saved CSVs for SPX and gold.
