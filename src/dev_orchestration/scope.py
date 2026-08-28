"""The scope fence: which paths agents may modify.

Governs modification, not reference. Repository code may legitimately read
excluded trees (helmfast-OS's pipeline/runtime/prompts.py loads templates
from three of them); the fence only decides what a run may change.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath

from dev_orchestration.config.models import Scope

_GLOB_METACHARACTERS = ("*", "?", "[")


def _matches(pattern: str, rel_path: str) -> bool:
    """Match one fence pattern against a repository-relative path.

    Three deliberate departures from a bare fnmatch:

    A pattern with no glob metacharacter is a **path prefix**, not a literal
    filename. `exclude: [secrets]` is the natural way to write a fence and
    under fnmatch it excluded nothing at all -- "secrets" does not match
    "secrets/keys.json" -- so a user could fence off a directory, be told
    the fence was fine, and have it protect nothing.

    A trailing slash keeps meaning the same thing, so existing configuration
    is unaffected.

    Otherwise matching uses PurePosixPath.full_match, where `*` does not cross
    a directory separator and `**` does. Under fnmatch `*` translated to `.*`,
    so `include: ["docs/*"]` silently meant "everything under docs/,
    recursively" -- over-permissive, which is the fail-open direction.
    """
    if pattern == "**":
        return True
    if pattern.endswith("/"):
        return rel_path == pattern.rstrip("/") or rel_path.startswith(pattern)
    if not any(ch in pattern for ch in _GLOB_METACHARACTERS):
        return rel_path == pattern or rel_path.startswith(pattern + "/")
    return PurePosixPath(rel_path).full_match(pattern)


def _escapes_the_repository(rel_path: str) -> bool:
    """True if a path is absolute or climbs out of the repository root.

    A run's worktree lives under a shared worktree root, so `../..` reaches
    sibling repositories. Nothing downstream re-checks this, and the default
    `include: ["**"]` matched everything -- including "/etc/passwd" and
    "../../etc/passwd" -- so an escaping path was *allowed* by the fence
    whose entire job is to contain modification.
    """
    if rel_path.startswith("/"):
        return True
    return ".." in PurePosixPath(rel_path).parts


@dataclass(frozen=True)
class ScopeFence:
    include: list[str]
    exclude: list[str]

    @classmethod
    def from_config(cls, scope: Scope) -> "ScopeFence":
        return cls(include=list(scope.include), exclude=list(scope.exclude))

    def allows(self, rel_path: str) -> bool:
        normalized = rel_path.removeprefix("./")
        # Checked before include/exclude, so no pattern can permit an escape.
        if _escapes_the_repository(normalized):
            return False
        if any(_matches(p, normalized) for p in self.exclude):
            return False
        return any(_matches(p, normalized) for p in self.include)

    def violations(self, paths: Iterable[str]) -> list[str]:
        return [p for p in paths if not self.allows(p)]
