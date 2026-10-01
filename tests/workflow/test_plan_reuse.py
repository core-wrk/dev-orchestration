import json

import pytest

from dev_orchestration.artifacts.store import CheckpointError, RunStore
from dev_orchestration.context.assembler import Category
from dev_orchestration.context.packet import ContextRef
from dev_orchestration.domain.enums import ProjectClass, Tier
from dev_orchestration.domain.run import GitBlock, RoleBinding, RunManifest
from dev_orchestration.workflow import plan_reuse
from dev_orchestration.workflow.plan_reuse import (
    RECEIPT,
    create_receipt,
    match_source,
)


def _store(root, run_id, *, request="change", base="base"):
    store = RunStore(root, run_id)
    store.initialize(
        RunManifest(
            run_id=run_id,
            repository=str(root),
            workflow="standard",
            tier=Tier.STANDARD,
            project_class=ProjectClass.INTERNAL_UTILITY,
            request_text=request,
            acceptance_criteria=["works"],
            active_profiles=[],
            context_included=[],
            config_layers=[{"scope": {"include": ["src/"], "exclude": []}}],
            resolved_config={"protected.autonomous_push": False},
            git=GitBlock(base_commit=base),
        )
    )
    return store


def _completed_source(root, review_payload=None):
    source = _store(root, "source")
    version = source.write_plan_version("# reviewed\n")
    source.approve_plan(version)
    review = source.write_json_artifact(
        "review/plan-review-v1.json",
        review_payload or {"outcome": "PASS", "findings": []},
        immutable=True,
    )
    payload = review_payload or {"outcome": "PASS", "findings": []}
    index = {}
    if any(finding["id"] == "F001" for finding in payload["findings"]):
        index["<none>|small note"] = "F001"
    source.record_finding_index(index)
    create_receipt(source, version, review)
    source.checkpoint(
        next_stage="implementation_worker",
        artifacts=[
            "planning/plan-v1.md",
            "planning/approved-plan.md",
            "planning/plan-review-receipt.json",
            "review/plan-review-v1.json",
            "review/finding-index.json",
        ],
        worktree={},
        inputs={},
    )
    return source


def _legacy_source(root):
    source = _store(root, "legacy")
    version = source.write_plan_version("# reviewed\n")
    source.approve_plan(version)
    source.write_json_artifact(
        "review/plan-review-v1.json", {"outcome": "PASS", "findings": []}, immutable=True
    )
    source.checkpoint(
        next_stage="implementation_worker",
        artifacts=[
            "planning/plan-v1.md",
            "planning/approved-plan.md",
            "review/plan-review-v1.json",
        ],
        worktree={},
        inputs={},
    )
    return source


def test_matching_review_inputs_reuse_source_receipt(tmp_path):
    source = _completed_source(tmp_path)
    current = _store(tmp_path, "current")

    matched, reason, receipt = match_source(source, current=current, plan_bytes=b"# reviewed\n")

    assert matched
    assert reason == "matching completed plan review receipt"
    assert receipt == json.loads((source.root / RECEIPT).read_text())


def test_accepted_review_with_nonblocking_findings_is_reusable(tmp_path):
    source = _completed_source(
        tmp_path,
        {
            "outcome": "PASS",
            "findings": [{"id": "F001", "severity": "minor", "summary": "small note"}],
        },
    )
    current = _store(tmp_path, "current")

    matched, _, _ = match_source(source, current=current, plan_bytes=b"# reviewed\n")

    assert matched


def test_legacy_source_without_receipt_requires_review(tmp_path):
    source = _legacy_source(tmp_path)
    current = _store(tmp_path, "current")

    matched, reason, receipt = match_source(source, current=current, plan_bytes=b"# reviewed\n")

    assert not matched
    assert "no plan review receipt" in reason
    assert receipt is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_text", "different"),
        ("acceptance_criteria", ["different"]),
        ("tier", Tier.SUBSTANTIAL),
        ("active_profiles", ["profile"]),
        ("available_profiles", ["profile"]),
        ("resolved_config", {"policy": "different"}),
        ("git", GitBlock(base_commit="different")),
    ],
)
def test_changed_review_input_requests_ordinary_review(tmp_path, field, value):
    source = _completed_source(tmp_path)
    current = _store(tmp_path, "current")
    current.update_manifest(**{field: value})

    matched, reason, receipt = match_source(source, current=current, plan_bytes=b"# reviewed\n")

    assert not matched
    expected = {
        "request_text": "request",
        "acceptance_criteria": "criteria",
        "tier": "effective_tier",
        "active_profiles": "profiles",
        "available_profiles": "profiles",
        "resolved_config": "resolved_policy",
        "git": "base_commit",
    }[field]
    assert expected in reason
    assert receipt is None


def test_changed_source_receipt_is_refused(tmp_path):
    source = _completed_source(tmp_path)
    receipt_path = source.root / RECEIPT
    receipt_path.write_text(receipt_path.read_text() + " ")

    with pytest.raises(CheckpointError, match="changed or is missing"):
        source.verify_checkpoint()


@pytest.mark.parametrize(
    ("kwargs", "plan_bytes", "changed"),
    [
        ({}, b"# edited\n", "plan_sha256"),
        (
            {"config_layers": [{"scope": {"include": ["other/"], "exclude": []}}]},
            b"# reviewed\n",
            "scope",
        ),
        ({"context_included": ["docs/context.md"]}, b"# reviewed\n", "context"),
        (
            {"roles": {"plan_reviewer": RoleBinding(adapter="other")}},
            b"# reviewed\n",
            "review_roles",
        ),
        (
            {"roles": {"plan_reconciler": RoleBinding(adapter="other")}},
            b"# reviewed\n",
            "review_roles",
        ),
    ],
)
def test_plan_scope_context_and_review_binding_changes_reject_reuse(
    tmp_path, kwargs, plan_bytes, changed
):
    source = _completed_source(tmp_path)
    current = _store(tmp_path, "current")
    current.update_manifest(**kwargs)
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs/context.md").write_text("context\n")

    matched, reason, _ = match_source(source, current=current, plan_bytes=plan_bytes)

    assert not matched
    assert changed in reason


def test_changed_profile_constraint_requests_review(tmp_path):
    source = _completed_source(tmp_path)
    current = _store(tmp_path, "current")
    constraints = [
        ContextRef(label=Category.INVARIANTS, path="profile:review", content="new constraint")
    ]

    matched, reason, _ = match_source(
        source,
        current=current,
        plan_bytes=b"# reviewed\n",
        profile_constraints=constraints,
    )

    assert not matched
    assert "profile_constraints" in reason


@pytest.mark.parametrize("changed_role", ["plan_reviewer", "plan_reconciler"])
def test_changed_review_prompt_requests_review(tmp_path, monkeypatch, changed_role):
    source = _completed_source(tmp_path)
    current = _store(tmp_path, "current")
    original = plan_reuse.load_role_prompt
    monkeypatch.setattr(
        plan_reuse,
        "load_role_prompt",
        lambda role: "changed prompt" if role == changed_role else original(role),
    )

    matched, reason, _ = match_source(source, current=current, plan_bytes=b"# reviewed\n")

    assert not matched
    assert "review_roles" in reason
