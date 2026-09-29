import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dev_orchestration.adapters.base import AgentResult
from dev_orchestration.adapters.usage import UsageObservation, parse_codex_snapshot, rejection
from dev_orchestration.domain.enums import RunState
from dev_orchestration.workflow.runner import execute_run
from dev_orchestration.workflow.scheduler import SchedulerQueue
from tests.workflow.test_resume import CONFIG, CRITERIA, Scripted, registry, repo


def test_codex_five_hour_threshold_and_missing_window():
    now = datetime.now(UTC)
    reset = int((now + timedelta(hours=1)).timestamp())
    for used, state in [(89, "available"), (90, "near")]:
        observed = parse_codex_snapshot(
            {
                "rateLimits": {
                    "primary": {"windowDurationMins": 300, "usedPercent": used, "resetsAt": reset},
                }
            },
            observed_at=now,
        )
        assert observed.state == state
        assert observed.limit_kind == "five_hour"
        assert observed.reset_at is not None
    absent = parse_codex_snapshot(
        {
            "rateLimits": {
                "primary": {"windowDurationMins": 10080, "usedPercent": 30},
                "secondary": None,
            }
        },
        observed_at=now,
    )
    assert absent.state == "unknown"


def test_sanitized_installed_codex_quota_fixture_parses():
    path = Path(__file__).resolve().parents[1] / "fixtures/codex_rate_limits_available.json"
    observation = parse_codex_snapshot(json.loads(path.read_text()), observed_at=datetime.now(UTC))
    assert observation.provider == "codex"
    assert observation.limit_kind == "five_hour"
    assert observation.state == "available"
    assert observation.used_percent == 18
    assert observation.reset_at is not None


def test_multiple_exhausted_windows_need_every_reset_time():
    now = datetime.now(UTC)
    weekly_reset = int((now + timedelta(days=7)).timestamp())
    payload = {
        "rateLimits": {
            "primary": {"windowDurationMins": 300, "usedPercent": 100},
            "secondary": {
                "windowDurationMins": 10080,
                "usedPercent": 100,
                "resetsAt": weekly_reset,
            },
        }
    }
    observed = parse_codex_snapshot(payload, observed_at=now)
    assert observed.state == "exhausted"
    assert observed.limit_kind == "unknown"
    assert observed.reset_at is None
    payload["rateLimits"]["primary"]["resetsAt"] = int((now + timedelta(hours=1)).timestamp())
    known = parse_codex_snapshot(payload, observed_at=now)
    assert known.limit_kind == "weekly"
    assert known.reset_at == datetime.fromtimestamp(weekly_reset, UTC)


class QuotaProbe(Scripted):
    def __init__(self, used):
        super().__init__()
        self.used = used

    def observe_usage(self):
        return UsageObservation(
            provider="fake",
            limit_kind="five_hour",
            state="near" if self.used >= 90 else "available",
            used_percent=self.used,
            reset_at=datetime.now(UTC) + timedelta(hours=1),
            observed_at=datetime.now(UTC),
            source="fake_quota",
        )


def test_near_limit_pauses_before_next_role_but_89_percent_runs(tmp_path):
    git = repo(tmp_path)
    queue = SchedulerQueue(tmp_path / "queue.json")
    near = QuotaProbe(90)
    paused = execute_run(
        git,
        CONFIG,
        registry(near),
        "change app",
        tmp_path / "wt",
        CRITERIA,
        auto_resume=True,
        scheduler_queue=queue,
    )
    assert paused.final_state is RunState.PAUSED_USAGE
    assert near.seen == []
    # A fresh repository is needed because the paused run retains its branch.
    second_root = tmp_path / "second"
    second_root.mkdir()
    other = repo(second_root)
    available = QuotaProbe(89)
    completed = execute_run(
        other,
        CONFIG,
        registry(available),
        "change app",
        second_root / "wt",
        CRITERIA,
        auto_resume=True,
        scheduler_queue=queue,
    )
    assert completed.final_state is RunState.COMPLETE_LOCAL
    assert available.seen[0] == "classifier"


def test_only_provider_error_channel_can_confirm_quota():
    now = datetime.now(UTC)
    model_prose = AgentResult("claude", None, 0, "I hit a five-hour usage limit", now, now)
    assert rejection(model_prose) is None
    timeout = AgentResult("codex", None, 1, "", now, now, stderr="timed out")
    assert rejection(timeout) is None
    credits = AgentResult("claude", None, 1, "", now, now, stderr="usage limit; buy credits")
    assert rejection(credits) is None
    context = AgentResult("claude", None, 1, "", now, now, stderr="context limit reached")
    assert rejection(context) is None
    tokens = AgentResult("codex", None, 1, "", now, now, stderr="token limit reached")
    assert rejection(tokens) is None
    api_throttle = AgentResult("codex", None, 1, "", now, now, stderr="rate limit exceeded")
    assert rejection(api_throttle) is None
    api_quota = AgentResult("codex", None, 1, "", now, now, stderr="API quota exceeded")
    assert rejection(api_quota) is None
    weekly = AgentResult(
        "claude",
        None,
        0,
        "",
        now,
        now,
        diagnostic={
            "type": "error",
            "error": "weekly usage limit reached",
            "reset_at": (now + timedelta(days=7)).isoformat(),
        },
    )
    observed = rejection(weekly)
    assert observed is not None
    assert observed.limit_kind == "weekly"
    assert observed.reset_at is not None


@pytest.mark.parametrize(
    "message,has_reset,expected_kind",
    [
        ("weekly usage limit reached", True, "weekly"),
        ("usage limit reached", False, "unknown"),
        ("five-hour usage limit reached", False, "five_hour"),
    ],
)
def test_rejection_retry_time_depends_on_confirmed_window(
    tmp_path, message, has_reset, expected_kind
):
    class RejectReview(Scripted):
        def run(self, request):
            if request.role == "plan_reviewer":
                now = datetime.now(UTC)
                diagnostic = {"type": "error", "error": message}
                if has_reset:
                    diagnostic["reset_at"] = (now + timedelta(days=7)).isoformat()
                return AgentResult(self.name, None, 0, "", now, now, diagnostic=diagnostic)
            return super().run(request)

    git = repo(tmp_path)
    queue = SchedulerQueue(tmp_path / "queue.json")
    outcome = execute_run(
        git,
        CONFIG,
        registry(RejectReview()),
        "change app",
        tmp_path / "wt",
        CRITERIA,
        auto_resume=True,
        scheduler_queue=queue,
    )
    assert outcome.final_state is RunState.PAUSED_USAGE
    from dev_orchestration.artifacts.store import RunStore

    pause = RunStore(git.root, outcome.run_id).read_manifest().pause
    assert pause.limit_kind == expected_kind
    if expected_kind == "unknown":
        assert pause.next_attempt_at is None
        assert queue.due(datetime.now(UTC) + timedelta(days=8)) == []
    else:
        assert pause.next_attempt_at is not None
        assert pause.next_attempt_at > pause.observed_at
        assert len(queue.due(pause.next_attempt_at + timedelta(seconds=1))) == 1
        if expected_kind == "five_hour":
            assert pause.next_attempt_at == pause.observed_at + timedelta(hours=5)
