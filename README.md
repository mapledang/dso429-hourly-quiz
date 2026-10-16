# DSO 429 Homework 3: Hourly Quiz pipeline

Automated submitter for "The Hourly Quiz". It runs entirely on GitHub-hosted Actions runners,
so no laptop needs to be on. Answers are committed to `answers/round-NNNN.json` on `main`.

## How it works

| Workflow | Trigger | What it does |
|---|---|---|
| `poll.yml` | every 10 min (`3-59/10 * * * *` UTC) + manual | Fetch `questions.csv`, reconcile **every** released round against `answers/` on remote `main`, solve the missing ones, validate, commit, push, re-fetch and verify byte-for-byte. |
| `reconcile.yml` | hourly at :37 UTC + manual | Same reconciliation (an independent second schedule), a watchdog that fails if `poll` hasn't succeeded in 45 min, and the full rounds 1–168 audit, uploaded as an artifact. |
| `keepalive.yml` | self-chaining (+ cron restarter every 2h) | **Doesn't depend on cron.** Queues its own successor first, then dispatches `poll` every 10 min for about 5.5h. GitHub's cron fired **no** runs in this repo's first hour; this chain is what guarantees cadence. Stops itself after Oct 23 02:00 UTC. To stop it early: Actions → keepalive → cancel the running and pending runs. |
| `llm-check.yml` | manual only | Scores the optional Claude fallback on 24 practice questions against the keys; never commits. |
| `recover.yml` | manual only | Reconcile specific rounds, e.g. `49-52,60`. Defaults to filling only missing/invalid files. |
| `ci.yml` | code pushes | Unit/integration tests, the 1,200-question practice evaluation (fails below 99%), and a live dry run. |

All three writers share the concurrency group `quiz-submit`, so only one pushes at a time.
Pushes made with `GITHUB_TOKEN` never trigger other workflows, so there are no loops. Nothing force-pushes.

**Schedule interpretation.** Round 49 releases at 2:00 PM PDT on Sat Oct 17 (21:00 UTC). Round 168
releases at 1:00 PM PDT on Thu Oct 22 (20:00 UTC). PDT (UTC−7) applies to the whole window; the
code uses `zoneinfo`, not a fixed offset. The poller fires at :03, :13, :23, :33, :43 and :53 UTC (the
same minutes in Pacific), after the source's ~5-minute publication lag. GitHub may delay or drop
scheduled events. A round keeps full timing credit for **3 hours**, so the pipeline has about 18
poller chances plus 3 reconcile chances per round.

**Solving.** Deterministic template solvers cover all 12 question families seen in practice:
compound interest, discount, revenue, FX conversion, weighted average, gross margin, break-even,
probability, % change, ratio split, sequences and date settlement. There are also extra variants
(compounding frequencies, simple interest, markup, tax, geometric sequences, N-way ratios). A
computed value counts as a match only when exactly one option equals it at that option's displayed
precision.

If no template applies and a Claude credential secret exists (`CLAUDE_CODE_OAUTH_TOKEN` for your
subscription, or `ANTHROPIC_API_KEY`), Claude (`claude-opus-5-5`) is called with structured JSON
output. The model must also return an arithmetic expression, which is
evaluated by a restricted AST walker (never `eval`) and matched independently. If neither works,
a flagged heuristic guess is submitted. It scores 38% on practice versus 25% for random. The run
then exits non-zero so GitHub emails you, and the answer may be corrected within the 3-hour window.

**Submission policy** (it implements the assignment's "which version counts" rule):
- A missing or invalid file is (re)submitted immediately, at any time.
- A valid file is revised only if all of these hold: this pipeline wrote it (the manifest records
  its hash, so hand-edited files are never touched), a question was flagged or the source text
  changed, the new answer is confident, and it is still more than 20 minutes before the 3-hour
  cutoff. After that, files are locked.
- An incomplete round (fewer than 25 valid questions) is held and retried. If it is still
  incomplete 2 hours after release, it is submitted with flagged guesses for the missing questions,
  rather than scoring zero.

**State.** `state/manifest.json` records the last source check, last submission, last verification,
per-round status, answer hash, flags, retries and recent errors. `state/rounds/round-NNNN.json`
records the method, computed value and status for every question. Both are bookkeeping only; the
`answers/` files on remote `main` are authoritative.

## Setup (one time)

1. Create a **public** repo and push this folder to `main` (see "Deploy" below).
2. Settings → Actions → General: Actions enabled. The workflows request `contents: write`
   explicitly. If "Workflow permissions" is locked to read-only by a policy, set it to "Read and write".
3. Optional Claude fallback. Pick one; each is capped at 60 calls per run (`QUIZ_LLM_MAX_CALLS`).
   - **Claude subscription (no API key):** run `claude setup-token`, then
     `gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo <user>/<repo>` and paste the token. The runner
     installs Claude Code 2.1.286 and calls `claude -p` with all tools, MCP servers and settings
     disabled. This uses your subscription's usage limits.
   - **API key:** `gh secret set ANTHROPIC_API_KEY --repo <user>/<repo>`.
   - Prove it works: Actions → `llm-check` → Run workflow. It scores 24 practice questions sent
     straight to Claude against the keys and never commits.
4. GitHub → Settings → Notifications → Actions: enable failure emails.
5. Submit `https://github.com/<user>/<repo>` to Brightspace "Homework 3 GitHub Repo Link" by
   **Fri Oct 16, 11:59 PM PT**.

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt -r requirements.txt
.venv/bin/python -m pytest -q                                  # test suite
.venv/bin/python -m quizbot evaluate                           # practice accuracy -> reports/practice_eval.json
.venv/bin/python -m quizbot run --mode dry-run --out /tmp/out  # solve live source, no git
.venv/bin/python -m quizbot run --mode practice --out /tmp/p   # solve 1-48 and check against official keys
.venv/bin/python -m quizbot audit --repo <user>/<repo> --course-root ".."   # final 1-168 audit
```

`--mode production` refuses to run outside GitHub Actions unless you pass `--allow-local-production`.

## Testing

See [TESTING.md](TESTING.md): 370 tests tagged with 211 requirement IDs. Each run generates a
traceability matrix (`reports/TEST_REPORT.md`) and JUnit XML.

## Monitoring and recovery

- **Is it working?** In the Actions tab, every run's summary lists the rounds found, solved,
  pushed and verified. Red runs mean an error (exit 1) or a flagged answer (exit 3). Each hourly
  `reconcile` run uploads `audit.json`.
- **Missed rounds:** nothing to do. Every run reconciles all rounds. To force it, use Actions →
  poll → Run workflow.
- **Specific rounds:** Actions → recover → rounds `49-52`.
- **Flagged answer (exit 3):** open `state/rounds/round-NNNN.json`, then fix the answer by hand
  within 3 hours of release if needed. Hand-edited files are never overwritten.
- **Scheduler stalled:** the `reconcile` watchdog fails and emails you. Run `poll` manually.

## Deadline checklist

- [ ] Fri Oct 16, 11:59 PM PT: repo URL submitted to Brightspace "Homework 3 GitHub Repo Link"
- [ ] Practice rounds 1–48 in `answers/` (the first production run does this)
- [ ] Oct 17–22: watch for red runs; the audit shows rounds 49–168 all `submitted`
- [ ] Thu Oct 22 **after 2:00 PM PT**: from the course root run
      `python3 .claude/weekly_report.py --days-back 7` (or `/weekly-report`), then upload
      **only** `usage_reports/<date>.json` to Brightspace "Homework 3". Keep the transcript locally.
- [ ] Keep the repo public, same name and owner, until grades are released
