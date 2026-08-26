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


def render_project_yaml(config: ProjectConfig) -> str:
    return yaml.safe_dump(config.model_dump(by_alias=True, mode="json"), sort_keys=False)


def render_agents_md(config: ProjectConfig) -> str:
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

The fence governs modification, not reference — repository code may legitimately
read excluded paths.

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

## Open decision

Ownership of planning state between `.ai/runs/` (dev-orchestration) and
`.planning/` (GSD) is unsettled. Treat `.ai/runs/` as the record of what a
dev-orchestration run did, and do not assume either system enforces the other's
boundaries.
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
