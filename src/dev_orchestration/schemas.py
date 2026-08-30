"""Provider-facing JSON schemas generated from the Pydantic result models."""

import json
from pathlib import Path

from pydantic import BaseModel

from dev_orchestration.domain.findings import Classification, ReviewResult, Verification

SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "classification": Classification,
    "review_result": ReviewResult,
    "verification": Verification,
}


def schema_for(model: type[BaseModel]) -> dict:
    schema = model.model_json_schema()
    schema["additionalProperties"] = False
    return schema


def write_schema(model: type[BaseModel], directory: Path) -> Path:
    try:
        name = next(name for name, candidate in SCHEMA_MODELS.items() if candidate is model)
    except StopIteration as exc:
        raise ValueError(f"no provider schema is registered for {model!r}") from exc
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{name}.json"
    target.write_text(json.dumps(schema_for(model), indent=2) + "\n", encoding="utf-8")
    return target
