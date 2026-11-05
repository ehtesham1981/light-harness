---
name: task-implement
description: Stage 3. Run the test-author subagent, pass the red gate, implement the approved plan until green, then run verification (red-gate integrity, anti-gaming, anti-drift). Status becomes Build.
disable-model-invocation: true
argument-hint: [task name]
---

You are running **/light-harness:task-implement** as the implementer.

**Which task.** Each task has its own folder, `.tasks/<name>/` (a git worktree on branch `task/<name>`), and this session works on one task. Use the task named in `$ARGUMENTS`, else the task this session started or worked on earlier, else run `light-harness tasks` and use the only task in progress, or ask the human which one. Below, `<name>` is that task:
- Add `--task <name>` to every `light-harness` command (they're shown that way below). If `light-harness` isn't found, use `sh "${CLAUDE_PLUGIN_ROOT}/bin/light-harness"` in its place, and give subagents that full command too.
- The task's contract is `.tasks/<name>/.light-harness/contract.md` (ignored by git, never committed). Scope paths and all code and test files are inside `.tasks/<name>/`. Run tests and git commits there, e.g. `cd .tasks/<name> && python3 -m pytest`.
- Tell every subagent the task folder, the task name and the contract path in its brief.
- If this session was opened inside a task folder itself, paths are relative to it and `--task` is optional.

### 0. Preconditions
Run `light-harness status --task <name>`.
- Status **Plan** with a valid approval, or **Rework**: run `light-harness set-status Build --task <name>`.
- Status **Build**: you're resuming. Continue from the first incomplete step below.
- Anything else, or approval missing or invalid: stop and tell the human what's needed. Never approve on their behalf.

If resuming from **Rework**, read every unresolved issue in §11.2 first. Those issues are now your work list, in addition to the plan. You may set an issue's status to **Fixed**, but never to Resolved.

### 1. Tests first (skip if the red gate is already ✓)
Delegate to the **`light-harness:test-author`** subagent: "Write the acceptance tests from contract.md §2–§4 and the §8.4 test strategy, targeting the §8.2 interfaces. Report the AC → tests mapping."
Review what it wrote: every AC covered, examples used verbatim, real assertions.

### 2. Stubs
Create the minimal stubs from §8.2 (correct signatures, bodies raising `NotImplementedError`) in files within §8.1 scope, so tests import and fail on behaviour, not on collection errors. Write no logic yet.

### 3. Red gate
Run `light-harness redgate --task <name>`. It checks that every AC is referenced by a test added or changed in this task, that the suite fails against the stubs, and that none of this task's new tests passes vacuously. Tests that already passed before the task started (from the task it builds on, or existing code) are expected to pass and must keep passing. It then locks the tests: it records a snapshot of them in the task's audit trail (nothing is committed to the branch).
- If tests error on import, fix the **stubs**.
- If a new test passes against the stubs, send it back to the test-author.
- If the red gate warns that an existing test fails against the stubs, the stubs replaced working code. Keep that behaviour, because `verify` requires every existing test to pass again.
- Use `--allow-passing "<reason>"` only for a new test that legitimately passes on stubs (e.g. "input validation already handled by the framework"), and tell the human you did.

### 4. Implement the plan
Work through §8.3 step by step:
- Implement only what the step needs, within §8.1 scope. If you must touch another file, first add `- path — reason` to §10.3 Deviations.
- Run the tests after each step. Commit each step in the task folder: `cd .tasks/<name> && git add -A && git commit -m "step N: <summary>"`.
- **Tests are locked.** If you believe a test is wrong, stop, explain in §10.3, and ask the human to run `python "${CLAUDE_PLUGIN_ROOT}/scripts/harness.py" unlock-tests --task <name> --name "Full Name" --reason "..."` in their own terminal, in the project's main folder. Never work around a test.
- No special-casing of example values, no test-runtime detection, no skips, no swallowed exceptions to force green.

### 5. Verify
Run `light-harness verify --task <name>`. This checks:
- **Red-gate integrity**: locked tests and protected files are byte-identical; every red-gate test now runs and passes.
- **Green**: the full suite passes.
- **Anti-gaming**: no runtime sniffing, skips, or focus markers since approval; warns on broad exception handlers, coverage exclusions, mocks in implementation code, and comparisons against test literals.
- **Anti-drift**: plan unchanged since approval; every changed file is in §8.1 scope or declared in §10.3.
- **Traceability**: every AC still has a test.

Fix every ✗ and re-run until it passes. For each ⚠, either fix it or explain it in §10.2 so the reviewer can judge it.

### 6. Record and hand off
Fill §10.2 Implementation Notes: what each step delivered, how to run the result, known limitations, and the explanation for any ⚠. Commit any remaining work on the task branch (the contract isn't part of the branch). Then report to the human: steps completed, test count, verification result, deviations, and "Next: `/light-harness:task-review`".

Status stays **Build**. `/light-harness:task-review` moves it to Review.
