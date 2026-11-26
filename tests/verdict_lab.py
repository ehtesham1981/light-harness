# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""A toy-repo lab for issue #39 (`verdict pass` reuses an anchored verify pass), shared by
tests/test_verdict_reuse.py, tests/test_verdict_fresh_preservation.py and tests/gen_verdict_golden.py.

Adapted from the `Lab` in tests/test_verify_cache.py. One lab = a COPY of plugins/light-harness and a toy git
repo with one task (`speedy`) whose one-test pytest suite counts its own runs in a file. Differences:

- Lab copies are interchangeable: the run counter is named by a *relative* path (`ST_COUNT`, resolved from
  the task folder, where the suite and the probes run) and the global git config is one shared file, so the
  environment part of the reuse key is the same in every copy of a lab, and a pass stored in one copy is a
  valid stored pass in its clones.
- Outside-the-repo fail switches: `suite.fail` / `probe.fail` next to the counter make the toy test / the toy
  probe fail without changing anything the reuse key covers.
- Probe runs are counted too (`probes.count`).
- `forge()` writes a passing cache entry under the *current* reuse key the way an agent importing harness.py
  could: a child process imports the copied harness and calls `store_reuse(reuse_key(...), ...)` (EX-7).

Human-only commands go through `human()` (require_human no-op'd), refused outside the sandbox folder.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import warnings
from pathlib import Path

TASK_DIR = Path(__file__).resolve().parents[1]
PLUGIN_SRC = TASK_DIR / "plugins" / "light-harness"
HARNESS_SRC = PLUGIN_SRC / "scripts" / "harness.py"
SLUG = "speedy"
REF = f"refs/light-harness/tasks/{SLUG}"
HEX64 = re.compile(r"[0-9a-f]{64}")
SEP = "|----|----------|--------|------|-------|-------------|------------------|"
SOURCE_PREFIX = "Suite and probes: "

IMPL = "def triple(x):\n    return 3 * x\n"
IMPL2 = "def triple(x):\n    return x * 3\n"            # same behaviour, different bytes
STUB = "def triple(x):\n    raise NotImplementedError\n"
WRONG = "def triple(x):\n    return 0\n"

# The suite and the probes run in the task folder (repo/.tasks/speedy), three levels below the lab root.
ST_COUNT_REL = "../../../runs.count"

TOY_TEST = '''import os
from src.t import triple

LAB = os.path.dirname(os.path.abspath(os.environ["ST_COUNT"]))


def test_ac1():
    """AC-1"""
    with open(os.environ["ST_COUNT"], "a") as f:
        f.write("x")
    switch_on = os.path.exists(os.path.join(LAB, "suite.fail"))
    assert switch_on is False, "the suite fail switch is on"
    assert triple(2) == 6
'''

TOY_PROBE = '''# toy probe {note}
import os, sys
lab = os.path.dirname(os.path.abspath(os.environ["ST_COUNT"]))
with open(os.path.join(lab, "probes.count"), "a") as f:
    f.write("p")
sys.exit(1 if os.path.exists(os.path.join(lab, "probe.fail")) else 0)
'''

DROP_ENV = {"CLAUDECODE", "CLAUDE_PROJECT_DIR", "CLAUDE_ENV_FILE", "CLAUDE_PLUGIN_ROOT", "HARNESS_ROOT",
            "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_CONFIG_GLOBAL"}
DROP_PREFIXES = ("CLAUDE_CODE_", "LIGHT_HARNESS_", "PYTEST_")

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

# EX-7: what an agent could do with a shell: import the harness and store a pass under the current key.
FORGE_CODE = r"""
import json, sys
scripts, task, spec = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, scripts)
import harness
harness.set_root(task)
with open(spec, encoding="utf-8") as f:
    results = json.load(f)
cfg = harness.load_config()
ok, msg, rg_state = harness.redgate_state()
appr = harness.latest("harness: plan-approved")
key = harness.reuse_key(cfg, harness.probes(), rg_state, appr[1] if appr else "")
harness.store_reuse(key, "forged output", results)
print("FORGED " + key)
"""

# A verify whose audit-trail anchor write fails (R-13): `record` raises for verify-pass subjects only.
FAILING_ANCHOR_CODE = r"""
import sys
scripts, kind = sys.argv[1], sys.argv[2]
sys.path.insert(0, scripts)
import harness
_record = harness.record
def record(msg, extra=None, slug=None):
    if str(msg).startswith("harness: verify-pass"):
        if kind == "exit":
            raise SystemExit(1)
        raise RuntimeError("simulated trail write failure")
    return _record(msg, extra, slug)
harness.record = record
sys.argv = ["light-harness"] + sys.argv[3:]
harness.main()
"""


def under(p: Path, root: Path) -> bool:
    try:
        Path(p).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def edit(path: Path, *pairs: str) -> None:
    text = path.read_text(encoding="utf-8")
    for old, new in zip(pairs[::2], pairs[1::2]):
        assert old in text, f"{old!r} not in {path}"
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def rmtree(p: Path) -> None:
    def onerror(func, path, exc):
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            func(path)
        except OSError:
            pass
    with warnings.catch_warnings():          # `onerror` is deprecated from 3.12 on, `onexc` is 3.12+ only
        warnings.simplefilter("ignore", DeprecationWarning)
        shutil.rmtree(p, onerror=onerror)


def block_of(text: str, name: str) -> str:
    """The body of an owned `<!-- harness:<name> -->` block."""
    return text.split(f"<!-- harness:{name} -->", 1)[1].split(f"<!-- /harness:{name} -->", 1)[0].strip()


def source_lines(verdict_block: str) -> list:
    """The `Suite and probes: …` lines of a §11.4 verdict block (text after the prefix, trailing space cut)."""
    return [l.rstrip()[len(SOURCE_PREFIX):] for l in verdict_block.splitlines() if l.startswith(SOURCE_PREFIX)]


class Lab:
    """One copy of the plugin and the toy repo, with its own run counters and fail switches."""

    def __init__(self, root: Path, sandbox: Path, gitconfig: Path):
        self.root = Path(root)
        self.sandbox = Path(sandbox)              # the human/forge helpers refuse anything outside it
        self.gitconfig = Path(gitconfig)          # shared by every lab: part of the run environment
        self.plugin = self.root / "plugin"
        self.scripts = self.plugin / "scripts"
        self.repo = self.root / "repo"
        self.task = self.repo / ".tasks" / SLUG
        self.contract = self.task / ".light-harness" / "contract.md"
        self.config = self.task / ".light-harness" / "config.json"
        self.cache = self.repo / ".git" / "light-harness" / f"verify-cache-{SLUG}.json"
        self.events_file = self.repo / ".git" / "light-harness" / "telemetry" / "events.jsonl"
        self.counter = self.root / "runs.count"
        self.probe_counter = self.root / "probes.count"
        self.extra_env: dict = {}
        self.root.mkdir(parents=True, exist_ok=True)
        for c in (self.counter, self.probe_counter):
            if not c.exists():
                c.write_text("", encoding="utf-8")
        if not self.gitconfig.exists():
            self.gitconfig.write_text("[user]\n\tname = Self Test\n\temail = t@t\n[core]\n\tautocrlf = false\n"
                                      "[init]\n\tdefaultBranch = main\n", encoding="utf-8")

    # --- environment and processes -------------------------------------------------------------
    def env(self, extra: dict | None = None) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k.upper() not in DROP_ENV and not k.upper().startswith(DROP_PREFIXES)}
        # no third-party pytest plugins in the toy suite: its output (in §10.4) is the same on every machine
        env.update(GIT_CONFIG_GLOBAL=str(self.gitconfig), ST_COUNT=ST_COUNT_REL, PYTHONIOENCODING="utf-8",
                   PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", COLUMNS="80")
        env.update(self.extra_env)
        env.update(extra or {})
        return env

    def run(self, cmd: list, cwd: Path, extra: dict | None = None) -> tuple[int, str]:
        r = subprocess.run(cmd, cwd=str(cwd), env=self.env(extra), capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=600)
        out = (r.stdout + r.stderr).decode("utf-8", errors="replace").replace("\r\n", "\n")
        return r.returncode, out

    def h(self, *args: str, extra: dict | None = None, task: bool = True) -> tuple[int, str]:
        """Run the copied harness.py from the main checkout (with --task unless task=False)."""
        cmd = [sys.executable, str(self.scripts / "harness.py"), *args] + (["--task", SLUG] if task else [])
        return self.run(cmd, self.repo, extra)

    def ok(self, *args: str, **kw) -> str:
        rc, out = self.h(*args, **kw)
        assert rc == 0, f"`{' '.join(args)}` failed (exit {rc}):\n{out}"
        return out

    def _confined(self) -> None:
        assert under(self.repo, self.sandbox), f"refusing to act outside {self.sandbox}: {self.repo}"
        assert under(self.scripts, self.sandbox), f"refusing to import a harness outside {self.sandbox}"
        assert not under(self.repo, TASK_DIR) and not under(self.scripts, TASK_DIR)

    def human(self, *args: str, task: bool = True) -> tuple[int, str]:
        """A human-only command, with require_human no-op'd, only inside the sandbox folder."""
        self._confined()
        cmd = [sys.executable, "-c", HUMAN_CODE, str(self.scripts), *args] + (["--task", SLUG] if task else [])
        return self.run(cmd, self.repo)

    def human_ok(self, *args: str) -> str:
        rc, out = self.human(*args)
        assert rc == 0, f"human `{' '.join(args)}` failed (exit {rc}):\n{out}"
        return out

    def git(self, *args: str, cwd: Path | None = None) -> str:
        rc, out = self.run(["git", *args], cwd or self.task)
        assert rc == 0, f"git {' '.join(args)} failed:\n{out}"
        return out

    def clone(self, dest: Path) -> "Lab":
        """A full copy of this lab (plugin, repo, counters) at `dest`, with the task worktree re-linked."""
        shutil.copytree(self.root, dest, symlinks=True)
        lab = Lab(dest, self.sandbox, self.gitconfig)
        lab.extra_env = dict(self.extra_env)
        lab.git("worktree", "repair", str(lab.task), cwd=lab.repo)
        return lab

    # --- switches and counters -------------------------------------------------------------------
    def fail_suite(self, on: bool = True) -> None:
        p = self.root / "suite.fail"
        p.write_text("on", encoding="utf-8") if on else p.unlink(missing_ok=True)

    def fail_probes(self, on: bool = True) -> None:
        p = self.root / "probe.fail"
        p.write_text("on", encoding="utf-8") if on else p.unlink(missing_ok=True)

    def runs(self) -> int:
        return len(self.counter.read_text(encoding="utf-8"))

    def probe_runs(self) -> int:
        return len(self.probe_counter.read_text(encoding="utf-8"))

    # --- observations ----------------------------------------------------------------------------
    def status_out(self, extra: dict | None = None) -> str:
        rc, out = self.h("status", extra=extra)
        assert rc == 0, f"status failed (exit {rc}):\n{out}"
        return out

    def verify_line(self, extra: dict | None = None) -> str | None:
        m = re.search(r"^Verify:[ \t]*(.*?)[ \t]*$", self.status_out(extra), re.M)
        return m.group(1) if m else None

    def stage(self) -> str:
        m = re.search(r"^Status: (\w+)", self.contract.read_text(encoding="utf-8"), re.M)
        assert m, "no Status line in the contract"
        return m.group(1)

    def text(self) -> str:
        return self.contract.read_text(encoding="utf-8")

    def block(self, name: str) -> str:
        return block_of(self.text(), name)

    def entry(self) -> dict:
        return json.loads(self.cache.read_text(encoding="utf-8"))

    def write_entry(self, e: dict) -> None:
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        self.cache.write_text(json.dumps(e), encoding="utf-8")

    def stored_key(self) -> str | None:
        try:
            e = self.entry()
        except (OSError, ValueError):
            return None
        return e.get("key") if isinstance(e, dict) and e.get("result") == "pass" else None

    def ref_sha(self) -> str:
        return self.git("rev-parse", REF, cwd=self.repo).strip()

    def trail_subjects(self) -> list:
        """The task's audit-trail subjects, oldest first."""
        out = self.git("log", "--first-parent", "--reverse", "--format=%s", REF, cwd=self.repo)
        return [s for s in out.splitlines() if s.startswith("harness:")]

    def trail_file(self, name: str, rev: str = REF) -> str | None:
        rc, out = self.run(["git", "show", f"{rev}:{name}"], self.repo)
        return out if rc == 0 else None

    def log_lines(self) -> list:
        """`light-harness log speedy`, event lines only."""
        return self.ok("log", SLUG, task=False).splitlines()[1:]

    def anchor_lines(self) -> list:
        return [l for l in self.log_lines() if re.search(r"\bverify-pass \(key [0-9a-f]{12}\)\s*$", l)]

    def events(self) -> list:
        if not self.events_file.exists():
            return []
        out = []
        for line in self.events_file.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict) and e.get("task") == SLUG:
                out.append(e)
        return out

    def suite_events(self) -> list:
        return [e for e in self.events() if e.get("event") == "suite"]

    def command_events(self, command: str) -> list:
        return [e for e in self.events() if e.get("event") == "command" and e.get("command") == command]

    def metrics(self) -> dict:
        return json.loads(self.ok("metrics", SLUG, "--json", task=False))

    # --- actions ---------------------------------------------------------------------------------
    def verify(self, *flags: str, extra: dict | None = None) -> tuple[int, str, int]:
        """(exit code, output, suite runs it caused)."""
        n = self.runs()
        rc, out = self.h("verify", *flags, extra=extra)
        return rc, out, self.runs() - n

    def verdict(self, *args: str, extra: dict | None = None) -> dict:
        """`verdict <args>`: exit code, output, suite runs, probe runs, the suite events it recorded."""
        n, p, s = self.runs(), self.probe_runs(), len(self.suite_events())
        rc, out = self.h("verdict", *args, extra=extra)
        return {"rc": rc, "out": out, "runs": self.runs() - n, "probe_runs": self.probe_runs() - p,
                "suites": self.suite_events()[s:]}

    def code(self, text: str) -> None:
        write(self.task / "src" / "t.py", text)

    def add_issues(self) -> None:
        rows = "".join(f"| RV-{i} | Low | Open | src | timing: c{i} | probe {i} | AC-1 |\n" for i in (1, 2, 3))
        edit(self.contract, SEP + "\n", SEP + "\n" + rows)

    def resolve_review(self) -> None:
        """The reviewer's §11 edits: every issue Resolved, AC-1 ticked, a §11.3 summary."""
        text = self.text()
        text = re.sub(r"^(\| RV-\d+ \| Low \| )Open( \|)", r"\1Resolved\2", text, flags=re.M)
        text = text.replace("- [ ] AC-1", "- [x] AC-1", 1)
        text = re.sub(r"(### 11\.3 Review Summary\n)<[^\n]*>", r"\1All issues resolved; no sweeps needed.", text)
        self.contract.write_text(text, encoding="utf-8")

    def probe_add(self, issue: str = "RV-1", note: str = "one") -> str:
        p = self.root / f"probe-{note}.py"
        write(p, TOY_PROBE.format(note=note))
        return self.ok("probe", "add", str(p), "--issue", issue, "--run", f'"{sys.executable}" {{file}} {{base}}')

    def forge(self, results: dict, extra: dict | None = None) -> dict:
        """EX-7: a child process imports the copied harness and stores a pass with `results` under the current
        reuse key (computed exactly as verify does); returns the entry written."""
        self._confined()
        spec = self.root / "forge-results.json"
        spec.write_text(json.dumps(results), encoding="utf-8")
        rc, out = self.run([sys.executable, "-c", FORGE_CODE, str(self.scripts), str(self.task), str(spec)],
                           self.task, extra)
        assert rc == 0 and "FORGED " in out, f"forge failed (exit {rc}):\n{out}"
        spec.unlink()
        e = self.entry()
        assert e["key"] == out.split("FORGED ", 1)[1].split()[0] and e["results"] == results, e
        return e

    def verify_failing_anchor(self, kind: str) -> tuple[int, str, int]:
        """`verify` in a child whose audit-trail write for the anchor fails (kind "raise" or "exit")."""
        self._confined()
        n = self.runs()
        rc, out = self.run([sys.executable, "-c", FAILING_ANCHOR_CODE, str(self.scripts), kind,
                            "verify", "--task", SLUG], self.repo)
        return rc, out, self.runs() - n


def build_template(root: Path, sandbox: Path, gitconfig: Path, harness_file: Path | None = None) -> Lab:
    """The toy task in Build: plan approved, red gate passed on a stub, the implementation committed and three
    Low issues logged. `harness_file` replaces the copied harness.py (the base-commit copy for the BR-7
    oracle)."""
    lab = Lab(root, sandbox, gitconfig)
    shutil.copytree(PLUGIN_SRC, lab.plugin, ignore=shutil.ignore_patterns("__pycache__"))
    if harness_file is not None:
        shutil.copyfile(harness_file, lab.scripts / "harness.py")
    lab.repo.mkdir()
    lab.git("init", "-q", cwd=lab.repo)
    write(lab.repo / "src" / "__init__.py", "")
    write(lab.repo / "src" / "m.py", "def double(x):\n    return x + x\n")
    write(lab.repo / "src" / "util.py", "def helper():\n    return 1\n")
    write(lab.repo / ".gitignore", ".env\n")
    lab.git("add", "-A", cwd=lab.repo)
    lab.git("commit", "-qm", "existing", cwd=lab.repo)
    lab.ok("start-task", SLUG, task=False)
    edit(lab.contract, "<one or two sentences: what outcome, for whom>", "Triple numbers",
         "| EX-1 | <input> | <expected> | <happy path / edge / error> |", "| EX-1 | 2 | 6 | happy |",
         "- AC-1: <Given … When … Then …>", "- AC-1: Given 2, then 6")
    lab.ok("set-status", "Initialize")
    edit(lab.contract, "| R-1 | <risk> | <L/M/H> | <L/M/H> | <mitigation> |", "| R-1 | none | L | L | none |",
         "| <area> | <change> | <who/what is affected> | <how to undo> |", "| src/t.py | new | none | delete |",
         "- <src/path/or/glob>", "- src/**\n- tests/**", "1. <step> (AC-1)", "1. triple (AC-1)")
    lab.ok("set-status", "Plan")
    lab.human_ok("approve", "--name", "Human Approver")
    lab.ok("set-status", "Build")
    write(lab.task / "tests" / "test_t.py", TOY_TEST)
    lab.code(STUB)
    lab.git("add", "-A")
    lab.git("commit", "-qm", "tests and stub")
    lab.ok("redgate")
    lab.code(IMPL)
    lab.git("add", "-A")
    lab.git("commit", "-qm", "triple")
    lab.add_issues()
    lab.counter.write_text("", encoding="utf-8")
    lab.probe_counter.write_text("", encoding="utf-8")
    return lab


def new_red_gate(lab: Lab) -> None:
    """Gate-record change: a human unlocks the tests and a new red gate is passed on the same tests (Build)."""
    lab.human_ok("unlock-tests", "--name", "Human Approver", "--reason", "revise tests")
    keep = (lab.task / "src" / "t.py").read_bytes()
    lab.code(STUB)
    lab.ok("redgate")
    (lab.task / "src" / "t.py").write_bytes(keep)


def new_approval(lab: Lab) -> None:
    """Gate-record change: Build -> Plan -> approve -> Build -> red gate (same plan, new approval record)."""
    lab.ok("set-status", "Plan")
    lab.human_ok("approve", "--name", "Human Approver")
    lab.ok("set-status", "Build")
    keep = (lab.task / "src" / "t.py").read_bytes()
    lab.code(STUB)
    lab.ok("redgate")
    (lab.task / "src" / "t.py").write_bytes(keep)
