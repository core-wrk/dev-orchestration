import errno
import json
import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from dev_orchestration.adapters.base import AgentTimeoutError
from dev_orchestration.artifacts.store import RunStore
from dev_orchestration.cli import app
from dev_orchestration.config.models import ValidationCommand
from dev_orchestration.domain.enums import ProjectClass, RunState, Tier
from dev_orchestration.domain.run import RunManifest
from dev_orchestration.git.repo import GitRepo
from dev_orchestration.workflow import bootstrap, validation
from dev_orchestration.workflow import runner as runner_module
from dev_orchestration.workflow.engine import Engine
from dev_orchestration.workflow.recovery import resume_due, resume_run
from dev_orchestration.workflow.reporting import read_events, summarize_epics
from dev_orchestration.workflow.scheduler import SchedulerQueue
from tests.workflow import test_resume, test_runner


def test_validation_stop_records_local_actor_and_exhausted_repair(tmp_path):
    git = test_runner.repo(tmp_path)
    config = test_runner.CONFIG.model_copy(
        update={
            "validation": {
                "unit": ValidationCommand(
                    command="echo synthetic-private-evidence >&2; exit 3",
                    required_for=[Tier.STANDARD],
                )
            }
        }
    )
    outcome = test_runner.execute_run(
        git,
        config,
        test_runner.registry(test_runner.Scripted(test_runner.script())),
        "synthetic request",
        tmp_path / "wt",
        test_runner.CRITERIA,
        goal_id="epic-1",
    )
    manifest = RunStore(git.root, outcome.run_id).read_manifest()
    assert manifest.epic_id == manifest.goal_id == "epic-1"
    stop = manifest.stop
    assert (stop.stage, stop.role, stop.provider, stop.model) == (
        RunState.VALIDATING,
        "validator",
        None,
        None,
    )
    assert stop.category == "validation failed"
    assert stop.repairs["validation"].model_dump() == {"used": 1, "allowed": 1, "exhausted": True}
    assert "synthetic-private-evidence" not in stop.model_dump_json()
    events = read_events(outcome.store_root)
    transitions = [event for event in events if event["event"] == "state_changed"]
    assert all({"role", "provider", "model"} <= event.keys() for event in transitions)
    assert any(event["to"] == "REMEDIATION" and event["model"] == "m" for event in transitions)
    assert any(event["event"] == "run_stopped" for event in events)


def test_plan_reconciliation_records_rounds_and_each_call(tmp_path):
    git = test_runner.repo(tmp_path)
    script = test_runner.script()
    script["plan_reviewer"] = [test_runner.BLOCKING]
    outcome = test_runner.execute_run(
        git,
        test_runner.CONFIG,
        test_runner.registry(test_runner.Scripted(script)),
        "synthetic request",
        tmp_path / "wt",
        test_runner.CRITERIA,
    )
    manifest = RunStore(git.root, outcome.run_id).read_manifest()
    assert outcome.final_state is RunState.ESCALATED
    assert manifest.stop.category == "plan rejected"
    assert manifest.stop.role == "plan_reviewer"
    assert manifest.stop.repairs["plan_review"].model_dump() == {
        "used": 2,
        "allowed": 2,
        "exhausted": True,
    }
    calls = [
        event
        for event in read_events(outcome.store_root)
        if event["event"] == "token_usage" and event["role"] == "plan_reconciler"
    ]
    assert [(event["provider"], event["model"], event["attempt"]) for event in calls] == [
        ("fake", "m", 1),
        ("fake", "m", 2),
    ]


def test_bootstrap_failure_keeps_safe_stop_and_epic_id(tmp_path, monkeypatch):
    git = test_runner.repo(tmp_path)

    def fail(*args):
        raise OSError("synthetic private bootstrap detail")

    monkeypatch.setattr(bootstrap, "create_worktree", fail)
    with pytest.raises(OSError, match="synthetic private bootstrap detail"):
        bootstrap.bootstrap_run(
            git, test_runner.CONFIG, "fixture", Tier.STANDARD, tmp_path / "wt", "bootstrap-epic"
        )
    (path,) = RunStore.runs_directory(git.root).glob("*/manifest.json")
    manifest = RunManifest.model_validate_json(path.read_text())
    assert manifest.epic_id == "bootstrap-epic"
    assert manifest.status is RunState.FAILED
    assert manifest.stop.stage is RunState.CREATED
    assert manifest.stop.category == "infrastructure"
    assert "synthetic private bootstrap detail" not in manifest.stop.evidence


@pytest.mark.parametrize("check", ["current_commit", "is_ancestor"])
def test_bootstrap_integrity_error_is_terminal_and_epic_can_retry(tmp_path, monkeypatch, check):
    git = test_runner.repo(tmp_path)
    original = getattr(GitRepo, check)
    calls = 0

    def fail_once(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == (2 if check == "current_commit" else 1):
            raise OSError("synthetic private git diagnostic")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(GitRepo, check, fail_once)
    with pytest.raises(OSError, match="synthetic private git diagnostic"):
        bootstrap.bootstrap_run(
            git, test_runner.CONFIG, "fixture", Tier.STANDARD, tmp_path / "wt", "retryable-epic"
        )

    (path,) = RunStore.runs_directory(git.root).glob("*/manifest.json")
    failed = RunManifest.model_validate_json(path.read_text())
    assert failed.status is RunState.FAILED
    assert failed.completed_at is not None
    assert failed.stop.category == "infrastructure"
    assert "synthetic private git diagnostic" not in failed.stop.evidence
    assert any(event["event"] == "run_cleanup_complete" for event in read_events(path.parent))

    store, _ = bootstrap.bootstrap_run(
        git, test_runner.CONFIG, "fixture retry", Tier.STANDARD, tmp_path / "wt", "retryable-epic"
    )
    assert store.read_manifest().goal_attempt == 2


def test_setup_failure_before_classification_is_terminal_and_epic_can_retry(tmp_path, monkeypatch):
    git = test_runner.repo(tmp_path)
    original = runner_module.classification_references_for_run
    failed_once = False

    def fail_once(*args, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("synthetic private setup diagnostic")
        return original(*args, **kwargs)

    monkeypatch.setattr(runner_module, "classification_references_for_run", fail_once)
    with pytest.raises(RuntimeError, match="synthetic private setup diagnostic"):
        test_runner.execute_run(
            git,
            test_runner.CONFIG,
            test_runner.registry(test_runner.Scripted(test_runner.script())),
            "synthetic setup failure",
            tmp_path / "wt",
            test_runner.CRITERIA,
            goal_id="setup-retryable-epic",
        )

    (path,) = RunStore.runs_directory(git.root).glob("*/manifest.json")
    failed = RunManifest.model_validate_json(path.read_text())
    assert failed.status is RunState.FAILED
    assert failed.completed_at is not None
    assert failed.stop.category == "unknown"
    assert "synthetic private setup diagnostic" not in failed.stop.evidence
    assert any(event["event"] == "run_cleanup_complete" for event in read_events(path.parent))

    outcome = test_runner.execute_run(
        git,
        test_runner.CONFIG,
        test_runner.registry(test_runner.Scripted(test_runner.script())),
        "synthetic setup retry",
        tmp_path / "wt",
        test_runner.CRITERIA,
        goal_id="setup-retryable-epic",
    )
    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert RunStore(git.root, outcome.run_id).read_manifest().goal_attempt == 2


@pytest.mark.parametrize(
    "role, stage, run_state",
    [
        ("planner", "PLANNING", RunState.CLASSIFIED),
        ("plan_reviewer", "PLAN_REVIEW", RunState.PLANNED),
        ("plan_reconciler", "PLAN_RECONCILIATION", RunState.PLANNED),
    ],
)
def test_planning_provider_stop_names_active_operation(tmp_path, role, stage, run_state):
    git = test_runner.repo(tmp_path)
    script = test_runner.script()
    if role == "plan_reconciler":
        script["plan_reviewer"] = [test_runner.BLOCKING]

    class FailingPlanningAdapter(test_runner.Scripted):
        name = "fake"

        def run(self, request):
            if request.role == role:
                raise OSError("synthetic private provider detail")
            return super().run(request)

    outcome = test_runner.execute_run(
        git,
        test_runner.CONFIG,
        test_runner.registry(FailingPlanningAdapter(script)),
        "synthetic planning failure",
        tmp_path / "wt",
        test_runner.CRITERIA,
    )
    stop = RunStore(git.root, outcome.run_id).read_manifest().stop
    assert stop.stage == stage
    assert stop.run_state is run_state
    assert (stop.role, stop.provider, stop.model) == (role, "fake", "m")


def test_resumed_planning_failure_keeps_operation_attribution(tmp_path):
    git = test_runner.repo(tmp_path)

    class ResumePlanningAdapter(test_resume.Scripted):
        def __init__(self):
            super().__init__()
            self.planner_calls = 0

        def run(self, request):
            if request.role == "planner":
                self.planner_calls += 1
                if self.planner_calls == 1:
                    raise AgentTimeoutError(request.role, 1, "fake")
                raise OSError("synthetic private resumed provider detail")
            return super().run(request)

    adapter = ResumePlanningAdapter()
    roles = test_resume.registry(adapter)
    paused = test_resume.execute_run(
        git,
        test_resume.CONFIG,
        roles,
        "synthetic planning pause",
        tmp_path / "wt",
        test_resume.CRITERIA,
    )
    assert paused.final_state is RunState.PAUSED_INTERRUPTED

    resumed = resume_run(git, test_resume.CONFIG, roles, paused.run_id)
    assert resumed.final_state is RunState.ESCALATED
    stop = RunStore(git.root, paused.run_id).read_manifest().stop
    assert (stop.stage, stop.run_state, stop.role, stop.provider, stop.model) == (
        "PLANNING",
        RunState.CLASSIFIED,
        "planner",
        "fake",
        "m",
    )


def test_historical_stop_record_without_run_state_still_parses():
    from dev_orchestration.domain.run import StopRecord

    stop = StopRecord.model_validate(
        {
            "stage": "PLANNED",
            "role": "plan_reviewer",
            "provider": "fake",
            "model": "m",
            "category": "plan rejected",
            "evidence": "plan review blocked the plan",
            "repairs": {},
        }
    )
    assert stop.stage is RunState.PLANNED
    assert stop.run_state is None


def test_manual_resume_records_actor_and_pause(tmp_path):
    git = test_resume.repo(tmp_path)
    paused = test_resume.execute_run(
        git,
        test_resume.CONFIG,
        test_resume.registry(test_resume.Scripted(timeout_worker=True)),
        "synthetic request",
        tmp_path / "wt",
        test_resume.CRITERIA,
        goal_id="resume-epic",
    )
    store = RunStore(git.root, paused.run_id)
    stop = store.read_manifest().stop
    assert stop.category == "infrastructure"
    assert (stop.role, stop.model) == ("implementation_worker", "m")
    completed = resume_run(
        git, test_resume.CONFIG, test_resume.registry(test_resume.Scripted()), paused.run_id
    )
    assert completed.final_state is RunState.COMPLETE_LOCAL
    assert store.read_manifest().stop is None
    (action,) = [e for e in read_events(store.root) if e["event"] == "human_action"]
    assert action["action"] == "resume"
    assert action["actor"]
    assert datetime.fromisoformat(action["ts"]).tzinfo is not None
    assert summarize_epics(git.root)["completed_with_human_intervention"] == 1


def test_scheduled_resume_counts_as_autonomous(tmp_path):
    git = test_resume.repo(tmp_path)
    queue = SchedulerQueue(tmp_path / "queue.json")
    paused = test_resume.execute_run(
        git,
        test_resume.CONFIG,
        test_resume.registry(test_resume.QuotaAtThirdReview()),
        "synthetic request",
        tmp_path / "wt",
        test_resume.CRITERIA,
        auto_resume=True,
        scheduler_queue=queue,
    )
    assert paused.final_state is RunState.PAUSED_USAGE
    result = resume_due(
        git,
        test_resume.CONFIG,
        test_resume.registry(test_resume.Scripted()),
        paused.run_id,
        now=datetime.now(UTC) + timedelta(minutes=2),
        scheduler_queue=queue,
    )
    assert result.final_state is RunState.COMPLETE_LOCAL
    assert all(e["event"] != "human_action" for e in read_events(paused.store_root))
    assert summarize_epics(git.root)["completed_without_human_intervention"] == 1


def test_manual_rerun_and_escalation_answer_are_recorded_on_child(tmp_path):
    git = test_runner.repo(tmp_path)
    rejected = test_runner.script(review=dict(test_runner.PASS, outcome="ESCALATE"))

    def run(script, **kwargs):
        return test_runner.execute_run(
            git,
            test_runner.CONFIG,
            test_runner.registry(test_runner.Scripted(script)),
            "synthetic request",
            tmp_path / "wt",
            test_runner.CRITERIA,
            goal_id="escalation-epic",
            **kwargs,
        )

    source = run(rejected)
    assert source.final_state is RunState.ESCALATED
    child = run(
        test_runner.script(), retry_of=source.run_id, retry_resolution_reason="synthetic answer"
    )
    assert child.final_state is RunState.COMPLETE_LOCAL
    actions = [e["action"] for e in read_events(child.store_root) if e["event"] == "human_action"]
    assert actions == ["rerun", "escalation_answer"]
    assert summarize_epics(git.root)["completed_with_human_intervention"] == 1


@pytest.mark.parametrize(
    "role, status",
    [
        ("implementation_reviewer", RunState.BLOCKED),
        ("verifier", RunState.FAILED),
    ],
)
def test_review_stops_attribute_the_rejecting_role(tmp_path, role, status):
    git = test_runner.repo(tmp_path)
    script = test_runner.script()
    if role == "implementation_reviewer":
        script[role] = [dict(test_runner.PASS, outcome="BLOCKED")]
    else:
        script[role] = [
            dict(test_runner.VERIFY, outcome="CHANGES_REQUIRED", unresolved_finding_ids=["F1"])
        ]
    outcome = test_runner.execute_run(
        git,
        test_runner.CONFIG,
        test_runner.registry(test_runner.Scripted(script)),
        "synthetic request",
        tmp_path / "wt",
        test_runner.CRITERIA,
    )
    assert outcome.final_state is status
    stop = RunStore(git.root, outcome.run_id).read_manifest().stop
    assert (stop.role, stop.model, stop.category) == (role, "m", "reviewer rejected")


def _run_record(root, run_id, epic, state, created, *, legacy=False):
    store = RunStore(root, run_id)
    store.initialize(
        RunManifest(
            run_id=run_id,
            repository="synthetic",
            workflow="standard",
            tier=Tier.STANDARD,
            project_class=ProjectClass.INTERNAL_UTILITY,
            created_at=created,
            goal_id=epic,
            epic_id=None if legacy else epic,
        )
    )
    store.append_event({"event": "run_branch_reserved"})
    store.update_manifest(
        status=state,
        completed_at=created + timedelta(minutes=5) if state is RunState.COMPLETE_LOCAL else None,
    )
    return store


def test_epic_report_uses_first_completion_and_keeps_legacy_coverage_unknown(tmp_path, monkeypatch):
    git = test_runner.repo(tmp_path)
    root = git.root
    now = datetime.now(UTC)
    old = now - timedelta(hours=1)
    _run_record(root, "complete", "automatic", RunState.COMPLETE_LOCAL, old)
    _run_record(root, "later", "automatic", RunState.FAILED, now).record_human_action("rerun")
    assisted = _run_record(root, "assisted", "manual", RunState.FAILED, old)
    assisted.append_event(
        {"event": "human_action", "action": "edit", "actor": "fixture", "ts": old.isoformat()}
    )
    _run_record(root, "assisted-complete", "manual", RunState.COMPLETE_LOCAL, now)
    _run_record(root, "pending", "pending", RunState.FAILED, now)
    legacy = _run_record(root, "old", "mixed", RunState.FAILED, old, legacy=True)
    before = (legacy.root / "manifest.json").read_bytes()
    _run_record(root, "mixed-complete", "mixed", RunState.COMPLETE_LOCAL, now)
    _run_record(root, "unlinked", None, RunState.FAILED, old, legacy=True)
    monkeypatch.chdir(root)
    result = CliRunner().invoke(app, ["epic-report", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "total_linked_epics": 4,
        "completed_without_human_intervention": 1,
        "completed_with_human_intervention": 1,
        "incomplete_epics": 1,
        "unknown_coverage_epics": 1,
        "unlinked_runs": 1,
    }
    assert (legacy.root / "manifest.json").read_bytes() == before


def test_record_edit_and_cancel_append_action_and_keep_reason_out_of_stop(tmp_path, monkeypatch):
    git = test_runner.repo(tmp_path)
    store = _run_record(git.root, "run", "edit", RunState.CREATED, datetime.now(UTC))
    monkeypatch.chdir(git.root)
    result = CliRunner().invoke(app, ["record-edit", "run"])
    assert result.exit_code == 0, result.output
    Engine(store).cancel("synthetic private cancellation detail")
    stop = store.read_manifest().stop
    assert stop.category == "operator cancelled"
    assert "synthetic private cancellation detail" not in stop.evidence
    actions = [e["action"] for e in read_events(store.root) if e["event"] == "human_action"]
    assert actions == ["edit", "cancel"]


def test_validation_retries_only_the_failed_launch(tmp_path, monkeypatch):
    launched = []
    delays = []

    def shell(command, **kwargs):
        launched.append(command[-1])
        if launched.count("retry") == 1 and command[-1] == "retry":
            raise BlockingIOError(errno.EAGAIN, "synthetic launch failure")
        return subprocess.CompletedProcess(command, 3 if command[-1] == "retry" else 0, "", "")

    monkeypatch.setattr(validation.subprocess, "run", shell)
    monkeypatch.setattr(validation, "sleep", delays.append)
    commands = {
        name: ValidationCommand(command=name, required_for=[Tier.STANDARD])
        for name in ("pass", "retry", "next")
    }
    outcomes = validation.run_validations(commands, Tier.STANDARD, tmp_path)
    assert launched == ["pass", "retry", "retry", "next"]
    assert delays == [1]
    assert [outcome.exit_code for outcome in outcomes] == [0, 3, 0]
