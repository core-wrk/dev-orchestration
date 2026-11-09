# Phase 4: close the verification and restart gaps

Depends on Phases 3a and 3b.

## Goal

Handle review findings by rule, at the earliest step that can. Evidence:

- **Important findings wait for verification** (i02, i07, h05): only `blocking` ones trigger
  review repair, so each costs a verifier call and a verification round.
- **An important finding shipped unnoticed** (AO-03b): the verifier answered PASS without listing it.
- **Minor findings forced a repair** (i07): the verifier failed a broad criterion over two of them.
- **Restarts cannot use the no-worker ask**, so any rejection fails them.
- **Earlier verifier answers are overwritten** (i02, i07).
- **The worker's report misled the verifier** (i02): its sandbox has no network.

## Changes

1. **Repair important findings at review** (`workflow/contract.py`, `runner.py`, `recovery.py`).
   Review repair also takes `important` findings that name a file, limited to those files, within
   the existing two review rounds, whatever the review's own label.
2. **Every finding gets a disposition** (`domain/findings.py`, `roles/verifier.md`). For each
   finding the verifier answers `resolved`, with evidence, or `unresolved`; omitting one gets the
   corrective retry. An unresolved finding that is not minor goes to the findings
   repair, even under a PASS.
3. **Minor findings never fail a criterion** (`domain/findings.py`, `workflow/contract.py`). A
   non-passing verdict names the findings it rests on; one resting only on minor findings counts as
   a pass with a note.
4. **Restarts may ask once more** (`workflow/recovery.py`), when the rejection wants only a command
   result, run fact or file. Restarts still get no worker repair.
5. **Keep every verifier answer** (`workflow/stages.py`) as `final-verification-v<n>.json`;
   `final-verification.json` stays the latest.
6. **Label the worker's report** (`workflow/stages.py`) as its own account from a sandbox without
   network, not validation evidence.
7. **Tests** in `tests/workflow/test_phase4.py`.

Older saved answers must load and route as today.

## Done when

- `.venv/bin/python -m pytest -p no:cacheprovider tests/workflow/test_phase4.py` passes and covers:
  - review PASS with an important finding naming a file: one review repair limited to that file,
    before any verifier call; naming no file, or after two rounds: no review repair;
  - PASS omitting an important finding's disposition: retried once, then escalates;
  - PASS with an important finding unresolved: a findings repair;
  - FAIL citing only minor findings: completes with a note, no worker call;
  - check-only restart whose UNKNOWN wants a file: one extra ask;
  - restart with minor-only notes completes (proposal item 4);
  - two verifications leave `final-verification-v1.json` and `-v2.json`;
  - an answer saved before this phase loads and routes the same way.
- `.venv/bin/ruff check --no-cache src tests`, `.venv/bin/ruff format --check --no-cache src tests`
  and `.venv/bin/python -m pytest -p no:cacheprovider` exit 0.
- Measurement, needing approval: one d07 replay (about 12 of 17 remaining calls).

## Risks

- **More review rounds.** Repairs can surface new findings (i07: 3, 1, 2, then 0); the two-round
  limit bounds it.
- **A heavier verifier answer** may raise schema retries; if so, drop step 3's cited findings.

## Not included

Restarting onto a main that changed the patch's files (proposal item 5 requires refusal).
