---
name: task-init
description: Stage 1. Interview the human to capture the task, business rules, examples and acceptance criteria in contract.md, then set status to Initialize.
disable-model-invocation: true
argument-hint: [short task name]
---

You are running **/light-harness:task-init** for this one-off task. Task name hint: `$ARGUMENTS`

0. **Install the harness into this repo** (first run only; safe to repeat). Run from the task repo root, a fresh dedicated directory per task:
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/install.py" "${CLAUDE_PLUGIN_ROOT}" "$(pwd)"`
   (use `python` or `py -3` if `python3` isn't available). It copies `contract.md`, `scripts/harness.py` and `.harness/config.json`, appends the harness rules to `CLAUDE.md`, and merges the harness permission rules into `.claude/settings.json`.

1. Run `python3 scripts/harness.py init --name "$ARGUMENTS"`, then `python3 scripts/harness.py status`. If status is beyond Initialize, tell the human that re-initialising will invalidate the plan approval, and ask whether to continue before doing anything.

2. **Interview the human, one question at a time.** Wait for each answer. Ask follow-ups where an answer is vague. Cover, in order:
   1. **Goal**: what outcome, for whom, and why now?
   2. **Inputs and outputs**: where does the data come from (ask for a sample file or payload and save it under `samples/`), and what exactly should be produced?
   3. **Business rules**: the rules the logic must follow, with thresholds, precedence, rounding, time zones, and what to do with invalid or duplicate input. Restate each one precisely and get confirmation.
   4. **Examples**: at least one happy path, one edge case and one error case, each with concrete input and exact expected output. Offer to draft examples from the rules for the human to correct.
   5. **Acceptance criteria**: propose 3–7 testable criteria in Given/When/Then form derived from the answers so far. Ask the human to accept, edit or add.
   6. **Constraints and scope**: systems it may and may not touch, data sensitivity, performance or scale, deadline, expected lifespan, and what's explicitly out of scope.

   Keep it to about 8 questions unless the task is genuinely complex. Never invent rules or expected outputs; anything unconfirmed goes in §6 Open Questions.

3. **Fill contract.md §1–§6** with the confirmed answers, using the IDs `BR-n`, `EX-n` and `AC-n`. Every AC must be observable and testable. Edit only these sections.

4. Show the human a compact summary (goal, rules, examples, ACs, open questions) and ask for explicit confirmation. Apply any corrections.

5. Run `python3 scripts/harness.py set-status Initialize`. If the gate fails, fix the contract and retry.

6. Finish with: status, AC count, open questions, and "Next: `/light-harness:task-plan`".

Don't explore the codebase, write a plan, or write any code in this stage.
