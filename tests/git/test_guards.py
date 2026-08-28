import ast
from pathlib import Path, PurePosixPath

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
    banned = (
        '"push"',
        "'push'",
        '"merge"',
        "'merge'",
        "force-with-lease",
        '"wrangler"',
        "'wrangler'",
    )
    offenders = []
    for path in SOURCE_ROOT.rglob("*.py"):
        if path.relative_to(SOURCE_ROOT) in EXEMPT_RELATIVE_PATHS:
            continue
        text = path.read_text()
        offenders += [f"{path}: {token}" for token in banned if token in text]
    assert offenders == []


# Binaries that reach a remote or a deployment target. None of them go through
# GitRepo.run_git, so its runtime allow-list does not cover them: for these,
# this scan is the only control that exists.
FORBIDDEN_ARGV_BINARIES = frozenset({"gh", "npx", "wrangler", "curl", "ssh", "scp"})

# Verbs that must never appear as a literal element of a subprocess argv,
# whatever the binary. "deploy" and "publish" cannot be scanned for as bare
# substrings -- init_repo.py's AGENTS.md template states the prohibition in
# prose, and resolver.py has a protected.autonomous_deploy key -- so they are
# matched here as argv elements, where a mention cannot be confused with a call.
FORBIDDEN_ARGV_TOKENS = frozenset({"push", "merge", "pull", "deploy", "publish"})


def _literal_subprocess_argvs(tree):
    """Yield (lineno, [literal argv strings]) for each subprocess call.

    Only literal list/tuple elements are returned; a starred or computed
    element is skipped rather than guessed at. That is the documented limit
    of this scan -- see test_the_argv_scan_states_what_it_cannot_catch.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
            continue
        if func.value.id != "subprocess":
            continue
        if not node.args or not isinstance(node.args[0], ast.List | ast.Tuple):
            continue
        elements = [
            e.value
            for e in node.args[0].elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
        yield node.lineno, elements


def test_no_module_shells_out_to_a_remote_or_deploy_binary():
    # A plain quoted-literal scan cannot see this: the reviewer planted
    # subprocess.run(["npx","wrangler","deploy","--env","production"]) in
    # cli.py and the suite stayed green at 180 passed, then planted a sentinel
    # shelling to /bin/sh -- a green run executed it and nothing noticed.
    offenders = []
    for path in SOURCE_ROOT.rglob("*.py"):
        if path.relative_to(SOURCE_ROOT) in EXEMPT_RELATIVE_PATHS:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for lineno, argv in _literal_subprocess_argvs(tree):
            if not argv:
                continue
            binary = PurePosixPath(argv[0]).name
            if binary in FORBIDDEN_ARGV_BINARIES:
                offenders.append(f"{path}:{lineno}: binary {binary!r}")
            offenders += [
                f"{path}:{lineno}: argv token {tok!r}"
                for tok in argv
                if tok in FORBIDDEN_ARGV_TOKENS
            ]
    assert offenders == []


def test_the_argv_scan_detects_a_planted_deploy(tmp_path):
    # Proves the scan above can actually fail. Without this, a scan that
    # silently matched nothing would look identical to a clean tree.
    planted = tmp_path / "planted.py"
    planted.write_text(
        'import subprocess\nsubprocess.run(["npx", "wrangler", "deploy", "--env", "production"])\n'
    )
    tree = ast.parse(planted.read_text(), filename=str(planted))
    found = list(_literal_subprocess_argvs(tree))
    assert found, "the scan did not see a literal subprocess argv at all"
    _, argv = found[0]
    assert PurePosixPath(argv[0]).name in FORBIDDEN_ARGV_BINARIES
    assert any(tok in FORBIDDEN_ARGV_TOKENS for tok in argv)


def test_the_argv_scan_states_what_it_cannot_catch():
    # The limit is real and must stay documented rather than implied: a verb
    # assembled at runtime has no literal to match. The runtime allow-list in
    # GitRepo.run_git is what covers the git path; for gh/npx/wrangler/curl
    # there is no runtime check, so a fully dynamic invocation of those is
    # genuinely uncaught by anything.
    source = 'import subprocess\nv = "dep" + "loy"\nsubprocess.run(["npx", v])\n'
    tree = ast.parse(source)
    ((_, argv),) = _literal_subprocess_argvs(tree)
    assert argv == ["npx"]
    assert not any(tok in FORBIDDEN_ARGV_TOKENS for tok in argv)
    assert _literal_subprocess_argvs.__doc__ is not None


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
