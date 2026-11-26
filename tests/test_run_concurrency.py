# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""`{concurrency}` in test_cmd (issue #16): AC-4 (BR-6, EX-3, EX-4). The no-placeholder half of AC-4 (argv
and gate outcomes as at base, BR-7) is swept in tests/test_run_preservation.py.

The suite spawn is captured by replacing `run_process` (and, for the base code path, the suite command
in the `subprocess` module the harness sees), so no `node` or `npx` is ever started."""
from __future__ import annotations

import os
import subprocess
import sys
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
from lh_machine import isolate, machine_paths, write_gitconfig  # noqa: E402

TEMP_VARS = ("TEMP", "TMP", "TMPDIR")


class _Sub(types.ModuleType):
    """`subprocess` for the harness: a suite command (not git) is recorded instead of started."""

    def __init__(self, spawned):
        super().__init__("subprocess")
        self._spawned = spawned

    def __getattr__(self, name):
        return getattr(subprocess, name)

    def run(self, cmd, *a, **kw):
        if isinstance(cmd, list) and cmd and os.path.basename(str(cmd[0])).lower() not in ("git", "git.exe"):
            self._spawned.append({"via": "subprocess", "cmd": list(cmd), "env": kw.get("env"), "timeout": None})
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.run(cmd, *a, **kw)


@pytest.fixture
def spawn(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path / "machine")
    repo = T.mini_repo(tmp_path / "repo")
    T.point(monkeypatch, H, repo)
    monkeypatch.setattr(H, "SLOT_POLL", 0.05)
    spawned = []

    def run_process(cmd, env, timeout, on_start=None):
        t = env.get("TEMP") if env else None
        spawned.append({"via": "run_process", "cmd": list(cmd), "env": dict(env or {}), "timeout": timeout,
                        "temp_state": (bool(t) and os.path.isdir(t) and not os.listdir(t))})
        return 0, "ok\n"

    monkeypatch.setattr(H, "run_process", run_process)
    monkeypatch.setattr(H, "subprocess", _Sub(spawned))

    def slots(n):
        write_gitconfig(machine_paths(tmp_path / "machine")["gitconfig"], n)

    return SimpleNamespace(spawned=spawned, slots=slots, repo=repo)


def _cpus(monkeypatch, n):
    monkeypatch.setattr(H.os, "cpu_count", lambda: n)


@pytest.mark.parametrize("cpus,slots,cmd,expected", [
    (24, 3, ["node", "--test", "--test-concurrency={concurrency}"],
     ["node", "--test", "--test-concurrency=8"]),
    (2, 4, ["node", "--test", "--test-concurrency={concurrency}"],
     ["node", "--test", "--test-concurrency=1"]),
    (16, 2, ["python3", "-m", "pytest", "-n", "{concurrency}"],
     [sys.executable, "-m", "pytest", "-n", "8"]),
    (24, None, ["npx", "jest", "--maxWorkers={concurrency}"],          # no setting: slots = 24 // 8 = 3
     ["npx", "jest", "--maxWorkers=8"]),
    (24, 3, ["npx", "jest", "--x={{concurrency}}", "{concurrency}-{concurrency}", "{concurrency_x}", "{}", "{x}"],
     ["npx", "jest", "--x={8}", "8-8", "{concurrency_x}", "{}", "{x}"]),
    (7, 2, ["sh", "-c", "pytest -n {concurrency}"],
     ["sh", "-c", "pytest -n 3"]),
], ids=["ex3", "few-cores", "python-swap", "default-slots", "edge-braces", "inside-an-argument"])
def test_ac4_ex3_concurrency_substituted(spawn, monkeypatch, cpus, slots, cmd, expected):
    """AC-4 EX-3 BR-6 (Q-14): each test_cmd argument gets `{concurrency}` replaced by max(1, cores // slots)
    (plain substring replacement: `{{concurrency}}` -> `{8}`; other braces kept), before the python ->
    sys.executable swap; junit_arg is appended unchanged. The spawn goes through run_process."""
    _cpus(monkeypatch, cpus)
    spawn.slots(slots)
    H.run_tests(dict(H.DEFAULT_CONFIG, test_cmd=cmd))
    assert [s["cmd"] for s in spawn.spawned] == [expected + [f"--junitxml={H.JUNIT}"]]
    assert spawn.spawned[0]["via"] == "run_process"


@pytest.mark.parametrize("cpus,setting,arg,expected", [
    (24, None, 3, 8), (2, None, 4, 1), (8, None, 3, 2), (1, None, 1, 1), (None, None, 4, 1),
    (24, 2, None, 12), (24, None, None, 8), (7, None, None, 7), (24, "abc", None, 8), (16, 5, None, 3),
])
def test_ac4_concurrency_value(spawn, monkeypatch, cpus, setting, arg, expected):
    """AC-4 BR-6 BR-3: concurrency(slots) = max(1, cores // slots); without an argument the slot count is
    slot_count() (lightharness.slots, else max(1, cores // 8))."""
    _cpus(monkeypatch, cpus)
    spawn.slots(setting)
    assert H.concurrency(arg) == expected


@pytest.mark.parametrize("cfg_extra,timeout", [({}, 3600), ({"suite_timeout": 900}, 900)])
def test_ac4_ex4_no_placeholder_argv_unchanged(spawn, monkeypatch, cfg_extra, timeout):
    """AC-4 EX-4 BR-7 BR-8 BR-1: `python3 -m pytest -q` (no placeholder) is spawned with the base argv
    ([sys.executable, -m, pytest, -q, --junitxml=<path>]) through run_process with the suite timeout, in an
    environment that is clean_env() plus TEMP = TMP = TMPDIR = a new, empty run folder."""
    _cpus(monkeypatch, 24)
    spawn.slots(3)
    rc, out, results = H.run_tests(dict(H.DEFAULT_CONFIG, test_cmd=["python3", "-m", "pytest", "-q"], **cfg_extra))
    assert (rc, out, results) == (0, "ok\n", {})
    assert len(spawn.spawned) == 1
    s = spawn.spawned[0]
    assert s["via"] == "run_process"
    assert s["cmd"] == [sys.executable, "-m", "pytest", "-q", f"--junitxml={H.JUNIT}"]
    assert s["timeout"] == timeout
    env = s["env"]
    temps = {env.get(k) for k in TEMP_VARS}
    assert len(temps) == 1 and None not in temps and s["temp_state"], {k: env.get(k) for k in TEMP_VARS}
    clean = H.clean_env()
    assert {k: v for k, v in env.items() if k.upper() not in TEMP_VARS} == \
        {k: v for k, v in clean.items() if k.upper() not in TEMP_VARS}
