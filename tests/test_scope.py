import pytest

from dev_orchestration.config.models import Scope
from dev_orchestration.scope import ScopeFence

HELMFAST_OS = ScopeFence(
    include=["pipeline/", "tests/", "planning/", "README.md"],
    exclude=[
        "marketing/",
        "operations/",
        "market-research/",
        "strategy/",
        "context/",
        "data-templates/",
        "var/",
        ".venv/",
    ],
)


def test_included_paths_are_allowed():
    assert HELMFAST_OS.allows("pipeline/runtime/store.py")
    assert HELMFAST_OS.allows("tests/runtime/test_store.py")
    assert HELMFAST_OS.allows("README.md")


def test_business_knowledge_is_fenced_out():
    assert not HELMFAST_OS.allows("strategy/icp-definition.md")
    assert not HELMFAST_OS.allows("marketing/brand/visual-identity.md")
    assert not HELMFAST_OS.allows("market-research/prospect-lists/README.md")


def test_paths_outside_include_are_denied_even_without_exclude():
    assert not HELMFAST_OS.allows("SOURCE-MANIFEST.md")


def test_exclude_beats_include():
    fence = ScopeFence(include=["**"], exclude=["secrets/"])
    assert fence.allows("src/app.py")
    assert not fence.allows("secrets/keys.json")


def test_violations_lists_every_offending_path():
    changed = ["pipeline/runtime/store.py", "strategy/pricing.md", "var/cache.db"]
    assert HELMFAST_OS.violations(changed) == ["strategy/pricing.md", "var/cache.db"]


def test_from_config_builds_an_equivalent_fence():
    fence = ScopeFence.from_config(Scope(include=["src/"], exclude=["src/vendor/"]))
    assert fence.allows("src/main.py")
    assert not fence.allows("src/vendor/lib.py")


def test_dot_prefixed_excluded_paths_are_denied():
    """Test that dot-prefixed excluded patterns correctly exclude dot-prefixed paths."""
    fence = ScopeFence(include=["**"], exclude=[".venv/"])
    assert not fence.allows(".venv/lib/python3.14/site-packages/foo.py")
    assert not fence.allows(".venv/bin/python")

    fence2 = ScopeFence(include=[".github/"], exclude=[])
    assert fence2.allows(".github/workflows/ci.yml")
    assert fence2.allows(".github/workflows/deploy.yml")


def test_empty_include_denies_all_paths():
    """Test that a fence with empty include list denies all paths (fail-closed)."""
    fence = ScopeFence(include=[], exclude=[])
    assert not fence.allows("anything.py")
    assert not fence.allows("src/app.py")
    assert not fence.allows("deeply/nested/path/file.txt")


def test_double_star_includes_deeply_nested_paths():
    """Test that ** include pattern matches deeply nested paths."""
    fence = ScopeFence(include=["**"], exclude=[])
    assert fence.allows("a/b/c/d.py")
    assert fence.allows("deeply/nested/path/to/file.txt")
    assert fence.allows("x.py")


def test_a_directory_pattern_without_a_trailing_slash_still_excludes():
    # The natural way to write a fence in YAML. Under the previous fnmatch
    # matcher this excluded nothing at all, so a user could fence off a
    # secrets directory, see doctor report the fence as fine, and have it
    # protect nothing.
    fence = ScopeFence(include=["**"], exclude=["secrets"])
    assert fence.allows("secrets/keys.json") is False
    assert fence.allows("secrets") is False
    assert fence.allows("src/app.py") is True


def test_a_bare_pattern_does_not_match_a_merely_similar_prefix():
    fence = ScopeFence(include=["**"], exclude=["secrets"])
    assert fence.allows("secrets-public/readme.md") is True


@pytest.mark.parametrize("pattern", [".git", "node_modules", ".env"])
def test_common_dotfile_and_vendor_excludes_work_without_a_slash(pattern):
    fence = ScopeFence(include=["**"], exclude=[pattern])
    assert fence.allows(f"{pattern}/inner.txt") is False


def test_a_single_star_does_not_cross_a_directory_separator():
    # fnmatch translated * to .*, so include=["docs/*"] silently meant
    # "everything under docs/, recursively" -- over-permissive, which is the
    # fail-open direction for an include list.
    fence = ScopeFence(include=["docs/*"], exclude=[])
    assert fence.allows("docs/readme.md") is True
    assert fence.allows("docs/nested/deep.md") is False


def test_a_double_star_does_cross_a_directory_separator():
    fence = ScopeFence(include=["docs/**"], exclude=[])
    assert fence.allows("docs/nested/deep.md") is True


@pytest.mark.parametrize(
    "escaping",
    ["../../etc/passwd", "/etc/passwd", "../sibling-repo/src/app.py", "a/../../b"],
)
def test_paths_that_escape_the_repository_are_never_allowed(escaping):
    # A run's worktree sits under a shared worktree root, so ".." reaches
    # sibling repositories. The default include of ["**"] matched these.
    fence = ScopeFence(include=["**"], exclude=[])
    assert fence.allows(escaping) is False
    assert fence.violations([escaping]) == [escaping]
