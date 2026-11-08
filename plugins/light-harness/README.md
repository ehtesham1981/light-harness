# light-harness

Lightweight gated harness for development tasks in Claude Code: contract-driven `task-init`, explorer-led `task-plan` with human approval, red/green `task-implement` with anti-gaming and anti-drift checks, and an independent `task-review` gate. It leaves nothing in the work repo: each task runs in its own git worktree, its contract, audit trail and telemetry live inside git, and a finished task is a branch holding only the reviewed work.

Install:

```
/plugin install light-harness --marketplace ehtesham1981/light-harness
```

Commands: `/light-harness:task-init`, `/light-harness:task-plan`, `/light-harness:task-implement`, `/light-harness:task-review`.
Subagents: `light-harness:explorer`, `light-harness:test-author`, `light-harness:reviewer`.
CLI (on PATH in Claude Code sessions): `light-harness`. KPIs: `light-harness metrics`, including each task's time split into agent, human and idle time (engineer KPIs are opt-in, under a nickname).

Full documentation: see the repository README.
