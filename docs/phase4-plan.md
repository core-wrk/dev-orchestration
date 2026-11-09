# Phase 4: close the verification and restart gaps

Depends on Phases 3a and 3b.

## Goal

Final verification decides on review findings by rule, not by how the verifier words its answer.
The evidence:

- **An important finding shipped unnoticed** (AO-03b): the verifier answered PASS without listing
  it, and such a PASS is accepted.
- **Minor findings forced a repair** (i07): the verifier failed a broad criterion over two minor
  findings, spending the last repair round.
- **Restarts cannot use the no-worker ask**, so any rejection fails them.
- **Earlier verifier answers are overwritten**, so their reasons are lost (i02, i07).
- **The worker's report misleads the verifier** (i02): its sandbox has no network, so it can show
  a test failure validation did not have.

## Changes

1. **Every finding gets a disposition** (`domain/findings.py`, `roles/verifier.md`). For each
   supplied review finding the verifier answers `resolved`, with evidence, or `unresolved`. Leaving
   one out gets the one corrective retry. An unresolved finding that is not minor goes to the
   findings repair, even under a PASS.
2. **Minor findings never fail a criterion** (`domain/findings.py`, `workflow/contract.py`). A
   non-passing verdict names the finding IDs it rests on; one resting only on minor findings counts
   as a pass with a note.
3. **Restarts may ask once more** (`workflow/recovery.py`), when the rejection wants only a command
   result, run fact or file. Restarts still get no worker repair.
4. **Keep every verifier answer** (`workflow/stages.py`) as
   `verification/final-verification-v<n>.json`; `final-verification.json` stays the latest.
5. **Label the worker's report** (`workflow/stages.py`) as its own account from a sandbox without
   network, not validation evidence.
6. **Tests** in `tests/workflow/test_phase4.py`.

Answers saved before this phase must still load and route as today.

## Done when

- `.venv/bin/python -m pytest -p no:cacheprovider tests/workflow/test_phase4.py` passes and covers:
  - PASS omitting an important finding's disposition: retried once, then escalates;
  - PASS with an important finding unresolved: a findings repair;
  - FAIL citing only minor findings: completes with a note, no worker call;
  - FAIL citing an important finding, or none: a product repair, as today;
  - check-only restart whose UNKNOWN wants a file: one extra ask, then completes;
  - restart with minor-only notes completes (proposal item 4);
  - two verifications leave `final-verification-v1.json` and `-v2.json`;
  - an answer saved before this phase loads and routes the same way.
- `.venv/bin/ruff check --no-cache src tests`, `.venv/bin/ruff format --check --no-cache src tests`
  and `.venv/bin/python -m pytest -p no:cacheprovider` exit 0.
- Measurement, needing separate approval: one d07 replay (about 12 of the 17 remaining calls).

## Risks

- **A heavier verifier answer** may mean more schema retries. If they rise, drop step 2's cited
  findings and keep the dispositions.

## Not included (separate decisions)

- Repairing important findings at review instead of verification: saves a verifier call per round,
  but reverses the Phase 1 rule that only blocking findings trigger review repair.
- Restarting onto a main that changed the patch's files: the proposal (item 5) requires refusal.
