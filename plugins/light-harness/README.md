# light-harness

Lightweight gated harness for development tasks in Claude Code: contract-driven `task-init`, explorer-led `task-plan` with human approval, red/green `task-implement` with anti-gaming and anti-drift checks, and an independent `task-review` gate. It leaves nothing in the work repo: each task runs in its own git worktree, its contract, audit trail and telemetry live inside git, and a finished task is a branch holding only the reviewed work.

Install:

```
/plugin install light-harness --marketplace ehtesham1981/light-harness
```

Commands: `/light-harness:task-init`, `/light-harness:task-plan`, `/light-harness:task-implement`, `/light-harness:task-review`.
Subagents: `light-harness:explorer`, `light-harness:test-author`, `light-harness:reviewer`, `light-harness:sweeper` (sweeps input-space rules in parallel with the reviewer; `parallel` in the task config, default 3, at most 4).
Review speed (1.8):
- `verify` reuses a passing run of the suite and probes for as long as its key (code, locked tests, probes, config, harness version and the run environment) is unchanged, however old it is; it says `cached, key <12 hex>`, and `status` shows it too. `verify --fresh` re-runs.
- Saved probes run in parallel under `probe_timeout`.
- `baseline --out <dir>` (a new or empty folder) gives each sweeper its own base copies.
Parallel tasks share the machine (2.0):
- Suite and probe runs take a machine-wide run slot (`git config --global lightharness.slots N`, default cores ÷ 8) and wait, saying who is ahead, when all are taken. `{concurrency}` in `test_cmd` becomes cores ÷ slots.
- A suite past `suite_timeout` (default 3600 s) is killed with its process tree and fails the gate with exit 124. A killed run's lock or slot is taken over at once on every OS, and its orphaned suite is stopped.
- Each run gets its own empty TEMP folder (`<drive>:\lh-tmp\…` on Windows, `$TMPDIR/lh/…` elsewhere), removed afterwards (one user per machine in this version; see the README).
Verdict reuse (2.1):
- `verdict pass` reuses the stored pass when its key is unchanged and the pass is anchored in the audit trail, and runs fresh otherwise; `verdict pass --fresh` forces a run.
CLI (on PATH in Claude Code sessions): `light-harness`. KPIs: `light-harness metrics`, including each task's time split into agent, human and idle time and its test-suite cost: suite runs, cache hit rate and suite minutes (engineer KPIs are opt-in, under a nickname).

Full documentation: see the repository README.
