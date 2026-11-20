# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""Regression sweeps for review round 2 of issues #16/#25/#30, one per defect class (§11.2):

- RV-11 (with RV-9) "undecodable record crashes the run": record bytes (random bytes, invalid UTF-8, other
  encodings, deep nesting, non-ASCII digits, out-of-range pids) × the record's role (owner, slot, lock)
  (BR-2, BR-5, BR-9, BR-14).
- RV-12 "environment-dependent start token": the reader's TZ × locale variables × ps's own variables; the
  environment `ps` runs in, and a fake `ps` that formats by that environment, must give one token
  (BR-2, BR-5, BR-9).
- RV-8 "entry in a dead run's place": entry kind × owner record × the name's pid × the name's suffix (BR-9).

Machine-wide state stays in pytest's temp folder (tests/lh_machine.py). Real processes are `sys.executable`
children only.
"""
from __future__ import annotations

import itertools
import json
import os
import random
import stat
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
from lh_machine import isolate  # noqa: E402


@pytest.fixture
def hroot(monkeypatch, tmp_path):
    env = isolate(monkeypatch, tmp_path / "machine", slots=4)
    repo = T.mini_repo(tmp_path / "repo")
    T.point(monkeypatch, H, repo)
    monkeypatch.setattr(H, "SLOT_POLL", 0.05)
    H.LEFTOVERS.clear()
    return SimpleNamespace(tmp=tmp_path, repo=repo, root=Path(env["LIGHT_HARNESS_TEMP_ROOT"]))


@pytest.fixture(scope="module")
def dead_pid():
    c = T.sleeper(0)
    c.wait()
    return c.pid


# ---------------------------------------------------------------- RV-11: record contents

def _contents(n_random: int = 160) -> list[bytes]:
    """Record bytes: hand-built shapes plus seeded random bytes of mixed lengths."""
    fixed = [
        b"", b"\xff", b"\xfe\xff", b"\x80junk", b"\xc3", b"\xe2\x82", b"\xed\xa0\x80", b"\xf8\x88\x80\x80\x80",
        b"\x00", b"\x00\x00\x00", b"\xef\xbb\xbf", b"\xef\xbb\xbf{\"pid\": 5}",  # a BOM before a record
        '{"pid": 5}'.encode("utf-16"), '{"pid": 5}'.encode("utf-32"), '{"pid": 5}'.encode("cp1252"),
        "1234".encode("utf-16-le"), "²".encode(), "١٢".encode(), "１２".encode(),
        b"12\xff", b"\xff12", b"{\"pid\": 5, \"start\": \"\xff\"}", b"[" * 100000, b"{\"a\":" * 50000,
        b"1" * 5000, b"{\"pid\": 1e400}", b"{\"pid\": NaN}", b"{\"pid\": Infinity}", b"null", b"true", b"3.5",
        b"-7", b"\"12\"", b"[1, 2]", b"{\"pid\": 99999999999999999999999}", b"{\"pid\": -99999999999999}",
        b"{\"pid\": 4294967296}", b"{\"pid\": 2147483648}", b"   \r\n\t ", b"garbage",
    ]
    rnd = random.Random(1102)
    rand = [bytes(rnd.randrange(256) for _ in range(rnd.choice([1, 2, 3, 7, 16, 64, 300])))
            for _ in range(n_random)]
    return fixed + rand


def _expected(raw: bytes) -> dict:
    """Oracle for read_record: UTF-8 text of 1 to 10 ASCII digits is a legacy pid (longer is no pid: {});
    UTF-8 text holding a JSON object is that object; anything else (undecodable, not an object,
    unparsable) is {}."""
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return {}
    if text and text.isascii() and text.isdigit():
        return {"pid": int(text)} if len(text) <= 10 else {}
    try:
        d = json.loads(text)
    except (ValueError, RecursionError):
        return {}
    return d if isinstance(d, dict) else {}


def test_rv11_read_record_never_raises_sweep(tmp_path):
    """BR-9 [sweep] RV-11: generator: record bytes {empty, lone and truncated UTF-8 lead bytes, surrogates,
    5-byte forms, NULs, BOMs, the record in UTF-16/UTF-32/cp1252, non-ASCII digits, deep nesting, huge and
    non-finite numbers, non-object JSON} plus 160 seeded random byte strings. Oracle: read_record never
    raises and returns what `_expected` gives."""
    diffs = []
    for i, raw in enumerate(_contents()):
        p = tmp_path / f"rec-{i}"
        p.write_bytes(raw)
        try:
            got = H.read_record(p)
        except Exception as e:  # noqa: BLE001 - the defect class is "raises"
            diffs.append(f"{raw[:24]!r}: raised {type(e).__name__}")
            continue
        exp = _expected(raw)
        if not isinstance(got, dict) or json.dumps(got, sort_keys=True, default=str) != \
                json.dumps(exp, sort_keys=True, default=str):
            diffs.append(f"{raw[:24]!r}: {got!r:.60} != {exp!r:.60}")
    print(f"RV-11 read_record sweep: {len(_contents())} contents, {len(diffs)} differ")
    assert not diffs, "\n  ".join(diffs[:10])


BAD = [b"\xff", b"\x80junk", b"\xc3", '{"pid": 5}'.encode("utf-16"), b"[" * 100000,
       b"{\"pid\": 99999999999999999999999}", b"{\"pid\": 2147483648}", b"\xef\xbb\xbf{\"pid\": 5}",
       "²".encode(), b"{\"pid\": 5, \"start\": \"\xff\"}"]


@pytest.mark.parametrize("raw", BAD, ids=[f"c{i}" for i in range(len(BAD))])
@pytest.mark.parametrize("role", ["owner", "slot", "lock"])
def test_rv11_bad_record_doesnt_stop_runs_sweep(hroot, dead_pid, role, raw):
    """BR-9 [sweep] RV-11: generator: record role {run-folder owner, machine slot, task run lock} × record
    bytes {undecodable, UTF-16, deep nesting, out-of-range pids, a BOM, non-ASCII digit, bad start}.
    Oracle: nothing raises; a record with no usable pid counts as a dead holder: the dead run's folder is
    removed, the slot is freed and taken, the lock is taken over."""
    if role == "owner":
        hroot.root.mkdir(parents=True, exist_ok=True)
        d = hroot.root / f"other-{dead_pid}"
        d.mkdir()
        (hroot.root / f"other-{dead_pid}.owner").write_bytes(raw)
        removed = H.sweep_temp_root(hroot.root)
        assert removed == [d.name] and not d.exists()
        assert not (hroot.root / f"other-{dead_pid}.owner").exists()
    elif role == "slot":
        from lh_machine import machine_paths, write_gitconfig
        write_gitconfig(machine_paths(hroot.tmp / "machine")["gitconfig"], 1)
        (H.slot_dir() / "slot-0.json").write_bytes(raw)
        t0 = time.monotonic()
        with H.run_slot("verify"):
            assert H.read_record(H.slot_dir() / "slot-0.json").get("pid") == os.getpid()
        assert time.monotonic() - t0 < 5
    else:
        lock = hroot.tmp / "base-lock-x"
        lock.write_bytes(raw)
        try:
            H._take_lock(lock)
            assert H.read_record(lock).get("pid") == os.getpid()
        finally:
            lock.unlink(missing_ok=True)
            H.HELD_LOCK.clear()


@pytest.mark.parametrize("pid", [0, -1, 2 ** 31, 2 ** 32 + 7, 2 ** 63, 10 ** 30, -(2 ** 40)])
@pytest.mark.parametrize("where", ["owner", "name", "holder"])
def test_rv11_out_of_range_pid_sweep(hroot, dead_pid, pid, where):
    """BR-9 [sweep] RV-11: generator: pid {0, negative, just past 32 bits, 64-bit, huge, hugely negative} ×
    where it sits {owner record, run folder's name, a lock or slot holder's record}. Oracle: never raises;
    it isn't a live process: an owner with it falls back to the name's (dead) pid, a folder named with it is
    removed, a holder with it is dead."""
    hroot.root.mkdir(parents=True, exist_ok=True)
    if where == "owner":
        d = hroot.root / f"other-{dead_pid}"
        d.mkdir()
        T.write_json(hroot.root / f"other-{dead_pid}.owner", {"pid": pid, "start": "1"})
        H.sweep_temp_root(hroot.root)
        assert not d.exists()
    elif where == "name":
        d = hroot.root / f"other-{abs(pid)}"
        d.mkdir()
        H.sweep_temp_root(hroot.root)
        assert not d.exists()
    else:
        assert H.holder_alive(pid, "1") is False
        assert H.holder_alive(pid, None) is False


# ---------------------------------------------------------------- RV-12: start token and environment

FAKE_PS = r"""
import os
# like `ps -o lstart=`: local time of one fixed start, in the locale's format
print("%s|%s" % (os.environ.get("TZ", "<local>"),
                 os.environ.get("LC_ALL") or os.environ.get("LC_TIME") or os.environ.get("LANG") or "<default>"),
      os.environ.get("PS_FORMAT", ""), os.environ.get("PS_PERSONALITY", ""))
"""

TZS = [None, "UTC", "Asia/Karachi", "America/New_York", "Europe/Berlin", ":/etc/localtime", "garbage+3"]
LOCALES = [None, "C", "POSIX", "de_DE.UTF-8", "ja_JP.UTF-8", "xx_YY.bogus"]
LOCALE_VARS = ["LANG", "LC_ALL", "LC_TIME", "LANGUAGE"]
PS_VARS = [{}, {"PS_FORMAT": "pid,lstart"}, {"PS_PERSONALITY": "bsd"}]


def _envs(n: int = 120):
    rnd = random.Random(1203)
    combos = list(itertools.product(TZS, LOCALES, LOCALE_VARS, range(len(PS_VARS))))
    rnd.shuffle(combos)
    return combos[:n]


def test_rv12_ps_start_token_independent_of_reader_env_sweep(monkeypatch, tmp_path):
    """BR-9 [sweep] RV-12: generator: the harness process's TZ {unset, UTC, three zones, a path, garbage} ×
    locale value {unset, C, POSIX, two locales, garbage} × locale variable {LANG, LC_ALL, LC_TIME,
    LANGUAGE} × ps's own variables {none, PS_FORMAT, PS_PERSONALITY}; 120 stratified combinations. Each
    runs the real `_run_ps` with a fake ps that prints a token from the environment it was started with,
    as `ps -o lstart=` formats by it. Oracle: every environment gives the same token, and ps sees TZ=UTC
    and LC_ALL=C."""
    fake = tmp_path / "fake_ps.py"
    fake.write_text(FAKE_PS)
    real_run = subprocess.run
    seen_env = []

    def run(argv, **kw):
        seen_env.append(kw.get("env"))
        return real_run([sys.executable, str(fake)] + list(argv[1:]), **kw)

    monkeypatch.setattr(H.subprocess, "run", run)
    tokens = {}
    for tz, loc, var, ps_i in _envs():
        with monkeypatch.context() as m:
            for k in ["TZ", "LANGUAGE", "PS_FORMAT", "PS_PERSONALITY"] + \
                     [k for k in os.environ if k == "LANG" or k.startswith("LC_")]:
                m.delenv(k, raising=False)
            if tz is not None:
                m.setenv("TZ", tz)
            if loc is not None:
                m.setenv(var, loc)
            for k, v in PS_VARS[ps_i].items():
                m.setenv(k, v)
            rc, out = H._run_ps(["ps", "-o", "lstart=", "-p", "1"])
            tokens[(tz, loc, var, ps_i)] = (rc, out.strip())
    distinct = set(tokens.values())
    print(f"RV-12 sweep: {len(tokens)} environments, {len(distinct)} distinct tokens")
    assert len(distinct) == 1, sorted(distinct)[:6]
    assert all(e and e.get("TZ") == "UTC" and e.get("LC_ALL") == "C" for e in seen_env)
    assert not any(k.startswith("PS_") or k == "LANGUAGE" for e in seen_env for k in e)


def test_rv12_ps_env_keeps_the_rest(monkeypatch):
    """RV-12: ps still runs with the rest of the harness's environment (PATH, HOME), so it is found."""
    monkeypatch.setenv("LH_UNRELATED_X", "kept")
    env = H.ps_env()
    assert env.get("LH_UNRELATED_X") == "kept" and env.get("PATH") == os.environ.get("PATH")


# ---------------------------------------------------------------- RV-8: entry in a dead run's place

def _kinds():
    """Entry kinds this OS can make without extra privileges."""
    kinds = ["empty-file", "file", "read-only-file", "folder", "nested-folder", "read-only-folder"]
    kinds.append("folder-link")  # a junction on Windows, a symlink elsewhere
    if os.name != "nt":
        kinds += ["file-link", "dangling-link", "fifo"]
    return kinds


def _make(kind: str, path: Path, target_dir: Path):
    if kind == "empty-file":
        path.write_bytes(b"")
    elif kind == "file":
        path.write_text("x" * 100)
    elif kind == "read-only-file":
        path.write_text("x")
        os.chmod(path, stat.S_IREAD)
    elif kind == "folder":
        path.mkdir()
    elif kind == "nested-folder":
        (path / "a" / "b").mkdir(parents=True)
        (path / "a" / "b" / "f.txt").write_text("x")
    elif kind == "read-only-folder":
        path.mkdir()
        (path / "f.txt").write_text("x")
        os.chmod(path / "f.txt", stat.S_IREAD)
        if os.name != "nt":
            os.chmod(path, stat.S_IREAD | stat.S_IEXEC)
    elif kind == "folder-link":
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(target_dir), str(path))
        else:
            os.symlink(target_dir, path)
    elif kind == "file-link":
        os.symlink(target_dir / "keep.txt", path)
    elif kind == "dangling-link":
        os.symlink(target_dir / "nowhere", path)
    elif kind == "fifo":
        os.mkfifo(path)


OWNERS = ["none", "dead-owner", "live-owner", "damaged-owner"]
SUFFIXES = ["", "-p3", "-r2", "-p1-r4"]


def test_rv8_entry_in_a_runs_place_sweep(hroot, dead_pid, tmp_path):
    """BR-9 [sweep] RV-8: generator: entry kind {empty, plain and read-only file, plain, nested and read-only
    folder, link to a folder; on POSIX also a link to a file, a dangling link, a FIFO} × owner file {none,
    a dead run's, a live run's, damaged bytes} × the pid in the name {dead, this live process} × name suffix
    {none, -p3, -r2, -p1-r4}. Oracle: the run is dead by its owner record, or by the name's pid when there
    is no usable owner; a dead run's entry is removed (with its owner) if it is a folder or a link, or if an
    owner file sits next to it; a file with no owner, and every live run's entry, is kept; a link's target
    is never touched."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    hroot.root.mkdir(parents=True, exist_ok=True)
    me = {"pid": os.getpid(), "start": H.proc_start(os.getpid()) or ""}
    diffs, n = [], 0
    for kind, owner, name_pid, suffix in itertools.product(_kinds(), OWNERS, ["dead", "live"], SUFFIXES):
        n += 1
        pid = dead_pid if name_pid == "dead" else os.getpid()
        name = f"case{n}-{pid}{suffix}"
        p = hroot.root / name
        _make(kind, p, target)
        o = hroot.root / f"{name}.owner"
        if owner == "dead-owner":
            T.write_json(o, {"pid": dead_pid, "start": "1"})
        elif owner == "live-owner":
            T.write_json(o, me)
        elif owner == "damaged-owner":
            o.write_bytes(b"\xff\xfe\x00")
        H.sweep_temp_root(hroot.root)
        if owner == "dead-owner":
            dead = True
        elif owner == "live-owner":
            dead = False
        else:
            dead = name_pid == "dead"
        container = kind in ("folder", "nested-folder", "read-only-folder", "folder-link", "file-link",
                             "dangling-link")
        expect_gone = dead and (container or owner != "none")
        gone = not os.path.lexists(p)
        if gone != expect_gone:
            diffs.append(f"{kind} × {owner} × {name_pid} × {suffix!r}: removed={gone}, expected {expect_gone}")
        if expect_gone and gone and os.path.lexists(o):
            diffs.append(f"{kind} × {owner} × {name_pid} × {suffix!r}: owner file left")
        if (target / "keep.txt").read_text() != "keep":
            diffs.append(f"{kind} × {owner}: link target touched")
            (target / "keep.txt").write_text("keep")
    print(f"RV-8 sweep: {n} cases, {len(diffs)} differ")
    assert not diffs, "\n  ".join(diffs[:10])
