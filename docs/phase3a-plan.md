# Phase 3a: let the worker repair "not demonstrated" verdicts

## Goal

Sometimes final verification rejects a change because a requirement was not demonstrated
(verdict UNKNOWN), not because it failed. When the missing piece is a test, the run should
send the change back to the worker to add that test instead of stopping. Today the run
stops if the verifier labels its overall answer BLOCKED. It gets a repair if the verifier
says CHANGES_REQUIRED instead. Saved runs show the verifier picks between those two labels
inconsistently.

Decisions already made (2026-11-08):
- Evidence repairs share the final-verification repair budget, which rises from 2 rounds to 3.
- An evidence repair may change product code inside the approved scope when a new test
  exposes a defect.

## Changes

1. **Verifier answer** (`src/dev_orchestration/domain/findings.py`,
   `src/dev_orchestration/roles/verifier.md`). Each criterion's verdict also says what is
   missing: a test, a command run, a file the verifier could not see, a fact from the run
   record, something else, or nothing. A PASS that relies on a test must name the test.
2. **Routing by verdicts, not the label** (`workflow/runner.py`, `workflow/recovery.py`).
   - Any FAIL verdict: the existing repair, as now.
   - Only UNKNOWN verdicts, all missing a test: an evidence repair.
   - Any other kind of missing evidence: stop and escalate, as now, until Phase 3b.
   - The ESCALATE label still stops the run.
3. **Evidence repair instruction** (`workflow/stages.py`, `roles/implementation_worker.md`).
   The worker adds tests that demonstrate each listed requirement. It changes product
   code only where a new test exposes a defect, and only within the approved scope. Then
   the change goes back through validation, review and verification, like any repair.
4. **Progress check.** An evidence repair that changes no test file counts as no progress
   and stops the run with `remediation_no_progress`.
5. **Budget** (`domain/run.py`, `artifacts/store.py`). `MAX_VERIFICATION_REPAIRS` goes from
   2 to 3, and the stop record reports 3. Restarted runs still get none.
6. **Older saved answers.** Verifier answers saved before this change have no "missing"
   field. They load normally, and their UNKNOWN verdicts escalate, as they do today.
7. **Tests** in `tests/workflow/test_phase3a.py` (below).

## Done when

- `.venv/bin/python -m pytest -p no:cacheprovider tests/workflow/test_phase3a.py` passes
  and covers:
  - BLOCKED with only "missing a test" UNKNOWN verdicts: one evidence repair, then
    COMPLETE_LOCAL;
  - the same verdicts labelled CHANGES_REQUIRED: the same path;
  - UNKNOWN missing a command run: ESCALATED `verification_escalated`, no worker call;
  - an evidence repair that changes no test file: `remediation_no_progress`;
  - a third verification repair runs; a fourth is refused with `verification_exhausted`;
  - resuming a run saved before this change loads its verifier answer without error.
- `.venv/bin/ruff check --no-cache src tests`,
  `.venv/bin/ruff format --check --no-cache src tests` and
  `.venv/bin/python -m pytest -p no:cacheprovider` all exit 0.
- Measurement, which needs separate approval (about 15 hosted calls): a fresh d05 replay
  reaches an evidence repair instead of stopping at its first final verification.

## Risks

- **Weak tests.** A trivial test could turn UNKNOWN into PASS. The implementation reviewer
  still reviews the new tests, and the verifier must name the test behind any PASS.
