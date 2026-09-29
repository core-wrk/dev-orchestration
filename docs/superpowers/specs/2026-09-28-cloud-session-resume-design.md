# Resume dev-orch runs in Claude Code and Codex cloud sessions

**Status:** Proposed. Depends on [run checkpoints](2026-09-28-resume-and-usage-window-design.md) and the [scheduler contract](2026-09-28-usage-window-restart-design.md).

## Goal

A run started in a Claude Code or Codex cloud session can continue after a subscription usage-window reset without repeating completed dev-orch stages. The cloud session is the execution location; the dev-orch run ID, approved plan, branch, scoped changes, and review gates remain authoritative. A provider integration advertises automatic cloud resume only after it proves both session continuation and preservation of unfinished work.

## Changes

1. Record a `cloud_session` reference in the run: provider, provider session/task ID, repository and branch identity, environment identity, and last observed session state. Never store login tokens in the manifest. Only sessions launched with dev-orch or explicitly linked to an intact dev-orch run artifact set are eligible; arbitrary old cloud chats are not assumed recoverable.
2. Require durable run artifacts and the **exact partial worktree snapshot**, including tracked and untracked files, before a cloud environment can be replaced. On reentry, compare the restored checkout to the pause snapshot and original base/branch. A conversation transcript or branch name alone is insufficient. If a provider only restores conversation while losing unfinished shell commands or files, keep the run paused and report the missing state rather than starting from a clean checkout.
3. Give each cloud adapter three operations: inspect whether its session is idle and accessible; deliver a resume request to that same session; and read the delivery result. The message contains the dev-orch run ID and checkpoint digest, then asks the cloud environment to call the normal `resume` path. The provider session ID is never treated as a dev-orch stage receipt. A new task is not silently substituted for a missing session.
4. Claude Code cloud has a documented CLI follow-up form, `claude -p ... --cloud <session-id>`, with a JSON delivery result. A Claude adapter may use it after checking that the session is not archived and that the run artifacts and checkout survived reentry. Claude documents that an expired cloud VM restores conversation history but not background shell commands; this is a required restart test, not evidence that the worktree survived. [Claude cloud sessions](https://code.claude.com/docs/en/claude-code-on-the-web)
5. Treat Codex Cloud as a separate capability. The installed `codex cloud` CLI exposes `exec`, `status`, `list`, `diff`, and `apply`; `exec` creates a new task and is not a same-task follow-up. Do not use an undocumented backend endpoint or substitute the [API-billed Agents API](https://developers.openai.com/api/docs/guides/agents-api/overview) for the user's subscription task. Until a supported same-task continuation path and worktree persistence are verified, `status` must say `Codex Cloud auto-resume unavailable` and keep the dev-orch checkpoint recoverable.
6. Run the due-time wakeup outside the cloud session so VM expiry cannot erase its timer. It may be a user-controlled hosted scheduler or the local macOS driver; both call the same idempotent `resume_due` operation. A cloud driver must prove a durable, atomic run claim across wakeups. Rejections update the due time; a missing session or lost snapshot stops automatic attempts and names the operator action.
7. Preserve dev-orch's local-only delivery boundary. A cloud session may not push, create a PR, merge, deploy, or publish as part of resume. A provider mode that implicitly publishes changes is ineligible unless that behavior can be disabled. The continuation receives the same approved plan, role, scope, and validation rules as the original run.

## Done when

- A fake Claude cloud session accepts one follow-up after its reset time, and the reentered environment invokes `dev-orch resume RUN_ID` once with the expected checkpoint digest.
- Expired-environment, archived-session, missing-artifact, changed-branch, and mismatched partial-diff fixtures leave the run paused without creating a replacement task or changing the worktree.
- A fake hosted scheduler and a local scheduler racing for the same run yield one stage invocation.
- Codex Cloud capability detection reports unavailable on the installed CLI until an official same-task continuation path is implemented; no `codex cloud exec` fallback runs.
- Cloud adapter fixtures prove that no resume command or provider mode pushes, opens a PR, merges, deploys, or publishes.

## Constraint

Provider cloud sessions and dev-orch runs are different state machines. The cloud connection may be restored while the dev-orch worktree is not. Automatic cloud resume is available per provider only after both sides of that contract pass a live, non-publishing test.
