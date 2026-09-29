# September 28 run recovery implementation

## Goal

An interrupted run can continue from its last trustworthy stage without losing its plan or partial edits. An opted-in run can wait for a confirmed subscription reset. Cloud continuation is enabled only where the provider and saved checkout pass the required checks.

## Changes

1. Add paused run states, immutable stage checkpoints, an exact worktree snapshot, and a per-run lock to the artifact store.
2. Make the workflow save a checkpoint after each completed stage and resume from the first unfinished stage using the saved request, plan, configuration, and role bindings.
3. Preserve an interrupted worker's partial edits, classify confirmed provider quota responses, and require manual adoption when an abrupt crash left unverified changes.
4. Add `resume`, `--auto-resume`, and status details. Reject changed policy, context, branch, artifacts, or active provider processes before invoking a role.
5. Add the provider usage observation contract, due-time calculation, one claimed `resume_due` operation, and scheduler registration, replacement, and cancellation.
6. Add the macOS scheduler command and provider-specific cloud capability checks. For a cloud run waiting for human approval, pin the reviewed plan and summary, record explicit approval, then require a separate resume with the saved checkpoint digest. Keep Codex Cloud automatic continuation unavailable until its same-task API and worktree persistence are proven.
7. Exercise interrupted plan review, partial worker recovery, due-time races, manual cloud approval, and cloud refusal cases with fake providers and disposable repositories.

## Done when

- `dev-orch resume RUN_ID` starts the first unfinished role and leaves completed review rounds and the approved plan unchanged.
- Changed or missing recovery evidence prevents any provider call and preserves the worktree.
- Two concurrent due wakeups invoke one role at most; cancellation prevents a later wakeup.
- `dev-orch status RUN_ID` states why a paused run can or cannot resume.
- `dev-orch approve RUN_ID` records approval only for an intact linked cloud run; `dev-orch resume RUN_ID --checkpoint-digest DIGEST` continues that exact plan.
- `.venv/bin/ruff check --no-cache src tests`, `.venv/bin/ruff format --check --no-cache src tests`, and `.venv/bin/python -m pytest -p no:cacheprovider` pass.

## Risk

A provider can write files immediately before a crash. The saved worktree digest and explicit adoption gate prevent those files from being silently treated as reviewed output.
