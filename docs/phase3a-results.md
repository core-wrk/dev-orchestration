# Phase 3a measurement: d05 replay

Measured 2026-11-08 on branch `ai/phase3a-evidence-repair` at commit `7f83c53`, one attempt.

## Setup

A fresh copy of the d05 snapshot, built the same way as the Phase 2 attempt d05b: starting
commit `1873300`, the proposal document supplied, and the same plan, criteria, models,
sandbox and umask 022. The launcher confirmed it loaded the Phase 3a code (3 verification
rounds, routing by verdicts). Earlier snapshots and worktrees were hidden from agents.
It used 6 hosted calls and $0.53 recorded (running total 97 of 170 calls, $7.06).

## Result

FAILED at its first final verification, with no repair. **The evidence repair path was not
exercised**: this time no criterion came back UNKNOWN.

- Validation needed 1 repair round (of 2), and implementation review returned PASS with one
  `important` finding (F001: older restart runs may fail checkpoint checks after this change).
- The verifier passed all four criteria, including criterion 2. That was UNKNOWN in Phase 2
  (d05b) and FAIL in the pre-Phase-2 control (c05), so the same criterion got three
  different verdicts across three runs.
- It still answered CHANGES_REQUIRED, citing F001 as unresolved.

The run stopped as `verification_rejected` (cause `other`) for two separate reasons, each
enough on its own:

1. **The verifier rewrote a criterion.** It copied criterion 4 without its backticks. The
   orchestrator compares criteria text exactly, so the answer counted as not judging the
   supplied criteria.
2. **An all-PASS rejection has no repair path.** A rejection that rests only on an
   unresolved review finding is not repaired, even when the finding names a file. The
   finding was `important`, not `minor`, and only minor findings may be noted on a passing
   answer.

## How common the first problem is

Of 51 saved final verifications from earlier runs, 8 (16%) had criteria text that did not
match the saved criteria exactly, and all 8 runs ended FAILED with cause `other`. Two of
those 8 verifier answers were PASS. The differences:
- 3 were formatting only (backticks dropped, one space added);
- 5 were rewording, splitting or merging of criteria.

## What this suggests (not decided)

- Have verdicts refer to criteria by position or ID instead of copying their text, so the
  orchestrator never compares the verifier's copy. This removes all 8 historical cases,
  not only the formatting ones.
- Give a rejection that rests only on unresolved review findings the review-repair path,
  limited to the files those findings name.
- Phase 3a still needs a run that actually returns an UNKNOWN verdict before it can be
  called measured. Given how much the verifier varies, one replay per change cannot show
  it reliably.

## Second replay (f05): criterion IDs and findings repair

Measured 2026-11-08 at commit `d449010`, one attempt. Same setup as e05 (start commit `1873300`,
proposal supplied, same plan and criteria). It used 11 hosted calls and $0.97 recorded (running
total 108 of 170 calls, $8.03).

Result: FAILED `validation_failed`, after 2 of 3 verification repair rounds. It got further than
any earlier d05 attempt.

1. **Criterion IDs worked.** Both verifier answers were accepted, with no text mismatch.
2. **Evidence repair ran for the first time on a live run.** The first verification marked
   criteria 2 and 3 UNKNOWN, missing a test. The worker added tests. After that, the second
   verification passed all four criteria.
3. **That second verification was still rejected, on a technicality.** It answered PASS with
   notes on two `minor` findings (F004, F005). The completion rule accepts notes on minor
   findings only with a CHANGES_REQUIRED outcome, not with PASS. The new findings route then sent
   the two minor findings to a repair limited to `restart.py`. Minor findings should never force
   a repair.
4. **The repair left a formatting error, and validation had no repairs left.** Three of five
   validation runs failed `ruff format` after a worker edit. The validation budget of 2 is
   counted per run, not per round, so the formatting failure after round 2 ended the run.

Had the second verification been accepted, the run would have completed: all criteria passed,
validation passed, and only minor findings remained.

What this suggests (not decided):
- Accept a PASS whose notes cover only minor findings, as CHANGES_REQUIRED already is. Send only
  non-minor findings to a findings repair.
- Stop formatting from using up repair rounds. Either the worker runs the project's format
  command before finishing, or a failure that is only formatting gets its own allowance.
