"""Cross-worktree goal reservations and bounded run history."""

import fcntl
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dev_orchestration.artifacts.store import local_actor
from dev_orchestration.domain.enums import RunState
from dev_orchestration.domain.run import RunManifest
from dev_orchestration.git.repo import GitRepo

MAX_PRODUCT_FAILURES = 3
MAX_NON_PRODUCT_FAILURES = 6
MAX_REPEATED_NON_PRODUCT_FAILURES = 2
_GOAL_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_HUMAN_DECISION_CAUSES = frozenset(
    {
        "implementation_review_escalated",
        "plan_review_escalated",
        "plan_review_exhausted",
        "scope_violation",
        "safety_escalation",
        "restart_governance_blocked",
    }
)
_TERMINAL = frozenset(
    {RunState.COMPLETE_LOCAL, RunState.ESCALATED, RunState.FAILED, RunState.CANCELLED}
)
_HUMAN_DECISION_REASONS = (
    "scope violation",
    "safety escalation",
    "implementation review: blocked",
    "implementation review: escalate",
    "plan review: blocked",
    "plan review: escalate",
)


class GoalError(RuntimeError):
    """A goal is active, closed, or exhausted and cannot accept another run."""


def validate_goal_id(goal_id: str) -> str:
    value = goal_id.strip().lower()
    if not _GOAL_ID.fullmatch(value):
        raise GoalError(
            "goal IDs must be 1–64 lowercase letters, digits, dots, underscores, or hyphens"
        )
    return value


@dataclass(frozen=True)
class GoalReservation:
    goal_id: str
    attempt: int
    generation: int
    has_prior_run: bool = False


def _requires_human_decision(manifest: RunManifest) -> bool:
    if manifest.terminal_cause in _HUMAN_DECISION_CAUSES:
        return True
    reason = (manifest.terminal_reason or "").lower()
    return any(marker in reason for marker in _HUMAN_DECISION_REASONS)


def _failure_class(manifest: RunManifest) -> str:
    # Pre-Goal-7 manifests have no class; preserve their history and count them
    # conservatively as product failures.
    return manifest.failure_class or "product"


class GoalLedger:
    """Coordinate all linked checkouts through the primary checkout's run records."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = GitRepo(repo_root).shared_root()
        self.runs_dir = self.repo_root / ".ai" / "runs"
        self.goals_dir = self.runs_dir / ".goals"

    def _events_path(self, goal_id: str) -> Path:
        return self.goals_dir / f"{goal_id}.jsonl"

    def _read_events(self, goal_id: str) -> list[dict]:
        path = self._events_path(goal_id)
        if not path.exists():
            return []
        try:
            return [
                json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line
            ]
        except (OSError, ValueError) as exc:
            raise GoalError(f"goal history for {goal_id} is unreadable; review {path}") from exc

    def intervention_events(self, goal_id: str) -> list[dict]:
        return [
            event
            for event in self._read_events(validate_goal_id(goal_id))
            if event.get("event") in {"human_decision_recorded", "goal_reopened"}
        ]

    @staticmethod
    def _generation(events: list[dict]) -> int:
        return max((int(item.get("generation", 0)) for item in events), default=0)

    def _manifests(self, goal_id: str) -> list[tuple[Path, RunManifest]]:
        result = []
        if not self.runs_dir.exists():
            return result
        for path in self.runs_dir.glob("*/manifest.json"):
            try:
                manifest = RunManifest.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise GoalError(f"cannot safely inspect run record {path}: {exc}") from exc
            if manifest.goal_id == goal_id:
                result.append((path.parent, manifest))
        return result

    def _check(
        self,
        goal_id: str,
        decision_resolution: tuple[str, str] | None = None,
    ) -> GoalReservation:
        events = self._read_events(goal_id)
        generation = self._generation(events)
        runs = self._manifests(goal_id)
        active = [
            manifest
            for _, manifest in runs
            if manifest.goal_generation == generation and manifest.status not in _TERMINAL
        ]
        if active:
            item = active[0]
            raise GoalError(
                f"goal {goal_id} already has active run {item.run_id} ({item.status}); "
                "resume or cancel that run before starting another"
            )

        current = [manifest for _, manifest in runs if manifest.goal_generation == generation]
        unfinished_cleanup = [
            manifest
            for path, manifest in runs
            if manifest.goal_generation == generation
            and manifest.status in {RunState.FAILED, RunState.ESCALATED, RunState.CANCELLED}
            and not any(
                event.get("event") == "run_cleanup_complete"
                for event in _read_event_file(path / "events.jsonl")
            )
        ]
        if unfinished_cleanup:
            item = unfinished_cleanup[0]
            raise GoalError(
                f"goal {goal_id} is reserved by run {item.run_id}; finish its saved cleanup before retrying"
            )

        completed = [item for item in current if item.status is RunState.COMPLETE_LOCAL]
        if completed:
            raise GoalError(
                f"goal {goal_id} is already complete in run {completed[-1].run_id}; "
                "use a new goal ID for separate work"
            )

        failed = [item for item in current if item.status in {RunState.FAILED, RunState.ESCALATED}]
        resolved_runs = {
            event.get("run_id")
            for event in events
            if event.get("event") == "human_decision_recorded"
            and event.get("generation") == generation
        }
        blocked = [
            item
            for item in failed
            if _requires_human_decision(item) and item.run_id not in resolved_runs
        ]
        if blocked:
            item = blocked[-1]
            if (
                decision_resolution is None
                or decision_resolution[0] != item.run_id
                or not decision_resolution[1].strip()
            ):
                raise GoalError(
                    f"goal {goal_id} stopped at run {item.run_id} for human decision "
                    f"({item.terminal_reason or item.terminal_cause}); "
                    "resolve it and explicitly reopen the goal"
                )
        product = [item for item in failed if _failure_class(item) == "product"]
        non_product = [item for item in failed if _failure_class(item) != "product"]
        ordered_failures = sorted(
            failed,
            key=lambda item: (
                item.goal_attempt or 0,
                item.created_at or datetime.min.replace(tzinfo=UTC),
            ),
        )
        repeated = (
            len(ordered_failures) >= MAX_REPEATED_NON_PRODUCT_FAILURES
            and _failure_class(ordered_failures[-1]) != "product"
            and _failure_class(ordered_failures[-2]) != "product"
            and ordered_failures[-1].failure_class == ordered_failures[-2].failure_class
            and ordered_failures[-1].failure_signature
            and ordered_failures[-1].failure_signature == ordered_failures[-2].failure_signature
        )
        if len(product) >= MAX_PRODUCT_FAILURES:
            ids = ", ".join(item.run_id for item in failed)
            raise GoalError(
                f"goal {goal_id} reached its {MAX_PRODUCT_FAILURES}-product-failure limit "
                f"({MAX_PRODUCT_FAILURES}-attempt limit; {ids}); "
                "resolve the failures and use `dev-orch goal-reopen` with a reason"
            )
        if len(non_product) >= MAX_NON_PRODUCT_FAILURES or repeated:
            item = ordered_failures[-1]
            limit = (
                "two consecutive non-product failures share the same cause signature"
                if repeated
                else f"the {MAX_NON_PRODUCT_FAILURES}-non-product-failure limit"
            )
            raise GoalError(
                f"goal {goal_id} reached {limit} at run {item.run_id}; correct the setup or "
                "evidence, then use `dev-orch goal-reopen` with a human reason"
            )
        if blocked:
            item = blocked[-1]
            self._append(
                goal_id,
                {
                    "event": "human_decision_recorded",
                    "actor": local_actor(),
                    "ts": datetime.now(UTC).isoformat(),
                    "generation": generation,
                    "run_id": item.run_id,
                    "reason": decision_resolution[1].strip(),
                    "recorded_at": datetime.now(UTC).isoformat(),
                },
            )
        return GoalReservation(goal_id, len(failed) + 1, generation, bool(runs))

    @contextmanager
    def reserve(
        self,
        goal_id: str,
        *,
        decision_resolution: tuple[str, str] | None = None,
    ):
        goal_id = validate_goal_id(goal_id)
        self.goals_dir.mkdir(parents=True, exist_ok=True)
        with (self.goals_dir / f"{goal_id}.lock").open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield self._check(goal_id, decision_resolution)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def reopen(self, goal_id: str, reason: str) -> int:
        goal_id = validate_goal_id(goal_id)
        if not reason.strip():
            raise GoalError("reopening a goal requires a non-empty human decision reason")
        with self.reserve_lock(goal_id):
            events = self._read_events(goal_id)
            current_generation = self._generation(events)
            runs = self._manifests(goal_id)
            active = [
                manifest
                for _, manifest in runs
                if manifest.goal_generation == current_generation
                and manifest.status not in _TERMINAL
            ]
            if active:
                raise GoalError(
                    f"goal {goal_id} still has active run {active[0].run_id}; resume or cancel it first"
                )
            incomplete = [
                manifest
                for path, manifest in runs
                if manifest.goal_generation == current_generation
                and manifest.status in {RunState.FAILED, RunState.ESCALATED, RunState.CANCELLED}
                and not any(
                    event.get("event") == "run_cleanup_complete"
                    for event in _read_event_file(path / "events.jsonl")
                )
            ]
            if incomplete:
                raise GoalError(
                    f"goal {goal_id} still has uncleaned run {incomplete[0].run_id}; finish cleanup first"
                )
            generation = current_generation + 1
            self._append(
                goal_id,
                {
                    "event": "goal_reopened",
                    "actor": local_actor(),
                    "ts": datetime.now(UTC).isoformat(),
                    "generation": generation,
                    "reason": reason.strip(),
                    "recorded_at": datetime.now(UTC).isoformat(),
                },
            )
            return generation

    @contextmanager
    def reserve_lock(self, goal_id: str):
        self.goals_dir.mkdir(parents=True, exist_ok=True)
        with (self.goals_dir / f"{goal_id}.lock").open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _append(self, goal_id: str, event: dict) -> None:
        with self._events_path(goal_id).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
            handle.flush()

    def associate(self, run_id: str, goal_id: str) -> None:
        goal_id = validate_goal_id(goal_id)
        if Path(run_id).name != run_id or run_id in {".", ".."}:
            raise GoalError("run ID must name one run directory")
        manifest_path = self.runs_dir / run_id / "manifest.json"
        if (
            manifest_path.resolve().parent != (self.runs_dir / run_id).resolve()
            or not manifest_path.is_file()
        ):
            raise GoalError(f"run {run_id!r} does not exist in this repository")
        path = manifest_path
        with self.reserve_lock(goal_id):
            try:
                manifest = RunManifest.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise GoalError(f"run {run_id!r} has an unreadable manifest") from exc
            if manifest.goal_id is not None:
                raise GoalError(f"run {run_id} already belongs to goal {manifest.goal_id}")
            events = self._read_events(goal_id)
            generation = self._generation(events)
            competing = [
                item
                for _, item in self._manifests(goal_id)
                if item.goal_generation == generation and item.status not in _TERMINAL
            ]
            if competing:
                raise GoalError(f"goal {goal_id} already has active run {competing[0].run_id}")
            manifest.goal_id = goal_id
            manifest.goal_generation = generation
            manifest.goal_attempt = 1 + sum(
                1
                for _, item in self._manifests(goal_id)
                if item.goal_generation == generation
                and item.status in {RunState.FAILED, RunState.ESCALATED}
            )
            temporary = path.parent / f".manifest-goal-{path.parent.name}.tmp"
            temporary.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
            temporary.replace(path)
            self._append(
                goal_id,
                {
                    "event": "legacy_run_associated",
                    "run_id": run_id,
                    "recorded_at": datetime.now(UTC).isoformat(),
                },
            )


def _read_event_file(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    except (OSError, ValueError) as exc:
        raise GoalError(f"run event history is unreadable: {path}") from exc
