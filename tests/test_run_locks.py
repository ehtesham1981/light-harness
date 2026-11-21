# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""Process identity, run locks and slot takeover (issue #25): AC-2 (BR-2, EX-6, EX-7) and AC-10 (BR-14).

OS branches are tested on every OS through injected fakes: a fake kernel32 object for `_start_windows`, a
fake /proc reader for `_start_linux`, a fake `kill` and `ps` runner for `_start_ps`. Real-process tests use
`sys.executable` children only. Machine-wide state stays in pytest's temp folder (tests/lh_machine.py).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
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

REFUSED = "another test or probe run of this task is in progress (pid {pid}); wait for it to finish."
ORPHAN = "stopped an orphaned suite from a killed run (pid {pid})"
STILL_ACTIVE = 259
RECORD_KEYS = {"pid", "start", "repo", "task", "gate", "since"}


@pytest.fixture
def hroot(monkeypatch, tmp_path):
    env = isolate(monkeypatch, tmp_path / "machine", slots=1)
    repo = T.mini_repo(tmp_path / "repo")
    T.point(monkeypatch, H, repo)
    monkeypatch.setattr(H, "SLOT_POLL", 0.05)
    return SimpleNamespace(tmp=tmp_path, repo=repo, machine=tmp_path / "machine", env=env)


# --- fakes for the three OS branches ------------------------------------------------------------
def _target(arg):
    """What a ctypes out-argument refers to: byref(x) -> x, pointer(x) -> x, else the argument itself."""
    if hasattr(arg, "_obj"):
        return arg._obj
    if hasattr(arg, "contents"):
        return arg.contents
    return arg


def _val(x):
    return getattr(x, "value", x)


class FakeK32:
    """kernel32 stand-in. procs: {pid: (exit code, creation FILETIME as an int)}; denied: pids whose
    OpenProcess fails with ERROR_ACCESS_DENIED (5); any other pid fails with ERROR_INVALID_PARAMETER (87)."""

    def __init__(self, procs: dict, denied=()):
        self.procs, self.denied = dict(procs), set(denied)
        self.err = 0
        self.calls: list = []
        self.handles: dict = {}

    def OpenProcess(self, access, inherit, pid):  # noqa: N802 - kernel32's names
        access, pid = int(_val(access)), int(_val(pid))
        self.calls.append(("OpenProcess", access, pid))
        if pid in self.denied:
            self.err = 5
            return 0
        if pid not in self.procs:
            self.err = 87
            return 0
        h = 0x100 + 4 * len(self.handles)
        self.handles[h] = pid
        return h

    def GetExitCodeProcess(self, h, code):  # noqa: N802
        pid = self.handles.get(_val(h))
        if pid is None:
            self.err = 6
            return 0
        _target(code).value = self.procs[pid][0]
        return 1

    def GetProcessTimes(self, h, creation, exit_time, kernel, user):  # noqa: N802
        pid = self.handles.get(_val(h))
        if pid is None:
            self.err = 6
            return 0
        ft = self.procs[pid][1]
        t = _target(creation)
        if hasattr(t, "dwLowDateTime"):
            t.dwLowDateTime, t.dwHighDateTime = ft & 0xFFFFFFFF, ft >> 32
        else:
            t.value = ft
        return 1

    def CloseHandle(self, h):  # noqa: N802
        self.calls.append(("CloseHandle", _val(h)))
        return 1

    def GetLastError(self):  # noqa: N802
        return self.err


def stat_line(pid: int, comm: str, start: int) -> str:
    """/proc/<pid>/stat with `start` as field 22 (starttime)."""
    fields = ["S"] + [str(i) for i in range(4, 22)] + [str(start)] + ["0"] * 30
    return f"{pid} ({comm}) " + " ".join(fields) + "\n"


# --- AC-2 (a): Windows branch -------------------------------------------------------------------
FT1, FT2 = 133_801_234_567_890_123, 132_000_000_000_000_007


@pytest.mark.parametrize("procs,denied,expected", [
    ({}, (), None),                                   # no such process: OpenProcess fails with 87
    ({}, (4242,), ""),                                # exists, access denied: start unknown
    ({4242: (0, FT1)}, (), None),                     # exited with code 0
    ({4242: (1, FT1)}, (), None),                     # exited with code 1
    ({4242: (STILL_ACTIVE, FT1)}, (), str(FT1)),      # running: its creation time
    ({4242: (STILL_ACTIVE, FT2)}, (), str(FT2)),      # another creation time
], ids=["invalid-87", "denied-5", "exited-0", "exited-1", "alive-ft1", "alive-ft2"])
def test_ac2_start_windows_fake_k32(procs, denied, expected):
    """AC-2 BR-2: _start_windows opens the pid with PROCESS_QUERY_LIMITED_INFORMATION (0x1000): error 5 -> "",
    any other failure -> None, exit code != STILL_ACTIVE (259) -> None, else the creation FILETIME as a
    decimal string."""
    k32 = FakeK32(procs, denied)
    assert H._start_windows(4242, k32=k32) == expected
    opens = [c for c in k32.calls if c[0] == "OpenProcess"]
    assert opens and opens[0][1] == 0x1000 and opens[0][2] == 4242, k32.calls


def test_ac2_ex7_windows_pid_reuse_and_access_denied(monkeypatch):
    """AC-2 EX-7 BR-2 RV-14 (Windows branch, fake pids): a record whose pid now belongs to a newer process
    (creation time differs) is dead; the same creation time is alive; a record with a start whose pid this
    user can't open (access denied) is dead, since only this user's harness writes records and it can open
    its own processes; a legacy record (no start) with a denied pid is alive while the pid exists; an exited
    process is dead."""
    k32 = FakeK32({4242: (STILL_ACTIVE, FT2), 4246: (0, FT1)}, denied=(4250,))
    monkeypatch.setattr(H, "proc_start", lambda pid: H._start_windows(pid, k32=k32))
    assert H.holder_alive(4242, str(FT1)) is False
    assert H.holder_alive(4242, str(FT2)) is True
    assert H.holder_alive(4250, str(FT1)) is False
    assert H.holder_alive(4250, None) is True
    assert H.holder_alive(4250, "") is True
    assert H.holder_alive(4246, str(FT1)) is False
    assert H.holder_alive(4254, str(FT1)) is False


# --- AC-2 (b): Linux and other POSIX branches ---------------------------------------------------
@pytest.mark.parametrize("comm,start", [("python3", 987654), ("we ) (ird", 12345678), ("x)(y) z", 4)])
def test_ac2_start_linux_fake_proc(comm, start):
    """AC-2 BR-2: _start_linux reads field 22 (starttime) of /proc/<pid>/stat, parsed after the last `)`
    (a command name may hold `) (`)."""
    seen = []

    def read(path):
        seen.append(str(path).replace("\\", "/"))
        return stat_line(4321, comm, start)

    assert H._start_linux(4321, read=read) == str(start)
    assert seen == ["/proc/4321/stat"]


def test_ac2_start_linux_missing_process():
    """AC-2 BR-2: no /proc/<pid>/stat -> None (no such process)."""
    def read(path):
        raise FileNotFoundError(path)

    assert H._start_linux(4321, read=read) is None


@pytest.mark.parametrize("kill_raises,ps,expected,ps_called", [
    (None, (0, "  Thu Nov 20 10:00:00 2026 \n"), "Thu Nov 20 10:00:00 2026", True),
    (None, (0, "Fri Jan  2 03:04:05 2026\n"), "Fri Jan  2 03:04:05 2026", True),
    (PermissionError, (0, "Sat Mar  7 08:09:10 2026\n"), "Sat Mar  7 08:09:10 2026", True),  # exists, another user
    (ProcessLookupError, (0, "Thu Nov 20 10:00:00 2026\n"), None, False),                   # no such process
    (None, (1, ""), "", True),                                                              # ps fails: unknown
], ids=["alive", "alive-2", "other-user", "gone", "ps-fails"])
def test_ac2_start_ps_fake_kill_and_ps(monkeypatch, kill_raises, ps, expected, ps_called):
    """AC-2 BR-2 (macOS / other POSIX): os.kill(pid, 0) decides existence (ProcessLookupError -> None,
    PermissionError -> exists), then `ps -o lstart= -p <pid>` stripped is the start; ps failing -> ""."""
    kills, runs = [], []

    def kill(pid, sig):
        kills.append((pid, sig))
        if kill_raises:
            raise kill_raises()

    def run(argv):
        runs.append([str(a) for a in argv])
        return ps

    monkeypatch.setattr(H.os, "kill", kill)
    assert H._start_ps(4242, run=run) == expected
    assert kills == [(4242, 0)]
    assert runs == ([["ps", "-o", "lstart=", "-p", "4242"]] if ps_called else [])


# --- AC-2: holder_alive and read_record ---------------------------------------------------------
@pytest.mark.parametrize("pid,start,current,expected", [
    (4242, "tok-1", {4242: "tok-1"}, True),
    (4242, "tok-1", {4242: "tok-2"}, False),          # pid reused by a newer process
    (4242, "tok-1", {}, False),                       # gone
    (4242, "", {4242: "tok-9"}, True),                # no recorded start (legacy): alive iff it exists
    (4242, None, {4242: "tok-9"}, True),
    (4242, None, {}, False),
    (4242, "tok-1", {4242: ""}, True),                # can't read its start: conservative
    (0, "tok-1", {0: "tok-1"}, False),
    (-5, "tok-1", {-5: "tok-1"}, False),
    ("4242", "tok-1", {4242: "tok-1"}, False),        # not an int
    (None, "tok-1", {}, False),
    (True, "tok-1", {1: "tok-1", True: "tok-1"}, False),  # a bool is not a pid
])
def test_ac2_holder_alive(monkeypatch, pid, start, current, expected):
    """AC-2 BR-2: alive only if the pid is a positive int, a process with it exists, and its start matches
    the recorded one (unknown on either side counts as alive while the process exists)."""
    monkeypatch.setattr(H, "proc_start", lambda p: current.get(p))
    assert H.holder_alive(pid, start) is expected


@pytest.mark.parametrize("content,expected", [
    ('{"pid": 4242, "start": "77", "repo": "r", "task": "t", "gate": "verify", "since": 1.5}',
     {"pid": 4242, "start": "77", "repo": "r", "task": "t", "gate": "verify", "since": 1.5}),
    ('{"pid": 99, "start": "", "suite_pid": 100, "suite_start": "5"}',
     {"pid": 99, "start": "", "suite_pid": 100, "suite_start": "5"}),
    ("4242", {"pid": 4242}),                          # a 1.9.0 lock: just the pid
    ("31337\n", {"pid": 31337}),
    ("", {}),
    ("not a lock {", {}),
    ('{"pid": 4242, "sta', {}),                       # cut short
    ("[4242]", {}),
    ("ÿ\u0000garbage", {}),
])
def test_ac2_read_record(tmp_path, content, expected):
    """AC-2 BR-2 (Q-2): a lock/slot file as a dict; a legacy file of digits -> {"pid": n}; anything
    unreadable -> {}."""
    p = tmp_path / "rec"
    p.write_text(content, encoding="utf-8")
    assert H.read_record(p) == expected
    assert H.read_record(tmp_path / "missing") == {}


# --- AC-2 (c): EX-6 -----------------------------------------------------------------------------
@pytest.mark.parametrize("content", ["999996", '{"pid": 999996, "start": "133801234567890123"}'],
                         ids=["legacy-plain-pid", "json-record"])
def test_ac2_ex6_windows_dead_lock_taken_at_once(hroot, monkeypatch, capsys, content):
    """AC-2 EX-6 BR-2: Windows branch (fake kernel32): a lock held by a pid that has exited, written 1 minute
    ago, is taken over at once (base: blocked for 2 hours). The new lock records this process."""
    k32 = FakeK32({})
    monkeypatch.setattr(H, "proc_start", lambda pid: H._start_windows(pid, k32=k32))
    lock = hroot.tmp / "base-lock-x"
    lock.write_text(content, encoding="utf-8")
    old = time.time() - 60
    os.utime(lock, (old, old))
    t0 = time.monotonic()
    H._take_lock(lock)
    assert time.monotonic() - t0 < 2
    rec = json.loads(lock.read_text(encoding="utf-8"))
    assert rec["pid"] == os.getpid() and RECORD_KEYS <= set(rec), rec
    assert "another test or probe run" not in capsys.readouterr().err


# --- AC-2 (d): real processes on the running OS -------------------------------------------------
def test_ac2_proc_start_real_processes():
    """AC-2 BR-2: proc_start of a live child is a non-empty token, stable between calls and different from
    this process's; once the child has exited (and been reaped) it is None."""
    child = T.sleeper(30)
    try:
        tok = H.proc_start(child.pid)
        assert isinstance(tok, str) and tok, tok
        assert H.proc_start(child.pid) == tok
        me = H.proc_start(os.getpid())
        assert isinstance(me, str) and me and H.proc_start(os.getpid()) == me
        assert H.holder_alive(child.pid, tok) is True
        assert H.holder_alive(child.pid, tok + "0") is False
    finally:
        child.kill()
        child.wait()
    assert T.wait_for(lambda: H.proc_start(child.pid) is None, 5), H.proc_start(child.pid)
    assert H.holder_alive(child.pid, tok) is False


def _dead_child():
    """(pid, start token) of a child that has exited and been reaped."""
    c = T.sleeper(30)
    tok = H.proc_start(c.pid)
    c.kill()
    c.wait()
    T.wait_for(lambda: H.proc_start(c.pid) is None, 5)
    return c.pid, tok


def test_ac2_lock_real_holders(hroot, capsys):
    """AC-2 BR-2: on the running OS, a lock from a child that has exited is taken at once; a live child with
    its right start blocks with the base message (exit 1, lock untouched); the same pid with a wrong start
    is taken; a legacy plain-pid lock is alive iff that pid exists."""
    lock = hroot.tmp / "base-lock-real"
    dead_pid, dead_tok = _dead_child()
    live = T.sleeper(60)
    try:
        live_tok = H.proc_start(live.pid)
        outcomes = {}
        for name, content in [("dead", json.dumps({"pid": dead_pid, "start": dead_tok})),
                              ("live", json.dumps({"pid": live.pid, "start": live_tok})),
                              ("reused", json.dumps({"pid": live.pid, "start": "1" + str(live_tok)})),
                              ("legacy-live", str(live.pid)),
                              ("legacy-dead", str(dead_pid))]:
            lock.write_text(content, encoding="utf-8")
            capsys.readouterr()
            try:
                H._take_lock(lock)
                rec = H.read_record(lock)
                outcomes[name] = ("taken", rec.get("pid"))
            except SystemExit as e:
                outcomes[name] = ("refused", e.code, lock.read_text(encoding="utf-8") == content,
                                  REFUSED.format(pid=live.pid) in capsys.readouterr().err)
            lock.unlink(missing_ok=True)
        me = os.getpid()
        assert outcomes == {"dead": ("taken", me), "live": ("refused", 1, True, True), "reused": ("taken", me),
                            "legacy-live": ("refused", 1, True, True), "legacy-dead": ("taken", me)}
    finally:
        live.kill()
        live.wait()


def _seed_slot(name: str, rec: dict) -> Path:
    d = H.slot_dir()
    d.mkdir(parents=True, exist_ok=True)
    return T.write_json(d / name, dict({"repo": "ghost", "task": "g", "gate": "verify", "since": time.time()}, **rec))


def test_ac2_ex7_slot_dead_or_reused_holder_taken_at_once(hroot, capsys):
    """AC-2 EX-7 BR-2 BR-5: with one slot, a slot file whose holder has exited, or whose pid now belongs to
    a newer process (start differs), is free: run_slot enters at once without a waiting line, and holds
    the only slot file itself; on exit no slot file is left."""
    dead_pid, dead_tok = _dead_child()
    live = T.sleeper(60)
    try:
        for rec in ({"pid": dead_pid, "start": dead_tok}, {"pid": live.pid, "start": "1" + str(H.proc_start(live.pid))}):
            _seed_slot("slot-0.json", rec)
            capsys.readouterr()
            t0 = time.monotonic()
            with H.run_slot("verify"):
                took = time.monotonic() - t0
                held = [H.read_record(p).get("pid") for p in H.slot_dir().glob("slot-*.json")]
            assert took < 2, f"waited {took:.1f}s for a slot held by a dead holder {rec}"
            assert held == [os.getpid()], held
            assert "waiting for a run slot" not in capsys.readouterr().out
            assert list(H.slot_dir().glob("slot-*.json")) == []
    finally:
        live.kill()
        live.wait()


def test_ac2_live_slot_holder_blocks(hroot):
    """AC-2 BR-2 BR-4: a slot held by a live process (right start) blocks a requester with one slot; once
    that process exits, the requester takes the slot without a restart."""
    live = T.sleeper(60)
    entered = threading.Event()
    errors = []
    try:
        _seed_slot("slot-0.json", {"pid": live.pid, "start": H.proc_start(live.pid)})

        def request():
            try:
                with H.run_slot("verify"):
                    entered.set()
            except BaseException as e:  # noqa: BLE001 - reported below
                errors.append(repr(e))

        t = threading.Thread(target=request, daemon=True)
        t.start()
        time.sleep(1.0)
        blocked = not entered.is_set()
        live.kill()
        live.wait()
        assert blocked and not errors, f"entered while a live holder had the only slot (errors {errors})"
        assert entered.wait(5), f"still waiting after the holder exited (errors {errors})"
        t.join(5)
    finally:
        live.kill()
        live.wait()


# --- BR-14 sweep (decision logic) ---------------------------------------------------------------
SELF_ID = 9
HOLDERS = ["alive", "dead", "pid reused", "alive, start unreadable", "legacy pid alive", "legacy pid dead",
           "unreadable file"]
SUITES = ["none", "alive", "dead", "pid reused", "start missing", "alive, start unreadable"]
BRANCHES = ["nt", "linux", "posix"]
PIDS = {"holder": 5001, "holder_reused": 5002, "holder_dead": 5003, "suite": 5004, "suite_reused": 5005,
        "suite_dead": 5006, "holder_unreadable": 5007, "suite_unreadable": 5008}


def _token(branch: str, ident: int) -> str:
    if branch == "nt":
        return str(133_000_000_000_000_000 + ident * 1_000_003)
    if branch == "linux":
        return str(500_000 + ident * 17)
    return f"Thu Nov 20 10:{ident:02d}:00 2026"


class FakeWorld:
    """Processes that exist (pid -> identity) and ones whose start can't be read, seen through one branch."""

    def __init__(self, branch: str):
        self.branch = branch
        self.ids = {PIDS["holder"]: 1, PIDS["holder_reused"]: 2, PIDS["suite"]: 4, PIDS["suite_reused"]: 5,
                    os.getpid(): SELF_ID}
        self.unreadable = {PIDS["holder_unreadable"], PIDS["suite_unreadable"]}
        procs = {p: (STILL_ACTIVE, int(_token("nt", i))) for p, i in self.ids.items()}
        self.k32 = FakeK32(procs, denied=self.unreadable)

    def read(self, path):
        m = re.search(r"/proc/(\d+)/stat$", str(path).replace("\\", "/"))
        pid = int(m.group(1)) if m else -1
        if pid in self.ids:
            return stat_line(pid, "python3", int(_token("linux", self.ids[pid])))
        raise FileNotFoundError(path)

    def kill(self, pid, sig):
        if pid not in self.ids and pid not in self.unreadable:
            raise ProcessLookupError(pid)

    def run(self, argv):
        pid = int(argv[-1])
        if pid in self.ids:
            return 0, _token("posix", self.ids[pid]) + "\n"
        return 1, ""

    def proc_start(self, pid):
        if self.branch == "nt":
            return H._start_windows(pid, k32=self.k32)
        if self.branch == "linux":
            return H._start_linux(pid, read=self.read)
        return H._start_ps(pid, run=self.run)


def _lock_content(branch: str, holder: str, suite: str) -> tuple[str, bool]:
    """(file content, whether the holder counts as alive by BR-2). On the nt branch the "start unreadable"
    holder is a pid this user can't open (access denied) with a recorded start: dead (BR-2, RV-14)."""
    tok = lambda i: _token(branch, i)  # noqa: E731
    holder_rec = {"alive": ({"pid": PIDS["holder"], "start": tok(1)}, True),
                  "dead": ({"pid": PIDS["holder_dead"], "start": tok(3)}, False),
                  "pid reused": ({"pid": PIDS["holder_reused"], "start": tok(77)}, False),
                  "alive, start unreadable": ({"pid": PIDS["holder_unreadable"], "start": tok(7)}, branch != "nt")}
    suite_rec = {"none": {}, "alive": {"suite_pid": PIDS["suite"], "suite_start": tok(4)},
                 "dead": {"suite_pid": PIDS["suite_dead"], "suite_start": tok(6)},
                 "pid reused": {"suite_pid": PIDS["suite_reused"], "suite_start": tok(78)},
                 "start missing": {"suite_pid": PIDS["suite"]},
                 "alive, start unreadable": {"suite_pid": PIDS["suite_unreadable"], "suite_start": tok(8)}}[suite]
    if holder in holder_rec:
        rec, alive = holder_rec[holder]
        rec = dict(rec, repo="repo-z", task="task-z", gate="verify", since=time.time() - 30, **suite_rec)
        return json.dumps(rec), alive
    if holder == "legacy pid alive":
        return str(PIDS["holder"]), True
    if holder == "legacy pid dead":
        return str(PIDS["holder_dead"]), False
    # a record cut short while it was written: it names the suite, but nothing in it can be trusted
    full = json.dumps(dict({"pid": PIDS["holder_dead"], "start": tok(3)}, **suite_rec))
    return full[: max(5, len(full) - 7)], False


def _sweep_cases() -> list:
    out = []
    for branch in BRANCHES:
        for holder in HOLDERS:
            if branch == "linux" and "unreadable" in holder and holder != "unreadable file":
                continue  # /proc has no "exists but start unreadable" state
            for suite in SUITES:
                if branch == "linux" and suite == "alive, start unreadable":
                    continue
                out.append((branch, holder, suite))
    return out


def test_br14_orphan_decision_sweep(hroot, monkeypatch, capsys):
    """AC-10 AC-2 BR-14 [sweep] BR-2: full cross product of lock holder {alive, dead, pid reused with a different
    start, alive with an unreadable start, legacy plain-pid record of a live pid, of a dead pid, unreadable
    (cut-short) file} x recorded suite {none, alive, dead, pid reused, suite_start missing, alive with an
    unreadable start} x OS branch {nt (fake kernel32), linux (fake /proc), other POSIX (fake kill/ps)}; the
    linux branch has no "start unreadable" state, so 114 cases. kill_tree is a recorder.
    Oracle: the suite is stopped (kill_tree once, with suite_pid, and the `stopped an orphaned suite ...`
    line) exactly when the holder is dead by BR-2 AND the recorded suite pid is alive with the same start;
    the lock is taken exactly when the holder isn't alive, else refused with the base message (exit 1)."""
    cases = _sweep_cases()
    assert len(cases) == 114
    killed: list = []
    current = {}

    def kill_tree(pid):  # records the call; the fake process then no longer exists
        killed.append(pid)
        current["world"].ids.pop(pid, None)
        current["world"].k32.procs.pop(pid, None)

    monkeypatch.setattr(H, "kill_tree", kill_tree)
    diffs = []
    for i, (branch, holder, suite) in enumerate(cases):
        world = current["world"] = FakeWorld(branch)
        monkeypatch.setattr(H, "proc_start", world.proc_start)
        monkeypatch.setattr(H.os, "kill", world.kill)
        content, alive = _lock_content(branch, holder, suite)
        is_json = holder in ("alive", "dead", "pid reused", "alive, start unreadable")
        expect_kill = (not alive) and is_json and suite == "alive"
        lock = hroot.tmp / "locks" / f"lock-{i}"
        lock.parent.mkdir(exist_ok=True)
        lock.write_text(content, encoding="utf-8")
        killed.clear()
        capsys.readouterr()
        try:
            H._take_lock(lock)
            got = "taken"
            code = None
        except SystemExit as e:
            got, code = "refused", e.code
        except BaseException as e:  # noqa: BLE001 - recorded as a difference
            got, code = f"raised {type(e).__name__}: {e}", None
        out = "".join(capsys.readouterr())
        problems = []
        want = "refused" if alive else "taken"
        if got != want:
            problems.append(f"{got} (expected {want})")
        if got == "refused" and (code != 1 or "another test or probe run of this task is in progress" not in out):
            problems.append(f"refused with exit {code} / without the base message")
        if got == "taken" and H.read_record(lock).get("pid") != os.getpid():
            problems.append(f"lock not rewritten for this process: {lock.read_text(encoding='utf-8')[:80]!r}")
        if killed != ([PIDS["suite"]] if expect_kill else []):
            problems.append(f"kill_tree calls {killed} (expected {[PIDS['suite']] if expect_kill else []})")
        said = ORPHAN.format(pid=PIDS["suite"]) in out
        if said != expect_kill:
            problems.append(f"orphan message printed={said} (expected {expect_kill})")
        if problems:
            diffs.append(f"[{branch} | holder {holder} | suite {suite}] " + "; ".join(problems))
    print(f"BR-14 sweep: {len(diffs)} of {len(cases)} cases differ from the oracle")
    assert not diffs, f"{len(diffs)} of {len(cases)} differ; first ones:\n  " + "\n  ".join(diffs[:8])


# --- AC-10: end to end ------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def snaps(tmp_path_factory):
    return T.chain(tmp_path_factory.getbasetemp(), "new")


def _sleeping_suite_ctl(pid_file: Path, seconds: int) -> str:
    return (f"import os, time\nif TEST == 't':\n    open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
            f"    time.sleep({seconds})\n")


def test_ac10_killed_command_orphan_stopped_by_next_run(snaps, tmp_path, tmp_path_factory):
    """AC-10 BR-14 AC-2: `verify` runs in a child process whose suite writes its pid and sleeps; the harness
    child is killed hard and the suite keeps running (an orphan). The next `verify` of that task says
    `stopped an orphaned suite from a killed run (pid N)` with the suite's pid, the suite is gone, and the
    lock is taken at once: that verify runs the (now fast) suite and passes."""
    toy = T.clone(snaps["green"], tmp_path / "lab", tmp_path_factory.getbasetemp())
    pid_file = tmp_path / "suite.pid"
    toy.ctl(_sleeping_suite_ctl(pid_file, 60))
    first = toy.start("verify")
    suite_pid = None
    try:
        assert T.wait_for(pid_file.exists, 60), "the suite never started"
        suite_pid = int(T.wait_for(lambda: pid_file.read_text().strip(), 5))
        first.kill()
        first.wait(30)
        assert H.proc_start(suite_pid) is not None, "the suite died with its command: no orphan to test"
        toy.ctl("")
        t0 = time.monotonic()
        rc, out = toy.h("verify")
        took = time.monotonic() - t0
        assert ORPHAN.format(pid=suite_pid) in out, out
        assert T.wait_for(lambda: H.proc_start(suite_pid) is None, 10), "the orphaned suite is still running"
        assert rc == 0, out
        assert "another test or probe run" not in out
        assert took < 60, f"the next verify took {took:.0f}s"
        assert not toy.lock.exists()
    finally:
        first.kill()
        T.kill_pid(suite_pid)


def test_ac10_live_run_suite_never_stopped(snaps, tmp_path, tmp_path_factory):
    """AC-10 BR-14 AC-2: while the first `verify` (and its command) is alive, a second `verify` of the same
    task is refused with the base message, says nothing about orphans, and the first run's suite keeps
    running."""
    toy = T.clone(snaps["green"], tmp_path / "lab", tmp_path_factory.getbasetemp())
    pid_file = tmp_path / "suite.pid"
    toy.ctl(_sleeping_suite_ctl(pid_file, 60))
    first = toy.start("verify")
    suite_pid = None
    try:
        assert T.wait_for(pid_file.exists, 60), "the suite never started"
        suite_pid = int(T.wait_for(lambda: pid_file.read_text().strip(), 5))
        tok = H.proc_start(suite_pid)
        rc, out = toy.h("verify")
        assert rc == 1, out
        assert REFUSED.format(pid=first.pid) in out, out
        assert "orphaned suite" not in out
        assert first.poll() is None, "the first command died"
        assert tok and H.proc_start(suite_pid) == tok, "the live run's suite was stopped"
    finally:
        first.kill()
        T.kill_pid(suite_pid)
