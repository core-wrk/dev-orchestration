"""Workers see the validation links (such as .venv) while they run, so they can self-check."""

import subprocess

from dev_orchestration.domain.enums import RunState
from dev_orchestration.workflow.recovery import resume_run
from tests.workflow import test_resume
from tests.workflow.run_helpers import execute_run
from tests.workflow.test_phase2 import Varying, _config
from tests.workflow.test_runner import CRITERIA, registry, repo, script


def _with_venv(git):
    """Give the base checkout an ignored .venv holding a stand-in tool."""
    tool = git.root / ".venv" / "bin" / "tool"
    tool.parent.mkdir(parents=True)
    tool.write_text("#!/bin/sh\necho tool-ran\n")
    tool.chmod(0o755)
    (git.root / ".gitignore").write_text(".ai/runs/\n.venv\n")
    subprocess.run(["git", "-C", str(git.root), "commit", "-qam", "ignore venv"], check=True)
    return git


class Seen:
    def __init__(self):
        self.calls = []

    def __call__(self, request, call):
        venv = request.cwd / ".venv"
        self.calls.append((venv.is_symlink(), (venv / "bin" / "tool").is_file()))


def test_every_worker_call_can_use_the_linked_venv_and_the_link_is_gone_after(tmp_path):
    git = _with_venv(repo(tmp_path))
    seen = Seen()
    # Validation passes only from the second worker call on: one implementation, one repair.
    adapter = Varying(script(), extra=seen)
    outcome = execute_run(
        git,
        _config("grep -qx 'x = 3' src/app.py"),
        registry(adapter),
        "go",
        tmp_path / "wt",
        CRITERIA,
    )

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert seen.calls == [(True, True), (True, True)]
    worktree = next((tmp_path / "wt").glob("*/*"))
    assert not (worktree / ".venv").exists() and not (worktree / ".venv").is_symlink()


def test_a_worker_without_a_base_venv_runs_as_before(tmp_path):
    seen = Seen()
    adapter = Varying(script(), extra=seen)
    outcome = execute_run(
        repo(tmp_path), _config("true"), registry(adapter), "go", tmp_path / "wt", CRITERIA
    )

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert seen.calls == [(False, False)]


def test_a_resumed_worker_also_gets_the_link(tmp_path):
    git = _with_venv(test_resume.repo(tmp_path))
    first = execute_run(
        git,
        test_resume.CONFIG,
        test_resume.registry(test_resume.Scripted(timeout_worker=True)),
        "change app",
        tmp_path / "wt",
        test_resume.CRITERIA,
    )

    class Recording(test_resume.Scripted):
        linked = None

        def run(self, request):
            if request.role == "implementation_worker":
                Recording.linked = (request.cwd / ".venv" / "bin" / "tool").is_file()
            return super().run(request)

    resumed = resume_run(git, test_resume.CONFIG, test_resume.registry(Recording()), first.run_id)

    assert resumed.final_state is RunState.COMPLETE_LOCAL
    assert Recording.linked is True
