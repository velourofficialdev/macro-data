# macro-data (private): rules for Claude sessions

This repo builds the Macro Dashboard (Brain item **B1**). It is private on purpose: the public page lives in `velourofficialdev/macro-dashboard` and holds only `index.html` and `Macro_Dashboard.xlsx`.

## Hard rules

- Use the **velourofficialdev** GitHub account only. Never the deiruuuu account.
- Work on a branch and open a pull request. **Never push to `main`.** A push to `main` publishes the public page. Merging is done only by the `merger` agent, under the owner's standing permission (2026-10-23): a PR may be merged when `judge` has given PASS on its current head sha, and a merge that breaks the live page is reverted at once. Nobody else merges.
- Never edit `.github/workflows/`, secrets, tokens or the public repo. Never print a secret or a token.
- Do not commit build output. Before committing run `git checkout -- docs data` and delete untracked build files, so a commit holds source changes only.
- Do not copy data CSVs, `playbook.json` internals or source code into the public repo or any public place.
- Data sources may use an API key (free or free-tier). Keys live only in repository secrets and reach the code as environment variables (for example `os.environ.get("FRED_API_KEY")`); never commit, log or print a key, and never put one in a URL that gets logged. A keyed source must fail soft: if its variable is missing, skip it and record that in `status.json` instead of failing the run. Adding a secret, or the `env:` line that passes it to the workflow, is the owner's step (workflows and secrets stay off-limits), so say exactly which secret name and line are needed. Ask before using anything that costs money or needs a paid plan, and check the source's terms allow showing its data on a public page.

## Layout

| File | Job |
|---|---|
| `update_data.py` | downloads FRED / CFTC / DXY / FX / Fed odds into `data/` (runs in GitHub Actions) |
| `playbook.json` | thresholds and the plain-English `label` / `meaning` / `so_what` for each reading, plus `cross_signals` |
| `template.html` | page layout and the design tokens at the top |
| `build_dashboard.py` | `data/*.csv` + `playbook.json` + `template.html` -> `docs/index.html` |
| `export_excel.py` | builds `docs/Macro_Dashboard.xlsx` |
| `data/status.json` | per-source health: `ok`, `checked_utc`, `last_data_date`, `error`, `note` |

## How changes are made: the agent team

Every change request goes through the `/team` skill (`.claude/skills/team/SKILL.md`). The session that receives the request is the lead: `planner` writes the plan, builders (`data-dev`, `dashboard-dev`, `playbook-editor`, `data-source-checker`) work in parallel in separate worktrees, checkers (`qa-tester`, `design-reviewer`, `data-auditor`, `code-reviewer`) review in parallel, `judge` decides PASS / FIX / HUMAN, and `merger` merges, watches the publish run and reverts if the live page breaks. The owner is asked only about things that need a human: money, secrets and workflow lines, licensing, a brand recolour, or a request whose intent is unclear. Requests come through chat only; the old scheduled Dashboard Builder is off.

## Check before every pull request

```
pip install -r requirements.txt
python tools/check.py            # rules, playbook, real build, fixture build, browser checks; must print PASS
python tools/shots.py PAGE OUT    # screenshots + layout suspects for design review
```

`tools/check.py` builds into `/tmp/macro-check`, never into `docs/`. Chromium is preinstalled under `/opt/pw-browsers`; never run `playwright install`. The sandbox cannot reach Cboe or Yahoo, so options code is tested with `tools/fixtures.py`. A pull request also triggers a build-only check that deploys nothing.

## Design and wording

- The page must stay simple and easy to look at. Keep the Auto / Light / Dark switch working.
- The owner wants the Velour look (base colour `#42070E`). The tokens at the top of `template.html` are currently a blue/purple dark theme. **Do not recolour on your own;** if a task touches colours, ask first.
- Wording is educational. Keep `meta.disclaimer` in `playbook.json`. Never write "buy", "sell" or a price target.

## Where requests come from

The owner asks in chat. The Notion database "Dashboard Requests (B1)" (under the brain page `4014ea14-1469-815e-80f3-f84947d9cb1e`, data source `collection://968819b4-ad5f-4967-893b-3b982ab62bb1`) still receives ideas from the weekly Dashboard Reviewer; they are built only when the owner asks for one in chat. If a request came from a row, update its Status and notes when done.

## Agents in `.claude/agents/`

| Agent | Role | Model |
|---|---|---|
| `planner` | request -> plan.md: tier, acceptance criteria, parallel packages, checkers, human-only items | sonnet |
| `data-dev` | feeds and calculations: `update_data.py`, `options_levels.py`, `tools/fixtures.py` | sonnet |
| `dashboard-dev` | layout, charts, panels, options desk: `template.html`, `build_dashboard.py`, `export_excel.py` | sonnet |
| `playbook-editor` | wording, bands, cross-signals: `playbook.json` | sonnet |
| `data-source-checker` | failing or stale sources, replacements | sonnet |
| `qa-tester` | full gate + every acceptance criterion tested in Chromium | sonnet |
| `design-reviewer` | screenshots in dark, light and phone; layout, readability, consistency | sonnet |
| `data-auditor` | recomputes numbers, checks units, dates, honest labels, licensing, key handling | sonnet |
| `code-reviewer` | diff: rule breaks, bugs, fail-soft, size | sonnet |
| `judge` | PASS / FIX (routed fix list) / HUMAN, re-verifies key claims | opus |
| `merger` | catch up with main, CI, merge, watch publish, verify live, auto-revert | sonnet |
