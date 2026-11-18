"""Regression tests for review issues RV-2 (suite-duration overflow crash) and RV-3 (suite minutes
non-finite), issue #20. AC-8, BR-8, BR-7 (`metrics` never raises on any suite-record content, §8.2).

Stored durations are written as JSON text into a real events.jsonl and read back through
telemetry.read_events, so huge integers arrive as Python ints, exactly as `light-harness metrics` sees them.

Oracle (BR-8: bad values add 0, nothing is subtracted or turned positive):
- `metrics` (kpis, json.dumps(allow_nan=False), render repo and task views) never raises;
- suite_runs equals the record count;
- suite_minutes per task and in the summary is finite and >= 0;
- a stored value adds float(v) only if that is finite and >= 0, else 0 (an int too big for a float adds 0);
- a value that would push a running total past the float range adds nothing;
- an event line holding an integer of more than 4300 digits can't be parsed by json, so read_events skips
  the line like any other unreadable line: it is not a record, so it adds no run and no minutes.
"""
from __future__ import annotations

import itertools
import json
import math
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "plugins" / "light-harness" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import telemetry  # noqa: E402

T0 = 1_760_000_000.0
NOW = T0 + 86400.0
MAXF = sys.float_info.max

# Stored `duration_s` as raw JSON text. 10^4299 is the largest power of ten Python's json parses by
# default (int_max_str_digits = 4300). 10^5000 can't be parsed at all (json.loads raises ValueError), so
# the whole line is skipped by read_events. Its text is built with string ops: str(10 ** 5000) itself
# exceeds the int-to-str digit limit.
OVERLONG = "1" + "0" * 5000
VALUES = [
    ("small int 60", "60"),
    ("small float 1.5", "1.5"),
    ("max float", repr(MAXF)),
    ("1.5e308", "1.5e308"),
    ("max/2", repr(MAXF / 2)),
    ("int 10^308", str(10 ** 308)),
    ("int 2*10^308", str(2 * 10 ** 308)),
    ("int 10^309", str(10 ** 309)),
    ("int 10^400", str(10 ** 400)),
    ("int 10^4299", str(10 ** 4299)),
    ("int 10^5000 (line skipped)", OVERLONG),
    ("negative int -10^400", str(-10 ** 400)),
    ("float literal 1e400", "1e400"),
    ("float literal -1e400", "-1e400"),
    ("negative float -1.5e308", "-1.5e308"),
]
RECORDS = (1, 2, 200)
TASKS = (1, 2)
PREFIX = (False, True)  # a valid 60 s record before the swept ones


def _adds(v) -> float:
    """What one stored value adds, in seconds (BR-8)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0.0
    try:
        f = float(v)
    except OverflowError:
        return 0.0
    return f if math.isfinite(f) and f >= 0 else 0.0


def _running(values) -> float:
    """Running total in which a value that would overflow the float range adds nothing."""
    total = 0.0
    for x in values:
        if math.isfinite(total + x):
            total += x
    return total


def _minutes_text(m) -> str:
    return f"{m:.2f}".rstrip("0").rstrip(".")


def _line(slug, i, text) -> str:
    rec = {"v": 1, "ts": telemetry.iso(T0 + i), "event": "suite", "harness": "1.9.0", "repo": "r",
           "task": slug, "session": "s1", "actor": "agent", "gate": "verify", "scope": "full",
           "result": "pass", "env_failures": 0, "duration_s": "@@D@@"}
    return json.dumps(rec).replace('"@@D@@"', text)


def _write_log(common: Path, slugs, n, prefix, text):
    lines, i = [], 0
    for slug in slugs:
        if prefix:
            i += 1
            lines.append(_line(slug, i, "60"))
        for _ in range(n):
            i += 1
            lines.append(_line(slug, i, text))
    p = telemetry.log_path(common)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _scenario(common: Path, tag, text, n, ntasks, prefix) -> list[str]:
    """Run every metrics view on one scenario; return its differences from the oracle."""
    slugs = [f"t{k}" for k in range(1, ntasks + 1)]
    _write_log(common, slugs, n, prefix, text)
    skipped = text == OVERLONG  # the line isn't a record at all
    swept = [] if skipped else [_adds(json.loads(text))] * n
    want_task_min = _running(([60.0] if prefix else []) + swept) / 60
    want_sum_min = _running([want_task_min] * ntasks)
    runs = len(swept) + (1 if prefix else 0)

    diffs = []
    try:
        events = telemetry.read_events(common)
        trails = {s: [(int(T0), f"harness: task-start {s}")] for s in slugs}
        data = telemetry.kpis(trails, set(), events, now=NOW)
    except Exception as e:  # outcome capture: a raise is itself a difference
        return [f"{tag} kpis raised {type(e).__name__}: {e}"]

    for s in slugs:
        t = data["tasks"][s]
        if t["suite_runs"] != runs:
            diffs.append(f"{tag} {s} suite_runs {t['suite_runs']} != {runs}")
        m = t["suite_minutes"]
        if not (isinstance(m, float) and math.isfinite(m) and m >= 0) or m != want_task_min:
            diffs.append(f"{tag} {s} suite_minutes {m!r} != {want_task_min!r}")
    sm = data["summary"]
    if sm["suite_runs"] != runs * ntasks:
        diffs.append(f"{tag} summary suite_runs {sm['suite_runs']} != {runs * ntasks}")
    m = sm["suite_minutes"]
    if not (isinstance(m, float) and math.isfinite(m) and m >= 0) or m != want_sum_min:
        diffs.append(f"{tag} summary suite_minutes {m!r} != {want_sum_min!r}")

    try:
        back = json.loads(json.dumps(data, indent=2, default=str, allow_nan=False))
        if back["summary"]["suite_minutes"] != want_sum_min:
            diffs.append(f"{tag} --json summary suite_minutes {back['summary']['suite_minutes']!r}")
        for s in slugs:
            one = json.loads(json.dumps(data["tasks"][s], indent=2, default=str, allow_nan=False))
            if one["suite_minutes"] != want_task_min:
                diffs.append(f"{tag} --json {s} suite_minutes {one['suite_minutes']!r}")
    except Exception as e:
        diffs.append(f"{tag} --json raised {type(e).__name__}: {e}")

    try:
        lines = telemetry.render(data).splitlines()
        want = f"  suite runs {runs * ntasks} · cache hit rate — · suite minutes {_minutes_text(want_sum_min)}"
        if want not in lines:
            diffs.append(f"{tag} repo render lacks summary line {want[:90]!r}")
        if any(("inf" in ln or "nan" in ln) for ln in lines if "uite" in ln):
            diffs.append(f"{tag} repo render shows a non-finite suite figure")
        for s in slugs:
            row = f"  {'Suite runs':<22} {runs} · cache hit rate — · suite minutes {_minutes_text(want_task_min)}"
            if row not in telemetry.render(data, s).splitlines():
                diffs.append(f"{tag} {s} task render lacks row {row[:90]!r}")
    except Exception as e:
        diffs.append(f"{tag} render raised {type(e).__name__}: {e}")
    return diffs


def test_ac8_br8_rv2_rv3_overflow_sweep(tmp_path):
    """AC-8 BR-8 BR-7 [sweep] RV-2 RV-3: stored duration as JSON text (15 kinds, incl. ints too big for a
    float, a 5001-digit int whose line is skipped, and finite floats whose sum overflows) x records per
    task {1, 2, 200} x tasks {1, 2} x a valid 60 s record first {no, yes}, through read_events -> kpis -> json.dumps(allow_nan=False) -> render
    (repo view and each task view). metrics never raises, runs = record count, minutes finite, >= 0 and
    equal to the BR-8 oracle."""
    combos = list(itertools.product(VALUES, RECORDS, TASKS, PREFIX))
    diffs, bad, first_per_kind = [], 0, {}
    for k, ((label, text), n, ntasks, prefix) in enumerate(combos):
        tag = f"[{label} x{n} tasks={ntasks} prefix={prefix}]"
        d = _scenario(tmp_path / f"c{k}", tag, text, n, ntasks, prefix)
        bad += bool(d)
        diffs += d
        if d:
            first_per_kind.setdefault(label, d[0])
    print(f"{bad} of {len(combos)} scenarios differ")
    assert not diffs, (f"{bad} of {len(combos)} scenarios differ ({len(diffs)} differences); first per "
                       "duration kind:\n" + "\n".join(first_per_kind.values()))


# --- the issues' own examples, checked directly ---------------------------------------------------
def test_ac8_br8_rv2_huge_int_adds_zero():
    """AC-8 BR-8 RV-2: an int too big for a float adds 0 (no OverflowError); a big int that fits adds its
    float value; a negative huge int adds 0."""
    assert telemetry._suite_seconds(10 ** 309) == 0.0
    assert telemetry._suite_seconds(2 * 10 ** 308) == 0.0
    assert telemetry._suite_seconds(-10 ** 400) == 0.0
    assert telemetry._suite_seconds(10 ** 308) == 1e308


def test_ac8_br8_rv2_sum_of_valid_big_ints_stays_finite():
    """AC-8 BR-8 RV-2: 200 records of 10^308 s each: 200 runs; the first adds 1e308 and every later one
    would overflow the total, so it adds nothing (no OverflowError from the division)."""
    f = telemetry.suite_figures([{"event": "suite", "duration_s": 10 ** 308}] * 200)
    assert f["suite_runs"] == 200
    assert f["suite_minutes"] == 1e308 / 60


def test_ac8_br8_rv2_overlong_int_line_skipped(tmp_path):
    """AC-8 BR-8 BR-7 RV-2: a valid 60 s record plus a line whose duration_s is a 5001-digit integer
    (json can't parse it): read_events skips that line, so metrics gives 1 run and 1 minute, and the
    task view renders. A second task with 120 s plus two such lines gives 1 run and 2 minutes."""
    lines = [_line("t1", 1, "60"), _line("t1", 2, OVERLONG),
             _line("t2", 3, OVERLONG), _line("t2", 4, "120"), _line("t2", 5, OVERLONG)]
    p = telemetry.log_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    events = telemetry.read_events(tmp_path)
    assert [e["duration_s"] for e in events] == [60, 120]
    trails = {s: [(int(T0), f"harness: task-start {s}")] for s in ("t1", "t2")}
    data = telemetry.kpis(trails, set(), events, now=NOW)
    assert telemetry.suite_figures([e for e in events if e["task"] == "t1"]) == {
        "suite_runs": 1, "cache_hits": 0, "cache_misses": 0, "cache_hit_rate": None, "suite_minutes": 1.0}
    assert (data["tasks"]["t1"]["suite_runs"], data["tasks"]["t1"]["suite_minutes"]) == (1, 1.0)
    assert (data["tasks"]["t2"]["suite_runs"], data["tasks"]["t2"]["suite_minutes"]) == (1, 2.0)
    assert (data["summary"]["suite_runs"], data["summary"]["suite_minutes"]) == (2, 3.0)
    assert f"  {'Suite runs':<22} 1 · cache hit rate — · suite minutes 1" in telemetry.render(data, "t1").splitlines()


def test_ac8_br8_rv3_finite_sum_overflow():
    """AC-8 BR-8 RV-3: two finite 1.5e308 durations: the second would push the sum to inf, so it adds
    nothing. Second case: 60 s then max float twice -> (60 + max) / 60, finite."""
    f = telemetry.suite_figures([{"event": "suite", "duration_s": 1.5e308}] * 2)
    assert f == {"suite_runs": 2, "cache_hits": 0, "cache_misses": 0, "cache_hit_rate": None,
                 "suite_minutes": 1.5e308 / 60}
    g = telemetry.suite_figures([{"event": "suite", "duration_s": 60},
                                 {"event": "suite", "duration_s": MAXF},
                                 {"event": "suite", "duration_s": MAXF}])
    assert g["suite_runs"] == 3
    assert g["suite_minutes"] == (60 + MAXF) / 60
    assert math.isfinite(g["suite_minutes"])


def test_ac8_br8_rv3_json_has_no_infinity():
    """AC-8 BR-8 RV-3: kpis over two tasks each holding two 1.5e308 records serialises with
    allow_nan=False (no `Infinity` token); the summary is the finite sum of the task minutes."""
    evs = []
    for k, slug in enumerate(("t1", "t2")):
        for j in range(2):
            t = T0 + 10 * k + j
            evs.append({"v": 1, "ts": telemetry.iso(t), "event": "suite", "task": slug, "session": "s1",
                        "gate": "verify", "duration_s": 1.5e308, "_t": t})
    trails = {s: [(int(T0), f"harness: task-start {s}")] for s in ("t1", "t2")}
    data = telemetry.kpis(trails, set(), evs, now=NOW)
    out = json.dumps(data, default=str, allow_nan=False)
    assert "Infinity" not in out
    assert data["tasks"]["t1"]["suite_minutes"] == 1.5e308 / 60
    assert data["tasks"]["t2"]["suite_minutes"] == 1.5e308 / 60
    assert data["summary"]["suite_minutes"] == 2 * (1.5e308 / 60)
    assert data["summary"]["suite_runs"] == 4
