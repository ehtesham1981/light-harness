# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""A toy-task lab for issue #50 (`amend-config`, the `{concurrency}` warning), used by
tests/test_amend_config.py and tests/test_concurrency_warning.py. A plain module, not conftest.py.

Built on tests/lh_toy.py (the toy repo, its deterministic runner `run_toy.py`, the machine isolation), with
one difference: the task's approved `test_cmd` is the node test runner, `["node", "--test"]`. No real `node`
is ever started. Every harness command runs through DRIVER, a child process that imports the copied
harness.py and replaces `run_process` with a fake `node`: a suite command whose first item is `node`
must be `node --test [--test-concurrency=<positive int>] --junitxml=<path>` (anything else exits 9), and
runs the toy runner instead. Each spawn's argv (after `{concurrency}` substitution) is appended to the file
named by `LHT_ARGV`, so a test can count suite runs and see the argv the harness built.

`human=True` additionally no-ops `require_human` (only inside pytest's temp folder); prompts are answered
from stdin (`input()` reads the piped text). `swap=<path>`: `input()` overwrites the task's config.json
with that file's bytes before returning the typed answer (a config changed while the prompt waited).

Snapshots (built once per pytest session, memoised in the base temp folder): `plan` (approved, status Plan),
`build` (Build, baseline recorded, task test + stub committed, before the red gate) and `green` (red gate
passed, implementation committed, issues RV-1..3 open).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import lh_toy as T  # noqa: E402

SLUG = T.SLUG
REF = f"refs/light-harness/tasks/{SLUG}"
NODE_CMD = ["node", "--test"]
NODE_CONC = ["node", "--test", "--test-concurrency={concurrency}"]

DRIVER = r'''
import builtins, json, os, re, sys
scripts = sys.argv[1]
sys.path.insert(0, scripts)
import harness
if os.environ.get("LHT_HUMAN") == "1":
    harness.require_human = lambda action: None
_swap = os.environ.get("LHT_SWAP")
if _swap:
    _input = builtins.input
    def _swapping_input(prompt=""):
        answer = _input(prompt)
        with open(_swap, "rb") as f:
            data = f.read()
        with open(str(harness.CONFIG), "wb") as f:
            f.write(data)
        return answer
    builtins.input = _swapping_input
_real_run_process = harness.run_process
def _run_process(cmd, env, timeout, *a, **kw):
    cmd = list(cmd)
    log = os.environ.get("LHT_ARGV")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(cmd) + "\n")
    if cmd and os.path.basename(str(cmd[0])).lower() in ("node", "node.exe"):
        junit = [x for x in cmd[1:] if x.startswith("--junitxml=")]
        other = [x for x in cmd[1:] if x != "--test" and x not in junit
                 and not re.fullmatch(r"--test-concurrency=[1-9][0-9]*", x)]
        if "--test" not in cmd[1:] or other:
            return 9, "fake node: unexpected arguments %r\n" % (other,)
        cmd = [sys.executable, "run_toy.py"] + junit
    return _real_run_process(cmd, env, timeout, *a, **kw)
harness.run_process = _run_process
sys.argv = ["light-harness"] + sys.argv[2:]
harness.main()
'''


class Lab(T.Toy):
    """One plugin copy and one toy repo (lh_toy.Toy), every harness command run through DRIVER."""

    def __init__(self, root: Path, sandbox: Path, machine: Path | None = None):
        root = Path(root)
        super().__init__(root, sandbox, machine or root / "machine")
        self.argv_log = self.root / "argv.log"

    def drive(self, *args: str, human: bool = False, stdin: str = "", extra: dict | None = None,
              swap: Path | None = None, task: bool = True, timeout: float = 300) -> tuple:
        """(exit code, stdout + stderr) of a harness command run through DRIVER."""
        if human:
            assert T.under(self.repo, self.sandbox), f"refusing a human-only command outside {self.sandbox}"
            assert T.under(self.scripts, self.sandbox) and not T.under(self.repo, T.TASK_DIR)
        env = self.env(extra)
        env["LHT_ARGV"] = str(self.argv_log)
        if human:
            env["LHT_HUMAN"] = "1"
        if swap is not None:
            env["LHT_SWAP"] = str(swap)
        cmd = [sys.executable, "-c", DRIVER, str(self.scripts), *args] + (["--task", SLUG] if task else [])
        r = subprocess.run(cmd, cwd=str(self.repo), env=env, capture_output=True,
                           input=stdin.encode("utf-8"), timeout=timeout)
        out = (r.stdout + r.stderr).decode("utf-8", errors="replace").replace("\r\n", "\n")
        return r.returncode, out

    def must(self, *args: str, **kw) -> str:
        rc, out = self.drive(*args, **kw)
        assert rc == 0, f"`{' '.join(args)}` failed (exit {rc}):\n{out}"
        return out

    def amend(self, name: str = "Pat Human", reason: str = "suite is flaky at full parallelism",
              answer: str = "AMEND", **kw) -> tuple:
        return self.drive("amend-config", "--name", name, "--reason", reason, human=True,
                          stdin=answer + "\n", **kw)

    def real(self, *args: str, stdin: str = "", extra: dict | None = None) -> tuple:
        """The copied harness.py itself (no patches), stdin a pipe (never a terminal)."""
        r = subprocess.run(self._cmd(args), cwd=str(self.repo), env=self.env(extra), capture_output=True,
                           input=stdin.encode("utf-8"), timeout=300)
        return r.returncode, (r.stdout + r.stderr).decode("utf-8", errors="replace").replace("\r\n", "\n")

    # --- observations --------------------------------------------------------------------------
    def cfg(self) -> dict:
        return json.loads(self.config.read_text(encoding="utf-8"))

    def set_cfg(self, cfg) -> None:
        T.write(self.config, json.dumps(cfg, indent=2) + "\n")

    def change_cfg(self, **changes) -> dict:
        """Live config with keys set (value None: key removed). Returns the new config."""
        cfg = self.cfg()
        for k, v in changes.items():
            if v is None:
                cfg.pop(k, None)
            else:
                cfg[k] = v
        self.set_cfg(cfg)
        return cfg

    def ref_sha(self) -> str:
        return self.git("rev-parse", "--verify", "-q", REF, cwd=self.repo).strip()

    def subjects(self) -> list:
        out = self.git("log", "--first-parent", "--format=%s", REF, cwd=self.repo)
        return [l for l in out.splitlines() if l.startswith("harness:")]

    def trail_rows(self) -> list:
        out = self.git("log", "--first-parent", "--format=%H%x09%s", REF, cwd=self.repo)
        return [tuple(l.split("\t", 1)) for l in out.splitlines() if "\tharness:" in l]

    def trail_file(self, sha: str, name: str):
        rc, out = self.run(["git", "show", f"{sha}:{name}"], cwd=self.repo)
        return out if rc == 0 else None

    def drop_from_newest_event(self, name: str) -> str:
        """Test set-up only: re-write the newest audit-trail event without the file `name` in its tree (same
        parents and message) and move the task's ref to it. Returns the new sha."""
        sha = self.ref_sha()
        entries = [e for e in self.git("ls-tree", sha, cwd=self.repo).splitlines()
                   if e and e.split("\t", 1)[1] != name]
        tree = subprocess.run(["git", "mktree"], cwd=str(self.repo), env=self.env(), capture_output=True,
                              input=("\n".join(entries) + "\n").encode("utf-8"), check=True).stdout.decode().strip()
        parents = self.git("log", "-1", "--format=%P", sha, cwd=self.repo).split()
        msg = self.git("log", "-1", "--format=%B", sha, cwd=self.repo).strip()
        args = ["commit-tree", tree, "-m", msg]
        for p in parents:
            args += ["-p", p]
        new = self.git(*args, cwd=self.repo).strip()
        self.git("update-ref", REF, new, sha, cwd=self.repo)
        return new

    def suite_argvs(self) -> list:
        if not self.argv_log.exists():
            return []
        return [json.loads(l) for l in self.argv_log.read_text(encoding="utf-8").splitlines() if l.strip()]

    def suite_runs(self) -> int:
        return len(self.suite_argvs())

    def text(self) -> str:
        return self.contract.read_text(encoding="utf-8")

    def command_events(self, command: str) -> list:
        if not self.events.exists():
            return []
        out = []
        for line in self.events.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict) and e.get("event") == "command" and e.get("command") == command:
                out.append((e, line))
        return out


def _build(root: Path, sandbox: Path) -> dict:
    lab = Lab(root / "live", sandbox, root / "machine")
    lab.root.mkdir(parents=True)
    T.make_plugin(lab.plugin, "new")
    lab.repo.mkdir()
    lab.git("init", "-q", cwd=lab.repo)
    T.write(lab.repo / "src" / "__init__.py", "")
    T.write(lab.repo / "src" / "m.py", "def double(x):\n    return x + x\n")
    T.write(lab.repo / "run_toy.py", T.RUNNER)
    T.write(lab.repo / "tests" / "test_old.py", T.OLD_TEST)
    T.write(lab.repo / ".gitignore", ".env\n")
    lab.git("add", "-A", cwd=lab.repo)
    lab.git("commit", "-qm", "existing", cwd=lab.repo)
    lab.must("start-task", SLUG, task=False)
    T.edit(lab.contract, "<one or two sentences: what outcome, for whom>", "Triple numbers",
           "| EX-1 | <input> | <expected> | <happy path / edge / error> |", "| EX-1 | 2 | 6 | happy |",
           "- AC-1: <Given … When … Then …>", "- AC-1: Given 2, then 6")
    lab.change_cfg(test_cmd=list(NODE_CMD))
    lab.must("set-status", "Initialize")
    T.edit(lab.contract, "| R-1 | <risk> | <L/M/H> | <L/M/H> | <mitigation> |", "| R-1 | none | L | L | none |",
           "| <area> | <change> | <who/what is affected> | <how to undo> |", "| src/t.py | new | none | delete |",
           "- <src/path/or/glob>", "- src/**\n- tests/**", "1. <step> (AC-1)", "1. triple (AC-1)")
    lab.must("set-status", "Plan")
    lab.must("approve", "--name", "Human Approver", human=True, stdin="APPROVE\n")
    snaps = {}

    def snap(name):
        dest = root / f"snap-{name}"
        shutil.copytree(lab.root, dest, symlinks=True, ignore=shutil.ignore_patterns("machine", "argv.log"))
        snaps[name] = str(dest)

    snap("plan")
    lab.must("set-status", "Build")
    T.write(lab.task / "tests" / "test_t.py", T.TASK_TEST)
    lab.code(T.STUB)
    lab.git("add", "-A")
    lab.git("commit", "-qm", "tests and stub")
    snap("build")
    lab.must("redgate")
    lab.code(T.IMPL)
    lab.git("add", "-A")
    lab.git("commit", "-qm", "triple")
    rows = "".join(f"| RV-{i} | Low | Open | src | timing: c{i} | probe {i} | AC-1 |\n" for i in (1, 2, 3))
    T.edit(lab.contract, T.SEP + "\n", T.SEP + "\n" + rows)
    snap("green")
    return snaps


def chain(base_temp: Path) -> dict:
    """The snapshots (see the module docstring), built once per pytest session."""
    base_temp = Path(base_temp)
    root = base_temp / "lhamend-node"
    done = root / "done.json"
    if done.exists():
        return json.loads(done.read_text(encoding="utf-8"))
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    snaps = _build(root, base_temp)
    done.write_text(json.dumps(snaps), encoding="utf-8")
    return snaps


def clone(snapshot, dest: Path, sandbox: Path) -> Lab:
    """A copy of a snapshot in `dest`, its task worktree re-linked."""
    snapshot, dest = Path(snapshot), Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    lab = Lab(dest, sandbox)
    shutil.copytree(snapshot / "plugin", lab.plugin, symlinks=True)
    shutil.copytree(snapshot / "repo", lab.repo, symlinks=True)
    # Re-link the task worktree to THIS copy: the copied `.git` file still names the snapshot's admin folder
    # (which exists, so `git worktree repair` alone leaves it alone), and that folder's `gitdir` names the
    # snapshot's worktree. Point both at the copy, then let git check the links.
    admin = lab.repo / ".git" / "worktrees" / SLUG
    assert admin.is_dir(), f"no worktree admin folder {admin}"
    (lab.task / ".git").write_text(f"gitdir: {admin.as_posix()}\n", encoding="utf-8")
    (admin / "gitdir").write_text(f"{(lab.task / '.git').as_posix()}\n", encoding="utf-8")
    lab.git("worktree", "repair", str(lab.task), cwd=lab.repo)
    listed = lab.git("worktree", "list", "--porcelain", cwd=lab.repo)
    assert Path(os.path.realpath(lab.task)).as_posix() in listed.replace("\\", "/") or \
        lab.task.as_posix() in listed.replace("\\", "/"), f"task worktree not re-linked:\n{listed}"
    return lab
