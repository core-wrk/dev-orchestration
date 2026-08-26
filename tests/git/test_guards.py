from pathlib import Path

import pytest

from dev_orchestration.config.models import DENIED_COMMAND_TOKENS
from dev_orchestration.git import guards

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"

# guards.py raises on push/merge/deploy by design and legitimately contains
# the literals, including the PROHIBITED_VERBS table that repo.py's
# run_git() delegates to at call time; config/models.py holds the
# deny-list that *rejects* these tokens, which is the inverse of invoking
# them. Neither is an invocation. Deliberately NOT exempted: repo.py. It
# was exempted in an earlier revision because it briefly held its own copy
# of the prohibited-verb table, but that made it a blind spot — a real
# subprocess.run(["git", "push", ...]) planted directly in repo.py's body
# (bypassing run_git and its dispatch into guards entirely) was invisible
# to both the scan and the runtime check. The verb table was moved into
# guards.py specifically so repo.py could be dropped from this exemption
# list and be fully covered by the scan below, with no literals left to
# excuse. Exemptions are by relative path, not bare filename, so an
# unrelated future module of the same name is still scanned. Each
# exemption is paired with a narrower, positive test that would fail if
# the file's actual behavior regressed:
#   - guards.py: test_prohibited_operations_raise
#   - config/models.py: test_the_exempted_deny_list_still_contains_all_prohibited_tokens
#     and test_the_exempted_config_module_has_no_subprocess_usage
EXEMPT_RELATIVE_PATHS = {
    Path("dev_orchestration/git/guards.py"),
    Path("dev_orchestration/config/models.py"),
}


def test_the_exemption_list_is_exactly_guards_and_the_deny_list():
    # Pin the exemption list itself. repo.py must never be re-added: if a
    # future change needs an exemption there, that is itself a signal the
    # prohibited-verb logic has leaked back out of guards.py.
    assert EXEMPT_RELATIVE_PATHS == {
        Path("dev_orchestration/git/guards.py"),
        Path("dev_orchestration/config/models.py"),
    }
    assert len(EXEMPT_RELATIVE_PATHS) == 2


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


def test_the_exempted_config_module_has_no_subprocess_usage():
    # The literal-scan exemption for config/models.py only excuses it from
    # the "push"/"merge" token scan because it is a deny-list, not an
    # invocation. That exemption is a whole-file blind spot unless we also
    # prove the file never touches subprocess at all: it is a pure Pydantic
    # module and has no legitimate reason to invoke anything.
    text = (SOURCE_ROOT / "dev_orchestration" / "config" / "models.py").read_text()
    assert "subprocess" not in text


def test_shell_equals_true_appears_nowhere():
    # subprocess.run("git push", shell=True) contains no adjacent-quoted
    # "push"/"merge" literal, so it walks straight through the scan above.
    # shell=True is separately forbidden by the plan's global constraints
    # (every subprocess call uses an argument array); enforce it here, with
    # no exemptions — no file has a legitimate reason to use it.
    offenders = [
        str(path) for path in SOURCE_ROOT.rglob("*.py") if "shell=True" in path.read_text()
    ]
    assert offenders == []


def test_the_sandbox_bypass_flag_appears_nowhere():
    for path in SOURCE_ROOT.rglob("*.py"):
        assert "dangerously-bypass" not in path.read_text()
