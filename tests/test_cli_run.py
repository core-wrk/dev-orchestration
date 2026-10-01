import hashlib
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

import dev_orchestration.cli as cli_module
from dev_orchestration.adapters.base import AdapterStatus, AgentResult
from dev_orchestration.adapters.registry import RoleRegistry
from dev_orchestration.artifacts.store import PlanOverwriteError, RunStore
from dev_orchestration.cli import app
from dev_orchestration.config.models import RoleConfig
from dev_orchestration.domain.enums import ProjectClass, RunState, Tier
from dev_orchestration.domain.run import RunManifest
from dev_orchestration.workflow.reporting import describe_status, list_runs
from dev_orchestration.workflow.runner import execute_run as runner_execute_run

PLAN = "# external plan\n"
PASS = {"outcome": "PASS", "findings": []}
ROLES = (
    "classifier",
    "planner",
    "plan_reviewer",
    "plan_reconciler",
    "implementation_worker",
    "implementation_reviewer",
    "verifier",
)


class ScriptedAdapter:
    def __init__(self, tier="standard", profiles=None):
        self.seen = []
        self.requests = []
        self.tier = tier
        self.profiles = profiles or []

    def healthcheck(self):
        return AdapterStatus(name="fake", available=True)

    def supports(self, capability):
        return capability in {"exec", "read_only_review"}

    def run(self, request):
        self.seen.append(request.role)
        self.requests.append(request)
        if request.role == "implementation_worker":
            (request.cwd / "src" / "app.py").write_text("x = 2\n")
        if request.role == "verifier":
            criteria = _criteria_from(request)
            payload = {
                "outcome": "PASS",
                "criteria": criteria,
                "verdicts": [
                    {"criterion": item, "verdict": "PASS", "evidence": "seen"} for item in criteria
                ],
                "unresolved_finding_ids": [],
            }
        else:
            payload = {
                "classifier": {
                    "tier": self.tier,
                    "rationale": "test",
                    "profiles": self.profiles,
                },
                "planner": PLAN,
                "plan_reviewer": PASS,
                "plan_reconciler": PLAN,
                "implementation_worker": "done",
                "implementation_reviewer": PASS,
            }[request.role]
        now = datetime.now(UTC)
        return AgentResult("fake", None, 0, payload, now, now)


def _criteria_from(request):
    """Read the acceptance criteria out of the verifier packet."""
    for item in request.context.items:
        if item.label == "acceptance_criteria":
            return [line for line in item.content.splitlines() if line]
    return []


def registry(adapter):
    return RoleRegistry(
        roles={role: RoleConfig(adapter="fake", model="m") for role in ROLES},
        adapters={"fake": adapter},
    )


def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], check=True)
    (tmp_path / ".ai").mkdir()
    (tmp_path / ".ai" / "project.yaml").write_text(
        'schema_version: "1.0"\nproject:\n  name: demo\n  class: internal_utility\nscope:\n  include: ["src/"]\n  exclude: []\n'
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True)
    return tmp_path


def store(root, run_id, status):
    result = RunStore(root, run_id)
    result.initialize(
        RunManifest(
            run_id=run_id,
            repository=str(root),
            workflow="standard",
            tier=Tier.STANDARD,
            project_class=ProjectClass.INTERNAL_UTILITY,
            status=status,
        )
    )
    return result


def test_status_shows_stage_and_next_action(tmp_path, monkeypatch):
    root = repo(tmp_path)
    store(root, "20260829-101500_standard_demo", RunState.IMPLEMENTATION_REVIEW)
    monkeypatch.chdir(root)
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "IMPLEMENTATION_REVIEW" in result.output and "Next:" in result.output


def test_status_never_prints_raw_json(tmp_path, monkeypatch):
    root = repo(tmp_path)
    store(root, "20260829-101500_standard_demo", RunState.VALIDATING)
    monkeypatch.chdir(root)
    result = CliRunner().invoke(app, ["status"])
    assert '{"' not in result.output and "schema_version" not in result.output


def test_status_shows_repeated_validation_evidence_paths(tmp_path, monkeypatch):
    root = repo(tmp_path)
    run = store(root, "20260829-101500_standard_retry", RunState.FAILED)
    run.append_event(
        {
            "event": "validation_failure_repeated",
            "name": "unit",
            "source_artifacts": ["execution/validation-v1.json"],
            "child_artifact": "execution/validation-v2.json",
        }
    )
    monkeypatch.chdir(root)
    result = CliRunner().invoke(app, ["status", run.run_id])
    assert result.exit_code == 0
    assert "Notice: validation failure repeated" in result.output
    assert "execution/validation-v1.json" in result.output
    assert "execution/validation-v2.json" in result.output


def test_status_shows_policy_excluded_retry_history(tmp_path, monkeypatch):
    root = repo(tmp_path)
    run = store(root, "20260829-101500_standard_retry", RunState.FAILED)
    run.update_manifest(retry_of="20260829-100000_standard_source")
    run.append_event(
        {"event": "prior_failures_excluded", "source_run": "20260829-100000_standard_source"}
    )
    monkeypatch.chdir(root)
    result = CliRunner().invoke(app, ["status", run.run_id])
    assert result.exit_code == 0
    assert "prompt history excluded by policy" in result.output
    assert "20260829-100000_standard_source" in result.output


def test_status_shows_unavailable_legacy_retry_evidence(tmp_path, monkeypatch):
    root = repo(tmp_path)
    run = store(root, "20260829-101500_standard_retry", RunState.FAILED)
    run.update_manifest(retry_of="20260829-100000_standard_source")
    run.append_event(
        {"event": "prior_failures_unavailable", "source_run": "20260829-100000_standard_source"}
    )
    monkeypatch.chdir(root)
    result = CliRunner().invoke(app, ["status", run.run_id])
    assert result.exit_code == 0
    assert "source evidence unavailable" in result.output
    assert "20260829-100000_standard_source" in result.output


def test_every_run_state_has_next_action():
    for state in RunState:
        manifest = RunManifest(
            run_id="r",
            repository="x",
            workflow="w",
            tier=Tier.STANDARD,
            project_class=ProjectClass.INTERNAL_UTILITY,
            status=state,
        )
        assert "Next:" in describe_status(manifest, [])


def test_runs_lists_newest_first(tmp_path):
    root = repo(tmp_path)
    store(root, "20260829-090000_standard_a", RunState.COMPLETE_LOCAL)
    store(root, "20260829-100000_standard_b", RunState.ESCALATED)
    assert [run_id for run_id, _ in list_runs(root)] == [
        "20260829-100000_standard_b",
        "20260829-090000_standard_a",
    ]


def test_status_unknown_run_fails_cleanly(tmp_path, monkeypatch):
    root = repo(tmp_path)
    monkeypatch.chdir(root)
    result = CliRunner().invoke(app, ["status", "no-such-run"])
    assert (
        result.exit_code == 1
        and "no-such-run" in result.output
        and "Traceback" not in result.output
    )


def test_run_refuses_unsupported_tier_without_traceback(tmp_path, monkeypatch):
    root = repo(tmp_path)
    monkeypatch.chdir(root)
    result = CliRunner().invoke(app, ["run", "rewrite auth", "--tier", "high_risk"])
    assert (
        result.exit_code == 1 and "high_risk" in result.output and "Traceback" not in result.output
    )


def test_reviewed_plan_source_requires_an_external_plan(tmp_path, monkeypatch):
    root = repo(tmp_path)
    monkeypatch.chdir(root)
    result = CliRunner().invoke(app, ["run", "add a flag", "--reviewed-plan-from", "source"])
    assert result.exit_code == 1
    assert "requires --plan FILE" in result.output


def test_run_refuses_dirty_repository_and_names_file(tmp_path, monkeypatch):
    root = repo(tmp_path)
    (root / "scratch.txt").write_text("dirty\n")
    monkeypatch.chdir(root)
    result = CliRunner().invoke(app, ["run", "add a flag"])
    assert (
        result.exit_code == 1
        and "scratch.txt" in result.output
        and "Traceback" not in result.output
    )


def _use_local_worktree(monkeypatch, tmp_path):
    def execute_in_tmp(repo, config, registry_value, request, _worktree_root, **kwargs):
        return runner_execute_run(
            repo,
            config,
            registry_value,
            request,
            tmp_path / "worktrees",
            **kwargs,
        )

    monkeypatch.setattr(cli_module, "execute_run", execute_in_tmp)


def _latest_manifest(root):
    manifests = sorted((root / ".ai" / "runs").glob("*/manifest.json"))
    return RunManifest.model_validate_json(manifests[-1].read_text(encoding="utf-8"))


def test_tier_override_is_effective_persisted_and_cannot_beat_minimum(tmp_path, monkeypatch):
    accepted_root = tmp_path / "accepted"
    accepted_root.mkdir()
    repo(accepted_root)
    accepted_adapter = ScriptedAdapter(tier="standard")
    monkeypatch.setattr(cli_module, "default_registry", lambda *a, **k: registry(accepted_adapter))
    _use_local_worktree(monkeypatch, accepted_root)
    monkeypatch.chdir(accepted_root)
    accepted = CliRunner().invoke(app, ["run", "fix", "--tier", "standard"])
    assert accepted.exit_code == 0, accepted.output
    accepted_manifest = _latest_manifest(accepted_root)
    assert accepted_manifest.tier is Tier.STANDARD
    assert accepted_manifest.tier_override is Tier.STANDARD

    rejected_root = tmp_path / "rejected"
    rejected_root.mkdir()
    repo(rejected_root)
    rejected_adapter = ScriptedAdapter(tier="standard")
    monkeypatch.setattr(cli_module, "default_registry", lambda *a, **k: registry(rejected_adapter))
    _use_local_worktree(monkeypatch, rejected_root)
    monkeypatch.chdir(rejected_root)
    rejected = CliRunner().invoke(app, ["run", "rewrite auth", "--tier", "trivial"])
    assert rejected.exit_code == 1
    assert "Traceback" not in rejected.output
    assert _latest_manifest(rejected_root).status is RunState.ESCALATED

    minimum_root = tmp_path / "minimum"
    minimum_root.mkdir()
    repo(minimum_root)
    (minimum_root / ".ai" / "project.yaml").write_text(
        """schema_version: \"1.0\"
project:
  name: demo
  class: internal_utility
profiles:
  available: [auth]
  definitions:
    auth:
      minimum_tier: standard
      controls:
        security_review: required
scope:
  include: [\"src/\"]
  exclude: []
"""
    )
    subprocess.run(["git", "-C", str(minimum_root), "add", ".ai/project.yaml"], check=True)
    subprocess.run(["git", "-C", str(minimum_root), "commit", "-qm", "add profile"], check=True)
    minimum_adapter = ScriptedAdapter(tier="trivial", profiles=["auth"])
    monkeypatch.setattr(cli_module, "default_registry", lambda *a, **k: registry(minimum_adapter))
    _use_local_worktree(monkeypatch, minimum_root)
    monkeypatch.chdir(minimum_root)
    minimum = CliRunner().invoke(app, ["run", "fix", "--tier", "trivial"])
    assert minimum.exit_code == 0, minimum.output
    minimum_manifest = _latest_manifest(minimum_root)
    assert minimum_manifest.tier is Tier.STANDARD
    assert minimum_manifest.minimum_tier is Tier.STANDARD
    assert minimum_manifest.active_profiles == ["auth"]


def test_external_plan_is_imported_hashed_reviewed_and_not_regenerated(tmp_path, monkeypatch):
    root = tmp_path / "external"
    root.mkdir()
    repo(root)
    source = tmp_path / "plan.md"
    source.write_text(PLAN)
    adapter = ScriptedAdapter(tier="standard")
    monkeypatch.setattr(cli_module, "default_registry", lambda *a, **k: registry(adapter))
    _use_local_worktree(monkeypatch, root)
    monkeypatch.chdir(root)
    result = CliRunner().invoke(app, ["run", "add a flag", "--plan", str(source)])
    assert result.exit_code == 0, result.output
    manifest = _latest_manifest(root)
    assert manifest.plan_origin == "external"
    assert manifest.plan_sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert manifest.approved_plan_version == "plan-v1.md"
    assert "planner" not in adapter.seen
    assert "plan_reviewer" in adapter.seen
    assert (
        root / ".ai" / "runs" / manifest.run_id / "planning" / "approved-plan.md"
    ).read_text() == PLAN
    run_store = RunStore(root, manifest.run_id)
    approved = run_store.root / "planning" / "approved-plan.md"
    with pytest.raises(PlanOverwriteError):
        run_store.approve_plan(run_store.root / "planning" / "plan-v1.md")
    assert approved.read_text() == PLAN


def test_global_config_supplies_the_worktree_root_and_role_bindings(tmp_path, monkeypatch):
    global_path = tmp_path / "config.yaml"
    global_path.write_text(
        "worktree_root: "
        + str(tmp_path / "elsewhere")
        + "\nroles:\n  planner:\n    adapter: claude\n    model: opus\n"
    )
    monkeypatch.setattr(cli_module, "GLOBAL_CONFIG_PATH", global_path)
    loaded = cli_module._global_config()
    assert loaded.worktree_root == str(tmp_path / "elsewhere")
    assert loaded.roles["planner"].adapter == "claude"


def test_missing_global_config_falls_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "GLOBAL_CONFIG_PATH", tmp_path / "absent.yaml")
    assert cli_module._global_config().worktree_root == "~/.dev-orchestration/worktrees"


def test_criteria_are_taken_from_the_command_line_when_supplied(tmp_path, monkeypatch):
    root = tmp_path / "criteria"
    root.mkdir()
    repo(root)
    monkeypatch.setattr(cli_module, "default_registry", lambda *a, **k: registry(ScriptedAdapter()))
    _use_local_worktree(monkeypatch, root)
    monkeypatch.chdir(root)
    result = CliRunner().invoke(
        app,
        [
            "run",
            "add a flag",
            "--criterion",
            "the flag exists",
            "--criterion",
            "tests cover it",
        ],
    )
    assert result.exit_code == 0, result.output
    assert _latest_manifest(root).acceptance_criteria == ["the flag exists", "tests cover it"]
    assert _latest_manifest(root).acceptance_criteria_source == "explicit"


def test_criteria_fall_back_to_the_request_and_say_so(tmp_path, monkeypatch):
    root = tmp_path / "fallback"
    root.mkdir()
    repo(root)
    monkeypatch.setattr(cli_module, "default_registry", lambda *a, **k: registry(ScriptedAdapter()))
    _use_local_worktree(monkeypatch, root)
    monkeypatch.chdir(root)
    result = CliRunner().invoke(app, ["run", "add a flag"])
    assert result.exit_code == 0, result.output
    manifest = _latest_manifest(root)
    assert manifest.acceptance_criteria == ["add a flag"]
    assert manifest.acceptance_criteria_source == "request_fallback"
    assert "--criterion" in result.output


def _dead_run(root, cause, retry_of=None, with_plan=True):
    dead = store(root, "20260829-101500_standard_dead", RunState.ESCALATED)
    dead.update_manifest(
        terminal_cause=cause,
        retry_of=retry_of,
        request_text="add a flag",
        acceptance_criteria=["flag exists"],
        config_layers=[{"scope": {"include": ["src/"], "exclude": []}}],
    )
    if with_plan:
        (dead.root / "planning").mkdir(exist_ok=True)
        plan = dead.root / "planning" / "approved-plan.md"
        plan.write_text(PLAN)
        dead.update_manifest(plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest())
    return dead


def test_retry_reuses_the_approved_plan_of_an_implementation_stage_death(tmp_path, monkeypatch):
    root = tmp_path / "retry"
    root.mkdir()
    repo(root)
    _dead_run(root, "implementation_review_exhausted")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "audit trail"], check=True)
    adapter = ScriptedAdapter(tier="standard")
    monkeypatch.setattr(cli_module, "default_registry", lambda *a, **k: registry(adapter))
    _use_local_worktree(monkeypatch, root)
    monkeypatch.chdir(root)
    result = CliRunner().invoke(app, ["retry", "20260829-101500_standard_dead"])
    assert result.exit_code == 0, result.output
    manifest = _latest_manifest(root)
    assert manifest.retry_of == "20260829-101500_standard_dead"
    assert manifest.plan_origin == "external"
    assert manifest.acceptance_criteria == ["flag exists"]
    assert "planner" not in adapter.seen and "plan_reviewer" in adapter.seen


@pytest.mark.parametrize(
    "cause,retry_of,fragment",
    [
        ("plan_review_exhausted", None, "plan itself must change"),
        ("other", None, "plan itself must change"),
        ("implementation_review_exhausted", "earlier", "already a retry"),
    ],
)
def test_retry_refuses_when_the_plan_is_suspect(tmp_path, monkeypatch, cause, retry_of, fragment):
    root = repo(tmp_path)
    _dead_run(root, cause, retry_of)
    monkeypatch.chdir(root)
    result = CliRunner().invoke(app, ["retry", "20260829-101500_standard_dead"])
    assert result.exit_code == 1
    assert fragment in result.output


def test_retry_refuses_trivial_validation_failure_with_fresh_run_guidance(tmp_path, monkeypatch):
    root = tmp_path / "trivial"
    root.mkdir()
    repo(root)
    _dead_run(root, "validation_failed", with_plan=False)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "audit trail"], check=True)
    adapter = ScriptedAdapter()
    monkeypatch.setattr(cli_module, "default_registry", lambda *a, **k: registry(adapter))
    _use_local_worktree(monkeypatch, root)
    monkeypatch.chdir(root)

    result = CliRunner().invoke(app, ["retry", "20260829-101500_standard_dead"])

    assert result.exit_code == 1
    assert "fresh `dev-orch run` is required" in result.output
    assert adapter.seen == []


def _saved_change(root, content="x = 3\n"):
    """Make a real patch of a change to src/app.py, then revert the checkout."""
    from dev_orchestration.git.repo import GitRepo

    target = root / "src" / "app.py"
    target.write_text(content)
    (root / "src" / "new.py").write_text("y = 1\n")
    patch = GitRepo(root).change_patch("HEAD")
    subprocess.run(["git", "-C", str(root), "checkout", "--", "src/app.py"], check=True)
    (root / "src" / "new.py").unlink()
    return patch


def _retry_fixture(tmp_path, monkeypatch, patch_text, cause="implementation_review_exhausted"):
    root = tmp_path / "continue"
    root.mkdir()
    repo(root)
    dead = _dead_run(root, cause)
    if patch_text is not None:
        dead.write_text_artifact("execution/final-change.patch", patch_text)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "audit trail"], check=True)
    adapter = ScriptedAdapter(tier="standard")
    monkeypatch.setattr(cli_module, "default_registry", lambda *a, **k: registry(adapter))
    _use_local_worktree(monkeypatch, root)
    monkeypatch.chdir(root)
    return root, adapter


def test_retry_review_escalation_requires_and_records_resolution_reason(tmp_path, monkeypatch):
    root, adapter = _retry_fixture(
        tmp_path, monkeypatch, None, cause="implementation_review_escalated"
    )
    command = CliRunner()
    refused = command.invoke(app, ["retry", "20260829-101500_standard_dead"])
    assert refused.exit_code == 1
    assert "--review-escalation-reason" in refused.output
    accepted = command.invoke(
        app,
        [
            "retry",
            "20260829-101500_standard_dead",
            "--review-escalation-reason",
            "reviewer concern was resolved",
        ],
    )
    assert accepted.exit_code == 0, accepted.output
    manifest = _latest_manifest(root)
    events = (root / ".ai" / "runs" / manifest.run_id / "events.jsonl").read_text()
    assert "reviewer concern was resolved" in events
    assert "implementation_worker" in adapter.seen


def test_retry_refuses_tampered_approved_plan_before_provider_call(tmp_path, monkeypatch):
    root, adapter = _retry_fixture(tmp_path, monkeypatch, None)
    plan = root / ".ai/runs/20260829-101500_standard_dead/planning/approved-plan.md"
    plan.write_text("tampered plan")
    result = CliRunner().invoke(app, ["retry", "20260829-101500_standard_dead"])
    assert result.exit_code == 1
    assert "approved plan hash is missing or changed" in result.output
    assert adapter.seen == []


def test_retry_refuses_changed_scope_before_provider_call(tmp_path, monkeypatch):
    root, adapter = _retry_fixture(tmp_path, monkeypatch, None)
    config_path = root / ".ai/project.yaml"
    config_path.write_text(
        'schema_version: "1.0"\nproject:\n  name: demo\n  class: internal_utility\n'
        'scope:\n  include: ["src/", "docs/"]\n  exclude: []\n'
    )
    result = CliRunner().invoke(app, ["retry", "20260829-101500_standard_dead"])
    assert result.exit_code == 1
    assert "scope is missing or changed" in result.output
    assert adapter.seen == []


def test_retry_continues_the_saved_change_without_reimplementing(tmp_path, monkeypatch):
    root = tmp_path / "src_repo"
    root.mkdir()
    repo(root)
    patch = _saved_change(root)
    import shutil

    shutil.rmtree(root)
    root, adapter = _retry_fixture(tmp_path, monkeypatch, patch)
    result = CliRunner().invoke(app, ["retry", "20260829-101500_standard_dead"])
    assert result.exit_code == 0, result.output
    manifest = _latest_manifest(root)
    assert "implementation_worker" not in adapter.seen
    events = (root / ".ai" / "runs" / manifest.run_id / "events.jsonl").read_text()
    assert "change_continued" in events
    worktree = Path(manifest.git.worktree)
    assert (worktree / "src" / "app.py").read_text() == "x = 3\n"
    assert (worktree / "src" / "new.py").read_text() == "y = 1\n"


def test_retry_fresh_reimplements_even_when_a_change_was_saved(tmp_path, monkeypatch):
    root = tmp_path / "src_repo"
    root.mkdir()
    repo(root)
    patch = _saved_change(root)
    import shutil

    shutil.rmtree(root)
    root, adapter = _retry_fixture(tmp_path, monkeypatch, patch)
    result = CliRunner().invoke(app, ["retry", "20260829-101500_standard_dead", "--fresh"])
    assert result.exit_code == 0, result.output
    assert "implementation_worker" in adapter.seen
    worker_request = next(item for item in adapter.requests if item.role == "implementation_worker")
    assert "Historical evidence from source run" in worker_request.context.render()
    assert "Evidence unavailable" in worker_request.context.render()


def test_retry_escalates_when_the_saved_change_no_longer_applies(tmp_path, monkeypatch):
    bad = (
        "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n"
        "@@ -1 +1 @@\n-not the current line\n+x = 3\n"
    )
    root, adapter = _retry_fixture(tmp_path, monkeypatch, bad)
    source_patch = root / ".ai/runs/20260829-101500_standard_dead/execution/final-change.patch"
    source_bytes = source_patch.read_bytes()
    result = CliRunner().invoke(app, ["retry", "20260829-101500_standard_dead"])
    assert result.exit_code == 1
    assert "no longer applies" in result.output
    assert "retry --fresh" in result.output
    assert "implementation_worker" not in adapter.seen
    assert source_patch.read_bytes() == source_bytes


def test_unproductive_death_saves_the_change_as_a_patch(tmp_path):
    from dev_orchestration.domain.run import GitBlock
    from dev_orchestration.workflow.engine import Engine
    from dev_orchestration.workflow.runner import CHANGE_PATCH_ARTIFACT, _save_change_patch

    root = repo(tmp_path)
    base = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    (root / "src" / "app.py").write_text("x = 9\n")
    (root / "src" / "added.py").write_text("z = 1\n")
    runs = tmp_path / "runs"
    runs.mkdir()
    run_store = store(runs, "20260829-101500_standard_dead", RunState.ESCALATED)
    run_store.update_manifest(
        terminal_cause="implementation_review_exhausted", git=GitBlock(base_commit=base)
    )
    _save_change_patch(Engine(run_store), root)
    saved = (run_store.root / CHANGE_PATCH_ARTIFACT).read_text()
    assert "+x = 9" in saved and "added.py" in saved
