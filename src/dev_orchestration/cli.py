"""Typer entry point. Commands register here; logic lives in modules."""

import typer

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Local-first orchestration for AI-assisted development.",
)


@app.callback(invoke_without_command=True)
def main() -> None:
    """Local-first orchestration for AI-assisted development."""
    pass
