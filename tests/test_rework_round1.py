# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""Regression sweeps for review round 1 of issues #16/#25/#30, one per defect class (§11.2):

- RV-6 "run folder never removed": folder contents depth (short, past MAX_PATH) × how the run ends × suite or
  probe folder × an old folder under the run's own name (none, removable, can't be removed) (BR-9, AC-5).
- RV-7 "live holder judged dead": record reads failing transiently (sharing violations) at any count, and a
  live holder rewriting its lock while other processes try to take it (BR-2).
- RV-1 "interrupt leaves runs running": interrupt kind × probe workers × probes in the batch (BR-4, R-4).
- RV-2 "unusable shared temp root": root state × OS branch (BR-8, Q-10).
- RV-4, RV-8, RV-9 edges: wait interrupted before a slot is taken; a link in a dead run's place; owner files
  that don't parse (BR-12, BR-9).

Machine-wide state stays in pytest's temp folder (tests/lh_machine.py). OS-specific branches are reached
through fakes on every OS; real processes are `sys.executable` children only.
"""
from __future__ import annotations

import itertools
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
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
from lh_machine import isolate  # noqa: E402


class _Proxy:
    """A module stand-in: given attributes override, the rest come from the real module."""

    def __init__(self, real, **over):
        self._real = real
        self.__dict__.update(over)

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture
def hroot(monkeypatch, tmp_path):
    env = isolate(monkeypatch, tmp_path / "machine", slots=4)
    repo = T.mini_repo(tmp_path / "repo")
    T.point(monkeypatch, H, repo)
    monkeypatch.setattr(H, "SLOT_POLL", 0.05)
    H.LEFTOVERS.clear()
    return SimpleNamespace(tmp=tmp_path, repo=repo, root=Path(env["LIGHT_HARNESS_TEMP_ROOT"]))


def _ext(p: str) -> str:
    if os.name != "nt":
        return p
    return "\\\\?\\" + os.path.abspath(p)


def _nest(top: Path, chars: int) -> int:
    """Fill `top` with nested folders until a path inside is at least `chars` long; returns that length."""
    cur = str(top)
    while len(cur) < chars:
        cur = os.path.join(cur, "nested_" + "d" * 24)
        os.mkdir(_ext(cur))
    with open(_ext(os.path.join(cur, "f.txt")), "w") as f:
        f.write("x")
    return len(cur)


# --- RV-6: run folder never removed ---------------------------------------------------------------
DEPTHS = [40, 300, 420]                       # short, past MAX_PATH (260), well past it
ENDINGS = ["normal", "error", "interrupt"]    # how the run's body ends
KINDS = ["", "-p2"]                           # suite folder, probe folder
OLD = ["none", "removable", "stuck"]          # an old folder already under this run's name


class _Boom(Exception):
    pass


def test_rv6_run_folder_removal_sweep(hroot, monkeypatch):
    """BR-9 [sweep] RV-6 AC-5: generator: full product depth {40, 300, 420 chars} × ending {normal, error,
    KeyboardInterrupt} × folder {suite, probe} × old same-name folder {none, removable, one that can't be
    removed (deletes of it fail)} = 54 runs. Oracle (property): run_temp never raises an error of its own;
    the body's exception comes out unchanged; the run gets a new, empty folder that isn't the stuck one;
    after the run that folder and its owner file are gone; a removable old folder is gone; a stuck one is
    left alone."""
    problems = []
    real_rm = H._rm_entry
    stuck_paths: set = set()

    def rm_entry(path):
        if any(os.path.normcase(str(path)).endswith(os.path.normcase(s)) for s in stuck_paths):
            raise PermissionError(13, "in use", str(path))
        return real_rm(path)

    monkeypatch.setattr(H, "_rm_entry", rm_entry)
    for depth, ending, kind, old in itertools.product(DEPTHS, ENDINGS, KINDS, OLD):
        case = f"depth={depth} ending={ending} kind={kind or 'suite'} old={old}"
        name = f"task-{os.getpid()}{kind}"
        hroot.root.mkdir(parents=True, exist_ok=True)
        stuck_paths.clear()
        if old != "none":
            od = hroot.root / name
            od.mkdir(exist_ok=True)
            (od / "old.txt").write_text("x")
            if old == "stuck":
                stuck_paths.add(os.sep + name)
        got = {}
        try:
            with H.run_temp(kind) as d:
                got["dir"] = d
                got["empty"] = not any(os.scandir(_ext(str(d))))
                _nest(d, depth)
                if ending == "error":
                    raise _Boom()
                if ending == "interrupt":
                    raise KeyboardInterrupt
        except _Boom:
            if ending != "error":
                problems.append(f"{case}: unexpected _Boom")
        except KeyboardInterrupt:
            if ending != "interrupt":
                problems.append(f"{case}: unexpected KeyboardInterrupt")
        except Exception as e:  # noqa: BLE001 - any error of run_temp's own is a difference
            problems.append(f"{case}: run_temp raised {e!r}")
            stuck_paths.clear()
            for leftover in list(hroot.root.iterdir()):
                try:
                    H._rm_run_dir(leftover) if leftover.is_dir() else leftover.unlink()
                except Exception:  # noqa: BLE001 - base code can't remove long paths; keep sweeping
                    pass
            continue
        else:
            if ending != "normal":
                problems.append(f"{case}: the body's exception was swallowed")
        d = got.get("dir")
        if d is None:
            problems.append(f"{case}: no folder")
            continue
        if not got["empty"]:
            problems.append(f"{case}: the run's folder wasn't empty")
        if os.path.lexists(_ext(str(d))):
            problems.append(f"{case}: run folder {d.name} still exists after the run")
        if os.path.lexists(str(d) + ".owner"):
            problems.append(f"{case}: owner file of {d.name} left after the folder went")
        if old == "stuck":
            if d.name == name:
                problems.append(f"{case}: used the stuck folder's name")
            if not (hroot.root / name).exists():
                problems.append(f"{case}: the stuck old folder was removed")
            stuck_paths.clear()
            if (hroot.root / name).exists():
                H._rm_run_dir(hroot.root / name)
        elif os.path.lexists(hroot.root / name) and d.name != name:
            problems.append(f"{case}: removable old folder {name} kept")
    assert not problems, "\n".join(problems[:30])


def test_rv6_long_path_folder_of_a_dead_run_removed_by_next_run(hroot):
    """BR-9 RV-6 AC-5: a dead run's folder holding a path past MAX_PATH is removed when another run starts."""
    child = T.sleeper(0)
    child.wait()
    hroot.root.mkdir(parents=True, exist_ok=True)
    dead = hroot.root / f"other-{child.pid}"
    dead.mkdir()
    T.write_json(hroot.root / f"other-{child.pid}.owner", {"pid": child.pid, "start": "1"})
    assert _nest(dead, 330) >= 330
    with H.run_temp():
        pass
    assert not os.path.lexists(_ext(str(dead))) and not (hroot.root / f"other-{child.pid}.owner").exists()


# --- RV-7: live holder judged dead ------------------------------------------------------------------
@pytest.mark.parametrize("failures,expect_record", [(0, True), (1, True), (3, True), (9, True), (10, False),
                                                    (50, False)])
@pytest.mark.parametrize("error", [PermissionError, OSError])
def test_rv7_read_record_retries_transient_errors_sweep(tmp_path, monkeypatch, failures, error, expect_record):
    """BR-2 [sweep] RV-7: generator: failed reads before a good one {0, 1, 3, 9, 10, 50} × error kind
    {PermissionError (a sharing violation), other OSError}. Oracle: a record readable within the retry window
    (fewer than 10 failures) is returned whole; past it, read_record gives {} without raising. A missing file
    is {} at once (not retried)."""
    p = tmp_path / "lock"
    p.write_text(json.dumps({"pid": 4242, "start": "s"}), encoding="utf-8")
    real = Path.read_text
    calls = {"n": 0}

    def read_text(self, *a, **k):
        if Path(self) == p and calls["n"] < failures:
            calls["n"] += 1
            raise error(13, "in use", str(self))
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(H.time, "sleep", lambda s: None)
    got = H.read_record(p)
    assert got == ({"pid": 4242, "start": "s"} if expect_record else {}), (failures, got)
    assert H.read_record(tmp_path / "missing") == {}


RACE_HOLDER = r"""
import sys, time
sys.path.insert(0, sys.argv[1])
import harness as H
from pathlib import Path
H.set_root(Path(sys.argv[2]))
lock = Path(sys.argv[3])
H._take_lock(lock)
open(sys.argv[4], "w").write("ready")
stop = Path(sys.argv[5])
n = 0
while not stop.exists():  # alive until the test says stop: takers never outlive it
    H.note_suite(1000 + n % 7)
    n += 1
print(n)
"""

RACE_TAKER = r"""
import sys, time, contextlib, io
sys.path.insert(0, sys.argv[1])
import harness as H
from pathlib import Path
H.set_root(Path(sys.argv[2]))
lock = Path(sys.argv[3])
end = time.monotonic() + float(sys.argv[4])
taken = tries = 0
while time.monotonic() < end:
    tries += 1
    try:
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            H._take_lock(lock)
        taken += 1
        break
    except SystemExit:
        pass
print(taken, tries)
"""


@pytest.mark.parametrize("takers", [1, 3])
def test_rv7_live_lock_never_taken_while_rewritten_sweep(hroot, takers):
    """BR-2 [sweep] RV-7: generator: one live holder rewriting its run lock (note_suite) as fast as it can for
    as long as {1, 3} other processes call _take_lock on that lock (3 s each; the holder stays alive until
    they're done). Oracle (property): while the holder is alive, no other process ever takes the lock."""
    lock = H.state_dir(create=True) / "base-lock-race"
    ready, stop = hroot.tmp / "ready", hroot.tmp / "stop"
    env = dict(os.environ)
    holder = subprocess.Popen([sys.executable, "-c", RACE_HOLDER, str(SCRIPTS), str(hroot.repo), str(lock),
                               str(ready), str(stop)], stdout=subprocess.PIPE, text=True, env=env)
    try:
        assert T.wait_for(ready.exists, 20)
        procs = [subprocess.Popen([sys.executable, "-c", RACE_TAKER, str(SCRIPTS), str(hroot.repo), str(lock), "3"],
                                  stdout=subprocess.PIPE, text=True, env=env) for _ in range(takers)]
        outs = [p.communicate(timeout=60)[0].split() for p in procs]
        stop.write_text("stop")
        writes = int(holder.communicate(timeout=60)[0].strip() or 0)
    finally:
        holder.kill()
    assert writes > 50, writes
    assert all(o and o[0] == "0" for o in outs), f"a live lock was taken: {outs} (holder wrote {writes} times)"


# --- RV-1: interrupt leaves runs running ------------------------------------------------------------
PROBE_SLEEP = "import os, sys, time\nopen(sys.argv[1] + '/pid-' + str(os.getpid()), 'w').close()\ntime.sleep(60)\n"


def _probe_batch(hroot, monkeypatch, workers: int, n: int, pids_dir: Path):
    """Saved probes as run_probes sees them: n sleeping probes, `workers` at a time."""
    pids_dir.mkdir(parents=True, exist_ok=True)
    entries = [{"id": f"RV-9.{i}", "issue": "RV-9", "file": f"p{i}.py"} for i in range(1, n + 1)]
    bases = [hroot.tmp / f"base{i}" for i in range(1, n + 1)]
    for b in bases:
        b.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(H, "state_file", lambda ref, name: PROBE_SLEEP)
    monkeypatch.setattr(H, "state_ref", lambda *a: "ref")
    monkeypatch.setattr(H, "probe_bases", lambda es: bases[:len(es)])
    monkeypatch.setattr(H, "probe_workers", lambda raw=None: workers)
    monkeypatch.setattr(H, "probe_cmd", lambda e, path, base=None: [sys.executable, str(path), str(pids_dir)])
    return entries


def _alive(pid: int) -> bool:
    return H.proc_start(pid) is not None


class _FutureInterrupt:
    """Stands in for ThreadPoolExecutor: the first result() waits until `n` probe processes run, then
    raises `exc` in the command's thread."""

    def __init__(self, n, exc, pids_dir):
        self.n, self.exc, self.pids_dir = n, exc, pids_dir

    def __call__(self, max_workers):
        from concurrent.futures import ThreadPoolExecutor
        ex = ThreadPoolExecutor(max_workers=max_workers)
        outer = self
        real_submit = ex.submit

        def submit(*a, **k):
            fut = real_submit(*a, **k)

            def result(timeout=None):
                T.wait_for(lambda: len(list(outer.pids_dir.glob("pid-*"))) >= outer.n, 30)
                raise outer.exc
            fut.result = result
            return fut
        ex.submit = submit
        return ex


INTERRUPTS = {"ctrl-c": lambda: KeyboardInterrupt(), "sigterm": lambda: SystemExit(143),
              "sighup": lambda: SystemExit(129)}


@pytest.mark.parametrize("workers,n", [(2, 2), (2, 3), (3, 3), (3, 4)])
@pytest.mark.parametrize("kind", sorted(INTERRUPTS))
def test_rv1_interrupt_stops_every_probe_sweep(hroot, monkeypatch, workers, n, kind):
    """BR-4 [sweep] RV-1: generator: interrupt kind {Ctrl-C (KeyboardInterrupt), SIGTERM and SIGHUP (the
    SystemExit their handler raises)} × (probe workers, probes in the batch) {(2,2), (2,3), (3,3), (3,4)},
    the interrupt arriving in the command's thread while every worker runs a probe. Oracle (property): the
    interrupt comes out unchanged within a few seconds (probes sleep 60 s), every probe process started is
    gone, and the run lock and slot are released."""
    pids_dir = hroot.tmp / f"pids-{kind}-{workers}-{n}"
    entries = _probe_batch(hroot, monkeypatch, workers, n, pids_dir)
    exc = INTERRUPTS[kind]()
    monkeypatch.setattr(H, "ThreadPoolExecutor", _FutureInterrupt(workers, exc, pids_dir))
    t0 = time.monotonic()
    with pytest.raises(type(exc)):
        H.run_probes(entries)
    took = time.monotonic() - t0
    pids = [int(p.name.split("-")[1]) for p in pids_dir.glob("pid-*")]
    alive_after = [p for p in pids if not T.wait_for(lambda p=p: not _alive(p), 5)]
    for p in pids:
        T.kill_pid(p)
    assert len(pids) >= workers
    assert took < 20, took
    assert not alive_after, alive_after
    assert list(H.state_dir().glob("base-lock-*")) == []
    assert list(H.slot_dir().glob("slot-*.json")) == []


def test_rv1_ctrl_c_single_worker_stops_probe(hroot, monkeypatch):
    """BR-4 RV-1: with one worker the probe runs in the command's own thread; a real Ctrl-C there
    (KeyboardInterrupt delivered to the main thread) stops it at once too."""
    import _thread
    pids_dir = hroot.tmp / "pids-1"
    entries = _probe_batch(hroot, monkeypatch, 1, 2, pids_dir)
    threading.Thread(target=lambda: (T.wait_for(lambda: any(pids_dir.glob("pid-*")), 30),
                                     _thread.interrupt_main()), daemon=True).start()
    t0 = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        H.run_probes(entries)
    assert time.monotonic() - t0 < 20
    pids = [int(p.name.split("-")[1]) for p in pids_dir.glob("pid-*")]
    ok = len(pids) == 1 and T.wait_for(lambda: not _alive(pids[0]), 10)
    for p in pids:
        T.kill_pid(p)
    assert ok, pids


# --- RV-2: unusable shared temp root ----------------------------------------------------------------
def _fake_stat(real, **over):
    vals = {k: getattr(real, k) for k in ("st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid",
                                         "st_size", "st_atime", "st_mtime", "st_ctime")}
    vals.update({k: v for k, v in over.items() if k != "st_file_attributes"})
    return SimpleNamespace(**vals, st_file_attributes=over.get("st_file_attributes", 0))


ROOT_STATES = ["absent", "mine", "mine-open", "other-user", "other-user-sticky", "unwritable", "a-file",
               "a-link"]


@pytest.mark.parametrize("osname", ["nt", "posix"])
@pytest.mark.parametrize("state", ROOT_STATES)
def test_rv2_unusable_temp_root_sweep(hroot, monkeypatch, capsys, osname, state):
    """BR-8 [sweep] RV-2: generator: OS branch {nt, posix} × state of the shared root {absent, this user's,
    this user's and open to all, another user's, another user's but sticky and open to all (like /tmp),
    not writable, a file in its place, a link in its place}; ownership and link-ness are faked through lstat
    and getuid, writability through mkstemp. Oracle (property): a run never fails because of the root; its
    folder is never under a root that is a link, a file, unwritable, or (posix) another user's root that
    isn't sticky and open to all; a usable shared root is used as is, otherwise one warning names the
    fallback, a private folder of this user's."""
    usertemp = hroot.tmp / "usertemp"
    usertemp.mkdir(parents=True, exist_ok=True)
    shared = usertemp / ("lh-tmp" if osname == "nt" else "lh")
    monkeypatch.delenv("LIGHT_HARNESS_TEMP_ROOT", raising=False)
    monkeypatch.setattr(H, "temp_root", lambda: shared)
    monkeypatch.setattr(H, "_TEMP_WARNED", [], raising=False)
    monkeypatch.setattr(H, "_ROOT_FALLBACK", [], raising=False)
    other = state.startswith("other-user")
    if state == "a-file":
        shared.write_text("x")
    elif state != "absent":
        shared.mkdir(parents=True, exist_ok=True)
    real_lstat = os.lstat

    def lstat(p, *a, **k):
        st = real_lstat(p, *a, **k)
        if os.path.normcase(os.path.abspath(p)) != os.path.normcase(os.path.abspath(shared)):
            return st
        mode = st.st_mode
        if state == "a-link":
            return _fake_stat(st, st_mode=stat.S_IFLNK | 0o777, st_file_attributes=0x400)
        if state in ("mine-open", "other-user-sticky"):
            mode = stat.S_IFDIR | 0o777 | (stat.S_ISVTX if state == "other-user-sticky" else 0)
        elif stat.S_ISDIR(mode):
            mode = stat.S_IFDIR | 0o755
        return _fake_stat(st, st_mode=mode, st_uid=(2000 if other else 1000))

    real_mkstemp = tempfile.mkstemp

    def mkstemp(*a, dir=None, **k):
        if state == "unwritable" and dir and os.path.normcase(os.path.abspath(dir)) == \
                os.path.normcase(os.path.abspath(shared)):
            raise PermissionError(13, "denied", dir)
        return real_mkstemp(*a, dir=dir, **k)

    monkeypatch.setattr(H, "os", _Proxy(os, name=osname, lstat=lstat, getuid=lambda: 1000,
                                        chmod=lambda *a, **k: None))
    monkeypatch.setattr(H, "tempfile", _Proxy(tempfile, gettempdir=lambda: str(usertemp), mkstemp=mkstemp))
    if osname == "nt" and os.name != "nt":
        # Windows faked on another OS: its owner-only descriptor can't be built here, so stand in a plain
        # private mkdir for the run folder (this test checks which root is chosen, not permissions)
        monkeypatch.setattr(H, "_mkdir_private", lambda d: d.mkdir(mode=0o700))
    with H.run_temp() as d:
        (d / "x").write_text("x")
        parent = Path(os.path.abspath(d.parent))
    if osname == "nt":  # ownership isn't checked on Windows (its ACLs decide what a user can write)
        usable = state not in ("unwritable", "a-file", "a-link")
    else:
        usable = state in ("absent", "mine", "mine-open", "other-user-sticky")
    same = os.path.normcase(str(parent)) == os.path.normcase(os.path.abspath(shared))
    out = capsys.readouterr()
    warn = [line for line in (out.out + out.err).splitlines() if "can't use" in line]
    if usable:
        assert same, (state, osname, parent)
        assert not warn, warn
    else:
        assert not same, (state, osname, parent)
        assert len(warn) == 1, out


# --- RV-4, RV-8, RV-9 ------------------------------------------------------------------------------
def test_rv4_interrupted_wait_isnt_suite_time(hroot, monkeypatch):
    """BR-12 RV-4: a run interrupted while still waiting for a slot records the wait so far in wait_s, so
    suite_timer leaves it out of duration_s."""
    import _thread
    from lh_machine import machine_paths, write_gitconfig
    write_gitconfig(machine_paths(hroot.tmp / "machine")["gitconfig"], 1)
    holder = T.sleeper(60)
    try:
        d = H.slot_dir()
        T.write_json(d / "slot-0.json", {"pid": holder.pid, "start": H.proc_start(holder.pid), "repo": "r",
                                         "task": "t", "gate": "verify", "since": time.time()})
        recs = []
        monkeypatch.setattr(H, "record_suite_run", lambda gate, **kw: recs.append(kw))
        threading.Timer(1.0, _thread.interrupt_main).start()
        with pytest.raises(KeyboardInterrupt):
            with H.suite_timer("verify"):
                with H.run_slot("verify"):
                    pass
        assert recs and recs[0]["result"] == "interrupted"
        assert (recs[0].get("wait_s") or 0) >= 0.8 and recs[0]["duration_s"] < 0.5, recs
    finally:
        holder.kill()
        holder.wait()


@pytest.mark.parametrize("owner", ["dead-owner", "no-owner"])
def test_rv8_link_in_a_dead_runs_place_is_unlinked(hroot, tmp_path, owner):
    """BR-9 RV-8: a link (a junction on Windows) named like a dead run's folder is removed itself; its target
    and the target's contents are untouched."""
    child = T.sleeper(0)
    child.wait()
    hroot.root.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    link = hroot.root / f"other-{child.pid}"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link)
    if owner == "dead-owner":
        T.write_json(hroot.root / f"other-{child.pid}.owner", {"pid": child.pid, "start": "1"})
    H.sweep_temp_root(hroot.root)
    assert not os.path.lexists(link)
    assert (target / "keep.txt").read_text() == "keep"


OWNER_TEXTS = ["", "garbage", "[1, 2]", '{"start": "x"}', '{"pid": "%d"}', '{"pid": true}', '{"pid": -3}']


@pytest.mark.parametrize("text", OWNER_TEXTS)
@pytest.mark.parametrize("name_pid", ["dead", "live"])
def test_rv9_damaged_owner_falls_back_to_name_pid_sweep(hroot, text, name_pid):
    """BR-9 [sweep] RV-9: generator: owner file content {empty, garbage, a JSON list, no pid, pid as a
    string, pid as a bool, a negative pid} × the pid in the folder's name {an exited process, this live
    process}. Oracle: an owner record that doesn't parse is ignored and the folder's own pid decides: a
    dead pid's folder is removed, a live pid's folder is kept."""
    if name_pid == "dead":
        c = T.sleeper(0)
        c.wait()
        pid = c.pid
    else:
        pid = os.getpid()
    hroot.root.mkdir(parents=True, exist_ok=True)
    d = hroot.root / f"other-{pid}"
    d.mkdir()
    (hroot.root / f"other-{pid}.owner").write_text(text.replace("%d", str(pid)))
    H.sweep_temp_root(hroot.root)
    assert d.exists() == (name_pid == "live"), (text, name_pid)
