# Pause for subscription usage windows and restart across sessions

**Status:** Proposed. Depends on [durable run resume](2026-09-28-resume-and-usage-window-design.md).

## Goal

An opted-in `dev-orch` run checks the quota of the provider assigned to its next role. When a trustworthy signal says its five-hour allowance is nearly spent, it pauses after the current stage. When a provider rejects a call for a confirmed usage limit, it pauses that stage. A scheduler restarts an eligible run at the provider's reset time, or at a conservative retry time when the provider confirms a five-hour limit but gives no usable timestamp. The run keeps its plan and completed work.

## Changes

1. Add `UsageObservation` to the provider adapter contract: provider, limit kind (`five_hour`, `weekly`, `other`, or `unknown`), state (`available`, `near`, `exhausted`, or `unknown`), used percent if supplied, reset timestamp in UTC if supplied, observation time, signal source, and raw diagnostic reference. Only the adapter interprets provider-specific output. A token count, elapsed time, a generic timeout, and model-generated prose do not establish remaining subscription allowance.
2. Probe the assigned provider just before each stage when a supported read-only quota signal exists. Pause proactively at **90% used** for a confirmed five-hour window only when a reset time can be scheduled. Do not interrupt a role already running. A near-limit signal for Claude does not stop a Codex role, or vice versa. If no reliable signal is available, show `usage unknown`, proceed normally, and still handle an eventual quota rejection.
3. Recognize confirmed usage-limit rejection from the provider's error channel or structured result, even when its process exits successfully. Capture the full diagnostic before schema validation or a schema retry. Distinguish five-hour exhaustion from weekly exhaustion, unavailable credits, authentication failure, provider outage, and the framework's own per-role timeout. Only confirmed quota failures enter `PAUSED_USAGE`; other errors keep their existing disposition.
4. Record `auto_resume`, `paused_provider`, `paused_role`, `limit_kind`, `observed_at`, `reset_at`, `next_attempt_at`, and retry count in the run. For a trustworthy reset timestamp, schedule one attempt just after it. If several exhausted windows are reported, use the latest required reset. For a confirmed five-hour limit without an unambiguous timestamp, use **rejection time plus five hours**. Unknown or longer limits without a reset time require manual resume. A retry that is still limited updates the pause record instead of creating another run or repeating finished stages.
5. Make automatic continuation opt-in with `dev-orch run ... --auto-resume` and `dev-orch resume RUN_ID --auto-resume`. Expose one idempotent `resume_due(repo, run_id)` entry point that claims the run, checks its due time and every recovery gate, then attempts one continuation. A scheduler driver can register, replace, or cancel a due wakeup, but never decides which stage to run. A local queue indexes repository and run ID; the run artifact is authoritative. Concurrent wakeups must produce one provider call.
6. Offer a macOS driver through `dev-orch scheduler enable`, installing a per-user `launchd` job; `disable` stops future wakeups without cancelling runs. A hosted runner can instead invoke `resume_due` from its own scheduled job or webhook. It must retain run artifacts, checkout contents, branch, and credentials across sessions and provide an atomic run claim shared by workers. [Claude Code and Codex cloud-session requirements](2026-09-28-cloud-session-resume-design.md) define the provider-specific gates.
7. `status` shows provider, next role, quota signal, retry time, and any manual action; `runs` marks paused runs; `cancel` removes due work. Authentication trouble, changed configuration, a mismatched partial-work snapshot, human approval, or a safety escalation removes automatic eligibility. The scheduler never buys credits, redeems resets, switches providers, changes model or tier, pushes, merges, deploys, publishes, or touches production data. A detectable account change refuses automatic resume; if identity is unavailable, an opted-in run uses the current login and displays `account unverified`.

## Provider feasibility

Codex exposes [`account/rateLimits/read`](https://github.com/openai/codex/blob/main/codex-rs/app-server-protocol/src/protocol/common.rs) with optional [usage and reset fields](https://github.com/openai/codex/blob/main/codex-rs/app-server-protocol/src/protocol/v2/account.rs). Its missing windows must remain `unknown`. Anthropic documents [near-limit warnings and reset-time errors](https://support.claude.com/en/articles/12466728-troubleshoot-claude-error-messages), but the available guidance does not establish a machine-readable headless quota probe. Claude's initial implementation may therefore only pause after a confirmed blocking response. Before adapter code is accepted, capture sanitized CLI fixtures from the installed versions for success, near-limit, five-hour rejection, weekly rejection, and absent data. Do not read stored account tokens or call undocumented subscription endpoints.

## Done when

- With fake Codex usage at 89% used, the next Codex role starts; at 90% with a reset time, it does not start and the run shows `PAUSED_USAGE`. A Claude quota observation does not pause a Codex role.
- A fake five-hour rejection during plan review schedules one later attempt and resumes that same review after the fake clock passes the due time; no completed plan or review round is invoked again.
- A weekly rejection with a reset time waits for that time; an unknown limit without one stays manual. A framework role timeout is never queued as a quota reset.
- Two schedulers racing for one run yield one provider invocation. Restarting the scheduler from saved files retains its due work. `cancel` prevents a due invocation.
- A fake hosted wakeup from a new process and a restored checkout path uses the same `resume_due` path; missing run artifacts or a mismatched change digest prevent invocation.
- An interrupted worker with an unchanged, in-scope pause snapshot continues automatically. A changed snapshot, missing approval, changed policy, invalid plan hash, missing authentication, or detectable account change prevents invocation.
- `.venv/bin/python -m pytest -p no:cacheprovider tests/workflow/test_usage_pause.py tests/workflow/test_resume_scheduler.py` passes.

## Risk

Quota data can lag usage in other Claude or Codex sessions. The check reduces avoidable failures but cannot guarantee that a role has enough allowance to finish. A rejected role still follows the durable pause and resume path.
