# Light Harness: Working Rules

This repo runs a single one-off task through four gated stages. **`contract.md` is the single source of truth**: requirements, plan, approval, build record, review and verdict all live there.

## Stage flow

| Command | Stage set | Gate to enter the next stage |
|---|---|---|
| `/light-harness:task-init` | Initialize | ≥1 example and ≥1 testable AC recorded |
| `/light-harness:task-plan` | Plan | explorer fills §7–§8; **human approves** with `harness.py approve` |
| `/light-harness:task-implement` | Build | red gate (tests fail on stubs) → implement → `harness.py verify` passes |
| `/light-harness:task-review` | Review → Done / Rework | reviewer verdict; PASS impossible with open Critical/High/Medium issues |

Check where you are at any time with `python3 scripts/harness.py status`.

## Non-negotiable rules

1. **Never edit the `Status:` line or any `harness:` owned block** (approval, redgate, verify, verdict, status-log). Change status only with `python3 scripts/harness.py set-status <Stage>`.
2. **Never approve a plan yourself** and never simulate approval. Approval and test unlocking are human-only commands, run in the human's own terminal.
3. **Never write implementation code before the plan is approved and status is Build.**
4. **After the red gate, tests are read-only.** Make the code satisfy the tests. If you believe a test is wrong, stop, explain why in §10.3, and ask the human to run `harness.py unlock-tests`.
5. **No gaming.** Do not special-case test inputs, detect the test runtime, skip or xfail tests, weaken assertions, swallow exceptions to get green, or edit test configuration. `verify` scans for these patterns, and the reviewer looks for the subtler ones.
6. **Stay in scope.** Only change files listed in §8.1. If you must change anything else, record it in §10.3 (`- path — reason`) before changing it.
7. **Editing §1–§8 after approval invalidates the approval.** If requirements or the plan must change, say so and go back through `/light-harness:task-plan`.
8. Never hardcode secrets; read them from env (`.env` is git-ignored and unreadable to agents). Ask before touching any production system, network endpoint, or real data, unless §1 Constraints explicitly allows it.

## Defaults

- Commands here use `python3`. On Windows, if `python3` isn't available, use `python` or `py -3`. The harness runs tests with the same interpreter that runs it.

- Stack: Python 3.12 + pytest unless the task says otherwise. For a different stack, update `test_cmd`, `test_globs` and `protected_globs` in `.harness/config.json` **during /light-harness:task-plan**, before approval.
- Commit after each plan step with `step N: <summary>`.
- Prefer the simplest implementation that satisfies the contract. No speculative features.

## Subagents

- `light-harness:explorer` — read-only investigation; writes §7–§8 of the contract.
- `light-harness:test-author` — writes failing tests from §2–§4 and §8.4; never writes implementation.
- `light-harness:reviewer` — independent review against ACs and the approved plan; logs issues in §11.
