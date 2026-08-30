"""A provenance-labelled, immutable packet supplied to one workflow stage."""

from pydantic import BaseModel, Field


class ContextRef(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}

    label: str
    path: str | None
    content: str

    def render(self) -> str:
        source = f" ({self.path})" if self.path else ""
        return f"## {self.label}{source}\n\n{self.content.rstrip()}\n"


class ContextPacket(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}

    stage: str
    items: list[ContextRef] = Field(default_factory=list)

    def render(self) -> str:
        return "\n".join(item.render() for item in self.items)

    def labels(self) -> list[str]:
        return [item.label for item in self.items]

    def total_bytes(self) -> int:
        return sum(len(item.content.encode("utf-8")) for item in self.items)
