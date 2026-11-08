# Phase 1 · Dev-orch efficiency

## Current behavior

Paths below start at `src/dev_orchestration/` unless specified.

| Area | File and function |
| --- | --- |
| Stage machine | `domain/enums.py` (`RunState`), `workflow/engine.py` (`LEGAL_TRANSITIONS`, `Engine.transition`), `workflow/runner.py` (`execute_run`), `workflow/recovery.py` (`resume_run`) |
| Stops | `runner._terminal` writes FAILED, ESCALATED and BLOCKED. `bootstrap.bootstrap_run` and `restart.restart_run` also write failure states directly. `cli.cancel` calls `Engine.cancel`. |
| Records | `artifacts/store.py` (`RunStore.initialize`, `_write_manifest`, `update_manifest`, `append_event`), `artifacts/events.py` (`append_event`) |
| Provider calls | `adapters/claude.py` (`ClaudeAdapter.run`), `adapters/codex.py` (`CodexAdapter.run`), `adapters/usage.py` (`UsageAwareAdapter.run`), `workflow/invoke.py` (`invoke_structured`) |
| Checks and repairs | `workflow/validation.py` (`run_validations`), `workflow/runner.py` (`_run_validation_stage`), `workflow/stages.py` (`repair_validation`, `remediate`, `reconcile`, `verify`) |

`runner.py:1564–1599` validates before implementation review. Some paths skip review. Change 1 can place its pre-check after validation. Change 2 extends existing repair paths. Today validation gets one round per run (`runner.py:982`), implementation review and plan reconciliation get two each (`stages.py:516`, `runner.py:1374`), and final verification gets zero (`runner.py:1790`). Check-only restarts get zero. Explicit retry and restart can already reuse evidence. Bootstrap and classification already precede expensive stages. The reconciler already passes through the usage wrapper.

## Changes

1. `domain/run.py`, `artifacts/store.py`, `workflow/{engine,bootstrap,runner,recovery,restart,legacy,stages}.py`. Add optional `epic_id` from `goal_id` at creation and an atomic manifest `stop` snapshot. Record stops at terminal, blocked and paused exits, including approval waits and handled failures. Fields are `stage, role, provider, model, category, evidence, repairs`. Repairs by gate carry `used, allowed, exhausted`. Use fixed diagnostics and eight categories: reviewer rejected, validation failed, plan rejected, model behaviour, infrastructure, operator cancelled, unknown, context or token limit. Add `role, provider, model` to transition events, including recovery writes. Local stages have null provider and model.
2. `cli.py`, `artifacts/goals.py` and workflow entry points above. Append `human_action` with UTC `ts, actor, action` for manual resume, rerun, supplied or adopted edits, and escalation answers or approvals. Use the local account. Add `dev-orch record-edit RUN_ID` for edits outside the framework. Scheduled resumes stay automatic.
3. `adapters/{usage,claude,codex}.py`, `workflow/{validation,cloud}.py`. Keep per-attempt provider, model and token usage, including reconciliation and missing usage as null. Retry safe transient failures at their call with 1, 2 and 4 seconds of backoff. Preserve quota pauses and interrupts. Skip replay when side effects are possible or a hosted tool failure is opaque.
4. `workflow/reporting.py` and `cli.py`. Add `dev-orch epic-report --json`. Count completed epics with zero human actions through first completion. Report incomplete and unknown coverage separately, including legacy history.
5. Add synthetic tests in `tests/workflow/test_phase1.py` and `tests/adapters/test_usage_ledger.py` for stop paths, repair counts, attribution, retries, usage and legacy metrics. Stage `phase1-report.md`, `phase1-report.json` and `SHA256SUMS` in the Air outbox.

## Done when

- `.venv/bin/ruff check --no-cache src tests` and `.venv/bin/ruff format --check --no-cache src tests` pass.
- `.venv/bin/python -m pytest -p no:cacheprovider` passes using local fixtures.
- The staged packet passes `shasum -a 256 -c SHA256SUMS`.

Work starts on a new branch after approval. Existing run records need no backfill.

## Replacement packet additions

The replacement proposal adds stage timing and a report on obstacles to local models. The Phase 1 work and the approved repair plan remain in scope. At approval, the checkout was on `main` with the Phase 1 changes uncommitted. The edits were preserved and committed on `ai/phase1-efficiency` before the additions were implemented.

1. In `src/dev_orchestration/domain/run.py`, `artifacts/store.py` and `artifacts/events.py`, add optional timing fields for new records. Each state transition will record the outgoing state's UTC start and end, the incoming state's start, and the relevant role, provider and model. Use one timestamp for the shared boundary. Preserve the existing event fields and allow old manifests and events to parse without timing fields.
2. In `src/dev_orchestration/workflow/runner.py`, `recovery.py`, `stages.py` and the shared event writer, record the actual start and end of provider and local validation operations. Planning and plan review occur before their corresponding completed states, so state-transition times alone would assign their duration to the wrong work. Include the epic ID, run ID, operation, role, provider, model, UTC start and end, and an attempt or cycle identifier. Record interrupted and failed operations as well as successful ones. Restarted runs use the same recovery path.
3. Define time consistently in the report. Stage work time sums completed operation intervals by role and stage. Epic elapsed time runs from its first run's creation to its first completion, including waiting and gaps. Paused or interrupted operations without an observed end are incomplete rather than assigned an invented duration. Keep historical records unchanged and mark their missing timing as unknown.
4. Extend `tests/workflow/test_phase1.py` with synthetic cases for planning before a state transition, validation, retries, repeated repair cycles, failure, pause and resume, and older records. Check UTC timestamps, correct role and model, ordering, interval calculations and no double counting. Run the existing lint, format and full test commands listed above.
5. Update `phase1-report.md` and `phase1-report.json` in a fresh Air outbox packet, including the repaired stop schema and test results. Add the local-model obstacles found in the current code: the default registry registers Claude Code and Codex adapters, and the Codex command ignores user configuration without selecting a local provider or endpoint. Record this as a finding for phases 5 to 7, with no provider integration changes. Add and verify `SHA256SUMS`.
