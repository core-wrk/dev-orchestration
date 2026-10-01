from dev_orchestration.domain.enums import ProjectClass, RunState, Tier
from dev_orchestration.domain.run import GitBlock, RunManifest


def test_manifest_round_trips_through_json():
    manifest = RunManifest(
        run_id="20260825-013500_feature_session-reconciliation",
        repository="helmfast-OS",
        workflow="feature",
        tier=Tier.STANDARD,
        project_class=ProjectClass.INTERNAL_OPERATING_SYSTEM,
        active_profiles=["canonical_knowledge"],
    )
    restored = RunManifest.model_validate(manifest.model_dump(mode="json"))
    assert restored == manifest


def test_manifest_starts_in_created_state():
    manifest = RunManifest(
        run_id="20260825-013500_feature_x",
        repository="r",
        workflow="feature",
        tier=Tier.TRIVIAL,
        project_class=ProjectClass.MARKETING_WEBSITE,
    )
    assert manifest.status is RunState.CREATED
    assert manifest.git == GitBlock()
    assert manifest.plan_origin == "generated"
    assert manifest.validation_repairs_reserved == 0
    assert manifest.validation_artifact is None


def test_old_manifest_loads_with_empty_validation_progress():
    manifest = RunManifest(
        run_id="old",
        repository="r",
        workflow="standard",
        tier=Tier.STANDARD,
        project_class=ProjectClass.INTERNAL_UTILITY,
    )
    saved = manifest.model_dump(mode="json")
    saved.pop("validation_repairs_reserved")
    saved.pop("validation_artifact")
    restored = RunManifest.model_validate(saved)
    assert restored.validation_repairs_reserved == 0
    assert restored.validation_artifact is None
