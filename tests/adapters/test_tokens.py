import json

from dev_orchestration.adapters.tokens import claude_usage, codex_usage


def _turn(**usage):
    return json.dumps({"type": "turn.completed", "usage": usage}) + "\n"


def test_codex_usage_separates_cached_from_uncached_input():
    stdout = _turn(input_tokens=1000, cached_input_tokens=800, output_tokens=50)
    assert codex_usage(stdout) == {
        "input_tokens": 200,
        "cached_input_tokens": 800,
        "cache_creation_tokens": 0,
        "output_tokens": 50,
        "reasoning_tokens": 0,
        "total_tokens": 1050,
    }


def test_codex_usage_sums_turns_and_does_not_double_count_reasoning():
    stdout = _turn(input_tokens=100, output_tokens=40, reasoning_output_tokens=30) + _turn(
        input_tokens=10, cached_input_tokens=10, output_tokens=5, reasoning_output_tokens=2
    )
    usage = codex_usage(stdout)
    assert usage["input_tokens"] == 100
    assert usage["cached_input_tokens"] == 10
    assert usage["output_tokens"] == 45
    assert usage["reasoning_tokens"] == 32
    assert usage["total_tokens"] == 155


def test_codex_usage_is_none_without_a_usage_event():
    assert codex_usage('{"type":"turn.failed"}\nnot json\n') is None
    assert codex_usage("") is None


def test_claude_usage_reads_envelope_including_cost():
    envelope = {
        "usage": {
            "input_tokens": 12,
            "cache_read_input_tokens": 300,
            "cache_creation_input_tokens": 40,
            "output_tokens": 8,
        },
        "total_cost_usd": 0.0421,
        "num_turns": 3,
    }
    usage = claude_usage(envelope)
    assert usage["input_tokens"] == 12
    assert usage["cached_input_tokens"] == 300
    assert usage["cache_creation_tokens"] == 40
    assert usage["total_tokens"] == 360
    assert usage["cost_usd"] == 0.0421
    assert usage["num_turns"] == 3


def test_claude_usage_is_none_when_envelope_has_no_usage():
    assert claude_usage({"result": "x"}) is None
    assert claude_usage(None) is None
    assert claude_usage("text") is None
