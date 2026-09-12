"""Typer entry point. Commands register here; logic lives in modules."""

from pathlib import Path

import typer
import yaml

from dev_orchestration import doctor as doctor_module
from dev_orchestration.adapters.registry import ReadOnlyRoleUnsupportedError, default_registry
from dev_orchestration.artifacts.store import NoApprovedPlanError
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
from dev_orchestration.init_repo import (
    FileExistsRefusal,
    UncommittedContractFileError,
    initialize_repo,
)
from dev_orchestration.roles.loader import MissingTemplateError
from dev_orchestration.workflow.bootstrap import BootstrapIntegrityError
from dev_orchestration.workflow.engine import IllegalTransitionError
from dev_orchestration.workflow.invoke import AgentInvocationError, SchemaEscalation
from dev_orchestration.workflow.reporting import describe_status, list_runs, read_events
from dev_orchestration.workflow.runner import execute_run
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
)

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Local-first orchestration for AI-assisted development.",
)


@app.callback(invoke_without_command=True)
def main() -> None:
    """Local-first orchestration for AI-assisted development."""


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
        from dev_orchestration.domain.run import RunManifest

        manifest = RunManifest.model_validate_json(
            (store_root / "manifest.json").read_text(encoding="utf-8")
        )
        typer.echo(describe_status(manifest, read_events(store_root)))
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
