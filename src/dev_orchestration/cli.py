"""Typer entry point. Commands register here; logic lives in modules."""

from pathlib import Path

import typer
import yaml

from dev_orchestration import doctor as doctor_module
from dev_orchestration.config.models import ProjectConfig
from dev_orchestration.git.repo import discover_repo
from dev_orchestration.init_repo import initialize_repo

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
    repo = discover_repo(Path.cwd())
    config = ProjectConfig.model_validate(yaml.safe_load(config_file.read_text()))
    for path in initialize_repo(repo.root, config, force=force):
        typer.echo(f"wrote {path.relative_to(repo.root)}")
