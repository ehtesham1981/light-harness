# Light Harness

A lightweight, gated development harness for [Claude Code](https://code.claude.com), for tasks where you want agentic work to be planned, test-driven, reviewed, and auditable. A task can be a standalone one-off or one story in a set that builds a project.

**It leaves nothing in your repo.** The harness runs from the plugin. Its contracts and audit trail live inside git, outside the working tree. No files, rules, settings, ignore entries or commits are added to your branches. What a task hands you is a branch with the work on it: tests and code, committed by the agent step by step.

Each task runs in its own folder inside the project, `.tasks/<name>/` (a git worktree on branch `task/<name>`), so several sessions can work in parallel on one repo. You always open the project's main folder, and the session that starts a task works on it. The task's contract is the source of truth, and four commands move the task through enforced gates:

```
task-init ─► Initialize ─► task-plan ─► Plan ─[human approves]─► task-implement ─► Build ─► task-review ─┬─► Done
                                                                        ▲                                │
                                                                        └──────────── Rework ◄───────────┘
```

| Command | Stage | What happens | Gate |
|---|---|---|---|
| `/light-harness:task-init` | Initialize | Starts the task in `.tasks/<name>/`, then interviews you for the goal, business rules, examples and acceptance criteria | ≥1 example, ≥1 testable acceptance criterion (AC) |
| `/light-harness:task-plan` | Plan | **explorer** subagent writes findings, risks, impact analysis, files in scope, interfaces, steps and test strategy | Every AC mapped to steps and tests; **a human approves**, and their name, the time and the plan hash are recorded |
| `/light-harness:task-implement` | Build | **test-author** subagent writes tests → stubs → **red gate** → implementation → **verify** | Red gate: the task's tests trace to every AC, fail on stubs, none pass vacuously, then get locked; tests from before the task must keep passing · Verify: locked tests unchanged and passing, anti-gaming scan, anti-drift vs approved scope |
| `/light-harness:task-review` | Done / Rework | **reviewer** subagent checks every AC with evidence and logs issues with severity | PASS refused while any Critical/High/Medium issue is unresolved, any AC is unticked, or verify fails |

## Install

In Claude Code:

```
/plugin marketplace add ehtesham1981/light-harness
/plugin install light-harness@light-harness
```

On Claude Code 2.1.275 or later, one step also works: `/plugin install light-harness --marketplace ehtesham1981/light-harness`.

From a local clone: `/plugin marketplace add ./light-harness`. To try it without installing: `claude --plugin-dir ./light-harness/plugins/light-harness`.

**Auto-install for a team:** add this to a shared project's `.claude/settings.json`, and Claude Code offers the plugin to anyone who opens the project. This is the one line of config you choose to add; the harness itself adds nothing.

```json
{
  "extraKnownMarketplaces": {
    "light-harness": { "source": { "source": "github", "repo": "ehtesham1981/light-harness" } }
  },
  "enabledPlugins": { "light-harness@light-harness": true }
}
```

**Prerequisites:** Python 3.9+ (found as `python3`, `python` or `py -3`), git, and your test runner (pytest by default). Works on macOS, Linux and Windows (Git Bash). The repo needs at least one commit, because a task folder is made from one.

## Use

Open the project's main folder (an existing repo, or a new one with a first commit) and start a task:

```bash
cd my-project
claude
```
```
/light-harness:task-init Invoice dedupe
```
This creates the task folder `.tasks/invoice-dedupe/` on branch `task/invoice-dedupe` and goes straight into the interview in the same session. If the task builds on a finished one, say so when asked. Then:
```
/light-harness:task-plan
```
Approve the plan **in your own terminal**, in the project's main folder. The command is interactive, and the agent is blocked from running it. `task-plan` prints the exact command with the plugin's path filled in. It looks like this (on Windows use PowerShell or Windows Terminal, and `python` instead of `python3`):
```bash
python3 "<plugin folder>/scripts/harness.py" approve --task invoice-dedupe --name "Your Full Name"
```
```
/light-harness:task-implement
/light-harness:task-review
```
In Claude Code sessions the command is on PATH as `light-harness`. Check progress at any time with `light-harness status --task invoice-dedupe`, or list all tasks with `light-harness tasks`.

After a PASS, `task-review` offers to finish the task. `finish-task` removes the task folder and keeps the reviewed work on its branch, which you merge or open a pull request from like any other branch. The contract and audit trail stay readable with `light-harness show <name>` and `light-harness log <name>`.

For another task, run `/light-harness:task-init <name>` in a new session in the same main folder. Several sessions can each work on their own task at once.

### Where everything lives

| What | Where | In your branches? |
|---|---|---|
| Task folder | `.tasks/<name>/`, a git worktree, ignored through `.git/info/exclude` and removed by `finish-task` | no |
| Live contract and stack config | `.tasks/<name>/.light-harness/contract.md` and `config.json`, ignored | no |
| Audit trail | `refs/light-harness/tasks/<name>`: one commit per gate event (task start, approval, baseline, red gate, unlocks, verdicts), each holding the contract and gate records plus a snapshot of the code at that moment | no (a hidden ref, not pushed by `git push`) |
| Finished work | the task's branch (`task/<name>`, or the name you pass to `finish-task --branch`), plus `refs/light-harness/done/<name>` so later tasks can build on it | the branch is the deliverable |
| Repo defaults | `.git/light-harness/config.json`, the last finished task's stack config, used to start the next task | no |
| Rules for the agent | the plugin's `rules.md`, added to the session's context by a SessionStart hook | no |
| Permission rules | enforced by the plugin's PreToolUse hook | no |

### Tasks and commands

```bash
light-harness start-task "<name>" [--from <finished-task>]   # new task folder .tasks/<name> + branch task/<name>
light-harness tasks                                         # tasks in progress and finished
light-harness <command> --task <name>                       # any stage command, for one task
light-harness finish-task <name> [--branch feature/x]       # Done task → remove folder, keep the reviewed work on its branch
light-harness show <name>                                   # print a task's contract
light-harness log <name>                                    # print a task's audit trail
```

- **Your main branch is never touched.** The harness commits nothing to it, and nothing is merged automatically. You merge or open a PR from a finished task's branch when you're ready.
- **The branch is exactly what was reviewed.** `finish-task` refuses while the task folder has uncommitted changes, or while the branch differs from the code the reviewer passed.
- **Parallel sessions are safe.** Each task has its own folder, branch and contract, and each session sticks to its task with `--task`. The guard hook decides from each edited file's path which task's rules apply. While tasks are in progress it blocks edits to the main checkout, and it never lets the agent switch branches, merge, rewrite history, or add or remove worktrees.
- **Building on earlier work:** `--from <finished-task>` starts the new task from the finished one's code, tests and stack config. Independent tasks start from the current branch.
- **Existing tests:** a passed verdict records which tests pass. On an existing codebase, entering Build records them the first time. At the red gate, those tests may pass (and must keep passing at `verify`). Only the tests added in this task must fail on the stubs. AC numbers start at 1 in every task, because the red gate and `verify` only count the tests added or changed in the current task.
- **Task folder location:** `worktree_dir` in the task's config (default `.tasks`).

### Human-only commands
Run these in your own terminal, in the project's main folder, as `python3 "<plugin folder>/scripts/harness.py" <command>`. The agent prints the exact path when one is needed.
```bash
approve --task <name> --name "Full Name"                        # approve the plan
unlock-tests --task <name> --name "Full Name" --reason "..."    # let tests be revised after the red gate
forget <name>                                                   # delete a finished task's contract and audit trail
uninstall [--dry-run] [--force]                                 # remove every trace of Light Harness from this repo
```

### Uninstall, and repos set up by 1.x

`uninstall` removes the task refs, `.git/light-harness/`, the lines in `.git/info/exclude` and the empty `.tasks/` folder. Task branches are kept. It refuses while tasks are in progress (`--force` drops their folders but keeps their branches), and while a finished task's work is on no branch.

Light Harness 1.3 and earlier copied itself into the work repo: `contract.md`, `scripts/harness.py`, `.harness/`, `.env.example`, a rules block in `CLAUDE.md`, permission rules in `.claude/settings.json` and lines in `.gitignore`. In such a repo, `start-task` asks you to run `uninstall` first. Uninstall removes only the harness's own content and keeps whatever else those shared files hold. It leaves the changes uncommitted for you to review. It keeps 1.x `done/*` tags and any 1.x task still in progress, which carries its own copy of the old harness: finish that task with its copy before you rely on 1.4. Commits that 1.x made stay in your history.

## What enforces what

| Mechanism | What it stops |
|---|---|
| Stage gates (`set-status`) | Skipping stages, building without approval, entering review with failing verification |
| Plan hash | Changing requirements or the plan after approval. Any edit to contract §1–§8 voids the approval and red gate |
| Git-anchored audit trail | Task start, approval, baseline, red gate, test unlocks and verdicts are commits on `refs/light-harness/tasks/<name>`, each with a code snapshot. Gates are recomputed from that history, not from a side file. The red gate's snapshot is what locked tests are compared against |
| Guard hook (PreToolUse) | Code edits before Build or during Review, edits to locked tests, hand edits of the Status line and harness-owned contract blocks, edits to the harness's files and to agent permissions. Bash: human-only commands, faked terminals, branch switching, merges, rebases, resets, amends, branch/tag deletion, worktree changes, `rm -rf`, and tampering with the audit trail. Network commands and pushes ask first, and `.env` files are unreadable. It fails closed in a harness repo if Python is missing, and it's inert in other repos |
| Red gate | New tests that pass before implementation, ACs not traced to this task's tests, skipped tests |
| `verify` | Modified, deleted or added protected files; test-runtime sniffing; skip/xfail/only markers; changes outside approved scope; stack config changed after approval; changes to agent permissions. Warns on swallowed exceptions, coverage exclusions, mocks in implementation code, and comparisons against test literals |
| `finish-task` | A branch that differs from the code the reviewer passed |
| Reviewer + verdict gate | Subtle gaming (special-casing example values), unmet ACs and rules, missing risk mitigations, unjustified deviations |

**Limits:** the agent has a shell, so the harness makes bypass costly and visible rather than impossible. For example, the agent could write git objects through a command the guard doesn't recognise. The defences are the guard, gates recomputed from git, and a reviewer that re-runs `verify` and reads the audit trail. For high-stakes tasks, run the review in a fresh session and glance at `light-harness log <name>`.

## Customise per task

- **Stack:** during planning, set `test_cmd`, `junit_arg` / `junit_path`, `test_globs` and `protected_globs` in `.tasks/<name>/.light-harness/config.json`. The config is frozen once the plan is approved, and the next task in the repo starts from it. Example for Jest: `"test_cmd": ["npx","jest","--ci","--reporters=default","--reporters=jest-junit"]`, `"junit_arg": ""`, `"junit_path": ".light-harness/junit.xml"`, with `JEST_JUNIT_OUTPUT_FILE=.light-harness/junit.xml` set in the environment.

## Repository layout

```
.claude-plugin/marketplace.json      marketplace (this repo is installable directly)
plugins/light-harness/
  .claude-plugin/plugin.json
  skills/task-{init,plan,implement,review}/SKILL.md    stage commands
  agents/{explorer,test-author,reviewer}.md            subagents
  hooks/hooks.json         SessionStart → scripts/session.sh, PreToolUse → scripts/guard.sh
  bin/light-harness        the command line (a Python launcher for scripts/harness.py)
  rules.md                 working rules added to sessions in repos with tasks
  scripts/harness.py       gate engine
  scripts/hook_guard.py    guard rules (file edits, reads, Bash)
  scripts/guard.sh         cross-platform Python launcher for the guard
  scripts/session.sh       puts bin/ on PATH and adds rules.md to the session
  template/                contract and default stack config for new tasks
tests/selftest.sh          end-to-end gate test (also run in CI)
```

## Contributing

1. Change things under `plugins/light-harness/`.
2. Run `bash tests/selftest.sh` (Linux or WSL; it needs `script` from util-linux). It drives toy tasks through every stage, including deliberate gaming attempts that must be caught. It also checks that the repo, the main checkout and the task branches end up free of any harness residue.
3. Run `claude plugin validate --strict .`
4. Bump `version` in `plugins/light-harness/.claude-plugin/plugin.json` and `__version__` in `scripts/harness.py`, so users get the update via `/plugin update`.

CI runs the self-test on Python 3.9 and 3.12, validates the manifests, and checks the hook launcher and a task start on Windows.

## License
MIT
