import json
from datetime import UTC, datetime

import pytest

from dev_orchestration.adapters.base import AgentResult, compose_prompt
from dev_orchestration.adapters.registry import RoleRegistry
from dev_orchestration.artifacts.store import RunStore
from dev_orchestration.config.models import ProjectConfig, RoleConfig
from dev_orchestration.context.assembler import PROMPT_BUDGET_BYTES, Category
from dev_orchestration.domain.enums import ProjectClass, Tier
from dev_orchestration.domain.run import RunManifest
from dev_orchestration.workflow.checkpoints import project_config_matches
from dev_orchestration.workflow.dependencies import (
    DEPENDENCY_CONTEXT_BUDGET,
    dependency_sources,
    render_dependency_context,
)
from dev_orchestration.workflow.invoke import AgentInvocationError
from dev_orchestration.workflow.stages import (
    discover_dependencies,
    plan,
    review_implementation,
    review_plan,
    verify,
)


class Analyst:
    def __init__(self, payload):
        self.payload = payload
        self.request = None

    def supports(self, capability):
        return capability == "read_only_review"

    def run(self, request):
        self.request = request
        now = datetime.now(UTC)
        return AgentResult("fake", "fake", 0, self.payload, now, now)


def config(**context):
    return ProjectConfig.model_validate(
        {
            "project": {"name": "demo", "class": "internal_utility"},
            "context": context,
        }
    )


def test_legacy_saved_config_without_dependency_option_matches_default_false():
    current = config()
    saved = current.model_dump(mode="json")
    saved["context"].pop("dependency_discovery")

    assert project_config_matches(saved, current)
    changed = config(dependency_discovery=True)
    assert not project_config_matches(saved, changed)


def test_inventory_reads_only_persistent_and_declared_on_demand_sources(tmp_path):
    (tmp_path / "AGENTS.md").write_text("architecture instructions")
    (tmp_path / "docs/specs").mkdir(parents=True)
    (tmp_path / "docs/specs/epic.md").write_text("Epic links to plan.md")
    (tmp_path / "private").mkdir()
    (tmp_path / "private/secret.md").write_text("not authorized")

    inventory, metadata = dependency_sources(
        tmp_path,
        config(persistent=["AGENTS.md"], on_demand={"specifications": "docs/specs"}),
    )

    assert "AGENTS.md" in inventory and "docs/specs/epic.md" in inventory
    assert "not authorized" not in inventory
    assert {source["path"] for source in metadata["sources"]} == {
        "AGENTS.md",
        "docs/specs/epic.md",
    }
    assert all(len(source["sha256"]) == 64 for source in metadata["sources"])


def test_inventory_reports_missing_roots_and_budget_exhaustion(tmp_path, monkeypatch):
    from dev_orchestration.workflow import dependencies

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/large.md").write_text("x" * 20)
    monkeypatch.setattr(dependencies, "DEPENDENCY_SOURCE_BUDGET", 8)

    _, metadata = dependency_sources(
        tmp_path,
        config(persistent=[], on_demand={"specifications": "docs", "epics": "missing"}),
    )

    assert metadata["budget_exhausted"] is True
    assert metadata["inaccessible_sources"] == ["missing: missing"]
    assert metadata["prompt_sources"][0]["truncated"] is True


def test_inventory_rejects_symlinks_outside_the_declared_source(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "private").mkdir()
    (tmp_path / "private/secret.md").write_text("UNDECLARED_FILE_CONTENT")
    (tmp_path / "docs/epic.md").symlink_to(tmp_path / "private/secret.md")

    inventory, metadata = dependency_sources(
        tmp_path, config(persistent=[], on_demand={"specifications": "docs"})
    )

    assert "UNDECLARED_FILE_CONTENT" not in inventory
    assert metadata["sources"] == []
    assert metadata["inaccessible_sources"] == [
        "docs/epic.md: target outside declared root (private/secret.md)"
    ]

    (tmp_path / "AGENTS.md").symlink_to(tmp_path / "private/secret.md")
    inventory, metadata = dependency_sources(
        tmp_path,
        config(persistent=["AGENTS.md"], on_demand={"specifications": "docs"}),
    )
    assert "UNDECLARED_FILE_CONTENT" not in inventory
    assert "AGENTS.md: resolves outside its declared source" in metadata["inaccessible_sources"]

    # Explicit persistent files authorize one exact file, not neighboring files.
    (tmp_path / "docs/other.md").write_text("not authorized")
    (tmp_path / "AGENTS.md").unlink()
    (tmp_path / "AGENTS.md").symlink_to(tmp_path / "docs/other.md")
    inventory, metadata = dependency_sources(
        tmp_path, config(persistent=["AGENTS.md"], on_demand={})
    )
    assert "not authorized" not in inventory
    assert metadata["sources"] == []
    assert metadata["inaccessible_sources"] == ["AGENTS.md: resolves outside its declared source"]


def test_on_demand_declared_file_symlink_does_not_supply_target(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "private").mkdir()
    (tmp_path / "private/secret.md").write_text("SECRET_CONTENT")
    (tmp_path / "docs/epic.md").symlink_to("../private/secret.md")

    inventory, metadata = dependency_sources(
        tmp_path, config(persistent=[], on_demand={"e": "docs/epic.md"})
    )

    assert "SECRET_CONTENT" not in inventory
    assert "SECRET_CONTENT" not in str(metadata["sources"])
    assert metadata["sources"] == []
    assert metadata["inaccessible_sources"] == [
        "docs/epic.md: resolves outside its declared source (private/secret.md)"
    ]


def test_on_demand_declared_root_symlink_does_not_supply_target_files(tmp_path):
    (tmp_path / "private").mkdir()
    (tmp_path / "private/secret.md").write_text("SECRET_CONTENT")
    (tmp_path / "specs").symlink_to("private", target_is_directory=True)

    inventory, metadata = dependency_sources(
        tmp_path, config(persistent=[], on_demand={"s": "specs"})
    )

    assert "SECRET_CONTENT" not in inventory
    assert "SECRET_CONTENT" not in str(metadata["sources"])
    assert metadata["sources"] == []
    assert metadata["inaccessible_sources"] == [
        "specs: resolves outside its declared source (private)"
    ]


def test_inventory_detects_symlink_retarget_within_declared_root(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/one.md").write_text("one")
    (tmp_path / "docs/two.md").write_text("two")
    (tmp_path / "docs/epic.md").symlink_to("one.md")
    config_value = config(persistent=[], on_demand={"specifications": "docs"})

    _, before = dependency_sources(tmp_path, config_value)
    (tmp_path / "docs/epic.md").unlink()
    (tmp_path / "docs/epic.md").symlink_to("two.md")
    _, after = dependency_sources(tmp_path, config_value)

    assert before["sources"] == after["sources"] and len(before["sources"]) == 2
    assert before["inaccessible_sources"] == [
        "docs/epic.md: target outside declared root (docs/one.md)"
    ]
    assert after["inaccessible_sources"] == [
        "docs/epic.md: target outside declared root (docs/two.md)"
    ]


def test_full_source_identity_inventory_is_not_limited_by_prompt_budget(tmp_path, monkeypatch):
    from dev_orchestration.workflow import dependencies

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/a.md").write_text("a" * 12)
    (tmp_path / "docs/z.md").write_text("tail")
    monkeypatch.setattr(dependencies, "DEPENDENCY_SOURCE_BUDGET", 8)
    config_value = config(persistent=[], on_demand={"specifications": "docs"})

    _, before = dependency_sources(tmp_path, config_value)
    (tmp_path / "docs/z.md").write_text("changed beyond prompt")
    (tmp_path / "docs/new.md").write_text("new source")
    _, after = dependency_sources(tmp_path, config_value)

    assert [source["path"] for source in before["sources"]] == ["docs/a.md", "docs/z.md"]
    assert [source["path"] for source in after["sources"]] == [
        "docs/a.md",
        "docs/new.md",
        "docs/z.md",
    ]
    assert before["sources"] != after["sources"]
    assert after["budget_exhausted"] is True


def test_dependency_prompt_omits_hash_inventory_but_keeps_findings_and_roots(tmp_path):
    (tmp_path / "docs").mkdir()
    for number in range(600):
        (tmp_path / "docs" / f"{number:03}.md").write_text("x")
    inventory, metadata = dependency_sources(
        tmp_path, config(persistent=[], on_demand={"specifications": "docs"})
    )
    report = {
        "findings": [
            {
                "source": "docs/000.md",
                "excerpt": "x",
                "current_behavior": "The interface exists.",
            }
        ],
        "searched_sources": [source["path"] for source in metadata["prompt_sources"]],
        "source_identities": metadata["sources"],
        "source_inventory_identity": "a" * 64,
        "declared_sources": metadata["declared_sources"],
        "inaccessible_sources": metadata["inaccessible_sources"],
        "incomplete_searches": [],
        "budget_exhausted": False,
    }

    rendered = render_dependency_context(report)

    assert len(rendered.encode("utf-8")) + 128 <= DEPENDENCY_CONTEXT_BUDGET
    assert "sha256" not in inventory
    assert "source_identities" not in rendered
    assert "searched_sources" not in rendered
    assert "a" * 64 not in rendered
    assert '"excerpt": "x"' in rendered
    assert "docs" in rendered
    assert len(metadata["sources"]) == 600
    assert all(len(source["sha256"]) == 64 for source in metadata["sources"])


def test_dependency_prompt_rejects_oversized_essential_findings():
    report = {
        "findings": [{"excerpt": "x" * 25_000}],
        "declared_sources": {"persistent": ["AGENTS.md"], "on_demand": {}},
    }

    with pytest.raises(ValueError, match="24,000 UTF-8 bytes.*narrow declared sources"):
        render_dependency_context(report)


def test_analyst_report_is_saved_with_framework_source_identities(tmp_path):
    (tmp_path / "spec.md").write_text("The interface is called CustomerId.")
    _, metadata = dependency_sources(tmp_path, config(persistent=["spec.md"]))
    inventory, _ = dependency_sources(tmp_path, config(persistent=["spec.md"]))
    store = RunStore(tmp_path, "run-1")
    store.initialize(
        RunManifest(
            run_id="run-1",
            repository="demo",
            workflow="standard",
            tier=Tier.STANDARD,
            project_class=ProjectClass.INTERNAL_UTILITY,
        )
    )
    payload = {
        "findings": [
            {
                "relationship": "shared_contract",
                "source": "spec.md",
                "excerpt": "CustomerId",
                "current_behavior": "The interface exists.",
                "future_work": "The task may update it.",
                "approval_status": "not_applicable",
                "effect_on_task": "Keep callers compatible.",
            }
        ],
        "searched_sources": [],
        "inaccessible_sources": [],
        "incomplete_searches": [],
        "budget_exhausted": False,
    }
    adapter = Analyst(payload)
    registry = RoleRegistry(
        {"dependency_analyst": RoleConfig(adapter="fake", model="test")}, {"fake": adapter}
    )

    report = discover_dependencies(registry, "update customer", inventory, metadata, [], store)

    saved = json.loads((store.root / "dependency-report.json").read_text())
    assert "CustomerId" in adapter.request.context.render()
    assert saved["searched_sources"] == ["spec.md"]
    assert saved["source_identities"] == metadata["sources"]
    assert json.loads(report)["findings"][0]["relationship"] == "shared_contract"


def test_analyst_citations_must_be_backed_by_supplied_source_text(tmp_path):
    (tmp_path / "spec.md").write_text("The interface is called CustomerId.")
    inventory, metadata = dependency_sources(tmp_path, config(persistent=["spec.md"]))
    store = RunStore(tmp_path, "run-unsupported-citation")
    store.initialize(
        RunManifest(
            run_id="run-unsupported-citation",
            repository="demo",
            workflow="standard",
            tier=Tier.STANDARD,
            project_class=ProjectClass.INTERNAL_UTILITY,
        )
    )
    payload = {
        "findings": [
            {
                "relationship": "shared_contract",
                "source": "spec.md",
                "excerpt": "Fabricated claim",
                "current_behavior": "It exists.",
                "future_work": "It may change.",
                "approval_status": "not_applicable",
                "effect_on_task": "Preserve it.",
            }
        ],
        "searched_sources": [],
        "inaccessible_sources": [],
        "incomplete_searches": [],
        "budget_exhausted": False,
    }
    adapter = Analyst(payload)
    registry = RoleRegistry(
        {"dependency_analyst": RoleConfig(adapter="fake", model="test")}, {"fake": adapter}
    )

    with pytest.raises(AgentInvocationError, match="unsupported evidence"):
        discover_dependencies(registry, "update customer", inventory, metadata, [], store)


def test_600_source_report_fits_real_downstream_requests_and_keeps_full_inventory(tmp_path):
    (tmp_path / "docs").mkdir()
    for number in range(600):
        (tmp_path / "docs" / f"{number:03}.md").write_text("x")
    context_config = config(persistent=[], on_demand={"specifications": "docs"})
    inventory, metadata = dependency_sources(tmp_path, context_config)
    store = RunStore(tmp_path, "run-downstream-dependency-budget")
    store.initialize(
        RunManifest(
            run_id="run-downstream-dependency-budget",
            repository="demo",
            workflow="standard",
            tier=Tier.STANDARD,
            project_class=ProjectClass.INTERNAL_UTILITY,
        )
    )
    finding = {
        "relationship": "shared_contract",
        "source": "docs/000.md",
        "excerpt": "x",
        "current_behavior": "The public interface is shared by existing callers.",
        "future_work": "The requested change may add a new caller.",
        "approval_status": "not_applicable",
        "effect_on_task": "Preserve compatibility with existing callers.",
    }
    analyst = Analyst(
        {
            "findings": [finding],
            "searched_sources": [],
            "inaccessible_sources": [],
            "incomplete_searches": [],
            "budget_exhausted": False,
        }
    )
    report_text = discover_dependencies(
        RoleRegistry(
            {"dependency_analyst": RoleConfig(adapter="fake", model="test")},
            {"fake": analyst},
        ),
        "add a caller",
        inventory,
        metadata,
        [],
        store,
    )
    saved_report = json.loads((store.root / "dependency-report.json").read_text())
    assert saved_report["source_identities"] == metadata["sources"]
    assert len(saved_report["source_identities"]) == 600
    assert all(len(source["sha256"]) == 64 for source in saved_report["source_identities"])

    class CapturingAdapter(Analyst):
        def __init__(self, payload):
            super().__init__(payload)
            self.requests = []

        def run(self, request):
            self.requests.append(request)
            return super().run(request)

    def stage_registry(role, adapter):
        return RoleRegistry({role: RoleConfig(adapter="fake", model="test")}, {"fake": adapter})

    review_payload = {"outcome": "PASS", "findings": []}
    verification_payload = {
        "outcome": "PASS",
        "criteria": ["Existing callers remain compatible."],
        "verdicts": [
            {
                "criterion": "Existing callers remain compatible.",
                "verdict": "PASS",
                "evidence": "The diff preserves the shared interface.",
            }
        ],
        "unresolved_finding_ids": [],
        "outcome_explanation": [],
    }
    adapters = [
        CapturingAdapter("# Plan\n"),
        CapturingAdapter(review_payload),
        CapturingAdapter(review_payload),
        CapturingAdapter(verification_payload),
    ]
    plan(
        stage_registry("planner", adapters[0]),
        "add a caller",
        [],
        [],
        store,
        dependency_report=report_text,
    )
    review_plan(
        stage_registry("plan_reviewer", adapters[1]),
        "add a caller",
        "# Plan\n",
        [],
        store,
        dependency_report=report_text,
    )
    review_implementation(
        stage_registry("implementation_reviewer", adapters[2]),
        "# Approved plan\n",
        "diff --git a/api.py b/api.py\n",
        "ruff passed",
        [],
        store,
        dependency_report=report_text,
    )
    verify(
        stage_registry("verifier", adapters[3]),
        "add a caller",
        "# Approved plan\n",
        ["Existing callers remain compatible."],
        "diff --git a/api.py b/api.py\n",
        "ruff passed",
        [],
        store,
        dependency_report=report_text,
    )

    for adapter in adapters:
        request = adapter.request
        dependency_ref = next(
            item for item in request.context.items if item.label == Category.DEPENDENCY_REPORT
        )
        rendered_dependency = dependency_ref.render()
        assert len(rendered_dependency.encode("utf-8")) <= DEPENDENCY_CONTEXT_BUDGET
        assert finding["source"] in rendered_dependency
        assert finding["current_behavior"] in rendered_dependency
        assert finding["effect_on_task"] in rendered_dependency
        assert "Reviewers must independently inspect" in rendered_dependency
        assert len(compose_prompt(request).encode("utf-8")) <= PROMPT_BUDGET_BYTES
