from dev_orchestration.workflow.reporting import render_token_usage, summarize_token_usage


def _event(role, model, total, *, reasoning="high", provider="codex", usage=True, **extra):
    body = {
        "input_tokens": total,
        "cached_input_tokens": 0,
        "cache_creation_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": total,
        **extra,
    }
    return {
        "event": "token_usage",
        "role": role,
        "provider": provider,
        "model": model,
        "reasoning": reasoning,
        "duration_seconds": 2.0,
        "usage": body if usage else None,
    }


def test_summary_groups_by_role_model_and_effort_and_sorts_by_total():
    events = [
        {"event": "state_changed"},
        _event("verifier", "luna", 100),
        _event("plan_reviewer", "sol", 500),
        _event("verifier", "luna", 50),
        _event("verifier", "luna", 10, reasoning="low"),
    ]
    rows = summarize_token_usage(events)
    assert [(r["role"], r["reasoning"], r["calls"], r["total_tokens"]) for r in rows] == [
        ("plan_reviewer", "high", 1, 500),
        ("verifier", "high", 2, 150),
        ("verifier", "low", 1, 10),
    ]
    assert rows[1]["duration_seconds"] == 4.0


def test_unreported_calls_are_counted_not_dropped():
    rows = summarize_token_usage([_event("planner", "opus", 0, usage=False)])
    assert rows[0]["calls"] == 1
    assert rows[0]["unreported"] == 1
    assert "1 unreported" in render_token_usage(rows)


def test_cost_is_summed_only_when_reported():
    rows = summarize_token_usage(
        [
            _event("planner", "opus", 10, provider="claude", cost_usd=0.5),
            _event("planner", "opus", 10, provider="claude", cost_usd=0.25),
        ]
    )
    assert rows[0]["cost_usd"] == 0.75
    assert "0.7500" in render_token_usage(rows)


def test_render_handles_empty_and_shows_totals():
    assert render_token_usage([]) == "No token usage recorded."
    text = render_token_usage(
        summarize_token_usage([_event("a", "x", 1000), _event("b", "y", 2000)])
    )
    assert text.splitlines()[-1].split()[0] == "TOTAL"
    assert "3,000" in text.splitlines()[-1]
