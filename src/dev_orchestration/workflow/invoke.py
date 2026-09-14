"""Provider invocation with strict validation and exactly one retry."""

import json
import re
from dataclasses import replace
from pathlib import Path

from pydantic import BaseModel, ValidationError

from dev_orchestration.adapters.base import (
    AgentAdapter,
    AgentRequest,
    AgentResult,
    agent_result_record,
    provider_failure_reason,
)
from dev_orchestration.schemas import write_schema


class SchemaEscalation(RuntimeError):
    """A provider failed to produce the required structured result."""


class AgentInvocationError(RuntimeError):
    """A provider did not complete a request successfully."""


def _validate(model: type[BaseModel], output: dict | str) -> BaseModel:
    if isinstance(output, str):
        return model.model_validate_json(output)
    return model.model_validate(output)


def invoke_structured(
    adapter: AgentAdapter,
    request: AgentRequest,
    model: type[BaseModel],
    schema_dir: Path,
) -> BaseModel:
    """Attach a durable generated schema to both attempts."""
    schema_path = request.expected_schema or write_schema(model, schema_dir)
    request = replace(request, expected_schema=schema_path)
    result = adapter.run(request)
    if result.exit_code != 0:
        _persist_provider_failure(schema_dir, request.role, result, attempt=1)
        raise AgentInvocationError(
            f"{model.__name__} {provider_failure_reason(request.role, result, attempt=1)}"
        )
    try:
        return _validate(model, result.output)
    except (ValidationError, ValueError) as first_error:
        correction = (
            f"{request.prompt}\n\nYour previous response did not satisfy the required "
            f"{model.__name__} schema:\n{first_error}\nReturn only valid {model.__name__} JSON."
        )
        retry = replace(request, prompt=correction, expected_schema=schema_path)
        retry_result = adapter.run(retry)
        if retry_result.exit_code != 0:
            _persist_provider_failure(schema_dir, request.role, retry_result, attempt=2)
            raise AgentInvocationError(
                f"{model.__name__} {provider_failure_reason(request.role, retry_result, attempt=2)}"
            )
        try:
            return _validate(model, retry_result.output)
        except (ValidationError, ValueError) as second_error:
            raise SchemaEscalation(
                f"{model.__name__} schema not satisfied after one retry: {second_error}"
            ) from second_error


def _persist_provider_failure(
    schema_dir: Path,
    role: str,
    result: AgentResult,
    *,
    attempt: int,
) -> None:
    """Keep failed structured calls diagnosable in the run directory."""
    artifact_root = schema_dir.parent if schema_dir.name == "schemas" else schema_dir
    target_dir = artifact_root / "execution"
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_role = re.sub(r"[^a-z0-9_-]+", "-", role.lower()).strip("-") or "provider"
    target = target_dir / f"provider-failure-{safe_role}-attempt-{attempt}.json"
    target.write_text(
        json.dumps(agent_result_record(result), indent=2, default=str) + "\n",
        encoding="utf-8",
    )
