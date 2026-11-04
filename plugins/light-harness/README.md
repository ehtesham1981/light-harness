# light-harness

Lightweight gated harness for one-off development tasks in Claude Code: contract-driven `task-init`, explorer-led `task-plan` with human approval, red/green `task-implement` with anti-gaming and anti-drift checks, and an independent `task-review` gate.

Install:

```
/plugin install light-harness --marketplace your-org/light-harness
```

Commands: `/light-harness:task-init`, `/light-harness:task-plan`, `/light-harness:task-implement`, `/light-harness:task-review`.
Subagents: `light-harness:explorer`, `light-harness:test-author`, `light-harness:reviewer`.

Full documentation: see the repository README.
