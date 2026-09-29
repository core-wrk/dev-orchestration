# Resume an interrupted dev-orch run

**Status:** Proposed. This is the run-recovery half of the feature; [usage-window scheduling](2026-09-28-usage-window-restart-design.md) builds on it.

## Goal

`dev-orch resume RUN_ID` continues from the first unfinished stage using durable run artifacts after a usage limit, process crash, or role timeout. A usage limit during the third plan review runs that review again against `plan-v3.md`; it does not repeat classification, planning, or the first two review rounds. Once a plan is approved, resumption never silently replaces it or repeats its review. New runs retain their ID, branch, and worktree contents across pauses.

## Changes

1. Add `PAUSED_USAGE` and `PAUSED_INTERRUPTED`, plus an exclusive per-run lock. A confirmed quota limit uses the first state; a killed process or role timeout uses the second and requires explicit resume. Both adapters record provider PID and start time. `resume` refuses while that process is alive and reconciles stale records against the operating system.
2. After each completed stage, atomically write an immutable receipt under `.ai/runs/<id>/checkpoints/`. It records the next role, stage attempt, plan/review/remediation cycle, output artifact hashes, worktree `HEAD` and change digest, and hashes of the request, criteria, policy, scope, role bindings, and context. Write immutable outputs before the receipt, then advance the manifest. Only matching receipts establish completed stages; an orphan output cannot silently supersede an attempt.
3. Normalize a provider usage-limit rejection before schema validation or retry. Preserve the original provider error and reset information, record the interrupted stage, and enter `PAUSED_USAGE`. A configured role timeout or process death enters `PAUSED_INTERRUPTED`; invalid schema, ordinary provider failure, failed validation, and safety escalation retain their existing outcomes. Do not classify arbitrary agent prose as a quota error.
4. Rebuild stage inputs from the saved run, not new CLI arguments. Before invoking a provider, check receipt hashes, approved plan, branch and base, worktree snapshot, process ownership, scope, protected policy, validation commands, approval requirement, role bindings, and context. A restored cloud checkout may use a new path but must match the recorded contents. Refuse changed inputs without reset, stash, or deletion.
5. Before a writing stage, save its worktree snapshot and reserve that isolated worktree for the run. On handled interruption, enforce the scope fence on partial changes and record their content hashes as the run-owned pause snapshot. On resume, a matching branch and unchanged snapshot let the same worker continue from the current diff, approved plan, and original remediation count. An abrupt crash without a pause snapshot requires explicit `--adopt-worktree` after showing the diff and checking scope. Never discard partial edits or count the interrupted attempt as a completed cycle. Read-only stages can retry; validation, review, and final verification judge the eventual diff afresh.
6. Keep the existing human and safety gates. Only `PAUSED_USAGE` can resume automatically; `PAUSED_INTERRUPTED` requires an operator. `AWAITING_APPROVAL`, `BLOCKED`, `CANCELLED`, `COMPLETE_LOCAL`, and other escalations are not automatic candidates. Resume cannot change tier, provider, plan, criteria, or approved scope. `status` names the next stage and any reason resumption is refused; `cancel` still terminates a live provider and removes scheduled continuation.
7. Recover pre-feature runs explicitly when artifacts identify one next stage. A stalled nonterminal run may keep its ID. A terminal quota-failed run gets a **linked new run** with copied, hash-pinned artifacts; its source remains unchanged. Recreate a removed clean worktree only from the recorded branch and base. An older dirty worktree requires `--adopt-worktree`: show its diff, check scope and branch, then record its current snapshot as operator-authorized run work. Ambiguous review order or missing approval proof still refuses recovery. Legacy runs are never scheduled automatically.

## Stage decisions

| Last trustworthy receipt | Next action |
|---|---|
| `plan-v3.md` reconciled; review 3 absent | Review `plan-v3.md`; retain review rounds 1 and 2 and their finding IDs. |
| `approved-plan.md` finalized | Apply that exact plan, after the existing human approval gate if required. |
| Worker completed | Validate saved changes; keep the original worktree. |
| Worker interrupted with a matching pause snapshot | Continue that worker against the preserved partial diff; keep the same stage and cycle. |
| Validation completed | Review the saved diff and validation result. |
| Implementation review completed with blocking findings | Resume the recorded remediation cycle in the same worktree after its snapshot matches. |
| Final verification completed; commit absent | Recheck the judged diff and commit locally only if every gate remains satisfied. |

## Done when

- A fake quota rejection during plan review 3 leaves one paused run; `dev-orch resume RUN_ID` invokes `plan_reviewer` once with `plan-v3.md`, preserving both earlier rounds and finding IDs.
- A pause after plan finalization invokes `implementation_worker` next with the same approved-plan SHA-256; classifier, planner, and plan reviewer are not called again.
- A pause after completed implementation proceeds to validation; an interrupted worker continues from unchanged partial edits in its own worktree, then reruns validation and review.
- A role timeout in any stage records `PAUSED_INTERRUPTED`; explicit resume retries that stage. An abrupt worker crash with unverified partial edits requires `--adopt-worktree` before continuation.
- Tampered artifacts, changed policy or context, a live provider, a mismatched branch or partial diff, and duplicate resume commands each fail before a provider call and preserve the worktree.
- A recoverable older terminal run produces a linked run and leaves its source manifest unchanged; an inconsistent older run is refused.
- `.venv/bin/python -m pytest -p no:cacheprovider tests/workflow/test_resume.py tests/test_cli_resume.py` passes.

## Risk

A stage may finish an output just before its process dies. The receipt boundary prevents guessing that it completed. A paused worker's snapshot permits continuation only in its reserved isolated worktree; edits after that snapshot force review. The scope fence and approved-plan hash remain mandatory.
