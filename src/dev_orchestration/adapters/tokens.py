"""Provider-neutral token accounting.

Providers count differently, so each adapter normalises what it reports into
one shape before it reaches the run artifacts:

- ``input_tokens``: prompt tokens that were *not* served from cache.
- ``cached_input_tokens``: prompt tokens read from cache.
- ``cache_creation_tokens``: prompt tokens written to cache (Claude only).
- ``output_tokens``: generated tokens, including reasoning.
- ``reasoning_tokens``: the reasoning share of ``output_tokens``. It is
  informational and is never added to ``total_tokens`` a second time.

Codex reports ``input_tokens`` inclusive of cached tokens; Claude reports them
exclusive. Normalising here keeps a cross-provider comparison honest.
"""

from __future__ import annotations

import json

COUNT_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_creation_tokens",
    "output_tokens",
    "reasoning_tokens",
)


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _finish(counts: dict[str, int], **extra: object) -> dict:
    counts["total_tokens"] = (
        counts["input_tokens"]
        + counts["cached_input_tokens"]
        + counts["cache_creation_tokens"]
        + counts["output_tokens"]
    )
    return {**counts, **{key: value for key, value in extra.items() if value is not None}}


def codex_usage(stdout: str) -> dict | None:
    """Sum every ``turn.completed`` usage event in ``codex exec --json`` output."""
    counts = dict.fromkeys(COUNT_FIELDS, 0)
    seen = False
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        seen = True
        cached = _count(usage.get("cached_input_tokens"))
        counts["input_tokens"] += max(_count(usage.get("input_tokens")) - cached, 0)
        counts["cached_input_tokens"] += cached
        counts["output_tokens"] += _count(usage.get("output_tokens"))
        counts["reasoning_tokens"] += _count(usage.get("reasoning_output_tokens"))
    return _finish(counts) if seen else None


def claude_usage(envelope: object) -> dict | None:
    """Read usage from the ``--output-format json`` result envelope."""
    if not isinstance(envelope, dict) or not isinstance(envelope.get("usage"), dict):
        return None
    usage = envelope["usage"]
    counts = dict.fromkeys(COUNT_FIELDS, 0)
    counts["input_tokens"] = _count(usage.get("input_tokens"))
    counts["cached_input_tokens"] = _count(usage.get("cache_read_input_tokens"))
    counts["cache_creation_tokens"] = _count(usage.get("cache_creation_input_tokens"))
    counts["output_tokens"] = _count(usage.get("output_tokens"))
    cost = envelope.get("total_cost_usd")
    turns = envelope.get("num_turns")
    return _finish(
        counts,
        cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
        num_turns=turns if isinstance(turns, int) else None,
    )
