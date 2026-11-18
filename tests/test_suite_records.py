"""Suite-run records written by the harness (issue #20): record_suite_run, suite_result, suite_timer,
run_suite, verify_reuse_decision and their call sites. AC-2, AC-3, AC-5, AC-7; BR-1, BR-2, BR-4, BR-5,
BR-6, BR-8, BR-9, BR-10; EX-3, EX-4, EX-5."""
from __future__ import annotations

import ast
import inspect
import json
import sys
import textwrap
import time
import types
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "plugins" / "light-harness" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import harness  # noqa: E402
import telemetry  # noqa: E402

ENVELOPE = {"cwd", "task", "session", "actor", "background"}


class Clock:
    """A fake monotonic clock that only moves when told to."""

    def __init__(self, start=1000.0):
        self.now = start

    def monotonic(self):
        return self.now

    def advance(self, s):
        self.now += s


class TimeProxy(types.ModuleType):
    """Stands in for the `time` module inside harness only: overrides some functions, delegates the rest."""

    def __init__(self, **overrides):
        super().__init__("time")
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(time, name)


@pytest.fixture
def emitted(monkeypatch, tmp_path):
    """Capture every telemetry.emit call as (event, attributes without the envelope and without None)."""
    calls = []

    def fake_emit(common, event, **kw):
        calls.append((event, {k: v for k, v in kw.items() if k not in ENVELOPE and v is not None}))

    monkeypatch.setattr(telemetry, "emit", fake_emit)
    monkeypatch.setattr(harness, "quiet_common", lambda: tmp_path)
    monkeypatch.setattr(harness, "quiet_slug", lambda: "t1")
    return calls


def suites(calls):
    return [a for e, a in calls if e == "suite"]


def fake_run_tests(monkeypatch, rc, seconds=0.0, clock=None, raises=None, out="out text", results=None):
    results = {"tests/test_x.py::test_a": "passed"} if results is None else results
    ret = (rc, out, results)
    seen = []

    def run_tests(cfg):
        seen.append(cfg)
        if clock:
            clock.advance(seconds)
        elif seconds:
            time.sleep(seconds)
        if raises:
            raise raises
        return ret

    monkeypatch.setattr(harness, "run_tests", run_tests)
    return ret, seen


def use_clock(monkeypatch, clock):
    monkeypatch.setattr(harness, "time", TimeProxy(monotonic=clock.monotonic))


SLACK = 0.05  # Windows' monotonic clock ticks every ~16 ms, and sleep() may wake a tick early


def three_decimals(x):
    return abs(x * 1000 - round(x * 1000)) < 1e-6


# --- AC-2: record_suite_run -------------------------------------------------------------------------
def test_ac2_record_suite_run_fields(emitted):
    """AC-2 BR-1 BR-5: one `suite` event with gate, scope full, duration, cache, result, env_failures 0, key."""
    harness.record_suite_run("verify", duration_s=12.345, result="pass", cache="miss", key="a" * 64)
    harness.record_suite_run("redgate", duration_s=3.5, result="fail")
    assert [e for e, _ in emitted] == ["suite", "suite"]
    assert suites(emitted) == [
        {"gate": "verify", "scope": "full", "duration_s": 12.345, "cache": "miss", "result": "pass",
         "env_failures": 0, "key": "a" * 64},
        {"gate": "redgate", "scope": "full", "duration_s": 3.5, "result": "fail", "env_failures": 0},
    ]


def test_ac2_record_suite_run_best_effort(monkeypatch, capsys):
    """AC-2 (Q13, R-2): a failing write never raises out of record_suite_run and prints nothing."""
    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(telemetry, "emit", boom)
    monkeypatch.setattr(harness, "quiet_common", boom)
    assert harness.record_suite_run("verify", duration_s=1.0, result="pass", cache="miss", key="k") is None
    assert capsys.readouterr() == ("", "")


# --- AC-2: suite_result -----------------------------------------------------------------------------
@pytest.mark.parametrize("rc,expected", [(0, "pass"), (1, "fail"), (2, "fail"), (5, "fail"), (127, "fail"),
                                         (-2, "interrupted"), (-9, "interrupted"), (None, None)])
def test_ac2_suite_result(rc, expected):
    """AC-2 BR-6 BR-10 (Q15): exit code 0 pass, negative interrupted, other ints fail, None None."""
    assert harness.suite_result(rc) == expected


# --- AC-2: run_suite --------------------------------------------------------------------------------
@pytest.mark.parametrize("gate,rc,result", [("redgate", 1, "fail"), ("redgate", 0, "pass"),
                                            ("baseline", 0, "pass"), ("baseline", 2, "fail")])
def test_ac2_run_suite_records_pass_fail(monkeypatch, emitted, capsys, gate, rc, result):
    """AC-2 EX-4 BR-1 BR-5 BR-6: run_suite returns run_tests's tuple unchanged and appends exactly one
    record: no cache or key for redgate/baseline, real duration, pass/fail by exit code."""
    ret, seen = fake_run_tests(monkeypatch, rc, seconds=0.15)
    cfg = {"test_cmd": ["x"]}
    got = harness.run_suite(cfg, gate)
    assert got == ret and got[1] is ret[1] and got[2] is ret[2]
    assert seen == [cfg] and seen[0] is cfg
    recs = suites(emitted)
    assert len(recs) == 1 and len(emitted) == 1
    rec = recs[0]
    assert 0.15 - SLACK <= rec["duration_s"] < 10 and three_decimals(rec["duration_s"])
    assert {k: v for k, v in rec.items() if k != "duration_s"} == {
        "gate": gate, "scope": "full", "result": result, "env_failures": 0}
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("gate,seconds,rc,result", [("baseline", 120.0, 0, "pass"), ("redgate", 60.0, 1, "fail"),
                                                    ("redgate", 7.25, 1, "fail")])
def test_ac2_ex4_exact_duration(monkeypatch, emitted, gate, seconds, rc, result):
    """AC-2 EX-4 BR-1: the recorded duration is the monotonic time the suite took (baseline 120 s -> 2 min,
    no cache result; it stays out of the hit rate)."""
    clock = Clock()
    use_clock(monkeypatch, clock)
    fake_run_tests(monkeypatch, rc, seconds=seconds, clock=clock)
    harness.run_suite({}, gate)
    assert suites(emitted) == [{"gate": gate, "scope": "full", "duration_s": seconds, "result": result,
                                "env_failures": 0}]
    fig = telemetry.suite_figures([dict(suites(emitted)[0], event="suite")])
    assert fig == {"suite_runs": 1, "cache_hits": 0, "cache_misses": 0, "cache_hit_rate": None,
                   "suite_minutes": seconds / 60}


def test_ac2_monotonic(monkeypatch, emitted):
    """AC-2 BR-8: durations come from a monotonic clock: a wall clock jumping backwards doesn't matter."""
    wall = iter(range(10 ** 9, 0, -3600))
    monkeypatch.setattr(harness, "time", TimeProxy(time=lambda: float(next(wall))))
    fake_run_tests(monkeypatch, 1, seconds=0.12)
    harness.run_suite({}, "redgate")
    (rec,) = suites(emitted)
    assert 0.12 - SLACK <= rec["duration_s"] < 10


def test_ac2_interrupted(monkeypatch, emitted):
    """AC-2 BR-10: Ctrl-C during the suite records `interrupted` with the time so far, then re-raises."""
    clock = Clock()
    use_clock(monkeypatch, clock)
    fake_run_tests(monkeypatch, None, seconds=7.0, clock=clock, raises=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        harness.run_suite({}, "redgate")
    assert suites(emitted) == [{"gate": "redgate", "scope": "full", "duration_s": 7.0, "result": "interrupted",
                                "env_failures": 0}]


def test_ac2_killed_child_is_interrupted(monkeypatch, emitted):
    """AC-2 BR-10 (Q15): a child killed by a signal (negative exit code) is recorded `interrupted`, and
    run_suite still returns its result unchanged."""
    clock = Clock()
    use_clock(monkeypatch, clock)
    ret, _ = fake_run_tests(monkeypatch, -9, seconds=3.0, clock=clock)
    assert harness.run_suite({}, "baseline") == ret
    assert suites(emitted) == [{"gate": "baseline", "scope": "full", "duration_s": 3.0, "result": "interrupted",
                                "env_failures": 0}]


def test_ac2_other_exception_not_recorded(monkeypatch, emitted):
    """AC-2 BR-10: any exception other than KeyboardInterrupt passes through and records nothing."""
    fake_run_tests(monkeypatch, 0, raises=OSError("no such test command"))
    with pytest.raises(OSError, match="no such test command"):
        harness.run_suite({}, "redgate")
    assert emitted == []


def test_ac2_best_effort(monkeypatch, capsys):
    """AC-2 (Q13, R-2): recording that raises never changes run_suite's result or prints anything, and a
    suite_timer body's results are untouched."""
    def boom(*a, **k):
        raise RuntimeError("cannot write")

    monkeypatch.setattr(telemetry, "emit", boom)
    ret, _ = fake_run_tests(monkeypatch, 1, out="FAILED test_x")
    assert harness.run_suite({}, "redgate") == ret
    with harness.suite_timer("verify", "miss", "k") as run:
        body = {"rc": 0, "probes": ["p"]}
        run["rc"] = 0
    assert body == {"rc": 0, "probes": ["p"]}
    assert capsys.readouterr() == ("", "")


# --- AC-2: suite_timer (verify's span covers the test command and the saved probes, Q11) -------------
def test_ac2_timer_spans_probes(emitted):
    """AC-2 EX-3 BR-1 (Q11): one record whose duration covers the test command and the probes."""
    with harness.suite_timer("verify", "miss", "k") as run:
        assert isinstance(run, dict)
        time.sleep(0.2)   # the test command
        run["rc"] = 0
        time.sleep(0.3)   # the saved probes
    (rec,) = suites(emitted)
    assert 0.5 - SLACK <= rec["duration_s"] < 10 and three_decimals(rec["duration_s"])
    assert {k: v for k, v in rec.items() if k != "duration_s"} == {
        "gate": "verify", "scope": "full", "cache": "miss", "result": "pass", "env_failures": 0, "key": "k"}


def test_ac2_ex3_verify_fails_after_45s(monkeypatch, emitted):
    """AC-2 EX-3 BR-6: verify runs the suite and it fails after 45 s -> gate verify, scope full, cache miss,
    result fail, 45 s; it counts as 1 run and 0.75 min."""
    clock = Clock()
    use_clock(monkeypatch, clock)
    with harness.suite_timer("verify", "miss", "key3") as run:
        clock.advance(45.0)
        run["rc"] = 1
    assert suites(emitted) == [{"gate": "verify", "scope": "full", "duration_s": 45.0, "cache": "miss",
                                "result": "fail", "env_failures": 0, "key": "key3"}]
    fig = telemetry.suite_figures([dict(suites(emitted)[0], event="suite")])
    assert fig == {"suite_runs": 1, "cache_hits": 0, "cache_misses": 1, "cache_hit_rate": 0.0,
                   "suite_minutes": 0.75}


def test_ac2_timer_interrupt_in_probes(monkeypatch, emitted):
    """AC-2 BR-10 (Q11): Ctrl-C during the probes records `interrupted` with the time so far (test part
    included) and is re-raised."""
    clock = Clock()
    use_clock(monkeypatch, clock)
    with pytest.raises(KeyboardInterrupt):
        with harness.suite_timer("review", "bypass", "kk") as run:
            clock.advance(20.0)
            run["rc"] = 0
            clock.advance(5.5)
            raise KeyboardInterrupt
    assert suites(emitted) == [{"gate": "review", "scope": "full", "duration_s": 25.5, "cache": "bypass",
                                "result": "interrupted", "env_failures": 0, "key": "kk"}]


def test_ac2_timer_other_exception(emitted):
    """AC-2 BR-10: a ValueError inside the timer passes through unchanged and nothing is recorded."""
    err = ValueError("probe broke")
    with pytest.raises(ValueError) as info:
        with harness.suite_timer("verify", "miss", "k") as run:
            run["rc"] = 0
            raise err
    assert info.value is err
    assert emitted == []


def test_ac2_timer_without_rc(monkeypatch, emitted):
    """AC-2: a body that never sets run["rc"] records no result (suite_result(None) is None)."""
    clock = Clock()
    use_clock(monkeypatch, clock)
    with harness.suite_timer("verify", "miss", "k"):
        clock.advance(2.0)
    assert suites(emitted) == [{"gate": "verify", "scope": "full", "duration_s": 2.0, "cache": "miss",
                                "env_failures": 0, "key": "k"}]


# --- call sites (source checks; the end-to-end flow is in tests/selftest.sh) --------------------------
def _fn_tree(fn):
    return ast.parse(textwrap.dedent(inspect.getsource(fn)))


def _calls(tree, name):
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and ((isinstance(n.func, ast.Name) and n.func.id == name)
                 or (isinstance(n.func, ast.Attribute) and n.func.attr == name))]


def _suite_with(tree):
    found = []
    for n in ast.walk(tree):
        if isinstance(n, ast.With):
            for item in n.items:
                c = item.context_expr
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "suite_timer":
                    found.append((n, item))
    return found


@pytest.mark.parametrize("fn,gate", [("cmd_redgate", "redgate"), ("ensure_baseline", "baseline")])
def test_ac2_callers_pass_gate(fn, gate):
    """AC-2 BR-1: cmd_redgate and ensure_baseline run the suite through run_suite(cfg, <their gate>)."""
    tree = _fn_tree(getattr(harness, fn))
    calls = _calls(tree, "run_suite")
    assert [ast.unparse(c) for c in calls] == [f"run_suite(cfg, '{gate}')"]
    assert _calls(tree, "run_tests") == []


def test_ac2_verify_times_tests_and_probes():
    """AC-2 BR-1 (Q11): in verify() the miss/bypass branch runs run_tests(cfg) and run_probes(saved) inside
    `with suite_timer(gate, cache, key) as run:` and sets run["rc"] = rc there; the gate is `review` while
    the status is Review, else `verify`."""
    tree = _fn_tree(harness.verify)
    withs = _suite_with(tree)
    assert len(withs) == 1
    node, item = withs[0]
    assert ast.unparse(item.context_expr) == "suite_timer(gate, cache, key)"
    assert isinstance(item.optional_vars, ast.Name) and item.optional_vars.id == "run"
    body = ast.Module(body=node.body, type_ignores=[])
    assert [ast.unparse(c) for c in _calls(body, "run_tests")] == ["run_tests(cfg)"]
    assert [ast.unparse(c) for c in _calls(body, "run_probes")] == ["run_probes(saved)"]
    assert "run['rc'] = rc" in [ast.unparse(s) for s in node.body]
    assert len(_calls(tree, "run_tests")) == 1 and len(_calls(tree, "run_probes")) == 1
    gate_assigns = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "gate" for t in n.targets)]
    assert len(gate_assigns) == 1
    expr = compile(ast.Expression(gate_assigns[0].value), "<gate>", "eval")
    for status, gate in (("Review", "review"), ("Build", "verify"), ("Rework", "verify")):
        assert eval(expr, {"get_status": lambda text=None, s=status: s, "text": "x"}) == gate


# --- AC-3: the reuse hit record ----------------------------------------------------------------------
def test_ac3_verify_records_hit():
    """AC-3 BR-2 (Q14): verify()'s reuse branch records cache hit, duration 0, result pass, its key."""
    tree = _fn_tree(harness.verify)
    calls = _calls(tree, "record_suite_run")
    assert len(calls) == 1
    c = calls[0]
    assert [ast.unparse(a) for a in c.args] == ["gate"]
    kw = {k.arg: ast.unparse(k.value) for k in c.keywords}
    assert kw == {"duration_s": "0", "result": "'pass'", "cache": "'hit'", "key": "key"}
    # it sits in the `if hit:` branch, not in the run branch
    ifs = [n for n in ast.walk(tree) if isinstance(n, ast.If) and ast.unparse(n.test) == "hit"
           and any(x is c for s in n.body for x in ast.walk(s))]
    assert len(ifs) == 1


# --- AC-5 / BR-4: the single reuse hook ---------------------------------------------------------------
@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("stored", [None, {"key": "k", "when": 1.0, "results": {}, "out": "o"}])
def test_ac5_hook_cross_product(monkeypatch, fresh, stored):
    """AC-5 BR-4: verify_reuse_decision(key, fresh) returns the entry the base expression
    `None if fresh else load_reuse(key)` gives, labelled bypass / hit / miss; load_reuse isn't asked when fresh."""
    asked = []

    def load_reuse(key):
        asked.append(key)
        return stored

    monkeypatch.setattr(harness, "load_reuse", load_reuse)
    label, entry = harness.verify_reuse_decision("the-key", fresh)
    base = None if fresh else stored
    assert entry is base
    assert label == ("bypass" if fresh else ("hit" if stored else "miss"))
    assert asked == ([] if fresh else ["the-key"])


def _entries(key):
    now = time.time()
    good = {"key": key, "when": now - 5, "label": "x", "out": "ok", "results": {"t::a": "passed"}}
    lim = harness.VERIFY_REUSE_S
    return {
        "absent": None,
        "not JSON": "{not json",
        "a list": "[]",
        "other key": dict(good, key="other"),
        "fresh match": good,
        "just inside the window": dict(good, when=now - (lim - 60)),
        "expired (window + 1 s)": dict(good, when=now - (lim + 1)),
        "future when": dict(good, when=now + 3600),
        "bool when": dict(good, when=True),
        "str when": dict(good, when=str(now)),
        "results not a dict": dict(good, results=["t::a"]),
        "results with a non-str value": dict(good, results={"t::a": 1}),
    }


@pytest.mark.parametrize("case", list(_entries("K")))
@pytest.mark.parametrize("fresh", [False, True])
def test_ac5_hook_matches_load_reuse_on_stored_files(monkeypatch, tmp_path, case, fresh):
    """AC-5 BR-4 (Q19 input space): on real stored files, the hook's entry equals the base expression
    `None if fresh else load_reuse(key)`; only a matching, unexpired, well-formed entry is a hit."""
    f = tmp_path / "verify-cache.json"
    monkeypatch.setattr(harness, "reuse_file", lambda: f)
    content = _entries("K")[case]
    if isinstance(content, dict):
        f.write_text(json.dumps(content), encoding="utf-8")
    elif isinstance(content, str):
        f.write_text(content, encoding="utf-8")
    label, entry = harness.verify_reuse_decision("K", fresh)
    assert entry == (None if fresh else harness.load_reuse("K"))
    hit = case in ("fresh match", "just inside the window")
    assert label == ("bypass" if fresh else ("hit" if hit else "miss"))
    assert (entry is not None) == (hit and not fresh)
    if hit and not fresh:
        assert entry["key"] == "K" and entry["results"] == {"t::a": "passed"}


def test_ac5_single_hook():
    """AC-5 BR-4: verify() decides reuse only through verify_reuse_decision (once), never load_reuse."""
    tree = _fn_tree(harness.verify)
    calls = _calls(tree, "verify_reuse_decision")
    assert [ast.unparse(c) for c in calls] == ["verify_reuse_decision(key, fresh)"]
    assert _calls(tree, "load_reuse") == []
    assigns = [ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.Assign) and n.value is calls[0]]
    assert assigns == ["cache, hit = verify_reuse_decision(key, fresh)"]


# --- AC-7 / BR-9: --fresh is a bypass ---------------------------------------------------------------
def test_ac7_fresh_is_bypass(monkeypatch, emitted):
    """AC-7 EX-5 BR-9: --fresh -> ("bypass", None) without consulting the store; the timed run records
    cache bypass and counts as a run with its minutes, outside the hit rate."""
    monkeypatch.setattr(harness, "load_reuse", lambda key: {"key": key, "when": 0, "results": {}})
    assert harness.verify_reuse_decision("kf", True) == ("bypass", None)
    clock = Clock()
    use_clock(monkeypatch, clock)
    with harness.suite_timer("verify", "bypass", "kf") as run:
        clock.advance(30.0)
        run["rc"] = 0
    assert suites(emitted) == [{"gate": "verify", "scope": "full", "duration_s": 30.0, "cache": "bypass",
                                "result": "pass", "env_failures": 0, "key": "kf"}]
    miss = {"event": "suite", "gate": "verify", "duration_s": 60, "cache": "miss", "result": "pass"}
    hit = {"event": "suite", "gate": "verify", "duration_s": 0, "cache": "hit", "result": "pass"}
    fig = telemetry.suite_figures([miss, hit, dict(suites(emitted)[0], event="suite")])
    assert fig == {"suite_runs": 2, "cache_hits": 1, "cache_misses": 1, "cache_hit_rate": 0.5,
                   "suite_minutes": 1.5}
