# Light Harness: Working Rules

Light Harness is running tasks in this repo. Each task runs through four gated stages in its own folder, `.tasks/<name>/` (a git worktree on branch `task/<name>`). **The task's contract, `.tasks/<name>/.light-harness/contract.md`, is the single source of truth**: requirements, plan, approval, build record, review and verdict all live there.

The harness keeps nothing in the work repo. Its contract and config sit in the task folder's `.light-harness/` (ignored by git), and its audit trail lives in git refs (`refs/light-harness/`) that never reach a branch. The task branch holds only the work: your commits of tests and code. Never add harness files, rules or settings to the repo.

`/light-harness:task-init` starts a task (`light-harness start-task <name>`, with `--from <earlier-task>` to build on a finished one), and the same session then works on it. A session works on one task: add `--task <name>` to every `light-harness` command, and edit, test and commit inside `.tasks/<name>/`. Other sessions may be working on other tasks at the same time. While tasks are in progress, the main checkout isn't edited.

## Stage flow

| Command | Stage set | Gate to enter the next stage |
|---|---|---|
| `/light-harness:task-init` | Initialize | ≥1 example and ≥1 testable AC recorded |
| `/light-harness:task-plan` | Plan | explorer fills §7–§8; **human approves** with `light-harness approve` |
| `/light-harness:task-implement` | Build | red gate (tests fail on stubs) → implement → `light-harness verify` passes |
| `/light-harness:task-review` | Review → Done / Rework | reviewer verdict; PASS impossible with open Critical/High/Medium issues |

Check where you are at any time with `light-harness status --task <name>`. If `light-harness` isn't on PATH, run it as `sh "<plugin root>/bin/light-harness"`; the skills give the full path.

## Non-negotiable rules

1. **Never edit the `Status:` line or any `harness:` owned block** (approval, redgate, verify, verdict, status-log). Change status only with `light-harness set-status <Stage>`.
2. **Never approve a plan yourself** and never simulate approval. Approval and test unlocking are human-only commands, run in the human's own terminal.
3. **Never write implementation code before the plan is approved and status is Build.**
4. **After the red gate, tests are read-only.** Make the code satisfy the tests. If you believe a test is wrong, stop, explain why in §10.3, and ask the human to run `light-harness unlock-tests`.
5. **No gaming.** Do not special-case test inputs, detect the test runtime, skip or xfail tests, weaken assertions, swallow exceptions to get green, or edit test configuration. `verify` scans for these patterns, and the reviewer looks for the subtler ones.
6. **Stay in scope.** Only change files listed in §8.1. If you must change anything else, record it in §10.3 (`- path — reason`) before changing it.
7. **Editing §1–§8 after approval invalidates the approval.** If requirements or the plan must change, say so and go back through `/light-harness:task-plan`.
8. **Stay on your task's branch.** Never switch branches, merge, rebase, amend, delete branches or tags, add or remove worktrees, or touch `refs/light-harness`. Tasks are started and finished only with `light-harness start-task` / `finish-task`.
9. Never hardcode secrets; read them from env (`.env` files are unreadable to agents during a task). Ask before touching any production system, network endpoint, or real data, unless §1 Constraints explicitly allows it.

## Defaults

- The harness runs Python 3.9+ (`python3`, `python` or `py -3`) and runs Python tests with the same interpreter.
- Stack: Python + pytest unless the task says otherwise. For a different stack, update `test_cmd`, `test_globs` and `protected_globs` in `.tasks/<name>/.light-harness/config.json` **during /light-harness:task-plan**, before approval. The next task in this repo starts from the last finished task's config.
- Commit after each plan step with `step N: <summary>`. Before `finish-task`, the branch must hold exactly the reviewed work, all committed.
- Tests that passed before the task started (from an earlier task or existing code) must keep passing; only the task's new tests must fail at the red gate. AC/BR/EX numbering starts at 1 in every task.
- Prefer the simplest implementation that satisfies the contract. No speculative features.

## Subagents

- `light-harness:explorer` — read-only investigation; writes §6–§8 of the contract.
- `light-harness:test-author` — writes failing tests from §2–§4 and §8.4; never writes implementation.
- `light-harness:reviewer` — independent review against ACs and the approved plan; logs issues in §11.

<!-- Copyright 2026 Algomade Consulting. Part of Light Harness (see NOTICE). SPDX-License-Identifier: Apache-2.0 -->
