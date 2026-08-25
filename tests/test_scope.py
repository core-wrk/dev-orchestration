from dev_orchestration.config.models import Scope
from dev_orchestration.scope import ScopeFence

HELMFAST_OS = ScopeFence(
    include=["pipeline/", "tests/", "planning/", "README.md"],
    exclude=["marketing/", "operations/", "market-research/", "strategy/",
             "context/", "data-templates/", "var/", ".venv/"],
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
