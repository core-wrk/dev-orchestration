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

Uncommitted work outside a dev-orch run's own isolated worktree is never modified,
stashed, or worked around. A resumed run may continue editing its recorded worktree
only when the branch, scope, and saved change snapshot still match. It never
discards partial work or adopts unrelated edits automatically.

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
/Users/andrewodonnell/GitRepos/dev-orchestration-spec/docs/decisions/README.md
```

Read it before assuming a rule here is current. Rules restated above are binding
on their own; the register carries the reasoning, the evidence, and whether a
decision still stands.
