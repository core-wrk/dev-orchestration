import subprocess

import pytest

from dev_orchestration.config.models import ProjectConfig
from dev_orchestration.domain.enums import Tier
from dev_orchestration.git.repo import DirtyWorktreeError, GitRepo
from dev_orchestration.scope import ScopeFence
from dev_orchestration.workflow.bootstrap import (
    bootstrap_run,
    resolve_run_policy,
    resolve_runtime_context,
)

CONFIG = ProjectConfig.model_validate(
    {
        "project": {"name": "demo", "class": "internal_utility"},
        "scope": {"include": ["src/"], "exclude": []},
        "context": {"persistent": []},
    }
)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "T"], check=True)
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("x\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)
    return GitRepo(root)


@pytest.mark.parametrize(
    "key",
    [
        "protected.autonomous_push",
        "protected.autonomous_merge",
        "protected.autonomous_deploy",
        "protected.autonomous_production_data_mutation",
    ],
)
def test_each_protected_rule_resists_override(key):
    section, name = key.split(".")
    assert resolve_run_policy([{section: {name: True}}])[key] is False


def test_strictness_never_relaxes():
    assert (
        resolve_run_policy(
            [
                {"review": {"implementation_review_required": True}},
                {"review": {"implementation_review_required": False}},
            ]
        )["review.implementation_review_required"]
        is True
    )


def test_bootstrap_records_policy_and_creates_isolated_branch(repo, tmp_path):
    store, worktree = bootstrap_run(repo, CONFIG, "add a flag", Tier.STANDARD, tmp_path / "wt")
    assert worktree.is_dir()
    assert store.read_manifest().resolved_config["protected.autonomous_push"] is False
    assert store.read_manifest().git.branch.startswith("ai/")


def test_bootstrap_blocks_dirty_repo(repo, tmp_path):
    (repo.root / "scratch.txt").write_text("dirty\n")
    with pytest.raises(DirtyWorktreeError):
        bootstrap_run(repo, CONFIG, "x", Tier.STANDARD, tmp_path / "wt")


def test_runtime_profile_minimum_and_context_are_joined(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "AGENTS.md").write_text("never push\n")
    config = ProjectConfig.model_validate(
        {
            "project": {"name": "x", "class": "internal_utility"},
            "profiles": {
                "available": ["auth"],
                "definitions": {
                    "auth": {
                        "minimum_tier": "standard",
                        "controls": {"security_review": "required"},
                    }
                },
            },
            "context": {"persistent": ["AGENTS.md"]},
            "scope": {"include": ["**"], "exclude": []},
        }
    )
    runtime = resolve_runtime_context(root, config, ["auth"], ScopeFence.from_config(config.scope))
    assert runtime.minimum_tier is Tier.STANDARD
    assert runtime.invariants[0].label == "invariants"
    assert runtime.profile_constraints[0].label == "profile_constraints"
