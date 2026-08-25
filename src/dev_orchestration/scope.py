"""The scope fence: which paths agents may modify.

Governs modification, not reference. Repository code may legitimately read
excluded trees (helmfast-OS's pipeline/runtime/prompts.py loads templates
from three of them); the fence only decides what a run may change.
"""

import fnmatch
from collections.abc import Iterable
from dataclasses import dataclass

from dev_orchestration.config.models import Scope


def _matches(pattern: str, rel_path: str) -> bool:
    if pattern == "**":
        return True
    if pattern.endswith("/"):
        return rel_path == pattern.rstrip("/") or rel_path.startswith(pattern)
    return fnmatch.fnmatch(rel_path, pattern)


@dataclass(frozen=True)
class ScopeFence:
    include: list[str]
    exclude: list[str]

    @classmethod
    def from_config(cls, scope: Scope) -> "ScopeFence":
        return cls(include=list(scope.include), exclude=list(scope.exclude))

    def allows(self, rel_path: str) -> bool:
        normalized = rel_path.removeprefix("./")
        if any(_matches(p, normalized) for p in self.exclude):
            return False
        return any(_matches(p, normalized) for p in self.include)

    def violations(self, paths: Iterable[str]) -> list[str]:
        return [p for p in paths if not self.allows(p)]
