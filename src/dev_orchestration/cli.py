"""Typer entry point. Commands register here; logic lives in modules."""

from pathlib import Path

import typer

from dev_orchestration import doctor as doctor_module

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
