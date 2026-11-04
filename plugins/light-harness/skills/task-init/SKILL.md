---
name: task-init
description: Stage 1. Start a task in its own folder (.tasks/<name>, a git worktree), then interview the human in the same session to capture the task, business rules, examples and acceptance criteria in its contract.md, and set status to Initialize.
disable-model-invocation: true
argument-hint: [short task name]
---

You are running **/light-harness:task-init**. Task name hint: `$ARGUMENTS`

0. **Install or update the harness in this checkout** (safe to repeat). Run from the repo root:
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/install.py" "${CLAUDE_PLUGIN_ROOT}" "$(pwd)"`
   (use `python` or `py -3` if `python3` isn't available). It copies `contract.md`, `scripts/harness.py` and `.harness/config.json`, appends the harness rules to `CLAUDE.md`, and merges the harness permission rules into `.claude/settings.json`. If the plugin is newer, it also updates `scripts/harness.py`. If it refuses because this folder holds task worktrees, stop and relay its message: the human opened the folder that contains the tasks instead of a task folder.

1. Run `python3 scripts/harness.py status` and read the **Task:** line. (Don't run `init --name`: the main checkout's contract.md must stay a blank template for future tasks.)

   **Main checkout** (`Task: main checkout`) with status **Draft**: start the task in its own folder, then carry straight on in this session.
   - If `$ARGUMENTS` names a task that's already in progress (`python3 scripts/harness.py tasks`), use it and skip to the status check below.
   - Otherwise ask for a short task name if `$ARGUMENTS` is empty, and ask whether this task **builds on a finished task** (listed by `python3 scripts/harness.py tasks`) or is **independent**.
   - Run `python3 scripts/harness.py start-task "<name>"`, adding `--from done/<earlier-task>` if it builds on one. It commits the harness files on this branch if needed, then creates `.tasks/<name>/` on branch `task/<name>`. `<name>` from here on is the name it prints (lowercased, hyphenated).
   - From now on, **this session's task is `<name>`**: add `--task <name>` to every `python3 scripts/harness.py` command, and edit `.tasks/<name>/contract.md`, never the main checkout's `contract.md`.
   - Run `python3 scripts/harness.py status --task <name>`. If it's beyond Initialize, tell the human that re-initialising will invalidate the plan approval, and ask whether to continue before doing anything. Otherwise go straight on to the interview.

   **Main checkout with status beyond Draft**: this repo predates task folders and runs its task in place. Carry on below in this checkout, without `--task`.

   **Inside a task folder** (`Task: <name> (worktree …)`): this session was opened in the task folder itself. Carry on below without `--task`. If status is beyond Initialize, ask before re-initialising, as above.

2. **Interview the human, one question at a time.** Wait for each answer. Ask follow-ups where an answer is vague. Cover, in order:
   1. **Goal**: what outcome, for whom, and why now?
   2. **Inputs and outputs**: where does the data come from (ask for a sample file or payload and save it under `samples/`), and what exactly should be produced?
   3. **Business rules**: the rules the logic must follow, with thresholds, precedence, rounding, time zones, and what to do with invalid or duplicate input. Restate each one precisely and get confirmation.
   4. **Examples**: at least one happy path, one edge case and one error case, each with concrete input and exact expected output. Offer to draft examples from the rules for the human to correct.
   5. **Acceptance criteria**: propose 3–7 testable criteria in Given/When/Then form derived from the answers so far. Ask the human to accept, edit or add.
   6. **Constraints and scope**: systems it may and may not touch, data sensitivity, performance or scale, deadline, expected lifespan, and what's explicitly out of scope.

   Keep it to about 8 questions unless the task is genuinely complex. Never invent rules or expected outputs; anything unconfirmed goes in §6 Open Questions.

3. **Fill the task's contract.md §1–§6** (`.tasks/<name>/contract.md`) with the confirmed answers, using the IDs `BR-n`, `EX-n` and `AC-n` (numbered from 1 in every task). Every AC must be observable and testable. Edit only these sections.

4. Show the human a compact summary (goal, rules, examples, ACs, open questions) and ask for explicit confirmation. Apply any corrections.

5. Run `python3 scripts/harness.py set-status Initialize --task <name>`. If the gate fails, fix the contract and retry.

6. Finish with: status, AC count, open questions, and "Next: `/light-harness:task-plan`".

Don't explore the codebase, write a plan, or write any code in this stage.
