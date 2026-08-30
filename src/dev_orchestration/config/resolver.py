"""Deterministic configuration resolution.

Layer order, lowest precedence first:
  framework defaults -> project class -> active profiles
  -> repo config -> workflow config -> CLI/run override
"""

from typing import Any

PROTECTED_DEFAULTS: dict[str, bool] = {
    "protected.autonomous_push": False,
    "protected.autonomous_merge": False,
    "protected.autonomous_deploy": False,
    "protected.autonomous_production_data_mutation": False,
}


class ProtectedRuleViolation(RuntimeError):
    """A configuration layer tried to set a protected rule."""


def _flatten(data: dict, prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, prefix=f"{path}."))
        else:
            flat[path] = value
    return flat


def _is_strictness_key(dotted: str) -> bool:
    return dotted.rsplit(".", 1)[-1].endswith("_required")


def resolve_policy(layers: list[dict]) -> dict[str, Any]:
    """Merge configuration layers and refuse protected-rule overrides."""
    resolved: dict[str, Any] = {}
    for layer in layers:
        for key, value in _flatten(layer).items():
            if key in PROTECTED_DEFAULTS and value is not False:
                raise ProtectedRuleViolation(
                    f"{key} was set to {value!r}; protected rules are not overridable"
                )
            if _is_strictness_key(key) and key in resolved:
                resolved[key] = bool(resolved[key]) or bool(value)
            else:
                resolved[key] = value
    resolved.update(PROTECTED_DEFAULTS)
    return resolved
