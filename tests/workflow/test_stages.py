import json
from datetime import UTC, datetime

import pytest

from dev_orchestration.adapters.base import (
    AdapterStatus,
    AgentInterruptedError,
    AgentResult,
    AgentTimeoutError,
)
from dev_orchestration.adapters.registry import RoleRegistry
from dev_orchestration.artifacts.store import RunStore
from dev_orchestration.config.models import RoleConfig
from dev_orchestration.context.assembler import Category, ContextContractError
from dev_orchestration.context.packet import ContextRef
from dev_orchestration.domain.enums import Outcome, ProjectClass, Tier
from dev_orchestration.domain.findings import Finding, Severity
from dev_orchestration.domain.run import RunManifest
from dev_orchestration.roles.loader import load_role_prompt
from dev_orchestration.workflow.stages import (
    MAX_REMEDIATION_CYCLES,
    FindingIdentityAmbiguity,
    RemediationExhausted,
    classify,
    execute,
    plan,
    reconcile,
    remediate,
    repair_validation,
    review_implementation,
    review_plan,
    verify,
)
from dev_orchestration.workflow.validation import ValidationOutcome


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


def test_classifier_receives_selected_specification_and_exact_criteria(store):
    adapter = Scripted([{"tier": "standard", "rationale": "scope", "profiles": []}])
    classify(
        registry(adapter, "classifier"),
        "Implement docs/spec.md",
        ProjectClass.INTERNAL_UTILITY,
        store,
        ["profile"],
        [ContextRef(label=Category.REFERENCES, path="docs/spec.md", content="selected text")],
        ["keep this criterion exactly"],
    )
    rendered = adapter.requests[0].context.render()
    assert "selected text" in rendered
    assert '["keep this criterion exactly"]' in rendered


@pytest.mark.parametrize(
    ("configured_budget", "expected_budgets"),
    [(30, [30, 30]), (90, [60, 90])],
)
def test_reliability_classifier_timeout_retries_once_with_configured_budget(
    store, configured_budget, expected_budgets
):
    class TimeoutOnce(Scripted):
        def run(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                raise AgentTimeoutError("classifier", request.timeout_seconds, "fake")
            now = datetime.now(UTC)
            return AgentResult(
                "fake", None, 0, {"tier": "trivial", "rationale": "small", "profiles": []}, now, now
            )

    adapter = TimeoutOnce([])
    codex_registry = RoleRegistry(
        {"classifier": RoleConfig(adapter="codex", model="m", timeout_seconds=configured_budget)},
        {"codex": adapter},
    )
    result = classify(codex_registry, "small change", ProjectClass.INTERNAL_UTILITY, store)
    assert result.tier is Tier.TRIVIAL
    assert [request.timeout_seconds for request in adapter.requests] == expected_budgets
    events = [json.loads(line) for line in (store.root / "events.jsonl").read_text().splitlines()]
    assert [
        event["event"] for event in events if event["event"].startswith("classifier_attempt")
    ] == ["classifier_attempt_failed", "classifier_attempt_succeeded"]


def test_reliability_classifier_second_timeout_and_interrupt_are_not_swallowed(store):
    class AlwaysTimeout(Scripted):
        def run(self, request):
            self.requests.append(request)
            raise AgentTimeoutError("classifier", request.timeout_seconds, "fake")

    adapter = AlwaysTimeout([])
    codex_registry = RoleRegistry(
        {"classifier": RoleConfig(adapter="codex", timeout_seconds=90)}, {"codex": adapter}
    )
    with pytest.raises(AgentTimeoutError):
        classify(codex_registry, "small change", ProjectClass.INTERNAL_UTILITY, store)
    assert [request.timeout_seconds for request in adapter.requests] == [60, 90]
    events = [json.loads(line) for line in (store.root / "events.jsonl").read_text().splitlines()]
    attempts = [event for event in events if event.get("event") == "classifier_attempt_failed"]
    assert [event["attempt"] for event in attempts] == [1, 2]

    class Interrupted(Scripted):
        def run(self, request):
            self.requests.append(request)
            raise AgentInterruptedError("classifier", "fake", 2)

    interrupted = Interrupted([])
    with pytest.raises(AgentInterruptedError):
        classify(
            registry(interrupted, "classifier"),
            "small change",
            ProjectClass.INTERNAL_UTILITY,
            store,
        )
    assert len(interrupted.requests) == 1


def test_planning_and_reconciliation_keep_plan_versions(store):
    first = plan(registry(Scripted(["# v1\n"]), "planner"), "brief", [], [], store)
    second = reconcile(
        registry(Scripted(["# v2\n"]), "plan_reconciler"), first.read_text(), [], store
    )
    assert (
        first.name == "plan-v1.md" and second.name == "plan-v2.md" and first.read_text() == "# v1\n"
    )


def test_planner_and_reconciler_prompts_preserve_small_explicit_designs(store):
    planner = Scripted(["# Plan\n"])
    plan(
        registry(planner, "planner"),
        "Update smoke_test.py to refuse when clients/smoketest-co* exists; do not add an ownership manifest or hashes.",
        [],
        [],
        store,
    )
    assert "explicitly requested design" in planner.requests[0].prompt
    assert "ownership maps" in planner.requests[0].prompt
    assert "smallest complete design" in planner.requests[0].prompt
    assert "dropping a criterion or crossing the approved scope" in load_role_prompt(
        "plan_reconciler"
    )


def test_reliability_planning_context_preserves_request_criteria_and_scope(store):
    request = (
        "Update smoke_test.py to refuse if clients/smoketest-co* exists; "
        "do not add an ownership manifest."
    )
    criteria = [
        "Refuse before creating files when any clients/smoketest-co* folder exists.",
        "Do not create an ownership manifest.",
    ]
    scope = ContextRef(
        label=Category.SCOPE_FENCE,
        path="config:scope",
        content='{"include":["src/","tests/"],"exclude":[".ai/"]}',
    )
    proposed = "# Plan\n\nAdd an ownership manifest to track clients/smoketest-co* folders.\n"
    planner = Scripted([proposed])
    plan_path = plan(
        registry(planner, "planner"),
        request,
        [],
        [],
        store,
        scope_fence=scope,
        acceptance_criteria=criteria,
    )
    review_adapter = Scripted(
        [
            {
                "outcome": "CHANGES_REQUIRED",
                "findings": [
                    {
                        "id": "ignored",
                        "severity": "blocking",
                        "summary": "Plan adds a forbidden ownership manifest",
                        "evidence_required": "revise the plan to refuse on an existing matching folder",
                    }
                ],
            }
        ]
    )
    review_plan(
        registry(review_adapter, "plan_reviewer"),
        request,
        plan_path.read_text(),
        [],
        store,
        criteria,
        scope,
    )
    reconciler = Scripted(
        ["# Revised plan\n\nRefuse if clients/smoketest-co* exists. Add no ownership manifest.\n"]
    )
    revised = reconcile(
        registry(reconciler, "plan_reconciler"),
        proposed,
        [
            Finding(
                id="F001",
                severity=Severity.BLOCKING,
                summary="Plan adds a forbidden ownership manifest",
                evidence_required="revise the plan",
            )
        ],
        store,
        request,
        criteria,
        scope,
    )

    expected_scope = scope.content
    for adapter in (planner, review_adapter, reconciler):
        rendered = adapter.requests[0].context.render()
        assert request in rendered
        assert json.dumps(criteria, ensure_ascii=False) in rendered
        assert expected_scope in rendered
    assert "ownership manifest" in planner.requests[0].context.render()
    assert "ownership manifest" in review_adapter.requests[0].context.render()
    assert "ownership manifest" in reconciler.requests[0].context.render()
    assert "Add no ownership manifest" in revised.read_text()
    assert MAX_REMEDIATION_CYCLES == 2


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


def test_execution_receives_saved_prior_failure_snapshot(store, tmp_path):
    store.write_json_artifact(
        "execution/prior-failures.json",
        {
            "prompt_excluded": False,
            "prompt_text": "Historical evidence from source run old: pytest failed",
        },
        immutable=True,
    )
    adapter = Scripted(["done"])
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    execute(registry(adapter, "implementation_worker"), "# approved", [], [], worktree, store)
    packet = adapter.requests[0].context
    assert Category.PRIOR_FAILURES in packet.labels()
    assert "pytest failed" in packet.render()


def test_implementation_review_uses_validation_evidence_and_stable_ids(store):
    adapter = Scripted([{"outcome": "PASS", "findings": []}])
    result = review_implementation(
        registry(adapter, "implementation_reviewer"), "# plan", "diff", "unit: exit 0", [], store
    )
    assert result.outcome is Outcome.PASS
    assert Category.VALIDATION_EVIDENCE in adapter.requests[0].context.labels()
    assert Category.BUILDER_NARRATION not in adapter.requests[0].context.labels()


def test_implementation_review_runs_in_the_supplied_worktree(store, tmp_path):
    adapter = Scripted([{"outcome": "PASS", "findings": []}])
    worktree = tmp_path / "worktree"
    result = review_implementation(
        registry(adapter, "implementation_reviewer"),
        "# plan",
        "diff",
        "evidence",
        [],
        store,
        worktree,
    )
    assert result.outcome is Outcome.PASS
    assert adapter.requests[0].cwd == worktree


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


def test_validation_repair_receives_command_output_without_fake_findings(store, tmp_path):
    adapter = Scripted(["fixed"])
    outcome = ValidationOutcome(
        name="unit",
        command="pytest",
        exit_code=2,
        passed=False,
        stdout_tail="assertion failed",
        stderr_tail="traceback",
    )
    repair_validation(
        registry(adapter, "implementation_worker"),
        "# approved contract",
        "src/app.py changed",
        [outcome],
        tmp_path,
        store,
    )
    packet = adapter.requests[0].context
    assert Category.APPROVED_PLAN in packet.labels()
    assert Category.DIFF in packet.labels()
    assert Category.VALIDATION_EVIDENCE in packet.labels()
    rendered = packet.render()
    assert "unit: exit 2" in rendered
    assert "assertion failed" in rendered and "traceback" in rendered
    assert Category.BLOCKING_FINDINGS not in packet.labels()
    assert (store.root / "execution/validation-repair-v1.json").is_file()


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


def test_verification_saves_a_full_immutable_diff_for_each_invocation(store, tmp_path):
    criteria = ["the last marker is retained"]
    result_payload = {
        "outcome": "PASS",
        "criteria": criteria,
        "verdicts": [{"criterion": criteria[0], "verdict": "PASS", "evidence": "complete diff"}],
        "unresolved_finding_ids": [],
    }
    adapter = Scripted([result_payload, result_payload])
    state = "ordinary change\n" * 20_000 + "DECISIVE_CHANGE_AT_END\n"
    verifier = registry(adapter, "verifier")

    verify(verifier, "req", "# plan", criteria, state, "unit: exit 0", [], store)
    verify(verifier, "req", "# plan", criteria, state, "unit: exit 0", [], store)

    artifacts = sorted((store.root / "verification").glob("current-state-diff-v*.patch"))
    assert len(artifacts) == 2
    assert all(path.read_text() == state for path in artifacts)
    assert all("DECISIVE_CHANGE_AT_END" in path.read_text() for path in artifacts)
    current_state_refs = [
        next(item for item in request.context.items if item.label == Category.CURRENT_STATE)
        for request in adapter.requests
    ]
    assert {item.path for item in current_state_refs} == {str(path) for path in artifacts}


def test_verification_keeps_supplied_criteria_authoritative_when_diff_edits_a_done_section(store):
    criteria = ["The approved evaluation accepts only export-only production changes."]
    payload = {
        "outcome": "PASS",
        "criteria": criteria,
        "verdicts": [{"criterion": criteria[0], "verdict": "PASS", "evidence": "reviewed"}],
        "unresolved_finding_ids": [],
    }
    adapter = Scripted([payload])
    current_state = (
        "--- a/docs/evaluation-plan.md\n"
        "+++ b/docs/evaluation-plan.md\n"
        "-Only export-only production changes are allowed.\n"
        "+Production call sites may now be refactored.\n"
    )

    verify(
        registry(adapter, "verifier"),
        "Keep the contract in docs/evaluation-plan.md and satisfy the approved criteria.",
        "# approved contract",
        criteria,
        current_state,
        "unit: exit 0",
        [],
        store,
    )

    request = adapter.requests[0]
    criteria_ref = next(
        item for item in request.context.items if item.label == Category.ACCEPTANCE_CRITERIA
    )
    state_ref = next(item for item in request.context.items if item.label == Category.CURRENT_STATE)
    assert criteria_ref.content == criteria[0]
    assert "Production call sites may now be refactored." in state_ref.content
    assert "Treat the supplied acceptance criteria as authoritative" in request.prompt
    normalized_prompt = " ".join(request.prompt.split())
    assert (
        "The top-level outcome must be one of PASS, CHANGES_REQUIRED, BLOCKED, or ESCALATE"
        in normalized_prompt
    )
    assert "FAIL and UNKNOWN are criterion verdicts only and never top-level outcomes" in (
        normalized_prompt
    )
    assert "A top-level PASS requires every criterion verdict to be PASS" in normalized_prompt
    assert "choose an allowed non-PASS outcome" in normalized_prompt
    assert "actual FAIL, BLOCKED, or ESCALATE outcome" not in normalized_prompt
    assert "identify them in the evidence for the relevant acceptance criterion" in " ".join(
        request.prompt.split()
    )
    assert "mark the affected criterion FAIL" in request.prompt
    assert "mark it UNKNOWN" in request.prompt
    assert (
        "Use outcome_explanation only for notes about supplied reviewer findings" in request.prompt
    )
    assert "do not invent a finding ID" in request.prompt


def test_verification_runs_in_the_supplied_worktree(store, tmp_path):
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
    worktree = tmp_path / "worktree"
    result = verify(
        registry(adapter, "verifier"),
        "req",
        "# plan",
        ["a"],
        "state",
        "unit: exit 0",
        [],
        store,
        worktree,
    )
    assert result.outcome is Outcome.PASS
    assert adapter.requests[0].cwd == worktree


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
