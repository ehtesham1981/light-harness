---
name: task-init
description: Stage 1. Start a task in its own folder (.tasks/<name>, a git worktree) without adding anything to the repo, then interview the human in the same session to capture the task, business rules, examples and acceptance criteria in its contract, and set status to Initialize.
disable-model-invocation: true
argument-hint: [short task name]
---

You are running **/light-harness:task-init**. Task name hint: `$ARGUMENTS`

Light Harness runs from the plugin and keeps nothing in the work repo: no files, rules or settings are added to it. The task's contract lives in the task folder's `.light-harness/` (ignored by git), and its audit trail in git refs that never reach a branch. If `light-harness` isn't found below, use `sh "${CLAUDE_PLUGIN_ROOT}/bin/light-harness"` in its place for the rest of this task, and give subagents that full command too.

1. Run `light-harness status` from the project's main folder.

   **Main checkout** (`Checkout: … main checkout`): start the task in its own folder, then carry straight on in this session.
   - If `$ARGUMENTS` names a task that's already in progress (`light-harness tasks`), use it and skip to the status check below.
   - Otherwise ask for a short task name if `$ARGUMENTS` is empty, and ask whether this task **builds on a finished task** (listed by `light-harness tasks`) or is **independent**.
   - Run `light-harness start-task "<name>"`, adding `--from <earlier-task>` if it builds on one. It creates `.tasks/<name>/` on branch `task/<name>`, with the contract at `.tasks/<name>/.light-harness/contract.md`. `<name>` from here on is the name it prints (lowercased, hyphenated).
     - If it says the repo has **no commits yet**, ask the human whether to make an empty first commit (`git commit --allow-empty -m "Initial commit"`); a task folder is made from a commit. Run `git init` first if the folder isn't a git repo, also with their OK.
     - If it says the repo still has **files from Light Harness 1.x**, stop. Relay the list, and tell the human to clean them up by running this in their own terminal in the project's main folder (`python3` on macOS/Linux): `python "${CLAUDE_PLUGIN_ROOT}/scripts/harness.py" uninstall` (add `--dry-run` first to see what it removes). It leaves the changes for them to review and commit. Write the command out with the plugin path filled in.
   - From now on, **this session's task is `<name>`**: add `--task <name>` to every `light-harness` command, and edit `.tasks/<name>/.light-harness/contract.md`.
   - Run `light-harness status --task <name>`. If it's beyond Initialize, tell the human that re-initialising will invalidate the plan approval, and ask whether to continue before doing anything. Otherwise go straight on to the interview.

   **Inside a task folder** (`Task: <name> (folder …)`): this session was opened in the task folder itself. Carry on below without `--task`. If status is beyond Initialize, ask before re-initialising, as above.

2. **Interview the human, one question at a time.** Wait for each answer. Ask follow-ups where an answer is vague. Cover, in order:
   1. **Goal**: what outcome, for whom, and why now?
   2. **Inputs and outputs**: where does the data come from (ask for a sample file or payload and save it under `samples/`), and what exactly should be produced?
   3. **Business rules**: the rules the logic must follow, with thresholds, precedence, rounding, time zones, and what to do with invalid or duplicate input. Restate each one precisely and get confirmation. When a rule holds for every input rather than for a few examples ("existing outcomes don't change", "the guard blocks every write into X", "the parser reads every legacy file"), tag it `[Preservation]` (outcomes unchanged from the base commit, apart from listed changes) or `[Invariant]` (a property of every input), and ask for its four parts as sub-bullets: `Input space:` (the dimensions and their values), `Oracle:` (the base-commit code, a reference, or a property), `Unacceptable failure:` (the failure direction that is never acceptable) and `Allowed changes:` (the outcomes this task may change, or `none`). Untagged rules are Example rules.
   4. **Examples**: at least one happy path, one edge case and one error case, each with concrete input and exact expected output. Offer to draft examples from the rules for the human to correct.
   5. **Acceptance criteria**: propose 3–7 testable criteria in Given/When/Then form derived from the answers so far. Ask the human to accept, edit or add.
   6. **Constraints and scope**: systems it may and may not touch, data sensitivity, performance or scale, deadline, expected lifespan, and what's explicitly out of scope.

   Keep it to about 8 questions unless the task is genuinely complex. Never invent rules or expected outputs; anything unconfirmed goes in §6 Open Questions.

3. **Fill the task's contract §1–§6** (`.tasks/<name>/.light-harness/contract.md`) with the confirmed answers, using the IDs `BR-n`, `EX-n` and `AC-n` (numbered from 1 in every task). Every AC must be observable and testable. Edit only these sections.

4. Show the human a compact summary (goal, rules, examples, ACs, open questions) and ask for explicit confirmation. Apply any corrections.

5. Run `light-harness set-status Initialize --task <name>`. If the gate fails, fix the contract and retry.

6. Finish with: status, AC count, open questions, and "Next: `/light-harness:task-plan`".

Don't explore the codebase, write a plan, or write any code in this stage.

<!-- Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE). SPDX-License-Identifier: Apache-2.0 -->
