# Phase 3b: give the verifier the evidence the orchestrator already has

Depends on Phase 3a: it uses the verifier's new "what is missing" field.

## Goal

In saved runs, 13 of the 21 "not demonstrated" (UNKNOWN) verdicts asked for evidence that
the orchestrator holds, or can produce without any model call:

- the exact command named in a requirement (only the full test suite had been run);
- a fact from the run record, such as whether the implementation review passed;
- a file the verifier could not see because the diff was cut off or the file was unchanged.

After this change the verifier receives that evidence up front. When it still asks for some,
the orchestrator supplies it and asks once more, without involving the worker.

## Changes

1. **Commands named in requirements** (`workflow/contract.py`, validation). The orchestrator
   runs a command quoted in a requirement as an extra validation check, only if it starts
   with the same program as an already registered validation command (for example
   `.venv/bin/python -m pytest`) and passes the existing rule that bars commands that
   deploy, publish or write to a remote. Any other quoted command is not run, and the
   verifier is told why.
2. **Run facts** (`workflow/stages.py` `verify`). The verifier also receives a short summary
   built from the run record: the implementation review's outcome, whether a saved patch was
   applied and its checksum, the starting commit, and the worker's final report.
3. **Whole files** (`roles/verifier.md`). The verifier is told the worktree path and may
   read any file in it, read-only, instead of judging from the diff alone.
4. **One more ask** (`workflow/runner.py`, `workflow/recovery.py`). Suppose every
   non-passing verdict is UNKNOWN and each one asks for a command, run fact or file. The
   orchestrator then supplies them and runs verification once more. This uses no repair
   round. If the second answer asks for the same thing, the run escalates.
5. **Tests** in `tests/workflow/test_phase3b.py` (below).

## Done when

- `.venv/bin/python -m pytest -p no:cacheprovider tests/workflow/test_phase3b.py` passes
  and covers:
  - a requirement quoting `.venv/bin/python -m pytest tests/x.py -k y`: that command runs
    and its result reaches the verifier;
  - a requirement quoting a command with any other program: it does not run;
  - the run-fact summary appears in the verifier's input;
  - UNKNOWN asking for a command: one extra verifier call and no worker call; asking again
    escalates.
- The three registered gates (lint, format check, full tests) exit 0.
- Measurement, which needs separate approval (about 14 hosted calls): rerun the verifier on
  the 14 saved historical cases with the new input and count how many still return UNKNOWN.

## Risks

- **Running commands taken from text.** Requirements are written by people and planners,
  so a quoted command is untrusted. Step 1 limits it to programs already approved as
  validation commands, plus the existing deny-list, and never runs anything else.
