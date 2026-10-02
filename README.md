
## Releasing

Other repos and the scheduler run a frozen copy of `dev-orch`, so editing or switching
branches in this checkout never disturbs work in progress.

- `dev-orch` is the frozen copy. It changes only when you run `dev-orch-dev promote`,
  which installs the latest commit (uncommitted edits are ignored) and switches to it.
- `dev-orch-dev` runs this checkout's live code. Use it to try changes.
- `dev-orch --version` shows which commit the frozen copy came from.

## Recovering an implementation

`dev-orch restart RUN_ID --reason "Describe the correction"` starts a linked
attempt from a failed or escalated run, carries its existing implementation into
a new isolated worktree, and runs validation, implementation review, and final
verification without repeating planning or asking an agent to edit the code.
Use `--patch PATH` to supply a complete replacement patch against the source
run's base.

`--from review` or `--from verification` skips earlier checks only when saved
evidence proves those checks passed against the identical diff. Otherwise the
command explains why and recommends `--from validation`. `--from remediation`
runs validation and review first, then allows the existing bounded remediation
loop to address actionable findings. `dev-orch status RUN_ID` reports eligible
restart stages for failed and escalated runs.
