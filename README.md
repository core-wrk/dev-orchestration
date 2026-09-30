
## Releasing

Other repos and the scheduler run a frozen copy of `dev-orch`, so editing or switching
branches in this checkout never disturbs work in progress.

- `dev-orch` is the frozen copy. It changes only when you run `dev-orch-dev promote`,
  which installs the latest commit (uncommitted edits are ignored) and switches to it.
- `dev-orch-dev` runs this checkout's live code. Use it to try changes.
- `dev-orch --version` shows which commit the frozen copy came from.
