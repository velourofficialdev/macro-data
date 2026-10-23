# macro-data (private): rules for Claude sessions

This repo builds the Macro Dashboard (Brain item **B1**). It is private on purpose: the public page lives in `velourofficialdev/macro-dashboard` and holds only `index.html` and `Macro_Dashboard.xlsx`.

## Hard rules

- Use the **velourofficialdev** GitHub account only. Never the deiruuuu account.
- Work on a branch and open a pull request. **Never push to `main`, and never merge unless the owner explicitly says to.** A push to `main` publishes the public page. When the owner gives permission to merge, merge that PR only, then check the publish run succeeded.
- Never edit `.github/workflows/`, secrets, tokens or the public repo. Never print a secret or a token.
- Do not commit build output. Before committing run `git checkout -- docs data` and delete untracked build files, so a commit holds source changes only.
- Do not copy data CSVs, `playbook.json` internals or source code into the public repo or any public place.
- Use only free, key-less data sources. If a source needs a key, a paid plan or an account, stop and ask.

## Layout

| File | Job |
|---|---|
| `update_data.py` | downloads FRED / CFTC / DXY / FX / Fed odds into `data/` (runs in GitHub Actions) |
| `playbook.json` | thresholds and the plain-English `label` / `meaning` / `so_what` for each reading, plus `cross_signals` |
| `template.html` | page layout and the design tokens at the top |
| `build_dashboard.py` | `data/*.csv` + `playbook.json` + `template.html` -> `docs/index.html` |
| `export_excel.py` | builds `docs/Macro_Dashboard.xlsx` |
| `data/status.json` | per-source health: `ok`, `checked_utc`, `last_data_date`, `error`, `note` |

## Check before every pull request

```
pip install -r requirements.txt
python build_dashboard.py          # must finish with no errors
python -c "import json; json.load(open('playbook.json'))"
```

Then open `docs/index.html` in headless Chromium (preinstalled; use `/opt/pw-browsers/chromium`, never run `playwright install`) through `file://` and confirm there are no console errors and the page is not empty. A pull request also triggers a build-only check that deploys nothing.

## Design and wording

- The page must stay simple and easy to look at. Keep the Auto / Light / Dark switch working.
- The owner wants the Velour look (base colour `#42070E`). The tokens at the top of `template.html` are currently a blue/purple dark theme. **Do not recolour on your own;** if a task touches colours, ask first.
- Wording is educational. Keep `meta.disclaimer` in `playbook.json`. Never write "buy", "sell" or a price target.

## Where requests come from

The owner's queue is the Notion database "Dashboard Requests (B1)" under the brain page `4014ea14-1469-815e-80f3-f84947d9cb1e` (data source `collection://968819b4-ad5f-4967-893b-3b982ab62bb1`). The scheduled Dashboard Builder works from it. Keep the row's Status and notes up to date when you finish a request.

## Specialist agents in `.claude/agents/`

- `dashboard-dev`: layout, charts and new panels (`template.html`, `build_dashboard.py`).
- `playbook-editor`: wording and thresholds in `playbook.json`.
- `data-source-checker`: investigates failing or stale sources and finds free replacements.
