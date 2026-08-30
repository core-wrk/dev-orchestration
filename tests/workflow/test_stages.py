from datetime import UTC, datetime

import pytest

from dev_orchestration.adapters.base import AdapterStatus, AgentResult
from dev_orchestration.adapters.registry import RoleRegistry
from dev_orchestration.artifacts.store import RunStore
from dev_orchestration.config.models import RoleConfig
from dev_orchestration.context.assembler import Category, ContextContractError
from dev_orchestration.context.packet import ContextRef
from dev_orchestration.domain.enums import Outcome, ProjectClass, Tier
from dev_orchestration.domain.findings import Finding, Severity
from dev_orchestration.domain.run import RunManifest
from dev_orchestration.workflow.stages import (
    MAX_REMEDIATION_CYCLES,
    FindingIdentityAmbiguity,
    RemediationExhausted,
    execute,
    plan,
    reconcile,
    remediate,
    review_implementation,
    review_plan,
    verify,
)


class Scripted:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.requests = []

    def healthcheck(self):
        return AdapterStatus(name="fake", available=True)

    def supports(self, capability):
        return capability in {"exec", "read_only_review"}

    def run(self, request):
        self.requests.append(request)
        now = datetime.now(UTC)
        return AgentResult("fake", None, 0, self.payloads.pop(0), now, now)


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


def registry(adapter, *roles):
    return RoleRegistry(
        roles={role: RoleConfig(adapter="fake", model="m") for role in roles},
        adapters={"fake": adapter},
    )


def test_plan_review_assigns_our_stable_ids(store):
    adapter = Scripted(
        [
            {
                "outcome": "CHANGES_REQUIRED",
                "findings": [
                    {
                        "id": "ignored",
                        "severity": "blocking",
                        "summary": "bad",
                        "evidence_required": "tests",
                    }
                ],
            }
        ]
    )
    result = review_plan(registry(adapter, "plan_reviewer"), "brief", "# plan", [], store)
    assert result.findings[0].id == "F001"


def test_planning_and_reconciliation_keep_plan_versions(store):
    first = plan(registry(Scripted(["# v1\n"]), "planner"), "brief", [], [], store)
    second = reconcile(
        registry(Scripted(["# v2\n"]), "plan_reconciler"), first.read_text(), [], store
    )
    assert (
        first.name == "plan-v1.md" and second.name == "plan-v2.md" and first.read_text() == "# v1\n"
    )


def test_execution_is_pinned_to_worktree_and_excludes_review_findings(store, tmp_path):
    adapter = Scripted(["done"])
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    execute(registry(adapter, "implementation_worker"), "# approved", [], [], worktree, store)
    assert adapter.requests[0].cwd == worktree
    assert Category.APPROVED_PLAN in adapter.requests[0].context.labels()
    with pytest.raises(ContextContractError):
        execute(
            registry(Scripted(["done"]), "implementation_worker"),
            "# approved",
            [],
            [ContextRef(label=Category.REVIEW_FINDINGS, path=None, content="F1")],
            worktree,
            store,
        )


def test_implementation_review_uses_validation_evidence_and_stable_ids(store):
    adapter = Scripted([{"outcome": "PASS", "findings": []}])
    result = review_implementation(
        registry(adapter, "implementation_reviewer"), "# plan", "diff", "unit: exit 0", [], store
    )
    assert result.outcome is Outcome.PASS
    assert Category.VALIDATION_EVIDENCE in adapter.requests[0].context.labels()
    assert Category.BUILDER_NARRATION not in adapter.requests[0].context.labels()


def test_remediation_is_limited_to_two_cycles(store, tmp_path):
    adapter = Scripted(["fixed", "fixed"])
    blocking = [Finding(id="F001", severity=Severity.BLOCKING, summary="s", evidence_required="e")]
    for cycle in (1, 2):
        remediate(
            registry(adapter, "implementation_worker"),
            blocking,
            "# plan",
            "state",
            tmp_path,
            store,
            cycle=cycle,
        )
    with pytest.raises(RemediationExhausted):
        remediate(
            registry(adapter, "implementation_worker"),
            blocking,
            "# plan",
            "state",
            tmp_path,
            store,
            cycle=MAX_REMEDIATION_CYCLES + 1,
        )


def test_verification_passes_every_criterion_to_the_model(store):
    adapter = Scripted(
        [
            {
                "outcome": "PASS",
                "criteria": ["a"],
                "verdicts": [{"criterion": "a", "verdict": "PASS", "evidence": "e"}],
                "unresolved_finding_ids": [],
            }
        ]
    )
    result = verify(
        registry(adapter, "verifier"), "req", "# plan", ["a"], "state", "unit: exit 0", [], store
    )
    assert result.verdicts[0].criterion == "a"
    assert Category.ACCEPTANCE_CRITERIA in adapter.requests[0].context.labels()


def _blocking(summary, line=12):
    return {
        "outcome": "CHANGES_REQUIRED",
        "findings": [
            {
                "id": "provider-made-this-up",
                "severity": "blocking",
                "summary": summary,
                "evidence_required": "a test",
                "file": "src/app.py",
                "line": line,
            }
        ],
    }


def test_a_repeated_finding_keeps_its_id_across_line_and_wording_drift(store):
    adapter = Scripted(
        [_blocking("Missing bounds check", 12), _blocking("Missing bound checks", 40)]
    )
    roles = registry(adapter, "implementation_reviewer")
    first = review_implementation(roles, "plan", "diff", "evidence", [], store)
    second = review_implementation(roles, "plan", "diff", "evidence", [], store)
    assert first.findings[0].id == "F001"
    assert second.findings[0].id == "F001"


def test_a_different_finding_gets_a_new_id(store):
    adapter = Scripted([_blocking("Missing bounds check"), _blocking("Unclosed file handle")])
    roles = registry(adapter, "implementation_reviewer")
    first = review_implementation(roles, "plan", "diff", "evidence", [], store)
    second = review_implementation(roles, "plan", "diff", "evidence", [], store)
    assert first.findings[0].id == "F001"
    assert second.findings[0].id == "F002"


def test_ambiguous_finding_identity_fails_closed(store):
    store.record_finding_index(
        {
            "src/app.py|missing bound check": "F001",
            "src/app.py|missing bound validation": "F002",
        }
    )
    adapter = Scripted([_blocking("Missing bound check validation")])
    with pytest.raises(FindingIdentityAmbiguity):
        review_implementation(
            registry(adapter, "implementation_reviewer"),
            "plan",
            "diff",
            "evidence",
            [],
            store,
        )
