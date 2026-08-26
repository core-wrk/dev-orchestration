import subprocess

import yaml
from typer.testing import CliRunner

from dev_orchestration.cli import app


def test_help_exits_zero():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Usage" in result.stdout


def _init_git_repo(root):
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "T"], check=True)


def test_init_writes_the_contract_into_the_repository(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump({"project": {"name": "helmfast-site", "class": "marketing_website"}})
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(app, ["init", "--config", str(config_file)])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "AGENTS.md").is_file()
    assert (tmp_path / ".ai" / "project.yaml").is_file()
    assert (tmp_path / ".ai" / "context.md").is_file()
    assert "wrote AGENTS.md" in result.output


def test_init_force_never_regenerates_context_md(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump({"project": {"name": "helmfast-site", "class": "marketing_website"}})
    )
    monkeypatch.chdir(tmp_path)

    CliRunner().invoke(app, ["init", "--config", str(config_file)])
    context_path = tmp_path / ".ai" / "context.md"
    context_path.write_text("Hand-edited: needs VPN.\n")

    result = CliRunner().invoke(app, ["init", "--config", str(config_file), "--force"])

    assert result.exit_code == 0, result.output
    assert context_path.read_text() == "Hand-edited: needs VPN.\n"
    assert "context.md" not in result.output
