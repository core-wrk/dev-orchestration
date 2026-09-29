import hashlib
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from dev_orchestration.artifacts.store import RunClaimError, RunStore
from dev_orchestration.domain.enums import RunState
from dev_orchestration.git.repo import GitRepo
from dev_orchestration.workflow.cloud import (
    ClaudeCloudGateway,
    CloudDelivery,
    CloudInspection,
    CodexCloudGateway,
    link_cloud_session,
    resume_cloud_due,
)
from dev_orchestration.workflow.recovery import ResumeRefused, resume_due, resume_run
from dev_orchestration.workflow.runner import execute_run
from dev_orchestration.workflow.scheduler import SchedulerQueue
from tests.workflow.test_resume import (
    CONFIG,
    CRITERIA,
    QuotaAtThirdReview,
    Scripted,
    registry,
    repo,
)


class FakeCloudGateway:
    def __init__(self, inspection):
        self.inspection = inspection
        self.messages = []

    def inspect(self, session):
        return self.inspection

    def deliver(self, session, message):
        self.messages.append(message)
        return {"ok": True, "session_id": session.session_id}

    def read_delivery(self, raw, session):
        return CloudDelivery(raw["ok"], raw["session_id"])


def paused_cloud_run(tmp_path):
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
    session = link_cloud_session(git, first.run_id, "claude", "session_test", "env_test")
    store = RunStore(git.root, first.run_id)
    store.update_manifest(
        cloud_session=session.model_copy(
            update={
                "continuation_verified": True,
            }
        )
    )
    pause = store.read_manifest().pause
    inspection = CloudInspection(
        True,
        True,
        True,
        worktree_digest=pause.worktree_digest,
        branch=session.branch,
        environment_id=session.environment_id,
    )
    return git, first, store, queue, inspection


def test_fake_cloud_delivery_uses_same_session_and_checkpoint(tmp_path):
    git, first, store, queue, inspection = paused_cloud_run(tmp_path)
    gateway = FakeCloudGateway(inspection)
    due = datetime.now(UTC) + timedelta(minutes=2)
    result = resume_cloud_due(git, first.run_id, gateway, now=due)
    assert result.accepted
    assert len(gateway.messages) == 1
    digest = hashlib.sha256(
        (store.root / store.read_manifest().checkpoint).read_bytes()
    ).hexdigest()
    assert f"dev-orch resume {first.run_id} --checkout <RUN_WORKTREE_PATH>" in gateway.messages[0]
    assert f"--checkpoint-digest {digest}" in gateway.messages[0]
    assert "git push" not in gateway.messages[0]
    with pytest.raises(ResumeRefused, match="already queued"):
        resume_cloud_due(git, first.run_id, gateway, now=due)
    adapter = Scripted()
    outcome = resume_run(
        git,
        CONFIG,
        registry(adapter),
        first.run_id,
        expected_checkpoint_digest=digest,
        scheduler_queue=queue,
    )
    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert adapter.seen[0] == "plan_reviewer"


def test_hosted_and_local_cloud_wakeups_deliver_once(tmp_path):
    git, first, _store, queue, inspection = paused_cloud_run(tmp_path)
    gateway = FakeCloudGateway(inspection)
    due = datetime.now(UTC) + timedelta(minutes=2)

    def wake():
        try:
            return resume_due(
                git,
                CONFIG,
                registry(Scripted()),
                first.run_id,
                now=due,
                scheduler_queue=queue,
                cloud_gateway=gateway,
            )
        except (RunClaimError, ResumeRefused) as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: wake(), range(2)))
    assert sum(isinstance(result, CloudDelivery) and result.accepted for result in results) == 1
    assert len(gateway.messages) == 1


def test_missing_cloud_session_stops_retry_without_losing_checkpoint(tmp_path):
    class MissingSession(FakeCloudGateway):
        def read_delivery(self, raw, session):
            return CloudDelivery(False, session.session_id, "Session not found", True)

    git, first, store, queue, inspection = paused_cloud_run(tmp_path)
    due = datetime.now(UTC) + timedelta(minutes=2)
    checkpoint = store.read_manifest().checkpoint
    result = resume_due(
        git,
        CONFIG,
        registry(Scripted()),
        first.run_id,
        now=due,
        scheduler_queue=queue,
        cloud_gateway=MissingSession(inspection),
    )
    assert not result.accepted
    manifest = store.read_manifest()
    assert manifest.status is RunState.PAUSED_USAGE
    assert manifest.checkpoint == checkpoint
    assert manifest.auto_resume is False
    assert manifest.pause.next_attempt_at is None
    assert manifest.cloud_session.last_state == "missing"
    assert queue.due(due + timedelta(minutes=10)) == []


def test_cloud_delivery_cannot_substitute_a_new_session(tmp_path):
    class SubstitutedSession(FakeCloudGateway):
        def read_delivery(self, raw, session):
            return CloudDelivery(True, "another_session")

    git, first, store, queue, inspection = paused_cloud_run(tmp_path)
    with pytest.raises(ResumeRefused, match="changed the linked session ID"):
        resume_due(
            git,
            CONFIG,
            registry(Scripted()),
            first.run_id,
            now=datetime.now(UTC) + timedelta(minutes=2),
            scheduler_queue=queue,
            cloud_gateway=SubstitutedSession(inspection),
        )
    assert store.read_manifest().cloud_session.last_delivery_digest is None


@pytest.mark.parametrize("failure", ["expired", "archived", "missing", "snapshot", "branch"])
def test_cloud_refusals_do_not_deliver_or_modify_worktree(tmp_path, failure):
    git, first, store, _queue, inspection = paused_cloud_run(tmp_path)
    original = (tmp_path / "wt" / "demo" / first.run_id / "src/app.py").read_text()
    if failure == "expired":
        inspection = CloudInspection(True, True, False, "environment expired")
    elif failure == "archived":
        session = store.read_manifest().cloud_session
        store.update_manifest(cloud_session=session.model_copy(update={"last_state": "archived"}))
    elif failure == "missing":
        (store.root / "planning/plan-v3.md").unlink()
    elif failure == "snapshot":
        inspection = CloudInspection(
            True,
            True,
            True,
            worktree_digest="wrong",
            branch=inspection.branch,
            environment_id=inspection.environment_id,
        )
    elif failure == "branch":
        inspection = CloudInspection(
            True,
            True,
            True,
            worktree_digest=inspection.worktree_digest,
            branch="wrong",
            environment_id=inspection.environment_id,
        )
    gateway = FakeCloudGateway(inspection)
    with pytest.raises(ResumeRefused, match="."):
        resume_cloud_due(git, first.run_id, gateway, now=datetime.now(UTC) + timedelta(minutes=2))
    assert gateway.messages == []
    assert (tmp_path / "wt" / "demo" / first.run_id / "src/app.py").read_text() == original


def test_codex_cloud_capability_and_claude_cli_delivery(monkeypatch):
    codex = CodexCloudGateway()
    assert "unavailable" in codex.inspect(None).reason
    with pytest.raises(ResumeRefused, match="unavailable"):
        codex.deliver(None, "resume")
    commands = []

    def fake_run(argv, **kwargs):
        commands.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, '{"ok": true, "session_id": "session_test"}', ""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    claude = ClaudeCloudGateway()
    from dev_orchestration.domain.run import CloudSession

    session = CloudSession(
        provider="claude",
        session_id="session_test",
        repository="repo",
        branch="branch",
        environment_id="env",
    )
    delivery = claude.read_delivery(claude.deliver(session, "resume run"), session)
    assert delivery.accepted
    assert commands == [
        ["claude", "-p", "resume run", "--cloud", "session_test", "--output-format", "json"]
    ]
    missing = claude.read_delivery(
        subprocess.CompletedProcess(
            [], 1, '{"ok": false, "session_id": "session_test", "error": "Session not found"}', ""
        ),
        session,
    )
    assert missing.permanent_failure


def test_restored_cloud_checkout_path_uses_saved_run_artifacts(tmp_path):
    git, first, store, queue, inspection = paused_cloud_run(tmp_path)
    gateway = FakeCloudGateway(inspection)
    due = datetime.now(UTC) + timedelta(minutes=2)
    delivery = resume_cloud_due(git, first.run_id, gateway, now=due)
    assert delivery.accepted
    digest = hashlib.sha256(
        (store.root / store.read_manifest().checkpoint).read_bytes()
    ).hexdigest()
    restored = tmp_path / "restored_control"
    subprocess.run(
        [
            "git",
            "clone",
            "-q",
            str(git.root),
            str(restored),
        ],
        check=True,
    )
    shutil.copytree(store.root, restored / ".ai" / "runs" / first.run_id)
    restored_worktree = tmp_path / "restored_worker"
    subprocess.run(
        [
            "git",
            "-C",
            str(restored),
            "worktree",
            "add",
            "-q",
            "-b",
            store.read_manifest().git.branch,
            str(restored_worktree),
            "origin/" + store.read_manifest().git.branch,
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(restored), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(restored), "config", "user.name", "T"], check=True)
    adapter = Scripted()
    outcome = resume_run(
        GitRepo(restored),
        CONFIG,
        registry(adapter),
        first.run_id,
        restored_worktree=restored_worktree,
        expected_checkpoint_digest=digest,
        scheduler_queue=queue,
    )
    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert adapter.seen[0] == "plan_reviewer"
