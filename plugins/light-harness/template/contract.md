# Contract: <task name>

Status: Draft

> This file is the single source of truth for the task. Sections 1–8 form the **plan region**: once the plan is approved, any edit there invalidates the approval.
> Blocks marked `harness:` (Status line, approval, red gate, verification, verdict, status log) are owned by the `light-harness` command. Agents must never edit them by hand.

<!-- harness:plan -->
## 1. Task

<!-- harness:goal -->
**Goal:** <one or two sentences: what outcome, for whom>
**Context / trigger:** <why now, who asked, where this runs>
**Inputs:** <data, files, APIs, parameters>
**Outputs:** <files, records, responses, side effects>
**Constraints:** <systems it may touch, data sensitivity, performance, deadline>
**Expected lifespan:** <throwaway | reused | likely permanent>
<!-- /harness:goal -->

## 2. Business Rules

<!-- harness:rules -->
- BR-1: <rule, stated precisely enough to test>
<!-- /harness:rules -->

## 3. Examples

<!-- harness:examples -->
| ID | Input | Expected output | Notes |
|----|-------|-----------------|-------|
| EX-1 | <input> | <expected> | <happy path / edge / error> |
<!-- /harness:examples -->

## 4. Acceptance Criteria

Each criterion must be observable and testable. Tests reference criteria by ID (e.g. `AC-1`).

<!-- harness:acs -->
- AC-1: <Given … When … Then …>
<!-- /harness:acs -->

## 5. Out of Scope

- <explicitly not doing>

## 6. Open Questions

- <question> — <answer / owner>

## 7. Exploration
*(filled by the explorer subagent during /light-harness:task-plan)*

### 7.1 Findings
<what exists today: code, data, systems, conventions, dependencies discovered>

### 7.2 Risks
<!-- harness:risks -->
| ID | Risk | Likelihood | Impact | Mitigation |
|----|------|------------|--------|------------|
| R-1 | <risk> | <L/M/H> | <L/M/H> | <mitigation> |
<!-- /harness:risks -->

### 7.3 Impact Analysis
<!-- harness:impact -->
| Area / file / system | Change | Blast radius | Rollback |
|----------------------|--------|--------------|----------|
| <area> | <change> | <who/what is affected> | <how to undo> |
<!-- /harness:impact -->

## 8. Implementation Plan

### 8.1 Files in scope
Paths or globs the implementation may create or change. Changes outside this list fail the anti-drift check unless recorded in §10.3.
<!-- harness:scope -->
- <src/path/or/glob>
<!-- /harness:scope -->

### 8.2 Interfaces
<function signatures, CLI usage, schemas: what the stubs will expose before the red gate>

### 8.3 Steps
Each step names the AC(s) it satisfies.
<!-- harness:steps -->
1. <step> (AC-1)
<!-- /harness:steps -->

### 8.4 Test Strategy
Map every AC to the tests that will prove it.
<!-- harness:test-strategy -->
- AC-1 → <unit / integration test description, examples used>
<!-- /harness:test-strategy -->
<!-- /harness:plan -->

## 9. Plan Approval
<!-- harness:approval -->
_Not approved. A human runs `light-harness approve --task <name> --name "Full Name"` in their own terminal._
<!-- /harness:approval -->

## 10. Build Record

### 10.1 Red Gate
<!-- harness:redgate -->
_Not run._
<!-- /harness:redgate -->

### 10.2 Implementation Notes
<what was built, per plan step, and anything the reviewer should know>

### 10.3 Deviations from Plan
Any file changed outside §8.1, or any step done differently, with justification. Each listed path is surfaced to the reviewer as a warning.
<!-- harness:deviations -->
<!-- /harness:deviations -->

### 10.4 Verification
<!-- harness:verify -->
_Not run._
<!-- /harness:verify -->

## 11. Review

### 11.1 Acceptance Criteria Check
*(reviewer ticks each AC only after confirming it with evidence)*
<!-- harness:ac-check -->
- [ ] AC-1
<!-- /harness:ac-check -->

### 11.2 Issues
Severity: Critical | High | Medium | Low. Status: Open | Fixed | Resolved | Won't fix.
Only the reviewer sets **Resolved**. **Won't fix** is allowed for Low only. Any Critical, High or Medium issue that isn't Resolved blocks a pass.
<!-- harness:issues -->
| ID | Severity | Status | Area | Description | Ref (AC/BR/Plan) |
|----|----------|--------|------|-------------|------------------|
<!-- /harness:issues -->

### 11.3 Review Summary
<reviewer's overall assessment>

### 11.4 Verdict
<!-- harness:verdict -->
_Pending._
<!-- /harness:verdict -->

## 12. Status Log
<!-- harness:status-log -->
<!-- /harness:status-log -->
