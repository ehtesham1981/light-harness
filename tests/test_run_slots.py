# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""Machine-wide run slots (issue #16): AC-3 (BR-3, BR-4, BR-5, EX-1, EX-2) and the BR-5 [sweep].

The slot folder and slot count come from a throwaway home / app-data folder and user-level git config
(tests/lh_machine.py); `test_isolation_guard` checks they resolve inside pytest's temp folder.
"""
from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
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
from lh_machine import isolate, machine_env, machine_paths, under, write_gitconfig  # noqa: E402

WAIT_RE = re.compile(r"^waiting for a run slot: (.+)$", re.M)
RUN_RE = r"(repo-[a-z])/toy running verify \((\d+ s|\d+ min)\)"


@pytest.fixture
def hroot(monkeypatch, tmp_path):
    env = isolate(monkeypatch, tmp_path / "machine", slots=1)
    repo = T.mini_repo(tmp_path / "repo")
    T.point(monkeypatch, H, repo)
    monkeypatch.setattr(H, "SLOT_POLL", 0.05)
    return SimpleNamespace(tmp=tmp_path, repo=repo, machine=tmp_path / "machine", env=env)


def test_isolation_guard(monkeypatch, tmp_path):
    """AC-3 AC-5 BR-3 BR-8 (R-9): with tests/lh_machine.py's overrides, slot_dir() and temp_root() resolve
    inside pytest's temp folder (the temp root is LIGHT_HARNESS_TEMP_ROOT itself), and the slot count comes
    from the throwaway git config."""
    isolate(monkeypatch, tmp_path, slots=2)
    sd, tr = H.slot_dir(), H.temp_root()
    assert under(sd, tmp_path), sd
    assert under(tr, tmp_path), tr
    assert os.path.normcase(os.path.realpath(tr)) == \
        os.path.normcase(os.path.realpath(machine_paths(tmp_path)["temp_root"]))
    assert H.slot_count() == 2
    isolate(monkeypatch, tmp_path, slots=3)
    assert H.slot_count() == 3


# --- BR-3: slot count and slot folder --------------------------------------------------------------
SLOT_VALUES = [None, "1", "3", "0", "-2", "abc", "2.5", "two", ""]
CPUS = [1, 7, 8, 24, None]


def _expected_slots(value, cpus):
    if value in ("1", "3"):
        return int(value)
    return max(1, (cpus or 1) // 8)


def test_ac3_slot_count_rule(monkeypatch, tmp_path):
    """AC-3 BR-3: slot_count() is `lightharness.slots` from the user-level git config if it is a positive whole
    number, else max(1, cores // 8): git config {unset, 1, 3, 0, -2, abc, 2.5, two, empty} x cpu_count
    {1, 7, 8, 24, None}."""
    isolate(monkeypatch, tmp_path)
    gc = machine_paths(tmp_path)["gitconfig"]
    bad = []
    for value in SLOT_VALUES:
        write_gitconfig(gc, value)
        for cpus in CPUS:
            monkeypatch.setattr(H.os, "cpu_count", lambda n=cpus: n)
            got = H.slot_count()
            if got != _expected_slots(value, cpus):
                bad.append(f"slots={value!r} cpus={cpus}: {got} (expected {_expected_slots(value, cpus)})")
    assert not bad, f"{len(bad)} of {len(SLOT_VALUES) * len(CPUS)} differ:\n  " + "\n  ".join(bad[:10])


class _Proxy(types.ModuleType):
    def __init__(self, real, **over):
        super().__init__(real.__name__)
        self._real = real
        self.__dict__.update(over)

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.mark.parametrize("osname,platform,env_drop,where", [
    ("nt", "win32", (), ("localappdata", "light-harness", "slots")),
    ("nt", "win32", ("LOCALAPPDATA",), ("home", "AppData", "Local", "light-harness", "slots")),
    ("posix", "darwin", (), ("home", "Library", "Caches", "light-harness", "slots")),
    ("posix", "linux", (), ("xdg_cache", "light-harness", "slots")),
    ("posix", "linux", ("XDG_CACHE_HOME",), ("home", ".cache", "light-harness", "slots")),
], ids=["windows", "windows-no-localappdata", "macos", "linux-xdg", "linux-no-xdg"])
def test_ac3_slot_dir_per_os(monkeypatch, tmp_path, osname, platform, env_drop, where):
    """AC-3 BR-3: the per-user slot folder outside any repo: %LOCALAPPDATA%\\light-harness\\slots (else
    ~\\AppData\\Local\\...) on Windows, ~/Library/Caches/light-harness/slots on macOS,
    $XDG_CACHE_HOME/light-harness/slots (else ~/.cache/...) elsewhere; created on first use."""
    isolate(monkeypatch, tmp_path)
    p = machine_paths(tmp_path)
    for k in env_drop:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(H, "os", _Proxy(os, name=osname))
    monkeypatch.setattr(H, "sys", _Proxy(sys, platform=platform))
    got = H.slot_dir()
    monkeypatch.undo()
    want = p[where[0]].joinpath(*where[1:])
    assert os.path.normcase(os.path.abspath(got)) == os.path.normcase(os.path.abspath(want))
    assert Path(want).is_dir()


# --- BR-4: the waiting line ---------------------------------------------------------------------
def _seed(name: str, rec: dict) -> Path:
    d = H.slot_dir()
    d.mkdir(parents=True, exist_ok=True)
    return T.write_json(d / name, rec)


def test_ac3_waiting_line_names_runs_ahead(hroot, capsys):
    """AC-3 BR-4 (Q-12): with no free slot the requester waits and prints, once per change of the runs ahead
    (not every poll), `waiting for a run slot: <repo>/<task> running <gate> (<age>)`, several joined with
    `, `, age `N s` under a minute else `N min`; it enters once the slot is free."""
    a, b = T.sleeper(60), T.sleeper(60)
    entered = threading.Event()
    errors = []
    try:
        now = time.time()
        _seed("slot-0.json", {"pid": a.pid, "start": H.proc_start(a.pid), "repo": "repo-b", "task": "task-x",
                              "gate": "redgate", "since": now - 250})
        _seed("slot-1.json", {"pid": b.pid, "start": H.proc_start(b.pid), "repo": "repo-c", "task": "task-y",
                              "gate": "verify", "since": now - 45.2})

        def request():
            try:
                with H.run_slot("verify"):
                    entered.set()
            except BaseException as e:  # noqa: BLE001 - reported below
                errors.append(repr(e))

        t = threading.Thread(target=request, daemon=True)
        t.start()
        time.sleep(0.8)
        first_wait = not entered.is_set()
        (H.slot_dir() / "slot-1.json").unlink()
        time.sleep(0.8)
        second_wait = not entered.is_set()
        (H.slot_dir() / "slot-0.json").unlink()
        assert entered.wait(5) and not errors, errors
        t.join(5)
    finally:
        for c in (a, b):
            c.kill()
            c.wait()
    assert first_wait and second_wait
    lines = WAIT_RE.findall(capsys.readouterr().out)
    assert len(lines) == 2, lines
    parts = lines[0].split(", ")
    assert len(parts) == 2 and "repo-b/task-x running redgate (4 min)" in parts, lines[0]
    other = [x for x in parts if x != "repo-b/task-x running redgate (4 min)"]
    assert re.fullmatch(r"repo-c/task-y running verify \(4[5-7] s\)", other[0]), lines[0]
    assert lines[1] == "repo-b/task-x running redgate (4 min)"


# --- AC-3 end to end: EX-1 and EX-2 ----------------------------------------------------------------
@pytest.fixture(scope="module")
def snaps(tmp_path_factory):
    return T.chain(tmp_path_factory.getbasetemp(), "new")


def _inside_ctl(inside: Path, counts: Path, hold: float) -> str:
    return (f"import os, time\nif TEST == 't':\n"
            f"    me = os.path.join({str(inside)!r}, str(os.getpid()))\n"
            f"    open(me, 'w').close()\n"
            f"    time.sleep({hold / 2})\n"
            f"    n = len(os.listdir({str(inside)!r}))\n"
            f"    open(os.path.join({str(counts)!r}, str(os.getpid())), 'w').write(str(n))\n"
            f"    time.sleep({hold / 2})\n"
            f"    os.remove(me)\n")


def _parallel_verifies(snaps, tmp_path, sandbox, names, slots, hold=3.0):
    machine = tmp_path / "machine"
    T.set_slots(machine, slots)
    inside, counts = tmp_path / "inside", tmp_path / "counts"
    inside.mkdir()
    counts.mkdir()
    toys = [T.clone(snaps["green"], tmp_path / n, sandbox, machine=machine, repo_name=f"repo-{n}") for n in names]
    for toy in toys:
        toy.ctl(_inside_ctl(inside, counts, hold))
    procs = [toy.start("verify") for toy in toys]
    outs = {}
    for n, p in zip(names, procs):
        out, _ = p.communicate(timeout=300)
        outs[n] = (p.returncode, out.decode("utf-8", "replace").replace("\r\n", "\n"))
    seen = [int(f.read_text()) for f in counts.iterdir()]
    return outs, seen, machine


def test_ac3_ex1_one_slot_two_repos_run_one_after_the_other(snaps, tmp_path, tmp_path_factory):
    """AC-3 EX-1 BR-3 BR-4: lightharness.slots=1; two tasks in different repos (repo-a, repo-b) start `verify`
    together: their suites never overlap (each counts the suites inside at once: always 1), both pass, and
    exactly one prints one waiting line naming the other: `waiting for a run slot: repo-x/toy running verify
    (N s)`. No slot file is left."""
    outs, seen, machine = _parallel_verifies(snaps, tmp_path, tmp_path_factory.getbasetemp(), ["a", "b"], 1)
    assert [rc for rc, _ in outs.values()] == [0, 0], outs
    assert sorted(seen) == [1, 1], seen
    waits = {n: WAIT_RE.findall(out) for n, (_, out) in outs.items()}
    waiters = [n for n, w in waits.items() if w]
    assert len(waiters) == 1, waits
    waiter = waiters[0]
    first = "b" if waiter == "a" else "a"
    assert len(waits[waiter]) == 1, waits
    m = re.fullmatch(RUN_RE, waits[waiter][0])
    assert m and m.group(1) == f"repo-{first}", waits[waiter]
    assert T.slot_files(machine) == []


def test_ac3_ex2_two_slots_three_runs(snaps, tmp_path, tmp_path_factory):
    """AC-3 EX-2 BR-4 BR-5: lightharness.slots=2; three tasks start `verify` together: two run at once (the
    most suites ever inside is exactly 2), the third waits, printing one line naming the two runs ahead."""
    outs, seen, machine = _parallel_verifies(snaps, tmp_path, tmp_path_factory.getbasetemp(), ["a", "b", "c"], 2)
    assert [rc for rc, _ in outs.values()] == [0, 0, 0], outs
    assert len(seen) == 3 and max(seen) == 2, seen
    waits = {n: WAIT_RE.findall(out) for n, (_, out) in outs.items()}
    lines = [(n, l) for n, w in waits.items() for l in w]
    assert len(lines) == 1, waits
    waiter, line = lines[0]
    parts = line.split(", ")
    assert len(parts) == 2, line
    named = sorted(re.fullmatch(RUN_RE, p).group(1) for p in parts if re.fullmatch(RUN_RE, p))
    assert named == sorted(f"repo-{n}" for n in "abc" if n != waiter), line
    assert T.slot_files(machine) == []


# --- BR-5 sweep -----------------------------------------------------------------------------------
REQUESTER = r'''
import json, os, sys, time
scripts, root, idx, hold, fate = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4]), sys.argv[5]
sys.path.insert(0, scripts)
import harness
harness.SLOT_POLL = 0.05
inside = os.path.join(root, "inside")
open(os.path.join(root, "ready", str(idx)), "w").close()
while not os.path.exists(os.path.join(root, "go")):
    time.sleep(0.01)
with harness.run_slot("verify"):
    me = os.path.join(inside, str(os.getpid()))
    open(me, "w").close()
    time.sleep(hold / 2)
    n = len(os.listdir(inside))
    time.sleep(hold / 2)
    os.remove(me)
    with open(os.path.join(root, "out", str(idx)), "w") as f:
        json.dump({"count": n, "pid": os.getpid()}, f)
    if fate == "crash":
        os._exit(3)
'''
FATES = ["finishes", "crashes holding its slot", "replaced by a reused pid"]
BOUND_S = 90


def _scenario(sc: dict) -> list:
    """Run one scenario; its problems (empty if the invariant held)."""
    root, n, slots, fate = sc["root"], sc["requesters"], sc["slots"], sc["fate"]
    for d in ("inside", "out", "ready"):
        (root / d).mkdir(parents=True)
    env = dict(sc["base_env"])
    env.update(machine_env(root / "machine", slots=slots))
    if fate == "replaced by a reused pid":
        mp = machine_paths(root / "machine")
        rec = {"pid": os.getpid(), "start": "1", "repo": "ghost", "task": "ghost", "gate": "verify",
               "since": time.time()}
        for d in (mp["localappdata"], mp["home"] / "Library" / "Caches", mp["xdg_cache"]):
            T.write_json(d / "light-harness" / "slots" / "slot-0.json", rec)
    procs = []
    for i in range(n):
        f = "crash" if (fate == "crashes holding its slot" and i == 0) else "normal"
        procs.append(subprocess.Popen(
            [sys.executable, "-c", REQUESTER, str(SCRIPTS), str(root), str(i), str(sc["holds"][i]), f],
            cwd=str(sc["repos"][i]), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE))
    problems = []
    end = time.monotonic() + BOUND_S
    while len(os.listdir(root / "ready")) < n and time.monotonic() < end and all(p.poll() is None for p in procs):
        time.sleep(0.02)
    (root / "go").write_text("", encoding="utf-8")
    while any(p.poll() is None for p in procs) and time.monotonic() < end:
        time.sleep(0.02)  # polling reaps exited children at once (no zombie looks alive)
    for i, p in enumerate(procs):
        crash = fate == "crashes holding its slot" and i == 0
        if p.poll() is None:
            problems.append(f"requester {i} still waiting after {BOUND_S}s (blocked by a dead or reused holder?)")
            p.kill()
            p.wait()
        elif p.returncode != (3 if crash else 0):
            err = p.stderr.read().decode("utf-8", "replace")[-300:]
            problems.append(f"requester {i} exited {p.returncode}: {err}")
        p.stderr.close()
        out = root / "out" / str(i)
        if not out.exists():
            problems.append(f"requester {i} never ran")
            continue
        c = json.loads(out.read_text(encoding="utf-8"))["count"]
        if c > slots:
            problems.append(f"requester {i} saw {c} live runs with {slots} slot(s)")
    return problems


def test_br5_slot_limit_sweep(tmp_path, monkeypatch):
    """AC-3 BR-5 [sweep] EX-2 EX-7: full cross product of slot count {1, 2, 3, 4} x requesters {1..6} (each a
    separate sys.executable child calling run_slot against the scenario's own throwaway slot folder, from
    its own repo) x fate of a holder {finishes, crashes holding its slot (os._exit, dead pid), replaced by a
    pre-seeded slot file whose pid is a live unrelated process with a different start} = 72 scenarios; hold
    times from random.Random(25); SLOT_POLL 0.05 s. Oracle: every count of live runs a requester sees inside
    its slot is <= the slot count, and every requester runs within the bound (a dead or reused-pid holder
    never blocks anyone)."""
    isolate(monkeypatch, tmp_path / "parent-machine")
    repos = [T.mini_repo(tmp_path / "repos" / f"repo-{i}") for i in range(6)]
    base_env = {k: v for k, v in os.environ.items()
                if k.upper() not in T.DROP_ENV and not k.upper().startswith(T.DROP_PREFIXES)}
    base_env.update(PYTHONDONTWRITEBYTECODE="1")
    rng = random.Random(25)
    scenarios = []
    for slots in (1, 2, 3, 4):
        for n in range(1, 7):
            for fate in FATES:
                scenarios.append({"slots": slots, "requesters": n, "fate": fate, "repos": repos,
                                  "holds": [round(rng.uniform(0.05, 0.25), 3) for _ in range(n)],
                                  "root": tmp_path / f"s{len(scenarios)}", "base_env": base_env})
    assert len(scenarios) == 72
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(_scenario, scenarios))
    bad = [f"slots {sc['slots']}, {sc['requesters']} requesters, {sc['fate']}: " + "; ".join(p)
           for sc, p in zip(scenarios, results) if p]
    print(f"BR-5 sweep: {len(bad)} of {len(scenarios)} scenarios break the invariant")
    assert not bad, f"{len(bad)} of {len(scenarios)} differ; first ones:\n  " + "\n  ".join(bad[:6])
