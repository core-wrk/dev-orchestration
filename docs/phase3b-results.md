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
