# Phase 1 baseline

- Selected epics measured: 5
- Completed without human intervention: 0
- Completed with human intervention: 0
- Incomplete: 5
- Unknown intervention coverage: 0
- Known stop reasons: 5 of 5 non-completions
- Provider calls with usage: 29 of 29

## Outcomes

- ESCALATED: 2
- FAILED: 3

## Stop categories

- model behaviour: 1
- plan rejected: 2
- validation failed: 2

## Phase 2 comparison

The Phase 1 gate was met. All five runs recorded a known stop reason.
The replays used filtered historical snapshots. Preflight found an age-sensitive
test in one snapshot and a missing test directory in another. Both replays
stopped at validation. Account for these starting conditions when comparing
Phase 2 results.
