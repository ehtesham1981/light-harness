# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""Regression sweeps for review round 5 of issues #16/#25/#30, one per blocking defect class (§11.2):

- RV-26 "deep run folder crashes clean-up": nesting depth (past Python's recursion limit) × what sits at the
  bottom × how the run ends × after-run removal or a later run's start-up clean-up (BR-9 `Otherwise:`).
- RV-25 "live run folder removed via a writable owner record": a shared root granting other users access ×
  suite / probe run × the owner record (BR-9, BR-8).

OS-dependent dimensions are built at collection time; no skips. Machine-wide state stays in pytest's temp
folder (tests/lh_machine.py), and tempfile's temp folder points there too.
"""
from __future__ import annotations

import itertools
import os
import stat
import subprocess
import sys
import tempfile
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


@pytest.fixture
def hroot(monkeypatch, tmp_path):
    env = isolate(monkeypatch, tmp_path / "machine", slots=1)
    repo = T.mini_repo(tmp_path / "repo")
    T.point(monkeypatch, H, repo)
    systemp = tmp_path / "systemp"
    systemp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(systemp))
    monkeypatch.setattr(H, "RUN_LOAD", {})
    monkeypatch.setattr(H, "_ROOT_FALLBACK", [])
    monkeypatch.setattr(H, "_TEMP_WARNED", [])
    monkeypatch.setattr(H, "main_checkout", lambda: Path("repo-x"))
    monkeypatch.setattr(H, "task_slug", lambda: "task-x")
    H.LEFTOVERS.clear()
    return SimpleNamespace(tmp=tmp_path, root=Path(env[TEMP_ROOT_ENV]))


# ---------------------------------------------------------------- RV-26: deep trees

# Depths past Python's default recursion limit (1000). POSIX keeps the whole path under PATH_MAX (4096) with
# one-letter names, so the tree stays legal there (deeper paths are RV-27's class, not this one).
DEPTHS = [995, 1100, 1300]
BOTTOMS = ["empty", "file", "read-only-file"] + (["deny-delete"] if NT else ["mode-500"])
ENDS = ["pass", "exception", "killed"]


def _ext(p) -> str:
    s = os.path.abspath(str(p))
    return "\\\\?\\" + s if NT else s


def _deep(top: Path, depth: int, bottom: str) -> str:
    cur = _ext(top)
    for _ in range(depth):
        cur = os.path.join(cur, "d")
        os.mkdir(cur)
    if bottom in ("file", "read-only-file"):
        f = os.path.join(cur, "f.txt")
        with open(f, "w") as fh:
            fh.write("x")
        if bottom == "read-only-file":
            os.chmod(f, stat.S_IREAD)
    elif bottom == "deny-delete":
        subprocess.run(["icacls", cur[4:] if cur.startswith("\\\\?\\") else cur, "/deny", "*S-1-1-0:(D)"],
                       capture_output=True)
    elif bottom == "mode-500":
        os.chmod(cur, 0o500)
    return cur


class Boom(Exception):
    pass


@pytest.mark.parametrize("depth", DEPTHS)
def test_rv26_deep_tree_never_crashes_cleanup_sweep(hroot, depth):
    """BR-9 [sweep] RV-26: generator: nesting depth {995, 1100, 1300} × what sits at the
    bottom {nothing, a file, a read-only file; Windows a delete-denied folder, POSIX a mode-500 folder} × how
    the run ends {passes, raises its own exception, is killed (cleaned up by the next run's start)}. Oracle
    (BR-9 `Otherwise:`, BR-1): clean-up never raises; a run's own outcome comes out unchanged (its exception,
    not a clean-up error); the folder and its owner file are gone after the run, or after the next run's
    start for a killed run; the next run_temp works."""
    diffs = []
    for bottom, end in itertools.product(BOTTOMS, ENDS):
        case = f"depth {depth} × {bottom} × {end}"
        try:
            if end == "killed":
                child = T.sleeper(0)
                child.wait()
                hroot.root.mkdir(parents=True, exist_ok=True)
                d = hroot.root / f"other-{child.pid}"
                d.mkdir()
                T.write_json(hroot.root / f"other-{child.pid}.owner",
                             {"pid": child.pid, "start": "1", "user": H._user()})
                _deep(d, depth, bottom)
                with H.run_temp():
                    pass
            else:
                raised = None
                try:
                    with H.run_temp() as d:
                        _deep(d, depth, bottom)
                        if end == "exception":
                            raise Boom("the run's own failure")
                except Boom as e:
                    raised = e
                except Exception as e:  # noqa: BLE001 - the class is "clean-up replaces the outcome"
                    diffs.append(f"{case}: the run ended with {type(e).__name__}: {str(e)[:80]}")
                    continue
                if (end == "exception") != (raised is not None):
                    diffs.append(f"{case}: the run's own outcome was lost")
        except Exception as e:  # noqa: BLE001
            diffs.append(f"{case}: raised {type(e).__name__}: {str(e)[:80]}")
            continue
        if os.path.lexists(_ext(d)):
            diffs.append(f"{case}: folder kept")
        if os.path.lexists(str(d) + ".owner"):
            diffs.append(f"{case}: owner file kept")
    print(f"RV-26 sweep (depth {depth}): {len(BOTTOMS) * len(ENDS)} cases, {len(diffs)} differ")
    assert not diffs, "\n  ".join(diffs[:10])


# ---------------------------------------------------------------- RV-25: owner records

OTHERS = ["S-1-5-11", "S-1-5-32-545", "S-1-1-0"]  # Authenticated Users, Users, Everyone


def _changeable_by_others(path) -> bool:
    if not NT:
        return bool(os.stat(path).st_mode & 0o022)
    out = subprocess.run(["icacls", str(path)], capture_output=True, text=True).stdout
    names = ["Authenticated Users", "BUILTIN\\Users", "Everyone"]
    write = ("(M)", "(F)", "(W)", "(WD)", "(AD)", "(D)")
    return any(nm in line and "DENY" not in line and any(w in line for w in write)
               for line in out.splitlines() for nm in names)


@pytest.mark.parametrize("grant", OTHERS if NT else ["mode-1777"])
def test_rv25_owner_record_private_sweep(hroot, grant):
    """BR-9 [sweep] RV-25: generator: a shared run root that lets other users change what is in it {Authenticated
    Users, Users, Everyone with inheritable Modify; POSIX: a sticky mode-1777 root} × run kind {suite, probe}
    × the owner record written {at the run's start, rewritten by a second run of this process under the same
    name}. Oracle (BR-9: a live run's folder is never removed; Q-20): no other user can change a run's owner
    record, so nobody else can make this user's own clean-up judge a live folder dead."""
    hroot.root.mkdir(parents=True, exist_ok=True)
    if NT:
        subprocess.run(["icacls", str(hroot.root), "/grant", f"*{grant}:(OI)(CI)(M)"], capture_output=True)
    else:
        os.chmod(hroot.root, 0o1777)
    diffs = []
    for suffix, again in itertools.product(["", "-p1"], [False, True]):
        with H.run_temp(suffix) as d:
            owner = Path(str(d) + ".owner")
            if again:  # the record rewritten the way the harness rewrites records
                H._write_record(owner, H.read_record(owner), private=True)
            if _changeable_by_others(owner):
                diffs.append(f"{grant} × {'suite' if not suffix else 'probe'} × rewritten={again}: owner record "
                             f"can be changed by other users")
            if H.read_record(owner).get("pid") != os.getpid():
                diffs.append(f"{grant} × {suffix or 'suite'}: owner record doesn't name this run")
    print(f"RV-25 sweep ({grant}): 4 runs, {len(diffs)} differ")
    assert not diffs, "\n  ".join(diffs)
