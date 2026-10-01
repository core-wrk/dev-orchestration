"""Typer entry point. Commands register here; logic lives in modules."""

import hashlib
import json
import shlex
from datetime import UTC, datetime
from pathlib import Path

import typer
import yaml

from dev_orchestration import doctor as doctor_module
from dev_orchestration import release
from dev_orchestration.adapters.registry import ReadOnlyRoleUnsupportedError, default_registry
from dev_orchestration.artifacts.store import (
    CheckpointError,
    NoApprovedPlanError,
    RunClaimError,
    RunStore,
)
from dev_orchestration.config.models import DeniedCommandError, GlobalConfig, ProjectConfig
from dev_orchestration.config.resolver import ProtectedRuleViolation
from dev_orchestration.context.assembler import ContextContractError
from dev_orchestration.domain.enums import RunState, Tier
from dev_orchestration.git.repo import (
    DirtyWorktreeError,
    EmptySnapshotError,
    GitCommandError,
    discover_repo,
)
from dev_orchestration.git.worktree import worktree_path
from dev_orchestration.init_repo import (
    FileExistsRefusal,
    UncommittedContractFileError,
    initialize_repo,
)
from dev_orchestration.roles.loader import MissingTemplateError
from dev_orchestration.workflow.bootstrap import BootstrapIntegrityError
from dev_orchestration.workflow.cloud import (
    CloudDelivery,
    link_cloud_session,
)
from dev_orchestration.workflow.engine import Engine, IllegalTransitionError
from dev_orchestration.workflow.invoke import AgentInvocationError, SchemaEscalation
from dev_orchestration.workflow.legacy import LegacyRecoveryError
from dev_orchestration.workflow.recovery import (
    ResumeRefused,
    approve_run,
    resume_blocker,
    resume_due,
    resume_run,
)
from dev_orchestration.workflow.reporting import (
    describe_status,
    list_runs,
    read_events,
    render_token_usage,
    summarize_token_usage,
)
from dev_orchestration.workflow.runner import cleanup_worktree_if_unproductive, execute_run
from dev_orchestration.workflow.scheduler import (
    SchedulerError,
    SchedulerQueue,
    adopt_login_shell_path,
    disable_launchd,
    enable_launchd,
)
from dev_orchestration.workflow.scope_check import ScopeViolation
from dev_orchestration.workflow.stages import RemediationExhausted
from dev_orchestration.workflow.tiers import TierDowngradeError, UnsupportedTierError

# Errors that represent a refusal or a bad input rather than a bug. The user
# needs the message, not a traceback: every one of these already explains what
# happened and what to do about it, and a stack trace only buries that.
EXPECTED_ERRORS = (
    UncommittedContractFileError,
    FileExistsRefusal,
    GitCommandError,
    DeniedCommandError,
    DirtyWorktreeError,
    EmptySnapshotError,
    UnsupportedTierError,
    TierDowngradeError,
    ProtectedRuleViolation,
    BootstrapIntegrityError,
    ScopeViolation,
    RemediationExhausted,
    SchemaEscalation,
    AgentInvocationError,
    ContextContractError,
    IllegalTransitionError,
    NoApprovedPlanError,
    MissingTemplateError,
    ReadOnlyRoleUnsupportedError,
    CheckpointError,
    RunClaimError,
    ResumeRefused,
    LegacyRecoveryError,
    SchedulerError,
)

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Local-first orchestration for AI-assisted development.",
)
scheduler_app = typer.Typer(no_args_is_help=True, help="Manage usage-window wakeups.")
app.add_typer(scheduler_app, name="scheduler")
cloud_app = typer.Typer(no_args_is_help=True, help="Link a saved run to a provider cloud session.")
app.add_typer(cloud_app, name="cloud")


def _show_version(value: bool) -> None:
    if value:
        typer.echo(release.installed_commit())
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_show_version,
        is_eager=True,
        help="Show the commit this dev-orch was promoted from.",
    ),
) -> None:
    """Local-first orchestration for AI-assisted development."""


@app.command()
def promote() -> None:
    """Freeze the latest commit as the dev-orch other repos and the scheduler use."""
    try:
        commit = release.promote(release.source_checkout())
    except release.PromoteError as exc:
        typer.echo(f"promote failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"dev-orch now runs commit {commit}")


@app.command()
def doctor() -> None:
    """Diagnose the local environment and repository contract."""
    typer.echo(doctor_module.render(doctor_module.run_checks(Path.cwd())))


@app.command()
def init(
    config_file: Path = typer.Option(  # noqa: B008 - Typer's option-as-default idiom
        ..., "--config", help="Prepared .ai/project.yaml"
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Overwrite existing AGENTS.md and .ai/project.yaml. "
            ".ai/context.md is never regenerated."
        ),
    ),
) -> None:
    """Write the repository contract into the current repository.

    .ai/context.md is written once and never regenerated, even with --force:
    it holds onboarding facts that cannot be recovered by reading the repo.
    """
    try:
        repo = discover_repo(Path.cwd())
        config = ProjectConfig.model_validate(yaml.safe_load(config_file.read_text()))
        written = initialize_repo(repo.root, config, force=force)
    except EXPECTED_ERRORS as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    for path in written:
        typer.echo(f"wrote {path.relative_to(repo.root)}")


class NoSuchRunError(RuntimeError):
    """The requested run id is not present in this repository."""


def _project_config(repo) -> ProjectConfig:
    path = repo.root / ".ai" / "project.yaml"
    if not path.is_file():
        raise GitCommandError(f"{path} does not exist; run `dev-orch init` first")
    return ProjectConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


GLOBAL_CONFIG_PATH = Path.home() / ".dev-orchestration" / "config.yaml"


def _global_config(path: Path | None = None) -> GlobalConfig:
    """Load machine-level configuration, or defaults when it is absent."""
    target = path or GLOBAL_CONFIG_PATH
    if not target.is_file():
        return GlobalConfig()
    return GlobalConfig.model_validate(yaml.safe_load(target.read_text(encoding="utf-8")) or {})


@app.command()
def run(
    request: str = typer.Argument(..., help="What you want done"),
    plan_file: Path | None = typer.Option(  # noqa: B008
        None, "--plan", help="Import an external plan"
    ),
    tier: str | None = typer.Option(None, "--tier", help="Override classification"),
    downgrade_reason: str | None = typer.Option(
        None,
        "--downgrade-reason",
        help="Why a --tier below the classified tier is justified; required to downgrade",
    ),
    criterion: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--criterion",
        help="An acceptance criterion final verification must judge; repeat for several",
    ),
    auto_resume: bool = typer.Option(
        False, "--auto-resume", help="Resume after confirmed usage resets"
    ),
) -> None:
    """Execute one local run. It never pushes, merges, or deploys."""
    criteria = list(criterion or [])
    criteria_source = "explicit"
    if not criteria:
        criteria = [request]
        criteria_source = "request_fallback"
        typer.echo(
            "No --criterion given; verifying against the request text itself. "
            "Pass --criterion to state what done actually means.",
            err=True,
        )
    try:
        repo = discover_repo(Path.cwd())
        config = _project_config(repo)
        override = None
        if tier is not None:
            try:
                override = Tier(tier)
            except ValueError as exc:
                raise UnsupportedTierError(f"unknown or unsupported tier {tier!r}") from exc
        global_config = _global_config()
        registry = default_registry(config, global_config)
        outcome = execute_run(
            repo,
            config,
            registry,
            request,
            Path(global_config.worktree_root).expanduser(),
            acceptance_criteria=criteria,
            acceptance_criteria_source=criteria_source,
            tier_override=override,
            downgrade_reason=downgrade_reason,
            external_plan=plan_file,
            auto_resume=auto_resume,
        )
    except EXPECTED_ERRORS as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"{outcome.run_id}: {outcome.final_state}")
    if outcome.reason:
        typer.echo(outcome.reason, err=True)
    if outcome.final_state is not RunState.COMPLETE_LOCAL:
        raise typer.Exit(1)


@app.command()
def approve(
    run_id: str = typer.Argument(..., help="Cloud run ID awaiting human approval"),
    checkout: Path | None = typer.Option(  # noqa: B008
        None, "--checkout", help="Restored cloud worktree path"
    ),
) -> None:
    """Approve the saved plan for a linked cloud run; resume separately in that session."""
    try:
        repo = discover_repo(Path.cwd())
        config = _project_config(repo)
        digest = approve_run(
            repo,
            config,
            default_registry(config, _global_config()),
            run_id,
            restored_worktree=checkout.resolve() if checkout is not None else None,
        )
    except EXPECTED_ERRORS as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"{run_id}: approved saved plan")
    checkout_arg = (
        f" --checkout {shlex.quote(str(checkout.resolve()))}" if checkout is not None else ""
    )
    typer.echo(
        f"In the linked cloud checkout, run: dev-orch resume {run_id} "
        f"--checkpoint-digest {digest}{checkout_arg}"
    )


@app.command()
def resume(
    run_id: str = typer.Argument(..., help="Saved run ID"),
    adopt_worktree: bool = typer.Option(
        False, "--adopt-worktree", help="Accept inspected, in-scope partial edits after a crash"
    ),
    auto_resume: bool = typer.Option(
        False, "--auto-resume", help="Opt in to later usage-window wakeups"
    ),
    checkout: Path | None = typer.Option(  # noqa: B008
        None, "--checkout", help="Restored cloud checkout path"
    ),
    checkpoint_digest: str | None = typer.Option(
        None, "--checkpoint-digest", help="Expected SHA-256 of the saved checkpoint"
    ),
) -> None:
    """Continue the first unfinished stage of an interrupted run."""
    try:
        repo = discover_repo(Path.cwd())
        config = _project_config(repo)
        registry = default_registry(config, _global_config())
        outcome = resume_run(
            repo,
            config,
            registry,
            run_id,
            adopt_worktree=adopt_worktree,
            auto_resume=True if auto_resume else None,
            restored_worktree=checkout.resolve() if checkout is not None else None,
            expected_checkpoint_digest=checkpoint_digest,
        )
    except EXPECTED_ERRORS as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"{outcome.run_id}: {outcome.final_state}")
    if outcome.reason:
        typer.echo(outcome.reason, err=True)
    if outcome.final_state is not RunState.COMPLETE_LOCAL:
        raise typer.Exit(1)


@cloud_app.command("link")
def cloud_link(
    run_id: str = typer.Argument(...),
    provider: str = typer.Option(..., "--provider"),
    session_id: str = typer.Option(..., "--session-id"),
    environment_id: str = typer.Option(..., "--environment-id"),
    checkout: Path | None = typer.Option(  # noqa: B008
        None, "--checkout", help="Restored cloud worktree path"
    ),
) -> None:
    """Link an intact paused run to an existing cloud session."""
    try:
        repo = discover_repo(Path.cwd())
        session = link_cloud_session(
            repo,
            run_id,
            provider,
            session_id,
            environment_id,
            restored_worktree=checkout.resolve() if checkout is not None else None,
        )
    except EXPECTED_ERRORS as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"{run_id}: linked {session.provider} session {session.session_id}")


@app.command()
def status(run_id: str | None = typer.Argument(None, help="Run id to inspect")) -> None:
    """Show the current stage and the next action for a run."""
    try:
        repo = discover_repo(Path.cwd())
        candidates = list_runs(repo.root)
        selected = run_id or (candidates[0][0] if candidates else None)
        if selected is None:
            raise NoSuchRunError("no runs found in this repository")
        store_root = repo.root / ".ai" / "runs" / selected
        if not (store_root / "manifest.json").is_file():
            raise NoSuchRunError(f"run {selected!r} does not exist")

        store = RunStore(repo.root, selected)
        manifest = store.read_manifest()
        blocker = None
        if manifest.status in {RunState.PAUSED_USAGE, RunState.PAUSED_INTERRUPTED}:
            try:
                config = _project_config(repo)
                blocker = resume_blocker(
                    repo, config, default_registry(config, _global_config()), selected
                )
            except EXPECTED_ERRORS as exc:
                blocker = str(exc)
        typer.echo(
            describe_status(manifest, read_events(store_root), store.latest_checkpoint(), blocker)
        )
    except EXPECTED_ERRORS + (NoSuchRunError,) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


_RETRYABLE_CAUSES = {
    "implementation_review_exhausted",
    "implementation_review_escalated",
    "validation_failed",
    "validation_failed_after_remediation",
}


@app.command()
def retry(
    run_id: str = typer.Argument(..., help="Dead run to relaunch with its approved plan"),
    fresh: bool = typer.Option(
        False, "--fresh", help="Re-implement from scratch instead of continuing the saved change"
    ),
    review_escalation_reason: str | None = typer.Option(
        None,
        "--review-escalation-reason",
        help="Resolution explaining why the implementation review escalation can be retried",
    ),
) -> None:
    """Relaunch a run that died after its plan was approved, continuing its saved change."""
    try:
        repo = discover_repo(Path.cwd())
        source = RunStore(repo.root, run_id)
        if not (source.root / "manifest.json").is_file():
            raise NoSuchRunError(f"run {run_id!r} does not exist")
        old = source.read_manifest()
        if old.terminal_cause == "implementation_review_escalated":
            review_files = list((source.root / "review").glob("implementation-review-v*.json"))
            if review_files:
                latest_review = max(
                    review_files, key=lambda path: int(path.stem.rsplit("-v", 1)[1])
                )
                review = json.loads(latest_review.read_text(encoding="utf-8"))
                typer.echo("Saved implementation review findings:")
                typer.echo(json.dumps(review.get("findings", []), indent=2, sort_keys=True))
            if not review_escalation_reason or not review_escalation_reason.strip():
                raise ContextContractError(
                    "implementation review escalation requires --review-escalation-reason TEXT"
                )
        if old.terminal_cause not in _RETRYABLE_CAUSES:
            raise ContextContractError(
                f"run {run_id} ended with cause {old.terminal_cause!r}; only "
                f"{sorted(_RETRYABLE_CAUSES)} can reuse the approved plan. If the plan "
                "itself must change, a fresh `dev-orch run` is required (optionally with --plan)."
            )
        if old.retry_of is not None:
            raise ContextContractError(
                f"run {run_id} was already a retry of {old.retry_of} and failed the same "
                "way; the plan is the likelier problem. Revise it and use `dev-orch run --plan`."
            )
        plan_path = source.root / "planning" / "approved-plan.md"
        if old.status not in {
            RunState.COMPLETE_LOCAL,
            RunState.CANCELLED,
            RunState.BLOCKED,
            RunState.ESCALATED,
            RunState.FAILED,
        }:
            raise ContextContractError(f"run {run_id} is not terminal and cannot be retried")
        if not plan_path.is_file() or not old.request_text or not old.acceptance_criteria:
            raise ContextContractError(
                f"run {run_id} has no approved plan, original request, or acceptance criteria to "
                "reuse; a fresh `dev-orch run` is required"
            )
        if (
            not old.plan_sha256
            or hashlib.sha256(plan_path.read_bytes()).hexdigest() != old.plan_sha256
        ):
            raise ContextContractError(f"run {run_id} approved plan hash is missing or changed")
        head = repo.current_commit()
        if old.git.base_commit and head != old.git.base_commit:
            typer.echo(
                f"Note: the repository moved since {run_id} ({old.git.base_commit[:8]} -> "
                f"{head[:8]}); the plan will be re-reviewed against the current state.",
                err=True,
            )
        saved_patch = source.root / "execution" / "final-change.patch"
        continue_patch = saved_patch if saved_patch.is_file() and not fresh else None
        if continue_patch is None and not fresh:
            typer.echo(
                f"No saved change found for {run_id}; re-implementing from the approved plan.",
                err=True,
            )
        config = _project_config(repo)
        source_scope = old.config_layers[0].get("scope") if old.config_layers else None
        if source_scope != config.model_dump(mode="json")["scope"]:
            raise ContextContractError(
                f"run {run_id} scope is missing or changed; a fresh `dev-orch run` is required"
            )
        global_config = _global_config()
        outcome = execute_run(
            repo,
            config,
            default_registry(config, global_config),
            old.request_text,
            Path(global_config.worktree_root).expanduser(),
            acceptance_criteria=list(old.acceptance_criteria),
            acceptance_criteria_source=old.acceptance_criteria_source,
            tier_override=old.tier_override,
            downgrade_reason=old.downgrade_reason,
            external_plan=plan_path,
            retry_of=run_id,
            retry_source=source,
            continue_patch=continue_patch,
            retry_resolution_reason=review_escalation_reason,
        )
    except EXPECTED_ERRORS + (NoSuchRunError,) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"{outcome.run_id}: {outcome.final_state}")
    if outcome.reason:
        typer.echo(outcome.reason, err=True)
    if outcome.final_state is not RunState.COMPLETE_LOCAL:
        raise typer.Exit(1)


@app.command()
def cancel(
    run_id: str = typer.Argument(..., help="Run id to cancel"),
    reason: str = typer.Option(
        "operator requested cancellation",
        "--reason",
        help="Why the operator is cancelling the run",
    ),
) -> None:
    """Move a non-terminal run to CANCELLED and record the operator's reason."""
    try:
        repo = discover_repo(Path.cwd())
        store = RunStore(repo.root, run_id)
        if not (store.root / "manifest.json").is_file():
            raise NoSuchRunError(f"run {run_id!r} does not exist")
        engine = Engine(store)
        provider_stopped = store.stop_active_provider()
        with store.claim(wait_seconds=10 if provider_stopped else 0):
            outcome = engine.cancel(reason)
            try:
                SchedulerQueue().cancel(repo.root, run_id)
            except OSError as exc:
                store.append_event({"event": "scheduler_cancel_failed", "detail": str(exc)})
                typer.echo(
                    f"{run_id}: cancelled, but scheduled wakeup removal failed: {exc}",
                    err=True,
                )
                raise typer.Exit(1) from exc
            try:
                config = _project_config(repo)
                worktree_root = Path(_global_config().worktree_root).expanduser()
                worktree = worktree_path(worktree_root, config.project.name, run_id)
            except GitCommandError:
                pass  # no project.yaml (yet, or ever) -- cancellation itself still stands
            else:
                cleanup_worktree_if_unproductive(engine, repo, worktree)
        suffix = " (active provider terminated)" if provider_stopped else ""
        typer.echo(f"{run_id}: {outcome.status}{suffix}")
    except EXPECTED_ERRORS + (NoSuchRunError,) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command()
def runs() -> None:
    """List local runs, newest first."""
    try:
        repo = discover_repo(Path.cwd())
        listed = list_runs(repo.root)
        if not listed:
            raise NoSuchRunError("no runs found in this repository")
        for run_id, state in listed:
            typer.echo(f"{run_id}: {state}")
    except EXPECTED_ERRORS + (NoSuchRunError,) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@scheduler_app.command("enable")
def scheduler_enable() -> None:
    """Install the current user's once-per-minute launchd wakeup."""
    try:
        path = enable_launchd()
    except SchedulerError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"scheduler enabled: {path}")


@scheduler_app.command("disable")
def scheduler_disable() -> None:
    """Stop future wakeups while retaining saved runs and due times."""
    disable_launchd()
    typer.echo("scheduler disabled")


@scheduler_app.command("tick", hidden=True)
def scheduler_tick() -> None:
    """Process due entries through the same claimed resume path as a hosted driver."""
    queue = SchedulerQueue()
    due = queue.due()
    if due:
        adopt_login_shell_path()
    for entry in due:
        repo_root = Path(entry["repository"])
        run_id = entry["run_id"]
        try:
            repo = discover_repo(repo_root)
            config = _project_config(repo)
            registry = default_registry(config, _global_config())
            outcome = resume_due(repo, config, registry, run_id, scheduler_queue=queue)
            if isinstance(outcome, CloudDelivery):
                typer.echo(
                    f"{run_id}: cloud delivery {'queued' if outcome.accepted else 'rejected'}"
                )
            else:
                typer.echo(f"{run_id}: {outcome.final_state}")
                if outcome.final_state is not RunState.PAUSED_USAGE:
                    queue.cancel(repo.root, run_id)
        except RunClaimError:
            typer.echo(f"{run_id}: another wakeup owns this run")
        except EXPECTED_ERRORS as exc:
            typer.echo(f"{run_id}: automatic resume stopped: {exc}", err=True)
            store = RunStore(repo_root, run_id)
            if (store.root / "manifest.json").is_file():
                try:
                    with store.claim():
                        manifest = store.read_manifest()
                        if (
                            manifest.status is RunState.PAUSED_USAGE
                            and manifest.auto_resume
                            and manifest.pause is not None
                            and manifest.pause.next_attempt_at is not None
                            and manifest.pause.next_attempt_at > datetime.now(UTC)
                        ):
                            queue.register(repo_root, run_id, manifest.pause.next_attempt_at)
                            continue
                        if manifest.status is RunState.PAUSED_USAGE:
                            store.update_manifest(auto_resume=False)
                            store.append_event({"event": "auto_resume_stopped", "detail": str(exc)})
                        queue.cancel(repo_root, run_id)
                except RunClaimError:
                    typer.echo(f"{run_id}: another wakeup owns this run")
            else:
                queue.cancel(repo_root, run_id)


@app.command()
def usage(
    run_id: str | None = typer.Argument(None, help="Run id; defaults to the newest run"),
    all_runs: bool = typer.Option(False, "--all", help="Aggregate every run in this repository"),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable rows"),
) -> None:
    """Show token usage per role and model, biggest cost sink first."""
    try:
        repo = discover_repo(Path.cwd())
        candidates = [run for run, _ in list_runs(repo.root)]
        if all_runs:
            selected = candidates
        else:
            chosen = run_id or (candidates[0] if candidates else None)
            if chosen is None:
                raise NoSuchRunError("no runs found in this repository")
            if not (repo.root / ".ai" / "runs" / chosen / "manifest.json").is_file():
                raise NoSuchRunError(f"run {chosen!r} does not exist")
            selected = [chosen]
        events = [
            event for run in selected for event in read_events(repo.root / ".ai" / "runs" / run)
        ]
        rows = summarize_token_usage(events)
    except EXPECTED_ERRORS + (NoSuchRunError,) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(rows, indent=2) if as_json else render_token_usage(rows))
