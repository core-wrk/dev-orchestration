import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dev_orchestration.adapters.base import AgentResult
from dev_orchestration.adapters.usage import UsageObservation
from dev_orchestration.artifacts.store import RunClaimError, RunStore
from dev_orchestration.domain.enums import RunState
from dev_orchestration.workflow.recovery import ResumeRefused, resume_blocker, resume_due
from dev_orchestration.workflow.runner import execute_run
from dev_orchestration.workflow.scheduler import (
    SchedulerError,
    SchedulerQueue,
    disable_launchd,
    enable_launchd,
)
from tests.workflow.test_resume import (
    CONFIG,
    CRITERIA,
    QuotaAtThirdReview,
    Scripted,
    registry,
    repo,
)


def test_concurrent_due_wakeups_invoke_one_continuation(tmp_path):
    git = repo(tmp_path)
    queue = SchedulerQueue(tmp_path / "queue.json")
    first = execute_run(
        git,
        CONFIG,
        registry(QuotaAtThirdReview()),
        "change app",
        tmp_path / "wt",
        CRITERIA,
        auto_resume=True,
        scheduler_queue=queue,
    )
    assert first.final_state is RunState.PAUSED_USAGE
    adapter = Scripted()
    due = datetime.now(UTC) + timedelta(minutes=2)

    def wake():
        try:
            return resume_due(
                git,
                CONFIG,
                registry(adapter),
                first.run_id,
                now=due,
                scheduler_queue=queue,
            )
        except (RunClaimError, ResumeRefused) as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: wake(), range(2)))
    assert (
        sum(getattr(result, "final_state", None) is RunState.COMPLETE_LOCAL for result in results)
        == 1
    )
    assert adapter.seen.count("plan_reviewer") == 1
    assert queue.due(due) == []


def test_hosted_wakeup_in_new_process_uses_restored_worktree_path(tmp_path):
    git = repo(tmp_path)
    queue_path = tmp_path / "queue.json"
    first = execute_run(
        git,
        CONFIG,
        registry(QuotaAtThirdReview()),
        "change app",
        tmp_path / "wt",
        CRITERIA,
        auto_resume=True,
        scheduler_queue=SchedulerQueue(queue_path),
    )
    assert first.final_state is RunState.PAUSED_USAGE
    original = RunStore(git.root, first.run_id).read_manifest().git.worktree
    restored = tmp_path / "restored_worktree"
    subprocess.run(
        ["git", "-C", str(git.root), "worktree", "move", original, str(restored)],
        check=True,
    )
    script = """
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from dev_orchestration.git.repo import GitRepo
from dev_orchestration.workflow.recovery import resume_due
from dev_orchestration.workflow.scheduler import SchedulerQueue
from tests.workflow.test_resume import CONFIG, Scripted, registry
repo, run_id, checkout, queue = sys.argv[1:]
outcome = resume_due(GitRepo(Path(repo)), CONFIG, registry(Scripted()), run_id, now=datetime.now(UTC) + timedelta(minutes=2), restored_worktree=Path(checkout), scheduler_queue=SchedulerQueue(Path(queue)))
print(outcome.final_state)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(git.root), first.run_id, str(restored), str(queue_path)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "COMPLETE_LOCAL"
    assert RunStore(git.root, first.run_id).read_manifest().status is RunState.COMPLETE_LOCAL


def test_second_quota_rejection_updates_same_run_and_due_time(tmp_path):
    class RejectReviewAgain(Scripted):
        def run(self, request):
            if request.role == "plan_reviewer":
                self.seen.append(request.role)
                now = datetime.now(UTC)
                return AgentResult(
                    self.name,
                    None,
                    0,
                    "",
                    now,
                    now,
                    diagnostic={
                        "type": "error",
                        "error": "five-hour usage limit reached",
                        "reset_at": (now + timedelta(minutes=30)).isoformat(),
                    },
                )
            return super().run(request)

    git = repo(tmp_path)
    queue = SchedulerQueue(tmp_path / "queue.json")
    first = execute_run(
        git,
        CONFIG,
        registry(QuotaAtThirdReview()),
        "change app",
        tmp_path / "wt",
        CRITERIA,
        auto_resume=True,
        scheduler_queue=queue,
    )
    assert first.final_state is RunState.PAUSED_USAGE
    assert RunStore(git.root, first.run_id).read_manifest().pause.retry_count == 0
    original_plan = (first.store_root / "planning/plan-v3.md").read_bytes()
    original_reviews = list((first.store_root / "review").glob("plan-review-v*.json"))
    adapter = RejectReviewAgain()
    outcome = resume_due(
        git,
        CONFIG,
        registry(adapter),
        first.run_id,
        now=datetime.now(UTC) + timedelta(minutes=2),
        scheduler_queue=queue,
    )
    assert outcome.final_state is RunState.PAUSED_USAGE
    assert outcome.run_id == first.run_id
    assert adapter.seen == ["plan_reviewer"]
    manifest = RunStore(git.root, first.run_id).read_manifest()
    assert manifest.pause.retry_count == 1
    assert (first.store_root / "planning/plan-v3.md").read_bytes() == original_plan
    assert list((first.store_root / "review").glob("plan-review-v*.json")) == original_reviews
    assert queue.due(manifest.pause.next_attempt_at + timedelta(seconds=1))


def test_scheduler_queue_survives_recreation_and_cancel(tmp_path):
    path = tmp_path / "queue.json"
    due = datetime.now(UTC) - timedelta(seconds=1)
    SchedulerQueue(path).register(tmp_path, "run-1", due)
    assert [item["run_id"] for item in SchedulerQueue(path).due()] == ["run-1"]
    SchedulerQueue(path).cancel(tmp_path, "run-1")
    assert SchedulerQueue(path).due() == []


def test_failed_launchd_enable_does_not_leave_a_future_wakeup(tmp_path, monkeypatch):
    plist = tmp_path / "scheduler.plist"
    monkeypatch.setattr("shutil.which", lambda _name: "/bin/dev-orch")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", "bootstrap failed"),
    )
    with pytest.raises(SchedulerError, match="bootstrap failed"):
        enable_launchd(plist)
    assert not plist.exists()


def test_failed_launchd_disable_keeps_job_file_for_retry(tmp_path, monkeypatch):
    plist = tmp_path / "scheduler.plist"
    plist.write_text("job")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", "bootout failed"),
    )
    with pytest.raises(SchedulerError, match="bootout failed"):
        disable_launchd(plist)
    assert plist.read_text() == "job"


def test_account_change_refuses_due_wakeup_before_provider_call(tmp_path):
    git = repo(tmp_path)
    queue = SchedulerQueue(tmp_path / "queue.json")
    first = execute_run(
        git,
        CONFIG,
        registry(QuotaAtThirdReview()),
        "change app",
        tmp_path / "wt",
        CRITERIA,
        auto_resume=True,
        scheduler_queue=queue,
    )
    store = RunStore(git.root, first.run_id)
    pause = store.read_manifest().pause
    store.update_manifest(pause=pause.model_copy(update={"account_id": "original"}))

    class ChangedAccount(Scripted):
        def observe_usage(self):
            return UsageObservation(provider="fake", account_id="different")

    adapter = ChangedAccount()
    with pytest.raises(ResumeRefused, match="account changed"):
        resume_due(
            git,
            CONFIG,
            registry(adapter),
            first.run_id,
            now=datetime.now(UTC) + timedelta(minutes=2),
            scheduler_queue=queue,
        )
    assert adapter.seen == []


def test_missing_authentication_refuses_due_wakeup_before_provider_call(tmp_path):
    class LoggedOut(Scripted):
        def authenticated(self):
            return False

    git = repo(tmp_path)
    queue = SchedulerQueue(tmp_path / "queue.json")
    first = execute_run(
        git,
        CONFIG,
        registry(QuotaAtThirdReview()),
        "change app",
        tmp_path / "wt",
        CRITERIA,
        auto_resume=True,
        scheduler_queue=queue,
    )
    adapter = LoggedOut()
    assert "not signed in" in resume_blocker(git, CONFIG, registry(adapter), first.run_id)
    with pytest.raises(ResumeRefused, match="not signed in"):
        resume_due(
            git,
            CONFIG,
            registry(adapter),
            first.run_id,
            now=datetime.now(UTC) + timedelta(minutes=2),
            scheduler_queue=queue,
        )
    assert adapter.seen == []
