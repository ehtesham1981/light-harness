# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""Regression sweeps for review round 3 of issues #16/#25/#30 and the round-4 rules (§8.4), one per rule or
defect class:

- BR-2 [sweep]: the holder decision table over record role × record form × process at that pid × owner
  user × OS branch, through the real `_take_lock`, `run_slot` and `sweep_temp_root` (RV-14, Q-19, Q-20).
- BR-14 `Otherwise:` (RV-15's class): holder start form × holder state × suite pid × suite start × suite
  state, through the real `_take_lock`, plus a real-process subset.
- BR-5 `Otherwise:` (Q-19, RV-13/RV-14 classes): damaged or foreign slot entries × the pid's process ×
  slot count × requesters, with real requester processes.
- BR-9 `Otherwise:` (RV-16, RV-17, Q-20 classes): contents the suite locks × outcome × suite/probe; run
  roots and fallback roots; owner records across users.
- RV-13: `waiting_line` never raises, over `since` × display fields × `now`.
- RV-14: a pid this user can't open, in every record role, and the `DENIED` marker.

OS branches run on every OS through fakes (a fake kernel32, a fake /proc reader, a fake kill/ps). Pids
this user can't open are faked everywhere and, on Windows, also taken from real processes found with
`OpenProcess` (none found: only the fake ones run; no skip). Machine-wide state stays in pytest's temp
folder (tests/lh_machine.py), and `tempfile`'s temp folder points there too, so no real slot folder, git
config, `<drive>:\\lh-tmp` or user TEMP is touched. Real processes are `sys.executable` children only.
"""
from __future__ import annotations

import contextlib
import csv
import errno
import functools
import itertools
import json
import math
import os
import queue
import re
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
from lh_machine import TEMP_ROOT_ENV, isolate, machine_env  # noqa: E402

STILL_ACTIVE = 259
REFUSED = "another test or probe run of this task is in progress"
ORPHAN = "stopped an orphaned suite from a killed run (pid {pid})"
WAITING = "waiting for a run slot: "
SELF_ID = 9
BRANCHES = ["nt", "linux", "posix"]


def this_user() -> str:
    """This user as the harness names it in owner records (§8.2 `_user`), computed independently."""
    if os.name == "nt":
        return f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lower()
    return str(os.getuid())


def other_user() -> str:
    return "otherdomain\\someone-else" if os.name == "nt" else str(os.getuid() + 4242)


def problems_report(name: str, diffs: list, total: int) -> str:
    print(f"{name}: {len(diffs)} of {total} inputs differ from the oracle")
    return f"{len(diffs)} of {total} differ; first ones:\n  " + "\n  ".join(diffs[:10])


@pytest.fixture
def hroot(monkeypatch, tmp_path):
    env = isolate(monkeypatch, tmp_path / "machine", slots=1)
    repo = T.mini_repo(tmp_path / "repo")
    T.point(monkeypatch, H, repo)
    systemp = tmp_path / "systemp"
    systemp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(systemp))  # the user's TEMP, as the harness sees it
    monkeypatch.setattr(H, "SLOT_POLL", 0.05)
    monkeypatch.setattr(H, "RUN_LOAD", {})
    monkeypatch.setattr(H, "_ROOT_FALLBACK", [])
    monkeypatch.setattr(H, "_TEMP_WARNED", [])
    H.LEFTOVERS.clear()
    return SimpleNamespace(tmp=tmp_path, repo=repo, root=Path(env[TEMP_ROOT_ENV]), env=env, systemp=systemp)


def fast_identity(monkeypatch):
    """`_holder_record` asks git for the repo and task on every claim (about 15 ms); the sweeps below make
    thousands of claims, so those two git lookups (not under test) answer from constants."""
    monkeypatch.setattr(H, "main_checkout", lambda: Path("repo-x"))
    monkeypatch.setattr(H, "task_slug", lambda: "task-x")


class Kills:
    """kill_tree stand-in: records every call (no process is killed)."""

    def __init__(self, world_ref=None):
        self.calls: list = []
        self.world_ref = world_ref

    def __call__(self, pid):
        self.calls.append(pid)
        if self.world_ref is not None and self.world_ref.get("w") is not None:
            self.world_ref["w"].forget(pid)


# --- fakes for the three OS branches (the FakeK32 shape of tests/test_run_locks.py, copied) -------
def _target(arg):
    if hasattr(arg, "_obj"):
        return arg._obj
    if hasattr(arg, "contents"):
        return arg.contents
    return arg


def _val(x):
    return getattr(x, "value", x)


class FakeK32:
    """kernel32 stand-in. procs: {pid: (exit code, creation FILETIME)}; denied: pids whose OpenProcess fails
    with ERROR_ACCESS_DENIED (5); notimes: pids whose GetProcessTimes fails (start unreadable); any other
    pid fails with ERROR_INVALID_PARAMETER (87)."""

    def __init__(self, procs: dict, denied=(), notimes=()):
        self.procs, self.denied, self.notimes = dict(procs), set(denied), set(notimes)
        self.err = 0
        self.handles: dict = {}

    def OpenProcess(self, access, inherit, pid):  # noqa: N802 - kernel32's names
        pid = int(_val(pid))
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
        if pid is None or pid in self.notimes:
            self.err = 5
            return 0
        ft = self.procs[pid][1]
        t = _target(creation)
        if hasattr(t, "dwLowDateTime"):
            t.dwLowDateTime, t.dwHighDateTime = ft & 0xFFFFFFFF, ft >> 32
        else:
            t.value = ft
        return 1

    def CloseHandle(self, h):  # noqa: N802
        return 1

    def GetLastError(self):  # noqa: N802
        return self.err


def stat_line(pid: int, start: int, state: str = "S") -> str:
    fields = [state] + [str(i) for i in range(4, 22)] + [str(start)] + ["0"] * 30
    return f"{pid} (python3) " + " ".join(fields) + "\n"


def token(branch: str, ident: int) -> str:
    if branch == "nt":
        return str(133_000_000_000_000_000 + ident * 1_000_003)
    if branch == "linux":
        return str(500_000 + ident * 17)
    return f"Thu Nov 20 10:{ident:02d}:00 2026"


class World:
    """Processes seen through one OS branch. alive: {pid: identity}; exited: processes that have exited
    (zombies on linux); denied: pids this user can't open (nt only); unreadable: pids that exist but whose
    start can't be read. This process is always alive."""

    def __init__(self, branch: str, alive: dict, exited=(), denied=(), unreadable=()):
        self.branch = branch
        self.alive = dict(alive)
        self.alive[os.getpid()] = SELF_ID
        self.exited, self.unreadable = set(exited), set(unreadable)
        self.denied = set(denied) if branch == "nt" else set()

    def forget(self, pid):
        self.alive.pop(pid, None)
        self.unreadable.discard(pid)
        self.denied.discard(pid)

    def _k32(self):
        procs = {p: (STILL_ACTIVE, int(token("nt", i))) for p, i in self.alive.items()}
        procs.update({p: (0, int(token("nt", 1))) for p in self.exited})
        procs.update({p: (STILL_ACTIVE, 1) for p in self.unreadable})
        return FakeK32(procs, self.denied, notimes=self.unreadable)

    def read(self, path):
        m = re.search(r"/proc/(\d+)/stat$", str(path).replace("\\", "/"))
        pid = int(m.group(1)) if m else -1
        if pid in self.alive:
            return stat_line(pid, int(token("linux", self.alive[pid])))
        if pid in self.exited:
            return stat_line(pid, int(token("linux", 1)), state="Z")
        if pid in self.unreadable:
            return f"{pid} (python3) S 1 2 3\n"  # too few fields: start unreadable
        raise FileNotFoundError(path)

    def kill(self, pid, sig):
        if pid in self.alive or pid in self.unreadable:
            return
        if pid in self.exited:
            raise OSError(errno.EINVAL, "exited")
        raise ProcessLookupError(pid)

    def run(self, argv):
        pid = int(argv[-1])
        if pid in self.alive:
            return 0, token("posix", self.alive[pid]) + "\n"
        return 1, ""

    def proc_start(self, pid):
        if self.branch == "nt":
            return H._start_windows(pid, k32=self._k32())
        if self.branch == "linux":
            return H._start_linux(pid, read=self.read)
        real = os.kill
        os.kill = self.kill  # _start_ps checks existence with os.kill(pid, 0)
        try:
            return H._start_ps(pid, run=self.run)
        finally:
            os.kill = real


class NoOpenK32:
    """kernel32 whose OpenProcess always fails with access denied (error 5)."""

    def OpenProcess(self, *a):  # noqa: N802
        return 0

    def GetLastError(self):  # noqa: N802
        return 5

    def CloseHandle(self, h):  # noqa: N802
        return 1


FAKE_DENIED = 2_000_000_004  # a pid no test machine has; proc_start is routed to NoOpenK32 for it


def with_fake_denied(monkeypatch, pids=(FAKE_DENIED,)):
    """proc_start as the real one, except that `pids` are processes this user can't open (Windows branch
    with a kernel32 that denies access)."""
    real = H.proc_start
    denied = set(pids)
    monkeypatch.setattr(H, "proc_start", lambda pid: H._start_windows(pid, k32=NoOpenK32())
                        if pid in denied else real(pid))


@functools.lru_cache(maxsize=1)
def unopenable_pids(limit: int = 2) -> tuple:
    """Windows only: up to `limit` pids of long-lived processes this user can't open (OpenProcess with
    PROCESS_QUERY_LIMITED_INFORMATION fails with access denied), found as probe RV-14.1 finds them but
    through kernel32 directly, so the choice doesn't depend on the harness under test. Elsewhere: ()."""
    if os.name != "nt":
        return ()
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, encoding="utf-8",
                             errors="replace", timeout=60, stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return ()
    rows = [r for r in csv.reader(out.splitlines()) if len(r) > 1 and r[1].isdigit()]
    prefer = ("csrss.exe", "wininit.exe", "services.exe", "smss.exe", "lsass.exe", "winlogon.exe")
    rows.sort(key=lambda r: (r[0].lower() not in prefer, int(r[1])))
    found = []
    for r in rows:
        pid = int(r[1])
        if pid in (0, 4):
            continue
        h = k.OpenProcess(0x1000, False, pid)
        if h:
            k.CloseHandle(h)
            continue
        if ctypes.get_last_error() == 5:
            found.append(pid)
        if len(found) >= limit:
            break
    return tuple(found)


def dead_child() -> tuple:
    """(pid, start token) of a sys.executable child that has exited and been reaped."""
    c = T.sleeper(30)
    tok = H.proc_start(c.pid)
    c.kill()
    c.wait()
    T.wait_for(lambda: H.proc_start(c.pid) is None, 5)
    return c.pid, str(tok)


def make_link(link: Path, target: Path):
    """A junction (Windows) or symlink to the folder `target`."""
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(str(target), str(link), target_is_directory=True)


def unlock_tree(path: Path):
    """Cleanup only: give everything under `path` its permissions back so pytest can delete it."""
    if os.name == "nt":
        subprocess.run(["icacls", str(path), "/reset", "/T", "/C", "/Q"], capture_output=True,
                       stdin=subprocess.DEVNULL, timeout=120)
    else:
        for dirpath, dirnames, _ in os.walk(path):
            for d in dirnames:
                with contextlib.suppress(OSError):
                    os.chmod(os.path.join(dirpath, d), 0o700)


class Blocked(BaseException):
    """Raised by the scan-counting slot_count once run_slot scans a second time: it didn't get a slot on
    its first scan, so it is waiting."""


def scan_counter(monkeypatch, slots: int = 1):
    """slot_count for run_slot: `slots` on the first scan of a claim, Blocked on the second. A free or dead
    holder's slot is taken on the first scan; a live holder makes run_slot print the waiting line, sleep and
    scan again. Returns the counter dict (reset "n" to 0 before each claim)."""
    scans = {"n": 0}

    def slot_count():
        scans["n"] += 1
        if scans["n"] > 1:
            raise Blocked()
        return slots

    monkeypatch.setattr(H, "slot_count", slot_count)
    monkeypatch.setattr(H, "SLOT_POLL", 0.0)
    return scans


def claim_once(scans) -> str:
    """'taken' if run_slot got a slot on its first scan, 'blocked' if it waited, else what it raised."""
    scans["n"] = 0
    try:
        with H.run_slot("verify"):
            return "taken"
    except Blocked:
        return "blocked"
    except BaseException as e:  # noqa: BLE001 - recorded as a difference
        return f"raised {type(e).__name__}: {e}"


def clear_slots():
    d = H.slot_dir()
    d.mkdir(parents=True, exist_ok=True)
    for p in d.glob("slot-*"):
        if p.is_dir() and not p.is_symlink():
            for c in p.iterdir():
                c.unlink()
            p.rmdir()
        else:
            p.unlink()
    return d


def take_lock_outcome(lock: Path, capsys) -> tuple:
    capsys.readouterr()
    try:
        H._take_lock(lock)
        got, code = "taken", None
    except SystemExit as e:
        got, code = "refused", e.code
    except BaseException as e:  # noqa: BLE001 - recorded as a difference
        got, code = f"raised {type(e).__name__}: {e}", None
    return got, code, "".join(capsys.readouterr())


# ================================================================ BR-2 [sweep]: the decision table
BR2_PIDS = {"none": 6001, "exited": 6002, "match": 6003, "other": 6004, "denied": 6005, "unreadable": 6006}
BR2_STATES = ["none", "exited", "match", "other", "denied", "unreadable"]
BR2_FORMS = ["json", "start-empty", "start-padded", "start-missing", "start-list", "start-dict", "start-true",
             "start-int", "start-float", "pid-str", "pid-float", "pid-bool", "pid-null", "pid-0", "pid-neg",
             "pid-2^31", "pid-10^30", "legacy", "undecodable", "truncated"]
BR2_USERS = ["this", "other", "missing"]


def br2_world(branch: str) -> World:
    return World(branch, {BR2_PIDS["match"]: 3, BR2_PIDS["other"]: 4}, exited={BR2_PIDS["exited"]},
                 denied={BR2_PIDS["denied"]}, unreadable={BR2_PIDS["unreadable"]})


def br2_bytes(form: str, pid: int, tok: str, extra: dict) -> bytes:
    """A record of `form` for a holder with pid `pid` and recorded start `tok`."""
    if form == "legacy":
        return str(pid).encode()
    if form == "undecodable":
        return b"\xff\xfe" + json.dumps(dict({"pid": pid, "start": tok}, **extra)).encode()
    rec = dict({"pid": pid, "start": tok}, **extra)
    if form == "truncated":
        s = json.dumps(rec)
        return s[: len(s) - 7].encode()
    num = tok.isdigit()
    starts = {"start-empty": "", "start-padded": f" {tok}\n", "start-list": [tok], "start-dict": {"t": tok},
              "start-true": True, "start-int": int(tok) if num else 12345,
              "start-float": float(tok) if num else 1.5}
    pids = {"pid-str": str(pid), "pid-float": float(pid), "pid-bool": True, "pid-null": None, "pid-0": 0,
            "pid-neg": -1, "pid-2^31": 2 ** 31, "pid-10^30": 10 ** 30}
    if form in starts:
        rec["start"] = starts[form]
    elif form == "start-missing":
        del rec["start"]
    elif form in pids:
        rec["pid"] = pids[form]
    return json.dumps(rec).encode()


def br2_expected_alive(role: str, form: str, state: str, user: str | None) -> bool:
    """BR-2's decision table (with Q-19 for slots, Q-20 for owners, Q-25: denial is Windows-only), written
    from the rule's text, not from the code."""
    exists = state in ("match", "other", "denied", "unreadable")
    pid_usable = form in ("json", "legacy") or form.startswith("start-")
    if not pid_usable:
        # lock and slot: a pid that isn't usable is a dead holder's. An owner record that doesn't give a
        # usable pid falls back to the pid in the folder's name (BR-9 Otherwise, RV-9), which is this pid.
        return exists if role == "owner" else False
    if not exists:
        return False
    kind = {"json": "tok", "start-padded": "tok", "start-empty": "empty", "start-missing": "missing",
            "legacy": "missing"}.get(form, "other")
    if role == "slot" and kind in ("missing", "other"):
        return False  # Q-19: a slot record without a string start is a dead holder's
    if kind != "tok":
        return True  # no usable start: alive while the pid exists
    if state == "denied":
        return user != "this" if role == "owner" else False  # RV-14; Q-20 for another user's folder
    if state == "unreadable":
        return True
    return state == "match"


def br2_cases() -> list:
    out = []
    for branch in BRANCHES:
        for state in BR2_STATES:
            if state == "denied" and branch != "nt":
                continue  # Q-25: only Windows has a process this user can't open
            for form in BR2_FORMS:
                for role in ("lock", "slot", "owner"):
                    if role == "slot" and form == "legacy":
                        continue  # there are no legacy slot files (Q-19)
                    for user in (BR2_USERS if role == "owner" else [None]):
                        out.append((branch, role, form, state, user))
    return out


def test_br2_holder_decision_sweep(hroot, monkeypatch, capsys):
    """AC-2 BR-2 [sweep] RV-14 RV-15 Q-19 Q-20: full cross product of record role {run lock via the real
    _take_lock, slot via the real run_slot (one slot; a claim that doesn't get the slot on its first scan is
    waiting), run-folder owner via the real sweep_temp_root} x record form {harness JSON with a string start,
    start "", padded " tok\\n", start missing, start a list / dict / true / int / float, pid a str / float /
    bool / null / 0 / -1 / 2**31 / 10**30, legacy plain pid (lock and owner only), undecodable bytes,
    truncated JSON} x process at that pid {none, exited or zombie, the recording process, this user's
    process with another start, a process this user can't open (nt only), exists with an unreadable start}
    x owner `user` {this user's, another user's, missing} (owners only) x OS branch {nt with a fake
    kernel32, linux with a fake /proc, other POSIX with fake kill/ps}.
    Oracle: BR-2's table: dead if the pid isn't usable (owners then fall back to the name's pid) or no
    process has it; a slot without a string start is dead (Q-19); a lock or owner without a usable start is
    alive while the pid exists; a record with a start whose pid can't be opened is dead (owners: only when
    the record's user is this user's, Q-20); an unreadable current start is alive; else alive iff the starts
    match. Checks: lock taken iff dead, else refused with the base message; slot taken at once iff dead,
    else the waiting line; folder (and owner file) removed iff dead. Nothing raises; nothing is killed."""
    fast_identity(monkeypatch)
    kills = Kills()
    monkeypatch.setattr(H, "kill_tree", kills)
    scans = scan_counter(monkeypatch)
    cases = br2_cases()
    me, other = this_user(), other_user()
    diffs = []
    lock = hroot.tmp / "locks" / "base-lock-x"
    lock.parent.mkdir()
    owner_cases: dict = {b: [] for b in BRANCHES}
    for idx, (branch, role, form, state, user) in enumerate(cases):
        world = br2_world(branch)
        monkeypatch.setattr(H, "proc_start", world.proc_start)
        pid, tok = BR2_PIDS[state], token(branch, 3)
        alive = br2_expected_alive(role, form, state, user)
        label = f"[{branch} | {role} | {form} | {state}" + (f" | user {user}]" if user else "]")
        if role == "owner":
            owner_cases[branch].append((idx, form, state, user, alive, label))
            continue
        extra = {"repo": "repo-z", "task": "task-z", "gate": "verify", "since": time.time() - 30}
        content = br2_bytes(form, pid, tok, extra)
        if role == "lock":
            lock.write_bytes(content)
            got, code, out = take_lock_outcome(lock, capsys)
            want = "refused" if alive else "taken"
            if got != want:
                diffs.append(f"{label} lock {got} (expected {want})")
            elif got == "refused" and (code != 1 or f"{REFUSED} (pid {pid})" not in out):
                diffs.append(f"{label} refused with exit {code} / without the base message: {out[-200:]!r}")
            elif got == "taken" and H.read_record(lock).get("pid") != os.getpid():
                diffs.append(f"{label} lock not rewritten for this process")
            lock.unlink(missing_ok=True)
        else:
            d = clear_slots()
            (d / "slot-0.json").write_bytes(content)
            capsys.readouterr()
            got = claim_once(scans)
            out = capsys.readouterr().out
            want = "blocked" if alive else "taken"
            if got != want:
                diffs.append(f"{label} slot {got} (expected {want})")
            elif got == "blocked" and WAITING not in out:
                diffs.append(f"{label} waited without printing the waiting line")
            elif got == "taken" and WAITING in out:
                diffs.append(f"{label} printed a waiting line for a dead holder")
    n_owner = 0
    for branch, items in owner_cases.items():
        world = br2_world(branch)
        monkeypatch.setattr(H, "proc_start", world.proc_start)
        root = hroot.tmp / f"owners-{branch}"
        root.mkdir()
        expected_removed = set()
        for idx, form, state, user, alive, label in items:
            pid = BR2_PIDS[state]
            name = f"o{idx}-{pid}"
            (root / name).mkdir()
            (root / name / "f.txt").write_text("x", encoding="utf-8")
            extra = {} if user == "missing" else {"user": me if user == "this" else other}
            (root / f"{name}.owner").write_bytes(br2_bytes(form, pid, token(branch, 3), extra))
            if not alive:
                expected_removed.add(name)
        try:
            removed = H.sweep_temp_root(root)
        except BaseException as e:  # noqa: BLE001
            diffs.append(f"[{branch} | owner] sweep_temp_root raised {type(e).__name__}: {e}")
            removed = []
        for idx, form, state, user, alive, label in items:
            n_owner += 1
            name = f"o{idx}-{BR2_PIDS[state]}"
            kept, owner_kept = (root / name).exists(), (root / f"{name}.owner").exists()
            if kept != alive or owner_kept != alive:
                diffs.append(f"{label} folder kept={kept} owner kept={owner_kept} (expected {alive})")
        if set(removed) != expected_removed:
            diffs.append(f"[{branch} | owner] sweep_temp_root returned {len(removed)} names, expected "
                         f"{len(expected_removed)}: extra {sorted(set(removed) - expected_removed)[:3]}, "
                         f"missing {sorted(expected_removed - set(removed))[:3]}")
    if kills.calls:
        diffs.append(f"kill_tree called {kills.calls[:5]} (nothing may be killed in this sweep)")
    assert n_owner + sum(1 for c in cases if c[1] != "owner") == len(cases)
    assert not diffs, problems_report("BR-2 sweep", diffs, len(cases))


# ================================================================ BR-14 Otherwise: (RV-15's class)
B14 = {"holder:alive": 7001, "holder:dead": 7002, "holder:reused": 7003, "holder:denied": 7004,
       "suite:alive": 7011, "suite:dead": 7012, "suite:reused": 7013, "suite:denied": 7014}
HSTART = ["match", "padded", "empty", "missing", "list", "dict", "true", "int", "float"]
HSTATES = ["alive", "dead", "reused", "denied"]
SPIDS = ["missing", "holder", "self", "other:alive", "other:dead", "other:reused", "other:denied", "str", "bool",
         "range"]
SSTART = ["match", "padded", "empty", "missing", "list", "int", "strmatch"]


def b14_world(branch: str) -> World:
    return World(branch, {B14["holder:alive"]: 1, B14["holder:reused"]: 2, B14["suite:alive"]: 11,
                          B14["suite:reused"]: 12},
                 denied={B14["holder:denied"], B14["suite:denied"]})


def b14_start(form: str, tok: str):
    num = tok.isdigit()
    return {"match": tok, "padded": f" {tok}\n", "empty": "", "list": [tok], "dict": {"t": tok}, "true": True,
            "int": 12345, "float": float(tok) if num else 1.5,
            "strmatch": int(tok) if num else [tok]}[form]


def b14_cases() -> list:
    out = []
    for branch in BRANCHES:
        for hs in HSTART:
            for hstate in HSTATES:
                if hstate == "denied" and branch != "nt":
                    continue
                for sp in SPIDS:
                    if sp == "other:denied" and branch != "nt":
                        continue
                    for ss in SSTART:
                        out.append((branch, hs, hstate, sp, ss))
    return out


def b14_record(branch, hs, hstate, sp, ss) -> tuple:
    """(lock record, holder pid, suite pid the oracle would kill or None)."""
    hpid = B14[f"holder:{hstate}"]
    rec = {"pid": hpid, "repo": "repo-z", "task": "task-z", "gate": "verify", "since": time.time() - 30}
    if hs != "missing":
        rec["start"] = b14_start(hs, token(branch, 1))
    # the suite's recorded start is the one its pid currently has where a naive check would match
    holder_now = {"alive": token(branch, 1), "reused": token(branch, 2)}.get(hstate, token(branch, 1))
    spid, stok = {"missing": (None, token(branch, 11)), "holder": (hpid, holder_now),
                  "self": (os.getpid(), token(branch, SELF_ID)), "str": (str(B14["suite:alive"]), token(branch, 11)),
                  "bool": (True, token(branch, 11)), "range": (2 ** 31, token(branch, 11))}.get(
        sp, (B14.get(f"suite:{sp[6:]}"), token(branch, 11)))
    if sp != "missing":
        rec["suite_pid"] = spid
    if ss != "missing":
        rec["suite_start"] = b14_start(ss, stok)
    return rec, hpid


def b14_expected(hs, hstate, sp, ss) -> tuple:
    """(holder alive, kill expected) by BR-2 and BR-14's Otherwise line."""
    if hstate == "dead":
        alive = False
    elif hs in ("match", "padded"):
        alive = hstate == "alive"  # reused: another start; denied with a start: dead (RV-14)
    else:
        alive = True  # a start that isn't a usable string is unknown: alive while the pid exists (RV-15)
    kill = (not alive) and sp == "other:alive" and ss in ("match", "padded")
    return alive, kill


def test_br14_otherwise_sweep(hroot, monkeypatch, capsys):
    """AC-10 AC-2 BR-14 [sweep] BR-2 RV-15: full cross product through the real _take_lock of holder start
    form {matching str, padded str, "", missing, list, dict, true, int, float} x holder state {alive, dead,
    pid reused, can't be opened (nt only)} x suite_pid {missing, the holder's own pid, this process's pid,
    another pid that is alive / dead / reused / can't be opened (nt only), a str, a bool, out of range} x
    suite_start {matching str, padded, "", missing, list, int, a non-str whose str() matches} x OS branch
    {nt fake kernel32, linux fake /proc, other POSIX fake kill/ps}; kill_tree is a recorder.
    Oracle (BR-14 Otherwise + BR-2): the holder is alive iff it exists and its start is unknown (not a
    usable str) or matches (denied with a start: dead); the lock is taken iff the holder isn't alive, else
    refused with the base message. A suite is stopped (kill_tree once with its pid, and the `stopped an
    orphaned suite` line) only when the holder is dead, suite_pid is a usable int that is neither the
    holder's nor this process's, suite_start is a non-empty str, and the suite's current start, read from a
    process this user can open, equals it. Nothing raises."""
    fast_identity(monkeypatch)
    ref: dict = {}
    kills = Kills(ref)
    monkeypatch.setattr(H, "kill_tree", kills)
    cases = b14_cases()
    lock = hroot.tmp / "locks" / "base-lock-x"
    lock.parent.mkdir()
    diffs = []
    for branch, hs, hstate, sp, ss in cases:
        world = ref["w"] = b14_world(branch)
        monkeypatch.setattr(H, "proc_start", world.proc_start)
        rec, hpid = b14_record(branch, hs, hstate, sp, ss)
        alive, kill = b14_expected(hs, hstate, sp, ss)
        lock.write_text(json.dumps(rec), encoding="utf-8")
        kills.calls.clear()
        got, code, out = take_lock_outcome(lock, capsys)
        label = f"[{branch} | holder start {hs} | holder {hstate} | suite_pid {sp} | suite_start {ss}]"
        problems = []
        want = "refused" if alive else "taken"
        if got != want:
            problems.append(f"{got} (expected {want})")
        elif got == "refused" and (code != 1 or f"{REFUSED} (pid {hpid})" not in out):
            problems.append(f"refused with exit {code} / without the base message")
        want_kills = [B14["suite:alive"]] if kill else []
        if kills.calls != want_kills:
            problems.append(f"kill_tree calls {kills.calls} (expected {want_kills})")
        said = "stopped an orphaned suite" in out
        if said != kill or (kill and ORPHAN.format(pid=B14["suite:alive"]) not in out):
            problems.append(f"orphan message printed={said} (expected {kill})")
        if problems:
            diffs.append(label + " " + "; ".join(problems))
        lock.unlink(missing_ok=True)
    assert not diffs, problems_report("BR-14 Otherwise sweep", diffs, len(cases))


@pytest.mark.parametrize("form", ["list", "dict", "true", "padded"])
def test_br14_otherwise_real_processes(hroot, capsys, form):
    """AC-10 BR-14 BR-2 RV-15 (real processes, RV-15.1's inputs): a live holder (a sys.executable child) whose
    lock record's start is a list, a dict, true or its real start padded with whitespace, and a live suite
    recorded with its exact pid and start: the holder counts as alive (an unknown or padded start), so the
    lock is refused with the base message and nothing is stopped: holder and suite still run with the same
    start afterwards. kill_tree is the real one."""
    holder = T.sleeper(60)
    suite = T.sleeper(60, start_new_session=os.name != "nt")
    try:
        htok, stok = H.proc_start(holder.pid), H.proc_start(suite.pid)
        assert htok and stok
        start = {"list": [htok], "dict": {"t": htok}, "true": True, "padded": f" {htok}\n"}[form]
        lock = hroot.tmp / "base-lock-real"
        T.write_json(lock, {"pid": holder.pid, "start": start, "repo": "r", "task": "t", "gate": "verify",
                            "since": time.time(), "suite_pid": suite.pid, "suite_start": stok})
        got, code, out = take_lock_outcome(lock, capsys)
        time.sleep(0.3)
        assert (got, code) == ("refused", 1), out
        assert f"{REFUSED} (pid {holder.pid})" in out
        assert "orphaned suite" not in out
        assert holder.poll() is None and suite.poll() is None
        assert H.proc_start(holder.pid) == htok and H.proc_start(suite.pid) == stok
    finally:
        for c in (holder, suite):
            c.kill()
            c.wait()


# ================================================================ BR-5 Otherwise: (Q-19)
WORKER = r'''
import contextlib, io, json, os, sys, time
from pathlib import Path
scripts = sys.argv[1]
fake_denied = {int(p) for p in sys.argv[2].split(",") if p}
sys.path.insert(0, scripts)
import harness as H
H.SLOT_POLL = 0.05
H.main_checkout = lambda: Path("requester")
H.task_slug = lambda: "w%d" % os.getpid()
real_proc_start = H.proc_start


class NoOpen:
    def OpenProcess(self, *a):
        return 0

    def GetLastError(self):
        return 5

    def CloseHandle(self, h):
        return 1


def proc_start(pid):
    if pid in fake_denied:
        return H._start_windows(pid, k32=NoOpen())
    return real_proc_start(pid)


H.proc_start = proc_start
OUT = sys.__stdout__
CUR = {"id": None}


def emit(**ev):
    ev["id"] = CUR["id"]
    OUT.write(json.dumps(ev) + "\n")
    OUT.flush()


class Lines(io.TextIOBase):
    def __init__(self):
        self.buf = ""

    def write(self, s):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            emit(ev="out", text=line)
        return len(s)


emit(ev="ready")
for raw in sys.stdin:
    cmd = json.loads(raw)
    CUR["id"] = cmd["id"]
    d, inside = Path(cmd["dir"]), Path(cmd["inside"])
    H.slot_dir = lambda d=d: d
    H.slot_count = lambda n=cmd["slots"]: n
    H.RUN_LOAD.clear()
    try:
        with contextlib.redirect_stdout(Lines()):
            with H.run_slot("verify"):
                me = inside / str(os.getpid())
                me.touch()
                emit(ev="in", count=len(os.listdir(inside)))
                time.sleep(cmd["hold"])
                me.unlink()
        emit(ev="done")
    except BaseException as e:
        emit(ev="error", text="%s: %s" % (type(e).__name__, e))
'''


class Workers:
    """`n` requester processes that each run one run_slot claim per command, reporting events."""

    def __init__(self, n: int, env: dict, cwd: Path, fake: str):
        self.n, self.env, self.cwd, self.fake = n, env, cwd, fake
        self.q: queue.Queue = queue.Queue()
        self.procs: list = [None] * n
        for i in range(n):
            self.start(i)

    def start(self, i: int):
        p = subprocess.Popen([sys.executable, "-u", "-c", WORKER, str(SCRIPTS), self.fake], cwd=str(self.cwd),
                             env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, encoding="utf-8", errors="replace", bufsize=1)
        self.procs[i] = p
        threading.Thread(target=self._read, args=(i, p), daemon=True).start()

    def _read(self, i, p):
        for line in p.stdout:
            try:
                self.q.put((i, json.loads(line)))
            except ValueError:
                pass

    def send(self, i: int, cmd: dict):
        self.procs[i].stdin.write(json.dumps(cmd) + "\n")
        self.procs[i].stdin.flush()

    def restart(self, i: int):
        with contextlib.suppress(OSError):
            self.procs[i].kill()
        self.procs[i].wait()
        self.start(i)

    def close(self):
        for p in self.procs:
            with contextlib.suppress(OSError):
                p.stdin.close()
            with contextlib.suppress(OSError):
                p.kill()
            p.wait()


DISPLAY_BAD = [("list", [1]), ("int", 7), ("empty", "")]


def b5_seeds(fake_pids: tuple) -> list:
    """(name, kind, builder args, has pid dimension). kind: bytes, record or folder."""
    seeds = [("bytes undecodable", "bytes", b"\xff\xfe\x80junk", False),
             ("bytes garbage", "bytes", b"not a slot {", False),
             ("bytes empty", "bytes", b"", False),
             ("json list", "bytes", b"[1, 2]", False),
             ("folder named slot-0.json", "folder", None, False)]
    for name, v in [("str", "PID"), ("float", 1.5), ("bool", True), ("null", None), ("0", 0), ("2**31", 2 ** 31),
                    ("10**30", 10 ** 30)]:
        seeds.append((f"pid {name}", "pid", v, False))
    for p in fake_pids:
        seeds.append((f"unopenable pid {p}", "unopenable", p, False))
    for name, v in [("missing", None), ("empty", ""), ("list", ["T"]), ("dict", {"t": "T"}), ("true", True),
                    ("int", 7)]:
        seeds.append((f"start {name}", "start", v, True))
    for name, v in [("NaN", "NaN"), ("Infinity", "Infinity"), ("str", '"x"'), ("null", "null")]:
        seeds.append((f"since {name}", "since", v, True))
    for field in ("repo", "task", "gate"):
        for name, v in DISPLAY_BAD:
            seeds.append((f"{field} {name}", field, v, True))
    return seeds


def b5_record(kind, v, pid, tok) -> bytes:
    """A slot file seeded for one scenario: a well-formed record of `pid` with start `tok`, with one field
    damaged as `kind`/`v` say ("T" in a start shape stands for `tok`)."""
    rec = {"pid": pid, "start": tok, "repo": "ghost", "task": "g", "gate": "verify", "since": time.time() - 5}
    since_raw = None
    if kind == "pid":
        rec["pid"] = str(pid) if v == "PID" else v
    elif kind == "unopenable":
        rec["pid"] = v
        rec["start"] = "133801234567890123"
    elif kind == "start":
        if v is None:
            del rec["start"]
        elif v == ["T"]:
            rec["start"] = [tok]
        elif v == {"t": "T"}:
            rec["start"] = {"t": tok}
        else:
            rec["start"] = v
    elif kind == "since":
        since_raw = v
    elif kind in ("repo", "task", "gate"):
        rec[kind] = v
    text = json.dumps(rec)
    if since_raw is not None:
        text = re.sub(r'"since": [^,}]+', f'"since": {since_raw}', text)
    return text.encode()


def b5_expected(kind, v, pid_state) -> str:
    """'live' or 'freed' by BR-5's Otherwise line and Q-19."""
    if kind in ("bytes", "folder", "pid", "unopenable"):
        return "freed"
    if kind == "start" and v != "":
        return "freed"  # missing or not a str: a dead holder's
    if pid_state == "dead":
        return "freed"
    if pid_state == "live-right":
        return "live"
    return "live" if (kind == "start" and v == "") else "freed"  # another start: dead; "" is unknown


def b5_line_pattern(kind, v) -> str:
    fields = {"repo": "ghost", "task": "g", "gate": "verify"}
    if kind in fields:
        fields[kind] = v

    def disp(x):
        return re.escape(x) if isinstance(x, str) and x else r"\?"

    age = r"\?" if kind == "since" else r"\d+ s"
    return (re.escape(WAITING) + disp(fields["repo"]) + "/" + disp(fields["task"]) + " running "
            + disp(fields["gate"]) + r" \(" + age + r"\)")


def b5_run(sc: dict, w: Workers) -> list:
    """One scenario on one pool of workers; its problems."""
    base, k, slots = sc["dir"], sc["requesters"], sc["slots"]
    sdir, inside = base / "slots", base / "inside"
    sdir.mkdir(parents=True)
    inside.mkdir()
    seed = sdir / "slot-0.json"
    if sc["kind"] == "folder":
        seed.mkdir()
        (seed / "f.txt").write_text("x", encoding="utf-8")
    else:
        seed.write_bytes(sc["content"])
    sid = sc["id"]
    events: dict = {i: [] for i in range(k)}

    def pump(pred, bound):
        end = time.monotonic() + bound
        while not pred():
            left = end - time.monotonic()
            if left <= 0:
                return False
            try:
                i, ev = w.q.get(timeout=min(left, 0.1))
            except queue.Empty:
                continue
            if ev.get("id") == sid and i in events:
                events[i].append(ev)
        return True

    def finished():
        return all(any(e["ev"] in ("done", "error") for e in events[i]) for i in range(k))

    def release():
        if seed.is_dir():
            for c in seed.iterdir():
                c.unlink()
            seed.rmdir()
        else:
            seed.unlink(missing_ok=True)

    for i in range(k):
        w.send(i, {"id": sid, "dir": str(sdir), "inside": str(inside), "slots": slots, "hold": 0.08})
    problems = []
    live = sc["expect"] == "live"
    if live and slots == 1:
        waited = pump(lambda: all(any(e["ev"] == "out" and e["text"].startswith(WAITING) for e in events[i])
                                  for i in range(k)), 5)
        entered = [i for i in range(k) if any(e["ev"] == "in" for e in events[i])]
        if not waited or entered:
            problems.append(f"a live holder didn't block: waiting lines from all={waited}, entered {entered}")
        if not os.path.lexists(seed):
            problems.append("a live holder's slot entry was removed")
        for i in range(k):
            lines = [e["text"] for e in events[i] if e["ev"] == "out" and e["text"].startswith(WAITING)]
            if lines and not re.fullmatch(sc["pattern"], lines[0]):
                problems.append(f"waiting line {lines[0]!r} doesn't match {sc['pattern']!r}")
        release()
    if not pump(finished, 5):
        stuck = [i for i in range(k) if not any(e["ev"] in ("done", "error") for e in events[i])]
        problems.append(f"requesters {stuck} still waiting after 5 s (blocked by a dead or damaged entry)")
        with contextlib.suppress(OSError):
            release()
        if not pump(finished, 5):
            for i in range(k):
                if not any(e["ev"] in ("done", "error") for e in events[i]):
                    w.restart(i)
                    pump(lambda: False, 0)
    for i in range(k):
        for e in events[i]:
            if e["ev"] == "error":
                problems.append(f"requester {i} raised {e['text']}")
    counts = [e["count"] for i in range(k) for e in events[i] if e["ev"] == "in"]
    room = slots - (1 if live and slots == 2 else 0)
    if counts and max(counts) > room:
        problems.append(f"{max(counts)} requesters inside at once with room for {room}")
    if live and slots == 2 and not os.path.lexists(seed):
        problems.append("a live holder's slot entry was removed")
    if not live and sc["kind"] != "folder" and os.path.lexists(seed):
        problems.append("a dead or damaged slot entry was kept")
    return problems


def test_br5_otherwise_sweep(hroot, tmp_path):
    """AC-3 BR-5 [sweep] BR-2 Q-19 RV-13 RV-14: full cross product, run by real requester processes
    (sys.executable children calling the real run_slot, SLOT_POLL 0.05 s) of slot count {1, 2} x one
    pre-seeded entry `slot-0.json` {undecodable bytes, garbage text, empty, a JSON list, a folder named
    slot-0.json, pid a str / float / bool / null / 0 / 2**31 / 10**30, a pid this user can't open with a
    start (fake everywhere; also real ones on Windows), start missing / "" / list / dict / true / int,
    since NaN / Infinity / "x" / null, repo / task / gate a list / int / ""} x the seeded pid {dead, a live
    child with its right start, a live child with another start} (only for entries that carry a pid) x
    requesters {1, 3}.
    Oracle (BR-5 Otherwise, Q-19): an entry that doesn't parse, whose pid isn't usable, whose start is
    missing or not a str, a folder named like a slot, or an unopenable pid with a start, is freed: every
    requester runs within 5 s, no more at once than the slots, and the entry is gone (a folder is skipped).
    A well-formed entry of a live child with its right start, or with start "", is live: with one slot
    every requester waits, printing the waiting line with `?` for a display field that can't be shown and
    for a non-finite since, and the entry stays; with two slots they run one at a time beside it. Nothing
    raises in any requester."""
    fake = (FAKE_DENIED,) + unopenable_pids()
    seeds = b5_seeds(fake)
    dead_pid, dead_tok = dead_child()
    live = T.sleeper(120)
    live_tok = str(H.proc_start(live.pid))
    pid_states = {"dead": (dead_pid, dead_tok), "live-right": (live.pid, live_tok),
                  "live-other": (live.pid, "1" + live_tok)}
    scenarios = []
    for (name, kind, v, has_pid), slots, k in itertools.product(seeds, (1, 2), (1, 3)):
        for ps in (pid_states if has_pid else ["live-right"]):
            pid, tok = pid_states[ps]
            n = len(scenarios)
            scenarios.append({"id": n, "dir": tmp_path / "b5" / f"s{n}", "requesters": k, "slots": slots,
                              "kind": kind, "content": v if kind == "bytes" else None if kind == "folder" else b5_record(kind, v, pid, tok),
                              "expect": b5_expected(kind, v, ps if has_pid else None),
                              "pattern": b5_line_pattern(kind, v),
                              "label": f"[{name} | pid {ps if has_pid else '-'} | slots {slots} | requesters {k}]"})
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("PYTEST_", "LIGHT_HARNESS_", "CLAUDE_CODE_"))}
    env.update(machine_env(tmp_path / "machine"))
    env["PYTHONIOENCODING"] = "utf-8"
    pools = [Workers(3, env, tmp_path, ",".join(str(p) for p in fake[:1])) for _ in range(4)]
    diffs: list = []
    lock = threading.Lock()

    def drive(pool_i):
        for sc in scenarios[pool_i::len(pools)]:
            try:
                probs = b5_run(sc, pools[pool_i])
            except Exception as e:  # noqa: BLE001 - recorded as a difference
                probs = [f"scenario raised {type(e).__name__}: {e}"]
            if probs:
                with lock:
                    diffs.append(sc["label"] + " " + "; ".join(probs))

    try:
        for p in pools:  # wait until every worker has imported the harness
            got = 0
            end = time.monotonic() + 60
            while got < p.n and time.monotonic() < end:
                with contextlib.suppress(queue.Empty):
                    if p.q.get(timeout=1)[1].get("ev") == "ready":
                        got += 1
            assert got == p.n, "a requester process didn't start"
        threads = [threading.Thread(target=drive, args=(i,)) for i in range(len(pools))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        for p in pools:
            p.close()
        live.kill()
        live.wait()
    diffs.sort()
    assert not diffs, problems_report("BR-5 Otherwise sweep", diffs, len(scenarios))


# ================================================================ BR-9 Otherwise: (RV-16, RV-17, Q-20)
CONTENT_SRC = r'''
def make_content(d, kind, link_target):
    import os, subprocess
    from pathlib import Path
    d = Path(d)
    if os.name == "nt":
        what, target = kind.split(":")
        if target == "sub":
            t = d / "locked"
            t.mkdir()
            (t / "f.txt").write_text("x")
        else:
            t = d / "f.txt"
            t.write_text("x")
        user = "%s\\%s" % (os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", ""))
        perm = what if what.startswith("(") else "(%s)" % what
        subprocess.run(["icacls", str(t), "/deny", user + ":" + perm], capture_output=True, check=True,
                       stdin=subprocess.DEVNULL, timeout=60)
        import _winapi
        _winapi.CreateJunction(str(link_target), str(d / "out-link"))
    else:
        t = d / "locked"
        t.mkdir()
        (t / "f.txt").write_text("x")
        os.symlink(str(link_target), str(d / "out-link"), target_is_directory=True)
        os.chmod(t, {"chmod0": 0, "chmod100": 0o100, "0500": 0o500}[kind])
'''
_ns: dict = {}
exec(CONTENT_SRC, _ns)  # noqa: S102 - the same helper runs in the killed child below
make_content = _ns["make_content"]

KILLED_CHILD = r'''
import contextlib, json, os, sys
scripts, specs, src, link_target = sys.argv[1], json.loads(sys.argv[2]), sys.argv[3], sys.argv[4]
sys.path.insert(0, scripts)
import harness as H
ns = {}
exec(src, ns)
stack = contextlib.ExitStack()
made = []
for i, (kind, probe) in enumerate(specs):
    H.task_slug = lambda i=i: "k%d" % i
    d = stack.enter_context(H.run_temp("-p1" if probe else "", label="probe RV-x" if probe else None))
    ns["make_content"](d, kind, link_target)
    made.append(str(d))
print(json.dumps(made), flush=True)
os._exit(9)
'''


def contents_kinds() -> list:
    if os.name == "nt":
        return [f"{what}:{target}" for what in ("RD", "D", "(OI)(CI)(D)", "F") for target in ("sub", "file")]
    return ["chmod0", "chmod100", "0500"]


def full_deny(kind: str) -> bool:
    return kind.startswith("F:")  # a full-control deny: BR-9's Otherwise lets it stay


def owner_rec(pid, start, user=None) -> dict:
    rec = {"pid": pid, "start": start}
    if user is not None:
        rec["user"] = user
    return rec


def part_a(hroot, diffs: list, live_pid: int, live_tok: str) -> int:
    """Contents the suite locks, run through run_temp: in-process outcomes and a killed run."""
    root = hroot.root
    root.mkdir(parents=True, exist_ok=True)
    outside = hroot.tmp / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    live_dir = root / f"livea-{live_pid}"
    live_dir.mkdir()
    (live_dir / "work.txt").write_text("busy", encoding="utf-8")
    T.write_json(root / f"livea-{live_pid}.owner", owner_rec(live_pid, live_tok, this_user()))
    n = 0
    for kind, probe, outcome in itertools.product(contents_kinds(), (False, True),
                                                  ("pass or fail", "exception", "timeout")):
        n += 1
        label = f"[A | {kind} | {'probe' if probe else 'suite'} | {outcome}]"
        made = []
        try:
            with contextlib.suppress(RuntimeError, H.SuiteTimeout):
                with H.run_temp("-p1" if probe else "", label="probe RV-x" if probe else None) as d:
                    made.append(d)
                    make_content(d, kind, outside)
                    if outcome == "exception":
                        raise RuntimeError("the harness failed after the suite")
                    if outcome == "timeout":
                        raise H.SuiteTimeout(5)
        except BaseException as e:  # noqa: BLE001
            diffs.append(f"{label} raised {type(e).__name__}: {e}")
            continue
        d = made[0]
        left = os.path.lexists(d) or os.path.lexists(d.parent / f"{d.name}.owner")
        if left and not full_deny(kind):
            diffs.append(f"{label} run folder or owner kept after the run")
    # killed CLI: one child enters a run folder per contents x suite/probe, locks them, and dies
    specs = [(kind, probe) for kind in contents_kinds() for probe in (False, True)]
    env = dict(os.environ)
    env.update(hroot.env)
    env.update(TEMP=str(hroot.systemp), TMP=str(hroot.systemp), TMPDIR=str(hroot.systemp))
    r = subprocess.run([sys.executable, "-c", KILLED_CHILD, str(SCRIPTS), json.dumps(specs), CONTENT_SRC,
                        str(outside)], capture_output=True, encoding="utf-8", errors="replace", env=env,
                       cwd=str(hroot.tmp), stdin=subprocess.DEVNULL, timeout=300)
    try:
        killed = [Path(p) for p in json.loads(r.stdout.strip().splitlines()[-1])]
    except (ValueError, IndexError):
        diffs.append(f"[A | killed] the child didn't make its folders: rc {r.returncode} {r.stderr[-300:]!r}")
        killed = []
    if killed and not all(p.exists() for p in killed):
        diffs.append("[A | killed] the killed run's folders weren't there before the next run")
    try:
        with H.run_temp():
            pass
    except BaseException as e:  # noqa: BLE001
        diffs.append(f"[A | killed] the next run raised {type(e).__name__}: {e}")
    for (kind, probe), p in zip(specs, killed):
        n += 1
        left = os.path.lexists(p) or os.path.lexists(p.parent / f"{p.name}.owner")
        if left and not full_deny(kind):
            diffs.append(f"[A | {kind} | {'probe' if probe else 'suite'} | killed CLI] folder or owner kept "
                         "after the next run's start")
    if not (live_dir / "work.txt").exists() or not (root / f"livea-{live_pid}.owner").exists():
        diffs.append("[A] a live run's folder was removed")
    if (outside / "keep.txt").read_text(encoding="utf-8") != "keep":
        diffs.append("[A] a link's target was changed")
    return n


def part_b_cases() -> list:
    cfgs = ["usable", "file", "link"] + ([] if os.name == "nt" else ["unwritable"])
    out = []
    for cfg, priv in itertools.product(cfgs, ("usable", "file")):
        for killed_in in ("cfg", "priv", "fallback"):
            if killed_in == "cfg" and cfg != "usable":
                continue
            if killed_in == "priv" and priv != "usable":
                continue
            out.append((cfg, priv, killed_in))
    return out


def part_b(hroot, monkeypatch, diffs: list, dead_pid: int, dead_tok: str, live_pid: int, live_tok: str) -> int:
    """Run roots (RV-17): a killed run's folder in the configured, private or a last-resort root, found by
    the next run whichever root it uses; empty last-resort roots; linked roots; a live run's folder."""
    me = this_user()
    old = time.time() - 90
    cases = part_b_cases()
    for i, (cfg, priv, killed_in) in enumerate(cases):
        label = f"[B | configured {cfg} | private {priv} | killed run in {killed_in}]"
        sc = hroot.tmp / "b" / f"s{i}"
        systemp = sc / "systemp"
        systemp.mkdir(parents=True)
        monkeypatch.setattr(tempfile, "tempdir", str(systemp))
        monkeypatch.setattr(H, "_ROOT_FALLBACK", [])
        monkeypatch.setattr(H, "_TEMP_WARNED", [])
        cfg_root = sc / "cfg"
        monkeypatch.setenv(TEMP_ROOT_ENV, str(cfg_root))
        priv_root = systemp / ("lh" if os.name == "nt" else f"lh-{os.getuid()}")
        cfg_target = sc / "cfg-target"
        cfg_target.mkdir()
        if cfg == "usable":
            cfg_root.mkdir(mode=0o700)
        elif cfg == "file":
            cfg_root.write_text("not a folder", encoding="utf-8")
        elif cfg == "link":
            make_link(cfg_root, cfg_target)
        else:
            cfg_root.mkdir()
            os.chmod(cfg_root, 0o500)
        if priv == "usable":
            priv_root.mkdir(mode=0o700)
        else:
            priv_root.write_text("not a folder", encoding="utf-8")
        fallback_dead = systemp / "lh-run-dead0001"
        holder = {"cfg": cfg_root, "priv": priv_root, "fallback": fallback_dead}[killed_in]
        holder.mkdir(exist_ok=True, mode=0o700)
        dead = holder / f"t-{dead_pid}"
        dead.mkdir()
        (dead / "f.txt").write_text("x", encoding="utf-8")
        T.write_json(holder / f"t-{dead_pid}.owner", owner_rec(dead_pid, dead_tok, me))
        # a dead run's folder behind the configured root's link target (never entered through the link)
        tdead = cfg_target / f"t-{dead_pid}"
        tdead.mkdir()
        T.write_json(cfg_target / f"t-{dead_pid}.owner", owner_rec(dead_pid, dead_tok, me))
        young, old_root = systemp / "lh-run-young001", systemp / "lh-run-old00001"
        young.mkdir(mode=0o700)
        old_root.mkdir(mode=0o700)
        os.utime(young, (time.time() - 30, time.time() - 30))
        os.utime(old_root, (old, old))
        ltarget = sc / "link-target"
        ltarget.mkdir()
        (ltarget / f"t-{dead_pid}").mkdir()
        T.write_json(ltarget / f"t-{dead_pid}.owner", owner_rec(dead_pid, dead_tok, me))
        os.utime(ltarget, (old, old))
        linked = systemp / "lh-run-link0001"
        make_link(linked, ltarget)
        live_root = systemp / "lh-run-live0001"
        live_root.mkdir(mode=0o700)
        (live_root / f"live-{live_pid}").mkdir()
        T.write_json(live_root / f"live-{live_pid}.owner", owner_rec(live_pid, live_tok, me))
        os.utime(live_root, (old, old))
        made = []
        try:
            with H.run_temp() as d:
                made.append(d)
        except BaseException as e:  # noqa: BLE001
            diffs.append(f"{label} the next run raised {type(e).__name__}: {e}")
        problems = []
        if os.path.lexists(dead) or os.path.lexists(holder / f"t-{dead_pid}.owner"):
            problems.append("the killed run's folder or owner was kept after the next run's start")
        if made and os.path.lexists(made[0]):
            problems.append("the run's own folder was kept")
        if not young.is_dir():
            problems.append("an empty last-resort root 30 s old was removed")
        if os.path.lexists(old_root):
            problems.append("an empty last-resort root 90 s old was kept")
        if not os.path.lexists(linked) or not (ltarget / f"t-{dead_pid}").is_dir():
            problems.append("a linked last-resort root was removed or entered")
        if not (live_root / f"live-{live_pid}").is_dir() or not (live_root / f"live-{live_pid}.owner").exists():
            problems.append("a live run's folder in a last-resort root was removed")
        if not tdead.is_dir():
            problems.append("the configured root's link was entered")
        if cfg == "file" and cfg_root.read_text(encoding="utf-8") != "not a folder":
            problems.append("the configured root's file was changed")
        if priv == "file" and priv_root.read_text(encoding="utf-8") != "not a folder":
            problems.append("the private root's file was changed")
        if problems:
            diffs.append(label + " " + "; ".join(problems))
        if cfg == "unwritable":
            os.chmod(cfg_root, 0o700)
    return len(cases)


def part_c(hroot, monkeypatch, diffs: list, dead_pid: int, dead_tok: str, live_pid: int, live_tok: str) -> int:
    """Owners across users (Q-20): owner user x owner pid x entry kind, one sweep of one root."""
    root = hroot.tmp / "c-root"
    root.mkdir()
    target = hroot.tmp / "c-target"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    denied = (FAKE_DENIED,) + unopenable_pids()
    with_fake_denied(monkeypatch, (FAKE_DENIED,))
    users = {"this": this_user(), "other": other_user(), "missing": None}
    pids = [("dead", dead_pid, dead_tok), ("live", live_pid, live_tok)] + \
        [(f"unopenable {p}", p, "133801234567890123") for p in denied]
    expected, n = {}, 0
    for (uname, user), (pname, pid, tok), kind in itertools.product(users.items(), pids, ("folder", "file", "link")):
        name = f"c{n}-{pid}"
        n += 1
        if kind == "folder":
            (root / name).mkdir()
            (root / name / "f.txt").write_text("x", encoding="utf-8")
        elif kind == "file":
            (root / name).write_text("x", encoding="utf-8")
        else:
            make_link(root / name, target)
        T.write_json(root / f"{name}.owner", owner_rec(pid, tok, user))
        dead = pname == "dead" or (pname.startswith("unopenable") and uname == "this")
        expected[name] = (not dead, f"[C | user {uname} | pid {pname} | {kind}]")
    try:
        H.sweep_temp_root(root)
    except BaseException as e:  # noqa: BLE001
        diffs.append(f"[C] sweep_temp_root raised {type(e).__name__}: {e}")
    for name, (keep, label) in expected.items():
        kept = os.path.lexists(root / name)
        if kept != keep:
            diffs.append(f"{label} kept={kept} (expected {keep})")
    if (target / "keep.txt").read_text(encoding="utf-8") != "keep":
        diffs.append("[C] a link's target was changed")
    return n


def test_br9_otherwise_sweep(hroot, monkeypatch):
    """AC-5 BR-9 [sweep] RV-16 RV-17 Q-20, on the running OS, in three parts:
    (A) contents the suite locks, through the real run_temp: Windows `icacls /deny` of (RD), (D),
    (OI)(CI)(D) or (F) on a subfolder or a file, POSIX `chmod 0` / `chmod 100` on a subfolder or a 0500
    folder holding a file (each run folder also holds a link to a folder outside) x outcome {pass or fail
    (run_temp can't tell them apart), exception after the suite, timeout, killed CLI (cleaned by the next
    run's start)} x suite or probe. Oracle: folder and owner file gone after the run, or after the next
    run's start; a full-control deny (F) may stay; a live run's folder under the root and the link's target
    are untouched; nothing raises.
    (B) run roots (RV-17): configured root {usable, a file, a link, unwritable (POSIX)} x private root
    {usable, a file} x the root a killed run used {configured, private, a last-resort lh-run-*}, so the
    next run uses the same root or another; each with lh-run-* roots that are empty and 30 s / 90 s old, a
    link, and one holding a live run's folder. Oracle: the killed run's folder is gone after the next run's
    start whichever root that run uses; an empty lh-run-* root over 60 s old is removed, a younger one kept;
    a linked root is never entered or removed; a live run's folder is kept.
    (C) owners across users (Q-20): owner `user` {this user's, another user's, missing} x owner pid {dead,
    live child, can't be opened (fake everywhere; real ones on Windows)} x entry {folder, file, link}.
    Oracle: removed iff the owner is dead: the pid is gone, or it can't be opened and the record is this
    user's; nothing raises."""
    dead_pid, dead_tok = dead_child()
    live = T.sleeper(120)
    diffs: list = []
    try:
        live_tok = str(H.proc_start(live.pid))
        n = part_a(hroot, diffs, live.pid, live_tok)
        n += part_b(hroot, monkeypatch, diffs, dead_pid, dead_tok, live.pid, live_tok)
        n += part_c(hroot, monkeypatch, diffs, dead_pid, dead_tok, live.pid, live_tok)
    finally:
        live.kill()
        live.wait()
        unlock_tree(hroot.tmp)
    assert not diffs, problems_report("BR-9 Otherwise sweep", diffs, n)


# ================================================================ RV-13: waiting_line never raises
SINCES = [float("nan"), float("inf"), float("-inf"), 1e400, 10 ** 400, 1e308, -1e308, 0, "x", None, True, [], {}]
SHAPES = ["r", "", None, 7, [1], {"a": 1}]
NOW = 1_763_700_000.0


def expected_age(since, now) -> str:
    if isinstance(since, bool) or not isinstance(since, (int, float)):
        return "?"
    try:
        if not (math.isfinite(float(since)) and math.isfinite(now)):
            return "?"
        diff = now - since
        if not math.isfinite(float(diff)):
            return "?"
    except OverflowError:
        return "?"
    s = max(0, int(diff))
    return f"{s} s" if s < 60 else f"{s // 60} min"


def shown(v) -> str:
    return v if isinstance(v, str) and v else "?"


def test_rv13_waiting_line_never_raises_sweep(hroot, monkeypatch, capsys):
    """AC-3 BR-5 [sweep] BR-4 RV-13: waiting_line over since {NaN, Infinity, -Infinity, 1e400, 10**400, 1e308,
    -1e308, 0, "x", None, True, [], {}} x a shape for repo, task and gate at once {"r", "", None, 7, [1],
    {"a": 1}} x now {a fixed time, NaN}: 156 calls; plus each shape in one field at a time with the other two
    "r": 18 calls; plus two holders joined. Oracle: never raises; the age is `?` unless since and now - since
    are finite, else `N s` under a minute, `N min` from one; a field that isn't a non-empty str prints `?`.
    Then the real run_slot, one slot held by a live child whose slot record's since is NaN, Infinity,
    -Infinity or 1e400: it waits and prints `waiting for a run slot: ghost/g running verify (?)`."""
    diffs, total = [], 0
    calls = [(s, (v, v, v), now) for s in SINCES for v in SHAPES for now in (NOW, float("nan"))]
    calls += [(NOW - 30, tuple(v if j == f else "r" for j in range(3)), NOW) for v in SHAPES for f in range(3)]
    for since, (repo, task, gate), now in calls:
        total += 1
        rec = {"repo": repo, "task": task, "gate": gate, "since": since}
        want = f"{WAITING}{shown(repo)}/{shown(task)} running {shown(gate)} ({expected_age(since, now)})"
        try:
            got = H.waiting_line([rec], now=now)
        except BaseException as e:  # noqa: BLE001
            got = f"raised {type(e).__name__}: {e}"
        if got != want:
            diffs.append(f"[since {since!r} | fields {(repo, task, gate)!r} | now {now}] {got!r} != {want!r}")
    two = [{"repo": "a", "task": "t1", "gate": "verify", "since": NOW - 45},
           {"repo": [2], "task": "t2", "gate": "redgate", "since": float("nan")}]
    total += 1
    want2 = f"{WAITING}a/t1 running verify (45 s), ?/t2 running redgate (?)"
    try:
        got2 = H.waiting_line(two, now=NOW)
    except BaseException as e:  # noqa: BLE001
        got2 = f"raised {type(e).__name__}: {e}"
    if got2 != want2:
        diffs.append(f"[two holders] {got2!r} != {want2!r}")
    live = T.sleeper(60)
    try:
        tok = str(H.proc_start(live.pid))
        scans = scan_counter(monkeypatch)
        for raw in ("NaN", "Infinity", "-Infinity", "1e400"):
            total += 1
            d = clear_slots()
            (d / "slot-0.json").write_text(
                f'{{"pid": {live.pid}, "start": {json.dumps(tok)}, "repo": "ghost", "task": "g", '
                f'"gate": "verify", "since": {raw}}}', encoding="utf-8")
            capsys.readouterr()
            got = claim_once(scans)
            out = capsys.readouterr().out
            if got != "blocked" or f"{WAITING}ghost/g running verify (?)" not in out.splitlines():
                diffs.append(f"[run_slot since {raw}] {got}; printed {out[-200:]!r}")
    finally:
        live.kill()
        live.wait()
    assert not diffs, problems_report("RV-13 sweep", diffs, total)


# ================================================================ RV-14: a pid this user can't open
def test_rv14_denied_pid_frees_slot_and_lock(hroot, monkeypatch, capsys):
    """AC-2 AC-3 AC-5 BR-2 BR-5 BR-9 RV-14 R-16: each pid this user can't open (a fake one through a kernel32
    that denies access, on every OS; on Windows also real ones found with OpenProcess) is put in: a slot
    record with a start -> freed within 3 s (the requester gets the only slot); a lock with a start -> taken
    at once; a legacy lock (plain pid, no start) -> refused with the base message; an owner record of this
    user with a start -> its folder removed; an owner of another user -> kept. Nothing is killed.
    Plus the marker: DENIED == "", it is falsy and a str, _start_windows returns it for access denied
    (error 5); and every record the harness writes has a plain-str start, even when its own start reads as
    DENIED (R-16)."""
    pids = (FAKE_DENIED,) + unopenable_pids()
    with_fake_denied(monkeypatch, (FAKE_DENIED,))
    kills = Kills()
    monkeypatch.setattr(H, "kill_tree", kills)
    me = this_user()
    diffs = []
    start = "133801234567890123"
    real_slot_count = H.slot_count
    for pid in pids:
        # slot: freed within 3 s
        d = clear_slots()
        T.write_json(d / "slot-0.json", {"pid": pid, "start": start, "repo": "ghost", "task": "g",
                                         "gate": "verify", "since": time.time()})
        t0 = time.monotonic()

        def bounded():
            if time.monotonic() - t0 > 3:
                raise Blocked()
            return 1

        monkeypatch.setattr(H, "slot_count", bounded)
        capsys.readouterr()
        try:
            with H.run_slot("verify"):
                held = sorted(p.name for p in d.glob("slot-*.json"))
            if held != ["slot-0.json"] or time.monotonic() - t0 > 3:
                diffs.append(f"[slot {pid}] held {held} after {time.monotonic() - t0:.1f} s")
        except Blocked:
            diffs.append(f"[slot {pid}] still waiting after 3 s: {capsys.readouterr().out[-150:]!r}")
        except BaseException as e:  # noqa: BLE001
            diffs.append(f"[slot {pid}] raised {type(e).__name__}: {e}")
        monkeypatch.setattr(H, "slot_count", real_slot_count)
        # lock with a start: taken; legacy lock: refused
        lock = hroot.tmp / "base-lock-x"
        for content, want in ((json.dumps({"pid": pid, "start": start}), "taken"), (str(pid), "refused")):
            lock.write_text(content, encoding="utf-8")
            got, code, out = take_lock_outcome(lock, capsys)
            if got != want or (want == "refused" and (code != 1 or f"{REFUSED} (pid {pid})" not in out)):
                diffs.append(f"[lock {pid} {content[:30]!r}] {got} exit {code} (expected {want})")
            elif want == "taken" and H.read_record(lock).get("pid") != os.getpid():
                diffs.append(f"[lock {pid}] not rewritten for this process")
            lock.unlink(missing_ok=True)
        # owners: this user's removed, another user's kept
        root = hroot.tmp / f"owners-{pid}"
        root.mkdir()
        for name, user, keep in ((f"mine-{pid}", me, False), (f"theirs-{pid}", other_user(), True)):
            (root / name).mkdir()
            T.write_json(root / f"{name}.owner", owner_rec(pid, start, user))
        try:
            H.sweep_temp_root(root)
        except BaseException as e:  # noqa: BLE001
            diffs.append(f"[owner {pid}] sweep raised {type(e).__name__}: {e}")
        for name, keep in ((f"mine-{pid}", False), (f"theirs-{pid}", True)):
            if (root / name).exists() != keep:
                diffs.append(f"[owner {pid} {name}] kept={(root / name).exists()} (expected {keep})")
    if kills.calls:
        diffs.append(f"kill_tree called {kills.calls}")
    # the marker
    denied = getattr(H, "DENIED", None)
    marker = getattr(H, "Unopenable", None)
    if denied is None or marker is None:
        diffs.append("no DENIED / Unopenable marker in the harness")
    else:
        if not (denied == "" and not denied and isinstance(denied, str) and isinstance(denied, marker)):
            diffs.append(f"DENIED isn't an empty, falsy str of Unopenable: {denied!r}")
        if not isinstance(H._start_windows(FAKE_DENIED, k32=NoOpenK32()), marker):
            diffs.append("_start_windows doesn't return DENIED for access denied")
        # every record the harness writes has a plain-str start, even if its own start reads as DENIED
        monkeypatch.setattr(H, "proc_start", lambda p: denied)
        rec = H._holder_record("verify")
        if type(rec["start"]) is not str or rec["start"] != "":
            diffs.append(f"_holder_record start {type(rec['start']).__name__} {rec['start']!r}")
        with H.run_temp() as d:
            owner = H.read_record(d.parent / f"{d.name}.owner")
            ot = json.loads((d.parent / f"{d.name}.owner").read_text(encoding="utf-8"))
        if type(ot.get("start")) is not str or owner.get("user") != me:
            diffs.append(f"run_temp's owner record {ot!r}")
        lock = hroot.tmp / "base-lock-note"
        lock.unlink(missing_ok=True)
        H._take_lock(lock)
        try:
            H.note_suite(4242)
            lk = json.loads(lock.read_text(encoding="utf-8"))
        finally:
            H.HELD_LOCK.clear()
        if lk.get("suite_pid") != 4242 or lk.get("suite_start") != "" or lk.get("start") != "":
            diffs.append(f"note_suite's record {lk!r}")
    assert not diffs, problems_report("RV-14", diffs, len(pids) * 5 + 4)
