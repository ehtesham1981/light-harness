"""Suite-run figures in `light-harness metrics` (issue #20): telemetry.suite_figures, _suite_seconds,
kpis() and render(). AC-1, AC-3, AC-7, AC-8; BR-2, BR-3, BR-8, BR-9; EX-1, EX-4, EX-5, EX-6."""
from __future__ import annotations

import datetime as dt
import math
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "plugins" / "light-harness" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import telemetry  # noqa: E402

T0 = 1_760_000_000.0
NOW = T0 + 86400.0
KEYS = ["suite_runs", "cache_hits", "cache_misses", "cache_hit_rate", "suite_minutes"]
ROW = "  " + f"{'Suite runs':<22} "
HEADER = f"{'Suite runs':<24} {'Runs':>6} {'Hit rate':>9} {'Minutes':>9}"


def _ts(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def ev(t, event="suite", task="t1", session="s1", **attrs):
    e = {"v": 1, "ts": _ts(T0 + t), "event": event, "harness": "1.9.0", "repo": "r", "task": task,
         "session": session, "actor": "agent", "_t": T0 + t}
    e.update({k: v for k, v in attrs.items() if v is not None})
    return e


def suite(t, gate, duration_s, result="pass", cache=None, task="t1", key=None, **extra):
    attrs = {"gate": gate, "scope": "full", "result": result, "env_failures": 0, "cache": cache, "key": key}
    attrs.update(extra)
    if duration_s is not ...:  # ... = no duration_s field at all
        attrs["duration_s"] = duration_s
    return ev(t, "suite", task=task, **attrs)


def figures(runs, hits, misses, rate, minutes):
    return {"suite_runs": runs, "cache_hits": hits, "cache_misses": misses, "cache_hit_rate": rate,
            "suite_minutes": minutes}


def kpis(task_events: dict, extra_events=()):
    trails = {slug: [(int(T0), f"harness: task-start {slug}")] for slug in task_events}
    events = [e for evs in task_events.values() for e in evs] + list(extra_events)
    events.sort(key=lambda e: e["_t"])
    return telemetry.kpis(trails, set(), events, now=NOW)


EX1 = [suite(100, "redgate", 60, "fail"),
       suite(300, "verify", 90, "pass", "miss", key="k1"),
       suite(900, "verify", 0, "pass", "hit", key="k1")]
# a second task with different numbers, so constants can't pass: 2 runs, 3 hits / 5 decisions, 1 minute
OTHER = [suite(100, "verify", 30, "pass", "miss", task="t2", key="a"),
         suite(200, "verify", 0, "pass", "hit", task="t2", key="a"),
         suite(300, "verify", 30, "fail", "miss", task="t2", key="b"),
         suite(400, "review", 0, "pass", "hit", task="t2", key="b"),
         suite(500, "verify", 0, "pass", "hit", task="t2", key="b")]


# --- AC-1 / BR-3 ------------------------------------------------------------------------------------
def test_ac1_ex1_figures():
    """AC-1 EX-1 BR-3 BR-2: redgate fail 60 s, verify miss 90 s, verify hit -> 2 runs, 50%, 2.5 min."""
    assert telemetry.suite_figures(EX1) == figures(2, 1, 1, 0.5, 2.5)
    assert list(telemetry.suite_figures(EX1)) == KEYS
    assert telemetry.suite_figures([dict(e, task="t2") for e in OTHER]) == figures(2, 3, 2, 0.6, 1.0)


def test_ac1_kpis_per_task_and_pooled_summary():
    """AC-1 BR-3: kpis() appends the five figures per task (after the existing keys) and totals them in
    the summary, the hit rate pooled over all decisions (4/7, not the mean of 50% and 60%)."""
    data = kpis({"t1": [ev(50, "command", command="check", ok=True)] + EX1, "t2": OTHER})
    t1, t2, s = data["tasks"]["t1"], data["tasks"]["t2"], data["summary"]
    assert {k: t1.get(k) for k in KEYS} == figures(2, 1, 1, 0.5, 2.5)
    assert {k: t2.get(k) for k in KEYS} == figures(2, 3, 2, 0.6, 1.0)
    assert list(t1)[-5:] == KEYS and list(t2)[-5:] == KEYS
    assert list(t1)[:-5] == ["status", "stage", "started", "lead_time_s", "age_s", "review_rounds",
                             "same_class_reopened", "first_pass", "approvals", "unlocks", "red_gate_fails",
                             "verify_fails", "gate_fails", "approval_wait_s", "time_in_stage_s", "time_split",
                             "guard_blocks", "guard_asks", "subagents", "fail_kinds", "gates", "sessions", "events"]
    assert list(s)[-5:] == KEYS
    assert {k: s[k] for k in KEYS if k != "cache_hit_rate"} == {"suite_runs": 4, "cache_hits": 4,
                                                               "cache_misses": 3, "suite_minutes": 3.5}
    assert s["cache_hit_rate"] == pytest.approx(4 / 7)
    # suite events are events like any other (Q17): t1 has the check command plus its 3 suite records
    assert t1["events"] == 4 and t1["gates"] == {"check": {"ok": 1, "fail": 0}}


def test_ac1_render_task_row_and_summary():
    """AC-1 BR-3 (Q18): task view row after "Gates (ok/refused)", summary line after "per task:", and a
    `Suite runs` table with one row per task."""
    data = kpis({"t1": EX1, "t2": OTHER})
    view = telemetry.render(data, "t1").split("\n")
    gates = next(i for i, l in enumerate(view) if l.startswith("  Gates (ok/refused)"))
    assert view[gates + 1] == ROW + "2 · cache hit rate 50% · suite minutes 2.5"
    assert telemetry.render(data, "t2").split("\n").count(ROW + "2 · cache hit rate 60% · suite minutes 1") == 1
    repo = telemetry.render(data).split("\n")
    per_task = next(i for i, l in enumerate(repo) if l.startswith("  per task:"))
    assert repo[per_task + 1] == "  suite runs 4 · cache hit rate 57% · suite minutes 3.5"
    h = repo.index(HEADER)
    assert [r.split() for r in repo[h + 1:h + 3]] == [["t1", "2", "50%", "2.5"], ["t2", "2", "60%", "1"]]
    assert repo[h + 3] == "" and repo[h + 4].startswith("Engineers")


def test_ac1_hit_rate_counts_review_gate():
    """AC-1 BR-3 (Q10): hit/miss records with gate `review` are decisions too; a review bypass isn't."""
    evs = [suite(10, "verify", 60, "pass", "miss"), suite(20, "review", 0, "pass", "hit"),
           suite(30, "review", 30, "pass", "miss"), suite(40, "review", 30, "pass", "bypass")]
    assert telemetry.suite_figures(evs) == {"suite_runs": 3, "cache_hits": 1, "cache_misses": 2,
                                            "cache_hit_rate": pytest.approx(1 / 3), "suite_minutes": 2.0}


def test_ac1_no_decisions_dash():
    """AC-1 BR-3 EX-4: redgate and baseline only -> no decisions, hit rate None / `—` (U+2014)."""
    evs = [suite(10, "baseline", 120, "pass"), suite(20, "redgate", 60, "fail")]
    assert telemetry.suite_figures(evs) == figures(2, 0, 0, None, 3.0)
    view = telemetry.render(kpis({"t1": evs}), "t1").split("\n")
    assert ROW + "2 · cache hit rate — · suite minutes 3" in view


def test_ac1_ex4_baseline_not_in_denominator():
    """AC-1 EX-4: baseline 120 s -> 1 run, 2 min, not in the hit-rate denominator."""
    assert telemetry.suite_figures([suite(10, "baseline", 120, "pass")]) == figures(1, 0, 0, None, 2.0)
    mixed = [suite(10, "baseline", 120, "pass"), suite(20, "verify", 0, "pass", "hit")]
    assert telemetry.suite_figures(mixed) == figures(1, 1, 0, 1.0, 2.0)


@pytest.mark.parametrize("seconds,shown", [(45, "0.75"), (150, "2.5"), (60, "1"), (0, "0"), (20, "0.33"),
                                           (3601, "60.02")])
def test_ac1_minutes_format(seconds, shown):
    """AC-1 (Q18): minutes print with up to 2 decimals, trailing zeros stripped."""
    data = kpis({"t1": [suite(10, "redgate", seconds, "fail")]})
    assert ROW + f"1 · cache hit rate — · suite minutes {shown}" in telemetry.render(data, "t1").split("\n")


def test_ac1_only_suite_events_counted():
    """AC-1 BR-3: only `suite` events count; a command event carrying the same fields doesn't."""
    evs = [ev(5, "command", command="verify", ok=True, cache="miss", duration_s=500, gate="verify"),
           suite(10, "verify", 120, "pass", "miss")]
    assert telemetry.suite_figures(evs) == figures(1, 0, 1, 0.0, 2.0)


def test_ac1_ignores_unknown_task():
    """AC-1 BR-3: suite records of a task with no trail are ignored, like other events."""
    data = kpis({"t1": EX1}, [suite(5, "verify", 600, "pass", "miss", task="ghost")])
    assert "ghost" not in data["tasks"]
    assert {k: data["summary"][k] for k in KEYS} == figures(2, 1, 1, 0.5, 2.5)


# --- AC-3 / BR-2 ------------------------------------------------------------------------------------
def test_ac3_hit_adds_no_run():
    """AC-3 BR-2 EX-1: a hit (duration 0, or even a bogus 99) adds no run and no minutes, but is a decision."""
    evs = [suite(10, "verify", 0, "pass", "hit"), suite(20, "verify", 99, "pass", "hit"),
           suite(30, "verify", 60, "pass", "miss")]
    assert telemetry.suite_figures(evs) == {"suite_runs": 1, "cache_hits": 2, "cache_misses": 1,
                                            "cache_hit_rate": pytest.approx(2 / 3), "suite_minutes": 1.0}
    assert telemetry.suite_figures(evs[:2]) == figures(0, 2, 0, 1.0, 0)


# --- AC-7 / BR-9 ------------------------------------------------------------------------------------
def test_ac7_ex5_bypass():
    """AC-7 EX-5 BR-9: miss 60 s, hit, --fresh bypass 30 s -> 2 runs, 50%, 1.5 min."""
    evs = [suite(10, "verify", 60, "pass", "miss"), suite(20, "verify", 0, "pass", "hit"),
           suite(30, "verify", 30, "pass", "bypass")]
    assert telemetry.suite_figures(evs) == figures(2, 1, 1, 0.5, 1.5)
    view = telemetry.render(kpis({"t1": evs}), "t1").split("\n")
    assert ROW + "2 · cache hit rate 50% · suite minutes 1.5" in view


def test_ac7_bypass_only():
    """AC-7 BR-9: bypass runs only -> runs and minutes count, no decisions (rate None)."""
    evs = [suite(10, "review", 45, "pass", "bypass"), suite(20, "verify", 75, "fail", "bypass")]
    assert telemetry.suite_figures(evs) == figures(2, 0, 0, None, 2.0)


def test_ac7_unknown_cache_counts_as_run_not_decision():
    """AC-7 BR-3 (Q14): an unknown cache value counts as a run, never as a decision."""
    evs = [suite(10, "verify", 60, "pass", "weird"), suite(20, "verify", 30, "pass", 7)]
    assert telemetry.suite_figures(evs) == figures(2, 0, 0, None, 1.5)


# --- AC-8 / BR-8 ------------------------------------------------------------------------------------
def test_ac8_ex6_bad_durations():
    """AC-8 EX-6 BR-8: durations 60, -30 and "abc" -> 3 runs, 1.0 minute (not 0.5, not 2.0); no error."""
    evs = [suite(10, "redgate", 60, "fail"), suite(20, "verify", -30, "pass", "miss"),
           suite(30, "verify", "abc", "fail", "miss")]
    assert telemetry.suite_figures(evs) == figures(3, 0, 2, 0.0, 1.0)
    view = telemetry.render(kpis({"t1": evs}), "t1").split("\n")
    assert ROW + "3 · cache hit rate 0% · suite minutes 1" in view


@pytest.mark.parametrize("value,expected", [
    (60, 60), (0, 0), (1.5, 1.5), (90.25, 90.25), (10 ** 6, 10 ** 6),
    (-30, 0), (-0.001, 0), ("abc", 0), ("60", 0), (True, 0), (False, 0), (None, 0),
    (math.nan, 0), (math.inf, 0), (-math.inf, 0), ([], 0), ({}, 0), ([60], 0),
])
def test_ac8_suite_seconds_table(value, expected):
    """AC-8 BR-8 (Q16): only a finite number >= 0 (not a bool) counts; everything else is 0."""
    got = telemetry._suite_seconds(value)
    assert got == expected and not isinstance(got, bool)
    assert isinstance(got, (int, float)) and math.isfinite(got)


def test_ac8_missing_and_odd_durations_still_count_as_runs():
    """AC-8 BR-8: missing, null, NaN, inf, bool and string durations add 0 minutes but still count as runs."""
    evs = [suite(10, "redgate", ..., "fail"), suite(20, "baseline", None, "pass"),
           suite(30, "verify", math.nan, "pass", "miss"), suite(40, "verify", math.inf, "pass", "bypass"),
           suite(50, "redgate", True, "fail"), suite(60, "redgate", "60", "fail"),
           suite(70, "verify", 120, "pass", "miss")]
    assert telemetry.suite_figures(evs) == figures(7, 0, 2, 0.0, 2.0)
    data = kpis({"t1": evs})
    assert ROW + "7 · cache hit rate 0% · suite minutes 2" in telemetry.render(data, "t1").split("\n")
    assert "  suite runs 7 · cache hit rate 0% · suite minutes 2" in telemetry.render(data).split("\n")
