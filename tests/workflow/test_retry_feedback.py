import pytest

from dev_orchestration.artifacts.store import CheckpointError, RunStore
from dev_orchestration.domain.enums import ProjectClass, RunState, Tier
from dev_orchestration.domain.run import RunManifest
from dev_orchestration.workflow.retry_feedback import (
    ARTIFACT,
    MAX_HISTORY_BYTES,
    build_snapshot,
    compare_validation,
    validation_signatures,
)


def _source(root, output="assertion  failed\n"):
    store = RunStore(root, "source")
    store.initialize(
        RunManifest(
            run_id="source",
            repository=str(root),
            workflow="standard",
            tier=Tier.STANDARD,
            project_class=ProjectClass.INTERNAL_UTILITY,
            status=RunState.FAILED,
            terminal_cause="validation_failed",
        )
    )
    store.write_json_artifact(
        "execution/validation-v1.json",
        [
            {
                "name": "unit",
                "command": "pytest -q",
                "exit_code": 2,
                "passed": False,
                "stdout_tail": output,
                "stderr_tail": "traceback",
            }
        ],
        immutable=True,
    )
    store.checkpoint(
        next_stage="verifier",
        artifacts=["execution/validation-v1.json"],
        worktree={},
        inputs={},
    )
    return store


def test_snapshot_carries_receipted_failure_and_is_bounded(tmp_path):
    source = _source(tmp_path)
    snapshot = build_snapshot(source, tmp_path, include_prompt=True)
    assert snapshot["source_run_id"] == "source"
    assert "pytest -q" in snapshot["prompt_text"]
    assert "execution/validation-v1.json" in snapshot["prompt_text"]
    assert len(snapshot["prompt_text"].encode("utf-8")) <= MAX_HISTORY_BYTES
    assert len(snapshot["validation_signatures"]) == 1
    assert ARTIFACT == "execution/prior-failures.json"


def test_oversized_history_keeps_references_and_omission_notice(tmp_path):
    source = _source(tmp_path, "failure " * 2_000)
    snapshot = build_snapshot(source, tmp_path, include_prompt=True)
    rendered = snapshot["prompt_text"]
    assert snapshot["omission_notice"]
    assert len(rendered.encode("utf-8")) <= MAX_HISTORY_BYTES
    assert "Earlier history omitted" in rendered
    assert "execution/validation-v1.json" in rendered


def test_changed_receipted_source_artifact_is_refused(tmp_path):
    source = _source(tmp_path)
    (source.root / "execution/validation-v1.json").write_text("[]", encoding="utf-8")
    with pytest.raises(CheckpointError, match="changed or is missing"):
        build_snapshot(source, tmp_path, include_prompt=True)


def test_legacy_source_and_policy_exclusion_are_visible(tmp_path):
    source = RunStore(tmp_path, "legacy")
    source.initialize(
        RunManifest(
            run_id="legacy",
            repository=str(tmp_path),
            workflow="standard",
            tier=Tier.STANDARD,
            project_class=ProjectClass.INTERNAL_UTILITY,
        )
    )
    legacy = build_snapshot(source, tmp_path, include_prompt=True)
    excluded = build_snapshot(source, tmp_path, include_prompt=False)
    assert legacy["evidence_unavailable"]
    assert "Evidence unavailable" in legacy["prompt_text"]
    assert excluded["prompt_excluded"]
    assert "excluded by context.include_prior_artifacts=none" in excluded["prompt_text"]


def test_signature_normalizes_whitespace_and_ignores_empty_or_successful():
    base = {
        "name": "unit",
        "command": "pytest",
        "exit_code": 1,
        "passed": False,
        "stdout_tail": "failed\n  here",
        "stderr_tail": "",
    }
    changed = {**base, "stdout_tail": "failed here"}
    empty = {**base, "stdout_tail": "  \n", "stderr_tail": ""}
    success = {**base, "passed": True, "exit_code": 0}
    assert validation_signatures([base]) == validation_signatures([changed])
    assert not validation_signatures([empty, success])


def test_repeated_failure_compares_only_exact_linked_evidence(tmp_path):
    source = _source(tmp_path)
    snapshot = build_snapshot(source, tmp_path, include_prompt=True)
    child = RunStore(tmp_path, "child")
    child.initialize(
        RunManifest(
            run_id="child",
            repository=str(tmp_path),
            workflow="standard",
            tier=Tier.STANDARD,
            project_class=ProjectClass.INTERNAL_UTILITY,
            retry_of="source",
        )
    )
    record = {
        "name": "unit",
        "command": "pytest -q",
        "exit_code": 2,
        "passed": False,
        "stdout_tail": "assertion failed\n",
        "stderr_tail": "traceback",
    }
    child.write_json_artifact("execution/validation-v1.json", [record])
    child.update_manifest(validation_artifact="execution/validation-v1.json")
    repeated = compare_validation(snapshot, child)
    assert repeated[0]["source_artifacts"] == ["execution/validation-v1.json"]
    child.write_json_artifact(
        "execution/validation-v2.json", [{**record, "stdout_tail": "different output"}]
    )
    child.update_manifest(validation_artifact="execution/validation-v2.json")
    assert compare_validation(snapshot, child) == []
