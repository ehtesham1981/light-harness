# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""BR-7 [Preservation] sweeps for issues #16, #25, #30 (AC-4): a `test_cmd` without `{concurrency}`, run
with a free slot and finishing within `suite_timeout`, gives the same argv, gate outcomes, records and
messages as the base commit. Oracle: tests/golden/run_base.json, written once by tests/gen_run_golden.py
from the base-commit harness in tests/base_harness/ (see tests/run_scenarios.py for the generators).

Allowed differences, and only these: the per-run TEMP/TMP/TMPDIR in the suite's environment (BR-8), the
load fields of suite-run records (BR-12) and the leftover-files warning line (BR-10). The allowed changes
themselves are checked by the example tests of AC-5, AC-6 and AC-8. Issue #39, merged later, also changed a
fresh `verdict pass` (its `Suite and probes: fresh run` line and its suite record's `miss`); the gate sweep
undoes exactly those two before comparing (tests/test_verdict_reuse.py checks them).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import lh_toy as T  # noqa: E402
import run_scenarios as RS  # noqa: E402
from lh_machine import machine_env  # noqa: E402

NEW_SCRIPTS = T.PLUGIN_SRC / "scripts"
GOLDEN = json.loads((HERE / "golden" / "run_base.json").read_text(encoding="utf-8"))


def _child_env(machine: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in T.DROP_ENV and not k.upper().startswith(T.DROP_PREFIXES)}
    env.update(machine_env(machine, slots=2))
    env.update(PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    return env


def _first_diff(a: dict, b: dict) -> str:
    for k in sorted(set(a) | set(b)):
        if a.get(k) != b.get(k):
            return f"{k}: new {json.dumps(a.get(k), ensure_ascii=False)[:400]} != base {json.dumps(b.get(k), ensure_ascii=False)[:400]}"
    return "?"


def _without_issue39_changes(rec: dict) -> dict:
    """Undo the two changes issue #39 made to a fresh `verdict pass`, which postdate this golden file:
    the verdict block's `Suite and probes: fresh run` line, and the review-gate suite record's cache field,
    now `miss` where the base harness, which never reused a run for the verdict, wrote `bypass`."""
    blocks = dict(rec["blocks"])
    # The line is appended as a Markdown line break: two spaces end the line before it.
    blocks["verdict"] = re.sub(r" *\nSuite and probes: fresh run$", "", blocks["verdict"])
    suite = [dict(e, cache="bypass") if e.get("gate") == "review" and e.get("cache") == "miss" else e
             for e in rec["suite"]]
    return dict(rec, blocks=blocks, suite=suite)


def test_br7_run_tests_sweep(tmp_path):
    """AC-4 EX-4 BR-7 [sweep]: run_tests level, full cross product of 13 test_cmd shapes (pytest, node --test,
    py, sh -c wrapper, npx jest, braces {x} / {} / {path} / {concurrency_x}, near misses of the placeholder,
    an empty argument, non-ASCII) x junit_arg {default, empty} x fake suite result {pass + junit, fail + junit,
    collection error rc 2, all skipped, no tests rc 5, killed by signal rc -9} = 156 cases. For each: the
    spawned argv, cwd, decoding and environment (vs clean_env(), TEMP/TMP/TMPDIR excepted), the returned
    (rc, out, results), LAST_RESULTS, the lock and the temp root afterwards equal the base harness's."""
    cases = RS.cases()
    assert len(cases) == 156 and len(GOLDEN["run_tests"]) == 156
    got = RS.run_tests_level(NEW_SCRIPTS, tmp_path / "rt", _child_env(tmp_path / "machine"))
    diffs = []
    for c in cases:
        cid = c["id"]
        new, base = got.get(cid), GOLDEN["run_tests"][cid]
        if new is None:
            diffs.append(f"{cid}: not run")
            continue
        a, b = RS.comparable_run(new), RS.comparable_run(base)
        if a != b:
            diffs.append(f"{cid}: {_first_diff(a, b)}")
        for s in new["spawns"]:
            env = s.get("env") or {}
            if "inherited" in env:
                continue
            bad = [k for k in env.get("extra", []) + env.get("changed", []) + env.get("missing", [])
                   if k.upper() not in RS.TEMP_VARS]
            if bad:
                diffs.append(f"{cid}: environment differs from clean_env() beyond TEMP/TMP/TMPDIR: {bad}")
    print(f"BR-7 run_tests sweep: {len(diffs)} of {len(cases)} cases differ from the base harness")
    assert not diffs, f"{len(diffs)} of {len(cases)} differ; first ones:\n  " + "\n  ".join(diffs[:6])


def test_br7_gates_sweep(tmp_path, tmp_path_factory):
    """AC-4 BR-7 [sweep]: gate level, end to end in a toy task: gates {redgate, verify, verdict pass, baseline
    (set-status Build), probe run} x suite result {pass, fail, collection error, skipped} = 20 runs, slot free,
    no timeout. Exit code, normalised output, the contract's redgate/verify/verdict blocks, the status, the
    run lock and the suite-run records (minus BR-12 load fields) equal the base harness's; only the BR-10
    warning line may be added."""
    base = tmp_path_factory.getbasetemp()
    snaps = T.chain(base, "new")
    got = RS.gates_level(snaps, tmp_path / "gates", base)
    keys = [f"{g} | {r}" for g, r in RS.GATE_CASES]
    assert len(keys) == 20 and sorted(GOLDEN["gates"]) == sorted(keys)
    diffs = []
    for k in keys:
        a, b = RS.comparable_gate(got[k]), RS.comparable_gate(GOLDEN["gates"][k])
        if k.startswith("verdict pass |"):
            a = _without_issue39_changes(a)
        if a != b:
            diffs.append(f"{k}: {_first_diff(a, b)}")
    print(f"BR-7 gates sweep: {len(diffs)} of {len(keys)} gate runs differ from the base harness")
    assert not diffs, f"{len(diffs)} of {len(keys)} differ; first ones:\n  " + "\n  ".join(diffs[:5])
