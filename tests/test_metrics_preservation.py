"""AC-4 / BR-7 (issue #20): metrics data written before suite-run records still loads, and every figure
`light-harness metrics` showed before is unchanged. The oracle is tests/golden/metrics_base.json, written
once by tests/gen_metrics_golden.py from the base-commit telemetry.py."""
from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "plugins" / "light-harness" / "scripts"
for p in (str(SCRIPTS), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import telemetry  # noqa: E402
import metrics_scenarios as MS  # noqa: E402

GOLDEN = HERE / "golden" / "metrics_base.json"
NEW_KEYS = ("suite_runs", "cache_hits", "cache_misses", "cache_hit_rate", "suite_minutes")
ZERO = {"suite_runs": 0, "cache_hits": 0, "cache_misses": 0, "cache_hit_rate": None, "suite_minutes": 0}

FIG = r"(\d+) · cache hit rate (\d+%|—) · suite minutes (\d+(?:\.\d{1,2})?)"
TASK_ROW = re.compile(r"^  " + re.escape(f"{'Suite runs':<22} ") + FIG + r"$")
SUMMARY_LINE = re.compile(r"^  suite runs " + FIG + r"$")
TABLE_HEADER = f"{'Suite runs':<24} {'Runs':>6} {'Hit rate':>9} {'Minutes':>9}"
TASK_ROW_ZERO = "  " + f"{'Suite runs':<22} " + "0 · cache hit rate — · suite minutes 0"
SUMMARY_ZERO = "  suite runs 0 · cache hit rate — · suite minutes 0"


def strip_new_lines(text: str) -> tuple[list[str], list[str]]:
    """(the text without the lines BR-7 allows to be added, the removed lines). Allowed: the task view's
    `Suite runs` row, the summary's `suite runs` line, and the `Suite runs` table (header, its rows up to
    the next blank line, and at most one blank line right before the header)."""
    kept, removed, in_table = [], [], False
    for line in text.split("\n"):
        if in_table:
            if line == "":
                in_table = False
                kept.append(line)
            else:
                removed.append(line)
            continue
        if line == TABLE_HEADER:
            if kept and kept[-1] == "":
                removed.append(kept.pop())
            removed.append(line)
            in_table = True
        elif TASK_ROW.match(line) or SUMMARY_LINE.match(line):
            removed.append(line)
        else:
            kept.append(line)
    return kept, removed


def strip_new_keys(data: dict) -> tuple[dict, list[str]]:
    """A deep copy of kpis() output without the five new keys, and the places where any was missing."""
    data = copy.deepcopy(data)
    missing = []
    for where, d in [("summary", data["summary"])] + [(f"tasks.{s}", t) for s, t in data["tasks"].items()]:
        for k in NEW_KEYS:
            if k in d:
                d.pop(k)
            else:
                missing.append(f"{where}.{k}")
    return data, missing


def test_ac4_ex2_old_data(monkeypatch):
    """AC-4 EX-2 BR-7: 1.8.1-shaped events with no suite records load; the new figures are 0 / — / 0,
    every other figure is the base commit's."""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))["scenarios"]
    sc = next(s for s in MS.scenarios() if s["name"] == "n1|in-progress|stages-commands|none")
    monkeypatch.setattr(telemetry, "_iso", MS.fixed_iso)
    got = MS.run(telemetry, sc)
    task = got["kpis"]["tasks"]["alpha-task"]
    assert {k: task.get(k, "<missing>") for k in NEW_KEYS} == ZERO
    assert {k: got["kpis"]["summary"].get(k, "<missing>") for k in NEW_KEYS} == ZERO
    assert list(task)[-5:] == list(NEW_KEYS), "new keys must come after the existing ones"
    assert list(got["kpis"]["summary"])[-5:] == list(NEW_KEYS)
    stripped, _ = strip_new_keys(got["kpis"])
    assert json.dumps(stripped) == json.dumps(golden[sc["name"]]["kpis"])
    view = got["tasks"]["alpha-task"].split("\n")
    assert view.count(TASK_ROW_ZERO) == 1, view
    gates_row = next(i for i, l in enumerate(view) if l.startswith("  Gates (ok/refused)"))
    assert view[gates_row + 1] == TASK_ROW_ZERO
    assert strip_new_lines(got["tasks"]["alpha-task"])[0] == golden[sc["name"]]["tasks"]["alpha-task"].split("\n")
    repo = got["repo"].split("\n")
    per_task = next(i for i, l in enumerate(repo) if l.startswith("  per task:"))
    assert repo[per_task + 1] == SUMMARY_ZERO
    assert strip_new_lines(got["repo"])[0] == golden[sc["name"]]["repo"].split("\n")


def test_ac4_empty_metrics():
    """AC-4 BR-7: empty metrics (no tasks, no events) still load; summary shows 0 / — / 0 and no table."""
    data = telemetry.kpis({}, set(), [], now=MS.NOW)
    assert {k: data["summary"].get(k, "<missing>") for k in NEW_KEYS} == ZERO
    text = telemetry.render(data)
    assert SUMMARY_ZERO in text.split("\n")
    assert TABLE_HEADER not in text


def test_ac4_br7_sweep_matches_base(monkeypatch):
    """AC-4 BR-7 [sweep]: every existing `metrics` figure (kpis JSON, repo view, task views) equals the
    base commit's on the same data, over task status x event family x suite records mixed in x task count
    (385 scenarios, tests/metrics_scenarios.py). Allowed changes: only the new suite-run figures, which
    are 0 / — / 0 for tasks that own no suite records."""
    monkeypatch.setattr(telemetry, "_iso", MS.fixed_iso)
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))["scenarios"]
    scs = MS.scenarios()
    assert sorted(golden) == sorted(s["name"] for s in scs), "golden file out of step with the scenarios"
    diffs = []
    for sc in scs:
        name, base = sc["name"], golden[sc["name"]]
        problems = []
        try:
            got = MS.run(telemetry, sc)
        except Exception as exc:  # a load/render error is itself the finding; recorded, then asserted below
            diffs.append((name, [f"raised {type(exc).__name__}: {exc}"]))
            continue
        stripped, missing = strip_new_keys(got["kpis"])
        if missing:
            problems.append(f"new keys missing: {missing[:4]}")
        if json.dumps(stripped) != json.dumps(base["kpis"]):
            problems.append("existing kpis() figures or key order differ from the base commit")
        kept, removed = strip_new_lines(got["repo"])
        if kept != base["repo"].split("\n"):
            problems.append("repo view: existing lines changed")
        summ = [l for l in removed if SUMMARY_LINE.match(l)]
        if len(summ) != 1:
            problems.append(f"repo view: expected one `suite runs` summary line, got {summ}")
        if sorted(got["tasks"]) != sorted(base["tasks"]):
            problems.append("task views differ in which tasks they show")
        for slug, text in got["tasks"].items():
            kept, removed = strip_new_lines(text)
            if kept != base["tasks"].get(slug, "").split("\n"):
                problems.append(f"task view {slug}: existing lines changed")
            rows = [l for l in removed if TASK_ROW.match(l)]
            if len(rows) != 1:
                problems.append(f"task view {slug}: expected one `Suite runs` row, got {rows}")
            if not sc["task_suite"] and rows != [TASK_ROW_ZERO]:
                problems.append(f"task view {slug}: no suite records, yet the row is {rows}")
        if not sc["task_suite"]:
            for where, d in [("summary", got["kpis"]["summary"])] + list(got["kpis"]["tasks"].items()):
                figs = {k: d.get(k, "<missing>") for k in NEW_KEYS}
                if figs != ZERO:
                    problems.append(f"{where}: no suite records owned, yet figures {figs}")
            if summ != [SUMMARY_ZERO]:
                problems.append(f"repo view: no suite records owned, yet summary {summ}")
        if problems:
            diffs.append((name, problems))
    head = "\n".join(f"  {n}: {'; '.join(p)}" for n, p in diffs[:5])
    assert not diffs, f"{len(diffs)} of {len(scs)} scenarios differ from the base commit; first few:\n{head}"
