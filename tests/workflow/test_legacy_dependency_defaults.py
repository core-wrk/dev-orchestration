from dev_orchestration.artifacts.store import RunStore
from dev_orchestration.domain.enums import RunState
from dev_orchestration.workflow.recovery import resume_run
from tests.workflow.test_resume import (
    CONFIG,
    CRITERIA,
    QuotaAtThirdReview,
    Scripted,
    execute_run,
    registry,
    repo,
)


def test_precheckpoint_run_without_dependency_option_recovers(tmp_path):
    git = repo(tmp_path)
    first = execute_run(
        git,
        CONFIG,
        registry(QuotaAtThirdReview()),
        "change app",
        tmp_path / "wt",
        CRITERIA,
    )
    source = RunStore(git.root, first.run_id)
    layers = source.read_manifest().config_layers
    layers[0]["context"].pop("dependency_discovery")
    source.update_manifest(
        config_layers=layers,
        status=RunState.FAILED,
        checkpoint=None,
        pause=None,
        terminal_reason="provider usage limit",
        schema_version="1.0",
    )
    for path in (source.root / "checkpoints").glob("*.json"):
        path.unlink()
    adapter = Scripted()
    resumed = resume_run(git, CONFIG, registry(adapter), first.run_id)
    assert resumed.final_state is RunState.COMPLETE_LOCAL
    assert resumed.run_id != first.run_id
    assert source.read_manifest().status is RunState.FAILED
    assert source.read_manifest().checkpoint is None
    assert RunStore(git.root, resumed.run_id).read_manifest().linked_source_run == first.run_id
    assert adapter.seen[0] == "plan_reviewer"
