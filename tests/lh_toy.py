# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""Toy tasks for end-to-end tests of suite runs, run slots, per-run temp folders and timeouts
(issues #16, #25, #30). A plain module, not conftest.py (a protected file).

A toy repo holds a tiny deterministic test runner (`run_toy.py`, the task's `test_cmd`), an existing test
(`tests/test_old.py`) and, inside the task, `tests/test_t.py`. Every toy test calls a hook that runs the
Python file named by `LHT_CTL` (outside the repo) if it exists, with `TEST` set to the hook's name
(`collect-old`, `old`, `collect-t`, `t`, `probe`). A test writes that file to make the suite pass, fail,
error at collection, skip, sleep, spawn a grandchild, write into its TEMP folder, and so on, without
changing the task's tree. The runner's output is deterministic (no timings, no tracebacks), so gate output
can be compared across harness versions and machines.

A chain of snapshots is built once per pytest session (memoised in pytest's base temp folder) for each
harness flavour: `plan` (approved, status Plan), `build` (Build, task test + stub, before the red gate),
`green` (red gate passed, implementation committed, issues RV-1..3 open), `probe` (green plus a saved
passing probe RV-1.1) and `review` (probe, AC-1 ticked, issues resolved, status Review).
`clone()` copies a snapshot into a test's own folder and re-links the task worktree.

Every harness child runs with `machine_env()`: run slots, slot count and temp root stay inside pytest's
temp folder.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from lh_machine import machine_env, machine_paths, write_gitconfig  # noqa: E402

TASK_DIR = HERE.parent
PLUGIN_SRC = TASK_DIR / "plugins" / "light-harness"
BASE_HARNESS = HERE / "base_harness"
SLUG = "toy"
SEP = "|----|----------|--------|------|-------|-------------|------------------|"

DROP_ENV = {"CLAUDECODE", "CLAUDE_PROJECT_DIR", "CLAUDE_ENV_FILE", "CLAUDE_PLUGIN_ROOT", "HARNESS_ROOT",
            "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_CONFIG_GLOBAL"}
DROP_PREFIXES = ("CLAUDE_CODE_", "LIGHT_HARNESS_", "PYTEST_", "OTEL_")

RUNNER = r'''"""Toy test runner: deterministic output, a junit file like pytest's."""
import importlib.util
import os
import sys
from xml.sax.saxutils import quoteattr


def main(argv):
    junit = next((a.split("=", 1)[1] for a in argv if a.startswith("--junitxml=")), None)
    sys.path.insert(0, os.getcwd())
    files = sorted(f for f in os.listdir("tests") if f.startswith("test_") and f.endswith(".py")) \
        if os.path.isdir("tests") else []
    cases, errors = [], []
    for f in files:
        name = "tests." + f[:-3]
        spec = importlib.util.spec_from_file_location(name, os.path.join("tests", f))
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            errors.append("tests/%s: %s: %s" % (f, type(e).__name__, e))
            continue
        for key, fn in list(vars(mod).items()):
            if key.startswith("test_") and callable(fn):
                try:
                    fn()
                    cases.append((name, key, "passed", ""))
                except Exception as e:
                    kind = "skipped" if type(e).__name__ == "Skipped" else "failed"
                    cases.append((name, key, kind, "%s: %s" % (type(e).__name__, e)))
    for e in errors:
        print("toy: ERROR collecting " + e)
    for mod, fn, outcome, msg in cases:
        print("toy: tests/%s.py::%s %s%s" % (mod.split(".")[-1], fn, outcome, " (%s)" % msg if msg else ""))
    counts = [(k, sum(c[2] == k for c in cases)) for k in ("passed", "failed", "skipped")]
    print("toy: " + ", ".join("%d %s" % (v, k) for k, v in counts) + ", %d errors" % len(errors))
    rc = 2 if errors else 5 if not cases else 1 if dict(counts)["failed"] else 0
    if junit and not errors:
        with open(junit, "w", encoding="utf-8") as fh:
            fh.write('<?xml version="1.0" encoding="utf-8"?>\n<testsuites><testsuite name="toy">')
            for mod, fn, outcome, msg in cases:
                child = {"failed": "<failure message=%s/>" % quoteattr(msg),
                         "skipped": "<skipped message=%s/>" % quoteattr(msg)}.get(outcome, "")
                fh.write("<testcase classname=%s name=%s>%s</testcase>" % (quoteattr(mod), quoteattr(fn), child))
            fh.write("</testsuite></testsuites>\n")
    sys.stdout.flush()
    print("toy-stderr: exit %d" % rc, file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''

HOOK = '''import os


def _hook(name):
    p = os.environ.get("LHT_CTL")
    if p and os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            src = f.read()
        exec(compile(src, p, "exec"), {"__name__": "lht_ctl", "TEST": name})
'''
OLD_TEST = HOOK + '''

_hook("collect-old")


def test_double():
    _hook("old")
    from src.m import double
    assert double(2) == 4
'''
TASK_TEST = HOOK + '''

_hook("collect-t")


def test_ac1():
    """AC-1"""
    _hook("t")
    from src.t import triple
    assert triple(2) == 6
'''
PROBE = HOOK + '''

_hook("probe")
print("probe: ok")
'''
IMPL = "def triple(x):\n    return 3 * x\n"
STUB = "def triple(x):\n    raise NotImplementedError\n"

HUMAN_CODE = r"""
import builtins, sys
scripts = sys.argv[1]
sys.path.insert(0, scripts)
import harness
harness.require_human = lambda action: None
builtins.input = lambda prompt="": "APPROVE"
sys.argv = ["light-harness"] + sys.argv[2:]
harness.main()
"""

# ctl snippets (Python run inside the toy tests; TEST is the hook's name)
CTL_PASS = "if TEST == 't':\n    import src.t as _m\n    _m.triple = lambda x: 3 * x\n"
CTL_FAIL = "if TEST in ('t', 'old'):\n    raise AssertionError('lht forced failure')\n"
CTL_COLLECT = "if TEST in ('collect-t', 'collect-old'):\n    raise RuntimeError('lht collection error')\n"
CTL_SKIP = ("class Skipped(Exception):\n    pass\n\n\n"
            "if TEST in ('t', 'old'):\n    raise Skipped('lht skip')\n")
SUITE_CTL = {"pass": CTL_PASS, "fail": CTL_FAIL, "collection error": CTL_COLLECT, "skipped": CTL_SKIP}
PROBE_CTL = {"pass": "",
             "fail": "if TEST == 'probe':\n    print('probe: difference found')\n    raise SystemExit(1)\n",
             "collection error": "if TEST == 'probe':\n    print('probe: broken')\n    raise SystemExit(2)\n",
             "skipped": "if TEST == 'probe':\n    print('probe: nothing to check')\n    raise SystemExit(5)\n"}


def under(p, root) -> bool:
    try:
        Path(os.path.realpath(p)).relative_to(Path(os.path.realpath(root)))
        return True
    except ValueError:
        return False


def write(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def edit(path: Path, *pairs: str) -> None:
    text = Path(path).read_text(encoding="utf-8")
    for old, new in zip(pairs[::2], pairs[1::2]):
        assert old in text, f"{old!r} not in {path}"
        text = text.replace(old, new, 1)
    Path(path).write_text(text, encoding="utf-8")


def wait_for(pred, timeout: float, step: float = 0.05):
    """pred() until it is truthy or `timeout` seconds pass; returns its last value."""
    end = time.monotonic() + timeout
    while True:
        v = pred()
        if v or time.monotonic() > end:
            return v
        time.sleep(step)


def kill_pid(pid) -> None:
    """Cleanup only: stop a leftover test process (never an assertion)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return
    if pid <= 0 or pid == os.getpid():
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=30)
        else:
            os.kill(pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass


def make_plugin(dest: Path, flavour: str) -> Path:
    """A copy of the plugin; flavour "base" swaps in the base-commit harness.py and telemetry.py."""
    shutil.copytree(PLUGIN_SRC, dest, ignore=shutil.ignore_patterns("__pycache__"))
    if flavour == "base":
        for name in ("harness.py", "telemetry.py"):
            shutil.copyfile(BASE_HARNESS / name, dest / "scripts" / name)
    return dest


class Toy:
    """One plugin copy and one toy repo, with its own ctl file, in one machine (slot folder, temp root)."""

    def __init__(self, root: Path, sandbox: Path, machine: Path, repo_name: str = "repo"):
        self.root = Path(root)
        self.sandbox = Path(sandbox)
        self.machine = Path(machine)
        self.plugin = self.root / "plugin"
        self.scripts = self.plugin / "scripts"
        self.repo = self.root / repo_name
        self.task = self.repo / ".tasks" / SLUG
        self.contract = self.task / ".light-harness" / "contract.md"
        self.config = self.task / ".light-harness" / "config.json"
        self.state = self.repo / ".git" / "light-harness"
        self.lock = self.state / f"base-lock-{SLUG}"
        self.cache = self.state / f"verify-cache-{SLUG}.json"
        self.events = self.state / "telemetry" / "events.jsonl"
        self.ctl_file = self.root / "ctl.py"
        self.extra_env: dict = {}

    # --- environment and processes ---------------------------------------------------------
    def env(self, extra: dict | None = None) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k.upper() not in DROP_ENV and not k.upper().startswith(DROP_PREFIXES)}
        env.update(machine_env(self.machine))
        env.update(LHT_CTL=str(self.ctl_file), PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
        env.update(self.extra_env)
        env.update(extra or {})
        return env

    def _cmd(self, args, task: bool = True) -> list:
        return [sys.executable, str(self.scripts / "harness.py"), *args] + (["--task", SLUG] if task else [])

    def run(self, cmd: list, cwd: Path | None = None, extra: dict | None = None, timeout: float = 300) -> tuple:
        r = subprocess.run(cmd, cwd=str(cwd or self.repo), env=self.env(extra), capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=timeout)
        out = (r.stdout + r.stderr).decode("utf-8", errors="replace").replace("\r\n", "\n")
        return r.returncode, out

    def run_split(self, *args: str, extra: dict | None = None) -> tuple:
        """(exit code, stdout, stderr) of a harness command."""
        r = subprocess.run(self._cmd(args), cwd=str(self.repo), env=self.env(extra), capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=300)
        dec = lambda b: b.decode("utf-8", errors="replace").replace("\r\n", "\n")  # noqa: E731
        return r.returncode, dec(r.stdout), dec(r.stderr)

    def h(self, *args: str, extra: dict | None = None, task: bool = True, timeout: float = 300) -> tuple:
        return self.run(self._cmd(args, task), extra=extra, timeout=timeout)

    def ok(self, *args: str, **kw) -> str:
        rc, out = self.h(*args, **kw)
        assert rc == 0, f"`{' '.join(args)}` failed (exit {rc}):\n{out}"
        return out

    def start(self, *args: str, extra: dict | None = None, code: str | None = None) -> subprocess.Popen:
        """A harness command in the background (stdout and stderr in one pipe). `code`: run this Python
        instead of harness.py, with argv [scripts, *args, --task toy] (see HUMAN_CODE)."""
        cmd = [sys.executable, "-c", code, str(self.scripts), *args, "--task", SLUG] if code else self._cmd(args)
        return subprocess.Popen(cmd, cwd=str(self.repo), env=self.env(extra), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)

    def human(self, *args: str, task: bool = True) -> tuple:
        assert under(self.repo, self.sandbox), f"refusing a human-only command outside {self.sandbox}"
        assert under(self.scripts, self.sandbox) and not under(self.repo, TASK_DIR)
        cmd = [sys.executable, "-c", HUMAN_CODE, str(self.scripts), *args] + (["--task", SLUG] if task else [])
        return self.run(cmd)

    def git(self, *args: str, cwd: Path | None = None) -> str:
        rc, out = self.run(["git", *args], cwd=cwd or self.task)
        assert rc == 0, f"git {' '.join(args)} failed:\n{out}"
        return out

    # --- control and observation -------------------------------------------------------------
    def ctl(self, code: str) -> None:
        write(self.ctl_file, code)

    def status(self) -> str:
        m = re.search(r"^Status:\s*(\S+)", self.contract.read_text(encoding="utf-8"), re.M)
        return m.group(1) if m else "?"

    def block(self, name: str) -> str:
        text = self.contract.read_text(encoding="utf-8")
        return text.split(f"<!-- harness:{name} -->", 1)[1].split(f"<!-- /harness:{name} -->", 1)[0].strip()

    def suite_records(self) -> list:
        if not self.events.exists():
            return []
        out = []
        for line in self.events.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("event") == "suite":
                out.append(e)
        return out

    def code(self, text: str) -> None:
        write(self.task / "src" / "t.py", text)


# --- machine-wide state of a test ---------------------------------------------------------------
def set_slots(machine: Path, slots) -> None:
    """`lightharness.slots` in the machine's user-level git config (None: unset)."""
    machine_env(machine)
    write_gitconfig(machine_paths(machine)["gitconfig"], slots)


def slot_files(machine: Path) -> list:
    return sorted(p for p in Path(machine).rglob("slot-*.json"))


def temp_root_of(machine: Path) -> Path:
    return machine_paths(machine)["temp_root"]


def run_folders(machine: Path) -> list:
    root = temp_root_of(machine)
    return sorted(p.name for p in root.iterdir()) if root.is_dir() else []


# --- in-process helpers -------------------------------------------------------------------------
def mini_repo(path: Path) -> Path:
    """A throwaway git repo with one empty commit (not a task checkout: the harness's slug is `task`).
    Call it after isolate(), so git uses the throwaway user-level config."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "start"]):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    return path


def point(monkeypatch, harness, repo: Path) -> None:
    """Point the imported harness at `repo` for one test (undone by monkeypatch)."""
    for name in ("ROOT", "CONTRACT", "CONFIG", "JUNIT"):
        monkeypatch.setattr(harness, name, getattr(harness, name))
    harness.set_root(repo)


def sleeper(seconds: float = 60, pid_file: Path | None = None, start_new_session: bool = False) -> subprocess.Popen:
    """A `sys.executable` child that sleeps (and writes its pid to `pid_file` first)."""
    code = "import os, sys, time\n"
    if pid_file:
        code += f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
    code += f"time.sleep({seconds})\n"
    kw = {"start_new_session": True} if start_new_session else {}
    return subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, **kw)


def write_json(path: Path, data) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# --- the chain of snapshots ---------------------------------------------------------------------
def _build_chain(root: Path, sandbox: Path, flavour: str, suite_timeout) -> dict:
    machine = root / "machine"
    toy = Toy(root / "live", sandbox, machine)
    toy.root.mkdir(parents=True)
    make_plugin(toy.plugin, flavour)
    toy.repo.mkdir()
    toy.git("init", "-q", cwd=toy.repo)
    write(toy.repo / "src" / "__init__.py", "")
    write(toy.repo / "src" / "m.py", "def double(x):\n    return x + x\n")
    write(toy.repo / "run_toy.py", RUNNER)
    write(toy.repo / "tests" / "test_old.py", OLD_TEST)
    write(toy.repo / ".gitignore", ".env\n")
    toy.git("add", "-A", cwd=toy.repo)
    toy.git("commit", "-qm", "existing", cwd=toy.repo)
    toy.ok("start-task", SLUG, task=False)
    edit(toy.contract, "<one or two sentences: what outcome, for whom>", "Triple numbers",
         "| EX-1 | <input> | <expected> | <happy path / edge / error> |", "| EX-1 | 2 | 6 | happy |",
         "- AC-1: <Given … When … Then …>", "- AC-1: Given 2, then 6")
    cfg = json.loads(toy.config.read_text(encoding="utf-8"))
    cfg["test_cmd"] = ["python3", "run_toy.py"]
    if suite_timeout is not None:
        cfg["suite_timeout"] = suite_timeout
    toy.config.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    toy.ok("set-status", "Initialize")
    edit(toy.contract, "| R-1 | <risk> | <L/M/H> | <L/M/H> | <mitigation> |", "| R-1 | none | L | L | none |",
         "| <area> | <change> | <who/what is affected> | <how to undo> |", "| src/t.py | new | none | delete |",
         "- <src/path/or/glob>", "- src/**\n- tests/**", "1. <step> (AC-1)", "1. triple (AC-1)")
    toy.ok("set-status", "Plan")
    rc, out = toy.human("approve", "--name", "Human Approver")
    assert rc == 0, out
    snaps = {}

    def snap(name):
        dest = root / f"snap-{name}"
        shutil.copytree(toy.root, dest, symlinks=True, ignore=shutil.ignore_patterns("machine"))
        snaps[name] = str(dest)

    snap("plan")
    toy.ok("set-status", "Build")
    write(toy.task / "tests" / "test_t.py", TASK_TEST)
    toy.code(STUB)
    toy.git("add", "-A")
    toy.git("commit", "-qm", "tests and stub")
    snap("build")
    toy.ok("redgate")
    toy.code(IMPL)
    toy.git("add", "-A")
    toy.git("commit", "-qm", "triple")
    rows = "".join(f"| RV-{i} | Low | Open | src | timing: c{i} | probe {i} | AC-1 |\n" for i in (1, 2, 3))
    edit(toy.contract, SEP + "\n", SEP + "\n" + rows)
    snap("green")
    probe = toy.root / "probe_toy.py"
    write(probe, PROBE)
    toy.ok("probe", "add", str(probe), "--issue", "RV-1", "--run", f'"{sys.executable}" {{file}} {{base}}')
    snap("probe")
    edit(toy.contract, "- [ ] AC-1", "- [x] AC-1", "| RV-1 | Low | Open", "| RV-1 | Low | Resolved",
         "| RV-2 | Low | Open", "| RV-2 | Low | Resolved", "| RV-3 | Low | Open", "| RV-3 | Low | Resolved")
    toy.ok("set-status", "Review")
    snap("review")
    return snaps


def chain(base_temp: Path, flavour: str = "new", suite_timeout=None) -> dict:
    """Snapshots of a toy task (see the module docstring), built once per pytest session."""
    base_temp = Path(base_temp)
    root = base_temp / f"lhtoy-{flavour}-{suite_timeout or 'default'}"
    done = root / "done.json"
    if done.exists():
        return json.loads(done.read_text(encoding="utf-8"))
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    snaps = _build_chain(root, base_temp, flavour, suite_timeout)
    done.write_text(json.dumps(snaps), encoding="utf-8")
    return snaps


def clone(snapshot, dest: Path, sandbox: Path, machine: Path | None = None, repo_name: str = "repo") -> Toy:
    """A copy of a snapshot in `dest`, its task worktree re-linked, in `machine` (default dest/machine)."""
    snapshot, dest = Path(snapshot), Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    toy = Toy(dest, sandbox, machine or dest / "machine", repo_name)
    shutil.copytree(snapshot / "plugin", toy.plugin, symlinks=True)
    shutil.copytree(snapshot / "repo", toy.repo, symlinks=True)
    for extra in ("probe_toy.py",):
        if (snapshot / extra).exists():
            shutil.copyfile(snapshot / extra, dest / extra)
    toy.git("worktree", "repair", str(toy.task), cwd=toy.repo)
    return toy
