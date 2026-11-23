# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""Regression sweeps for review round 4 of issues #16/#25/#30, one per blocking defect class (§11.2):

- RV-20 "open error taken as no process": every OpenProcess error code class × record role (lock, slot,
  run-folder owner) × record form (with a start, legacy, start "") (BR-2).
- RV-23 "finished holder's slot left live": release unlink failing 0..always × slot count × follow-on claim
  by the same process and by another process (BR-5).
- RV-18 "outside file changed through a hard link" (Windows): outside file attributes × its ACL kind × what
  stops the delete × outcome (BR-9 `Otherwise:`, never a link's target). POSIX: the same with chmod.
- RV-19 "run folder open to other users": a root granting other users access × suite / probe folder ×
  what the suite makes inside (BR-8, BR-9).

OS-dependent dimensions are built at collection time; no skips. Machine-wide state stays in pytest's temp
folder (tests/lh_machine.py), and tempfile's temp folder points there too.
"""
from __future__ import annotations

import ctypes
import itertools
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
from lh_machine import TEMP_ROOT_ENV, isolate  # noqa: E402

NT = os.name == "nt"
STILL_ACTIVE = 259


@pytest.fixture
def hroot(monkeypatch, tmp_path):
    env = isolate(monkeypatch, tmp_path / "machine", slots=1)
    repo = T.mini_repo(tmp_path / "repo")
    T.point(monkeypatch, H, repo)
    systemp = tmp_path / "systemp"
    systemp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(systemp))
    monkeypatch.setattr(H, "SLOT_POLL", 0.05)
    monkeypatch.setattr(H, "RUN_LOAD", {})
    monkeypatch.setattr(H, "_ROOT_FALLBACK", [])
    monkeypatch.setattr(H, "_TEMP_WARNED", [])
    monkeypatch.setattr(H, "main_checkout", lambda: Path("repo-x"))
    monkeypatch.setattr(H, "task_slug", lambda: "task-x")
    H.LEFTOVERS.clear()
    return SimpleNamespace(tmp=tmp_path, repo=repo, root=Path(env[TEMP_ROOT_ENV]), systemp=systemp)


# ---------------------------------------------------------------- RV-20: OpenProcess error codes

class FakeK32:
    """kernel32 stand-in: OpenProcess fails with `err` for one pid; other calls succeed for a live process."""

    def __init__(self, err):
        self.err = err
        self.last = 0

    def OpenProcess(self, access, inherit, pid):  # noqa: N802
        self.last = self.err
        return 0

    def GetLastError(self):  # noqa: N802
        return self.last

    def GetExitCodeProcess(self, h, code):  # noqa: N802
        return 1

    def GetProcessTimes(self, h, *t):  # noqa: N802
        return 1

    def CloseHandle(self, h):  # noqa: N802
        return 1


# Win32 error codes: 87 is the only "no such pid"; 5 is "exists, not ours"; the rest say nothing about existence
ERRORS = [87, 5, 1, 4, 6, 8, 14, 31, 299, 1450, 1453, 1455, 1460, 0xFFFF]
FORMS = ["with-start", "legacy", "empty-start"]
ROLES = ["lock", "slot", "owner-mine", "owner-other", "owner-no-user"]


def _rv20_expected(err, role, form):
    """Alive?  87: no process → dead. 5 (DENIED): dead with a recorded start (owner: only this user's own),
    alive without one (slot: a start that isn't a string is dead, `""` is unknown → alive). Any other error:
    the process exists, start unknown → alive."""
    if err == 87:
        return False
    if role == "slot" and form == "legacy":
        return False  # no string start: a dead holder's slot (Q-19)
    has_start = form == "with-start"
    if err == 5 and has_start:
        return role in ("owner-other", "owner-no-user")
    return True


def test_rv20_open_errors_mean_exists_sweep(monkeypatch, tmp_path):
    """BR-2 [sweep] RV-20: generator: OpenProcess error {87, 5, and 11 others: generic, resource, quota,
    handle, unknown} × record role {lock, slot, run-folder owner of this user, of another user, without a
    user} × record form {with a start, legacy (no start), start ""}. Oracle: only 87 means no such process;
    5 is a process this user can't open (BR-2's denied rule); every other error means the process exists
    with an unknown start, so the holder stays alive (BR-2 `Otherwise:`, R-2)."""
    diffs, n = [], 0
    for err, role, form in itertools.product(ERRORS, ROLES, FORMS):
        n += 1
        k32 = FakeK32(err)
        monkeypatch.setattr(H, "proc_start", lambda pid, k32=k32: H._start_windows(pid, k32=k32))
        rec = {"pid": 4242}
        if form == "with-start":
            rec["start"] = "133801234567890123"
        elif form == "empty-start":
            rec["start"] = ""
        if role == "lock":
            got = H.holder_alive(rec["pid"], rec.get("start"))
        elif role == "slot":
            got = H._slot_alive(rec)
        else:
            if role == "owner-mine":
                rec["user"] = H._user()
            elif role == "owner-other":
                rec["user"] = "otherdomain\\someone-else"
            o = tmp_path / f"case{n}-4242.owner"
            T.write_json(o, rec)
            got = H._folder_alive(f"case{n}-4242", o)
        exp = _rv20_expected(err, role, form)
        if got != exp:
            diffs.append(f"error {err} × {role} × {form}: alive={got}, expected {exp}")
    print(f"RV-20 sweep: {n} cases, {len(diffs)} differ")
    assert not diffs, "\n  ".join(diffs[:12])


# ---------------------------------------------------------------- RV-23: slot release that fails

FAILS = [0, 1, 3, 10, 1000]  # release unlink failures in a row (1000: never succeeds)


@pytest.mark.parametrize("slots", [1, 2])
def test_rv23_release_failure_never_blocks_sweep(hroot, monkeypatch, slots):
    """BR-5 [sweep] RV-23: generator: the release unlink fails {0, 1, 3, 10 times, always} × slot count {1, 2}
    × the next claim {this process right after, this process from two threads at once, another process}.
    Oracle: a finished holder never blocks: this process's next claims enter at once (a leftover record of
    its own counts as dead, or the retried release removed it), another process enters once this one exits,
    and live holders never exceed the slot count."""
    monkeypatch.setattr(H, "slot_count", lambda: slots)
    real_unlink = Path.unlink
    diffs = []
    for fails in FAILS:
        left = {"n": fails}

        def flaky(self, missing_ok=False, left=left):
            if self.name.startswith("slot-") and left["n"] > 0:
                left["n"] -= 1
                raise PermissionError(32, "in use", str(self))
            return real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", flaky)
        with H.run_slot("verify"):
            pass
        monkeypatch.setattr(Path, "unlink", real_unlink)
        # this process again, bounded: a scan budget instead of waiting forever
        scans = {"n": 0}

        def bounded(scans=scans):
            scans["n"] += 1
            if scans["n"] > 3:
                raise TimeoutError
            return slots
        monkeypatch.setattr(H, "slot_count", bounded)
        try:
            with H.run_slot("verify"):
                pass
        except TimeoutError:
            diffs.append(f"{fails} failed release(s) × {slots} slot(s): this process's next claim waited on itself")
        # two threads of this process at once (verify's probe batch)
        scans["n"] = -100
        entered, errs = [], []

        def claim():
            try:
                with H.run_slot("probe"):
                    entered.append(1)
                    time.sleep(0.2)
            except Exception as e:  # noqa: BLE001
                errs.append(e)
        ts = [threading.Thread(target=claim) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
        if len(entered) != 2 or errs:
            diffs.append(f"{fails} failed release(s) × {slots} slot(s): two probe threads entered {len(entered)}, {errs}")
        monkeypatch.setattr(H, "slot_count", lambda: slots)
        live = [f for f in H.slot_dir().glob("slot-*.json") if H._slot_alive(H.read_record(f))]
        own_left = [f for f in live if H.read_record(f).get("pid") == os.getpid()]
        if own_left and fails < 1000:
            diffs.append(f"{fails} failed release(s): own slot record left after the retried release")
        for f in H.slot_dir().glob("slot-*.json"):
            real_unlink(f, missing_ok=True)
    print(f"RV-23 sweep: {len(FAILS)} failure counts × {slots} slot(s), {len(diffs)} differ")
    assert not diffs, "\n  ".join(diffs)


# ---------------------------------------------------------------- RV-18: files outside the run folder

def _acl(path) -> str:
    if not NT:
        return oct(os.stat(path).st_mode)
    out = subprocess.run(["icacls", str(path)], capture_output=True, text=True).stdout.splitlines()
    lines = [ln.strip() for ln in out if ln.strip() and not ln.startswith("Successfully")]
    return "|".join([lines[0][len(str(path)):].strip()] + lines[1:]) if lines else ""


def _explicit(path):
    if NT:
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{os.environ.get('USERNAME')}:(F)"],
                       capture_output=True)
    else:
        os.chmod(path, 0o600)


def _blockers():
    """What stops the first delete of the run folder (OS-dependent, built at collection time)."""
    if NT:
        return ["none", "deny-delete", "deny-list", "deny-delete-inherited", "file-held-open"]
    return ["none", "dir-mode-500", "dir-mode-0"]


def _block(kind, sub: Path):
    if kind == "deny-delete":
        subprocess.run(["icacls", str(sub), "/deny", "*S-1-1-0:(D)"], capture_output=True)
    elif kind == "deny-list":
        subprocess.run(["icacls", str(sub), "/deny", "*S-1-1-0:(RD)"], capture_output=True)
    elif kind == "deny-delete-inherited":
        subprocess.run(["icacls", str(sub), "/deny", "*S-1-1-0:(OI)(CI)(D)"], capture_output=True)
    elif kind == "dir-mode-500":
        os.chmod(sub, 0o500)
    elif kind == "dir-mode-0":
        os.chmod(sub, 0)
    return None


def _unblock(sub: Path):
    if NT:
        subprocess.run(["icacls", str(sub), "/reset", "/T", "/C", "/Q"], capture_output=True)
    else:
        for p in [sub] + [Path(r) / x for r, ds, fs in os.walk(sub) for x in ds + fs]:
            with_mode = 0o700 if p.is_dir() else 0o600
            try:
                os.chmod(p, with_mode)
            except OSError:
                pass


@pytest.mark.parametrize("blocker", _blockers())
def test_rv18_outside_file_unchanged_sweep(hroot, blocker):
    """BR-9 [sweep] RV-18: generator: an outside file hard-linked into the run folder's TEMP (as a local git
    clone does) × its attributes {read-only, writable} × its permissions {inherited, explicit owner-only}
    × where the link sits {top of TEMP, under a subfolder} × what stops the first delete {none, deny entries,
    a file held open; POSIX: unwritable folders} × run end {normal, start-up clean-up of a killed run}.
    Oracle (BR-9 `Otherwise:`): the outside file's content, attributes and permissions are exactly as
    before; the run folder goes, or stays only while something in it can't be removed."""
    outside_dir = hroot.tmp / "outside"
    outside_dir.mkdir()
    diffs, n = [], 0
    for ro, explicit, deep, killed in itertools.product([True, False], [True, False], [True, False], [False, True]):
        n += 1
        f = outside_dir / f"o{n}.txt"
        f.write_text(f"data {n}")
        if explicit:
            _explicit(f)
        if ro:
            os.chmod(f, stat.S_IREAD)
        before = ((os.stat(f).st_size, os.stat(f).st_mtime_ns), bool(os.stat(f).st_mode & stat.S_IWRITE), _acl(f))
        held = None
        try:
            if killed:
                child = T.sleeper(0)
                child.wait()
                d = hroot.root / f"other-{child.pid}"
                hroot.root.mkdir(parents=True, exist_ok=True)
                d.mkdir()
                T.write_json(hroot.root / f"other-{child.pid}.owner", {"pid": child.pid, "start": "1",
                                                                        "user": H._user()})
            sub = None
            if killed:
                place = d
            else:
                cm = H.run_temp()
                d = cm.__enter__()
                place = d
            if deep:
                sub = place / "sub"
                sub.mkdir()
                place = sub
            os.link(f, place / "link.txt")
            (place / "own.txt").write_text("x")
            if blocker == "file-held-open":
                held = open(place / "own.txt")
            elif sub is not None:
                _block(blocker, sub)
            # the suite's own actions (its icacls propagates too) are its business: compare from here on
            before = ((os.stat(f).st_size, os.stat(f).st_mtime_ns), bool(os.stat(f).st_mode & stat.S_IWRITE), _acl(f))
            if killed:
                H.sweep_temp_root(hroot.root)
            else:
                cm.__exit__(None, None, None)
        finally:
            if held:
                held.close()
        after = ((os.stat(f).st_size, os.stat(f).st_mtime_ns), bool(os.stat(f).st_mode & stat.S_IWRITE), _acl(f))
        if before != after:
            diffs.append(f"ro={ro} explicit={explicit} deep={deep} killed={killed} × {blocker}: "
                         f"outside file changed {before[1:]} -> {after[1:]}")
        if sub is not None and sub.exists():
            _unblock(sub)
        if NT:  # the suite's inherited deny reached the outside file through the link: the test undoes it
            subprocess.run(["icacls", str(f), "/reset", "/C", "/Q"], capture_output=True)
        os.chmod(f, stat.S_IWRITE | stat.S_IREAD)
    print(f"RV-18 sweep ({blocker}): {n} cases, {len(diffs)} differ")
    assert not diffs, "\n  ".join(diffs[:8])


# ---------------------------------------------------------------- RV-19: run folders private

OTHERS = ["S-1-5-11", "S-1-5-32-545", "S-1-1-0"]  # Authenticated Users, Users, Everyone


def _open_to_others(path) -> bool:
    if not NT:
        return bool(os.stat(path).st_mode & 0o077)
    out = subprocess.run(["icacls", str(path)], capture_output=True, text=True).stdout
    names = ["Authenticated Users", "BUILTIN\\Users", "Everyone"]
    return any(nm in out and "DENY" not in line for line in out.splitlines() for nm in names if nm in line)


@pytest.mark.parametrize("grant", OTHERS if NT else ["mode-777"])
def test_rv19_run_folder_private_sweep(hroot, grant):
    """BR-9 [sweep] RV-19: generator: a run root that grants another principal access {Authenticated Users
    Modify, Users Modify, Everyone Modify, all inheritable; POSIX: a root of mode 777} × run kind {suite,
    probe} × what the suite makes inside {a file, a subfolder, a file in the subfolder}. Oracle (BR-8: the
    user's TEMP is private outside the run as at base): the run folder and everything made in it can be
    opened only by this user (plus SYSTEM and Administrators on Windows)."""
    hroot.root.mkdir(parents=True, exist_ok=True)
    if NT:
        subprocess.run(["icacls", str(hroot.root), "/grant", f"*{grant}:(OI)(CI)(M)"], capture_output=True)
    else:
        os.chmod(hroot.root, 0o777)
    diffs = []
    for suffix in ["", "-p1"]:
        with H.run_temp(suffix) as d:
            (d / "f.txt").write_text("secret")
            (d / "sub").mkdir()
            (d / "sub" / "g.txt").write_text("secret")
            # POSIX: a folder of mode 700 keeps everything in it private, whatever their own modes
            for p in ([d, d / "f.txt", d / "sub", d / "sub" / "g.txt"] if NT else [d]):
                if _open_to_others(p):
                    diffs.append(f"{grant} × {'suite' if not suffix else 'probe'}: {p.relative_to(d.parent)} is open to others")
    print(f"RV-19 sweep ({grant}): {len(diffs)} differ")
    assert not diffs, "\n  ".join(diffs)
