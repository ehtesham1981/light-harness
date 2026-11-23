# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""A temp folder per run (issue #30): AC-5 (BR-8, BR-9, EX-8), AC-6 (BR-10, EX-9) and the BR-9 [sweep].

Suites used here only write into their TEMP when it is inside the test's own temp root, so a harness that
doesn't give them a run folder (the stubs) never litters the user's real temp folder."""
from __future__ import annotations

import json
import ntpath
import os
import pathlib
import posixpath
import re
import stat
import subprocess
import sys
import tempfile
import time
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
from lh_machine import TEMP_ROOT_ENV, isolate, machine_paths  # noqa: E402

TEMP_VARS = ("TEMP", "TMP", "TMPDIR")
LEFTOVER_RE = re.compile(r"⚠ Suite cleanup: (.+?) left (\d+) entries in its temp folder \(more than 50\); "
                         r"the suite's own clean-up looks broken")

# A suite (run as `python3 suite.py spec.json`): reports its TEMP/TMP/TMPDIR and the folder's first listing,
# fills the folder only if it is inside spec["root"], then sleeps and exits as told.
SUITE = r'''
import json, os, stat, sys, time
spec = json.load(open(sys.argv[1], encoding="utf-8"))
t = os.environ.get("TEMP") or ""
info = {k: os.environ.get(k) for k in ("TEMP", "TMP", "TMPDIR")}
info["listing"] = sorted(os.listdir(t)) if os.path.isdir(t) else None
info["pid"] = os.getpid()
with open(spec["report"], "w", encoding="utf-8") as f:
    json.dump(info, f)
root = os.path.realpath(spec["root"])
inside = bool(t) and os.path.realpath(t).startswith(root + os.sep)
kind = spec.get("contents", "empty")
if inside and kind == "files":
    for i in range(spec.get("n", 60)):
        open(os.path.join(t, "f%03d.tmp" % i), "w").write("x")
elif inside and kind == "nested":
    d = t
    for i in range(5):
        d = os.path.join(d, "level%d" % i)
        os.mkdir(d)
        open(os.path.join(d, "x.txt"), "w").write("x")
elif inside and kind == "read-only":
    os.mkdir(os.path.join(t, "sub"))
    for p in (os.path.join(t, "ro.txt"), os.path.join(t, "sub", "ro2.txt")):
        open(p, "w").write("x")
        os.chmod(p, stat.S_IREAD)
if spec.get("ready"):
    open(spec["ready"], "w").close()
time.sleep(spec.get("sleep", 0))
sys.exit(spec.get("rc", 0))
'''

HARNESS_CHILD = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
import harness
harness.set_root(sys.argv[2])
harness.run_tests(dict(harness.DEFAULT_CONFIG, test_cmd=json.loads(sys.argv[3]), junit_arg=""))
'''


@pytest.fixture
def hroot(monkeypatch, tmp_path):
    env = isolate(monkeypatch, tmp_path / "machine", slots=4)
    repo = T.mini_repo(tmp_path / "repo")
    T.point(monkeypatch, H, repo)
    monkeypatch.setattr(H, "SLOT_POLL", 0.05)
    (tmp_path / "suite.py").write_text(SUITE, encoding="utf-8")
    return SimpleNamespace(tmp=tmp_path, repo=repo, machine=tmp_path / "machine", env=env,
                           root=machine_paths(tmp_path / "machine")["temp_root"], suite=tmp_path / "suite.py")


def _cmd(h, name: str, **spec) -> list:
    spec.setdefault("root", str(h.root))
    spec.setdefault("report", str(h.tmp / f"{name}.report.json"))
    sp = T.write_json(h.tmp / f"{name}.spec.json", spec)
    return ["python3", str(h.suite), str(sp)]


def _report(h, name: str) -> dict:
    p = h.tmp / f"{name}.report.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _run(cmd):
    """run_tests in this process; the exception's type name, or None."""
    try:
        H.run_tests(dict(H.DEFAULT_CONFIG, test_cmd=cmd, junit_arg=""))
        return None
    except BaseException as e:  # noqa: BLE001 - the outcome under test
        return type(e).__name__


# --- AC-5: one new, empty folder per run, removed afterwards -------------------------------------
@pytest.mark.parametrize("outcome", ["pass", "fail", "timeout"])
def test_ac5_run_folder_shape_and_removal(hroot, monkeypatch, outcome):
    """AC-5 BR-8 BR-9: the suite sees TEMP = TMP = TMPDIR = <temp root>/<task>-<harness pid> (`task` for a
    checkout that isn't a task), new and empty; after a passing, failing or timed-out run the folder and
    its `.owner` file are gone and nothing else is left under the temp root."""
    if outcome == "timeout":
        monkeypatch.setattr(H, "suite_timeout", lambda cfg: 1)
    spec = {"pass": {"rc": 0}, "fail": {"rc": 1}, "timeout": {"sleep": 6}}[outcome]
    raised = _run(_cmd(hroot, outcome, contents="files", n=3, **spec))
    rep = _report(hroot, outcome)
    assert raised == ("SuiteTimeout" if outcome == "timeout" else None)
    temps = {rep.get(k) for k in TEMP_VARS}
    assert len(temps) == 1, rep
    t = Path(rep["TEMP"])
    assert t.parent == Path(hroot.root) or os.path.samefile(t.parent, hroot.root), rep
    assert t.name == f"task-{os.getpid()}", t.name
    assert rep["listing"] == []
    assert not t.exists() and not Path(str(t) + ".owner").exists()
    assert T.run_folders(hroot.machine) == []
    if outcome == "timeout":
        assert T.wait_for(lambda: H.proc_start(rep["pid"]) is None, 10)
        T.kill_pid(rep["pid"])


def test_ac5_user_temp_unchanged_outside_runs(hroot):
    """AC-5 BR-8 (Q-17): the harness's own TEMP/TMP/TMPDIR are the same before and after a run (set only in
    the child's environment), while the suite saw its run folder."""
    before = {k: os.environ.get(k) for k in TEMP_VARS}
    assert _run(_cmd(hroot, "env")) is None
    rep = _report(hroot, "env")
    assert {k: os.environ.get(k) for k in TEMP_VARS} == before
    assert rep["TEMP"] not in before.values() and Path(rep["TEMP"]).name == f"task-{os.getpid()}", rep


def test_ac5_killed_run_cleaned_by_another_task(hroot, tmp_path):
    """AC-5 BR-9 (Q-5): a harness child killed hard mid-suite leaves its run folder and owner file; the next
    run of a different task (another repo, this process) removes them when it starts."""
    other = T.mini_repo(tmp_path / "other-repo")
    cmd = _cmd(hroot, "killed", contents="files", n=5, sleep=30, ready=str(tmp_path / "killed.ready"))
    child = subprocess.Popen([sys.executable, "-c", HARNESS_CHILD, str(SCRIPTS), str(other), json.dumps(cmd)],
                             cwd=str(other), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    rep = {}
    try:
        assert T.wait_for((tmp_path / "killed.ready").exists, 60), "the killed run's suite never started"
        rep = _report(hroot, "killed")
        child.kill()
        child.wait()
        folder = Path(rep["TEMP"])
        assert folder.name == f"task-{child.pid}" and folder.is_dir(), rep
        assert _run(_cmd(hroot, "next")) is None
        assert not folder.exists() and not Path(str(folder) + ".owner").exists()
        assert T.run_folders(hroot.machine) == []
    finally:
        child.kill()
        T.kill_pid(rep.get("pid"))


class _Proxy(types.ModuleType):
    def __init__(self, real, **over):
        super().__init__(real.__name__)
        self._real = real
        self.__dict__.update(over)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _norm(p) -> str:
    return re.sub(r"/+", "/", str(p).replace("\\", "/")).rstrip("/").lower()


@pytest.mark.parametrize("osname,tempdir,fail_root,expected,warns", [
    ("nt", r"D:\Users\x\Temp", False, "d:/lh-tmp", 0),
    ("nt", r"E:\Temp", False, "e:/lh-tmp", 0),
    ("nt", r"D:\Users\x\Temp", True, "d:/users/x/temp/lh", 1),     # Q-10: root can't be created
    ("posix", "/tmp/xyz", False, "/tmp/xyz/lh", 0),
    ("posix", "/var/folders/ab/T", False, "/var/folders/ab/t/lh", 0),
], ids=["windows-d", "windows-e", "windows-fallback", "posix", "posix-macos-like"])
def test_ac5_temp_root_shape(monkeypatch, capsys, osname, tempdir, fail_root, expected, warns):
    """AC-5 BR-8 (Q-10): without the override the temp root is <drive>:\\lh-tmp on Windows (<drive> = the
    drive of the user's TEMP), falling back to <TEMP>\\lh with one warning line if it can't be created, and
    $TMPDIR/lh elsewhere. Folder creation is intercepted: nothing is created on a real drive."""
    monkeypatch.delenv(TEMP_ROOT_ENV, raising=False)
    made = []

    def fake_mkdir(self, *a, **k):
        made.append(str(self))
        if fail_root and "lh-tmp" in str(self):
            raise PermissionError(13, "denied", str(self))

    def fake_makedirs(p, *a, **k):
        fake_mkdir(Path(p))

    path_mod = ntpath if osname == "nt" else posixpath
    monkeypatch.setattr(pathlib.Path, "mkdir", fake_mkdir)
    monkeypatch.setattr(H, "os", _Proxy(os, name=osname, path=path_mod, sep=path_mod.sep,
                                        makedirs=fake_makedirs, mkdir=lambda p, *a, **k: fake_mkdir(Path(p))))
    monkeypatch.setattr(H, "tempfile", _Proxy(tempfile, gettempdir=lambda: tempdir), raising=False)
    got = H.temp_root()
    monkeypatch.undo()
    assert _norm(got) == expected
    out, err = capsys.readouterr()
    assert len([l for l in (out + err).splitlines() if l.strip()]) == warns, out + err


@pytest.mark.parametrize("value", ["{tmp}/root A", "{tmp}/röot-ü"])
def test_ac5_temp_root_override(monkeypatch, tmp_path, value):
    """AC-5 BR-8 (Q-10): LIGHT_HARNESS_TEMP_ROOT, when set and non-empty, is the temp root itself; empty is
    ignored (the OS default is used)."""
    want = value.format(tmp=tmp_path)
    monkeypatch.setenv(TEMP_ROOT_ENV, want)
    assert os.path.normcase(os.path.abspath(H.temp_root())) == os.path.normcase(os.path.abspath(want))
    monkeypatch.setenv(TEMP_ROOT_ENV, "")
    monkeypatch.setattr(H, "tempfile", _Proxy(tempfile, gettempdir=lambda: str(tmp_path / "usertemp")),
                        raising=False)
    monkeypatch.setattr(pathlib.Path, "mkdir", lambda self, *a, **k: None)  # never create a real drive root
    monkeypatch.setattr(H, "os", _Proxy(os, makedirs=lambda *a, **k: None, mkdir=lambda *a, **k: None))
    got = H.temp_root()
    assert os.path.normcase(os.path.abspath(got)) != os.path.normcase(os.path.abspath(want))
    assert _norm(got).endswith(("/lh-tmp", "/lh"))


# --- EX-8: two tasks at once ----------------------------------------------------------------------
@pytest.fixture(scope="module")
def snaps(tmp_path_factory):
    return T.chain(tmp_path_factory.getbasetemp(), "new")


def test_ac5_ex8_two_tasks_own_empty_temp(snaps, tmp_path, tmp_path_factory):
    """AC-5 EX-8 BR-8 BR-9: two tasks (repo-a, repo-b, two slots) run `verify` at once; each suite sees
    TEMP = TMP = TMPDIR, the two folders differ, both are empty when the suite starts and inside the temp
    root, and both are gone afterwards (no run folder or owner file left)."""
    machine = tmp_path / "machine"
    T.set_slots(machine, 2)
    sandbox = tmp_path_factory.getbasetemp()
    toys = {n: T.clone(snaps["green"], tmp_path / n, sandbox, machine=machine, repo_name=f"repo-{n}") for n in "ab"}
    for n, toy in toys.items():
        seen = tmp_path / f"seen-{n}.json"
        toy.ctl("import json, os, time\nif TEST == 't':\n"
                "    t = os.environ.get('TEMP') or ''\n"
                f"    json.dump({{k: os.environ.get(k) for k in ('TEMP', 'TMP', 'TMPDIR')}} | "
                f"{{'listing': sorted(os.listdir(t)) if os.path.isdir(t) else None}}, open({str(seen)!r}, 'w'))\n"
                "    time.sleep(1.5)\n")
    procs = {n: toy.start("verify") for n, toy in toys.items()}
    for n, p in procs.items():
        out, _ = p.communicate(timeout=300)
        assert p.returncode == 0, out.decode("utf-8", "replace")
    seen = {n: json.loads((tmp_path / f"seen-{n}.json").read_text()) for n in "ab"}
    for n, s in seen.items():
        assert s["TEMP"] == s["TMP"] == s["TMPDIR"] and s["TEMP"], s
        assert s["listing"] == [], s
        assert T.under(s["TEMP"], T.temp_root_of(machine)), s
    assert seen["a"]["TEMP"] != seen["b"]["TEMP"]
    assert not Path(seen["a"]["TEMP"]).exists() and not Path(seen["b"]["TEMP"]).exists()
    assert T.run_folders(machine) == []


# --- AC-6: the leftover-files warning -------------------------------------------------------------
@pytest.mark.parametrize("files,dirs,warn", [(50, 0, False), (51, 0, True), (60, 0, True), (30, 21, True),
                                             (49, 1, False), (0, 0, False)])
def test_ac6_leftover_count_boundary(hroot, files, dirs, warn):
    """AC-6 BR-10 (Q-6): a run leaving more than 50 top-level entries (files, folders) in its temp folder is
    listed in LEFTOVERS with its count; 50 or fewer (whatever is nested inside) are not; the folder is
    removed either way."""
    H.LEFTOVERS.clear()
    with H.run_temp() as tmp:
        tmp = Path(tmp)
        for i in range(files):
            (tmp / f"f{i}.tmp").write_text("x", encoding="utf-8")
        for i in range(dirs):
            d = tmp / f"d{i}"
            d.mkdir()
            for j in range(20):
                (d / f"x{j}").write_text("x", encoding="utf-8")
    assert not tmp.exists()
    got = list(H.LEFTOVERS)
    if warn:
        assert len(got) == 1 and got[0][1] == files + dirs, got
    else:
        assert got == []


def _fill_ctl(test: str, root: Path, n: int, fail: bool) -> str:
    return ("import os, os.path\n"
            f"if TEST == {test!r}:\n"
            "    t = os.environ.get('TEMP') or ''\n"
            f"    if t and os.path.realpath(t).startswith(os.path.realpath({str(root)!r}) + os.sep):\n"
            f"        for i in range({n}):\n"
            "            open(os.path.join(t, 'left%03d.tmp' % i), 'w').write('x')\n"
            + ("    raise AssertionError('lht forced failure')\n" if fail else ""))


def _fails(out: str) -> list:
    return [re.sub(r"\b[0-9a-f]{7,64}\b", "<HEX>", l) for l in out.splitlines() if "✗" in l]


def test_ac6_ex9_failing_suite_leaves_60_files(snaps, tmp_path, tmp_path_factory):
    """AC-6 EX-9 BR-10 BR-9: a failing suite that leaves 60 files in its TEMP: verify fails exactly as it does
    without them (same ✗ lines, exit 1), the folder is removed, and one ⚠ line says the suite left 60
    entries (more than 50) and its own clean-up looks broken."""
    toy = T.clone(snaps["green"], tmp_path / "lab", tmp_path_factory.getbasetemp())
    root = T.temp_root_of(toy.machine)
    toy.ctl(_fill_ctl("t", root, 60, fail=True))
    rc1, out1 = toy.h("verify", "--fresh")
    toy.ctl(_fill_ctl("t", root, 0, fail=True))
    rc2, out2 = toy.h("verify", "--fresh")
    assert rc1 == rc2 == 1, out1
    warn1, warn2 = LEFTOVER_RE.findall(out1), LEFTOVER_RE.findall(out2)
    assert len(warn1) == 1 and warn1[0][1] == "60", out1
    assert warn2 == [], out2
    assert _fails(out1) == _fails(out2) and _fails(out1), (out1, out2)
    assert T.run_folders(toy.machine) == []


def test_ac6_probe_leaving_60_entries_is_named(snaps, tmp_path, tmp_path_factory):
    """AC-6 BR-10 (Q-6): a saved probe that leaves 60 entries in its own temp folder: verify still passes and
    one ⚠ line names the probe (RV-1.1) and the count; the suite (which leaves nothing) isn't named."""
    toy = T.clone(snaps["probe"], tmp_path / "lab", tmp_path_factory.getbasetemp())
    toy.ctl(_fill_ctl("probe", T.temp_root_of(toy.machine), 60, fail=False))
    rc, out = toy.h("verify", "--fresh")
    assert rc == 0, out
    warns = LEFTOVER_RE.findall(out)
    assert len(warns) == 1 and "RV-1.1" in warns[0][0] and warns[0][1] == "60", out
    assert T.run_folders(toy.machine) == []


# --- BR-9 sweep -------------------------------------------------------------------------------------
OUTCOMES = ["pass", "fail", "timeout", "exception in the harness", "harness killed"]
CONTENTS = ["empty", "files", "nested", "read-only"]


def _plant(root: Path, live_pid: int, live_tok: str, dead_pid: int, dead_tok: str) -> dict:
    """The neighbours of a run under the temp root: a live run's folder (owner alive), a dead run's folder
    (owner dead), a folder without an owner file whose pid is gone, and entries the rule doesn't know."""
    root.mkdir(parents=True, exist_ok=True)
    live = root / f"other-{live_pid}"
    live.mkdir()
    (live / "keep.txt").write_text("live run", encoding="utf-8")
    T.write_json(root / f"other-{live_pid}.owner", {"pid": live_pid, "start": live_tok})
    dead = root / f"ghost-{dead_pid}"
    (dead / "sub").mkdir(parents=True)
    (dead / "sub" / "old.txt").write_text("dead run", encoding="utf-8")
    T.write_json(root / f"ghost-{dead_pid}.owner", {"pid": dead_pid, "start": dead_tok})
    (root / f"nobody-{dead_pid}-p2").mkdir()
    keep = {"notes": "dir", f"ghost-12ab": "dir", f"x-{dead_pid}-q1": "dir", f"stray-{dead_pid}": "file"}
    for name, kind in keep.items():
        if kind == "dir":
            (root / name).mkdir()
            (root / name / "f.txt").write_text(name, encoding="utf-8")
        else:
            (root / name).write_text(name, encoding="utf-8")
    return {"live": live, "dead": [dead, root / f"ghost-{dead_pid}.owner", root / f"nobody-{dead_pid}-p2"],
            "keep": keep}


def test_br9_cleanup_sweep(hroot, monkeypatch, tmp_path):
    """AC-5 BR-9 [sweep] BR-8: full cross product of outcome {pass, fail, timeout, exception raised in the harness
    after the suite starts, harness child killed hard (cleaned by the next run of another task)} x folder
    contents {empty, 60 files, nested 5 levels, read-only files} = 20 cases, each with a live run's folder
    (owner: a live child, right start), a dead run's folder (owner: an exited child), a dead run's folder
    without an owner file, and entries the rule doesn't recognise (`notes`, `ghost-12ab`, `x-<pid>-q1`, a
    file `stray-<pid>`) under the root. Oracle: after the run (or after the next run for a killed one) the
    run's folder and owner file don't exist, the dead runs' folders are gone, and the live run's folder,
    its owner file and the unrecognised entries are untouched."""
    other = T.mini_repo(tmp_path / "other-repo")
    live = T.sleeper(600)
    c = T.sleeper(60)
    dead_tok = H.proc_start(c.pid)
    c.kill()
    c.wait()
    dead_pid = c.pid
    live_tok = H.proc_start(live.pid)
    diffs, orphans = [], []
    cases = [(o, k) for o in OUTCOMES for k in CONTENTS]
    try:
        for i, (outcome, contents) in enumerate(cases):
            root = tmp_path / f"root{i}"
            monkeypatch.setenv(TEMP_ROOT_ENV, str(root))
            planted = _plant(root, live.pid, live_tok, dead_pid, dead_tok)
            name = f"c{i}"
            spec = {"root": str(root), "contents": contents, "n": 60, "report": str(tmp_path / f"{name}.rep")}
            spec.update({"pass": {"rc": 0}, "fail": {"rc": 1}, "timeout": {"sleep": 4},
                         "exception in the harness": {"sleep": 1},
                         "harness killed": {"sleep": 30, "ready": str(tmp_path / f"{name}.ready")}}[outcome])
            cmd = ["python3", str(hroot.suite), str(T.write_json(tmp_path / f"{name}.spec", spec))]
            problems = []
            with monkeypatch.context() as m:
                if outcome == "timeout":
                    m.setattr(H, "suite_timeout", lambda cfg: 1)
                if outcome == "exception in the harness":
                    def boom(pid, report=Path(spec["report"])):
                        T.wait_for(report.exists, 30)  # the suite is up and has reported its TEMP
                        raise RuntimeError("harness exception after the suite started")
                    m.setattr(H, "note_suite", boom)
                if outcome == "harness killed":
                    child = subprocess.Popen([sys.executable, "-c", HARNESS_CHILD, str(SCRIPTS), str(other),
                                              json.dumps(cmd)], cwd=str(other), stdin=subprocess.DEVNULL,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if not T.wait_for(Path(spec["ready"]).exists, 60):
                        problems.append("the killed run's suite never started")
                    child.kill()
                    child.wait()
                    _run(_cmd(hroot, f"{name}-next"))  # the next run, of another task, in this process
                else:
                    _run(cmd)
            rep = json.loads(Path(spec["report"]).read_text()) if Path(spec["report"]).exists() else {}
            orphans.append(rep.get("pid"))
            t = rep.get("TEMP") or ""
            if not t or not T.under(t, root):
                problems.append(f"the suite's TEMP {t!r} isn't a run folder under the temp root")
            elif os.path.lexists(t) or os.path.lexists(t + ".owner"):
                problems.append("the run's folder or its owner file is still there")
            gone = [p.name for p in planted["dead"] if os.path.lexists(p)]
            if gone:
                problems.append(f"dead runs' entries kept: {gone}")
            if not ((planted["live"] / "keep.txt").is_file() and (root / f"other-{live.pid}.owner").is_file()):
                problems.append("the live run's folder or owner file was removed")
            lost = [n for n, kind in planted["keep"].items()
                    if not ((root / n).is_dir() if kind == "dir" else (root / n).is_file())]
            if lost:
                problems.append(f"unrecognised entries removed: {lost}")
            if problems:
                diffs.append(f"[{outcome} | {contents}] " + "; ".join(problems))
    finally:
        live.kill()
        live.wait()
        for pid in orphans:
            T.kill_pid(pid)
    print(f"BR-9 sweep: {len(diffs)} of {len(cases)} cases differ from the oracle")
    assert not diffs, f"{len(diffs)} of {len(cases)} differ; first ones:\n  " + "\n  ".join(diffs[:8])
