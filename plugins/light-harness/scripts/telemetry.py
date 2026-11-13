#!/usr/bin/env python3
# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""Light Harness telemetry: a local event log from which task, agent and engineer KPIs are derived.

Where events go
  * Always (unless disabled): one JSON line per event in
    <git common dir>/light-harness/telemetry/events.jsonl. That is inside .git, so it never reaches
    the work tree, a commit, a branch or a push. `uninstall` deletes it with the rest of the
    harness state, and `forget <task>` drops that task's events.
  * Optionally: an OpenTelemetry collector. Set LIGHT_HARNESS_OTLP_ENDPOINT (or
    `git config lightharness.otlpEndpoint`) to its OTLP/HTTP base URL, e.g. http://localhost:4318,
    and events are also sent as OTLP log records (best effort, 2 s timeout). Extra headers come
    from LIGHT_HARNESS_OTLP_HEADERS / lightharness.otlpHeaders ("k=v,k2=v2"). Events carry
    `session.id`, the same attribute Claude Code's own telemetry uses, so tokens and cost join
    onto tasks and stages.
  * Off: LIGHT_HARNESS_TELEMETRY=0, or `git config lightharness.telemetry false`.

What an event holds
  v, ts (UTC), event, harness (version), repo (folder name), task, session (Claude Code session
  id when known), actor (agent | human), plus event attributes. No code, contract text, test
  output or file contents.

Engineer identity is opt-in
  An engineer is attributed only after they run `telemetry opt-in --as <nickname>` themselves
  (a human-only command), and only by that nickname, which can be anything they like. Without it,
  events carry no engineer at all, and engineer KPIs exist only in aggregate. The git user name
  and email, and the name given to `approve` (which the audit trail keeps), are never used.

Telemetry never changes a gate's outcome: every failure in here is swallowed.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

VERSION = "?"           # set by harness.py
STATE = "light-harness"
UTF8 = "utf-8"
NICK_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}")
OFF = {"0", "false", "off", "no"}
ENGINEER_KEY = "lightharness.engineer"


# --- settings -----------------------------------------------------------------------------------
@lru_cache(maxsize=None)
def git_config(key: str, cwd: str, scope: str = "") -> str:
    args = ["git", "config"] + ([f"--{scope}"] if scope else []) + ["--get", key]
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, encoding=UTF8, errors="replace")
    except (OSError, ValueError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def enabled(cwd) -> bool:
    if os.environ.get("LIGHT_HARNESS_TELEMETRY", "").strip().lower() in OFF:
        return False
    return git_config("lightharness.telemetry", str(cwd)).lower() not in OFF


def engineer(cwd):
    """The opted-in engineer's nickname, or None (not opted in)."""
    nick = git_config(ENGINEER_KEY, str(cwd))
    return nick if NICK_RE.fullmatch(nick) else None


def engineer_scope(cwd) -> str:
    if git_config(ENGINEER_KEY, str(cwd), "local"):
        return "this repo"
    return "all your repos" if git_config(ENGINEER_KEY, str(cwd), "global") else ""


def set_engineer(cwd, nick, repo_only: bool):
    """Opt in (nick) or out (None). Returns an error message, or ''."""
    scope = "--local" if repo_only else "--global"
    if nick is None:
        args = ["git", "config", scope, "--unset-all", ENGINEER_KEY]
    else:
        if not NICK_RE.fullmatch(nick):
            return ("a nickname is 1-40 characters: letters, digits, spaces, '.', '_' or '-', "
                    "starting with a letter or digit")
        args = ["git", "config", scope, ENGINEER_KEY, nick]
    r = subprocess.run(args, cwd=str(cwd), capture_output=True, encoding=UTF8, errors="replace")
    git_config.cache_clear()
    if r.returncode not in (0, 5):  # 5: nothing to unset
        return r.stderr.strip() or f"git config exited {r.returncode}"
    return ""


GAP_CAP_DEFAULT = 7200  # 2 h


def gap_cap(cwd) -> float:
    """How much of a gap between turns counts as human response time (the rest is idle), in
    seconds: LIGHT_HARNESS_HUMAN_GAP_CAP or `git config lightharness.humanGapCap`, e.g. 2h, 90m,
    600s or 600. Default 2 h."""
    raw = (os.environ.get("LIGHT_HARNESS_HUMAN_GAP_CAP") or git_config("lightharness.humanGapCap", str(cwd))).strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smh]?)", raw.lower())
    if not m:
        return GAP_CAP_DEFAULT
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def otlp_endpoint(cwd) -> str:
    url = (os.environ.get("LIGHT_HARNESS_OTLP_ENDPOINT") or git_config("lightharness.otlpEndpoint", str(cwd))).strip()
    if not re.match(r"https?://", url):
        return ""
    url = url.rstrip("/")
    return url if url.endswith("/v1/logs") else url + "/v1/logs"


def log_path(common: Path) -> Path:
    return Path(common) / STATE / "telemetry" / "events.jsonl"


# --- emitting -----------------------------------------------------------------------------------
def iso(epoch: float) -> str:
    """UTC ISO-8601 with milliseconds, the format of every event's `ts`."""
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def emit(common, event: str, *, cwd, task=None, session=None, actor=None, background=False, **attrs):
    """Append one event (and export it if an OTLP endpoint is set). Never raises, never exits.
    background=True (hooks) hands the export to a detached process, so a slow collector never
    delays a turn."""
    try:
        common = Path(common)
        if not (common / STATE).is_dir() or not enabled(cwd):
            return  # only in repos where Light Harness runs tasks
        rec = {"v": 1, "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
               "event": event, "harness": VERSION,
               "repo": common.parent.name if common.name == ".git" else common.name,
               "task": task, "session": session or os.environ.get("LIGHT_HARNESS_SESSION_ID") or None,
               "actor": actor or ("agent" if os.environ.get("CLAUDECODE") else "human")}
        nick = engineer(cwd)
        if nick:
            rec["engineer"] = nick
        rec.update(attrs)
        rec = {k: v for k, v in rec.items() if v is not None and v != {} and v != []}
        p = log_path(common)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "ab") as f:  # one write per line: appends from parallel sessions don't interleave
            f.write((json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n").encode(UTF8))
        _export(rec, cwd, background)
    except (Exception, SystemExit):
        pass


def _otlp_value(v):
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)}


def otlp_body(rec: dict) -> dict:
    keys = {"session": "session.id", "event": "event.name"}
    attrs = [{"key": keys.get(k, f"light_harness.{k}"),
              "value": _otlp_value(f"light_harness.{v}" if k == "event" else v)}
             for k, v in rec.items() if k not in ("v", "ts", "harness")]
    when = dt.datetime.fromisoformat(rec["ts"].replace("Z", "+00:00"))
    res = [{"key": "service.name", "value": {"stringValue": "light-harness"}},
           {"key": "service.version", "value": {"stringValue": str(rec.get("harness", "?"))}}]
    return {"resourceLogs": [{"resource": {"attributes": res}, "scopeLogs": [{
        "scope": {"name": "light-harness", "version": str(rec.get("harness", "?"))},
        "logRecords": [{"timeUnixNano": str(int(when.timestamp() * 1e9)), "severityText": "INFO",
                        "body": {"stringValue": rec["event"]}, "attributes": attrs}]}]}]}


def _export(rec: dict, cwd, background: bool = False):
    url = otlp_endpoint(cwd)
    if not url:
        return
    headers = {"Content-Type": "application/json"}
    raw = os.environ.get("LIGHT_HARNESS_OTLP_HEADERS") or git_config("lightharness.otlpHeaders", str(cwd))
    for pair in filter(None, (p.strip() for p in raw.split(","))):
        k, _, v = pair.partition("=")
        if k.strip():
            headers[k.strip()] = v.strip()
    job = {"url": url, "headers": headers, "body": otlp_body(rec)}
    if background:
        try:  # headers may hold a token: they go through stdin, never the command line
            kw = {"stdin": subprocess.PIPE, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
            if os.name == "nt":
                kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            else:
                kw["start_new_session"] = True
            p = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--post"], **kw)
            p.stdin.write(json.dumps(job).encode(UTF8))
            p.stdin.close()
            return
        except Exception:
            pass  # fall back to sending it here
    _post(job)


def _post(job: dict):
    req = urllib.request.Request(job["url"], data=json.dumps(job["body"]).encode(UTF8),
                                 headers=job["headers"], method="POST")
    try:
        urllib.request.urlopen(req, timeout=2).close()
    except Exception:
        pass


# --- reading ------------------------------------------------------------------------------------
def read_events(common) -> list[dict]:
    p = log_path(Path(common))
    out = []
    if not p.is_file():
        return out
    for line in p.read_text(encoding=UTF8, errors="replace").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(e, dict) and e.get("event") and _epoch(e.get("ts")) is not None:
            e["_t"] = _epoch(e["ts"])
            out.append(e)
    out.sort(key=lambda e: e["_t"])
    return out


def drop_task(common, slug: str) -> int:
    """Remove a task's events (for `forget`). Returns how many were removed."""
    p = log_path(Path(common))
    if not p.is_file():
        return 0
    keep, n = [], 0
    for line in p.read_text(encoding=UTF8, errors="replace").splitlines():
        try:
            mine = json.loads(line).get("task") == slug
        except (json.JSONDecodeError, AttributeError):
            mine = False
        n += mine
        if not mine:
            keep.append(line)
    tmp = p.with_suffix(".tmp")
    tmp.write_text("".join(l + "\n" for l in keep), encoding=UTF8)
    os.replace(tmp, p)
    return n


def _epoch(ts):
    try:
        return dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# --- KPIs ---------------------------------------------------------------------------------------
def _median(xs):
    return statistics.median(xs) if xs else None


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _merge(iv) -> list:
    """Sorted, non-overlapping [start, end] intervals covering the same time as `iv`."""
    out = []
    for a, b in sorted((float(a), float(b)) for a, b in iv if b > a):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _minus(a, b) -> list:
    """Merged intervals `a` with the time in merged intervals `b` taken out."""
    out = []
    for s, e in a:
        cur = s
        for bs, be in b:
            if be <= cur or bs >= e:
                continue
            if bs > cur:
                out.append([cur, bs])
            cur = max(cur, be)
            if cur >= e:
                break
        if cur < e:
            out.append([cur, e])
    return out


def _clip(iv, lo, hi) -> list:
    return [[max(a, lo), min(b, hi)] for a, b in iv if min(b, hi) > max(a, lo)]


def _span(iv) -> float:
    return sum(b - a for a, b in iv)


def _interval(e, start_key, len_key):
    t0 = _epoch(e.get(start_key)) if e.get(start_key) else None
    try:
        n = float(e.get(len_key))
    except (TypeError, ValueError):
        return None
    return (t0, t0 + max(0.0, n)) if t0 is not None else None


def time_split(ev: list[dict], start: float, end: float, stage_spans: list) -> dict | None:
    """Split the task's wall-clock time [start, end] into agent, human and idle time, from its
    `turn` and `wait` events (1.6.0 on). None if it has no turn events.
      agent  main-agent turns, minus the time inside them spent waiting on a human
      human  in-turn waits (permission dialogs, AskUserQuestion) and the gaps between a turn
             ending and the next prompt in the same session, up to the human-gap cap
      idle   the rest: no session working on the task, or the part of a gap over the cap
    Sessions running at once are merged, agent time taking precedence over human time, so the
    three always add up to the total."""
    turns = [e for e in ev if e["event"] == "turn"]
    if not turns or start is None or end <= start:
        return None
    by_session, waits_by_session, gaps = defaultdict(list), defaultdict(list), []
    for e in turns:
        if e.get("phase") == "stop":
            iv = _interval(e, "turn_start", "turn_s")
            if iv:
                by_session[e.get("session")].append(iv)
        elif e.get("phase") == "start":
            iv = _interval(e, "gap_start", "response_s")
            if iv:
                gaps.append(iv)
    for e in ev:
        if e["event"] == "wait":
            iv = _interval(e, "wait_start", "wait_s")
            if iv:
                waits_by_session[e.get("session")].append(iv)
    agent = _merge([iv for s in by_session for iv in _minus(_merge(by_session[s]), _merge(waits_by_session[s]))])
    waits = _minus(_merge([iv for s in waits_by_session for iv in waits_by_session[s]]), agent)
    responses = _minus(_minus(_merge(gaps), agent), waits)
    agent, waits, responses = (_clip(x, start, end) for x in (agent, waits, responses))

    def split(lo, hi):
        a, w, r = (_span(_clip(x, lo, hi)) for x in (agent, waits, responses))
        return {"agent_s": a, "human_s": w + r, "idle_s": max(0.0, (hi - lo) - a - w - r)}

    total = end - start
    out = split(start, end)
    out.update(total_s=total, response_s=_span(responses), in_turn_wait_s=_span(waits),
               agent_pct=out["agent_s"] / total if total else None,
               turns=sum(e.get("phase") == "start" for e in turns),
               interrupted=sum(e.get("phase") == "stop" and bool(e.get("interrupted")) for e in turns))
    by_stage = defaultdict(lambda: {"agent_s": 0.0, "human_s": 0.0, "idle_s": 0.0})
    for name, lo, hi in stage_spans:
        lo, hi = max(lo, start), min(hi, end)
        if hi > lo:
            for k, v in split(lo, hi).items():
                by_stage[name][k] += v
    out["by_stage"] = dict(by_stage)
    return out


def class_name(s) -> str:
    """The short name of a review issue's defect class: its Class cell up to the first ':' or ';'
    (the dimensions and examples follow), lowercased. "" for none or a placeholder such as "-"."""
    s = re.split(r"[:;]|\s[—–]\s", str(s or ""), maxsplit=1)[0]
    s = re.sub(r"\s+", " ", s.replace("`", "")).strip(" .-*_").lower()
    return "" if s in ("n/a", "na", "none", "tbd") else s


def same_class_reopened(rounds: list) -> int:
    """How often a defect class came back in a later review round. `rounds` holds the issues table at
    each snapshot (each review handed in, each verdict), oldest first, as dicts with id, class and
    status. Counts a new issue whose class matches an earlier snapshot's, and an issue set back to Open
    after it was Fixed or Resolved. An issue that just stays Open doesn't count."""
    seen, status, count = set(), {}, 0
    for k, rows in enumerate(rounds):
        for r in rows:
            iid, cls = str(r.get("id", "")).upper(), class_name(r.get("class"))
            st = str(r.get("status", "")).strip().lower()
            if iid not in status:
                count += bool(k and cls and cls in seen)
            elif st == "open" and status[iid] in ("fixed", "resolved"):
                count += 1
        for r in rows:
            status[str(r.get("id", "")).upper()] = str(r.get("status", "")).strip().lower()
            if class_name(r.get("class")):
                seen.add(class_name(r.get("class")))
    return count


def kpis(trails: dict, finished: set, events: list[dict], now: float | None = None,
         issue_rounds: dict | None = None) -> dict:
    """KPIs from the audit trails ({task: [(epoch, subject), ...]} oldest first, available for every
    task ever run here) and the event log (richer, from 1.5.0 on). `issue_rounds` ({task: the issues
    table each time a review started and at each verdict, oldest first}) gives the same-class-reopened
    count."""
    now = now or time.time()
    issue_rounds = issue_rounds or {}
    s2t = defaultdict(list)  # session -> [(time, task)] of its events that name a task
    for e in events:
        if e.get("task") and e.get("session"):
            s2t[e["session"]].append((e["_t"], e["task"]))

    def owner(e):
        """The task an event belongs to: its own, else its session's (the task the session last
        named before it, or the first it names if none came before)."""
        if e.get("task"):
            return e["task"]
        tl = s2t.get(e.get("session"))
        if not tl:
            return None
        before = [task for t, task in tl if t <= e["_t"]]
        return before[-1] if before else tl[0][1]

    by_task = defaultdict(list)
    for e in events:
        if owner(e):
            by_task[owner(e)].append(e)

    tasks = {}
    all_waits = []  # (seconds, engineer)
    for slug in sorted(trails):
        tr, ev = trails[slug], by_task.get(slug, [])
        start = next((t for t, s in tr if s.startswith("harness: task-start")), tr[0][0] if tr else None)
        verdicts = [(t, "pass" if s.startswith("harness: verdict pass") else "fail")
                    for t, s in tr if s.startswith("harness: verdict")]
        done_t = verdicts[-1][0] if verdicts and verdicts[-1][1] == "pass" else None
        cmds = [e for e in ev if e["event"] == "command"]
        gates = defaultdict(lambda: {"ok": 0, "fail": 0})
        fail_kinds = Counter()
        for e in cmds:
            key = e.get("command", "?") + (f" {e['target']}" if e.get("target") else "")
            gates[key]["ok" if e.get("ok") else "fail"] += 1
            if not e.get("ok"):
                fail_kinds.update(e.get("fail_kinds") or {})
        stages = [e for e in ev if e["event"] == "stage" and e.get("to")]
        in_stage = defaultdict(float)
        spans = []  # (stage, from, to)
        if stages and start is not None:
            spans.append(("Draft", start, stages[0]["_t"]))
        for a, b in zip(stages, stages[1:]):
            spans.append((a["to"], a["_t"], b["_t"]))
        if stages and stages[-1]["to"] != "Done":
            spans.append((stages[-1]["to"], stages[-1]["_t"], now))
        for name, lo, hi in spans:
            in_stage[name] += max(0.0, hi - lo)
        split = time_split(ev, start, done_t or now, spans)
        waits, plan_t = [], None
        for e in cmds:
            if e.get("command") == "set-status" and e.get("target") == "Plan" and e.get("ok"):
                plan_t = e["_t"]
            elif e.get("command") == "approve" and e.get("ok") and plan_t is not None:
                waits.append(e["_t"] - plan_t)
                all_waits.append((e["_t"] - plan_t, e.get("engineer")))
                plan_t = None
        guard = [e for e in ev if e["event"] == "guard"]
        starts = {e.get("agent_id"): e["_t"] for e in ev if e["event"] == "agent" and e.get("phase") == "start"}
        agents = defaultdict(lambda: {"runs": 0, "seconds": 0.0})
        for e in ev:
            if e["event"] == "agent" and e.get("phase") == "stop":
                a = agents[e.get("agent_type") or "main"]
                a["runs"] += 1
                if isinstance(e.get("run_s"), (int, float)):
                    a["seconds"] += e["run_s"]
                elif e.get("agent_id") in starts:
                    a["seconds"] += e["_t"] - starts[e["agent_id"]]
        tasks[slug] = {
            "status": "finished" if slug in finished else ("done" if done_t else "in progress"),
            "stage": stages[-1]["to"] if stages else None,
            "started": _iso(start),
            "lead_time_s": done_t - start if done_t and start else None,
            "age_s": None if done_t or start is None else now - start,
            "review_rounds": len(verdicts),
            "same_class_reopened": same_class_reopened(issue_rounds.get(slug) or []),
            "first_pass": verdicts[0][1] == "pass" if verdicts else None,
            "approvals": sum(s.startswith("harness: plan-approved") for _, s in tr),
            "unlocks": sum(s.startswith("harness: tests-unlocked") for _, s in tr),
            "red_gate_fails": gates["redgate"]["fail"] if "redgate" in gates else 0,
            "verify_fails": sum(v["fail"] for k, v in gates.items() if k in ("verify", "set-status Review")),
            "gate_fails": sum(v["fail"] for v in gates.values()),
            "approval_wait_s": waits,
            "time_in_stage_s": dict(in_stage),
            "time_split": split,
            "guard_blocks": dict(Counter(e.get("rule", "?") for e in guard if e.get("decision") == "block")),
            "guard_asks": sum(e.get("decision") == "ask" for e in guard),
            "subagents": {k: dict(v) for k, v in agents.items()},
            "fail_kinds": dict(fail_kinds),
            "gates": {k: dict(v) for k, v in gates.items()},
            "sessions": len({e["session"] for e in ev if e.get("session")}),
            "events": len(ev),
        }

    engineers = defaultdict(lambda: {"tasks": set(), "sessions": set(), "approvals": 0, "unlocks": 0,
                                     "prompts": 0, "approval_wait_s": [], "response_s": []})
    responses = []  # every human response between turns (capped), for the summary
    for e in events:
        resp = e.get("response_s") if e["event"] == "turn" and e.get("phase") == "start" else None
        if isinstance(resp, (int, float)):
            responses.append(resp)
        nick = e.get("engineer")
        if not nick:
            continue
        d = engineers[nick]
        if isinstance(resp, (int, float)):
            d["response_s"].append(resp)
        if owner(e):
            d["tasks"].add(owner(e))
        if e.get("session"):
            d["sessions"].add(e["session"])
        if e["event"] == "command" and e.get("ok") and e.get("command") == "approve":
            d["approvals"] += 1
        if e["event"] == "command" and e.get("ok") and e.get("command") == "unlock-tests":
            d["unlocks"] += 1
        if e["event"] == "guard" and e.get("decision") == "ask":
            d["prompts"] += 1
    for w, nick in all_waits:
        if nick:
            engineers[nick]["approval_wait_s"].append(w)
    eng_out = {n: {"tasks": len(d["tasks"]), "sessions": len(d["sessions"]), "approvals": d["approvals"],
                   "median_approval_wait_s": _median(d["approval_wait_s"]), "unlocks": d["unlocks"],
                   "prompts": d["prompts"], "median_response_s": _median(d["response_s"])}
               for n, d in sorted(engineers.items())}

    vals = list(tasks.values())
    splits = [t["time_split"] for t in vals if t["time_split"]]
    split_tot = {k: sum(s[k] for s in splits) for k in ("total_s", "agent_s", "human_s", "idle_s")} if splits else None
    if split_tot:
        split_tot["agent_pct"] = split_tot["agent_s"] / split_tot["total_s"] if split_tot["total_s"] else None
        split_tot["tasks"] = len(splits)
    reviewed = [t for t in vals if t["first_pass"] is not None]
    stage_tot, agent_tot, blocks_tot = defaultdict(float), defaultdict(lambda: {"runs": 0, "seconds": 0.0}), Counter()
    for t in vals:
        for k, v in t["time_in_stage_s"].items():
            stage_tot[k] += v
        for k, v in t["subagents"].items():
            agent_tot[k]["runs"] += v["runs"]
            agent_tot[k]["seconds"] += v["seconds"]
        blocks_tot.update(t["guard_blocks"])
    summary = {
        "tasks": len(vals),
        "done": sum(t["lead_time_s"] is not None for t in vals),
        "median_lead_time_s": _median([t["lead_time_s"] for t in vals if t["lead_time_s"] is not None]),
        "first_pass_yield": _mean([1.0 if t["first_pass"] else 0.0 for t in reviewed]),
        "mean_review_rounds": _mean([t["review_rounds"] for t in reviewed]),
        "same_class_reopened_per_task": _mean([t["same_class_reopened"] for t in vals]),
        "median_approval_wait_s": _median([w for w, _ in all_waits]),
        "time_split": split_tot,
        "median_response_s": _median(responses),
        "red_gate_fails_per_task": _mean([t["red_gate_fails"] for t in vals]),
        "verify_fails_per_task": _mean([t["verify_fails"] for t in vals]),
        "guard_blocks_per_task": _mean([sum(t["guard_blocks"].values()) for t in vals]),
        "unlocks_per_task": _mean([t["unlocks"] for t in vals]),
        "guard_blocks_by_rule": dict(blocks_tot.most_common()),
        "time_in_stage_s": dict(stage_tot),
        "subagents": {k: dict(v) for k, v in agent_tot.items()},
        "events": len(events),
        "since": events[0]["ts"] if events else None,
    }
    return {"summary": summary, "tasks": tasks, "engineers": eng_out}


def _iso(t):
    return dt.datetime.fromtimestamp(t).astimezone().strftime("%Y-%m-%d %H:%M") if t else None


def dur(s) -> str:
    if s is None:
        return "-"
    s = int(round(s))
    if s < 60:
        return f"{s}s"
    m, h, d = s // 60, s // 3600, s // 86400
    if s < 3600:
        return f"{m}m"
    if s < 86400:
        return f"{h}h {m % 60:02d}m"
    return f"{d}d {h % 24}h"


def _pct(x):
    return "-" if x is None else f"{x * 100:.0f}%"


def _num(x):
    return "-" if x is None else (f"{x:.1f}".rstrip("0").rstrip(".") if isinstance(x, float) else str(x))


def _split_rows(x) -> list:
    """The time-split rows of a task's detail view."""
    if not x:
        return [("Time split", "- (no turn data: recorded from 1.6.0 on)")]
    rows = [("Time split", f"total {dur(x['total_s'])} = agent {dur(x['agent_s'])} ({_pct(x['agent_pct'])}) + "
                           f"human {dur(x['human_s'])} + idle {dur(x['idle_s'])}"),
            ("  Human time", f"responses between turns {dur(x['response_s'])} · waits inside turns "
                             f"{dur(x['in_turn_wait_s'])}"),
            ("  Turns", f"{x['turns']}" + (f" ({x['interrupted']} interrupted)" if x["interrupted"] else ""))]
    if x["by_stage"]:
        rows.append(("  By stage", " · ".join(f"{k} {dur(v['agent_s'])}/{dur(v['human_s'])}/{dur(v['idle_s'])}"
                                               for k, v in x["by_stage"].items()) + "  (agent/human/idle)"))
    return rows


def render(data: dict, only: str | None = None) -> str:
    s, out = data["summary"], []
    if only:
        t = data["tasks"][only]
        out.append(f"Task {only}: {t['status']}" + (f", stage {t['stage']}" if t["stage"] else "")
                   + f", started {t['started'] or '-'}")
        rows = [("Lead time", dur(t["lead_time_s"]) if t["lead_time_s"] is not None else f"- (open for {dur(t['age_s'])})"),
                ("Review rounds", t["review_rounds"]), ("Same class reopened", t["same_class_reopened"]),
                ("First-pass review", {True: "yes", False: "no", None: "-"}[t["first_pass"]]),
                ("Plan approvals", t["approvals"]), ("Approval wait", ", ".join(map(dur, t["approval_wait_s"])) or "-"),
                ("Test unlocks", t["unlocks"]), ("Red-gate refusals", t["red_gate_fails"]),
                ("Verify failures", t["verify_fails"]), ("All gate refusals", t["gate_fails"]),
                ("Guard prompts (asks)", t["guard_asks"]),
                ("Guard blocks", ", ".join(f"{k} {v}" for k, v in t["guard_blocks"].items()) or "0"),
                ("Failure kinds", ", ".join(f"{k} {v}" for k, v in t["fail_kinds"].items()) or "-"),
                ("Time in stage", " · ".join(f"{k} {dur(v)}" for k, v in t["time_in_stage_s"].items()) or "-"),
                *_split_rows(t["time_split"]),
                ("Subagents", " · ".join(f"{k} {v['runs']}× {dur(v['seconds'])}" for k, v in t["subagents"].items()) or "-"),
                ("Gates (ok/refused)", " · ".join(f"{k} {v['ok']}/{v['fail']}" for k, v in t["gates"].items()) or "-"),
                ("Sessions / events", f"{t['sessions']} / {t['events']}")]
        out += [f"  {k:<22} {v}" for k, v in rows]
        return "\n".join(out)

    out.append(f"Tasks: {s['tasks']} ({s['done']} done)" + (f" · events since {s['since'][:10]}" if s["since"] else
                                                          " · no telemetry events yet (audit-trail KPIs only)"))
    out.append(f"  median lead time {dur(s['median_lead_time_s'])} · first-pass yield {_pct(s['first_pass_yield'])} · "
               f"review rounds {_num(s['mean_review_rounds'])} · same class reopened "
               f"{_num(s['same_class_reopened_per_task'])} per task · median approval wait {dur(s['median_approval_wait_s'])}")
    out.append(f"  per task: red-gate refusals {_num(s['red_gate_fails_per_task'])} · verify failures "
               f"{_num(s['verify_fails_per_task'])} · guard blocks {_num(s['guard_blocks_per_task'])} · "
               f"test unlocks {_num(s['unlocks_per_task'])}")
    if s["time_in_stage_s"]:
        out.append("  time in stage: " + " · ".join(f"{k} {dur(v)}" for k, v in s["time_in_stage_s"].items()))
    if s["time_split"]:
        x = s["time_split"]
        out.append(f"  time split ({x['tasks']} tasks with turn data): agent {dur(x['agent_s'])} ({_pct(x['agent_pct'])}) · "
                   f"human {dur(x['human_s'])} · idle {dur(x['idle_s'])} · median human response "
                   f"{dur(s['median_response_s'])}")
    if s["subagents"]:
        out.append("  subagents: " + " · ".join(f"{k} {v['runs']}× avg {dur(v['seconds'] / v['runs'] if v['runs'] else 0)}"
                                                for k, v in s["subagents"].items()))
    if s["guard_blocks_by_rule"]:
        out.append("  guard blocks: " + ", ".join(f"{k} {v}" for k, v in s["guard_blocks_by_rule"].items()))
    if data["tasks"]:
        out.append("")
        out.append(f"{'Task':<24} {'Status':<12} {'Lead time':>10} {'Reviews':>8} {'1st pass':>8} "
                   f"{'Appr. wait':>10} {'Red-gate✗':>9} {'Verify✗':>8} {'Blocks':>7} {'Unlocks':>8} {'Reopened':>8}")
        for slug, t in data["tasks"].items():
            lead = dur(t["lead_time_s"]) if t["lead_time_s"] is not None else f"({dur(t['age_s'])})"
            fp = {True: "yes", False: "no", None: "-"}[t["first_pass"]]
            out.append(f"{slug[:24]:<24} {t['status']:<12} {lead:>10} {t['review_rounds']:>8} {fp:>8} "
                       f"{dur(_median(t['approval_wait_s'])):>10} {t['red_gate_fails']:>9} {t['verify_fails']:>8} "
                       f"{sum(t['guard_blocks'].values()):>7} {t['unlocks']:>8} {t['same_class_reopened']:>8}")
        split = {k: t["time_split"] for k, t in data["tasks"].items() if t["time_split"]}
        if split:
            out.append("")
            out.append(f"{'Time split':<24} {'Total':>10} {'Agent':>10} {'Human':>10} {'Idle':>10} {'Agent %':>8} "
                       f"{'Responses':>10} {'Waits':>10}")
            for slug, x in split.items():
                out.append(f"{slug[:24]:<24} {dur(x['total_s']):>10} {dur(x['agent_s']):>10} {dur(x['human_s']):>10} "
                           f"{dur(x['idle_s']):>10} {_pct(x['agent_pct']):>8} {dur(x['response_s']):>10} "
                           f"{dur(x['in_turn_wait_s']):>10}")
    out.append("")
    if data["engineers"]:
        out.append("Engineers (opted in, by nickname):")
        for n, d in data["engineers"].items():
            out.append(f"  {n:<22} tasks {d['tasks']} · sessions {d['sessions']} · approvals {d['approvals']} · "
                       f"median approval wait {dur(d['median_approval_wait_s'])} · unlocks {d['unlocks']} · "
                       f"prompts {d['prompts']} · median response {dur(d['median_response_s'])}")
    else:
        out.append("Engineers: none opted in (engineer KPIs are opt-in: `telemetry opt-in --as <nickname>`).")
    return "\n".join(out)


if __name__ == "__main__" and sys.argv[1:] == ["--post"]:  # the detached exporter (_export)
    try:
        _post(json.loads(sys.stdin.buffer.read().decode(UTF8)))
    except Exception:
        pass
