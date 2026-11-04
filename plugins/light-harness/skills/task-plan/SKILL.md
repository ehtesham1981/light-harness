---
name: task-plan
description: Stage 2. Run the explorer subagent to fill in findings, risks, impact analysis and the implementation plan, set status to Plan, then stop for human approval.
disable-model-invocation: true
argument-hint: [task name]
---

You are running **/light-harness:task-plan**.

**Which task.** Each task has its own folder, `.tasks/<name>/` (a git worktree on branch `task/<name>`), and this session works on one task. Use the task named in `$ARGUMENTS`, else the task this session started or worked on earlier, else run `python3 scripts/harness.py tasks` and use the only task in progress, or ask the human which one. Below, `<name>` is that task:
- Add `--task <name>` to every `python3 scripts/harness.py` command (they're shown that way below).
- `contract.md`, scope paths and all code and test files are inside `.tasks/<name>/`. Run tests and git commits there, e.g. `cd .tasks/<name> && python3 -m pytest`.
- Tell every subagent the task folder and name in its brief.
- If this session was opened inside a task folder itself, paths are relative to it and `--task` is optional.

1. Run `python3 scripts/harness.py status --task <name>`. Status must be **Initialize**, or **Plan** (re-plan), or **Build/Rework** (re-plan after a requirement or plan change; warn the human that the current approval and red gate will be invalidated). Otherwise stop and say which command to run.

2. **Delegate to the `light-harness:explorer` subagent.** Give it this brief: "Read contract.md, explore the environment, and fill §6 (open questions), §7 (findings, risks, impact analysis) and §8 (files in scope, interfaces, steps, test strategy) following your instructions. Run `python3 scripts/harness.py check plan --task <name>` before returning."

3. When it returns, read the contract yourself and run `python3 scripts/harness.py check plan --task <name>`. If there are gaps, send the explorer back with the specific gaps. Don't fill them yourself.

4. **Open questions.** If §6 has questions that need a human, ask them now, one at a time. Record the answers in §6. If an answer changes rules, examples or ACs, update §2–§4 (with the human's confirmation) and have the explorer revise the plan.

5. Run `python3 scripts/harness.py set-status Plan --task <name>`.

6. **Present the plan for approval.** Include: scope (files), interfaces, numbered steps with ACs, top risks and mitigations, impact and rollback, the test strategy per AC, and any config change needed for a non-Python stack. Then tell the human:

   > To approve, run this **in your own terminal** (PowerShell or Windows Terminal on Windows) in the project's main folder (it's interactive and records your name, the time and the plan hash in the task's contract.md and git):
   > `python3 scripts/harness.py approve --name "Your Full Name" --task <name>`
   > Ask me for changes instead if anything should be different. Any later edit to §1–§8 invalidates the approval.

7. **Stop here.** Don't run the approve command, don't simulate approval, and don't start implementing. When the human says they've approved, confirm with `python3 scripts/harness.py status --task <name>` and point them to `/light-harness:task-implement`.
