"""Test support for issue #24 (guard latency): fixture repos, running `guard.sh` end to end, the BR-1 / BR-2 /
BR-4 [sweep] input spaces and the normaliser that makes outcomes comparable across fixtures and platforms.

Used by tests/test_guard_sh_preservation.py, tests/test_guard_latency.py, tests/gen_guard_sh_golden.py and
tests/bench_guard.py. Nothing here reads .light-harness-base/: the generator is given a base export folder,
and the tests compare against tests/golden/guard_sh_base.json.

Every environment variable a run needs is set through a Python env dict (R-11), never on a shell command line.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import random
import re
import shutil
import stat
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
PLUGIN = REPO / "plugins" / "light-harness"
LIVE_SCRIPTS = (PLUGIN / "scripts").resolve()
sys.path.insert(0, str(HERE))
from lh_machine import relink_worktrees  # noqa: E402
GOLDEN = HERE / "golden" / "guard_sh_base.json"
GOLDEN_RV9 = HERE / "golden" / "guard_sh_rv9_base.json"
PLATFORM = "nt" if os.name == "nt" else "posix"
STATUSES = ["Draft", "Plan", "Build", "Review", "Rework"]
SID = "sweep-r"                      # the session id of every recorded-mode stdin
WORKERS = max(4, min(16, os.cpu_count() or 4))
SH = shutil.which("sh") or "/bin/sh"
REAL_PY = sys.executable
NOT_FOUND = ("[light-harness] Blocked: Python 3.9+ not found (tried python3, python, py -3), so the harness guard "
             "can't run. Install Python 3.9+ and make it available on PATH.")
TELEMETRY_MSG = ("[light-harness] Blocked: telemetry settings and the engineer's opt-in belong to the human. "
                 "Ask them to change it.")
ENV_MSG_START = "[light-harness] Blocked: .env holds secrets"


def fwd(p) -> str:
    """A native path with forward slashes (what Claude Code passes as $0 on Windows, F-1)."""
    return str(p).replace("\\", "/")


# --- fixture --------------------------------------------------------------------------------------------

def git_env(root: Path) -> dict:
    """A clean environment: no LIGHT_HARNESS_*, GIT_*, CLAUDE* or HARNESS_ROOT from the developer, git config
    isolated to the fixture."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("LIGHT_HARNESS_", "GIT_", "CLAUDE")) and k not in (
               "HARNESS_ROOT", "PYTHONPATH", "MSYS_NO_PATHCONV", "FAKEPY_LOG", "SUBPROC_LOG", "GITLOG")}
    env.update(GIT_CONFIG_GLOBAL=str(Path(root) / "gitconfig"), GIT_CONFIG_NOSYSTEM="1",
               PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    return env


def _git(*args, cwd, env):
    r = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}: {r.stderr}")
    return r.stdout


def unversion(s: str) -> str:
    """A golden event string with the harness release version as <V>, as normalise() writes live events."""
    return re.sub(r'"harness": "[^"]*"', '"harness": "<V>"', s)


def _write(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:   # Path.write_text has newline= only from 3.10
        f.write(text)


def info(root: Path) -> dict:
    root = Path(root).resolve()
    m = root / "proj"
    common = m / ".git"
    return {"root": root, "M": m, "T": {s: m / ".tasks" / s.lower() for s in STATUSES}, "N": root / "notask",
            "O": root / "outside", "common": common, "state": common / "light-harness",
            "events": common / "light-harness" / "telemetry" / "events.jsonl", "gitconfig": root / "gitconfig"}


def fixture(tmp) -> dict:
    """Build a fixture under `tmp`: a main checkout `proj` with task worktrees draft/plan/build/review/rework in
    those statuses (started with the live harness's `start-task`), a repo with no task (`notask`) and a folder
    outside any repo (`outside`). Returns info(tmp)."""
    root = Path(tmp).resolve()
    root.mkdir(parents=True, exist_ok=True)
    _write(root / "gitconfig", "[user]\n\tname = Fixture\n\temail = fixture@example.invalid\n"
                               "[init]\n\tdefaultBranch = main\n")
    fx = info(root)
    env = git_env(root)
    m = fx["M"]
    m.mkdir()
    _git("init", "-q", cwd=m, env=env)
    for rel, text in {"src/__init__.py": "", "src/a.py": "x = 1\n", "src/x.py": "y = 2\n", "README.md": "# proj\n",
                      "tests/test_a.py": "def test_a():\n    assert True\n", ".gitignore": "*.pyc\n"}.items():
        _write(m / rel, text)
    _git("add", "-A", cwd=m, env=env)
    _git("commit", "-qm", "existing project", cwd=m, env=env)
    for s in STATUSES:
        r = subprocess.run([REAL_PY, str(LIVE_SCRIPTS / "harness.py"), "start-task", s.lower()], cwd=m, env=env,
                           capture_output=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise RuntimeError(f"start-task {s.lower()} failed: {r.stdout}{r.stderr}")
    for s in STATUSES:
        c = fx["T"][s] / ".light-harness" / "contract.md"
        text = c.read_text(encoding="utf-8")
        new = re.sub(r"^Status: .*$", f"Status: {s}", text, count=1, flags=re.M)
        assert new != text or s == "Draft", f"no Status line in {c}"
        c.write_bytes(new.encode("utf-8"))
        for rel in ("dir with space/ü.py", ".env", ".env.local", ".env.example"):
            _write(fx["T"][s] / rel, "x\n")
    n = fx["N"]
    n.mkdir()
    _git("init", "-q", cwd=n, env=env)
    _write(n / "a.py", "x\n")
    _git("add", "-A", cwd=n, env=env)
    _git("commit", "-qm", "init", cwd=n, env=env)
    _write(fx["O"] / "a.py", "x\n")
    return fx


def copy_fixture(src_root, dest) -> dict:
    """A working copy of a fixture at `dest` (the worktree links repaired)."""
    shutil.copytree(src_root, dest, symlinks=True)
    fx = info(dest)
    relink_worktrees(fx["M"], [fx["T"][s] for s in STATUSES], git_env(fx["root"]))
    return fx


_TEMPLATES: dict = {}


def template(tmp_path_factory) -> dict:
    """One fixture per test session, shared by the test modules (copy it before changing it)."""
    key = id(tmp_path_factory)
    if key not in _TEMPLATES:
        _TEMPLATES[key] = fixture(tmp_path_factory.mktemp("gsh") / "fx")
    return _TEMPLATES[key]


# --- interpreters, wrappers and PATHs --------------------------------------------------------------------

def script(path: Path, body: str, mtime_offset: float = -100.0) -> Path:
    """An executable `#!/bin/sh` script (MSYS runs these on Windows too). Its mtime is moved by
    `mtime_offset` seconds, so by default it is older than a record written afterwards."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(("#!/bin/sh\n" + body).encode("utf-8"))
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    t = time.time() + mtime_offset
    os.utime(path, (t, t))
    return path


def recording_python(path: Path, mtime_offset: float = -100.0) -> Path:
    """A fake interpreter: appends its arguments (one line) to $FAKEPY_LOG, if set, then runs the real Python."""
    return script(path, '[ -n "$FAKEPY_LOG" ] && printf \'%s\\n\' "$*" >> "$FAKEPY_LOG"\n'
                        f'exec "{fwd(REAL_PY)}" "$@"\n', mtime_offset)


def stub(path: Path, code: int, mtime_offset: float = -100.0) -> Path:
    """A fake interpreter that can't run anything: logs like recording_python, then exits `code`."""
    return script(path, f'[ -n "$FAKEPY_LOG" ] && printf \'%s\\n\' "$*" >> "$FAKEPY_LOG"\nexit {code}\n',
                  mtime_offset)


def wrap_dir(d: Path) -> Path:
    """A folder holding a recording `python3` (first on PATH: logs every probe and run)."""
    recording_python(d / "python3")
    return d


def old_python_dir(d: Path) -> Path:
    """python3, python and py that fail the 3.9+ probe (they exit 1 on everything)."""
    for name in ("python3", "python", "py"):
        stub(d / name, 1)
    return d


TOOL_NAMES = ["sh", "bash", "dash", "cat", "dirname", "basename", "mkdir", "rm", "rmdir", "mv", "cp", "ls", "head",
              "tail", "sed", "grep", "tr", "cut", "find", "touch", "date", "env", "git", "printf", "test", "chmod",
              "readlink", "realpath", "wc", "sort", "uniq", "true", "false", "sleep", "id", "uname", "stat", "tee",
              "expr", "mktemp", "ln", "xargs", "od"]


def tools_path(root: Path) -> list:
    """PATH entries giving the shell and git but no Python at all."""
    if PLATFORM == "nt":
        dirs = [str(Path(SH).parent), str(Path(shutil.which("git")).parent)]
        for d in dirs:
            assert not any((Path(d) / f"{n}{x}").exists() for n in ("python", "python3", "py")
                           for x in ("", ".exe")), f"{d} holds a Python"
        return list(dict.fromkeys(dirs))
    d = Path(root) / "tools"
    if not d.is_dir():
        d.mkdir(parents=True)
        for n in TOOL_NAMES:
            w = shutil.which(n)
            if w:
                (d / n).symlink_to(os.path.realpath(w))
    return [str(d)]


def path_with(*front, rest=None) -> str:
    rest = os.environ.get("PATH", "") if rest is None else os.pathsep.join(rest)
    return os.pathsep.join([str(f) for f in front] + ([rest] if rest else []))


def guard_env(fx: dict, project=None, **extra) -> dict:
    """The hook environment Claude Code gives guard.sh: project dir, CLAUDECODE, no bytecode writing."""
    env = git_env(fx["root"])
    env.update(CLAUDECODE="1", CLAUDE_PROJECT_DIR=str(project or fx["M"]))
    env.update({k: str(v) for k, v in extra.items()})
    return env


def write_record(fx: dict, sid: str, content: str) -> Path:
    p = fx["state"] / "py" / sid
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content.encode("utf-8"))
    return p


def clear_record(fx: dict, sid: str):
    try:
        (fx["state"] / "py" / sid).unlink()
    except FileNotFoundError:
        pass


# --- running ----------------------------------------------------------------------------------------------

@dataclass
class Outcome:
    exit: int
    stdout: str
    stderr: str
    events: list

    @property
    def decision(self):
        try:
            return json.loads(self.stdout)["hookSpecificOutput"]["permissionDecision"]
        except (ValueError, KeyError, TypeError):
            return None

    @property
    def rule(self):
        return self.events[0].get("rule") if self.events else None

    @property
    def stderr_norm(self):
        return self.stderr.strip()


def _new_events(log: Path, before: int) -> list:
    if not log or not log.exists():
        return []
    with open(log, "rb") as f:
        f.seek(before)
        raw = f.read()
    out = []
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append({"unparsable": line})
    return out


def _size(log) -> int:
    try:
        return Path(log).stat().st_size
    except (OSError, TypeError):
        return 0


def run_cmd(argv, stdin: bytes, env: dict, events=None, cwd=None, timeout=180) -> Outcome:
    before = _size(events)
    r = subprocess.run(argv, input=stdin, env=env, cwd=cwd, capture_output=True, timeout=timeout)
    return Outcome(r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace"),
                   _new_events(events, before))


def run_guard(scripts_dir, stdin: bytes, env: dict, events=None, cwd=None) -> Outcome:
    """`sh <scripts_dir>/guard.sh` with this stdin and environment; `events` is the fixture's telemetry log,
    whose new lines become Outcome.events."""
    return run_cmd([SH, fwd(Path(scripts_dir) / "guard.sh")], stdin, env, events, cwd)


def run_hook_guard(scripts_dir, stdin: bytes, env: dict, events=None, cwd=None) -> Outcome:
    """The interpreter run directly on <scripts_dir>/hook_guard.py (the BR-4 oracle)."""
    return run_cmd([REAL_PY, str(Path(scripts_dir) / "hook_guard.py")], stdin, env, events, cwd)


def run_raw(argv, stdin: bytes, env: dict, cwd=None, timeout=180) -> tuple:
    """(exit, stdout bytes, stderr bytes), nothing decoded or stripped."""
    r = subprocess.run(argv, input=stdin, env=env, cwd=cwd, capture_output=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def run_guard_raw(scripts_dir, stdin: bytes, env: dict, cwd=None) -> tuple:
    """`sh <scripts_dir>/guard.sh` with raw output: (exit, stdout bytes, stderr bytes), for the byte-exact tests
    (RV-11, F-13: CRLF line ends are kept)."""
    return run_raw([SH, fwd(Path(scripts_dir) / "guard.sh")], stdin, env, cwd)


def run_hook_guard_raw(scripts_dir, stdin: bytes, env: dict, cwd=None, python=None) -> tuple:
    """`python` (default the real interpreter) run directly on <scripts_dir>/hook_guard.py, raw output."""
    return run_raw([python or REAL_PY, str(Path(scripts_dir) / "hook_guard.py")], stdin, env, cwd)


def git_logger(d: Path) -> Path:
    """A `git` wrapper in folder `d` (put it first on PATH): appends its arguments ("$*", one line) to $GITLOG,
    when set, then execs the real git. Returns the wrapper's path."""
    real = shutil.which("git")
    assert real, "no git on PATH"
    return script(Path(d) / "git", '[ -n "$GITLOG" ] && printf \'%s\\n\' "$*" >> "$GITLOG"\n'
                                   f'exec "{fwd(real)}" "$@"\n')


# A sitecustomize.py (put its folder on PYTHONPATH) that, when $SUBPROC_LOG is set, logs "started", then
# `env LIGHT_HARNESS_GIT_COMMON=<value>` (or `=<unset>`) as this Python sees it, then every subprocess's argv
# as one JSON list per line.
SITECUSTOMIZE = '''import json, os, subprocess
_log = os.environ.get("SUBPROC_LOG")
if _log:
    with open(_log, "a", encoding="utf-8") as f:
        f.write("started\\n")
        f.write("env LIGHT_HARNESS_GIT_COMMON=" + os.environ.get("LIGHT_HARNESS_GIT_COMMON", "<unset>") + "\\n")
    _init = subprocess.Popen.__init__
    def _logged(self, args, *a, **k):
        with open(_log, "a", encoding="utf-8") as f:
            f.write(json.dumps([str(x) for x in (args if isinstance(args, (list, tuple)) else [args])]) + "\\n")
        _init(self, args, *a, **k)
    subprocess.Popen.__init__ = _logged
'''


# --- normalising ------------------------------------------------------------------------------------------

def _forms(root: Path) -> list:
    s = str(root)
    forms = {s, s.replace("\\", "/"), json.dumps(s)[1:-1]}
    if PLATFORM == "nt" and len(s) > 2 and s[1] == ":":
        forms.add("/" + s[0].lower() + s[2:].replace("\\", "/"))
        forms.add("/" + s[0].upper() + s[2:].replace("\\", "/"))
    real = os.path.realpath(s)
    forms.add(real)
    return sorted(forms, key=len, reverse=True)


def norm_text(text: str, fx: dict) -> str:
    for f in _forms(fx["root"]):
        text = re.sub(re.escape(f), "<F>", text, flags=re.I if PLATFORM == "nt" else 0)
    return text.replace("<F>\\", "<F>/")


def _norm_value(v, fx):
    if isinstance(v, str):
        return norm_text(v, fx)
    if isinstance(v, list):
        return [_norm_value(x, fx) for x in v]
    if isinstance(v, dict):
        return {k: _norm_value(x, fx) for k, x in v.items()}
    return v


def normalise(out: Outcome, fx: dict, sid=None) -> tuple:
    """(exit, stdout, stderr, events) with fixture paths as <F>, `ts` dropped, the stdin's session id as <S> and
    a Python traceback as <traceback>. Comparable across fixtures, sessions and runs."""
    so = out.stdout.strip()
    try:
        so = json.dumps(json.loads(so), sort_keys=True, ensure_ascii=False) if so else ""
    except ValueError:
        pass
    se = out.stderr.strip()
    if "Traceback (most recent call last)" in se:
        se = "<traceback>"
    evs = []
    for e in out.events:
        e = {k: v for k, v in e.items() if k != "ts"}
        if "harness" in e:   # the release version, not the guard's behaviour (goldens: unversion())
            e["harness"] = "<V>"
        if sid is not None and e.get("session") == sid:
            e["session"] = "<S>"
        evs.append(_norm_value(e, fx))
    return (out.exit, norm_text(so, fx), norm_text(se, fx), json.dumps(evs, sort_keys=True, ensure_ascii=False))


# --- a pool of fixture copies (one per worker, so each run's telemetry events are its own) -----------------

class Pool:
    def __init__(self, template_root, base_dir, n: int = WORKERS):
        base_dir = Path(base_dir)
        base_dir.mkdir(parents=True, exist_ok=True)
        with ThreadPoolExecutor(n) as ex:
            self.fixtures = list(ex.map(lambda i: copy_fixture(template_root, base_dir / f"w{i}"), range(n)))
        self.n = n

    def map(self, fn, items):
        """[fn(fixture, item) for item in items], each fixture used by one run at a time."""
        free: queue.Queue = queue.Queue()
        for fx in self.fixtures:
            free.put(fx)

        def one(item):
            fx = free.get()
            try:
                return fn(fx, item)
            finally:
                free.put(fx)

        with ThreadPoolExecutor(self.n) as ex:
            return list(ex.map(one, items))


# --- BR-1 input space ---------------------------------------------------------------------------------------

FILE_TOOLS = ["Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "Glob"]   # Glob: a name the matcher doesn't guard
# (id, where, path): where T = the status's task folder, M = main checkout, O = outside any repo, N = repo with no
# task, REL = relative to the hook's cwd (the task folder)
TARGETS = [
    ("src", "T", "src/a.py"), ("env", "T", ".env"), ("env_local", "T", ".env.local"),
    ("env_example", "T", ".env.example"), ("env_sample", "T", ".env.sample"), ("env_template", "T", ".env.template"),
    ("env_example_local", "T", ".env.example.local"), ("envrc", "T", ".envrc"), ("ENV", "T", ".ENV"),
    ("xenv", "T", "x.env"), ("space_unicode", "T", "dir with space/ü.py"), ("relative", "REL", "src/a.py"),
    ("contract_owned", "T", ".light-harness/contract.md"), ("contract_unowned", "T", ".light-harness/contract.md"),
    ("config", "T", ".light-harness/config.json"), ("junit", "T", ".light-harness/junit.xml"),
    ("base_copy", "T", ".light-harness-base/x.py"), ("test", "T", "tests/test_a.py"),
    ("agents", "T", ".claude/agents/x.md"), ("git_state", "M", ".git/light-harness/config.json"),
    ("main", "M", "src/x.py"), ("outside", "O", "a.py"), ("notask", "N", "a.py"),
]
TARGET_IDS = [t[0] for t in TARGETS]
FILE_SHAPES = ["spaced", "reversed", "pretty", "esc_slash", "u_dot", "altsep", "no_ti", "null_ti", "truncated"]


def scenarios_files() -> list:
    """BR-1 parts (1) and (2): tool x target x status in the compact Claude Code shape (690), then tool x target in
    Build x 9 JSON shapes (1,242)."""
    out = [{"part": "files", "tool": tool, "target": t, "status": s, "shape": "cc"}
           for s in STATUSES for tool in FILE_TOOLS for t in TARGET_IDS]
    out += [{"part": "shapes", "tool": tool, "target": t, "status": "Build", "shape": shape}
            for shape in FILE_SHAPES for tool in FILE_TOOLS for t in TARGET_IDS]
    return out


# Every command in tests/selftest.sh's `= 2` and `= 0` guard loops and its hookout checks at issue #24's start
# (alpha -> build), frozen here so later selftest edits don't move the golden.
SELFTEST_CMDS = [
    "light-harness approve --task build --name X", "python3 /p/scripts/harness.py unlock-tests --name X --reason y",
    "git checkout main", "git -C .tasks/build reset --hard", "git commit --amend -m x", "git branch -D task/build",
    "git push --force origin x", "git update-ref -d refs/light-harness/tasks/build", "rm -rf src",
    "sed -i s/Draft/Build/ .tasks/build/.light-harness/contract.md",
    "echo x > .tasks/build/.light-harness/config.json", "script -qc 'light-harness approve' /dev/null",
    "light-harness telemetry opt-in --as Bob", "git config --global lightHarness.engineer Bob",
    "LIGHT_HARNESS_TELEMETRY=0 light-harness verify", "env -u LIGHT_HARNESS_SESSION_ID light-harness verify",
    "cat .git/light-harness/telemetry/events.jsonl", "LIGHT_HARNESS_HUMAN_GAP_CAP=1s light-harness verify",
    "git config lightharness.humanGapCap 1s", "sed -i 's/a/b/' .tasks/build/.light-harness/contract.md",
    "sed -ni 's/a/b/' .tasks/build/.light-harness/contract.md",
    "sed -i.bak 's/a/b/' .tasks/build/.light-harness/contract.md",
    "sed --in-place 's/a/b/' .tasks/build/.light-harness/contract.md",
    "awk -i inplace '{print}' .tasks/build/.light-harness/contract.md",
    "echo x > .tasks/build/.light-harness/contract.md", "echo x >> .tasks/build/.light-harness/contract.md",
    "cat x | tee .tasks/build/.light-harness/contract.md", "cp x .tasks/build/.light-harness/contract.md",
    "mv x .tasks/build/.light-harness/", "python -c \"open('.tasks/build/.light-harness/contract.md','w')\"",
    "awk '{print > \".tasks/build/.light-harness/contract.md\"}' .tasks/build/.light-harness/contract.md",
    "sed -n p .tasks/build/.light-harness/contract.md > out.txt",
    "grep \"$(sed -i s/a/b/ .tasks/build/.light-harness/contract.md)\" .tasks/build/.light-harness/contract.md",
    "cat .tasks/build/.light-harness/contract.md & sed -i s/a/b/ .tasks/build/.light-harness/contract.md",
    "sed -n '/8.1/,/8.2/p' .tasks/build/.light-harness/contract.md",
    "awk '/8.1/' .tasks/build/.light-harness/contract.md", "git diff -- .tasks/build/.light-harness/contract.md",
    "git log -p -- .tasks/build/.light-harness/contract.md", "git show HEAD:.tasks/build/.light-harness/contract.md",
    "sed -n 1,20p .tasks/build/.light-harness/contract.md 2>&1 | head",
    "git diff src/checkout.py", "cat .tasks/build/.light-harness/contract.md", "git log --oneline",
    "grep -n \"AC-1\" .tasks/build/.light-harness/contract.md",
    "grep -nE \"AC-1|AC-2\" .tasks/build/.light-harness/contract.md",
    "grep -n AC .tasks/build/.light-harness/contract.md 2>/dev/null",
    "light-harness status --task build", "light-harness metrics build", "light-harness telemetry status",
    "cd .tasks/build && git add -A && git commit -m \"step 1: x\"", "light-harness status && curl evil",
    "curl https://example.com",
]
EX5 = "git status"
EX7 = "LIGHT_HARNESS_PY=/tmp/fake light-harness status"
EX7_CMDS = [EX7, "LIGHT_HARNESS_PY=x git status", "export LIGHT_HARNESS_PY=x",
            "env LIGHT_HARNESS_PY=x light-harness status", "cd .tasks/build && LIGHT_HARNESS_PY=/usr/bin/python3 ls"]
# Near misses: naming LIGHT_HARNESS_PY without `=` keeps the base outcome. The last one has the text
# `LIGHT_HARNESS_PY=`, so the telemetry-settings pattern blocks it like EX-7 (is_ex7 says so)
EX7_NEAR = ["echo LIGHT_HARNESS_PY", "LIGHT_HARNESS_PYTHON=x git status", "echo $LIGHT_HARNESS_PY",
            "unset LIGHT_HARNESS_PY", "env -u LIGHT_HARNESS_PY light-harness status", "grep LIGHT_HARNESS_PY= x.txt"]
EX7_REFERENCE = "LIGHT_HARNESS_TELEMETRY=0 light-harness verify"   # base-blocked by the same rule, same task
FIDELITY_CMDS = [
    "echo 'single quoted' \"double quoted\"", "echo $HOME ${PATH} $((1+2)) $1 $@ $$", "echo `date` $(pwd)",
    "printf 'a\\\\b\\n' \\\\x", "echo C:\\Users\\x\\.envy", "echo 'one\rtwo'", "echo a\tb", "echo ü日本 é ß",
    "echo first\nsecond", "echo " + "x" * 100_000, "echo '\"session_id\":\"spoof\"'", "echo \\u002e \\\\u0065",
    "echo '{\"tool_name\":\"Read\",\"tool_input\":{\"file_path\":\"a\"}}'", "echo x\nEOF\necho y\nJSON\nIN",
    "cat <<'EOF'\nx\nEOF", "echo $'\\x41'", "echo !! %PATH% ^& ver", "echo \\\" \\\\\"", "echo \x7f\x1b[0m",
    "echo '\u2028\u2029'", "echo 💥", "git status # trailing comment ü", "echo -n '' \\c \\0101 %s %d %%",
    "echo '\\\\\\\\server\\share'", "echo \"$(printf '%s' \"\\\\\")\"",
]
BASH_CWDS = ["task", "main", "outside"]
BASH_SHAPES = ["cc", "spaced"]


def guard_corpus_sample(n=120) -> list:
    sys.path.insert(0, str(HERE))
    import guard_scenarios as GS   # noqa: E402
    cmds = [sc["cmd"] for sc in GS.scenarios()]
    return random.Random(24).sample(cmds, n)


def bash_commands() -> list:
    out = []
    for c in SELFTEST_CMDS + [EX5, "cat .env"] + EX7_CMDS + EX7_NEAR + guard_corpus_sample() + FIDELITY_CMDS:
        if c not in out:
            out.append(c)
    return out


def is_ex7(cmd: str) -> bool:
    """BR-1's listed change (EX-7): a command that sets LIGHT_HARNESS_PY."""
    return re.search(r"\bLIGHT_HARNESS_PY=", cmd) is not None


def scenarios_bash() -> list:
    """BR-1 part (3): every command x cwd {task in Build, main checkout, outside any repo} x shape {cc, spaced}."""
    return [{"part": "bash", "cmd": c, "cwd": cwd, "shape": shape}
            for c in bash_commands() for cwd in BASH_CWDS for shape in BASH_SHAPES]


def br1_scenarios() -> list:
    return scenarios_files() + scenarios_bash()


def key(sc: dict) -> str:
    return json.dumps(sc, sort_keys=True, ensure_ascii=False)


def sha256(scs: list) -> str:
    h = hashlib.sha256()
    for sc in scs:
        h.update(key(sc).encode("utf-8") + b"\n")
    return h.hexdigest()


def target_path(fx: dict, target: str, status: str):
    """(value for file_path, the hook's cwd) for a BR-1 target."""
    tid, where, rel = next(t for t in TARGETS if t[0] == target)
    t = fx["T"][status]
    if where == "REL":
        return rel, t
    base = {"T": t, "M": fx["M"], "O": fx["O"], "N": fx["N"]}[where]
    return str(base / rel), (t if where in ("T", "M") else base)


def tool_input(tool: str, target: str, path: str, status: str) -> dict:
    old = {"contract_owned": f"Status: {status}", "contract_unowned": "## 7. Exploration"}.get(target, "a")
    content = {"contract_owned": "Status: Done\n"}.get(target, "x")
    if tool == "Read":
        return {"file_path": path}
    if tool == "Edit":
        return {"file_path": path, "old_string": old, "new_string": "b"}
    if tool == "Write":
        return {"file_path": path, "content": content}
    if tool == "MultiEdit":
        return {"file_path": path, "edits": [{"old_string": old, "new_string": "b"}]}
    if tool == "NotebookEdit":
        return {"notebook_path": path, "new_source": "x"}
    return {"file_path": path}


def _altsep(p: str) -> str:
    return p.replace("\\", "/") if PLATFORM == "nt" else p.replace("/", "\\")


def cc_payload(fx: dict, sid, cwd, tool: str, ti, **extra) -> dict:
    """The compact Claude Code PreToolUse shape (F-1 key order)."""
    d = {}
    if sid is not None:
        d["session_id"] = sid
    d.update(transcript_path=str(fx["root"] / "t.jsonl"), cwd=str(cwd), permission_mode="default",
             hook_event_name="PreToolUse", tool_name=tool, tool_input=ti, tool_use_id="toolu_01")
    d.update(extra)
    return d


def compact(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def render(sc: dict, fx: dict, sid=SID) -> bytes:
    """The hook stdin for a BR-1 scenario in fixture `fx`."""
    if sc["part"] == "bash":
        cwd = {"task": fx["T"]["Build"], "main": fx["M"], "outside": fx["O"]}[sc["cwd"]]
        if sc["shape"] == "spaced":   # the selftest's bash_json
            return json.dumps({"tool_name": "Bash", "cwd": str(cwd), "tool_input": {"command": sc["cmd"]}}).encode()
        ti = {"command": sc["cmd"], "description": "run it"}
        return compact(cc_payload(fx, sid, cwd, "Bash", ti)).encode("utf-8")
    tool, target, status, shape = sc["tool"], sc["target"], sc["status"], sc["shape"]
    path, cwd = target_path(fx, target, status)
    if shape == "altsep":
        path = _altsep(path)
        cwd = _altsep(str(cwd))
    if shape == "esc_slash":
        path, cwd = fwd(path), fwd(cwd)
    ti = tool_input(tool, target, path, status)
    if shape == "spaced":   # the selftest's file_json shape: json.dumps defaults, no session id
        return json.dumps({"tool_name": tool, "cwd": str(cwd), "tool_input": ti}).encode()
    d = cc_payload(fx, sid, cwd, tool, ti)
    if shape == "no_ti":
        del d["tool_input"]
    if shape == "null_ti":
        d["tool_input"] = None
    if shape == "reversed":
        d = dict(reversed(list(d.items())))
    if shape == "pretty":
        return json.dumps(d, ensure_ascii=False, indent=2).encode("utf-8")
    text = compact(d)
    if shape == "esc_slash":
        text = text.replace("/", "\\/")
    if shape == "u_dot":
        text = text.replace(".", "\\u002e")
    if shape == "truncated":
        text = text[: len(text) // 2]
    return text.encode("utf-8")


def br1_project(sc: dict, fx: dict):
    return fx["M"]


def run_br1(scripts_dir, fx: dict, sc: dict, sid=SID, env=None) -> tuple:
    """The normalised outcome of one BR-1 scenario through <scripts_dir>/guard.sh."""
    out = run_guard(scripts_dir, render(sc, fx, sid), env or guard_env(fx), fx["events"], cwd=fx["M"])
    return normalise(out, fx, sid)


# --- BR-4 input space (the Read fast path) -----------------------------------------------------------------

BR4_BASENAMES = ["README.md", "a.py", ".env", ".env.local", ".env.example", ".env.sample", ".env.template", ".ENV",
                 ".Env", "x.env", ".envrc", ".env.example.local", "ü.py", "a b.py", "env", ".env.", "a.env.b",
                 'q"uote.py', "日本.md", ".environment", "foo.ENV.bak", ".env.EXAMPLE", "config.json", "contract.md"]
SAFE_BASENAMES = ["README.md", "a.py", "ü.py", "a b.py", "env", "日本.md", "config.json", "contract.md", 'q"uote.py']
BR4_DIRS = ["task", "task_src", "task_lh", "task_base", "draft_src", "main", "main_git", "notask", "outside"]
BR4_TOOLS = ["Read", "read", "Edit", "Write", "NotebookEdit", "Bash", "missing", "dup_read_edit", "dup_edit_read",
             "nested_read", "nested_only"]
BR4_TYPES = ["str", "num", "null", "list", "obj"]
PATH_STYLES = ["json", "fwd", "slash", "winsep", "u2e", "u65", "ascii"]
PLAIN_STYLES = ["json", "fwd", "slash", "winsep"]
TOP_KEYS = ["session_id", "transcript_path", "cwd", "permission_mode", "hook_event_name", "tool_name", "tool_input",
            "tool_use_id"]
CORRUPTIONS = ["trunc", "append", "prepend_space", "bom", "nul", "double", "garbage", "array"]
N_BR4 = 3000


def _spec_plain(r: random.Random) -> dict:
    return {"order": "cc", "ws": "compact", "tool": "Read", "dir": r.choice(BR4_DIRS),
            "base": r.choice(SAFE_BASENAMES if r.random() < 0.75 else BR4_BASENAMES),
            "rel": r.random() < 0.1, "fp_type": "str", "ti_type": "obj", "cwd_type": "str",
            "cwd_dir": r.choice(["task", "main", "outside"]), "esc_fp": r.choice(PLAIN_STYLES),
            "esc_cwd": r.choice(PLAIN_STYLES), "nb": None, "extra": r.random() < 0.3,
            "sid": "rec" if r.random() < 0.95 else r.choice(["other", "missing", "bad"]),
            "agent": r.random() < 0.3, "corrupt": None}


def _perturb(spec: dict, r: random.Random) -> dict:
    dim = r.choice(["order", "ws", "tool", "fp_type", "ti_type", "cwd_type", "esc_fp", "esc_cwd", "nb", "corrupt",
                    "base"])
    s = dict(spec)
    if dim == "order":
        keys = TOP_KEYS[:]
        r.shuffle(keys)
        s["order"] = keys
    elif dim == "ws":
        s["ws"] = r.choice(["spaced", "pretty", "loose"])
    elif dim == "tool":
        s["tool"] = r.choice(BR4_TOOLS[1:])
    elif dim in ("fp_type", "ti_type", "cwd_type"):
        s[dim] = r.choice(BR4_TYPES[1:] + (["missing"] if dim == "cwd_type" else []))
    elif dim in ("esc_fp", "esc_cwd"):
        s[dim] = r.choice(["u2e", "u65", "ascii"])
    elif dim == "nb":
        s["nb"] = r.choice(["same", "env", "null"])
    elif dim == "corrupt":
        s["corrupt"] = [r.choice(CORRUPTIONS), round(r.random(), 3)]
    else:
        s["base"] = r.choice([b for b in BR4_BASENAMES if b not in SAFE_BASENAMES])
    return s


def _spec_random(r: random.Random) -> dict:
    keys = TOP_KEYS[:]
    r.shuffle(keys)
    return {"order": "cc" if r.random() < 0.5 else keys, "ws": r.choice(["compact", "spaced", "pretty", "loose"]),
            "tool": r.choice(BR4_TOOLS), "dir": r.choice(BR4_DIRS), "base": r.choice(BR4_BASENAMES),
            "rel": r.random() < 0.15, "fp_type": r.choice(BR4_TYPES) if r.random() < 0.3 else "str",
            "ti_type": r.choice(BR4_TYPES[1:] + ["obj"]) if r.random() < 0.2 else "obj",
            "cwd_type": r.choice(BR4_TYPES + ["missing"]) if r.random() < 0.25 else "str",
            "cwd_dir": r.choice(["task", "main", "outside"]), "esc_fp": r.choice(PATH_STYLES),
            "esc_cwd": r.choice(PATH_STYLES), "nb": r.choice([None, None, None, "same", "env", "null"]),
            "extra": r.random() < 0.3, "sid": r.choice(["rec", "rec", "rec", "other", "missing"]),
            "agent": r.random() < 0.3,
            "corrupt": [r.choice(CORRUPTIONS), round(r.random(), 3)] if r.random() < 0.2 else None}


def scenarios_shapes() -> list:
    """BR-4 [sweep] inputs: N_BR4 hook stdins drawn with random.Random(24) from the BR-4 input space. About a
    third are the plain Claude Code Read shape (the fast path's), a quarter are that shape with one dimension
    perturbed (key order, whitespace, tool name, a value's type, a \\u escape, notebook_path, corruption or a
    .env-like basename), and the rest are drawn at random over every dimension."""
    r = random.Random(24)
    out = []
    for i in range(N_BR4):
        x = r.random()
        if x < 0.35:
            spec = _spec_plain(r)
        elif x < 0.6:
            spec = _perturb(_spec_plain(r), r)
        else:
            spec = _spec_random(r)
        spec["i"] = i
        out.append(spec)
    return out


def _jstr(s: str, style: str = "json") -> str:
    """A JSON string literal for `s`, escaped in `style`."""
    if style == "fwd":
        return json.dumps(s.replace("\\", "/"), ensure_ascii=False)
    if style == "slash":
        return json.dumps(s.replace("\\", "/"), ensure_ascii=False).replace("/", "\\/")
    if style == "winsep":
        return json.dumps(s.replace("/", "\\"), ensure_ascii=False)
    if style == "u2e":
        return json.dumps(s, ensure_ascii=False).replace(".", "\\u002e")
    if style == "u65":
        return json.dumps(s, ensure_ascii=False).replace("e", "\\u0065")
    if style == "ascii":
        return json.dumps(s, ensure_ascii=True)
    return json.dumps(s, ensure_ascii=False)


def _typed(kind: str, lit: str) -> str:
    return {"str": lit, "num": "5", "null": "null", "list": "[" + lit + "]", "obj": '{"p":' + lit + "}"}[kind]


def _dump(pairs, ws: str, depth: int = 0) -> str:
    """An object from (raw key literal, raw value) pairs, keys possibly repeated, in whitespace style `ws`."""
    if ws == "pretty":
        pad, inner = "  " * depth, "  " * (depth + 1)
        if not pairs:
            return "{}"
        return "{\n" + ",\n".join(f"{inner}{k}: {v}" for k, v in pairs) + "\n" + pad + "}"
    colon, comma = {"compact": (":", ","), "spaced": (": ", ", "), "loose": (" : ", " ,\n ")}[ws]
    return "{" + comma.join(f"{k}{colon}{v}" for k, v in pairs) + "}"


def br4_paths(spec: dict, fx: dict):
    t = fx["T"]["Build"]
    d = {"task": t, "task_src": t / "src", "task_lh": t / ".light-harness", "task_base": t / ".light-harness-base",
         "draft_src": fx["T"]["Draft"] / "src", "main": fx["M"], "main_git": fx["M"] / ".git", "notask": fx["N"],
         "outside": fx["O"]}[spec["dir"]]
    cwd = {"task": t, "main": fx["M"], "outside": fx["O"]}[spec["cwd_dir"]]
    p = d / spec["base"]
    path = os.path.relpath(p, cwd) if spec["rel"] else str(p)
    return path, str(cwd)


def render_shape(spec: dict, fx: dict, sid=SID) -> bytes:
    """The hook stdin for a BR-4 spec in fixture `fx` (deterministic)."""
    path, cwd = br4_paths(spec, fx)
    ws = spec["ws"]
    fp = _typed(spec["fp_type"], _jstr(path, spec["esc_fp"]))
    ti_pairs = []
    tool = spec["tool"]
    if tool in ("nested_read", "nested_only"):
        ti_pairs.append(('"tool_name"', '"Read"'))
    ti_pairs.append(('"file_path"', fp))
    if spec["extra"]:
        ti_pairs += [('"offset"', "1"), ('"limit"', "20")]
    if spec["nb"]:
        nb_path = {"same": path, "env": str(Path(path).parent / ".env")}.get(spec["nb"])
        ti_pairs.append(('"notebook_path"', "null" if nb_path is None else _jstr(nb_path, spec["esc_fp"])))
    ti_obj = _dump(ti_pairs, ws, 1)
    ti = {"obj": ti_obj, "str": _jstr("file_path"), "num": "7", "null": "null", "list": "[" + ti_obj + "]"}[
        spec["ti_type"]]
    sid_val = {"rec": sid, "other": f"o-{spec['i']}", "missing": None, "bad": "a/b"}[spec["sid"]]
    first_tool = {"Read": "Read", "read": "read", "Edit": "Edit", "Write": "Write", "NotebookEdit": "NotebookEdit",
                  "Bash": "Bash", "missing": None, "dup_read_edit": "Read", "dup_edit_read": "Edit",
                  "nested_read": "Edit", "nested_only": None}[tool]
    values = {"session_id": None if sid_val is None else _jstr(sid_val),
              "transcript_path": _jstr(str(fx["root"] / "t.jsonl"), spec["esc_cwd"]),
              "cwd": None if spec["cwd_type"] == "missing" else _typed(spec["cwd_type"], _jstr(cwd, spec["esc_cwd"])),
              "permission_mode": '"default"', "hook_event_name": '"PreToolUse"',
              "tool_name": None if first_tool is None else _jstr(first_tool), "tool_input": ti,
              "tool_use_id": '"toolu_01"'}
    order = TOP_KEYS if spec["order"] == "cc" else spec["order"]
    pairs = [(f'"{k}"', values[k]) for k in order if values[k] is not None]
    if tool in ("dup_read_edit", "dup_edit_read"):
        second = '"Edit"' if tool == "dup_read_edit" else '"Read"'
        at = [k for k, _ in pairs].index('"tool_input"') + 1
        pairs.insert(at, ('"tool_name"', second))
    if spec["agent"]:
        pairs.append(('"agent_type"', '"light-harness:implementer"'))
    text = _dump(pairs, ws)
    c = spec["corrupt"]
    if c:
        kind, frac = c
        cut = int(len(text) * frac)
        text = {"trunc": lambda: text[:cut], "append": lambda: text + ' {"tool_name":"Edit"}',
                "prepend_space": lambda: " \n" + text, "bom": lambda: "\ufeff" + text,
                "nul": lambda: text[:cut] + "\x00" + text[cut:], "double": lambda: text + text,
                "garbage": lambda: text[:cut] + "\x01}{]\\" + text[cut:], "array": lambda: "[" + text + "]"}[kind]()
    return text.encode("utf-8")


def br4_sid(spec: dict, sid=SID):
    return {"rec": sid, "other": f"o-{spec['i']}", "missing": None, "bad": "a/b"}[spec["sid"]]


# --- BR-1 [sweep] part (4): unencodable paths (RV-9's class, F-15) ----------------------------------------------

# A NUL everywhere; lone surrogates only on POSIX (Windows paths are UTF-16, so they aren't unencodable there)
UNENC_CHARS = ["0000"] + (["d800", "dfff", "dc80"] if PLATFORM == "posix" else [])
# where the bad character goes in a task-folder path; `?` marks it
UNENC_POSITIONS = {"after_env_local": ".env.local?", "after_env": ".env?", "in_name": "src/a?.py",
                   "in_folder": "s?/a.py", "in_env_folder": ".env?/x"}
UNENC_FILE_TOOLS = ["Read", "Edit", "Write"]
UNENC_STATUSES = ["Draft", "Build"]
HUMAN_ONLY_CMD = SELFTEST_CMDS[0]   # a human-only harness command with --task build
UNENC_BASH_CMDS = [HUMAN_ONLY_CMD, "git status", "light-harness status --task build", "git push",
                   "cp x .light-harness/contract.md"]


def scenarios_unencodable() -> list:
    """BR-1 [sweep] part (4), a full cross product: Read, Edit and Write of a task-folder path holding a bad
    character (a NUL; on POSIX also the lone surrogates U+D800, U+DFFF, U+DC80) at 5 positions, and Bash with cwd
    `<task>/s?` x 5 commands, each in Draft and in Build. The bad character reaches the hook as a JSON `\\u` escape,
    as Claude Code would send it."""
    out = [{"part": "unenc", "tool": tool, "char": c, "pos": pos, "status": s}
           for c in UNENC_CHARS for s in UNENC_STATUSES for tool in UNENC_FILE_TOOLS for pos in UNENC_POSITIONS]
    out += [{"part": "unenc", "tool": "Bash", "char": c, "cmd": cmd, "status": s}
            for c in UNENC_CHARS for s in UNENC_STATUSES for cmd in UNENC_BASH_CMDS]
    return out


def render_unencodable(sc: dict, fx: dict, sid=SID) -> bytes:
    """The hook stdin for a part (4) scenario: the Claude Code shape, every non-ASCII character \\u-escaped."""
    t = fx["T"][sc["status"]]
    bad = chr(int(sc["char"], 16))
    if sc["tool"] == "Bash":
        cwd, ti = str(t / "s") + bad, {"command": sc["cmd"], "description": "run it"}
    else:
        path = str(t / UNENC_POSITIONS[sc["pos"]].replace("?", bad))
        cwd, ti = str(t), tool_input(sc["tool"], "src", path, sc["status"])
    d = cc_payload(fx, sid, cwd, sc["tool"], ti)
    return json.dumps(d, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def triple(out: Outcome) -> list:
    """[exit, permissionDecision or None, the first guard event's rule or None]: part (4)'s comparison."""
    return [out.exit, out.decision, out.rule]


def run_unencodable(scripts_dir, fx: dict, sc: dict, sid=SID, env=None) -> list:
    out = run_guard(scripts_dir, render_unencodable(sc, fx, sid), env or guard_env(fx), fx["events"], cwd=fx["M"])
    return triple(out)
