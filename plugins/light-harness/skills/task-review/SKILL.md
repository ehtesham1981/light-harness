---
name: task-review
description: Stage 4. Run the reviewer subagent against the acceptance criteria and approved plan, log issues with severity, and record a PASS/FAIL verdict. PASS → Done, FAIL → Rework.
disable-model-invocation: true
argument-hint: [task name]
---

You are running **/light-harness:task-review**.

**Which task.** Each task has its own folder, `.tasks/<name>/` (a git worktree on branch `task/<name>`), and this session works on one task. Use the task named in `$ARGUMENTS`, else the task this session started or worked on earlier, else run `python3 scripts/harness.py tasks` and use the only task in progress, or ask the human which one. Below, `<name>` is that task:
- Add `--task <name>` to every `python3 scripts/harness.py` command (they're shown that way below).
- `contract.md`, scope paths and all code and test files are inside `.tasks/<name>/`. Run tests and git commits there, e.g. `cd .tasks/<name> && python3 -m pytest`.
- Tell every subagent the task folder and name in its brief.
- If this session was opened inside a task folder itself, paths are relative to it and `--task` is optional.

1. Run `python3 scripts/harness.py status --task <name>`. Status must be **Build** (or **Review** if a previous review was interrupted). Otherwise stop and name the right command.

2. If status is Build, run `python3 scripts/harness.py set-status Review --task <name>`. This re-runs full verification and refuses to enter Review if it fails. On refusal, report the failures and tell the human to run `/light-harness:task-implement`.

3. **Delegate to the `light-harness:reviewer` subagent** with this brief: "Review the implementation independently against contract.md: re-run verify, check every AC with evidence (§11.1), check business rules, plan conformance, deviations, test quality and gaming. Log issues in §11.2 with severity and re-check any existing ones. Write §11.3. Return your recommended verdict and the blocking issue IDs."
   Don't pass it your own opinion of the work. It must judge from the contract, code, tests and git history.

4. Record the verdict the reviewer recommends:
   - PASS: `python3 scripts/harness.py verdict pass --by reviewer --task <name>`
   - FAIL: `python3 scripts/harness.py verdict fail --by reviewer --task <name>`

   The harness refuses PASS if any Critical/High/Medium issue is unresolved, any AC is unticked, or verification fails. If it refuses, record **FAIL**. Never edit issues to get a pass.

5. Report to the human:
   - **PASS** → status **Done**. Summarise the evidence and any open Low issues. Then offer the close-out:
     - **Finish the task** (default): with the human's OK, run `python3 scripts/harness.py finish-task <name>` from the project's main folder. It tags the work `done/<name>`, then removes `.tasks/<name>/` and the `task/<name>` branch. The tag keeps the code, contract and audit trail, and later tasks can build on it with `start-task <new-name> --from done/<name>`. (If this session was opened inside the task folder itself, it can only tag; the human then runs the same command from the main folder.)
     - **Promote it** into a permanent repo with a backfilled story that links to contract.md and brings the tests along.
   - **FAIL** → status **Rework**. List the blocking issues (ID, severity, one line each). Next: `/light-harness:task-implement` to fix them, then `/light-harness:task-review` again.
