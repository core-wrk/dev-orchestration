"""Write the repository contract. Additive only: never moves or edits.

`.ai/context.md` is never regenerated, even with `force=True`. It is the one
file whose content cannot be recovered by reading the repository — it holds
facts discovered during onboarding that are both important and not safely
inferable. `force` only regenerates `AGENTS.md` and `.ai/project.yaml`,
which are rendered wholly from `config` and so lose nothing by being
rewritten.
"""

from pathlib import Path

import yaml

from dev_orchestration.config.models import ProjectConfig


class FileExistsRefusal(FileExistsError):
    """A contract file already exists and force was not requested."""


class UncommittedContractFileError(RuntimeError):
    """--force would overwrite a contract file that has uncommitted edits."""


def _reject_uncommitted_targets(repo_root: Path, names: list[str]) -> None:
    """Refuse to overwrite a contract file carrying uncommitted changes.

    --force exists to re-render a generated file after the template or the
    config changes. It does not exist to discard a user's edits. AGENTS.md in
    particular invites hand-editing -- its own template carries a "Written
    during onboarding" placeholder -- and an overwritten uncommitted edit is
    in no git object and no stash, so it is gone.

    Scoped to the target paths rather than the whole worktree: a user with
    unrelated work in progress should still be able to re-run init.
    """
    from dev_orchestration.git.repo import GitCommandError, GitRepo

    try:
        dirty = {line[3:] for line in GitRepo(repo_root).status_porcelain().splitlines()}
    except GitCommandError:
        # Not a git repository: there is no uncommitted work to protect.
        return
    clashes = sorted(set(names) & dirty)
    if clashes:
        raise UncommittedContractFileError(
            f"{', '.join(clashes)} has uncommitted changes that --force would "
            "overwrite, and they exist in no commit or stash. Commit or stash "
            "them first, or move the file aside."
        )


def render_project_yaml(config: ProjectConfig) -> str:
    return yaml.safe_dump(config.model_dump(by_alias=True, mode="json"), sort_keys=False)


# The one place settled cross-repository questions are recorded. Generated AGENTS.md
# files point here instead of restating decisions that then drift out of date.
DECISIONS_REGISTER = (
    "/Users/andrewodonnell/GitRepos/dev-orchestration-spec/docs/decisions/README.md"
)


def render_agents_md(config: ProjectConfig) -> str:
    decisions_register = DECISIONS_REGISTER
    scope_in = "\n".join(f"- `{p}`" for p in config.scope.include)
    scope_out = "\n".join(f"- `{p}`" for p in config.scope.exclude) or "- (none)"
    commands = (
        "\n".join(f"- `{name}`: `{cmd.command}`" for name, cmd in config.validation.items())
        or "- (none proven yet — an unproven command is not a gate)"
    )
    return f"""# AGENTS.md — {config.project.name}

Persistent agent contract. Carried on every invocation, so everything here earns
its place by being an invariant — something an agent cannot safely infer, whose
violation would cost something.

Generic engineering practice is deliberately absent, as is anything this
repository already demonstrates. Read the repository for structure, conventions,
framework choice and test layout.

## Purpose

_One or two sentences: what this repository is for. Written during onboarding._

## Never allowed autonomously

- push, force push, or remote branch deletion;
- merge;
- deploy or publish;
- production data mutation.

Uncommitted work is never modified, stashed, or worked around.

## Escalate rather than improvise

Stop and escalate on changes crossing a security, privacy, data, payment,
architecture, or approved-scope boundary.

## Scope

Modifiable:

{scope_in}

Never modifiable:

{scope_out}

The fence governs modification, not prompt-source eligibility — repository code
may legitimately read excluded paths. The orchestrator supplies prompt context
only from explicitly declared sources in `.ai/project.yaml`; a scope match does
not authorize a prompt read, and a declared source remains eligible when it is
excluded from modification.

## Facts that cannot be inferred

See `.ai/context.md`. If it is empty, nothing about this repository has yet been
found that the code does not already show.

## Where authority lives

| Question | Source |
|---|---|
| Orchestration policy for this repo | `.ai/project.yaml` |
| Non-inferable repository facts | `.ai/context.md` |
| What the code currently does | the repository |
| Product intent, architecture decisions | `docs/`, retrieved on demand |

Documents under `docs/` are eligible, not automatic. A task must have a reason to
load one.

## Validation

{commands}

A command that deploys, publishes, or writes to a remote can never be registered
as a validation gate; the deny-list is not overridable.

## Planning state ownership

GSD owns intent and planning state. `dev-orch` owns execution: classification,
approved scope, isolated execution, validation, review, verification, run
artifacts, and the commit policy.

This repository carries `.ai/project.yaml`, so GSD phase commands do not run in
it — GSD subagents honour neither the scope fence nor the validation deny-list.
A GSD plan enters only as an external approved plan (`plan_origin: external`).

Decided 2026-09-01 from the `helmfast-OS` pilot. Full record: ADR-002.

## Decisions

Settled questions live in one register, not in this file:

```text
{decisions_register}
```

Read it before assuming a rule here is current. Rules restated above are binding
on their own; the register carries the reasoning, the evidence, and whether a
decision still stands.
"""


def render_context_md(config: ProjectConfig) -> str:
    return f"""# Repository context — {config.project.name}

Facts that are **both important and not safely inferable** from the code. That
conjunction is the whole filter.

Belongs here: a setup step with no trace in the repository; a pitfall that has
already caused a real failure; a generated-code boundary that looks hand-written;
a test-environment constraint invisible from the test files; a validation command
proven to work — or proven not to.

Does not belong here: directory structure, naming conventions, which framework is
in use, how tests are organised. The repository shows all of these.

If a reader could learn it by opening the repository, leave it out.

_Populate during onboarding._
"""


def initialize_repo(repo_root: Path, config: ProjectConfig, force: bool = False) -> list[Path]:
    regenerable = {
        repo_root / "AGENTS.md": render_agents_md(config),
        repo_root / ".ai" / "project.yaml": render_project_yaml(config),
    }
    context_path = repo_root / ".ai" / "context.md"

    if force:
        _reject_uncommitted_targets(
            repo_root, [p.relative_to(repo_root).as_posix() for p in regenerable]
        )

    if not force:
        for path in regenerable:
            if path.exists():
                raise FileExistsRefusal(
                    f"{path} already exists; dev-orch init never overwrites. "
                    "Move it aside or pass --force."
                )
        if context_path.exists():
            raise FileExistsRefusal(
                f"{context_path} already exists; dev-orch init never overwrites. "
                "Move it aside or pass --force."
            )

    written: list[Path] = []
    for path, content in regenerable.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)

    # context.md is written only when it does not exist. --force never
    # regenerates it: it is the one file that carries onboarding knowledge
    # unrecoverable from the repository itself, so it is silently preserved
    # rather than reset to a stub.
    if not context_path.exists():
        context_path.parent.mkdir(parents=True, exist_ok=True)
        context_path.write_text(render_context_md(config), encoding="utf-8")
        written.append(context_path)

    return written
