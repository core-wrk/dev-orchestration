"""Provider invocation with strict validation and exactly one retry."""

from dataclasses import replace
from pathlib import Path

from pydantic import BaseModel, ValidationError

from dev_orchestration.adapters.base import AgentAdapter, AgentRequest
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
        raise AgentInvocationError(
            f"{model.__name__} provider {result.provider!r} exited with {result.exit_code}"
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
            raise AgentInvocationError(
                f"{model.__name__} provider {retry_result.provider!r} exited with "
                f"{retry_result.exit_code} on the bounded retry"
            )
        try:
            return _validate(model, retry_result.output)
        except (ValidationError, ValueError) as second_error:
            raise SchemaEscalation(
                f"{model.__name__} schema not satisfied after one retry: {second_error}"
            ) from second_error
