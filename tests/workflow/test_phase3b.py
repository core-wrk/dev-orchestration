"""Phase 3b: the verifier gets the evidence the orchestrator already has, and is asked once more."""

from datetime import UTC, datetime

from dev_orchestration.adapters.base import AgentResult, AgentTimeoutError
from dev_orchestration.config.models import ValidationCommand
from dev_orchestration.domain.enums import RunState
from dev_orchestration.domain.findings import AcceptanceVerdict, CriterionVerdict
from dev_orchestration.workflow.criteria_commands import plan_criteria_commands
from dev_orchestration.workflow.recovery import resume_run
from tests.workflow import test_resume
from tests.workflow.run_helpers import execute_run
from tests.workflow.test_phase2 import Varying, _config, _labels, _manifest, _store
from tests.workflow.test_runner import CRITERIA, registry, repo, script

PYTEST = ".venv/bin/python -m pytest -p no:cacheprovider"


def _answer(verdict="PASS", missing=None, outcome=None):
    return {
        "outcome": outcome or ("PASS" if verdict == "PASS" else "BLOCKED"),
        "verdicts": [
            {
                "criterion_id": "AC1",
                "verdict": verdict,
                "evidence": "seen" if verdict == "PASS" else "could not see it",
                "missing_evidence": missing,
            }
        ],
        "unresolved_finding_ids": [],
        "outcome_explanation": [],
    }


def _validation(*commands):
    return {
        f"check{index}": ValidationCommand(command=command, required_for=["standard"])
        for index, command in enumerate(commands)
    }


def _verifiers(adapter):
    return [r for r in adapter.requests if r.role == "verifier"]


# --- commands named in requirements ------------------------------------------------------


def test_a_quoted_extension_of_a_registered_command_is_planned_to_run():
    runnable, refused = plan_criteria_commands(
        ["`.venv/bin/python -m pytest tests/x.py -k y` passes"], _validation(PYTEST)
    )

    assert [command.argv for command in runnable] == [
        [".venv/bin/python", "-m", "pytest", "tests/x.py", "-k", "y"]
    ]
    assert refused == []


def test_registered_commands_paths_and_names_are_neither_run_nor_refused():
    runnable, refused = plan_criteria_commands(
        [f"`{PYTEST}` exits 0", "`src/app.py` exists", "`restart_status` reports it"],
        _validation(PYTEST),
    )

    assert (runnable, refused) == ([], [])


def test_other_programs_shells_control_characters_and_denied_words_are_refused():
    criteria = [
        "`.venv/bin/python -c 'import os'` works",
        "`sh -c 'echo hi'` works",
        "`.venv/bin/python -m pytest tests; rm -rf src` works",
        "`.venv/bin/python -m pytest --publish-results` works",
        "`env X=1 .venv/bin/python -m pytest` works",
    ]
    runnable, refused = plan_criteria_commands(
        criteria, _validation(PYTEST, "sh -c true", "env X=1 true")
    )

    assert runnable == []
    assert len(refused) == 5
    assert all(note.startswith("Not run: ") for note in refused)


def test_a_quoted_command_runs_and_its_result_reaches_the_verifier(tmp_path):
    criteria = ["`python3 -m platform --terse` prints the platform"]
    adapter = Varying(script(verifier=_answer()))
    outcome = execute_run(
        repo(tmp_path),
        _config("python3 -m platform"),
        registry(adapter),
        "go",
        tmp_path / "wt",
        criteria,
    )

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    evidence = _labels(_verifiers(adapter)[0], "validation_evidence")[0]
    assert "`python3 -m platform --terse`: exit 0 (PASS)" in evidence


def test_a_refused_command_is_explained_to_the_verifier(tmp_path):
    criteria = ["`sh -c 'echo hi'` prints hi"]
    adapter = Varying(script(verifier=_answer()))
    execute_run(repo(tmp_path), _config("true"), registry(adapter), "go", tmp_path / "wt", criteria)

    evidence = _labels(_verifiers(adapter)[0], "validation_evidence")[0]
    assert "Not run: `sh -c 'echo hi'`" in evidence


# --- run facts ---------------------------------------------------------------------------


def test_the_verifier_receives_a_summary_of_run_facts(tmp_path):
    adapter = Varying(script(verifier=_answer()))
    outcome = execute_run(
        repo(tmp_path), _config("true"), registry(adapter), "go", tmp_path / "wt", CRITERIA
    )

    facts = _labels(_verifiers(adapter)[0], "run_facts")[0]
    manifest = _manifest(outcome)
    assert "Implementation review: PASS" in facts
    assert f"Starting commit: {manifest.git.base_commit}" in facts
    assert f"Worktree (read-only): {tmp_path / 'wt'}" in facts
    assert "Worker's latest report:\ndone" in facts


# --- several missing kinds and one more ask ----------------------------------------------


def test_an_answer_saved_with_a_single_missing_kind_still_loads_as_a_list():
    saved = {"criterion": "x", "verdict": "UNKNOWN", "evidence": "e", "missing_evidence": "test"}
    assert AcceptanceVerdict.model_validate(saved).missing_evidence == ["test"]
    answered = {"criterion_id": "AC1", "verdict": "UNKNOWN", "evidence": "e"} | {
        "missing_evidence": "file"
    }
    assert CriterionVerdict.model_validate(answered).missing_evidence == ["file"]


def test_unknown_asking_for_a_file_gets_one_more_ask_and_no_worker_call(tmp_path):
    adapter = Varying(script() | {"verifier": [_answer("UNKNOWN", ["file"]), _answer()]})
    outcome = execute_run(
        repo(tmp_path), _config("true"), registry(adapter), "go", tmp_path / "wt", CRITERIA
    )

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert adapter.worker_calls == 1
    first, second = _verifiers(adapter)
    assert "Your previous answer" not in _labels(first, "run_facts")[0]
    previous = _labels(second, "run_facts")[0]
    assert "Your previous answer" in previous and "could not see it" in previous
    manifest = _manifest(outcome)
    assert manifest.verification_ask_rounds == [0]
    assert manifest.verification_repairs_reserved == 0


def test_asking_again_for_the_same_kind_escalates(tmp_path):
    adapter = Varying(script(verifier=_answer("UNKNOWN", ["file", "run_fact"])))
    outcome = execute_run(
        repo(tmp_path), _config("true"), registry(adapter), "go", tmp_path / "wt", CRITERIA
    )

    assert outcome.final_state is RunState.ESCALATED
    assert adapter.worker_calls == 1
    assert len(_verifiers(adapter)) == 2
    assert _manifest(outcome).terminal_cause == "verification_escalated"


def test_unknown_asking_for_a_file_and_a_test_gets_an_evidence_repair(tmp_path):
    def add_test(request, call):
        if call == 2:
            (request.cwd / "src" / "test_app.py").write_text("def test_x():\n    pass\n")

    adapter = Varying(
        script() | {"verifier": [_answer("UNKNOWN", ["file", "test"]), _answer()]},
        extra=add_test,
    )
    outcome = execute_run(
        repo(tmp_path), _config("true"), registry(adapter), "go", tmp_path / "wt", CRITERIA
    )

    assert outcome.final_state is RunState.COMPLETE_LOCAL
    assert adapter.worker_calls == 2
    assert _manifest(outcome).verification_ask_rounds == []


def test_unknown_naming_other_still_escalates_without_an_ask(tmp_path):
    adapter = Varying(script(verifier=_answer("UNKNOWN", ["file", "other"])))
    outcome = execute_run(
        repo(tmp_path), _config("true"), registry(adapter), "go", tmp_path / "wt", CRITERIA
    )

    assert outcome.final_state is RunState.ESCALATED
    assert len(_verifiers(adapter)) == 1


# --- resume ------------------------------------------------------------------------------


class Answers(test_resume.Scripted):
    def __init__(self, answers, *, timeout_on_verifier_call=None):
        super().__init__()
        self.answers = list(answers)
        self.timeout_on = timeout_on_verifier_call
        self.verifier_calls = 0
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        if request.role != "verifier":
            return super().run(request)
        self.seen.append(request.role)
        self.verifier_calls += 1
        if self.verifier_calls == self.timeout_on:
            raise AgentTimeoutError(request.role, 7, self.name)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        now = datetime.now(UTC)
        return AgentResult(self.name, None, 0, answer, now, now)


def test_an_interrupted_ask_resumes_as_an_ask(tmp_path):
    git = test_resume.repo(tmp_path)
    first = execute_run(
        git,
        test_resume.CONFIG,
        test_resume.registry(Answers([_answer("UNKNOWN", ["file"])], timeout_on_verifier_call=2)),
        "change app",
        tmp_path / "wt",
        test_resume.CRITERIA,
    )
    assert first.final_state is RunState.PAUSED_INTERRUPTED
    assert _store(git, first).latest_checkpoint()["next_stage"] == "verifier"

    adapter = Answers([_answer()])
    resumed = resume_run(git, test_resume.CONFIG, test_resume.registry(adapter), first.run_id)

    assert resumed.final_state is RunState.COMPLETE_LOCAL
    assert adapter.seen == ["verifier"]
    assert "Your previous answer" in _labels(adapter.requests[0], "run_facts")[0]


def test_a_resumed_rejection_asking_for_a_file_gets_its_ask(tmp_path):
    git = test_resume.repo(tmp_path)
    first = execute_run(
        git,
        test_resume.CONFIG,
        test_resume.registry(test_resume.Scripted(timeout_worker=True)),
        "change app",
        tmp_path / "wt",
        test_resume.CRITERIA,
    )

    adapter = Answers([_answer("UNKNOWN", ["command"]), _answer()])
    resumed = resume_run(git, test_resume.CONFIG, test_resume.registry(adapter), first.run_id)

    assert resumed.final_state is RunState.COMPLETE_LOCAL
    assert adapter.verifier_calls == 2
    assert _store(git, first).read_manifest().verification_ask_rounds == [0]
