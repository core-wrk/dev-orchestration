# Repository context — dev-orchestration

Facts that are **both important and not safely inferable** from the code. That
conjunction is the whole filter.

Belongs here: a setup step with no trace in the repository; a pitfall that has
already caused a real failure; a generated-code boundary that looks hand-written;
a test-environment constraint invisible from the test files; a validation command
proven to work — or proven not to.

Does not belong here: directory structure, naming conventions, which framework is
in use, how tests are organised. The repository shows all of these.

If a reader could learn it by opening the repository, leave it out.

## Verified environment facts

- On the verified macOS build, Codex reports a stable Goal Mode feature but has
  no headless Goal entry point. The V1 execution path is therefore `codex exec`.
- The independent verifier is bound to Claude because the Codex adapter cannot
  enforce the read-only review capability.
- Git worktrees do not copy ignored `.venv` directories. The framework exposes
  the base checkout's environment only to its validation subprocess and removes
  the temporary link afterward.
- Validation commands in `.ai/project.yaml` were proven on 2026-09-02. The
  test command disables pytest's cache provider so validation does not create
  ignored cache residue in an isolated worktree.

## Self-hosting boundary

The framework's own `.ai/project.yaml` is the source of orchestration policy for
this repository. `.ai/runs/` is framework-owned evidence and is excluded from
agent modification scope; agents may change only the paths listed by the scope
fence. Self-hosting bootstrap files were created before the first self-hosted
run and must be committed before a run begins.
