# Phase 3b measurement: d05 replay (h05)

Measured 2026-11-09 at commit `cf5e7dd`, one attempt. Same setup as e05, f05 and g05: start
commit `1873300`, proposal supplied, same plan, criteria, models, sandbox and umask 022. It
used 10 hosted calls and $0.69 recorded (running total 129 of 170 calls, $9.55).

## Result

**COMPLETE_LOCAL**, with final commit `1e1367f` in the h05 copy (4 files, +153/-7, including
91 lines of new tests). This is the first d05 attempt to complete, out of six on record
(d05, d05b, c05, e05, f05, g05 all stopped earlier).

How it got there:

1. Validation needed 2 repair rounds (formatting, then one failing test) before passing.
2. Implementation review passed with one `important` finding (F001: older restart runs could
   fail checkpoint checks) and two minor ones.
3. The first final verification passed all four criteria and rejected the change only over
   F001. The **findings repair** (from `d449010`) gave the worker F001, limited to
   `checkpoints.py`. The worker fixed it there.
4. Validation passed with no repair. The second review found two minor findings (F004, F005).
5. The second final verification answered PASS with notes on F004 and F005. The **minor-notes
   rule** (from `449e69a`) accepted it. Before that change, this answer was rejected (f05).

What was not exercised: no criterion came back UNKNOWN, so the Phase 3b "ask once more" path
and the several-missing-kinds routing did not run. No commands were quoted beyond the
registered ones. The verifier did receive run facts and the instruction to read files; whether
that is why no UNKNOWN appeared cannot be told from one run.

## Finding: the worker cannot run the validation commands

The worker's findings-repair report says it could not run the self-check: `.venv/bin/ruff` and
`.venv/bin/python` were absent, and `git` failed. The `.venv` link exists in the worktree only
while the orchestrator runs validation, and the replay sandbox has no Xcode command-line tools.
This explains why formatting failed after almost every worker change across e05 to h05: the
worker is told to run the format check but cannot. Worth fixing: keep the validation
environment available to the worker during its call, or run the project's formatter after
the worker returns.

## Left open in the delivered change (minor findings, accepted)

- F004: a patch conflict is refused only after a child run has been created.
- F005: `restart_status` reports a changed-base restart as unavailable even when a validation
  restart would be allowed.

## Fix: the worker's validation environment (2026-11-09)

Two causes, both confirmed without any hosted call by running the worker's self-check commands
in h05's real run worktree, under the replay's exact Codex sandbox settings (`codex sandbox`):

1. **Product (commit after `7e78824`).** The `.venv` link existed in the worktree only while the
   orchestrator ran validation. Every worker call now runs with the project's
   `validation_links` linked, and the link is removed before the fence checks.
2. **Replay harness only.** The replay sandbox gave the worker `:minimal` read access, so the
   venv's Homebrew Python and Apple's git could not start; a normal run has full disk read.
   The new launcher `replay_3e.py` adds read-only access to `/opt/homebrew`, the Xcode
   command-line tools and their selection settings, and the epic's own `.venv` and `.git`,
   and sets `GIT_CONFIG_GLOBAL=/dev/null` for the worker.

| Self-check command in the worker sandbox | Before | After |
|---|---|---|
| `.venv/bin/ruff check` (changed files) | not found | passed |
| `.venv/bin/ruff format --check` | not found | passed |
| `.venv/bin/python` | not found | 3.14.6 |
| `.venv/bin/python -m pytest tests/domain` | not found | 23 passed |
| `git status`, `git diff` | failed (developer tools unreadable) | exit 0 |

The link was gone after the stage in both cases. pytest imports the worktree's own code
(`pythonpath = ["src"]`), not the base checkout's.

## d02 and d07 replays with the worker environment fixed (i02, i07)

Measured 2026-11-09 at commit `99dc8f6` with launcher `replay_3e.py`, run in series. Setups match
the earlier attempts: i02 = d02 (start `726605b`, no proposal), i07 = d07d (start `17635ba`,
proposal supplied). Total 22 hosted calls and $2.81 (running total 153 of 170 calls, $12.36).

| Run | Result | Validation repairs | Verification rounds | Extra asks | Calls |
|---|---|---|---|---|---|
| i02 | COMPLETE_LOCAL (`c209f9f`) | 1 | 1 (findings) | 1 | 10 |
| i07 | COMPLETE_LOCAL (`770afe4`) | 0 | 3 of 3 (product) | 0 | 12 |

With d05 (h05), all three expansion epics have now completed on the current code.

**i02.** Validation passed first time. An important finding (pydantic version floor) went to a
findings repair limited to `config/models.py`. After one validation repair (a failing test), the
verifier marked the "all commands exit 0" criterion UNKNOWN; the extra ask, with no worker call,
returned PASS. The worker's own sandbox has no network, so its report said "605 passed, 1 failed"
(a release test that downloads a build tool); that report reaches the verifier as a run fact and is
the likely cause of the UNKNOWN.

**i07.** Every validation passed first time. Criterion 2 ("behavior matches the proposal") failed in
three rounds, each time over the open review findings: F001-F003, then F004, then only the minor
F005 and F006. The third repair fixed F005 alone. Each review found new issues in the code just
changed (3, 1, 2, then 0 findings), and the run completed on its last allowed round.

What these show:
- The worker environment fix holds: 1 validation repair across both runs, against 2-3 per run before.
- The verifier can turn minor findings into a criterion FAIL, which spends a product repair round
  that the minor-findings rule meant to avoid.
- Review CHANGES_REQUIRED with important (not blocking) findings does not trigger review repair;
  those findings reach verification, where each costs a verifier call and a verification round.
- Each verification overwrites the previous answer, so the reasons behind earlier rounds are lost.
