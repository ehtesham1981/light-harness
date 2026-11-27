# Plan: issue #50 — human-only `amend-config`; warn when `test_cmd` has no `{concurrency}`

Issue: https://github.com/ehtesham1981/light-harness/issues/50
Branch: `algomade/happy-planck-um7j8g` (from `main` @ 195dec5). No version bump (owner's decision).

## Decisions taken with the owner

- Part 1 uses approach (a): a HUMAN-ONLY `amend-config` command that records a new config snapshot in the
  audit trail. The byte-for-byte anti-gaming comparison in `verify` stays; only its reference changes.
- Accepted changes: concurrency arguments in `test_cmd` (both directions, no cap), `parallel`, and the two
  timeouts (`suite_timeout`, `probe_timeout`): lowering always allowed, raising capped.
- The "no `{concurrency}`" warning covers `node --test`, `pytest -n auto|logical`, jest and vitest.

## Business rules

- **BR-1 Who and when.** `amend-config` is human-only: `require_human` (interactive terminal), listed in
  `HUMAN_ONLY` and `TRACKED`, and blocked for agents by the guard hook (`hook_guard.py` `BLOCK_BASH`, rule id
  `human-only`). It runs only when the status is Build, Review or Rework **and** the current plan approval is
  valid (`approval_state` ok). In Draft/Initialize/Plan it refuses and says to edit the config directly
  (in Plan: "edit config.json, then run `approve` again"; earlier: nothing approved yet). Requires `--name` and a non-empty `--reason`.
- **BR-2 Reference snapshot.** The *effective approved config* is the `config.json` in the newest
  `harness: config-amended` event that is newer than the current `harness: plan-approved` event, else the one
  in `plan-approved`. A new `approve` therefore discards earlier amendments (they are older than it).
- **BR-2a Inputs must be readable.** Refuse (record nothing) when the live `config.json` is missing, is not
  valid JSON or is not a JSON object, or when the effective approved snapshot has no `config.json` or it
  is not a JSON object.
- **BR-3 Nothing to amend.** If the live config parses to the same JSON value as the effective approved
  config, refuse (`nothing to amend`), exit non-zero, record nothing.
- **BR-4 Classification (the oracle).** Compare live `L` with the effective approved `A` (both parsed JSON
  objects; a config that is not a JSON object is refused). Every difference must be one of:
  1. **Concurrency argument in `test_cmd`.** `test_cmd` is compared with defaults filled in (a missing key
     means `DEFAULT_CONFIG["test_cmd"]`, as `load_config()` does). After removing the *runner's*
     concurrency arguments from both `A.test_cmd` and `L.test_cmd`, the remaining lists must be identical
     (same items, same order). Runner detection and the removable arguments:
     - node test runner: `test_cmd[0]` basename is `node`/`node.exe` and a `--test` item appears before the
       first non-option item. Removable: `--test-concurrency=V`, V ∈ {positive integer, `{concurrency}`}.
     - pytest: an item whose basename is `pytest`/`py.test`/`pytest.exe`, or the pair `-m`, `pytest`.
       Removable: `-n V`, `-nV`, `-n=V`, `--numprocesses V`, `--numprocesses=V`;
       V ∈ {non-negative integer, `auto`, `logical`, `{concurrency}`}.
     - jest / vitest: an item whose basename (minus `.js`/`.cmd`/`.mjs`) is `jest`/`vitest`.
       Removable: `--maxWorkers V`, `--maxWorkers=V`; V ∈ {positive integer, `N%`, `{concurrency}`}.
       (jest's `-w` short form and npm-script wrappers are not recognised: refused, documented.)
     **Position rule (anti-bypass).** A token (or flag+value pair) is removable only if, in its list, it
     sits **immediately after the runner marker** (node: the `--test` item; pytest: the `pytest` item or the
     `-m pytest` pair; jest/vitest: the runner item) — several removable tokens may follow one another
     there — **or** (pytest/jest/vitest only) it is the **last** token/pair of the list and the list has no
     `--` item. Anything else (after `--`, before the runner token, after an option that might take a
     separate value, after a script path, a value outside V, a flag of another runner) is not removable,
     so it shows up as a difference and is refused.
  2. **`parallel`**: added, removed or changed, provided the live value (when present) is an int 1..4
     (not bool). Any direction. The summary states the effective value: a missing key means probes run one
     at a time (`probe_workers`), so removal reads `parallel 3 → (absent: probes one at a time)`.
  3. **`suite_timeout`, `probe_timeout`**: added, removed or changed. The effective values (missing key ⇒
     default 3600 / 600, via the existing `suite_timeout()`/`probe_timeout()` helpers for the approved
     side) are compared with the value in the **plan-approved** snapshot (not the last
     amendment, so chained amendments can't walk past the cap). A live value present must be a positive
     int (not bool). Lowering: always allowed. Raising: allowed up to `TIMEOUT_RAISE_CAP = 3` × the
     plan-approved effective value; beyond it refuse with `needs re-approval`.
  Any other difference (any other key added, removed or changed — `test_globs`, `protected_globs`,
  `red_exit_codes`, `junit_arg`, `junit_path`, `always_allowed`, `worktree_dir`, reporters, filters,
  paths, `test_cmd[0]` …) refuses the whole amendment with a message that names the field(s) and says to go
  back to Plan and re-approve. Refusal records nothing.
- **BR-5 Record.** The live config bytes are read **once**; classification runs on those bytes. After the
  human types `AMEND`, the file is read again and, if its bytes differ, the command refuses (`config.json
  changed while waiting; run amend-config again`). The checked bytes are passed to `record()` as
  `extra={"config.json": raw, ...}` so what is recorded is exactly what was checked. `--name`/`--reason`
  containing a newline, `<!--` or `-->` are refused. On success `record()` writes
  `harness: config-amended by NAME: <change summary> — <reason>` (one line; the summary lists each change,
  e.g. `test_cmd +--test-concurrency={concurrency}`, `probe_timeout 600 → 1200`, `parallel 3 → 2`).
  The event's tree carries the new `config.json` (record() already does) plus `config-amend.json` with
  `{name, reason, when, changes:[…], raised_timeouts:[…]}`. `--name` is never put in telemetry (as
  `unlock-tests`). `telemetry.HUMAN_COMMANDS` gains `amend-config` (kept in step with `HUMAN_ONLY`). The
  contract's approval block gets **appended** (not replaced; several amendments stack) a line
  `**Config amended** by NAME at WHEN: <summary> — <reason>`. Plan hash and red gate are untouched (the
  approval block is harness-owned and excluded from the plan hash — to be confirmed in tests).
- **BR-6 Verify.** The anti-gaming config check compares the live config as a parsed JSON value (as today,
  via `_json_or_raw`) with the effective approved config (BR-2). A config identical to the latest amendment
  passes; one that differs from it fails with the existing message. With no amendment, behaviour is exactly
  as at base.
- **BR-7 Red gate and approval untouched.** After `amend-config`, `approval_state` and `redgate_state`
  return what they returned before (no re-approval, no new red gate). `verdict pass` works as before.
- **BR-8 Cache.** The verify reuse key already hashes the config, so a stored pass from before the amendment
  is not reused (no code change; covered by a test).
- **BR-9 Reviewer visibility.** While any timeout is raised above its plan-approved value, `verify` adds a
  ⚠ line: `Config amended after approval: probe_timeout 600 → 1200 raised (reviewer: check probes/tests
  that rely on the timeout)`. `agents/reviewer.md` tells the reviewer to check such probes/tests. `status`
  shows the latest amendment, if any.
- **BR-10 Concurrency warning.** `parallel_runner_warning(cfg)` returns a warning string iff `test_cmd`
  has no item containing `{concurrency}` and the runner parallelises by default: node `--test`; pytest with
  `-n`/`--numprocesses` (any value other than 0/1); jest unless `--runInBand`/`-i`/`--maxWorkers=1`/`-w 1`;
  vitest unless `--no-file-parallelism`/`--maxWorkers=1`; node unless `--test-concurrency=1`. A fixed cap
  above 1 still warns (slots don't scale it). The text says `lightharness.slots` limits how many suite runs share the machine,
  not how many test files run at once inside one run, and suggests the `{concurrency}` form for that runner.
  Printed (never failing the gate) by `redgate`, `verify` (as a ⚠ line in its report) and the Build
  baseline run (`ensure_baseline`, only when it actually runs the suite). `check plan` prints it as a ⚠ hint
  (exit code unchanged); the explorer agent prompt asks for `{concurrency}` in `test_cmd`.
- **BR-11 Preservation.** For configs and audit trails without `config-amended` events, every existing gate
  outcome and every existing guard outcome is unchanged; all existing tests and goldens pass unmodified.
  The guard's existing human-only message text is not changed (a golden embeds it); `amend-config` gets its
  own `BLOCK_BASH` entry with the same rule id.

## Acceptance criteria

- **AC-1** (end-to-end, with a fake `node` shim — an executable named `node` first on PATH that accepts
  `--test`/`--test-concurrency=N` and runs the toy runner — or with `run_process` captured as in
  `tests/test_run_concurrency.py`) A human runs `amend-config --name N --reason R` in Build after adding
  `--test-concurrency={concurrency}` to a node `test_cmd`; the trail gets one `harness: config-amended by N`
  event naming the change and R, then `verify` passes its config check without re-approval or a new red
  gate (`approval_state`, `redgate_state` still ok). (BR-1, BR-2, BR-5, BR-6, BR-7)
- **AC-2** The classifier (BR-4) accepts exactly the allowed changes and refuses everything else, naming the
  field: swept over generated pairs of configs (runner × flag form × value × other-arg mutations × key
  mutations) against an independent oracle written in the test. Refusals record nothing. (BR-4)
- **AC-2a** Position bypasses are refused: concurrency token placed between an option and its value
  (`--test-name-pattern --test-concurrency=2 foo`), after `--`, before the runner token (`npx --maxWorkers=2
  jest`), after a node script path (`node run.mjs --test --test-concurrency=1`), value outside V
  (`-n tests/x.py`). (BR-4.1)
- **AC-3** Timeouts: lowering any amount is accepted; raising up to 3× the plan-approved value accepted,
  above refused with `needs re-approval`; chained amendments can't exceed the cap; invalid values refused.
  Raised timeouts appear as a ⚠ in verify. (BR-4.3, BR-9)
- **AC-4** Refusals: not human (no tty) exit 3; wrong stage; no valid approval; nothing to amend; not
  typing `AMEND`; live config missing / invalid JSON / not an object; approved snapshot without a config;
  `--name`/`--reason` with a newline or comment marker; config file changed while the prompt waited (the
  recorded config is the checked one). The guard blocks `light-harness amend-config …` / `harness.py amend-config …` from an
  agent with rule `human-only`. (BR-1, BR-3)
- **AC-5** A new `approve` after an amendment makes the plan-approved config the reference again; a live
  config that differs from the latest amendment fails verify's anti-gaming check; a cached verify pass from
  before the amendment is not reused. (BR-2, BR-6, BR-8)
- **AC-6** The concurrency warning (BR-10) appears for each listed runner without `{concurrency}`, not for
  the same commands with `{concurrency}`, not for plain pytest / serial jest / serial vitest / non-test
  commands; shown by redgate, verify, baseline and `check plan`, never changing their exit codes. Swept.
- **AC-7** Preservation: all existing tests and goldens pass unchanged. (BR-11)
- **AC-7a** Telemetry: `amend-config` is a `command` event with `actor="human"`, counted as human by
  `telemetry.HUMAN_COMMANDS`, never carrying `--name`.
- **AC-8** Docs: README (main and plugin) explain `amend-config`, what `lightharness.slots` does vs
  `{concurrency}`, and the warning; `rules.md` / `task-implement` skill mention asking the human for
  `amend-config` when the suite only passes with capped concurrency; reviewer and explorer prompts updated.

## Files in scope

- `plugins/light-harness/scripts/harness.py` — `amend-config` command, classifier, effective approved
  config, verify/redgate/baseline/check warnings, status line.
- `plugins/light-harness/scripts/hook_guard.py` — new human-only `BLOCK_BASH` entry (existing message and
  the `config-frozen` message unchanged: goldens embed them).
- `plugins/light-harness/scripts/telemetry.py` — `HUMAN_COMMANDS` gains `amend-config`.
- `plugins/light-harness/agents/reviewer.md`, `agents/explorer.md`, `rules.md`,
  `skills/task-implement/SKILL.md` (the human edits `config.json` themselves, then runs `amend-config`), `skills/task-plan/SKILL.md` (if it lists config advice),
  `plugins/light-harness/README.md`, `README.md`.
- New tests: `tests/test_amend_config.py`, `tests/test_concurrency_warning.py` (may use a small lab helper).

## Pre-existing state

On untouched `main` in this container 40 tests already fail (`test_verify_cache.py`, `test_verdict_reuse.py`
labs; same under Python 3.12; CI not affected as far as known). "Green" for this task means: every new test
passes and no test outside that recorded list fails.

## Steps

1. Tests first (test-author subagent): AC-1..AC-7 tests, failing against stubs (`amend-config` registered
   but dying "not implemented"; `classify_config_change`, `effective_approved_config`,
   `parallel_runner_warning` stubbed).
2. Independent test review against the issue and these ACs.
3. Implement `classify_config_change(approved, plan_approved, live) -> {"ok", "changes", "raised", "refused"}`.
4. Implement `effective_approved_row()` / config and switch `verify` to it; add the ⚠ for raised timeouts.
5. Implement `cmd_amend_config`, register it, add to `HUMAN_ONLY`/`TRACKED`; approval-block note; status.
6. Guard entry in `hook_guard.py`.
7. `parallel_runner_warning` and its call sites (redgate, verify, ensure_baseline, check plan).
8. Docs and agent prompts.
9. Full suite green; independent review loop (cap 5).

## Risks

| Risk | Mitigation |
|---|---|
| Classifier lets a selection change through (e.g. `-n tests/x.py`, token wedged between an option and its value) | Per-flag value sets + position rule (after runner marker, or last with no `--`); adversarial sweep |
| Config swapped by a background process while the human reads the prompt | Bytes read once, re-checked after `AMEND`, checked bytes recorded |
| Guard is pattern-based (string tricks) | Real gate is `require_human` (tty) + typed `AMEND`; tested (exit 3) |
| Chained amendments bypass the timeout cap | Cap measured against the plan-approved value, not the last amendment |
| Guard golden embeds the human-only message | Separate `BLOCK_BASH` entry; existing text untouched; goldens unchanged |
| npm-script wrappers (`npm test -- --maxWorkers=2`) not recognised as jest | Refused (needs re-approval); documented; the human can name the runner in `test_cmd` at the next plan |
| Lower concurrency hides race bugs | Change is explicit, audited and shown in the trail/approval block |
| Concurrency warning noisy for deliberate fixed caps | Only one line; suggests `{concurrency}`; never fails a gate |
