# AGENTS.md — dev-orchestration

Persistent agent contract. Carried on every invocation, so everything here earns
its place by being an invariant — something an agent cannot safely infer, whose
violation would cost something.

Generic engineering practice is deliberately absent, as is anything this
repository already demonstrates. Read the repository for structure, conventions,
framework choice and test layout.

## Purpose

This repository is a local-first, vendor-neutral control plane for coding-agent
workflows. It standardizes role boundaries, scope, evidence, validation, and
handoffs; providers and model names are replaceable implementations.

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

- `src/`
- `tests/`
- `docs/`
- `README.md`
- `pyproject.toml`

Never modifiable:

- `.ai/`
- `.git/`
- `.venv/`
- `.worktrees/`
- `.superpowers/`

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

- `lint`: `.venv/bin/ruff check --no-cache src tests`
- `format`: `.venv/bin/ruff format --check --no-cache src tests`
- `test`: `.venv/bin/python -m pytest -p no:cacheprovider`

A command that deploys, publishes, or writes to a remote can never be registered
as a validation gate; the deny-list is not overridable.

## Planning state ownership — decided 2026-09-12

A repository carrying `.ai/project.yaml` is governed by dev-orchestration. GSD
phase commands do not run in it. GSD subagents do not honour the scope fence or
the validation deny-list, and the fence protects a dev-orchestration run rather
than the repository, so a GSD executor here would write wherever it liked.

Layers:

- the repository's own in-fence planning directory is the intent layer, authored
  by humans;
- `.ai/runs/` is the evidence layer, framework-owned and outside the fence.

A GSD-authored plan may still be imported as an external approved plan
(`plan_origin: external`). Import is a supported entry point; GSD execution is
not.

Decided from the 2026-09-01 `helmfast-OS` pilot: no observed failure would have
been prevented by a stronger intent layer, and the fence demonstrably fired on
real writes. See §11.1 of the v1 design specification.
