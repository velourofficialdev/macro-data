---
name: team
description: Run any Macro Dashboard change through the agent team - plan, parallel builders, parallel checkers (QA, design, data, code), judge, auto-merge with publish watch and auto-revert. Use for every change request to this repo ("add X", "fix Y", "change the wording", "/team ..."), and when the owner says "merge" on an open PR.
---

# /team: how the lead runs a change

You are the **lead**. You coordinate; the agents do the work. The owner only wants to hear about things that need a human. Keep your own context small: never read big files yourself, read the agents' report files (`$TEAM_DIR/*.md`) and their short replies.

Standing permission given by the owner in chat to the lead on 2026-10-23: agents do everything except truly human tasks; a PR is auto-merged after judge PASS on its head and auto-reverted if the live page breaks. Requests come through chat. Everything in `CLAUDE.md` hard rules still applies (no workflows, no secrets, no build output, velourofficialdev only).

## 0. Set up (once per request)

```
RUN=$(date -u +%Y%m%d-%H%M)-<slug>
TEAM_DIR=<repo>/.team/$RUN        # absolute path; .team/ is gitignored; every agent gets this path
git fetch origin main && git checkout -b team/<slug> origin/main
```
Pass `TEAM_DIR`, the branch name and the repo path in every agent prompt. Each Bash call starts a fresh shell, so tell agents to run the tools as `TEAM_DIR=<path> python tools/check.py --out <path>/<agent>-check`: the gate refuses to delete an output folder outside `/tmp` or `$TEAM_DIR`. Agents write their full report to `$TEAM_DIR/<agent>.md` and reply in at most 6 lines.

## 1. Plan

- **Tier S** (wording, one threshold, a one-line fix): skip the planner. Write `$TEAM_DIR/plan.md` yourself in 5 lines (request, tier, criteria, package, checkers: qa-tester + code-reviewer).
- **Tier M/L**: run `planner`. Read its 6-line reply. If it lists human-only items that block the work (intent unclear, money, licensing), ask the owner now with AskUserQuestion, one round, concrete options. Do not ask about anything an agent can decide.

## 2. Build in parallel

Check out `team/<slug>` in your directory first. Then launch **one Agent call per package in the same message**, each with `isolation: "worktree"` and `run_in_background: true`:
- prompt: the package text from the plan, `TEAM_DIR`, "create branch `pkg/<slug>-<n>` from `team/<slug>`, commit there, do not push", and the attribution lines.
- owners: `data-dev`, `dashboard-dev`, `playbook-editor`, `data-source-checker`.
Packages never share a file (the planner guarantees it). Wait for all notifications. Do not poll.

## 3. Integrate

Merge each `pkg/*` branch into `team/<slug>` (they live in the same repository). On a conflict, do not hand-edit big code: send the conflicting package's owner a fix prompt. Then push, set upstream tracking (`git branch -u origin/team/<slug>`), and open the PR through REST:
`gh api repos/velourofficialdev/macro-data/pulls -f title=... -f head=team/<slug> -f base=main -F body=@$TEAM_DIR/pr.md`
The PR body: what changed in plain words, the acceptance criteria, the human-only follow-ups, and the attribution footer.

## 4. Check in parallel

Checker areas (the single source for which reports a change makes stale):

| Checker | Area: re-run when the change touches |
|---|---|
| `qa-tester` | anything (always re-run after any fix) |
| `code-reviewer` | anything outside `data/` and `docs/` (all code, `tools/`, `.claude/`, `CLAUDE.md`, `.gitignore`) |
| `design-reviewer` | `template.html`, `build_dashboard.py`, `playbook.json`, `tools/shots.py`, any CSS or JS |
| `data-auditor` | `update_data.py`, `options_levels.py`, `fed_rate_odds.py`, `export_excel.py`, `build_dashboard.py`, `playbook.json`, `calendar.json`, any `data/README*` |


In **one message**, launch every checker the plan names, all `run_in_background: true`, all on the PR branch's current head (tell each the sha):
- `qa-tester` (always), `code-reviewer` (always), `design-reviewer` (any page change), `data-auditor` (data, calculations, labels or sources changed).
Checkers that need their own checkout use `isolation: "worktree"` and `git checkout team/<slug>`. Each writes its gate output to `$TEAM_DIR/<checker>-check`. Wait for all of them.

## 5. Judge

Run `judge`, and pass `round: <n>` (1 for the first verdict) in its prompt. Then:
- **PASS** go to step 6.
- **FIX**: launch the named owners in parallel with the judge's fix items (same worktree pattern, branch `fix/<slug>-r<round>-<n>` from `team/<slug>`); items owned by `lead` (instruction files) you edit yourself meanwhile. Integrate and push, then compute `git diff --name-only <old head> <new head>` and re-run `qa-tester` plus every planned checker whose area (table in step 4) that list touches, not only the ones the fix list names. Then the judge again. At most 2 FIX rounds; the third non-PASS verdict is HUMAN. A FIX verdict whose only items are re-runs of stale reports does not count as a round.
- **HUMAN**: ask the owner the judge's one question (AskUserQuestion), then continue from the step it affects.

## 6. Merge and watch

Run `merger` with the judge's head sha. It merges exactly that head, watches the publish run, checks the live page and reverts on its own if needed. If it reports "needs builder", route the conflict to the owning builder. If it reports "needs quick re-judge" or "head moved", run the judge on the new head (the area rule keeps reports valid when only `data/`/`docs/` changed, so this is cheap), then the merger again.

## 7. Tell the owner

If the request came from a Notion row, update its Status and notes. Then tell the owner in at most 6 lines: what is now live (one line per visible change), the PR and merge links, anything reverted and why, and the human-only follow-ups as numbered steps (exact secret name, exact workflow line, exact email to send). Attach one or two screenshots from `$TEAM_DIR/shots` with SendUserFile when the change is visible. No step-by-step recap.

## When the owner says "merge" on an existing PR

Treat the PR as already built: set up `TEAM_DIR`, write a 5-line plan from the PR body, run step 4 (all four checkers), then 5 and 6.

## Lessons already learned here (do not relearn them)

- The sandbox cannot reach Cboe or Yahoo; FRED and GitHub work. Options code is tested with `tools/fixtures.py`; the first scheduled run (weekdays 22:30 UTC, about 06:30 Taipei) is the live test. A merge triggers an immediate run, which during US hours captures intraday data.
- `main` gets auto-update commits every weekday and other sessions merge too; always merge `origin/main` before merging a PR.
- The stop hook wants an upstream for every pushed branch: always `git branch -u origin/<branch>` after the first push.
- GraphQL is blocked; `gh api` REST works (`pulls`, `pulls/<n>/merge`, `actions/runs`).
- Cboe's terms say showing their data needs permission; it is a standing human-only follow-up, not a blocker.
- Bucket rounding: `np.floor(x / step + 0.5) * step`. Stats rows need `flex: none`. Labels must not overstate the data.
