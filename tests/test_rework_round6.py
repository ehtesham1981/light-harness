# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""Regression sweep for review round 6 of issues #16/#25/#30 (§11.2):

- RV-28 "live run folder removed via a writable owner record": a file another user creates in advance under
  the name the owner record's temp file will get × what that file grants × which names are planted × suite
  (main thread) / probe (worker thread) run (BR-9, RV-25's class).

OS-dependent dimensions are built at collection time; no skips. Machine-wide state stays in pytest's temp
folder (tests/lh_machine.py), and tempfile's temp folder points there too.
"""
from __future__ import annotations

import itertools
import os
import subprocess
import sys
import tempfile
import threading
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
from lh_machine import TEMP_ROOT_ENV, isolate  # noqa: E402

NT = os.name == "nt"
PLANTED = b'{"pid": 1, "start": "1", "user": "someone-else"}'


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


# Who the planted file lets in: Authenticated Users, Users, Everyone (full control); POSIX: mode 666.
GRANTS = ["S-1-5-11", "S-1-5-32-545", "S-1-1-0"] if NT else ["mode-666"]
# Which temp names are planted: the old predictable `<owner>.<pid>.<thread id>.tmp`; or the first 1 / the
# first 5 names the name generator hands out (another user who could guess or race the names).
PLANTS = ["pid-thread", "first-1", "first-5"]
RUNS = ["suite", "probe"]  # suite: the main thread; probe: a worker thread, as `probe run` uses


def _changeable_by_others(path) -> bool:
    if not NT:
        return bool(os.stat(path).st_mode & 0o022)
    out = subprocess.run(["icacls", str(path)], capture_output=True, text=True).stdout
    names = ["Authenticated Users", "BUILTIN\\Users", "Everyone"]
    write = ("(M)", "(F)", "(W)", "(WD)", "(AD)", "(D)")
    return any(nm in line and "DENY" not in line and any(w in line for w in write)
               for line in out.splitlines() for nm in names)


def _plant(path: Path, grant: str) -> None:
    path.write_bytes(PLANTED)
    if NT:
        subprocess.run(["icacls", str(path), "/grant", f"*{grant}:(F)"], capture_output=True)
    else:
        os.chmod(path, 0o666)


class _Names:
    """Stands in for the `secrets` module: hands out the given names first, then fresh ones."""

    def __init__(self, first):
        self.first = list(first)
        self.n = 0

    def token_hex(self, nbytes=32):
        self.n += 1
        if self.first:
            return self.first.pop(0)
        return f"fresh{self.n:04d}".ljust(2 * nbytes, "0")


def _one_run(hroot, monkeypatch, grant, plant, run):
    """One real run_temp with names planted just before its owner record is written. Returns differences."""
    suffix = "" if run == "suite" else "-p1"
    name = f"task-x-{os.getpid()}{suffix}"
    owner = hroot.root / f"{name}.owner"
    k = {"pid-thread": 0, "first-1": 1, "first-5": 5}[plant]
    tokens = [f"guess{i:02d}".ljust(16, "0") for i in range(k)]
    monkeypatch.setattr(H, "secrets", _Names(tokens), raising=False)
    planted: list[Path] = []
    real_sweep = H.sweep_other_roots

    def plant_then_sweep(root):  # after run_temp's start-up clean-up, just before the owner record is written
        r = real_sweep(root)
        if plant == "pid-thread":
            names = [f"{owner.name}.{os.getpid()}.{threading.get_ident()}.tmp"]
        else:
            names = [f"{owner.name}.{os.getpid()}.{t}.tmp" for t in tokens]
        for n in names:
            _plant(owner.with_name(n), grant)
            planted.append(owner.with_name(n))
        return r

    monkeypatch.setattr(H, "sweep_other_roots", plant_then_sweep)
    diffs = []
    label = f"{grant} × {plant} × {run}"

    def body():
        with H.run_temp(suffix) as d:
            if not d.is_dir():
                diffs.append(f"{label}: no run folder")
            if _changeable_by_others(owner):
                diffs.append(f"{label}: owner record can be changed by other users")
            if H.read_record(owner).get("pid") != os.getpid():
                diffs.append(f"{label}: owner record doesn't name this run ({H.read_record(owner)})")
            for p in planted:
                if not p.exists() or p.read_bytes() != PLANTED:
                    diffs.append(f"{label}: planted file {p.name} was opened, moved or changed")

    if run == "suite":
        body()
    else:
        with ThreadPoolExecutor(1) as ex:
            ex.submit(body).result()
    for p in planted:
        p.unlink(missing_ok=True)
    monkeypatch.setattr(H, "sweep_other_roots", real_sweep)
    return diffs


@pytest.mark.parametrize("grant", GRANTS)
def test_rv28_planted_temp_name_sweep(hroot, monkeypatch, grant):
    """BR-9 [sweep] RV-28: generator: a shared run root that lets other users create files in it × a file another
    user made in advance under the owner record's temp name {the old predictable pid.thread-id name, the first
    1 / first 5 names handed out} granting {Authenticated Users, Users, Everyone full control; POSIX: mode 666}
    × run kind {suite in the main thread, probe in a worker thread}. Oracle (BR-9: a live run's folder is never
    removed; Q-20): the owner record is never a file someone else made, so no other user can change it, and
    the planted file is left as it was (never opened, moved or written)."""
    hroot.root.mkdir(parents=True, exist_ok=True)
    if NT:
        subprocess.run(["icacls", str(hroot.root), "/grant", f"*{grant}:(OI)(CI)(M)"], capture_output=True)
    else:
        os.chmod(hroot.root, 0o1777)
    diffs = []
    cases = list(itertools.product(PLANTS, RUNS))
    for plant, run in cases:
        diffs += _one_run(hroot, monkeypatch, grant, plant, run)
    print(f"RV-28 sweep ({grant}): {len(cases)} runs, {len(diffs)} differ")
    assert not diffs, "\n  ".join(diffs)


@pytest.mark.parametrize("private", [True, False])
def test_rv28_every_name_taken_never_writes_a_planted_file(hroot, monkeypatch, private):
    """RV-28: when every name handed out is already taken, the record writer gives up with FileExistsError
    rather than writing into a file someone else made; the planted files and the old record stay as they were."""
    hroot.root.mkdir(parents=True, exist_ok=True)
    rec = hroot.root / "x.owner"
    rec.write_bytes(b'{"pid": 2}')
    tokens = [f"taken{i:03d}".ljust(16, "0") for i in range(100)]
    for t in tokens:
        _plant(hroot.root / f"x.owner.{os.getpid()}.{t}.tmp", GRANTS[0])
    monkeypatch.setattr(H, "secrets", _Names(tokens), raising=False)
    with pytest.raises(FileExistsError):
        H._write_record(rec, {"pid": os.getpid()}, private=private)
    assert rec.read_bytes() == b'{"pid": 2}'
    for t in tokens:
        assert (hroot.root / f"x.owner.{os.getpid()}.{t}.tmp").read_bytes() == PLANTED
