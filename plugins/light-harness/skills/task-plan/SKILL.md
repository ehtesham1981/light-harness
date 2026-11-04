---
name: task-plan
description: Stage 2. Run the explorer subagent to fill in findings, risks, impact analysis and the implementation plan, set status to Plan, then stop for human approval.
disable-model-invocation: true
---

You are running **/light-harness:task-plan**.

1. Run `python3 scripts/harness.py status`. Status must be **Initialize**, or **Plan** (re-plan), or **Build/Rework** (re-plan after a requirement or plan change; warn the human that the current approval and red gate will be invalidated). Otherwise stop and say which command to run.

2. **Delegate to the `light-harness:explorer` subagent.** Give it this brief: "Read contract.md, explore the environment, and fill §6 (open questions), §7 (findings, risks, impact analysis) and §8 (files in scope, interfaces, steps, test strategy) following your instructions. Run `python3 scripts/harness.py check plan` before returning."

3. When it returns, read the contract yourself and run `python3 scripts/harness.py check plan`. If there are gaps, send the explorer back with the specific gaps. Don't fill them yourself.

4. **Open questions.** If §6 has questions that need a human, ask them now, one at a time. Record the answers in §6. If an answer changes rules, examples or ACs, update §2–§4 (with the human's confirmation) and have the explorer revise the plan.

5. Run `python3 scripts/harness.py set-status Plan`.

6. **Present the plan for approval.** Include: scope (files), interfaces, numbered steps with ACs, top risks and mitigations, impact and rollback, the test strategy per AC, and any config change needed for a non-Python stack. Then tell the human:

   > To approve, run this **in your own terminal** at the repo root (it's interactive and records your name, the time and the plan hash in contract.md and git):
   > `python3 scripts/harness.py approve --name "Your Full Name"`
   > Ask me for changes instead if anything should be different. Any later edit to §1–§8 invalidates the approval.

7. **Stop here.** Don't run the approve command, don't simulate approval, and don't start implementing. When the human says they've approved, confirm with `python3 scripts/harness.py status` and point them to `/light-harness:task-implement`.
