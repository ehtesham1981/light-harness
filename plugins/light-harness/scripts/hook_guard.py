#!/usr/bin/env python3
"""PreToolUse hook: keeps agents inside the harness stages, without any settings in the work repo.

Active only in a repo where Light Harness has started a task (it has .git/light-harness/); inert
everywhere else. Exit code 2 blocks the tool call and shows stderr to the agent; a JSON
`permissionDecision` asks the human or skips the prompt.

File edits (Edit, Write, MultiEdit, NotebookEdit):
  1. The task's harness files (.light-harness/) are off limits, except the unowned sections of
     contract.md, and config.json before the plan is approved.
  2. In contract.md, the Status line and harness-owned blocks are never edited by hand.
  3. Tests and protected files are read-only once the red gate has passed.
  4. Implementation files can only change in Build or Rework.
  5. While tasks are in progress, the main checkout isn't edited (work happens in .tasks/<name>/).
Reads: .env files are not read.
Bash: human-only harness commands, history rewrites, branch/worktree surgery, tampering with the
  audit trail or the telemetry settings, and `rm -rf` are blocked; network and push commands ask;
  read-only and harness commands run without a prompt.
Every block and ask is also a `guard` telemetry event (rule id, tool, task, session, agent type).
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H  # noqa: E402

PRE_APPROVAL = ("Draft", "Initialize", "Plan")
OWNED_RE = re.compile(r"harness:(" + "|".join(map(re.escape, H.OWNED_BLOCKS)) + r")\b|^Status:", re.M)


CTX: dict = {}  # this tool call: common dir, task, session, tool, agent type (for telemetry)


def note(decision: str, rule: str):
    c = CTX.get("common")
    if c:
        H.telemetry.emit(c, "guard", cwd=c, task=CTX.get("task"), session=CTX.get("session"), actor="agent",
                         decision=decision, rule=rule, tool=CTX.get("tool"), agent_type=CTX.get("agent_type"))


def block(msg: str, rule: str):
    note("block", rule)
    print(f"[light-harness] Blocked: {msg}", file=sys.stderr)
    sys.exit(2)


def decide(decision: str, reason: str, rule: str = ""):
    if decision != "allow":
        note(decision, rule)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "permissionDecision": decision,
                                             "permissionDecisionReason": f"[light-harness] {reason}"}}))
    sys.exit(0)


def existing(p: Path) -> Path:
    d = p if p.is_dir() else p.parent
    while not d.exists() and d != d.parent:
        d = d.parent
    return d


def git_common(d: Path):
    d = existing(d)
    try:
        r = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=d, capture_output=True,
                           encoding=H.UTF8, errors="replace")
    except OSError:
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    p = Path(r.stdout.strip())
    return (p if p.is_absolute() else d / p).resolve()


def active(d: Path):
    """The repo's git common dir if Light Harness has started a task in it, else None."""
    c = git_common(d)
    return c if c and (c / H.STATE).is_dir() else None


def task_of(root):
    """The task (slug) whose worktree is `root`, for telemetry."""
    if not root:
        return None
    H.set_root(root)
    return H.quiet_slug()


def owned_spans(text: str):
    spans = [m.span() for name in H.OWNED_BLOCKS if (m := H.block_span(text, name))]
    if (m := re.search(r"^Status:.*$", text, re.M)):
        spans.append(m.span())
    return spans


def owned_snapshot(text: str):
    return [H.get_status(text)] + [H.block(name, text) for name in H.OWNED_BLOCKS]


# --- Bash ---------------------------------------------------------------------
GIT = r"\bgit(?:\s+-[cC]\s+\S+|\s+--[\w-]+(?:=\S+)?)*\s+"   # `git`, its global options, then the subcommand
BLOCK_BASH = [  # (pattern, why, only if this also matches, rule id for telemetry)
    (r"\b(approve|unlock-tests|forget|uninstall|opt-in|opt-out)\b",
     "approve, unlock-tests, forget, uninstall and telemetry opt-in/opt-out are human-only. Ask the human to "
     "run it in their own terminal.", r"light-harness|harness\.py", "human-only"),
    (r"\bLIGHT_HARNESS_(OTLP\w*|TELEMETRY|SESSION_ID)=|(^|[\s;&|(])(unset|-u)\s+[^\n;&|]*\bLIGHT_HARNESS_|"
     + GIT + r"config\b[^\n;&|]*\b(?i:lightharness)\.",
     "telemetry settings and the engineer's opt-in belong to the human. Ask them to change it.", None,
     "telemetry-settings"),
    (r"(^|[\s;&|(])(script|unbuffer|winpty|expect)(\s|$)", "faking an interactive terminal is not allowed.", None,
     "fake-terminal"),
    (GIT + r"(rebase|reset|filter-branch|filter-repo|checkout|switch|merge|worktree|update-ref|symbolic-ref|"
     r"replace|commit-tree|mktree|read-tree|gc|prune)\b|" + GIT + r"hash-object\b[^\n;&|]*\s-w\b|"
     + GIT + r"reflog\s+(delete|expire)\b",
     "this git command could switch branches, rewrite history or touch other tasks. Stay on your task's "
     "branch; tasks are started and finished with `light-harness start-task` / `finish-task`.", None, "git-surgery"),
    (GIT + r"commit\b[^\n;&|]*--amend", "amending commits is not allowed.", None, "amend"),
    (GIT + r"push\b[^\n;&|]*(\s--force\S*|\s-f\b|\s\+)", "force-pushing is not allowed.", None, "force-push"),
    (GIT + r"branch\b[^\n;&|]*\s(-d|-D|--delete|-m|-M|--move|-c|-C|--copy|-f|--force)\b",
     "deleting, moving or resetting branches is not allowed.", None, "branch-change"),
    (GIT + r"tag\b[^\n;&|]*\s(-d|--delete|-f|--force)\b", "deleting or moving tags is not allowed.", None,
     "tag-change"),
    (r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r|-r\s+-f|-f\s+-r)", "`rm -rf` is not allowed.", None,
     "rm-rf"),
    (r"\.git[/\\]+light-harness|refs/light-harness", "the task's audit trail and telemetry are maintained by the "
     "harness only. Read them with `light-harness show` / `log` / `metrics`.", None, "audit-trail"),
]
ASK_BASH = r"(^|[\s;&|(])(curl|wget|ssh|scp|psql|mysql)(\s|$)|" + GIT + r"push\b"
LH_SAFE = (r"(status|check|set-status|redgate|verify|verdict|start-task|finish-task|tasks|show|log|metrics|"
           r"telemetry\s+status|version)\b.*")
SAFE_SEGMENT = [
    r"(\S*[/\\])?light-harness\s+" + LH_SAFE,
    r"sh\s+(\"[^\"]*light-harness\"|'[^']*light-harness')\s+" + LH_SAFE,
    r"(python3?|py\s+-3)\s+-m\s+pytest\b.*",
    r"git\s+(status|diff|log|show|add)\b.*",
    r"git\s+commit\s+(-q\s+)?-m\s+(\"[^\"]*\"|'[^']*')",
    r"cd\s+\S+",
]
READ_ONLY = {"cat", "head", "tail", "less", "more", "grep", "rg", "wc", "ls", "diff", "file", "stat"}


def bash_rules(cmd: str):
    for pat, why, needs, rule in BLOCK_BASH:
        if re.search(pat, cmd) and (needs is None or re.search(needs, cmd)):
            block(why, rule)
    for seg in re.split(r"&&|\|\||[;|\n]", cmd):
        if f"{H.LIVE}/" in seg or f"{H.LIVE}\\" in seg:
            words = seg.split()
            if ">" in seg or not words or words[0] not in READ_ONLY:
                block(f"{H.LIVE}/ holds the task's contract and config. Edit contract.md with the Edit tool, "
                      "and change status with `light-harness set-status`.", "harness-files")
    if re.search(ASK_BASH, cmd):
        decide("ask", "network access and pushes need the human's OK during a task.", "network")
    # Skip the prompt only for a plain chain (`a && b`) of known-safe commands
    parts = cmd.strip().split("&&")
    if not re.search(r"[`$<>;|&\n]", cmd.replace("&&", " ")) and all(
            any(re.fullmatch(p, s.strip()) for p in SAFE_SEGMENT) for s in parts):
        decide("allow", "harness or read-only command")
    sys.exit(0)


# --- file edits ---------------------------------------------------------------
def edit_rules(tool: str, ti: dict, p: Path, common: Path):
    if H.is_within(p, common):
        block("files inside .git are maintained by git and the harness.", "git-dir")
    root = H.task_root_for(p)
    if root is None:
        # The main checkout while tasks are in progress: their work belongs in their own folders
        top = H._toplevel(existing(p))
        if not top:
            sys.exit(0)
        H.set_root(top)
        try:
            main, busy = H.main_checkout(), H.tasks_in_progress()
        except SystemExit:
            sys.exit(0)
        if busy and H.same_path(top, main):
            block(f"tasks are in progress ({', '.join(s for s, _ in busy)}); their work happens in their "
                  "own folders (.tasks/<name>/), not in the main checkout.", "main-checkout")
        sys.exit(0)

    CTX["task"] = task_of(root)
    H.set_root(root)
    rel = p.resolve().relative_to(H.ROOT).as_posix()
    current = H.CONTRACT.read_text(encoding=H.UTF8)
    status = H.get_status(current)

    if rel == f"{H.LIVE}/config.json":
        if status in PRE_APPROVAL:
            sys.exit(0)  # stack configuration is part of planning, frozen by approval
        block(f"{rel} is frozen once the plan is approved.", "config-frozen")
    if rel.startswith(f"{H.LIVE}/") and rel != f"{H.LIVE}/contract.md":
        block(f"{rel} is harness machinery. Use `light-harness ...` commands instead of editing it.", "harness-files")
    if H.matches(rel, H.MACHINERY):
        block(f"{rel} controls agent permissions or agents; it can't change during a task.", "machinery")

    cfg = H.load_config()

    if rel == f"{H.LIVE}/contract.md":
        if tool == "Write":
            if owned_snapshot(current) != owned_snapshot(ti.get("content", "")):
                block("this rewrite changes the Status line or a harness-owned block. "
                      "Change status with `light-harness set-status`; leave owned blocks alone.", "contract-owned")
        else:
            edits = ti.get("edits") or [ti]
            for e in edits:
                old, new = e.get("old_string", ""), e.get("new_string", "")
                if OWNED_RE.search(old) or OWNED_RE.search(new):
                    block("edit touches the Status line or a harness marker. Use `light-harness` commands.",
                          "contract-owned")
                pos = current.find(old) if old else -1
                if pos >= 0 and any(s <= pos < t for s, t in owned_spans(current)):
                    block("edit falls inside a harness-owned block (approval/red gate/verify/verdict/status log).",
                          "contract-owned")
        sys.exit(0)

    is_test = H.matches(rel, cfg["test_globs"])
    is_protected = H.matches(rel, cfg["protected_globs"])

    if (is_test or is_protected) and H.redgate_state()[0]:
        block(f"{rel} is locked by the red gate. Make the implementation satisfy the tests; never edit them. "
              "If a test is genuinely wrong, log it in contract §10.3 and ask a human to run "
              "`light-harness unlock-tests`.", "locked-test")

    if is_protected and status in PRE_APPROVAL:
        sys.exit(0)  # e.g. sample data gathered during /task-init

    if status not in ("Build", "Rework"):
        block(f"status is {status}. Code and tests may only change in Build/Rework "
              f"(after the plan is approved and /task-implement starts).", "stage")
    sys.exit(0)


def main():
    try:
        # Claude Code sends UTF-8; decode explicitly so contract.md rewrites compare correctly
        # against the file (read as UTF-8) instead of being misread with the locale's codepage.
        data = json.loads(sys.stdin.buffer.read().decode(H.UTF8))
    except (json.JSONDecodeError, UnicodeDecodeError):
        sys.exit(0)
    tool = data.get("tool_name", "")
    ti = data.get("tool_input", {}) or {}
    cwd = Path(data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    CTX.update(session=data.get("session_id"), tool=tool, agent_type=data.get("agent_type"))

    if tool == "Bash":
        common = active(cwd)
        if common:
            cmd = ti.get("command", "") or ""
            m = re.search(r"--task[\s=]+[\"']?([^\s\"';&|]+)", cmd)
            CTX.update(common=common, task=re.sub(r"[^a-z0-9]+", "-", m.group(1).lower()).strip("-") if m
                       else task_of(H.task_root_for(cwd)))
            bash_rules(cmd)
        sys.exit(0)

    fp = ti.get("file_path") or ti.get("notebook_path")
    if not fp:
        sys.exit(0)
    p = Path(fp)
    if not p.is_absolute():
        p = cwd / p
    common = active(p)
    if not common:
        sys.exit(0)  # not a repo where Light Harness runs tasks (hooks fire everywhere)
    CTX["common"] = common

    if tool == "Read":
        n = p.name
        if n == ".env" or (n.startswith(".env.") and n.split(".")[-1] not in ("example", "sample", "template")):
            block(f"{n} holds secrets and isn't read during a task; ask the human for what you need.", "env-file")
        sys.exit(0)
    edit_rules(tool, ti, p, common)


if __name__ == "__main__":
    main()
