from pathlib import Path

import pytest

from dev_orchestration.config.models import DENIED_COMMAND_TOKENS
from dev_orchestration.git import guards

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"

# guards.py raises on push/merge/deploy by design and legitimately contains
# the literals; config/models.py holds the deny-list that *rejects* these
# tokens, which is the inverse of invoking them. Both are exempt from the
# invocation scan below. Exemptions are by relative path, not bare filename,
# so an unrelated future models.py elsewhere is still scanned.
EXEMPT_RELATIVE_PATHS = {
    Path("dev_orchestration/git/guards.py"),
    Path("dev_orchestration/config/models.py"),
}


def test_source_root_resolves_to_the_src_directory():
    assert SOURCE_ROOT.is_dir()
    assert SOURCE_ROOT.name == "src"
    assert (SOURCE_ROOT / "dev_orchestration" / "git" / "repo.py").is_file()


@pytest.mark.parametrize("operation", ["push", "merge", "deploy"])
def test_prohibited_operations_raise(operation):
    with pytest.raises(guards.ProhibitedOperationError):
        getattr(guards, operation)()


def test_no_module_invokes_a_remote_write():
    banned = ('"push"', "'push'", '"merge"', "'merge'", "force-with-lease")
    offenders = []
    for path in SOURCE_ROOT.rglob("*.py"):
        if path.relative_to(SOURCE_ROOT) in EXEMPT_RELATIVE_PATHS:
            continue
        text = path.read_text()
        offenders += [f"{path}: {token}" for token in banned if token in text]
    assert offenders == []


def test_the_exempted_deny_list_still_contains_all_prohibited_tokens():
    # The models.py exemption above could hide a real regression (someone
    # quietly shrinking the deny-list). Pin its contents explicitly.
    assert DENIED_COMMAND_TOKENS == {
        "deploy",
        "wrangler",
        "publish",
        "push",
        "merge",
        "release",
    }


def test_the_sandbox_bypass_flag_appears_nowhere():
    for path in SOURCE_ROOT.rglob("*.py"):
        assert "dangerously-bypass" not in path.read_text()
