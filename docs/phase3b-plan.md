# Phase 3b: give the verifier the evidence the orchestrator already has

Depends on Phase 3a: it uses the verifier's "what is missing" field.

## Goal

In saved runs, 13 of the 21 "not demonstrated" (UNKNOWN) verdicts asked for evidence that
the orchestrator holds, or can produce without any model call:

- the exact command named in a requirement (only the full test suite had been run);
- a fact from the run record, such as whether the implementation review passed;
- a file the verifier could not see because the diff was cut off or the file was unchanged.

After this change the verifier receives that evidence up front. When it still asks for some,
the orchestrator asks it once more, without involving the worker.

The g05 replay (2026-11-08) added one case: a verdict that needed both a file and a test
could report only one of them, so it reported "file" and the run escalated instead of getting
an evidence repair.

## Changes

1. **Commands named in requirements** (new `workflow/criteria_commands.py`, called just
   before each final verification). A command quoted in a requirement is run and its result
   added to the verifier's validation evidence, only when all of these hold:
   - it is not exactly a registered validation command (those already ran);
   - it starts with the same program and subcommand as a registered validation command, for
     example `.venv/bin/python -m pytest` or `.venv/bin/ruff check`. A registered command with
     no subcommand, such as `sh -c ...`, lends no prefix, so no shell or bare interpreter is
     ever run this way;
   - it passes the existing rule that bars commands that deploy, publish or write to a remote;
   - it contains no shell control characters. It runs directly, not through a shell.

   Any other quoted command is not run, and the verifier is told why. These results are
   evidence for the verifier only, not a new validation gate.
2. **Run facts** (`workflow/stages.py` `verify`). The verifier also receives a short summary
   built from the run record: the implementation review's outcome, whether a saved patch was
   applied and its checksum, the starting commit, the worktree path, repair rounds used, and
   the worker's latest report.
3. **Whole files** (`roles/verifier.md`). The verifier is told it may read any file in the
   worktree, read-only, and must look before returning UNKNOWN for want of a file.
4. **Several missing kinds** (`domain/findings.py`, `workflow/contract.py`). An UNKNOWN verdict
   names every kind of missing evidence that applies, as a list. Answers saved with a single
   kind still load. Routing: any FAIL means a product repair. Otherwise, if any UNKNOWN lacks a
   kind or includes "other", the run escalates. If any UNKNOWN includes "test", the run gets an
   evidence repair. If none does, it gets one more ask (step 5).
5. **One more ask** (`workflow/runner.py`, `workflow/recovery.py`). When every UNKNOWN asks
   only for a command, run fact or file, the orchestrator runs verification once more and shows
   the verifier its previous verdicts. This uses no worker call and no repair round. Each
   verification round allows one such ask; if the second answer still asks only for those, the
   run escalates. A resumed run continues the ask where it stopped.
6. **Tests** in `tests/workflow/test_phase3b.py` (below).

## Done when

- `.venv/bin/python -m pytest -p no:cacheprovider tests/workflow/test_phase3b.py` passes
  and covers:
  - a requirement quoting `.venv/bin/python -m pytest tests/x.py -k y`: that command runs
    and its result reaches the verifier;
  - a requirement quoting a command with any other program, a shell, or a denied word: it does
    not run, and the verifier is told why;
  - the run-fact summary appears in the verifier's input;
  - UNKNOWN asking for a file: one extra verifier call and no worker call; asking again
    escalates;
  - UNKNOWN asking for a file and a test: an evidence repair;
  - an answer saved with a single missing kind still loads and routes the same way;
  - an interrupted ask resumes as an ask.
- The three registered gates (lint, format check, full tests) exit 0.
- Measurement, which needs separate approval: replay d05 once more (about 10 to 14 hosted
  calls).

## Risks

- **Running commands taken from text.** Requirements are written by people and planners, so a
  quoted command is untrusted. Step 1 runs only extensions of approved validation commands,
  never through a shell, never a shell or bare interpreter, and never a denied word. Tests the
  repository already contains can run code, but the registered test command already runs them.
- **A quoted command that changes files.** Verification must not change the worktree. The
  existing check after verification already stops the run if anything changed.
