#!/usr/bin/env python3
"""PreToolUse hook: blocks file edits that would bypass the harness stages.

Exit code 2 blocks the tool call; stderr is shown to the agent so it can correct course.
Rules:
  1. Harness machinery (.harness/, scripts/harness.py, scripts/hook_guard.py,
     .claude/settings.json) is never edited by an agent.
  2. In contract.md, the Status line and harness-owned blocks are never edited by hand.
  3. Tests and protected files are read-only once the red gate has passed.
  4. Implementation files can only change in Build or Rework (no coding before approval,
     no "fixing" during Review).
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H  # noqa: E402

MACHINERY = H.MACHINERY + [".harness/**"]
PRE_APPROVAL = ("Draft", "Initialize", "Plan")
OWNED_RE = re.compile(r"harness:(" + "|".join(map(re.escape, H.OWNED_BLOCKS)) + r")\b|^Status:", re.M)


def block(msg: str):
    print(f"[light-harness] Blocked: {msg}", file=sys.stderr)
    sys.exit(2)


def owned_spans(text: str):
    spans = [m.span() for name in H.OWNED_BLOCKS if (m := H.block_span(text, name))]
    if (m := re.search(r"^Status:.*$", text, re.M)):
        spans.append(m.span())
    return spans


def owned_snapshot(text: str):
    return [H.get_status(text)] + [H.block(name, text) for name in H.OWNED_BLOCKS]


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)
    tool = data.get("tool_name", "")
    ti = data.get("tool_input", {}) or {}
    fp = ti.get("file_path") or ti.get("notebook_path")
    if not fp:
        sys.exit(0)
    p = Path(fp)
    if not p.is_absolute():
        p = Path(data.get("cwd") or H.ROOT) / p
    try:
        rel = p.resolve().relative_to(H.ROOT).as_posix()
    except ValueError:
        sys.exit(0)  # outside the task repo: left to normal permissions

    # Only act inside a harness task repo (matters when installed as a plugin: hooks fire everywhere)
    if not H.CONTRACT.exists():
        sys.exit(0)
    current = H.CONTRACT.read_text()
    if "<!-- harness:plan -->" not in current:
        sys.exit(0)

    status = H.get_status(current)
    if rel == ".harness/config.json" and status in PRE_APPROVAL:
        sys.exit(0)  # stack configuration is part of planning, frozen by approval
    if H.matches(rel, MACHINERY):
        block(f"{rel} is harness machinery. Use `python3 scripts/harness.py ...` instead of editing it.")

    cfg = H.load_config()

    if rel == "contract.md":
        if tool == "Write":
            if owned_snapshot(current) != owned_snapshot(ti.get("content", "")):
                block("this rewrite changes the Status line or a harness-owned block. "
                      "Change status with `harness.py set-status`; leave owned blocks alone.")
        else:
            edits = ti.get("edits") or [ti]
            for e in edits:
                old, new = e.get("old_string", ""), e.get("new_string", "")
                if OWNED_RE.search(old) or OWNED_RE.search(new):
                    block("edit touches the Status line or a harness marker. Use `harness.py` commands.")
                pos = current.find(old) if old else -1
                if pos >= 0 and any(s <= pos < t for s, t in owned_spans(current)):
                    block("edit falls inside a harness-owned block (approval/red gate/verify/verdict/status log).")
        sys.exit(0)

    is_test = H.matches(rel, cfg["test_globs"])
    is_protected = H.matches(rel, cfg["protected_globs"])

    if (is_test or is_protected) and H.redgate_state()[0]:
        block(f"{rel} is locked by the red gate. Make the implementation satisfy the tests; never edit them. "
              "If a test is genuinely wrong, log it in contract §10.3 and ask a human to run "
              "`harness.py unlock-tests`.")

    if is_protected and status in PRE_APPROVAL:
        sys.exit(0)  # e.g. sample data gathered during /task-init

    if status not in ("Build", "Rework"):
        block(f"status is {status}. Code and tests may only change in Build/Rework "
              f"(after the plan is approved and /task-implement starts).")
    sys.exit(0)


if __name__ == "__main__":
    main()
