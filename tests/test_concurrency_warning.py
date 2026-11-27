# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""Issue #50 AC-6 (BR-10): `parallel_runner_warning(cfg)` warns when test_cmd's runner runs test files in
parallel by default and test_cmd has no `{concurrency}`; it is printed by redgate, verify (a ⚠ line of its
report), the Build baseline run and `check plan`, never changing their exit codes.

The sweep calls the function in-process and compares it with an oracle written here from BR-10. The call
sites run end to end on the toy task of tests/amend_lab.py, whose approved test_cmd is `node --test` (no
`{concurrency}`: a warning case), with a fake `node` (no real node is started).

Sweep choices (spec ambiguities kept out of the space): jest's `--maxWorkers 1` (space form) and `-w=1`, and
pytest's `-n1`/`--numprocesses=0` forms of a serial value, are not generated.
"""
from __future__ import annotations

import copy
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "plugins" / "light-harness" / "scripts"
for p in (str(SCRIPTS), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import harness as H  # noqa: E402
import amend_lab as AL  # noqa: E402

CONC = "{concurrency}"


# --- the oracle (BR-10) ---------------------------------------------------------------------------------
def _base(item: str) -> str:
    b = re.split(r"[\\/]", item)[-1]
    for ext in (".exe", ".cmd", ".js", ".mjs"):
        if b.endswith(ext):
            return b[: -len(ext)]
    return b


def should_warn(tc: list) -> bool:
    if any(CONC in t for t in tc):
        return False
    names = [_base(t) for t in tc]
    if tc and names[0] == "node" and "--test" in tc:
        return "--test-concurrency=1" not in tc
    if "pytest" in names or "py.test" in names:
        for i, t in enumerate(tc):
            if t in ("-n", "--numprocesses") and i + 1 < len(tc):
                return tc[i + 1] not in ("0", "1")
            if t.startswith("--numprocesses="):
                return t.split("=", 1)[1] not in ("0", "1")
            if t.startswith("-n=") or (t.startswith("-n") and len(t) > 2 and not t.startswith("-n=")):
                v = t[3:] if t.startswith("-n=") else t[2:]
                return v not in ("0", "1")
        return False
    if "jest" in names:
        serial = "--runInBand" in tc or "-i" in tc or "--maxWorkers=1" in tc or \
            any(tc[i] == "-w" and tc[i + 1] == "1" for i in range(len(tc) - 1))
        return not serial
    if "vitest" in names:
        return not ("--no-file-parallelism" in tc or "--maxWorkers=1" in tc)
    return False


PREFIXES = {
    "node": [["node"], ["/usr/bin/node"], ["node.exe"]],
    "pytest": [["pytest"], ["python", "-m", "pytest"], ["python3", "-m", "pytest", "-q"], ["py.test"]],
    "jest": [["npx", "jest"], ["jest"], ["./node_modules/.bin/jest"]],
    "vitest": [["npx", "vitest", "run"], ["vitest"]],
}
FLAGS = {   # (flags after the runner, does the runner still parallelise by default?)
    "node": [(["--test"], True), (["--test", "--test-concurrency=4"], True),
             (["--test", "--test-concurrency=1"], False), (["--test", "--test-reporter=dot"], True),
             (["--test", "--test-concurrency=" + CONC], False), (["run.mjs"], False)],
    "pytest": [([], False), (["-x"], False), (["-n", "auto"], True), (["-n", "logical"], True), (["-n", "4"], True),
               (["-nauto"], True), (["-n=auto"], True), (["--numprocesses", "auto"], True),
               (["--numprocesses=4"], True), (["-n", "0"], False), (["-n", "1"], False),
               (["-n", CONC], False), (["-k", "fast"], False)],
    "jest": [([], True), (["--ci"], True), (["--maxWorkers=4"], True), (["--maxWorkers=50%"], True),
             (["--runInBand"], False), (["-i"], False), (["--maxWorkers=1"], False), (["-w", "1"], False),
             (["--maxWorkers=" + CONC], False), (["--maxWorkers", CONC], False)],
    "vitest": [([], True), (["--reporter=dot"], True), (["--maxWorkers=3"], True),
               (["--no-file-parallelism"], False), (["--maxWorkers=1"], False), (["--maxWorkers=" + CONC], False)],
}
NON_TEST = [["python3", "run_toy.py"], ["make", "test"], ["cargo", "test"], ["go", "test", "./..."], ["npm", "test"],
            ["sh", "-c", "pytest -n " + CONC], ["python3", "-m", "unittest"], ["deno", "test"]]


def commands() -> list:
    """(id, test_cmd, expected) for every runner prefix x flag set, with the flags after the runner and, where
    the runner takes trailing paths, followed by one."""
    out = []
    for fam, prefixes in PREFIXES.items():
        for pre in prefixes:
            for flags, par in FLAGS[fam]:
                for tail in ([], ["tests/"]):
                    tc = pre + flags + tail
                    out.append((" ".join(tc), tc, should_warn(tc)))
                    assert should_warn(tc) is par, f"flag table disagrees with the oracle for {tc}"
    for tc in NON_TEST:
        out.append((" ".join(tc), tc, should_warn(tc)))
    return out


COMMANDS = commands()


def test_ac6_space_has_both_outcomes():
    """AC-6 BR-10 [sweep]: the generated commands hold warning and non-warning cases for every runner."""
    by_fam = {}
    for cid, tc, warn in COMMANDS:
        fam = next((f for f, pres in PREFIXES.items() if any(tc[:len(p)] == p for p in pres)), "other")
        by_fam.setdefault(fam, set()).add(warn)
    for fam in PREFIXES:
        assert by_fam[fam] == {True, False}, fam
    assert by_fam["other"] == {False}


@pytest.mark.parametrize("cid,tc,warn", COMMANDS, ids=[c[0] for c in COMMANDS])
def test_ac6_sweep_parallel_runner_warning(cid, tc, warn):
    """AC-6 BR-10 [sweep]: `parallel_runner_warning` returns a string iff test_cmd has no `{concurrency}` and
    its runner parallelises by default (node --test unless --test-concurrency=1; pytest with -n/--numprocesses
    other than 0/1; jest unless --runInBand/-i/--maxWorkers=1/-w 1; vitest unless --no-file-parallelism/
    --maxWorkers=1; a fixed cap above 1 still warns). Plain pytest, serial runs and non-test commands don't.
    The warning mentions `lightharness.slots` and the `{concurrency}` form."""
    cfg = dict(copy.deepcopy(H.DEFAULT_CONFIG), test_cmd=list(tc))
    got = H.parallel_runner_warning(cfg)
    if warn:
        assert isinstance(got, str) and got.strip(), f"no warning for {tc}"
        assert "lightharness.slots" in got and "{concurrency}" in got, got
    else:
        assert got is None, f"unexpected warning for {tc}: {got!r}"


def test_ac6_default_config_no_warning():
    """AC-6 BR-10: a config without test_cmd (the default `python3 -m pytest -q`) gets no warning."""
    cfg = copy.deepcopy(H.DEFAULT_CONFIG)
    cfg.pop("test_cmd")
    assert H.parallel_runner_warning(dict(H.DEFAULT_CONFIG)) is None
    assert H.parallel_runner_warning(dict(cfg, test_cmd=list(H.DEFAULT_CONFIG["test_cmd"]))) is None


@pytest.mark.parametrize("tc", [["node", "--test"], ["npx", "jest"], ["npx", "vitest", "run"],
                                ["python3", "-m", "pytest", "-n", "auto"]], ids=["node", "jest", "vitest", "pytest"])
def test_ac6_warning_text_explains_slots(tc):
    """AC-6 BR-10: the text says `lightharness.slots` limits suite runs sharing the machine, not test files
    inside one run, and suggests the `{concurrency}` form for that runner."""
    got = H.parallel_runner_warning(dict(H.DEFAULT_CONFIG, test_cmd=tc))
    assert isinstance(got, str), got
    forms = {"node": ["--test-concurrency={concurrency}"], "jest": ["--maxWorkers={concurrency}"],
             "vitest": ["--maxWorkers={concurrency}"],
             "python3": ["-n {concurrency}", "-n={concurrency}", "--numprocesses={concurrency}",
                         "--numprocesses {concurrency}", '"-n", "{concurrency}"']}[tc[0] if tc[0] != "npx" else tc[1]]
    assert any(f in got for f in forms), (forms, got)
    assert "lightharness.slots" in got and "file" in got.lower(), got


# --- call sites (end to end, toy task with `node --test`) -----------------------------------------------
@pytest.fixture(scope="module")
def snaps(tmp_path_factory):
    return AL.chain(tmp_path_factory.getbasetemp())


def _lab(snaps, name, tmp_path, tmp_path_factory) -> AL.Lab:
    return AL.clone(snaps[name], tmp_path / "lab", tmp_path_factory.getbasetemp())


def _warn_lines(out: str) -> list:
    return [l for l in out.splitlines() if "lightharness.slots" in l]


def test_ac6_redgate_prints_warning(snaps, tmp_path, tmp_path_factory):
    """AC-6 BR-10: redgate on a `node --test` task prints the warning and still passes (exit 0)."""
    lab = _lab(snaps, "build", tmp_path, tmp_path_factory)
    rc, out = lab.drive("redgate")
    assert rc == 0, out
    assert _warn_lines(out), out
    assert "Red gate passed" in out


def test_ac6_failing_redgate_keeps_exit_code(snaps, tmp_path, tmp_path_factory):
    """AC-6 BR-10: a redgate that refuses after running the suite (the new tests already pass: the
    implementation is in place) still exits 1, and still records no red gate, with the warning printed."""
    lab = _lab(snaps, "build", tmp_path, tmp_path_factory)
    lab.code(AL.T.IMPL)
    ref = lab.ref_sha()
    rc, out = lab.drive("redgate")
    assert rc == 1, out
    assert "tests PASS before implementation" in out, out
    assert _warn_lines(out), out
    assert lab.ref_sha() == ref


def test_ac6_redgate_no_warning_with_concurrency(snaps, tmp_path, tmp_path_factory):
    """AC-6 BR-10: with `{concurrency}` in test_cmd, redgate prints no warning (and passes as before)."""
    lab = _lab(snaps, "build", tmp_path, tmp_path_factory)
    lab.change_cfg(test_cmd=AL.NODE_CONC)
    rc, out = lab.drive("redgate")
    assert rc == 0 and "Red gate passed" in out, out
    assert not _warn_lines(out), out
    assert re.fullmatch(r"--test-concurrency=[1-9][0-9]*", lab.suite_argvs()[-1][2]), lab.suite_argvs()[-1]


def test_ac6_verify_report_has_warning_line(snaps, tmp_path, tmp_path_factory):
    """AC-6 BR-10: verify's report on a `node --test` task has a `- ⚠` line with the warning; the result is
    still PASS (exit 0), as without it."""
    lab = _lab(snaps, "green", tmp_path, tmp_path_factory)
    rc, out = lab.drive("verify")
    assert rc == 0 and "**Result: PASS**" in out, out
    lines = _warn_lines(out)
    assert lines and all(l.startswith("- ⚠") for l in lines), out
    assert "{concurrency}" in lines[0]


def test_ac6_verify_failure_exit_unchanged(snaps, tmp_path, tmp_path_factory):
    """AC-6 BR-10: a failing verify on the same task fails as before (exit 1) and still shows the warning."""
    lab = _lab(snaps, "green", tmp_path, tmp_path_factory)
    lab.code(AL.T.STUB)
    rc, out = lab.drive("verify")
    assert rc == 1 and "**Result: FAIL**" in out, out
    assert _warn_lines(out), out


def test_ac6_baseline_prints_warning(snaps, tmp_path, tmp_path_factory):
    """AC-6 BR-10: entering Build runs the baseline (the existing suite) and prints the warning; the move
    still succeeds (exit 0) and records the baseline."""
    lab = _lab(snaps, "plan", tmp_path, tmp_path_factory)
    rc, out = lab.drive("set-status", "Build")
    assert rc == 0, out
    assert "Baseline:" in out and _warn_lines(out), out
    assert any(s.startswith("harness: baseline") for s in lab.subjects())


def test_ac6_no_baseline_run_no_warning(snaps, tmp_path, tmp_path_factory):
    """AC-6 BR-10: the warning comes with the baseline only when the baseline actually runs the suite:
    Build -> Plan -> Build again (baseline already recorded) prints none."""
    lab = _lab(snaps, "build", tmp_path, tmp_path_factory)
    lab.must("set-status", "Plan")
    n = lab.suite_runs()
    rc, out = lab.drive("set-status", "Build")
    assert rc == 0, out
    assert lab.suite_runs() == n, "the baseline ran again"
    assert not _warn_lines(out), out


def test_ac6_check_plan_hint(snaps, tmp_path, tmp_path_factory):
    """AC-6 BR-10: `check plan` prints the warning as a ⚠ hint and still exits 0 on a complete plan; with
    `{concurrency}` in test_cmd it prints none."""
    lab = _lab(snaps, "plan", tmp_path, tmp_path_factory)
    rc, out = lab.drive("check", "plan")
    assert rc == 0 and "satisfies the plan gate" in out, out
    lines = _warn_lines(out)
    assert lines and all(l.startswith("⚠") for l in lines), out
    lab.change_cfg(test_cmd=AL.NODE_CONC)
    rc, out = lab.drive("check", "plan")
    assert rc == 0 and not _warn_lines(out), out


def test_ac6_check_plan_failing_exit_unchanged(snaps, tmp_path, tmp_path_factory):
    """AC-6 BR-10: an incomplete plan still fails `check plan` with exit 1; the hint doesn't change that."""
    lab = _lab(snaps, "plan", tmp_path, tmp_path_factory)
    AL.T.edit(lab.contract, "1. triple (AC-1)", "")
    rc, out = lab.drive("check", "plan")
    assert rc == 1, out
    assert "✗" in out
