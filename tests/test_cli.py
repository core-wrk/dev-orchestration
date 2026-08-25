from typer.testing import CliRunner

from dev_orchestration.cli import app


def test_help_exits_zero():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Usage" in result.stdout
