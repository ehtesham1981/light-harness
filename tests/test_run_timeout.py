# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""Suite timeout (issue #25): AC-1 (BR-1, EX-5). Every suite run the harness starts has a timeout; on
timeout the run's whole process tree is killed, the run lock and slot are released, the base copies are
put back and the gate fails with `suite timed out after N s`, exit 124."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
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


# --- BR-1: the setting --------------------------------------------------------------------------
@pytest.mark.parametrize("cfg,expected", [
    ({}, 3600), ({"suite_timeout": 5}, 5), ({"suite_timeout": 7}, 7), ({"suite_timeout": 1}, 1),
    ({"suite_timeout": 0}, 3600), ({"suite_timeout": -1}, 3600), ({"suite_timeout": "9"}, 3600),
    ({"suite_timeout": True}, 3600), ({"suite_timeout": 3.5}, 3600), ({"suite_timeout": None}, 3600),
    ({"suite_timeout": [5]}, 3600),
])
def test_ac1_suite_timeout_setting(cfg, expected):
    """AC-1 BR-1: suite_timeout(cfg) is the config's `suite_timeout` if it is a positive whole number (not a
    bool), else 3600."""
    assert H.suite_timeout(cfg) == expected


def test_ac1_default_config_and_template():
    """AC-1 BR-1: DEFAULT_CONFIG and the template config.json both carry `"suite_timeout": 3600`."""
    assert H.DEFAULT_CONFIG.get("suite_timeout") == 3600
    tpl = json.loads((T.PLUGIN_SRC / "template" / "config.json").read_text(encoding="utf-8"))
    assert tpl.get("suite_timeout") == 3600


# --- BR-1: run_process --------------------------------------------------------------------------
TREE = r'''
import os, subprocess, sys, time
out = sys.argv[1]
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
with open(out, "w") as f:
    f.write(f"{os.getpid()} {g.pid}")
time.sleep(60)
'''


def test_ac1_run_process_timeout_kills_whole_tree(tmp_path):
    """AC-1 BR-1 R-3: run_process with a 2 s timeout on a child that starts a grandchild and sleeps: it calls
    on_start with the child's pid, raises subprocess.TimeoutExpired after about 2 s, and neither the child
    nor the grandchild is left running."""
    pids = tmp_path / "pids"
    started = []
    t0 = time.monotonic()
    child = grand = None
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            H.run_process([sys.executable, "-c", TREE, str(pids)], dict(os.environ), 2, on_start=started.append)
        took = time.monotonic() - t0
        child, grand = (int(x) for x in pids.read_text().split())
        assert started == [child]
        assert 1.5 <= took < 30, took
        assert T.wait_for(lambda: H.proc_start(child) is None and H.proc_start(grand) is None, 10), \
            "a process of the timed-out run is still running"
    finally:
        T.kill_pid(child)
        T.kill_pid(grand)


@pytest.mark.parametrize("rc,out,err", [(3, "out-a", "err-a"), (0, "ü ✓", "")])
def test_ac1_run_process_returns_code_and_output(tmp_path, rc, out, err):
    """AC-1 BR-1 BR-7: within the timeout, run_process returns (exit code, stdout + stderr) like the base run."""
    code = (f"import sys\nsys.stdout.reconfigure(encoding='utf-8')\nsys.stderr.reconfigure(encoding='utf-8')\n"
            f"sys.stdout.write({out!r})\nsys.stdout.flush()\nsys.stderr.write({err!r})\nsys.exit({rc})\n")
    got = H.run_process([sys.executable, "-c", code], dict(os.environ), 30)
    assert got == (rc, out + err)


# --- BR-1: the record of a timed-out run ---------------------------------------------------------
@pytest.fixture
def emitted(monkeypatch, tmp_path):
    calls = []

    def fake_emit(common, event, **kw):
        calls.append((event, {k: v for k, v in kw.items() if k not in ENVELOPE and v is not None}))

    monkeypatch.setattr(telemetry, "emit", fake_emit)
    monkeypatch.setattr(H, "quiet_common", lambda: tmp_path)
    monkeypatch.setattr(H, "quiet_slug", lambda: "t1")
    return calls


@pytest.mark.parametrize("gate,seconds", [("redgate", 5), ("baseline", 17)])
def test_ac1_timed_out_run_recorded_as_timeout(monkeypatch, emitted, gate, seconds):
    """AC-1 BR-1 BR-12 (Q-7): a suite that times out is recorded as one suite run with result `timeout`,
    and SuiteTimeout(N) passes through with its seconds."""
    def run_tests(cfg):
        raise H.SuiteTimeout(seconds)

    monkeypatch.setattr(H, "run_tests", run_tests)
    with pytest.raises(H.SuiteTimeout) as ei:
        H.run_suite({}, gate)
    assert ei.value.seconds == seconds
    recs = [a for e, a in emitted if e == "suite"]
    assert len(recs) == 1
    rec = recs[0]
    assert isinstance(rec.get("duration_s"), (int, float)) and rec["duration_s"] >= 0
    assert {k: v for k, v in rec.items() if k != "duration_s"} == {
        "gate": gate, "scope": "full", "result": "timeout", "env_failures": 0}


@pytest.fixture
def hroot(monkeypatch, tmp_path):
    env = isolate(monkeypatch, tmp_path / "machine", slots=2)
    repo = T.mini_repo(tmp_path / "repo")
    T.point(monkeypatch, H, repo)
    monkeypatch.setattr(H, "SLOT_POLL", 0.05)
    return SimpleNamespace(tmp=tmp_path, repo=repo, machine=tmp_path / "machine", env=env)


def test_ac1_run_tests_timeout_unwinds_everything(hroot, monkeypatch):
    """AC-1 BR-1 EX-5: run_tests with suite_timeout 1 on a suite that sleeps raises SuiteTimeout(1) only after
    its contexts unwound: the run lock and the slot are released, the base copies are back, the run's temp
    folder is gone, and the suite process is no longer running."""
    base = hroot.repo / ".light-harness-base" / "src"
    base.mkdir(parents=True)
    (base / "m.py").write_text("x = 1\n", encoding="utf-8")
    pid_file = hroot.tmp / "suite.pid"
    cfg = dict(H.DEFAULT_CONFIG, suite_timeout=1, junit_arg="",
               test_cmd=["python3", "-c", f"import os, time\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
                                          "time.sleep(6)\n"])
    t0 = time.monotonic()
    pid = None
    try:
        with pytest.raises(H.SuiteTimeout) as ei:
            H.run_tests(cfg)
        assert ei.value.seconds == 1
        assert time.monotonic() - t0 < 15
        pid = int(pid_file.read_text())
        assert T.wait_for(lambda: H.proc_start(pid) is None, 10)
        assert (base / "m.py").read_text(encoding="utf-8") == "x = 1\n"
        assert list(H.state_dir().glob("base-lock-*")) == []
        assert T.slot_files(hroot.machine) == []
        assert T.run_folders(hroot.machine) == []
    finally:
        T.kill_pid(pid)


# --- EX-5 end to end ------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def snaps5(tmp_path_factory):
    return T.chain(tmp_path_factory.getbasetemp(), "new", suite_timeout=5)


def _tree_ctl(pids: Path, seconds: int) -> str:
    return ("import os, subprocess, sys, time\nif TEST == 't':\n"
            f"    g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep({seconds})'])\n"
            f"    open({str(pids)!r}, 'w').write('%d %d' % (os.getpid(), g.pid))\n"
            f"    time.sleep({seconds})\n")


def test_ac1_ex5_verify_times_out(snaps5, tmp_path, tmp_path_factory):
    """AC-1 EX-5 BR-1 (Q-7): a toy task with suite_timeout 5 stores a passing verify; then its suite starts a
    grandchild and sleeps: `verify` fails with `suite timed out after 5 s`, exit 124; the suite and the
    grandchild are gone; no run lock or slot file is left; `.light-harness-base/` is back; the stored pass
    is dropped and a `timeout` suite record is written. The next verify (fast suite) runs and passes."""
    toy = T.clone(snaps5["green"], tmp_path / "lab", tmp_path_factory.getbasetemp())
    toy.ok("baseline", "src/m.py")
    base_copy = toy.task / ".light-harness-base" / "src" / "m.py"
    assert base_copy.is_file()
    rc, out = toy.h("verify")
    assert rc == 0 and toy.cache.exists(), out
    pids = tmp_path / "pids"
    toy.ctl(_tree_ctl(pids, 120))
    suite = grand = None
    try:
        t0 = time.monotonic()
        rc, out = toy.h("verify", "--fresh")
        took = time.monotonic() - t0
        assert rc == 124, out
        assert "suite timed out after 5 s" in out, out
        assert 5 <= took < 60, took
        suite, grand = (int(x) for x in pids.read_text().split())
        assert T.wait_for(lambda: H.proc_start(suite) is None and H.proc_start(grand) is None, 10), \
            "a process of the timed-out suite is still running"
        assert not toy.lock.exists()
        assert T.slot_files(toy.machine) == []
        assert base_copy.is_file()
        assert not toy.cache.exists(), "a timed-out verify left the stored pass in place"
        assert toy.suite_records()[-1].get("result") == "timeout"
    finally:
        T.kill_pid(suite)
        T.kill_pid(grand)
    toy.ctl("")
    rc, out = toy.h("verify")
    assert rc == 0 and "timed out" not in out, out


# --- the other gates ------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def snaps(tmp_path_factory):
    return T.chain(tmp_path_factory.getbasetemp(), "new")


SHORT = r"""
import sys
sys.path.insert(0, sys.argv[1])
import harness
harness.suite_timeout = lambda cfg: 1
sys.argv = ["light-harness"] + sys.argv[2:]
harness.main()
"""


@pytest.mark.parametrize("snap,args,status_after", [
    ("build", ["redgate"], "Build"),
    ("review", ["verdict", "pass", "--by", "Reviewer"], "Review"),
    ("plan", ["set-status", "Build"], "Plan"),
], ids=["redgate", "verdict-pass", "set-status-build"])
def test_ac1_gates_exit_124(snaps, tmp_path, tmp_path_factory, snap, args, status_after):
    """AC-1 BR-1 (Q-7): with suite_timeout 1 (patched in the harness child) and a suite that sleeps, `redgate`,
    `verdict pass` and `set-status Build` (the baseline) each fail with `suite timed out after 1 s`, exit
    124; the status is unchanged (set-status Build stays Plan), the red gate isn't recorded, and the run
    lock is released."""
    toy = T.clone(snaps[snap], tmp_path / "lab", tmp_path_factory.getbasetemp())
    redgate_before = toy.block("redgate")
    toy.ctl("import time\nif TEST in ('t', 'old'):\n    time.sleep(4)\n")
    p = toy.start(*args, code=SHORT)
    out, _ = p.communicate(timeout=120)
    out = out.decode("utf-8", "replace")
    assert p.returncode == 124, out
    assert "suite timed out after 1 s" in out, out
    assert toy.status() == status_after
    assert toy.block("redgate") == redgate_before
    assert not toy.lock.exists()
