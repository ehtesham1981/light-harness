# Light Harness

A lightweight, gated development harness for [Claude Code](https://code.claude.com), for one-off tasks that have no story and no existing repo, where you still want agentic work to be planned, test-driven, reviewed, and auditable.

Each task runs in its own repo. A single `contract.md` is the source of truth, and four commands move the task through enforced gates:

```
task-init ─► Initialize ─► task-plan ─► Plan ─[human approves]─► task-implement ─► Build ─► task-review ─┬─► Done
                                                                        ▲                                │
                                                                        └──────────── Rework ◄───────────┘
```

| Command | Stage | What happens | Gate |
|---|---|---|---|
| `/light-harness:task-init` | Initialize | Installs the harness on first run, then interviews you for the goal, business rules, examples and acceptance criteria | ≥1 example, ≥1 testable acceptance criterion (AC) |
| `/light-harness:task-plan` | Plan | **explorer** subagent writes findings, risks, impact analysis, files in scope, interfaces, steps and test strategy | Every AC mapped to steps and tests; **a human approves**, and their name, the time and the plan hash are recorded |
| `/light-harness:task-implement` | Build | **test-author** subagent writes tests → stubs → **red gate** → implementation → **verify** | Red gate: tests trace to every AC, fail on stubs, none pass vacuously, then get locked · Verify: locked tests unchanged and passing, anti-gaming scan, anti-drift vs approved scope |
| `/light-harness:task-review` | Done / Rework | **reviewer** subagent checks every AC with evidence and logs issues with severity | PASS refused while any Critical/High/Medium issue is unresolved, any AC is unticked, or verify fails |

## Install

In Claude Code:

```
/plugin marketplace add your-org/light-harness
/plugin install light-harness@light-harness
```

On Claude Code 2.1.275 or later, one step also works: `/plugin install light-harness --marketplace your-org/light-harness`.

From a local clone: `/plugin marketplace add ./light-harness`. To try it without installing: `claude --plugin-dir ./light-harness/plugins/light-harness`.

**Auto-install for a team:** add this to a shared project's `.claude/settings.json`, and Claude Code offers the plugin to anyone who opens the project:

```json
{
  "extraKnownMarketplaces": {
    "light-harness": { "source": { "source": "github", "repo": "your-org/light-harness" } }
  },
  "enabledPlugins": { "light-harness@light-harness": true }
}
```

**Prerequisites:** Python 3.9+ (found as `python3`, `python` or `py -3`), git, and your test runner (pytest by default). Works on macOS, Linux and Windows (Git Bash).

## Use

```bash
mkdir invoice-dedupe && cd invoice-dedupe   # one fresh directory per task
claude
```
```
/light-harness:task-init Invoice dedupe
/light-harness:task-plan
```
Approve the plan **in your own terminal** (it's interactive, and the agent is blocked from running it):
```bash
python3 scripts/harness.py approve --name "Your Full Name"
```
```
/light-harness:task-implement
/light-harness:task-review
```
Check progress at any time with `python3 scripts/harness.py status`.

The first `task-init` installs the harness into the task repo. It copies `contract.md`, `scripts/harness.py` and `.harness/config.json`, appends the rules to `CLAUDE.md`, merges deny rules into `.claude/settings.json`, and runs `git init`. Plugins can't ship permission rules or a loaded `CLAUDE.md`, so the installer adds them to the repo.

### Human-only commands
```bash
python3 scripts/harness.py approve --name "Full Name"                       # approve the plan
python3 scripts/harness.py unlock-tests --name "Full Name" --reason "..."   # let tests be revised after the red gate
```

## What enforces what

| Mechanism | What it stops |
|---|---|
| Stage gates (`set-status`) | Skipping stages, building without approval, entering review with failing verification |
| Plan hash | Changing requirements or the plan after approval. Any edit to contract §1–§8 voids the approval and red gate |
| Git-anchored state | Approval, red gate, test unlocks and verdicts are `harness:` commits. Gates are recomputed from git history, not a side file |
| Guard hook | Code edits before Build or during Review, edits to locked tests, hand edits of the Status line and harness-owned contract blocks, edits to harness machinery. Fails closed in a harness repo if Python is missing; inert in other repos |
| Red gate | Tests that pass before implementation, tests not traced to an AC, skipped tests |
| `verify` | Modified, deleted or added protected files; test-runtime sniffing; skip/xfail/only markers; changes outside approved scope; harness tampering. Warns on swallowed exceptions, coverage exclusions, mocks in implementation code, and comparisons against test literals |
| Reviewer + verdict gate | Subtle gaming (special-casing example values), unmet ACs and rules, missing risk mitigations, unjustified deviations |

**Limits:** the agent has a shell, so the harness makes bypass costly and visible rather than impossible. For example, the agent could forge a `harness:` commit. The defences are deny rules, gates recomputed from git, and a reviewer that re-runs `verify` and inspects history. For high-stakes tasks, run the review in a fresh session and glance at `git log --oneline`.

## Customise per task

- **Stack:** during planning, set `test_cmd`, `junit_arg` / `junit_path`, `test_globs` and `protected_globs` in `.harness/config.json`. The config is frozen once the plan is approved. Example for Jest: `"test_cmd": ["npx","jest","--ci","--reporters=default","--reporters=jest-junit"]`, `"junit_arg": ""`, `"junit_path": ".harness/junit.xml"`, with `JEST_JUNIT_OUTPUT_FILE=.harness/junit.xml` set in the environment.
- **Team rules:** the harness section appended to the task repo's `CLAUDE.md`.

## Repository layout

```
.claude-plugin/marketplace.json      marketplace (this repo is installable directly)
plugins/light-harness/
  .claude-plugin/plugin.json
  skills/task-{init,plan,implement,review}/SKILL.md    stage commands
  agents/{explorer,test-author,reviewer}.md            subagents
  hooks/hooks.json                                     guard hook → scripts/guard.sh
  scripts/harness.py      gate engine (vendored into each task repo by install.py)
  scripts/hook_guard.py   guard rules
  scripts/guard.sh        cross-platform Python launcher for the hook
  scripts/install.py      installs the harness into a task repo
  template/               contract, rules, config, permissions copied on install
tests/selftest.sh         end-to-end gate test (also run in CI)
```

## Contributing

1. Change things under `plugins/light-harness/`.
2. Run `bash tests/selftest.sh`. It drives a toy task through every stage, including deliberate gaming attempts that must be caught.
3. Run `claude plugin validate --strict .`
4. Bump `version` in `plugins/light-harness/.claude-plugin/plugin.json` so users get the update via `/plugin update`.

CI runs the self-test on Python 3.9 and 3.12, validates the manifests, and checks the hook launcher on Windows.

## License
MIT
