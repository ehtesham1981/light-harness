---
name: task-review
description: Stage 4. Run the reviewer subagent against the acceptance criteria and approved plan, log issues with severity, and record a PASS/FAIL verdict. PASS → Done, FAIL → Rework.
disable-model-invocation: true
---

You are running **/light-harness:task-review**.

1. Run `python3 scripts/harness.py status`. Status must be **Build** (or **Review** if a previous review was interrupted). Otherwise stop and name the right command.

2. If status is Build, run `python3 scripts/harness.py set-status Review`. This re-runs full verification and refuses to enter Review if it fails. On refusal, report the failures and tell the human to run `/light-harness:task-implement`.

3. **Delegate to the `light-harness:reviewer` subagent** with this brief: "Review the implementation independently against contract.md: re-run verify, check every AC with evidence (§11.1), check business rules, plan conformance, deviations, test quality and gaming. Log issues in §11.2 with severity and re-check any existing ones. Write §11.3. Return your recommended verdict and the blocking issue IDs."
   Don't pass it your own opinion of the work. It must judge from the contract, code, tests and git history.

4. Record the verdict the reviewer recommends:
   - PASS: `python3 scripts/harness.py verdict pass --by reviewer`
   - FAIL: `python3 scripts/harness.py verdict fail --by reviewer`

   The harness refuses PASS if any Critical/High/Medium issue is unresolved, any AC is unticked, or verification fails. If it refuses, record **FAIL**. Never edit issues to get a pass.

5. Report to the human:
   - **PASS** → status **Done**. Summarise the evidence and any open Low issues. Then offer the close-out: archive the repo, or promote it into a permanent repo with a backfilled story that links to contract.md and brings the tests along.
   - **FAIL** → status **Rework**. List the blocking issues (ID, severity, one line each). Next: `/light-harness:task-implement` to fix them, then `/light-harness:task-review` again.
