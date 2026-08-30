import hashlib

import pytest

from dev_orchestration.artifacts.store import NoApprovedPlanError, PlanOverwriteError, RunStore
from dev_orchestration.domain.enums import ProjectClass, Tier
from dev_orchestration.domain.run import RunManifest


@pytest.fixture
def store(tmp_path):
    result = RunStore(tmp_path, "run-1")
    result.initialize(
        RunManifest(
            run_id="run-1",
            repository="r",
            workflow="standard",
            tier=Tier.STANDARD,
            project_class=ProjectClass.INTERNAL_UTILITY,
        )
    )
    return result


def test_approved_plan_is_an_immutable_copy(store):
    first = store.write_plan_version("# v1\n")
    approved = store.approve_plan(first)
    assert approved.read_text() == "# v1\n"
    second = store.write_plan_version("# v2\n")
    with pytest.raises(PlanOverwriteError):
        store.approve_plan(second)
    assert first.read_text() == "# v1\n"
    assert store.approved_plan() == "# v1\n"


def test_reading_before_approval_fails(store):
    store.write_plan_version("# v1\n")
    with pytest.raises(NoApprovedPlanError):
        store.approved_plan()


def test_external_plan_is_copied_hashed_and_recorded(store, tmp_path):
    source = tmp_path / "external.md"
    source.write_text("# external\n")
    copied, digest = store.import_external_plan(source)
    assert copied.name == "plan-v1.md"
    assert digest == hashlib.sha256(source.read_bytes()).hexdigest()
    manifest = store.read_manifest()
    assert manifest.plan_origin == "external"
    assert manifest.plan_sha256 == digest
