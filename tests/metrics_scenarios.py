"""Deterministic metrics scenarios for the BR-7 [sweep] (issue #20).

Shared by tests/gen_metrics_golden.py (which runs them through the BASE-commit telemetry.py once and
writes tests/golden/metrics_base.json) and tests/test_metrics_preservation.py (which runs them through
the current telemetry.py and compares). Depends on nothing but the standard library.

Generator (full cross product, no randomness):
  task status   x event family   x suite records mixed in   x task count
  5               7                5                          {0, 1, 2}
Task count 0 has no trails, so its status dimension collapses to one value.
"""
from __future__ import annotations

import datetime as dt
import math

T0 = 1_760_000_000.0          # 2025-10-09 08:53:20 UTC
NOW = T0 + 10 * 86400.0        # fixed "now" for every scenario
SLUGS = ("alpha-task", "beta-task-with-a-really-long-slug")

STATUSES = ("in-progress", "verdict-pass", "verdict-fail-only", "finished", "empty-trail")
FAMILIES = ("none", "commands", "stages-commands", "time-split", "guard", "engineer", "bad-values")
SUITE_MIXES = ("none", "valid", "malformed", "unknown-task", "session-only")
TASK_COUNTS = (0, 1, 2)


def ts(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def fixed_iso(t):
    """Replacement for telemetry._iso (local time zone) so text output is machine-independent."""
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d %H:%M") if t else None


def ev(t: float, event: str, **attrs) -> dict:
    e = {"v": 1, "ts": ts(t), "event": event, "harness": "1.8.1", "repo": "demo", "actor": "agent"}
    e.update({k: v for k, v in attrs.items() if v is not None})
    e["_t"] = t
    return e


def trail_for(slug: str, status: str, t: float) -> list:
    start = (int(t), f"harness: task-start {slug}")
    approved = (int(t + 600), "harness: plan-approved by Approver [plan:abc]")
    if status == "in-progress":
        return [start, approved]
    if status == "verdict-pass":
        return [start, approved, (int(t + 3600), "harness: tests-unlocked by Approver: fix"),
                (int(t + 7200), "harness: verdict pass by Reviewer")]
    if status == "verdict-fail-only":
        return [start, approved, (int(t + 5000), "harness: verdict fail by Reviewer")]
    if status == "finished":
        return [start, approved, (int(t + 4000), "harness: verdict fail by Reviewer"),
                (int(t + 6000), "harness: plan-approved by Approver [plan:def]"),
                (int(t + 9000), "harness: verdict pass by Reviewer")]
    if status == "empty-trail":
        return []
    raise ValueError(status)


def family_events(slug: str, family: str, t: float) -> list:
    s1, s2 = f"{slug}-s1", f"{slug}-s2"
    c = lambda dt_, name, ok, **kw: ev(t + dt_, "command", task=slug, session=s1, command=name, ok=ok, **kw)
    commands = [
        c(60, "set-status", True, target="Plan", ms=1200),
        c(600, "approve", True, wait_s=540.0, actor="human"),
        c(700, "redgate", False, reason="tests PASS", fail_kinds={"Traceability": 1}, ms=5000),
        c(800, "redgate", True, ms=60000, new_tests=4),
        c(900, "verify", False, fail_kinds={"Green gate": 2, "Anti-gaming": 1}, ms=90000),
        c(1000, "set-status", False, target="Review", fail_kinds={"Green gate": 1}, ms=30000),
        c(1100, "verify", True, ms=45000, reused=False),
        c(1150, "verify", True, ms=800, reused=True),
        c(1200, "probe", True, target="drop", ms=10),
        c(1300, "check", True, target="plan", ms=150),
        c(1400, "verdict", True, target="pass", review_round=1, lead_time_s=1400.0, first_pass=True),
    ]
    if family == "none":
        return []
    if family == "commands":
        return commands
    if family == "stages-commands":
        stages = [ev(t + dt_, "stage", task=slug, session=s1, **{"from": a, "to": b})
                  for dt_, a, b in ((30, "Draft", "Initialize"), (60, "Initialize", "Plan"), (650, "Plan", "Build"),
                                    (1050, "Build", "Review"), (1250, "Review", "Rework"), (1270, "Rework", "Build"),
                                    (1350, "Build", "Review"), (1450, "Review", "Done"))]
        return stages + commands
    if family == "time-split":
        return [
            ev(t + 10, "turn", task=slug, session=s1, phase="start", source="human", stage="Draft"),
            ev(t + 200, "turn", task=slug, session=s1, phase="stop", turn_start=ts(t + 10), turn_s=190.0,
               interrupted=False, wait_s=20.0, agent_s=170.0),
            ev(t + 80, "wait", task=slug, session=s1, kind="permission", tool="Bash", wait_start=ts(t + 60),
               wait_s=20.0),
            ev(t + 500, "turn", task=slug, session=s1, phase="start", source="human", gap_start=ts(t + 200),
               gap_s=300.0, response_s=300.0, capped=False, engineer=None),
            ev(t + 520, "agent", task=slug, session=s1, phase="start", agent_id="a1",
               agent_type="light-harness:test-author"),
            ev(t + 900, "agent", task=slug, session=s1, phase="stop", agent_id="a1",
               agent_type="light-harness:test-author", run_s=380.0, run_start=ts(t + 520)),
            ev(t + 950, "turn", task=slug, session=s1, phase="stop", turn_start=ts(t + 500), turn_s=450.0,
               interrupted=True),
            ev(t + 1600, "turn", task=slug, session=s1, phase="start", source="notification",
               gap_start=ts(t + 950), gap_s=650.0, bg_s=600.0),
            ev(t + 1700, "turn", task=slug, session=s1, phase="stop", turn_start=ts(t + 1600), turn_s=100.0,
               interrupted=False),
            ev(t + 1750, "command", task=slug, session=s1, command="verify", ok=True, ms=40000),
            ev(t + 1800, "turn", task=slug, session=s2, phase="start", source="human", gap_start=ts(t + 1000),
               gap_s=800.0, response_s=800.0),
            ev(t + 2000, "turn", task=slug, session=s2, phase="stop", turn_start=ts(t + 1800), turn_s=200.0),
            ev(t + 2100, "agent", session=s2, phase="stop", agent_type="light-harness:reviewer", run_s=50),
        ]
    if family == "guard":
        return [
            ev(t + 100, "guard", task=slug, session=s1, decision="block", rule="locked-test", tool="Edit"),
            ev(t + 110, "guard", task=slug, session=s1, decision="block", rule="locked-test", tool="Write"),
            ev(t + 120, "guard", task=slug, session=s1, decision="block", rule="git-surgery", tool="Bash"),
            ev(t + 130, "guard", task=slug, session=s1, decision="ask", rule="network", tool="Bash"),
            ev(t + 140, "guard", session=s1, decision="block", rule="stage", tool="Edit"),
        ]
    if family == "engineer":
        return [
            c(60, "set-status", True, target="Plan", ms=1200),
            ev(t + 900, "command", task=slug, session=s1, command="approve", ok=True, actor="human",
               engineer="Night Owl"),
            ev(t + 950, "command", task=slug, session=s1, command="unlock-tests", ok=True, actor="human",
               engineer="Night Owl"),
            ev(t + 960, "guard", task=slug, session=s1, decision="ask", rule="network", engineer="Night Owl"),
            ev(t + 980, "turn", task=slug, session=s1, phase="stop", turn_start=ts(t + 970), turn_s=10.0),
            ev(t + 1100, "turn", task=slug, session=s1, phase="start", source="human", gap_start=ts(t + 980),
               gap_s=120.0, response_s=120.0, engineer="Night Owl"),
            ev(t + 1200, "turn", task=slug, session=s1, phase="stop", turn_start=ts(t + 1100), turn_s=100.0),
        ]
    if family == "bad-values":
        return [
            c(100, "verify", False, ms="abc", fail_kinds={"Green gate": 1}),
            c(110, "redgate", True, ms=None),
            c(120, "check", True, ms=-500),
            ev(t + 130, "agent", task=slug, session=s1, phase="stop", agent_type="x", run_s="soon"),
            ev(t + 140, "agent", task=slug, session=s1, phase="stop", agent_type="x", agent_id="zz"),
            ev(t + 150, "turn", task=slug, session=s1, phase="stop", turn_start=ts(t + 140), turn_s="bad"),
            ev(t + 160, "turn", task=slug, session=s1, phase="start", source="human", gap_start="not a time",
               response_s=5),
            ev(t + 170, "wait", task=slug, session=s1, wait_start=ts(t + 165), wait_s=-5),
            ev(t + 180, "stage", task=slug, session=s1, **{"from": "Plan"}),
            ev(t + 190, "guard", task=slug, session=s1, decision="block"),
            ev(t + 200, "mystery", task=slug, session=s1, payload=[1, 2]),
        ]
    raise ValueError(family)


def suite_events(slug: str, mix: str, t: float) -> list:
    s1 = f"{slug}-s1"
    su = lambda dt_, **kw: ev(t + dt_, "suite", task=slug, session=s1, scope="full", env_failures=0, **kw)
    if mix == "none":
        return []
    if mix == "valid":
        return [
            su(640, gate="baseline", duration_s=120.0, result="pass"),
            su(705, gate="redgate", duration_s=60.0, result="fail"),
            su(905, gate="verify", duration_s=90.0, cache="miss", result="fail", key="k1"),
            su(1105, gate="verify", duration_s=45.5, cache="miss", result="pass", key="k2"),
            su(1155, gate="verify", duration_s=0, cache="hit", result="pass", key="k2"),
            su(1160, gate="verify", duration_s=30.0, cache="bypass", result="pass", key="k2"),
            su(1170, gate="verify", duration_s=12.25, cache="miss", result="interrupted", key="k3"),
            su(1180, gate="review", duration_s=0, cache="hit", result="pass", key="k3"),
            su(1190, gate="review", duration_s=75.0, cache="bypass", result="pass", key="k3"),
        ]
    if mix == "malformed":
        return [
            su(300, gate="verify", duration_s=-30, cache="miss", result="pass"),
            su(310, gate="verify", duration_s="abc", cache="miss", result="fail"),
            su(320, gate="redgate", duration_s="60", result="fail"),
            su(330, gate="redgate", duration_s=True, result="fail"),
            su(340, gate="verify", duration_s=math.nan, cache="miss", result="pass"),
            su(345, gate="verify", duration_s=math.inf, cache="bypass", result="pass"),
            su(350, gate="baseline", result="pass"),
            su(360, gate="verify", duration_s=20, cache="weird", result="pass"),
            su(370, gate="nightly", duration_s=10, result="maybe"),
            su(380, gate="verify", duration_s=None, cache=None, result=None),
            su(390, gate="verify", duration_s=[1], cache=3, result="pass"),
        ]
    if mix == "unknown-task":
        return [ev(t + 400, "suite", task="ghost-task", session="ghost-s", gate="verify", scope="full",
                   duration_s=50.0, cache="miss", result="pass", env_failures=0),
                ev(t + 410, "suite", task="ghost-task", session="ghost-s", gate="verify", scope="full",
                   duration_s=0, cache="hit", result="pass", env_failures=0)]
    if mix == "session-only":
        so = f"{slug}-only"
        return [ev(t + 420, "command", task=slug, session=so, command="check", ok=True, ms=100),
                ev(t + 430, "suite", session=so, gate="verify", scope="full", duration_s=30.0, cache="miss",
                   result="pass", env_failures=0),
                ev(t + 440, "suite", session=so, gate="verify", scope="full", duration_s=0, cache="hit",
                   result="pass", env_failures=0)]
    raise ValueError(mix)


def issue_rounds_for(status: str) -> list:
    if status in ("verdict-fail-only", "finished"):
        return [[{"id": "I-1", "class": "parsing: dates", "status": "Open"}],
                [{"id": "I-1", "class": "parsing", "status": "Fixed"},
                 {"id": "I-2", "class": "Parsing; times", "status": "Open"}],
                [{"id": "I-1", "class": "parsing", "status": "Open"},
                 {"id": "I-2", "class": "parsing", "status": "Resolved"}]]
    return []


def scenarios() -> list[dict]:
    """Every scenario as {name, trails, finished, events, issue_rounds, now, task_suite}; built fresh on
    each call. `task_suite` is True when some task in the trails owns a suite record."""
    out = []
    for count in TASK_COUNTS:
        for si, status in enumerate(STATUSES if count else STATUSES[:1]):
            for family in FAMILIES:
                for mix in SUITE_MIXES:
                    trails, finished, events, rounds = {}, set(), [], {}
                    for k in range(max(count, 1)):
                        slug = SLUGS[k]
                        st = STATUSES[(si + k) % len(STATUSES)]
                        t = T0 + k * 3 * 86400.0
                        if count:
                            trails[slug] = trail_for(slug, st, t)
                            if st == "finished":
                                finished.add(slug)
                            rounds[slug] = issue_rounds_for(st)
                        if k == 0 or count:
                            events += family_events(slug, family, t)
                            events += suite_events(slug, mix, t)
                    events.sort(key=lambda e: e["_t"])
                    out.append({
                        "name": f"n{count}|{status if count else '-'}|{family}|{mix}",
                        "trails": trails, "finished": finished, "events": events, "issue_rounds": rounds,
                        "now": NOW, "task_suite": bool(count) and mix in ("valid", "malformed", "session-only"),
                    })
    return out


def run(telemetry_mod, sc: dict) -> dict:
    """kpis() and render() of one scenario through the given telemetry module ({kpis, repo, tasks})."""
    data = telemetry_mod.kpis(sc["trails"], sc["finished"], sc["events"], now=sc["now"],
                              issue_rounds=sc["issue_rounds"])
    return {"kpis": data, "repo": telemetry_mod.render(data),
            "tasks": {slug: telemetry_mod.render(data, slug) for slug in data["tasks"]}}
