import pytest
from pydantic import ValidationError

from dev_orchestration.context.packet import ContextPacket, ContextRef


def test_render_labels_every_item_with_its_provenance():
    rendered = ContextPacket(
        stage="planning",
        items=[
            ContextRef(label="invariants", path="AGENTS.md", content="never push"),
            ContextRef(label="brief", path=None, content="add a flag"),
        ],
    ).render()
    assert rendered.index("AGENTS.md") < rendered.index("never push")
    assert "add a flag" in rendered


def test_render_is_deterministic_and_preserves_item_order():
    packet = ContextPacket(
        stage="planning",
        items=[
            ContextRef(label="second", path=None, content="B"),
            ContextRef(label="first", path=None, content="A"),
        ],
    )
    assert packet.render().index("B") < packet.render().index("A")


def test_empty_packet_renders_empty():
    assert ContextPacket(stage="validation", items=[]).render() == ""


def test_packet_is_immutable():
    with pytest.raises(ValidationError):
        ContextPacket(stage="planning", items=[]).stage = "execution"


def test_total_bytes_and_labels():
    packet = ContextPacket(
        stage="planning", items=[ContextRef(label="a", path=None, content="12345")]
    )
    assert packet.total_bytes() >= 5
    assert packet.labels() == ["a"]
