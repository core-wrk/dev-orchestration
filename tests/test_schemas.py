import json

import pytest

from dev_orchestration.domain.findings import Classification, ReviewResult, Verification
from dev_orchestration.schemas import SCHEMA_MODELS, schema_for, write_schema


def test_every_structured_stage_has_a_schema():
    assert set(SCHEMA_MODELS) == {"classification", "review_result", "verification"}


@pytest.mark.parametrize("name", ["classification", "review_result", "verification"])
def test_schema_is_generated_from_the_model_not_hand_written(name, tmp_path):
    model = SCHEMA_MODELS[name]
    path = write_schema(model, tmp_path)
    assert json.loads(path.read_text()) == schema_for(model)


def test_schema_forbids_extra_keys_recursively():
    schema = schema_for(ReviewResult)
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["Finding"]["additionalProperties"] is False


def test_schema_requires_every_provider_object_property():
    for model in (Classification, ReviewResult, Verification):
        schema = schema_for(model)
        objects = [schema, *schema.get("$defs", {}).values()]
        for object_schema in objects:
            properties = object_schema.get("properties", {})
            assert set(properties) <= set(object_schema.get("required", []))


def test_schema_round_trips_a_real_payload():
    assert (
        Classification.model_validate(
            {"tier": "standard", "rationale": "two modules", "profiles": []}
        ).tier
        == "standard"
    )


def test_verification_schema_names_the_verdict_enum():
    assert "UNKNOWN" in json.dumps(schema_for(Verification))
