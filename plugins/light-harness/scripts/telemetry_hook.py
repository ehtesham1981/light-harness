#!/usr/bin/env python3
"""SessionStart / SessionEnd / SubagentStart / SubagentStop hook: records session and subagent
events in the telemetry log (see telemetry.py), for agent KPIs such as how often and how long each
subagent runs per task. Prints nothing (SessionStart output would reach the agent's context) and
always exits 0: telemetry never gets in the way of a session.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main():
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return
    import harness as H  # after reading stdin: importing runs git in the cwd

    ev = data.get("hook_event_name", "")
    cwd = Path(data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    H.set_root(cwd)
    common = H.quiet_common()
    if not common or not (common / H.STATE).is_dir():
        return
    root = H.task_root_for(cwd)
    task = None
    if root:
        H.set_root(root)
        task = H.quiet_slug()
    base = dict(cwd=common, task=task, session=data.get("session_id"))
    if ev in ("SubagentStart", "SubagentStop"):
        H.telemetry.emit(common, "agent", actor="agent", phase="start" if ev == "SubagentStart" else "stop",
                         agent_type=data.get("agent_type"), agent_id=data.get("agent_id"), **base)
    elif ev == "SessionStart":
        H.telemetry.emit(common, "session", phase="start", source=data.get("source"), **base)
    elif ev == "SessionEnd":
        H.telemetry.emit(common, "session", phase="end", reason=data.get("reason"), **base)


if __name__ == "__main__":
    try:
        main()
    except (Exception, SystemExit):
        pass
    sys.exit(0)
