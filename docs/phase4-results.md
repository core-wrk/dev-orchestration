# Phase 4 measurement: d07 replay (j07)

Measured 2026-11-10 at commit `f411e25`, one attempt, set up like i07 (start `17635ba`, proposal
supplied, same plan, criteria, models, sandbox and worker environment). It used 8 hosted calls and
$1.95 recorded (running total 161 of 170 calls, $14.32).

## Result

**ESCALATED** (`implementation_review_exhausted`) after about 47 minutes: a blocking finding,
F008, was still open after the two review repair rounds. The run never reached final verification,
so the verification half of Phase 4 (finding dispositions, minor-only counting, restart ask,
versioned answers, labelled worker report) was **not exercised**. Only step 1, repairing important
findings at review, ran. No schema retries: 8 calls match the 8 expected (classifier, plan review,
worker, three reviews, two repairs).

| Round | Review findings | Repaired |
|---|---|---|
| 1 | F001 blocking, F002-F004 important, F005 minor | F001-F004, in `runner.py` and `recovery.py` |
| 2 | F006 important, F007 minor | F006, in `runner.py` |
| 3 | **F008 blocking** | no round left, so the run escalated |

F008 is a regression the round-1 repair introduced: the repair of F004 (an important finding in
`recovery.py`) read a variable that is unset on the resume path, raising an unhandled error. The
review caught it, so nothing unsafe was kept, but the run had no repair left to fix it.

## Why this is a design problem, not only variance

i07 (before Phase 4) had the same kind of findings and completed. There the important findings
reached final verification, which has three repair rounds of its own, separate from review's two.
Under Phase 4 the important findings spend review rounds first, so a blocking regression found
late (F008) has none left, and the budget for important-level work shrank from 3 rounds to 2.

This is one run. The reviewer also rated different issues than in i07 (a blocking F001 this time),
so the comparison is not controlled. The mechanism is clear, though: review rounds are now
shared between important findings and the regressions that repairing them causes.

## What this suggests (not decided)

Options for review repair of important findings:
1. **Keep one important-only round.** Repair important findings only in the first review round;
   later rounds take blocking findings only, and leftover important findings go to verification.
   In j07 this would have sent F006 to verification and avoided a third review round.
2. **A third review round for blocking findings only.** Keeps the current behavior and reserves
   room for regressions.
3. **Revert step 1** and keep the verification-side changes, which this run did not test.

Whichever is chosen, the verification-side steps still need a run that reaches verification.
Nine hosted calls remain, which is less than one full replay.
