# Phase 2 measurement: eight epics

Measured 2026-11-08 on branch `ai/phase2-efficiency` at commit `15b7c97`.

## Result in one line

None of the eight epics measured completed. The rollout gate cannot be shown to be met, and
the three expansion epics (which all completed in their original runs) do not support it.
Phase 3 should wait for a controlled comparison (see "Recommended next step").

## What was run

- The same five epics as the Phase 1 baseline: 090, 092, 093, 094, 095.
- The same starting snapshots (fresh single-commit clones of the Phase 1 commits `eca3e60`,
  `7c9e947`, `33528a1`, `3bc07c8`, `0d60623`, with no Phase 1 run history), the same
  sandbox rules hiding historical answers, and the same model for each role.
- One attempt per epic. No restarts, no human intervention.
- Models: planner and implementation reviewer `claude-opus-5-5`; plan reconciler
  `claude-sonnet-5-5`; plan reviewer `gpt-6.1-sol`; classifier, worker and verifier
  `gpt-6-luna`.
- Approved limits: 120 hosted calls and a $150 spending limit. A shared guard stopped new
  calls at 120 calls or $125 recorded. Neither limit was reached.

## Outcomes

| Epic | Phase 1 | Phase 2 | What Phase 2 tested |
|---|---|---|---|
| 090 | FAILED, `validation_failed` | FAILED, `worker_no_changes` | Nothing. The plan needs files under `evaluations/`, which the snapshot's scope excludes, so the worker refused. |
| 092 | FAILED, `validation_failed` | FAILED, `validation_failed` | Two validation repair rounds. The same check (`node --test tests/`, one test failing) failed throughout. Possibly the known snapshot issue below; not confirmed. |
| 093 | FAILED, `remediation_no_progress` | FAILED, `worker_no_changes` | Nothing. The repo rules forbid changing `migrations/`; the plan requires a migration. |
| 094 | ESCALATED, `plan_review_exhausted` | ESCALATED, `plan_review_exhausted` | Nothing. Stopped in plan review. |
| 095 | ESCALATED, `plan_review_exhausted` | ESCALATED, `implementation_review_exhausted` | Everything except final verification (see below). |

## Epic 095, the only full test of the new logic

- Validation (`python_unit`) failed twice and passed after the second repair round. The
  previous single-repair rule would have stopped the run here.
- Implementation review blocked three times (3, 5, then 4 findings). Both review repair
  rounds ran, each limited to the one file the findings named (`processors/providers.py`).
  The third review still had a blocking finding, so the run escalated.
- Final verification was never reached. The final-verification repair loop is covered only
  by synthetic tests, not yet by a real run.
- Each re-review raised new findings rather than confirming the fixes. The extra rounds
  spent calls without converging.

## Rollout gate

| Gate item | Phase 1 | Phase 2 |
|---|---|---|
| Completed without human intervention | 0 of 5 | 0 of 5 |
| Failures after the worker produced a change (090, 092, 093 vs 092, 095) | 3 | 2 |
| Median attempts to first completion | cannot compute (no completions) | cannot compute |
| Share of runs restarting from planning | not measured | not measured (one attempt each) |

Going from 3 to 2 late failures is not the halving the gate asks for, and five epics with
run-to-run model variation cannot show it either way. Plan review is unchanged by Phase 2,
so 095 getting past plan review this time most likely reflects model variation, not Phase 2.

## Usage

30 hosted calls (Phase 1: 29): worker 10, plan reviewer 7, classifier 6, implementation
reviewer 3, planner 2, plan reconciler 2. Recorded Claude spend $2.95. This is Claude Code's
API-equivalent estimate. Claude Code is logged in through claude.ai with no API key set.
Codex is set to ChatGPT login, but `OPENAI_API_KEY` is present in the shell environment, and
whether Codex ignores it was not confirmed.

## Known starting-condition issues

The historical plans for 090, 093 and 094 do not fit their snapshots' scope or instructions;
in the original runs a human presumably resolved this. Phase 1 also recorded an age-sensitive
test in one snapshot and a missing test directory in another. These were left unchanged so
the two phases start from the same conditions.

The 090 worker also reported that `git` failed inside its sandbox ("Apple developer tools
are missing"). The same sandbox rules were used in Phase 1, but whether Phase 1 workers hit
this was not checked.

## Next step: three more epics (approved: 8 total)

### Selection record (written before any new epic was run)

Source: this repository's own completed historical runs (the only ones with saved plans,
criteria and plain `ruff` / `pytest` validation, so no snapshot-specific scope conflicts).
An epic qualifies only if all of these hold:

1. A completed historical run (`COMPLETE_LOCAL`) with a saved request, acceptance criteria
   and approved external plan.
2. A first attempt: no seed patch, no retry, no restart, goal attempt 1. (Eliminates runs
   that started from someone's partial edit.)
3. The plan's files fall inside the snapshot's scope and no repo instruction forbids them.
4. The validation commands pass on the unmodified snapshot, checked locally with no hosted
   calls. An epic that fails this is replaced by the next one in the order below.
5. Not one of the five already measured.

Ranking among qualifying epics, applied mechanically: historical validation repair first,
then substantial tier over standard, then more acceptance criteria, then larger approved
plan, then earliest run.

Change from the first draft of these criteria: the draft required that the historical run
had used a repair. Only one unseeded first-attempt run did, so that became a ranking
preference. "Different goal families" was dropped because all five qualifying runs belong to
one proposal (`attempt-budget-failure`); they are separate tasks on separate code, each
replayed from its own base commit.

Qualifying epics in ranked order:

| Order | Historical goal | Tier | Criteria | Used a validation repair |
|---|---|---|---|---|
| 1 | attempt-budget-failure-02-dependencies | substantial | 5 | yes |
| 2 | attempt-budget-failure-07-failure-classes-limits | substantial | 4 | no |
| 3 | attempt-budget-failure-05-changed-base-restart | substantial | 4 | no |
| 4 (replacement) | attempt-budget-failure-01-tracked-preservation | standard | 4 | no |
| 5 (replacement) | attempt-budget-failure-04-minor-findings | standard | 3 | no |

Selected: orders 1, 2 and 3. Once launched, no epic is swapped. The same limits apply across
all eight epics: 120 calls and $150 in total, of which 30 calls and $2.95 are already used.

Known weakness of this choice: all three are tasks on this repository's own workflow code,
and none has a snapshot-scope conflict by construction, so results will say more about
Phase 2 than about the harder product-repository epics.

### Preparation record (before any hosted call on the new epics)

- Snapshots `d02`, `d07`, `d05` were built from the historical base commits as filtered
  single-commit trees (source, tests, docs, project config), with no run history and no later work.
- Two setup mistakes were found and fixed by the validation preflight, before any launch:
  a shared virtual environment symlink had been committed into the snapshots, and the shared
  environment's editable install pointed at the current repository, so tests that start a new
  process ran today's code against an older snapshot. Each snapshot now has a private
  environment whose install points at its own source. The failing test was confirmed to fail
  identically on the pre-Phase-2 code, so it was a setup artifact, not a Phase 2 effect.
- Preflight on the unmodified snapshots (lint, format check, full tests): d02 596 passed,
  d07 634 passed, d05 616 passed. None needed replacing.
- These three epics are tasks on this repository itself, so its history contains the real
  solutions. The sandbox for these runs hides the live checkout, the release worktree, the
  spec repository, all old run worktrees and earlier experiment folders, and the other
  snapshots. Denial was tested with the real rules: the snapshot stays readable, all of those
  are blocked.

## Expansion results (three further epics, approved: 8 total)

These three are completed historical runs on this repository (see the selection record).
Each completed in its original run with no human action, a saved external plan, and one final
verification. Final attempt for each shown; earlier invalid attempts are listed after.

| Epic (original goal) | Original run | Phase 2 replay (valid attempt) | Gates reached |
|---|---|---|---|
| d02 attempt-budget-failure-02-dependencies | COMPLETE_LOCAL, 1 validation repair | FAILED, `validation_failed` | Validation only: lint and format repaired (rounds 1 and 2), one test never repaired. |
| d05 attempt-budget-failure-05-changed-base-restart | COMPLETE_LOCAL, no repairs | ESCALATED, `verification_escalated` | Validation (1 repair), review PASS, final verification returned BLOCKED because one criterion was UNKNOWN. No verification repair was attempted. |
| d07 attempt-budget-failure-07-failure-classes-limits | COMPLETE_LOCAL, no repairs | ESCALATED, `verification_exhausted` | All four gates. Validation 2 of 2 rounds, final verification 2 of 2 rounds; the same criterion failed in all three verifications. |

### What d07 shows (the first complete run of the new loop)

- A rejected final verification went back to the worker in the same worktree, then back
  through validation, review and verification, twice, as designed.
- Budgets were independent: a verification repair that broke lint and formatting was repaired
  from validation's own budget (validation 2 of 2), not verification's.
- Review findings that were not blocking did not trigger remediation (review budget unused).
- The loop did not converge: the same criterion ("product, non-product, repeated-signature,
  cancellation, pause/resume, and governance behavior matches the proposal") failed each time,
  and the run escalated with the correct stop record.

### What d02 shows

Its remaining failing test (`test_hosted_wakeup_in_new_process_uses_restored_worktree_path`)
fails when the worker's saved change is applied to a clean copy, under either file-permission
setting, and passes on the unmodified baseline. It is a real defect in the worker's change.
Note that d02 ran without the proposal document (see Corrections).

## Comparison with the original runs

All three expansion epics completed in their original runs (3 of 3, one verification each,
no repairs beyond one validation repair in d02). In the replays 0 of 3 completed. This is
not evidence of a Phase 2 regression by itself:

- The original runs used earlier code, ran in the live repository with its full documents,
  and were tasks written after earlier failed attempts, so they were already tuned.
- The replays run in a restricted sandbox with answers hidden and the repository history
  removed. d02 and d05 also lacked the proposal document the plans cite.
- The model varies run to run. One sample each cannot separate these effects.

It also does not show Phase 2 helps. The only way to separate the effect of Phase 2 from the
harness is to run the pre-Phase-2 code (`ff6a49b`) through the same harness on the same
snapshots.

## Corrections and harness faults found during the measurement

These are listed so the results are not over- or under-read.

- **Missing proposal document (my preparation miss).** All three plans cite
  `docs/proposals/2026-10-12-attempt-budget-failure-classes.md`, which is git-ignored and was
  not in any snapshot. d07's first attempt was blocked in plan review for that reason. The
  file is unedited since before the original runs and contains no results. d07 was rerun with
  it supplied. d02 and d05b ran without it.
- **Hidden folder broke the Claude CLI (my mistake).** Hiding `/private/tmp/claude-501` made
  every Claude call fail. d05's first attempt (validation passed after 2 repairs, then the
  reviewer crashed) is invalid at review. Fixed by hiding only my own session folder, and
  verified with a real call. d05b reran under the fix.
- **File-permission setting from the Phase 1 launcher (umask 077).** On the unmodified d07
  baseline, `test_seed_patch_binary_and_executable_changes_reach_worker` fails under `umask
  077` and passes under the normal setting. d07's second attempt failed validation only
  because of it, so it is invalid at the final validation. The third attempt used a normal
  umask. The original five epics ran under `umask 077`, as in Phase 1, so both phases are
  equally affected; whether it contributed to 092's single failing `node` test was not tested.
- **Shared environment.** Earlier setup errors (a committed symlink, and an environment
  whose install pointed at the current repository) were caught by the preflight before any
  hosted call and fixed with private per-snapshot environments.

### All attempts on record

| Attempt | Result | Valid for Phase 2? |
|---|---|---|
| 090, 092, 093, 094, 095 | see first table | yes (limits noted above) |
| d02 | FAILED, validation | yes |
| d05 (first) | ESCALATED, environment (my sandbox mistake) | validation yes, review no |
| d05b | ESCALATED, `verification_escalated` | yes |
| d07 (first) | BLOCKED, plan review (missing proposal) | no |
| d07b | aborted by me after 1 call (sandbox mistake) | no |
| d07c | FAILED, validation (umask) | repairs of lint and format yes; final validation no |
| d07d | ESCALATED, `verification_exhausted` | yes |

## Usage

74 recorded hosted calls (limit 120) and $5.05 of recorded Claude spend (limit $150). Of the
calls, 5 were small test calls made by hand while checking the sandbox, and 1 belongs to the
aborted d07b. The dollar figure is Claude Code's API-equivalent estimate; Claude Code is
logged in through claude.ai. Whether Codex ignores `OPENAI_API_KEY` in this environment was
not confirmed.

## Design observations for Phase 3 (not changed during the measurement)

1. **A verifier outcome of BLOCKED ends the run without a repair.** In d05b the verifier
   returned BLOCKED because one criterion was UNKNOWN (not demonstrated), and the framework
   itself classified that as an evidence failure. A repair, such as adding a test that proves
   the criterion, was never tried. Treating BLOCKED with only UNKNOWN verdicts as repairable
   is a judgment call worth deciding.
2. **Extra review rounds did not converge in 095.** Each re-review raised new findings, so the
   second round spent calls without progress.
3. **Verification verdicts name no files**, so verification repairs are limited only by the
   approved scope. Only review findings that name a file get a named-file limit.
4. **Failed repairs are not retried differently.** In d07 the same criterion failed three
   times with the same kind of repair. A repair that sees its previous attempt's result could
   differ.

## Recommended next step

Run the pre-Phase-2 code (`ff6a49b`) through the same harness on the same three expansion
snapshots, with the proposal supplied and a normal umask, then compare gate by gate. That is
the only comparison that separates Phase 2 from the harness and from model variation. It
needs fresh approval: roughly 45 calls, which would bring the total past the 120-call limit
(74 used), so it needs the limit raised or a smaller scope (for example d07 only, about 15
calls).
