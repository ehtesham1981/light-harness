---
name: task-review
description: Stage 4. Run the reviewer subagent against the acceptance criteria and approved plan, log issues with severity, and record a PASS/FAIL verdict. PASS → Done, FAIL → Rework.
disable-model-invocation: true
argument-hint: [task name]
---

You are running **/light-harness:task-review**.

**Which task.** Each task has its own folder, `.tasks/<name>/` (a git worktree on branch `task/<name>`), and this session works on one task. Use the task named in `$ARGUMENTS`, else the task this session started or worked on earlier, else run `light-harness tasks` and use the only task in progress, or ask the human which one. Below, `<name>` is that task:
- Add `--task <name>` to every `light-harness` command (they're shown that way below). If `light-harness` isn't found, use `sh "${CLAUDE_PLUGIN_ROOT}/bin/light-harness"` in its place, and give subagents that full command too.
- The task's contract is `.tasks/<name>/.light-harness/contract.md` (ignored by git, never committed). Scope paths and all code and test files are inside `.tasks/<name>/`. Run tests and git commits there, e.g. `cd .tasks/<name> && python3 -m pytest`.
- Tell every subagent the task folder, the task name and the contract path in its brief.
- If this session was opened inside a task folder itself, paths are relative to it and `--task` is optional.

1. Run `light-harness status --task <name>`. Status must be **Build** (or **Review** if a previous review was interrupted). Otherwise stop and name the right command.

2. If status is Build, run `light-harness set-status Review --task <name>`. This re-runs full verification and refuses to enter Review if it fails. On refusal, report the failures and tell the human to run `/light-harness:task-implement`.

3. **Delegate to the `light-harness:reviewer` subagent** with this brief: "Review the implementation independently against contract.md: re-run verify, check every AC with evidence (§11.1), check business rules, plan conformance, deviations, test quality and gaming. For every Invariant or Preservation BR, run a sweep of your own against its oracle, record method, sample size and differences in §11.3, and save any sweep that found a defect with `light-harness probe add`. Log issues in §11.2 with severity and a defect Class, and re-check any existing ones over their whole class. Write §11.3. Return your recommended verdict and the blocking issue IDs."
   Don't pass it your own opinion of the work. It must judge from the contract, code, tests and git history.

4. Record the verdict the reviewer recommends:
   - PASS: `light-harness verdict pass --by reviewer --task <name>`
   - FAIL: `light-harness verdict fail --by reviewer --task <name>`

   The harness refuses PASS if any Critical/High/Medium issue is unresolved, any AC is unticked, or verification fails. If it refuses, record **FAIL**. Never edit issues to get a pass.

5. Report to the human:
   - **PASS** → status **Done**. Summarise the evidence and any open Low issues. Then offer the close-out:
     - **Finish the task** (default): with the human's OK, first commit anything still uncommitted in the task folder (`cd .tasks/<name> && git add -A && git commit -m "<summary>"`), so the branch holds exactly the reviewed work. Then run `light-harness finish-task <name>` from the project's main folder (add `--branch <name>` if the human wants the branch named differently, e.g. `feature/<ticket>`). It removes `.tasks/<name>/` and keeps the work on its branch, ready to merge or to open a pull request from; the branch carries no harness files. The contract and audit trail stay available with `light-harness show <name>` and `light-harness log <name>`, and later tasks can build on this one with `start-task <new-name> --from <name>`. (If this session was opened inside the task folder itself, it can only mark the task finished; the human then runs the same command from the main folder.)
     - Merging or pushing the branch is the human's call; don't do it unless they ask.
   - **FAIL** → status **Rework**. List the blocking issues (ID, severity, one line each). Next: `/light-harness:task-implement` to fix them, then `/light-harness:task-review` again.

<!-- Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE). SPDX-License-Identifier: Apache-2.0 -->
