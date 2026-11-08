#!/usr/bin/env python3
"""Telemetry hook: records session, subagent, turn and human-wait events in the telemetry log (see
telemetry.py), from which `metrics` splits each task's time into agent, human and idle time.

Hook events handled:
  SessionStart / SessionEnd          `session` events
  SubagentStart / SubagentStop       `agent` events; the stop carries the run time (run_s)
  UserPromptSubmit                   `turn` start; carries the human's response time since the
                                     previous turn ended (gap_s, and response_s capped at the
                                     human-gap cap, see telemetry.gap_cap)
  Stop / StopFailure                 `turn` stop; carries turn_s, wait_s (time inside the turn
                                     spent waiting on a human) and agent_s = turn_s - wait_s
  PermissionRequest                  opens a wait (a permission dialog is showing)
  PostToolUse / PostToolUseFailure   closes waits: a permission wait ends when its tool starts
                                     (the hook's time minus duration_ms), and an AskUserQuestion
                                     wait lasts until the question is answered. `wait` events.

Claude Code doesn't fire Stop when the human interrupts a turn (Esc). Such a turn is closed when
the next prompt or the session end arrives, at the session's last recorded activity, and marked
interrupted. A permission that is denied closes at the agent's next tool or the end of the turn.

Hooks are separate processes, so what a turn needs to remember (when it started, open waits,
subagent starts) lives in <git common dir>/light-harness/telemetry/sessions/<session id>.json,
deleted at SessionEnd. Most of these hooks run async (hooks.json), so they can overlap: each one
takes its timestamp first, then changes the session's state under a per-session lock. The hook
path needs neither git nor the harness: the common dir comes from telemetry.sh, and the task and
stage from the files of the task worktree.

Prints nothing (SessionStart and UserPromptSubmit output would reach the agent's context) and
always exits 0: telemetry never gets in the way of a session.
"""
import time

NOW = time.time()  # first: the moment the hook fired (async hooks start a moment after the event)

import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from contextlib import contextmanager  # noqa: E402
from pathlib import Path  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

STATE = "light-harness"
LIVE = ".light-harness"
QUESTION_TOOL = "AskUserQuestion"
SID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
STALE_S = 7 * 86400  # session files left by sessions that never ended
LOCK_WAIT_S, LOCK_STALE_S = 5.0, 15.0


# --- the session's state file ---------------------------------------------------------------------
def state_path(common, sid: str) -> Path:
    return Path(common) / STATE / "telemetry" / "sessions" / f"{sid}.json"


def load(p):
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
        return st if isinstance(st, dict) else None
    except (OSError, ValueError):
        return None


def save(p, st: dict):
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(st), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


@contextmanager
def locked(p: Path):
    """Hold the session's lock (an exclusively created file) while its state changes. Gives up
    waiting after a few seconds, and breaks a lock left by a hook that died."""
    lock, held = p.with_suffix(".lock"), False
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + LOCK_WAIT_S
        while True:
            try:
                os.close(os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                held = True
                break
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > LOCK_STALE_S:
                        lock.unlink()
                        continue
                except OSError:
                    pass
                if time.time() > deadline:
                    break
                time.sleep(0.02)
    except OSError:
        pass
    try:
        yield
    finally:
        if held:
            try:
                lock.unlink()
            except OSError:
                pass


# --- where the hook fired: common dir, task and stage, from files only --------------------------
def common_dir(cwd: Path):
    env = os.environ.get("LIGHT_HARNESS_GIT_COMMON")
    if env:
        return Path(env)
    try:
        r = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=str(cwd),
                           capture_output=True, encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


def task_and_stage(cwd: Path):
    """(slug, status) of the task worktree around cwd, or (None, None). Same answer as the
    harness's task_root_for + task_slug + get_status, without running git."""
    try:
        p = Path(cwd).resolve()
        for d in (p, *p.parents):
            g = d / ".git"
            if not g.exists():
                continue
            contract = d / LIVE / "contract.md"
            if not (g.is_file() and contract.is_file()):
                return None, None
            m = re.match(r"gitdir:\s*(.+)", g.read_text(encoding="utf-8").strip())
            gitdir = Path(m.group(1)) if m else None
            if gitdir is not None and not gitdir.is_absolute():
                gitdir = (d / gitdir).resolve()
            head = (gitdir / "HEAD").read_text(encoding="utf-8").strip() if gitdir else ""
            ref = head[len("ref: refs/heads/"):] if head.startswith("ref: refs/heads/") else ""
            slug = ref[len("task/"):] if ref.startswith("task/") else None
            s = re.search(r"^Status:\s*(\S+)", contract.read_text(encoding="utf-8"), re.M)
            return slug, (s.group(1) if s else "Draft")
    except (OSError, ValueError):
        pass
    return None, None


def plugin_version() -> str:
    try:
        return json.loads((HERE.parent / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        return "?"


# --- turn accounting --------------------------------------------------------------------------------
def r3(x: float) -> float:
    return round(max(0.0, x), 3)


def close_waits(st: dict, tool, now: float, dur: float, ending: bool = False) -> list:
    """Close the session's open waits. Returns [(kind, tool, start, seconds)], adding each to the
    turn's wait total. A PostToolUse closes its own tool's wait at the moment the tool started,
    and any wait opened before that tool started (a denied permission) at the same moment. At the
    end of a turn (ending=True) every wait still open closes now."""
    start_of_tool = now - dur
    out, keep, matched = [], [], False
    for w in st.get("waits") or []:
        t0, wtool = float(w.get("t", now)), w.get("tool")
        if ending:
            out.append(("question" if wtool == QUESTION_TOOL else "permission", wtool, t0, now - t0))
        elif not matched and wtool == tool:
            matched = True
            end = now if tool == QUESTION_TOOL else start_of_tool  # a question waits until it's answered
            out.append(("question" if tool == QUESTION_TOOL else "permission", tool, t0, end - t0))
        elif t0 < start_of_tool:
            out.append(("permission", wtool, t0, start_of_tool - t0))
        else:
            keep.append(w)
    if not ending and not matched and tool == QUESTION_TOOL:  # no permission dialog came first
        out.append(("question", tool, start_of_tool, dur))
    st["waits"] = keep
    out = [(k, t, s, max(0.0, secs)) for k, t, s, secs in out]
    st["turn_wait"] = float(st.get("turn_wait") or 0.0) + sum(o[3] for o in out)
    return out


def apply(ev: str, st: dict, data: dict, now: float, task, stage, cap, iso) -> list:
    """Apply one hook event to the session state. Returns the events to record, as
    [(event, actor, attrs)]."""
    out = []

    def waits_out(waits):
        for kind, wtool, start, secs in waits:
            out.append(("wait", "human", dict(kind=kind, tool=wtool, stage=st.get("stage"),
                                              wait_start=iso(start), wait_s=r3(secs))))

    def close_turn(end: float, interrupted: bool, failed: bool = False):
        t0 = float(st.pop("turn"))
        turn_s = max(0.0, end - t0)
        wait_s = min(float(st.pop("turn_wait", 0.0) or 0.0), turn_s)
        out.append(("turn", "agent", dict(phase="stop", stage=st.get("stage"), turn_start=iso(t0), turn_s=r3(turn_s),
                                          wait_s=r3(wait_s), agent_s=r3(turn_s - wait_s), interrupted=interrupted,
                                          failed=True if failed else None)))
        st["stop"] = end
        st["waits"] = []

    if ev in ("PostToolUse", "PostToolUseFailure"):
        st["last"] = max(now, float(st.get("last") or 0.0))
        d = data.get("duration_ms")
        dur = d / 1000.0 if isinstance(d, (int, float)) and d >= 0 else 0.0
        tool = data.get("tool_name") or ""
        if st.get("waits") or tool == QUESTION_TOOL:
            waits_out(close_waits(st, tool, now, dur))
    elif ev == "PermissionRequest":
        if st.get("turn") is not None:
            st.setdefault("waits", []).append({"t": now, "tool": data.get("tool_name")})
            st["last"] = max(now, float(st.get("last") or 0.0))
    elif ev in ("SubagentStart", "SubagentStop"):
        agents = st.setdefault("agents", {})
        aid = str(data.get("agent_id") or "")
        run_s = None
        if ev == "SubagentStart":
            agents[aid] = now
        elif aid in agents:
            run_s = r3(now - float(agents.pop(aid)))
        st["last"] = max(now, float(st.get("last") or 0.0))
        out.append(("agent", "agent", dict(phase="start" if ev == "SubagentStart" else "stop",
                                           agent_type=data.get("agent_type"), agent_id=data.get("agent_id"),
                                           run_s=run_s)))
    elif ev == "UserPromptSubmit":
        if st.get("turn") is not None:  # the previous turn was interrupted: no Stop came
            close_turn(max(float(st.get("last") or 0.0), float(st["turn"])), interrupted=True)
        gap = {}
        if st.get("stop") is not None:
            g = now - float(st["stop"])
            gap = dict(gap_start=iso(float(st["stop"])), gap_s=r3(g), response_s=r3(min(g, cap)), capped=g > cap)
        st.update(turn=now, turn_wait=0.0, waits=[], last=now, task=task, stage=stage)
        out.append(("turn", "human", dict(phase="start", stage=stage, **gap)))
    elif ev in ("Stop", "StopFailure"):
        if st.get("turn") is not None:  # else the turn began before the harness was installed
            waits_out(close_waits(st, None, now, 0.0, ending=True))
            close_turn(now, interrupted=False, failed=ev == "StopFailure")
            st["last"] = now
    elif ev == "SessionEnd":
        if st.get("turn") is not None:
            close_turn(max(float(st.get("last") or 0.0), float(st["turn"])), interrupted=True)
        out.append(("session", "agent", dict(phase="end", reason=data.get("reason"))))
    elif ev == "SessionStart":
        out.append(("session", "agent", dict(phase="start", source=data.get("source"))))
    return out


def main():
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return
    ev = data.get("hook_event_name", "")
    sid = str(data.get("session_id") or "")
    cwd = Path(data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    common = common_dir(cwd)
    if not common or not (common / STATE).is_dir():
        return
    p = state_path(common, sid) if SID_RE.fullmatch(sid) else None
    hot = ev in ("PostToolUse", "PostToolUseFailure")
    if hot and (p is None or not p.is_file()):
        return  # every tool call lands here: nothing to do outside a turn
    import telemetry as T  # noqa: E402
    T.VERSION = plugin_version()
    task, stage = (None, None) if hot else task_and_stage(cwd)
    cap = T.gap_cap(common) if ev == "UserPromptSubmit" else 0.0
    if p is None:
        st, out = {}, apply(ev, {}, data, NOW, task, stage, cap, T.iso)
    else:
        with locked(p):
            st = load(p) or {}
            out = apply(ev, st, data, NOW, task, stage, cap, T.iso)
            if ev == "SessionEnd":
                try:
                    p.unlink()
                except OSError:
                    pass
            elif ev != "SessionStart" or st:
                save(p, st)
    for event, actor, attrs in out:
        T.emit(common, event, cwd=common, task=task or st.get("task"), session=sid or None, actor=actor,
               background=True, **attrs)
    if ev == "SessionStart":
        prune(common)


def prune(common: Path):
    d = Path(common) / STATE / "telemetry" / "sessions"
    try:
        for f in d.iterdir():
            if NOW - f.stat().st_mtime > STALE_S:
                f.unlink()
    except OSError:
        pass


if __name__ == "__main__":
    try:
        main()
    except (Exception, SystemExit):
        pass
    sys.exit(0)
