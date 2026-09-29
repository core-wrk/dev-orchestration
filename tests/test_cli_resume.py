import hashlib

from typer.testing import CliRunner

from dev_orchestration import cli
from dev_orchestration.config.models import GlobalConfig
from dev_orchestration.domain.enums import RunState
from dev_orchestration.git.repo import GitRepo
from dev_orchestration.workflow.runner import execute_run
from tests.test_cli_run import repo
from tests.workflow.test_resume import CRITERIA, Scripted, registry


def test_status_and_resume_command_preserve_saved_run(tmp_path, monkeypatch):
    root = repo(tmp_path / "repo")
    git = GitRepo(root)
    config = cli._project_config(git)
    first = execute_run(
        git,
        config,
        registry(Scripted(timeout_worker=True)),
        "change app",
        tmp_path / "wt",
        CRITERIA,
    )
    assert first.final_state is RunState.PAUSED_INTERRUPTED
    adapter = Scripted()
    monkeypatch.setattr(cli, "default_registry", lambda *_: registry(adapter))
    monkeypatch.chdir(root)
    runner = CliRunner()
    status = runner.invoke(cli.app, ["status", first.run_id])
    assert status.exit_code == 0
    assert "Next role: implementation_worker" in status.stdout
    assert "Manual action" in status.stdout
    receipt_path = first.store_root / cli.RunStore(root, first.run_id).read_manifest().checkpoint
    bad = runner.invoke(cli.app, ["resume", first.run_id, "--checkpoint-digest", "0" * 64])
    assert bad.exit_code == 1
    assert adapter.seen == []
    digest = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    resumed = runner.invoke(cli.app, ["resume", first.run_id, "--checkpoint-digest", digest])
    assert resumed.exit_code == 0, resumed.output
    assert "COMPLETE_LOCAL" in resumed.output
    assert adapter.seen[0] == "implementation_worker"


def test_run_auto_resume_option_is_saved(tmp_path, monkeypatch):
    root = repo(tmp_path / "repo")
    adapter = Scripted()
    monkeypatch.setattr(cli, "default_registry", lambda *_: registry(adapter))
    monkeypatch.setattr(
        cli, "_global_config", lambda: GlobalConfig(worktree_root=str(tmp_path / "wt"))
    )
    monkeypatch.chdir(root)
    result = CliRunner().invoke(
        cli.app, ["run", "change app", "--criterion", CRITERIA[0], "--auto-resume"]
    )
    assert result.exit_code == 0, result.output
    run_id = result.output.split(":", 1)[0].splitlines()[-1]
    assert cli.RunStore(root, run_id).read_manifest().auto_resume is True
