# Phase 1 repair plan

## Goal

A failed setup releases the epic for another attempt, and a stop record identifies the work that was underway when the run stopped.

## Changes

1. In `workflow/bootstrap.py`, route exceptions from the base-commit and ancestry checks through the existing bootstrap failure and worktree cleanup path. Persist `FAILED`, the completion time, and a safe diagnostic before cleanup. Keep the original exception visible to the caller. Reuse the existing ownership checks when cleaning up the branch and worktree.
2. In `workflow/runner.py`, give failures during setup before classification the same terminal outcome. Reuse `_terminal` and the existing guarded cleanup, and release the run claim even if failure recording or cleanup raises. Leave failures to acquire the claim to the current claim handling, since another process may own the run.
3. In `domain/run.py` and `artifacts/store.py`, let stop records name the active planning operation explicitly. Use the role recorded at provider invocation to distinguish `PLANNING`, `PLAN_REVIEW`, and `PLAN_RECONCILIATION`. Keep the previous lifecycle state in a separate optional field so the transition can still be reconstructed. Accept historical stop records as written. Use the same stop-record builder for normal execution and recovery so both paths apply the same attribution.
4. Extend `tests/workflow/test_phase1.py` with injected failures for the bootstrap Git checks, setup before classification, and the three planning operations. Include a resumed planning failure and a historical record that lacks the new field.

## Done when

- Each injected setup failure records `FAILED` with a stop reason, surfaces the original error, and permits another attempt for the same epic without manual cancellation when cleanup succeeds.
- Each injected planning failure records the active operation, role, and model. The previous lifecycle state remains available, and historical records still parse.
- `.venv/bin/ruff check --no-cache src tests`, `.venv/bin/ruff format --check --no-cache src tests`, and `.venv/bin/python -m pytest -p no:cacheprovider` pass using local fixtures.

The five-epic baseline remains a separate rollout gate before phase 2.
