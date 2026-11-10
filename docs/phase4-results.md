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

## Second replay with first-round-only repair (k07)

Measured 2026-11-10 at commit `9b5b991`, one attempt, same setup as i07 and j07. The call limit
was raised from 170 to 200 first (user approval). It used 10 hosted calls and $1.81 recorded
(running total 171 of 200 calls, $16.13).

**COMPLETE_LOCAL** (`314d057` in the k07 copy; 8 files, +237/-14), in about 51 minutes.

| Run | Code | Result | Calls | Repairs | Time |
|---|---|---|---|---|---|
| i07 | before Phase 4 | COMPLETE_LOCAL | 12 | 3 verification | 64 min |
| j07 | Phase 4, important findings at every review round | ESCALATED at review | 8 | 2 review | 47 min |
| k07 | Phase 4, important findings in round 1 only | COMPLETE_LOCAL | 10 | 1 review + 1 verification | 51 min |

What ran, and how:
1. **Review round 1** took F001 (blocking) and F002 (important), limited to `goals.py` and
   `runner.py`. The second review found F003 and F004 (important) and F005 (minor); with the new rule
   none was repaired at review, and the run moved on to final verification with no escalation.
2. **Dispositions.** The first verifier answer gave F003, F004 and F005 each a disposition (all
   unresolved) and failed criterion 2, citing F003 and F004. Because they are important, not minor,
   the failure went to a product repair; it fixed both. No retry was needed (10 calls = the 10
   expected).
3. **The second answer** was PASS with only F006, a minor finding, noted as unresolved. The new
   completion rule accepted it.
4. **Both answers were kept** (`final-verification-v1.json`, `-v2.json`); the latest file equals v2.
5. **Review rounds were not exhausted:** the blocking-only later rounds were never needed, but the
   j07 situation (important findings using both rounds) cannot recur.

Not exercised on a live run: the restart ask, the AO-03b case (a PASS leaving an important finding
unlisted; here the verifier listed every finding), and minor-only counting of a FAIL. Tests cover
all three. This is one run per variant; the verifier and reviewer both vary from run to run.
