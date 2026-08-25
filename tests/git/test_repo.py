import subprocess

import pytest

from dev_orchestration.git.repo import DirtyWorktreeError, GitRepo, discover_repo


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


def test_discover_repo_walks_up_from_a_subdirectory(repo):
    nested = repo.root / "deep" / "nested"
    nested.mkdir(parents=True)
    assert discover_repo(nested).root == repo.root
