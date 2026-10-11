import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from dev_orchestration.artifacts.goals import GoalError, GoalLedger
from dev_orchestration.artifacts.store import RunStore
from dev_orchestration.config.models import ProjectConfig
from dev_orchestration.domain.enums import ProjectClass, RunState, Tier
from dev_orchestration.domain.run import RunManifest
from dev_orchestration.git.repo import DirtyWorktreeError, GitRepo
from dev_orchestration.scope import ScopeFence
from dev_orchestration.workflow.bootstrap import (
    ProtectedRuleViolation,
    bootstrap_run,
    prepare_classification_references,
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
    (root / ".gitignore").write_text(".ai/runs/\n")
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
    with pytest.raises(ProtectedRuleViolation):
        resolve_run_policy([{section: {name: True}}])


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
    store, worktree = bootstrap_run(
        repo, CONFIG, "add a flag", Tier.STANDARD, tmp_path / "wt", "test-goal"
    )
    assert worktree.is_dir()
    assert store.read_manifest().resolved_config["protected.autonomous_push"] is False
    assert store.read_manifest().git.branch.startswith("ai/")


def test_bootstrap_blocks_dirty_repo(repo, tmp_path):
    (repo.root / "scratch.txt").write_text("dirty\n")
    with pytest.raises(DirtyWorktreeError):
        bootstrap_run(repo, CONFIG, "x", Tier.STANDARD, tmp_path / "wt", "test-goal")


def test_one_active_run_per_goal_is_shared_across_linked_checkouts(repo, tmp_path):
    linked_root = tmp_path / "linked"
    repo.run_git("worktree", "add", "--quiet", "--detach", str(linked_root), repo.current_commit())
    linked = GitRepo(linked_root)

    start = threading.Barrier(2)

    def launch(checkout, request, worktree_root):
        start.wait()
        try:
            return bootstrap_run(
                checkout, CONFIG, request, Tier.STANDARD, worktree_root, "epic-088"
            )
        except GoalError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda args: launch(*args),
                [
                    (repo, "first attempt", tmp_path / "first-worktrees"),
                    (linked, "duplicate attempt", tmp_path / "linked-worktrees"),
                ],
            )
        )
    admitted = [result for result in results if not isinstance(result, GoalError)]
    refused = [result for result in results if isinstance(result, GoalError)]
    assert len(admitted) == 1
    assert len(refused) == 1
    first, _ = admitted[0]
    assert f"active run {first.run_id}" in str(refused[0])

    other, _ = bootstrap_run(
        linked,
        CONFIG,
        "independent goal",
        Tier.STANDARD,
        tmp_path / "other-worktrees",
        "epic-089",
    )
    assert other.read_manifest().goal_id == "epic-089"


def test_reopening_a_goal_starts_a_new_attempt_generation(repo):
    store, _ = bootstrap_run(
        repo, CONFIG, "first attempt", Tier.STANDARD, repo.root.parent / "worktrees", "epic-088"
    )
    from dev_orchestration.workflow.engine import Engine

    Engine(store).cancel("fixture cleanup")
    from dev_orchestration.workflow.runner import cleanup_worktree_if_unproductive

    cleanup_worktree_if_unproductive(Engine(store), repo)
    ledger = GoalLedger(repo.root)
    assert ledger.reopen("epic-088", "human approved a new implementation direction") == 1
    with ledger.reserve("epic-088") as reservation:
        assert reservation.generation == 1
        assert reservation.attempt == 1


def test_legacy_run_needs_explicit_goal_association_before_it_counts(repo):
    store = RunStore(repo.root, "legacy-run")
    store.initialize(
        RunManifest(
            run_id="legacy-run",
            repository=str(repo.root),
            workflow="standard",
            tier=Tier.STANDARD,
            project_class=ProjectClass.INTERNAL_UTILITY,
            status=RunState.FAILED,
        )
    )
    store.append_event({"event": "run_cleanup_complete", "branch": None})

    ledger = GoalLedger(repo.root)
    ledger.associate("legacy-run", "legacy-goal")

    assert store.read_manifest().goal_id == "legacy-goal"
    with ledger.reserve("legacy-goal") as reservation:
        assert reservation.attempt == 2


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


def test_classification_only_reads_explicit_authorized_markdown(tmp_path):
    (tmp_path / "docs" / "specs").mkdir(parents=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "docs" / "specs" / "feature.md").write_text("feature requirements\n")
    (tmp_path / "src" / "secret.md").write_text("not declared\n")
    config = ProjectConfig.model_validate(
        {
            "project": {"name": "x", "class": "internal_utility"},
            "context": {"persistent": [], "on_demand": {"specifications": "docs/specs"}},
            "scope": {"include": ["src/"], "exclude": []},
        }
    )
    refs, artifact = prepare_classification_references(
        tmp_path,
        config,
        "Implement docs/specs/feature.md and ../../src/secret.md",
        ScopeFence.from_config(config.scope),
    )
    assert "feature requirements" in refs[0].content
    assert refs[1].content.startswith("[rejected:")
    assert artifact["sources"][0]["sha256"]
    assert "not declared" not in "\n".join(item.content for item in refs)


def test_classification_reference_rejects_escaping_symlink(tmp_path):
    (tmp_path / "docs" / "specs").mkdir(parents=True)
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text("secret\n")
    (tmp_path / "docs" / "specs" / "escape.md").symlink_to(outside)
    config = ProjectConfig.model_validate(
        {
            "project": {"name": "x", "class": "internal_utility"},
            "context": {"persistent": [], "on_demand": {"specifications": "docs/specs"}},
            "scope": {"include": ["**"], "exclude": []},
        }
    )
    refs, _ = prepare_classification_references(
        tmp_path,
        config,
        "Implement docs/specs/escape.md",
        ScopeFence.from_config(config.scope),
    )
    assert refs[0].content.startswith("[rejected:")
    assert "secret" not in refs[0].content
    outside.unlink()


def test_classification_reference_excerpt_is_bounded_and_identified(tmp_path):
    (tmp_path / "docs" / "specs").mkdir(parents=True)
    (tmp_path / "docs" / "specs" / "large.md").write_text("é" * 20_000)
    config = ProjectConfig.model_validate(
        {
            "project": {"name": "x", "class": "internal_utility"},
            "context": {"persistent": [], "on_demand": {"specifications": "docs/specs"}},
            "scope": {"include": ["**"], "exclude": []},
        }
    )
    refs, artifact = prepare_classification_references(
        tmp_path,
        config,
        "Implement docs/specs/large.md",
        ScopeFence.from_config(config.scope),
    )
    excerpt = refs[0].content.split("\n", 2)[2].split("\n[truncated", 1)[0]
    assert len(excerpt.encode("utf-8")) <= 16_000
    assert "Original bytes: 40000" in refs[0].content
    assert "SHA-256:" in refs[0].content
    assert "truncated" in refs[0].content
    assert artifact["sources"][0]["original_bytes"] == 40_000
