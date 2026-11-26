# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""Load fields in suite-run records and `metrics` (issue #16): AC-8 (BR-12). Records gain `slots`,
`running`, `load_avg` and `wait_s`; waiting for a slot isn't suite time; `metrics` shows the load figures
only when some record carries them (Q-1), so records without them render exactly as before (the existing
tests/test_suite_metrics.py and tests/test_metrics_preservation.py pin that)."""
from __future__ import annotations

import datetime as dt
import os
import re
import sys
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
SCRIPTS = HERE.parent / "plugins" / "light-harness" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import harness as H  # noqa: E402
import lh_toy as T  # noqa: E402
import telemetry  # noqa: E402
from lh_machine import isolate  # noqa: E402

ENVELOPE = {"cwd", "task", "session", "actor", "background"}
T0 = 1_760_000_000.0
NOW = T0 + 86400.0


class Clock:
    def __init__(self, start=1000.0):
        self.now = start

    def monotonic(self):
        return self.now


class TimeProxy(types.ModuleType):
    def __init__(self, **over):
        super().__init__("time")
        self.__dict__.update(over)

    def __getattr__(self, name):
        return getattr(time, name)


@pytest.fixture
def emitted(monkeypatch, tmp_path):
    calls = []

    def fake_emit(common, event, **kw):
        calls.append((event, {k: v for k, v in kw.items() if k not in ENVELOPE and v is not None}))

    monkeypatch.setattr(telemetry, "emit", fake_emit)
    monkeypatch.setattr(H, "quiet_common", lambda: tmp_path)
    monkeypatch.setattr(H, "quiet_slug", lambda: "t1")
    return calls


def _suites(calls):
    return [a for e, a in calls if e == "suite"]


# --- (a) the record ---------------------------------------------------------------------------------
@pytest.mark.parametrize("gate,wait,run_s,slots,running,load", [
    ("redgate", 30.0, 10.0, 3, 2, 1.25),
    ("baseline", 90.5, 4.0, 1, 1, None),
])
def test_ac8_wait_not_suite_time(monkeypatch, emitted, gate, wait, run_s, slots, running, load):
    """AC-8 BR-12: a run that waited `wait` s for a slot and then ran `run_s` s is recorded with duration_s =
    run_s (waiting isn't suite time) and wait_s, slots, running, load_avg as the slot reported them in
    RUN_LOAD (load_avg absent where there is none); metrics counts run_s only."""
    clock = Clock()
    monkeypatch.setattr(H, "time", TimeProxy(monotonic=clock.monotonic))

    def run_tests(cfg):
        assert H.RUN_LOAD.get("gate") == gate and H.RUN_LOAD.get("wait_s") is None  # reset on entry
        H.RUN_LOAD.update(slots=slots, running=running, load_avg=load, wait_s=wait)
        clock.now += wait + run_s
        return 1, "out", {}

    monkeypatch.setattr(H, "run_tests", run_tests)
    H.run_suite({}, gate)
    want = {"gate": gate, "scope": "full", "duration_s": run_s, "result": "fail", "env_failures": 0,
            "slots": slots, "running": running, "wait_s": wait}
    if load is not None:
        want["load_avg"] = load
    assert _suites(emitted) == [want]
    rec = dict(_suites(emitted)[0], event="suite")
    assert telemetry.suite_figures([rec])["suite_minutes"] == run_s / 60


@pytest.fixture
def hroot(monkeypatch, tmp_path):
    env = isolate(monkeypatch, tmp_path / "machine", slots=2)
    repo = T.mini_repo(tmp_path / "repo")
    T.point(monkeypatch, H, repo)
    monkeypatch.setattr(H, "SLOT_POLL", 0.05)
    return SimpleNamespace(tmp=tmp_path, repo=repo, machine=tmp_path / "machine", env=env)


class _Proxy(types.ModuleType):
    def __init__(self, real, drop=(), **over):
        super().__init__(real.__name__)
        self._real, self._drop = real, set(drop)
        self.__dict__.update(over)

    def __getattr__(self, name):
        if name in self._drop:
            raise AttributeError(name)
        return getattr(self._real, name)


@pytest.mark.parametrize("loadavg,expected", [((1.25, 0.5, 0.25), 1.25), ((7.5, 1.0, 1.0), 7.5), (None, None)],
                         ids=["load-1.25", "load-7.5", "no-getloadavg"])
def test_ac8_run_slot_fills_run_load(hroot, monkeypatch, loadavg, expected):
    """AC-8 BR-12 (Q-13): run_slot sets RUN_LOAD: slots = the slot count (2), running = live holders including
    this run (1), load_avg = os.getloadavg()[0] where it exists (None where it doesn't), wait_s ~ 0."""
    if loadavg is None:
        monkeypatch.setattr(H, "os", _Proxy(os, drop=("getloadavg",)))
    else:
        monkeypatch.setattr(H, "os", _Proxy(os, getloadavg=lambda: loadavg))
    H.RUN_LOAD.clear()
    H.RUN_LOAD.update(gate="verify", slots=None, running=None, load_avg=None, wait_s=None)
    with H.run_slot("verify"):
        pass
    got = dict(H.RUN_LOAD)
    assert (got.get("slots"), got.get("running"), got.get("load_avg")) == (2, 1, expected), got
    assert isinstance(got.get("wait_s"), (int, float)) and 0 <= got["wait_s"] < 1, got


def test_ac8_run_slot_measures_wait(hroot):
    """AC-8 BR-12: with the only slot held by a live process for ~0.8 s, run_slot adds the time it waited to
    RUN_LOAD["wait_s"] and counts the live runs when it took its slot (1: only itself, the holder left)."""
    from lh_machine import machine_paths, write_gitconfig
    write_gitconfig(machine_paths(hroot.machine)["gitconfig"], 1)
    holder = T.sleeper(60)
    try:
        d = H.slot_dir()
        d.mkdir(parents=True, exist_ok=True)
        T.write_json(d / "slot-0.json", {"pid": holder.pid, "start": H.proc_start(holder.pid), "repo": "r",
                                         "task": "t", "gate": "verify", "since": time.time()})
        threading.Timer(0.8, lambda: (holder.kill(), holder.wait())).start()
        H.RUN_LOAD.clear()
        H.RUN_LOAD.update(gate="verify", slots=None, running=None, load_avg=None, wait_s=None)
        with H.run_slot("verify"):
            pass
        got = dict(H.RUN_LOAD)
        assert got.get("slots") == 1 and got.get("running") == 1, got
        assert 0.6 <= got.get("wait_s", -1) < 10, got
    finally:
        holder.kill()
        holder.wait()


# --- (b) metrics with load fields -------------------------------------------------------------------
def _ts(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def ev(t, task="t1", **attrs):
    e = {"v": 1, "ts": _ts(T0 + t), "event": "suite", "harness": "1.9.0", "repo": "r", "task": task,
         "session": "s1", "actor": "agent", "_t": T0 + t, "gate": "verify", "scope": "full", "result": "pass",
         "env_failures": 0}
    e.update({k: v for k, v in attrs.items() if v is not None})
    return e


SET_A = [ev(100, duration_s=60, cache="miss", slots=2, running=1, load_avg=2.5, wait_s=30),
         ev(200, duration_s=30, cache="miss", slots=3, running=3, load_avg=4.0, wait_s=90),
         ev(300, duration_s=0, cache="hit"),                                   # reused: no suite ran
         ev(400, duration_s=20, cache="miss", wait_s="x", running=-1, load_avg=float("nan"))]  # ignored values
FIG_A = {"slot_wait_minutes": 2.0, "slots": 3, "mean_running": 2.0, "max_load_avg": 4.0}
SET_B = [ev(100, task="t2", duration_s=10, slots=4, running=2, wait_s=15),
         ev(200, task="t2", duration_s=10, slots=4, running=3, wait_s=45),
         ev(300, task="t2", duration_s=10, slots=1, running=4, wait_s=0)]
FIG_B = {"slot_wait_minutes": 1.0, "slots": 1, "mean_running": 3.0, "max_load_avg": None}


@pytest.mark.parametrize("events,expected", [(SET_A, FIG_A), (SET_B, FIG_B)], ids=["a", "b"])
def test_ac8_load_figures(events, expected):
    """AC-8 BR-12: load_figures over suite-run records (not hits) carrying load fields: total slot wait in
    minutes, the latest slot count, mean runs in progress, max load average; bad values are ignored."""
    assert telemetry.load_figures(events) == expected


def _kpis(task_events: dict):
    trails = {slug: [(int(T0), f"harness: task-start {slug}")] for slug in task_events}
    events = sorted([e for evs in task_events.values() for e in evs], key=lambda e: e["_t"])
    return telemetry.kpis(trails, set(), events, now=NOW)


def test_ac8_kpis_and_render_show_load():
    """AC-8 BR-12 (Q-1): with load fields, `suite_load` is the last key of the task's dict and of the summary;
    the task view has a `Suite load` row right after `Suite runs`, the repo view a `suite load` line right
    after `suite runs`, with the slot wait, slots, runs in progress and max load average."""
    data = _kpis({"t1": SET_A, "t2": SET_B})
    t1, t2, s = data["tasks"]["t1"], data["tasks"]["t2"], data["summary"]
    assert list(t1)[-1] == "suite_load" and t1["suite_load"] == FIG_A
    assert list(t2)[-1] == "suite_load" and t2["suite_load"] == FIG_B
    assert list(s)[-1] == "suite_load" and s["suite_load"] == telemetry.load_figures(SET_A + SET_B)
    rows = telemetry.render(data, "t1").splitlines()
    i = next(n for n, l in enumerate(rows) if l.strip().startswith("Suite runs"))
    row = rows[i + 1]
    assert row.strip().startswith("Suite load"), rows
    assert re.search(r"slot wait 2(\.0+)?\b", row) and re.search(r"slots 3\b", row), row
    assert re.search(r"runs in progress 2(\.0+)?\b", row) and re.search(r"load avg max 4(\.0+)?\b", row), row
    lines = telemetry.render(data).splitlines()
    j = next(n for n, l in enumerate(lines) if l.startswith("  suite runs "))
    assert lines[j + 1].startswith("  suite load "), lines


def test_ac8_records_without_load_fields_render_as_before():
    """AC-8 BR-12 (Q-1): records without load fields (as every record before this change) still load:
    load_figures is None, no `suite_load` key in the task dict or the summary, and no load row or line."""
    old = [ev(100, duration_s=60, cache="miss", key="k"), ev(200, duration_s=0, cache="hit", key="k"),
           ev(300, gate="redgate", duration_s=5, result="fail")]
    assert telemetry.load_figures(old) is None
    assert telemetry.load_figures([]) is None
    data = _kpis({"t1": old})
    assert "suite_load" not in data["tasks"]["t1"] and "suite_load" not in data["summary"]
    text = telemetry.render(data) + "\n" + telemetry.render(data, "t1")
    assert "Suite load" not in text and "suite load" not in text
