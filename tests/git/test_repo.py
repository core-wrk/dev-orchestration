import subprocess

import pytest

from dev_orchestration.git.guards import ProhibitedOperationError
from dev_orchestration.git.repo import (
    DirtyWorktreeError,
    EmptySnapshotError,
    GitRepo,
    discover_repo,
)


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], check=True)
    (tmp_path / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"], check=True)
    return GitRepo(tmp_path)


def test_clean_repo_passes_the_guard(repo):
    repo.ensure_clean()


def test_dirty_repo_raises_and_names_the_files(repo):
    (repo.root / "dirty.txt").write_text("uncommitted\n")
    with pytest.raises(DirtyWorktreeError) as excinfo:
        repo.ensure_clean()
    assert "dirty.txt" in str(excinfo.value)


def test_unstaged_dotfile_modification_keeps_its_leading_character(repo):
    # `git status --porcelain` for an unstaged modification is
    # " M path" -- a leading space in the status field. A naive
    # `.strip()` of the whole porcelain block eats that space off the
    # *first* line only, truncating the first character of a dotfile
    # path (".env.example" -> "env.example"). This is the real-world
    # regression case: a dotfile is the most likely first entry.
    (repo.root / ".env.example").write_text("KEY=1\n")
    subprocess.run(["git", "-C", str(repo.root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo.root), "commit", "-q", "-m", "add env"], check=True)
    (repo.root / ".env.example").write_text("KEY=2\n")
    with pytest.raises(DirtyWorktreeError) as excinfo:
        repo.ensure_clean()
    message = str(excinfo.value)
    assert ".env.example" in message
    assert "env.example" not in message.replace(".env.example", "")


def test_multiple_dirty_files_all_keep_their_full_paths_when_first_is_unstaged(repo):
    # The truncation bug only ever touched the first porcelain line, so a
    # single-file test cannot tell us the fix covers the general case.
    (repo.root / ".env.example").write_text("KEY=1\n")
    subprocess.run(["git", "-C", str(repo.root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo.root), "commit", "-q", "-m", "add env"], check=True)
    (repo.root / ".env.example").write_text("KEY=2\n")
    (repo.root / "second.txt").write_text("second\n")
    with pytest.raises(DirtyWorktreeError) as excinfo:
        repo.ensure_clean()
    message = str(excinfo.value)
    assert ".env.example" in message
    assert "second.txt" in message
    assert "env.example" not in message.replace(".env.example", "")


def test_status_porcelain_reports_staged_unstaged_and_untracked_paths_exactly(repo):
    (repo.root / "staged.txt").write_text("1\n")
    subprocess.run(
        ["git", "-C", str(repo.root), "add", "staged.txt"], check=True, capture_output=True
    )
    (repo.root / "README.md").write_text("changed\n")
    (repo.root / "new.txt").write_text("new\n")

    lines = repo.status_porcelain().splitlines()
    paths = [line[3:] for line in lines]

    assert "staged.txt" in paths
    assert "README.md" in paths
    assert "new.txt" in paths


def test_current_commit_is_a_full_sha(repo):
    assert len(repo.current_commit()) == 40


def test_create_branch_switches_to_it(repo):
    repo.create_branch("ai/20260825-013500_feature_x")
    assert repo.current_branch() == "ai/20260825-013500_feature_x"


def test_commit_records_only_named_paths(repo):
    (repo.root / "a.txt").write_text("a\n")
    (repo.root / "b.txt").write_text("b\n")
    sha = repo.commit("ai(run): add a", ["a.txt"])
    assert len(sha) == 40
    assert "b.txt" in repo.status_porcelain()


def test_commit_return_value_is_the_actual_new_head(repo):
    base_sha = subprocess.run(
        ["git", "-C", str(repo.root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repo.root / "a.txt").write_text("a\n")
    sha = repo.commit("ai(run): add a", ["a.txt"])
    expected_sha = subprocess.run(
        ["git", "-C", str(repo.root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert sha == expected_sha
    assert sha != base_sha


def test_changed_files_lists_paths_since_base(repo):
    base = repo.current_commit()
    (repo.root / "c.txt").write_text("c\n")
    repo.commit("ai(run): add c", ["c.txt"])
    assert repo.changed_files(base) == ["c.txt"]


def test_change_inventory_covers_renames_and_worktree_states(repo):
    base = repo.current_commit()
    subprocess.run(
        ["git", "-C", str(repo.root), "mv", "README.md", "renamed.md"],
        check=True,
        capture_output=True,
    )
    (repo.root / "renamed.md").write_text("changed\n")
    (repo.root / "staged.txt").write_text("staged\n")
    subprocess.run(["git", "-C", str(repo.root), "add", "staged.txt"], check=True)
    (repo.root / "untracked.txt").write_text("untracked\n")
    inventory = repo.change_inventory(base)
    assert {
        "README.md",
        "renamed.md",
        "staged.txt",
        "untracked.txt",
    }.issubset(inventory)


def test_ignored_paths_names_individual_files_inside_an_ignored_directory(repo):
    (repo.root / ".gitignore").write_text("dist/\n")
    (repo.root / "dist" / "sub").mkdir(parents=True)
    (repo.root / "dist" / "payload.sh").write_text("x\n")
    (repo.root / "dist" / "sub" / "deep.txt").write_text("y\n")
    assert repo.ignored_paths() == ["dist/payload.sh", "dist/sub/deep.txt"]


def test_ignored_paths_excludes_tracked_and_plain_untracked_files(repo):
    (repo.root / ".gitignore").write_text("*.log\n")
    (repo.root / "app.log").write_text("noise\n")
    (repo.root / "visible.txt").write_text("seen\n")
    assert repo.ignored_paths() == ["app.log"]


def test_change_inventory_still_ignores_ignored_files_without_a_stage_snapshot(repo):
    base = repo.current_commit()
    (repo.root / ".gitignore").write_text("dist/\n")
    (repo.root / "dist").mkdir()
    (repo.root / "dist" / "payload.sh").write_text("x\n")
    assert "dist/payload.sh" not in repo.change_inventory(base)


def test_ignored_delta_detects_overwrite_delete_and_type_change(repo):
    (repo.root / ".gitignore").write_text("dist/\n")
    (repo.root / "dist").mkdir()
    payload = repo.root / "dist" / "payload.sh"
    payload.write_text("before\n")
    before = repo.ignored_paths()
    payload.write_text("after\n")
    assert repo.ignored_delta(before) == ["dist/payload.sh"]
    payload.unlink()
    assert repo.ignored_delta(before) == ["dist/payload.sh"]
    payload.mkdir()
    (payload / "nested").write_text("new\n")
    assert repo.ignored_delta(before) == ["dist/payload.sh", "dist/payload.sh/nested"]


def test_commit_snapshot_refuses_an_empty_change_set(repo):
    with pytest.raises(EmptySnapshotError):
        repo.commit_snapshot("ai(run): nothing happened", [])


def test_commit_snapshot_records_only_the_reviewed_paths(repo):
    base = repo.current_commit()
    (repo.root / "reviewed.txt").write_text("in the review\n")
    (repo.root / "unreviewed.txt").write_text("not in the review\n")
    sha = repo.commit_snapshot("ai(run): complete", ["reviewed.txt"])
    committed = repo.run_git("diff", "--name-only", base, sha).splitlines()
    assert committed == ["reviewed.txt"]
    assert "unreviewed.txt" in repo.status_paths()


def test_commit_snapshot_records_a_deletion(repo):
    base = repo.current_commit()
    (repo.root / "README.md").unlink()
    sha = repo.commit_snapshot("ai(run): remove", ["README.md"])
    assert repo.run_git("diff", "--name-status", base, sha) == "D\tREADME.md"


def test_discover_repo_walks_up_from_a_subdirectory(repo):
    nested = repo.root / "deep" / "nested"
    nested.mkdir(parents=True)
    assert discover_repo(nested).root == repo.root


def test_run_git_rejects_push(repo):
    with pytest.raises(ProhibitedOperationError):
        repo.run_git("push", "origin", "main")


def test_run_git_rejects_merge(repo):
    with pytest.raises(ProhibitedOperationError):
        repo.run_git("merge", "other")


def test_run_git_rejects_a_dynamically_built_push_verb(repo):
    # A literal-string source scan cannot see this verb being assembled at
    # runtime; the check inside run_git compares the actual string value,
    # so it catches this even though no source file contains "push".
    verb = "pu" + "sh"
    with pytest.raises(ProhibitedOperationError):
        repo.run_git(verb)


def test_run_git_still_permits_ordinary_operations(repo):
    assert repo.run_git("status", "--porcelain") == ""
    assert len(repo.run_git("rev-parse", "HEAD")) == 40


def test_run_git_rejects_a_leading_global_option_hiding_a_push(repo):
    # git accepts global options before the subcommand, so a check that looks
    # only at args[0] sees "-c", finds no prohibited name, and lets the push
    # through. This reached subprocess before the allow-list was added.
    with pytest.raises(ProhibitedOperationError):
        repo.run_git("-c", "credential.helper=x", "push", "origin", "main")


def test_run_git_rejects_pull_because_it_merges(repo):
    with pytest.raises(ProhibitedOperationError):
        repo.run_git("pull", "--rebase")


@pytest.mark.parametrize("verb", ["reset", "clean", "stash", "fetch", "remote", "rebase"])
def test_run_git_rejects_verbs_the_framework_never_uses(repo, verb):
    # Not an exhaustive deny-list — the point is that anything outside the
    # allow-list is refused, so verbs nobody thought to name are still caught.
    with pytest.raises(ProhibitedOperationError):
        repo.run_git(verb)


def test_run_git_rejects_checkout_that_would_discard_uncommitted_work(repo):
    (repo.root / "README.md").write_text("edited but not committed\n")
    with pytest.raises(ProhibitedOperationError):
        repo.run_git("checkout", "--", ".")
    assert (repo.root / "README.md").read_text() == "edited but not committed\n"


def test_run_git_still_creates_branches_with_checkout(repo):
    repo.run_git("checkout", "-q", "-b", "ai/example")
    assert repo.current_branch() == "ai/example"
