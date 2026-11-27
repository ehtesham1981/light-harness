# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""Acceptance tests for issue #14: a passing `verify` is reused for as long as its key is unchanged.

Every end-to-end test drives a COPY of plugins/light-harness (in pytest's tmp_path) by subprocess
against a toy git repo whose one-test pytest suite counts its own runs in a file. A "run" means that
counter went up by one. "Status says X" means the `Verify:` line of `light-harness status`.

The toy task is built once per module (Plan -> approve -> Build -> red gate -> implementation) and
copied into each test's tmp_path; `git worktree repair` re-links the copied task worktree.

Human-only commands (approve, unlock-tests, probe drop) go through `human()`, which imports the copied
harness.py in a child process with `require_human` replaced by a no-op. It refuses any repo or plugin
copy outside pytest's temp folder.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lh_machine import machine_env, relink_worktrees  # noqa: E402

TASK_DIR = Path(__file__).resolve().parents[1]
PLUGIN_SRC = TASK_DIR / "plugins" / "light-harness"
HARNESS_SRC = PLUGIN_SRC / "scripts" / "harness.py"
SLUG = "speedy"
PART_NAMES = ["tree", "tests", "probes", "config", "version", "approval", "base", "python", "env"]
HEX64 = re.compile(r"[0-9a-f]{64}")
HEX12 = r"[0-9a-f]{12}"
CACHED_RE = re.compile(rf"^cached, key ({HEX12}) \(passed (.+)\)$")
STALE_RE = re.compile(rf"^stale \(changed: ([a-z]+(?:, [a-z]+)*)\), stored key ({HEX12})$")
SEP = "|----|----------|--------|------|-------|-------------|------------------|"

IMPL = "def triple(x):\n    return 3 * x\n"
IMPL2 = "def triple(x):\n    return x * 3\n"            # same behaviour, different bytes
STUB = "def triple(x):\n    raise NotImplementedError\n"
WRONG = "def triple(x):\n    return 0\n"
TOY_TEST = '''import os
from src.t import triple


def test_ac1():
    """AC-1"""
    with open(os.environ["ST_COUNT"], "a") as f:
        f.write("x")
    assert triple(2) == 6
'''

# Variables that would make a child run see the agent session, the harness or pytest's own volatile
# state (pytest's own PYTEST_* variables change between test phases and would leak into the run
# environment, which is part of the reuse key).
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


def _under(p: Path, root: Path) -> bool:
    try:
        Path(p).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def _edit(path: Path, *pairs: str) -> None:
    text = path.read_text(encoding="utf-8")
    for old, new in zip(pairs[::2], pairs[1::2]):
        assert old in text, f"{old!r} not in {path}"
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


class Lab:
    """One copy of the plugin and the toy repo, with its own run counter."""

    def __init__(self, root: Path, sandbox: Path):
        self.root = root
        self.sandbox = sandbox                      # pytest's temp folder: the human helper refuses anything outside
        self.plugin = root / "plugin"
        self.scripts = self.plugin / "scripts"
        self.repo = root / "repo"
        self.task = self.repo / ".tasks" / SLUG
        self.contract = self.task / ".light-harness" / "contract.md"
        self.config = self.task / ".light-harness" / "config.json"
        self.cache = self.repo / ".git" / "light-harness" / f"verify-cache-{SLUG}.json"
        self.counter = root / "runs.count"
        self.gitconfig = root / "gitconfig"
        self.extra_env: dict = {}
        if not self.counter.exists():
            self.counter.write_text("", encoding="utf-8")
        self.gitconfig.write_text("[user]\n\tname = Self Test\n\temail = t@t\n[core]\n\tautocrlf = false\n"
                                  "[init]\n\tdefaultBranch = main\n", encoding="utf-8")

    # --- environment and processes -------------------------------------------------------------
    def env(self, extra: dict | None = None) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k.upper() not in DROP_ENV and not k.upper().startswith(DROP_PREFIXES)}
        # machine-wide state (run slots, slot count, per-run temp root) stays inside pytest's temp folder
        env.update(machine_env(Path(self.sandbox) / "lh-machine", gitconfig=self.gitconfig))
        env.update(GIT_CONFIG_GLOBAL=str(self.gitconfig), ST_COUNT=str(self.counter),
                   PYTHONIOENCODING="utf-8")
        env.update(self.extra_env)
        env.update(extra or {})
        return env

    def _run(self, cmd: list, cwd: Path, extra: dict | None = None) -> tuple[int, str]:
        r = subprocess.run(cmd, cwd=str(cwd), env=self.env(extra), capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=600)
        out = (r.stdout + r.stderr).decode("utf-8", errors="replace").replace("\r\n", "\n")
        return r.returncode, out

    def h(self, *args: str, extra: dict | None = None, task: bool = True) -> tuple[int, str]:
        """Run the copied harness.py from the main checkout (with --task unless task=False)."""
        cmd = [sys.executable, str(self.scripts / "harness.py"), *args] + (["--task", SLUG] if task else [])
        return self._run(cmd, self.repo, extra)

    def ok(self, *args: str, **kw) -> str:
        rc, out = self.h(*args, **kw)
        assert rc == 0, f"`{' '.join(args)}` failed (exit {rc}):\n{out}"
        return out

    def human(self, *args: str, task: bool = True) -> tuple[int, str]:
        """A human-only command, with require_human no-op'd, only inside pytest's temp folder."""
        assert _under(self.repo, self.sandbox), f"refusing a human-only command outside {self.sandbox}: {self.repo}"
        assert _under(self.scripts, self.sandbox), f"refusing to import a harness outside {self.sandbox}"
        assert not _under(self.repo, TASK_DIR) and not _under(self.scripts, TASK_DIR)
        cmd = [sys.executable, "-c", HUMAN_CODE, str(self.scripts), *args] + (["--task", SLUG] if task else [])
        return self._run(cmd, self.repo)

    def git(self, *args: str, cwd: Path | None = None) -> str:
        rc, out = self._run(["git", *args], cwd or self.task)
        assert rc == 0, f"git {' '.join(args)} failed:\n{out}"
        return out

    # --- observations ----------------------------------------------------------------------------
    def runs(self) -> int:
        return len(self.counter.read_text(encoding="utf-8"))

    def verify_line(self, extra: dict | None = None) -> str | None:
        """The text after `Verify:` in `status`, or None if status has no such line."""
        rc, out = self.h("status", extra=extra)
        assert rc == 0, f"status failed (exit {rc}):\n{out}"
        m = re.search(r"^Verify:[ \t]*(.*?)[ \t]*$", out, re.M)
        return m.group(1) if m else None

    def entry(self) -> dict:
        return json.loads(self.cache.read_text(encoding="utf-8"))

    def stored_key(self) -> str | None:
        try:
            e = self.entry()
        except (OSError, ValueError):
            return None
        return e.get("key") if isinstance(e, dict) and e.get("result") == "pass" else None

    def verify(self, *flags: str, extra: dict | None = None) -> tuple[int, str, int]:
        """(exit code, output, suite runs it caused)."""
        n = self.runs()
        rc, out = self.h("verify", *flags, extra=extra)
        return rc, out, self.runs() - n

    def first_pass(self) -> str:
        """A verify that runs the suite and stores a pass; returns the stored key."""
        rc, out, ran = self.verify()
        assert ran == 1, f"the first verify ran the suite {ran} times:\n{out}"
        assert "cached" not in out, out
        k = self.stored_key()
        assert k is not None and HEX64.fullmatch(k), f"no pass stored after a passing verify: {k!r}"
        return k

    # --- toy code --------------------------------------------------------------------------------
    def code(self, text: str) -> None:
        _write(self.task / "src" / "t.py", text)

    def add_issues(self) -> None:
        rows = "".join(f"| RV-{i} | Low | Open | src | timing: c{i} | probe {i} | AC-1 |\n" for i in (1, 2, 3))
        _edit(self.contract, SEP + "\n", SEP + "\n" + rows)

    def probe_script(self, name: str, exit_code: int, note: str = "") -> Path:
        p = self.root / name
        _write(p, f"# probe {note}\nimport sys\nsys.exit({exit_code})\n")
        return p

    def probe_add(self, script: Path, issue: str) -> tuple[int, str]:
        return self.h("probe", "add", str(script), "--issue", issue, "--run", f'"{sys.executable}" {{file}} {{base}}')


def _build_template(root: Path, sandbox: Path) -> None:
    lab = Lab(root, sandbox)
    shutil.copytree(PLUGIN_SRC, lab.plugin, ignore=shutil.ignore_patterns("__pycache__"))
    lab.repo.mkdir()
    lab.git("init", "-q", cwd=lab.repo)
    _write(lab.repo / "src" / "__init__.py", "")
    _write(lab.repo / "src" / "m.py", "def double(x):\n    return x + x\n")
    _write(lab.repo / "src" / "util.py", "def helper():\n    return 1\n")
    _write(lab.repo / ".gitignore", ".env\n")
    lab.git("add", "-A", cwd=lab.repo)
    lab.git("commit", "-qm", "existing", cwd=lab.repo)
    lab.ok("start-task", SLUG, task=False)
    _edit(lab.contract, "<one or two sentences: what outcome, for whom>", "Triple numbers",
          "| EX-1 | <input> | <expected> | <happy path / edge / error> |", "| EX-1 | 2 | 6 | happy |",
          "- AC-1: <Given … When … Then …>", "- AC-1: Given 2, then 6")
    lab.ok("set-status", "Initialize")
    _edit(lab.contract, "| R-1 | <risk> | <L/M/H> | <L/M/H> | <mitigation> |", "| R-1 | none | L | L | none |",
          "| <area> | <change> | <who/what is affected> | <how to undo> |", "| src/t.py | new | none | delete |",
          "- <src/path/or/glob>", "- src/**\n- tests/**", "1. <step> (AC-1)", "1. triple (AC-1)")
    lab.ok("set-status", "Plan")
    rc, out = lab.human("approve", "--name", "Human Approver")
    assert rc == 0, out
    lab.ok("set-status", "Build")
    _write(lab.task / "tests" / "test_t.py", TOY_TEST)
    lab.code(STUB)
    lab.git("add", "-A")
    lab.git("commit", "-qm", "tests and stub")
    lab.ok("redgate")
    lab.code(IMPL)
    lab.git("add", "-A")
    lab.git("commit", "-qm", "triple")
    lab.add_issues()
    lab.counter.write_text("", encoding="utf-8")


@pytest.fixture(scope="module")
def template(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("lh14tpl")
    _build_template(root, tmp_path_factory.getbasetemp())
    return root


@pytest.fixture
def lab(template, tmp_path, tmp_path_factory) -> Lab:
    root = tmp_path / "lab"
    shutil.copytree(template, root, symlinks=True)
    lab = Lab(root, tmp_path_factory.getbasetemp())
    lab.counter.write_text("", encoding="utf-8")
    relink_worktrees(lab.repo, [lab.task], lab.env())
    assert lab.cache.exists() is False
    return lab


def _assert_stale(line: str | None, part: str, stored: str, exact: bool = True) -> list:
    m = STALE_RE.match(line or "")
    assert m, f"status doesn't say stale: Verify: {line!r}"
    changed = m.group(1).split(", ")
    if exact:
        assert changed == [part], f"changed parts {changed}, expected exactly [{part!r}]"
    else:
        assert part in changed, f"changed parts {changed} lack {part!r}"
    assert m.group(2) == stored[:12], f"stored key {m.group(2)} != {stored[:12]}"
    return changed


def _assert_miss_then_run(lab: Lab, part: str, stored: str, extra: dict | None = None) -> str:
    _assert_stale(lab.verify_line(extra), part, stored)
    rc, out, ran = lab.verify(extra=extra)
    assert ran == 1, f"after a {part} change the suite ran {ran} times:\n{out}"
    assert "cached" not in out, out
    k = lab.stored_key()
    assert k is not None and k != stored, f"no new pass stored after the {part} miss (key {k!r})"
    return k


# --- AC-1 -------------------------------------------------------------------------------------------
def test_ac1_set_status_review_reuses_cached_pass(lab):
    """AC-1 EX-1 BR-2 BR-6: verify passes; nothing changes; `set-status Review` doesn't run the suite and
    says "cached, key <12 hex>", the key `status` showed; §10.4 says so too. `verdict pass` reuses the
    anchored pass too (#39 AC-7: updated per its Q-2)."""
    rc, out, ran = lab.verify()
    assert rc == 0 and ran == 1, out
    assert "cached" not in out
    m = CACHED_RE.match(lab.verify_line() or "")
    assert m, f"status doesn't show the cached pass: {lab.verify_line()!r}"
    k = m.group(1)
    assert k == lab.entry()["key"][:12]
    n = lab.runs()
    out = lab.ok("set-status", "Review")
    assert lab.runs() == n, f"set-status Review re-ran the suite:\n{out}"
    assert f"cached, key {k}" in out, out
    assert re.search(rf"Green gate: test suite passed \(1 tests; cached, key {k}, passed .+; `verify --fresh` re-runs\)",
                     out), out
    block = lab.contract.read_text(encoding="utf-8").split("<!-- harness:verify -->", 1)[1].split("<!-- /harness:verify -->", 1)[0]
    assert f"cached, key {k}" in block, block
    # #39: the final gate reuses a pass stored and anchored by a real run on an unchanged key
    _edit(lab.contract, "- [ ] AC-1", "- [x] AC-1", "| RV-1 | Low | Open", "| RV-1 | Low | Resolved",
          "| RV-2 | Low | Open", "| RV-2 | Low | Resolved", "| RV-3 | Low | Open", "| RV-3 | Low | Resolved")
    n = lab.runs()
    out = lab.ok("verdict", "pass")
    assert lab.runs() == n, f"verdict pass re-ran the suite on an unchanged, anchored key:\n{out}"


# --- AC-2 -------------------------------------------------------------------------------------------
def test_ac2_miss_tree(lab):
    """AC-2 EX-3 BR-1 BR-3: a behaviour-preserving edit of a tracked source file -> `stale (changed: tree)`,
    and the next verify runs the suite."""
    k = lab.first_pass()
    lab.code(IMPL2)
    _assert_miss_then_run(lab, "tree", k)


def test_ac2_miss_tests(lab):
    """AC-2 EX-4 BR-3: unlock-tests -> verify fails at the red gate without running or reusing anything,
    status `no cached pass`; a new red gate on the same code -> `stale (changed: tests)` -> the suite runs."""
    k = lab.first_pass()
    rc, out = lab.human("unlock-tests", "--name", "Human Approver", "--reason", "revise tests")
    assert rc == 0, out
    rc, out, ran = lab.verify()
    assert rc != 0 and ran == 0, out
    assert "Red gate" in out and "cached" not in out, out
    assert lab.verify_line() == "no cached pass"
    lab.code(STUB)
    lab.ok("redgate")
    lab.code(IMPL)
    _assert_miss_then_run(lab, "tests", k)


def test_ac2_miss_probes(lab):
    """AC-2 EX-4 BR-3: saving a passing probe -> `stale (changed: probes)` -> the suite runs."""
    k = lab.first_pass()
    rc, out = lab.probe_add(lab.probe_script("good.py", 0), "RV-1")
    assert rc == 0, out
    _assert_miss_then_run(lab, "probes", k)


def test_ac2_miss_config(lab):
    """AC-2 EX-5 BR-3: a config.json value changes -> `stale (changed: config)` -> the suite runs
    (verify may fail anti-gaming; only the run and the wording are asserted)."""
    k = lab.first_pass()
    cfg = json.loads(lab.config.read_text(encoding="utf-8"))
    assert cfg["parallel"] == 3
    cfg["parallel"] = 2
    lab.config.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    _assert_miss_then_run(lab, "config", k)


def test_ac2_miss_version(lab):
    """AC-2 EX-6 BR-3: harness code changes (a comment appended to the copied harness.py) and then the
    version string changes -> each time `stale (changed: version)` -> the suite runs."""
    k = lab.first_pass()
    h = lab.scripts / "harness.py"
    h.write_bytes(h.read_bytes() + b"\n# a local edit\n")
    k = _assert_miss_then_run(lab, "version", k)
    text = h.read_text(encoding="utf-8")
    new, n = re.subn(r'^__version__ = "[^"]*"', '__version__ = "9.9.9-test"', text, count=1, flags=re.M)
    assert n == 1
    h.write_text(new, encoding="utf-8")
    assert lab.ok("version", task=False).strip() == "9.9.9-test"
    _assert_miss_then_run(lab, "version", k)


# --- AC-3 -------------------------------------------------------------------------------------------
def test_ac3_fresh_runs_suite(lab):
    """AC-3 EX-7 BR-4: with the key unchanged, `verify --fresh` runs the suite and doesn't say cached;
    its pass is stored, so the next plain verify is cached."""
    k = lab.first_pass()
    assert lab.verify_line() == f"cached, key {k[:12]} (passed {lab.entry()['label']})"
    rc, out, ran = lab.verify("--fresh")
    assert rc == 0 and ran == 1, out
    assert "cached" not in out, out
    rc, out, ran = lab.verify("--fresh")
    assert ran == 1 and "cached" not in out, out
    e = lab.entry()
    assert e["key"] == k and e["result"] == "pass"
    assert lab.verify_line() == f"cached, key {k[:12]} (passed {e['label']})"
    rc, out, ran = lab.verify()
    assert ran == 0 and f"cached, key {k[:12]}" in out, out


# --- AC-4 -------------------------------------------------------------------------------------------
def test_ac4_old_pass_reused(lab):
    """AC-4 EX-2 BR-2: a pass recorded 30 min + 1 s, 30 days and 2 hours ago with an unchanged
    key is reused (status cached, verify doesn't run the suite and says cached); last, with the 2-hour-old
    pass, `set-status Review` doesn't run the suite either."""
    k = lab.first_pass()
    label = lab.entry()["label"]
    for age_s in (30 * 60 + 1, 30 * 86400, 2 * 3600):
        e = lab.entry()
        e["when"] = time.time() - age_s
        lab.cache.write_text(json.dumps(e), encoding="utf-8")
        assert lab.verify_line() == f"cached, key {k[:12]} (passed {label})", f"age {age_s}s"
        rc, out, ran = lab.verify()
        assert ran == 0, f"a pass {age_s}s old wasn't reused:\n{out}"
        assert f"cached, key {k[:12]}" in out, out
    n = lab.runs()
    out = lab.ok("set-status", "Review")
    assert lab.runs() == n, f"set-status Review re-ran the suite for a pass 2 hours old:\n{out}"
    assert f"cached, key {k[:12]}" in out, out


# --- AC-5 -------------------------------------------------------------------------------------------
def test_ac5_status_shows_cache_state(lab):
    """AC-5 BR-1 BR-6: status says `no cached pass` before any verify, `cached, key <12> (passed <label>)`
    after a pass (the key in the stored entry and in the cached verify output), `stale (changed: tree)`
    after an edit, `stale (changed: tree, config)` after two changes, `no cached pass` for a corrupt cache
    file. The stored entry holds exactly the 9 part hashes and result pass, and no env value (R-6)."""
    sentinel = "lh14-sentinel-value-7f3a9c"
    lab.extra_env = {"LH14_SECRET_TOKEN": sentinel}
    assert lab.verify_line() == "no cached pass"
    k = lab.first_pass()
    raw = lab.cache.read_text(encoding="utf-8")
    assert sentinel not in raw, "an environment value was written to the cache file (R-6)"
    e = json.loads(raw)
    assert HEX64.fullmatch(e["key"]) and e["key"] == k
    assert e["result"] == "pass"
    assert sorted(e["parts"]) == sorted(PART_NAMES), e.get("parts")
    assert all(isinstance(v, str) and HEX64.fullmatch(v) for v in e["parts"].values()), e["parts"]
    assert isinstance(e["when"], float) and abs(time.time() - e["when"]) < 600
    assert e["results"] == {"tests.test_t::test_ac1": "passed"}
    assert lab.verify_line() == f"cached, key {k[:12]} (passed {e['label']})"
    rc, out, ran = lab.verify()
    assert ran == 0 and f"cached, key {k[:12]}" in out, out
    lab.code(IMPL2)
    assert lab.verify_line() == f"stale (changed: tree), stored key {k[:12]}"
    # two parts changed at once: both named, in reuse_parts order (tree before config)
    cfg = json.loads(lab.config.read_text(encoding="utf-8"))
    cfg["probe_timeout"] = 601
    lab.config.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    assert lab.verify_line() == f"stale (changed: tree, config), stored key {k[:12]}"
    # a corrupt cache file never fails status
    lab.cache.write_text("not json {", encoding="utf-8")
    assert lab.verify_line() == "no cached pass"
    lab.cache.write_text("[]", encoding="utf-8")
    assert lab.verify_line() == "no cached pass"


# --- AC-6 -------------------------------------------------------------------------------------------
def _functions(tree: ast.Module) -> dict:
    return {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _is_key_lookup(node: ast.AST) -> bool:
    """`x["key"]` or `x.get("key")`."""
    if isinstance(node, ast.Subscript):
        s = node.slice
        s = getattr(s, "value", s) if type(s).__name__ == "Index" else s   # Python 3.8 compat shape
        return isinstance(s, ast.Constant) and s.value == "key"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
        return bool(node.args) and isinstance(node.args[0], ast.Constant) and node.args[0].value == "key"
    return False


def _calls(fn: ast.AST, name: str) -> int:
    return sum(1 for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name)


def _names(fn: ast.AST) -> set:
    return {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)} | \
           {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}


def test_ac6_single_decision_point():
    """AC-6 BR-2 BR-5: the reuse decision is made in load_reuse only (no age check, no VERIFY_REUSE_S); its
    callers are verify (or #20's verify_reuse_decision) and reuse_report; cmd_status reads the cache only
    through reuse_report; the §8.2 interfaces exist with their signatures."""
    src = HARNESS_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fns = _functions(tree)
    sigs = {"scripts_hash": [], "reuse_parts": ["cfg", "saved", "rg_state", "appr_state"],
            "reuse_key": ["cfg", "saved", "rg_state", "appr_state"], "stored_reuse": [], "load_reuse": ["key"],
            "store_reuse": ["key", "out", "results"], "drop_reuse": [], "reuse_report": ["cfg"]}
    for name, params in sigs.items():
        assert name in fns, f"{name}() missing"
        assert [a.arg for a in fns[name].args.args] == params, f"{name} signature changed"
    consts = {}
    for n in tree.body:
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    consts[t.id] = n.value
    assert isinstance(consts.get("SHORT_KEY"), ast.Constant) and consts["SHORT_KEY"].value == 12
    assert isinstance(consts.get("LAST_REUSE_PARTS"), ast.Dict) and not consts["LAST_REUSE_PARTS"].keys
    assert "VERIFY_REUSE_S" not in _names(tree), "VERIFY_REUSE_S (the 30-minute rule) is still there"
    assert "time" not in _names(fns["load_reuse"]), "load_reuse still looks at the clock"

    # the only comparison of a stored "key" with another key is inside load_reuse
    where = []
    for name, fn in fns.items():
        for n in ast.walk(fn):
            if isinstance(n, ast.Compare) and any(_is_key_lookup(x) for x in [n.left, *n.comparators]):
                where.append(name)
    assert where and set(where) == {"load_reuse"}, f"stored-key comparisons in: {where}"

    callers = {name for name, fn in fns.items() if _calls(fn, "load_reuse")}
    if "verify_reuse_decision" in fns:
        assert callers == {"verify_reuse_decision", "reuse_report"}, callers
        assert _calls(fns["verify"], "load_reuse") == 0
    else:
        assert callers == {"verify", "reuse_report"}, callers
        assert _calls(fns["verify"], "load_reuse") == 1
    st = _names(fns["cmd_status"])
    assert _calls(fns["cmd_status"], "reuse_report") == 1
    assert not st & {"reuse_file", "stored_reuse", "load_reuse", "reuse_key", "reuse_parts"}, st
    rep = _names(fns["reuse_report"])
    assert "load_reuse" in rep and "stored_reuse" in rep


# --- AC-7 -------------------------------------------------------------------------------------------
DOCS = ["README.md", "plugins/light-harness/README.md", "plugins/light-harness/skills/task-implement/SKILL.md",
        "plugins/light-harness/agents/reviewer.md"]


@pytest.mark.parametrize("doc", DOCS)
def test_ac7_docs_drop_30_minute_rule(doc):
    """AC-7: no doc describes the 30-minute reuse limit any more."""
    text = (TASK_DIR / doc).read_text(encoding="utf-8")
    hits = [l.strip()[:120] for l in text.splitlines() if re.search(r"30[\s-]*min", l, re.I)]
    assert not hits, f"{doc} still mentions 30 minutes: {hits}"


def test_ac7_docs_describe_key_reuse():
    """AC-7: README.md's reuse row describes reuse by key: it names the key, the harness version,
    `verify --fresh`, and that `verdict pass` reuses on an unchanged key while `verdict pass --fresh` re-runs
    (#39 AC-7: updated per its Q-2); and it has no 30-minute condition."""
    text = (TASK_DIR / "README.md").read_text(encoding="utf-8")
    rows = [l for l in text.splitlines() if l.startswith("|") and re.search(r"reuse", l, re.I)
            and "verify --fresh" in l]
    assert len(rows) == 1, f"expected one README table row about reused verify runs, found {len(rows)}"
    row = rows[0]
    assert re.search(r"\bkey\b", row) and re.search(r"harness version", row, re.I), row
    assert re.search(r"verdict pass`?\**[^|]{0,120}?\breus", row) and "verdict pass --fresh" in row, row
    assert not re.search(r"verdict pass`?\**[^|]{0,80}?\b(never reuses?|always runs)", row), row
    assert not re.search(r"30[\s-]*min", row, re.I), row
    assert not re.search(r"minutes? old", row, re.I), row


# --- BR-5 -------------------------------------------------------------------------------------------
def test_br5_failed_result_never_served(lab):
    """BR-5 AC-2 AC-5: (a) a failing suite drops the stored pass and both failing verifies run; (b) a failing
    saved probe: both verifies run; (c) a forged entry with the current key and result fail is not served;
    (d) a 1.8-format entry (no parts, no result) with the current key is not served (R-8)."""
    lab.first_pass()
    lab.code(WRONG)
    rc1, out1, ran1 = lab.verify()
    rc2, out2, ran2 = lab.verify()
    assert (rc1 != 0, rc2 != 0, ran1, ran2) == (True, True, 1, 1), out2
    assert "cached" not in out1 + out2
    assert lab.verify_line() == "no cached pass"

    lab.code(IMPL)
    rc, out = lab.probe_add(lab.probe_script("bad.py", 1), "RV-2")
    assert rc == 0, out
    rc1, out1, ran1 = lab.verify()
    rc2, out2, ran2 = lab.verify()
    assert (rc1 != 0, rc2 != 0, ran1, ran2) == (True, True, 1, 1), out2
    assert "cached" not in out1 + out2
    assert lab.verify_line() == "no cached pass"
    rc, out = lab.human("probe", "drop", "RV-2.1", "--name", "Human Approver", "--reason", "done")
    assert rc == 0, out

    k = lab.first_pass()
    e = lab.entry()
    e["result"] = "fail"
    lab.cache.write_text(json.dumps(e), encoding="utf-8")
    assert lab.verify_line() == f"stale (changed: unknown), stored key {k[:12]}"
    rc, out, ran = lab.verify()
    assert ran == 1 and "cached" not in out, out

    # (d) AC-5 R-8: a 1.8-format entry (no parts, no result) carrying the current key is not served, and
    # status says `stale (changed: unknown)` without crashing
    e = lab.entry()
    assert e["key"] == k and e["result"] == "pass"
    old = {"key": k, "when": time.time(), "label": e["label"], "out": e["out"], "results": e["results"]}
    lab.cache.write_text(json.dumps(old), encoding="utf-8")
    assert lab.verify_line() == f"stale (changed: unknown), stored key {k[:12]}"
    rc, out, ran = lab.verify()
    assert ran == 1 and "cached" not in out, out


# --- BR-3 sweep -----------------------------------------------------------------------------------
def _sweep_cells(lab: Lab) -> list:
    """(label, key part or None for a control, action). Fixed order; each change cell starts from the pass
    stored by the previous cell's run."""
    t, env_extra = lab.task, {"LH14_EXTRA_VAR": "1"}

    def write(rel, text):
        return lambda: _write(t / rel, text)

    def unlink(rel):
        return lambda: (t / rel).unlink()

    def rename(a, b):
        def go():
            (t / a).rename(t / b)
        return go

    def crlf():
        p = t / "src" / "m.py"
        p.write_bytes(p.read_bytes().replace(b"\n", b"\r\n"))

    def commit_same():
        lab.git("add", "-A")
        lab.git("commit", "-qm", "same tree")

    def caches():
        _write(t / "src" / "__pycache__" / "x.cpython-39.pyc", "junk")
        _write(t / ".pytest_cache" / "v" / "new", "x")
        _write(t / "tests" / "__pycache__" / "y.pyc", "junk")

    def new_redgate():
        rc, out = lab.human("unlock-tests", "--name", "Human Approver", "--reason", "sweep")
        assert rc == 0, out
        rc, out, ran = lab.verify()
        problem = None if (ran == 0 and "cached" not in out and rc != 0) else \
            f"verify right after unlock ran the suite {ran}x or reused (exit {rc})"
        keep = (t / "src" / "t.py").read_bytes()
        lab.code(STUB)
        lab.ok("redgate")
        (t / "src" / "t.py").write_bytes(keep)
        return problem

    def probe(name, issue, note):
        def go():
            rc, out = lab.probe_add(lab.probe_script(name, 0, note), issue)
            assert rc == 0, out
        return go

    def drop():
        rc, out = lab.human("probe", "drop", "RV-1.1", "--name", "Human Approver", "--reason", "sweep")
        assert rc == 0, out

    def cfg(mutate):
        def go():
            c = json.loads(lab.config.read_text(encoding="utf-8"))
            text = mutate(c)
            lab.config.write_text(text, encoding="utf-8")
        return go

    def set_value(c):
        c["parallel"] = 2
        return json.dumps(c, indent=2) + "\n"

    def reformat(c):
        return json.dumps(c, indent=4) + "\n"

    def script_byte(name):
        def go():
            p = lab.scripts / name
            p.write_bytes(p.read_bytes() + b"\n# sweep\n")
        return go

    def version_string():
        p = lab.scripts / "harness.py"
        text, n = re.subn(r'^__version__ = "[^"]*"', '__version__ = "1.8.1+sweep"', p.read_text(encoding="utf-8"),
                          count=1, flags=re.M)
        assert n == 1
        p.write_text(text, encoding="utf-8")

    return [
        ("control: nothing changed", None, lambda: None, None),
        ("tree: edit tracked file", "tree", lambda: lab.code(IMPL2), None),
        ("tree: add untracked file", "tree", write("src/extra.py", "X = 1\n"), None),
        ("control: commit of the same tree", None, commit_same, None),
        ("tree: add untracked unicode file in a new folder", "tree", write("src/neu ordner/naïve ü.txt", "ü\n"), None),
        ("tree: line endings only (LF -> CRLF) in a tracked file", "tree", crlf, None),
        ("tree: delete tracked file", "tree", unlink("src/m.py"), None),
        ("tree: rename tracked file", "tree", rename("src/util.py", "src/util_renamed.py"), None),
        ("tree: add a new ignored file", "tree", write(".env", "SECRET=1\n"), None),
        ("control: edit an ignored file's content", None, write(".env", "SECRET=2\n"), None),
        ("tree: remove that ignored file", "tree", unlink(".env"), None),
        ("control: add __pycache__/ and .pytest_cache/ entries", None, caches, None),
        ("tests: unlock + new red gate", "tests", new_redgate, None),
        ("probes: add", "probes", probe("p1.py", "RV-1", "one"), None),
        ("probes: add a new version for the same issue", "probes", probe("p2.py", "RV-1", "two"), None),
        ("probes: drop", "probes", drop, None),
        ("config: value changed", "config", cfg(set_value), None),
        ("config: whitespace-only reformat", "config", cfg(reformat), None),
        ("version: __version__ string changed", "version", version_string, None),
        ("version: harness.py byte changed", "version", script_byte("harness.py"), None),
        ("version: telemetry.py byte changed", "version", script_byte("telemetry.py"), None),
        ("version: guard.sh byte changed", "version", script_byte("guard.sh"), None),
        ("env: extra variable set for the run", "env", lambda: None, env_extra),
    ]


def test_br3_sweep_every_key_part_change_forces_run(lab):
    """BR-3 [sweep] AC-2 BR-1 BR-2: every (key part x change kind) cell, chained from one stored pass to the
    next: the status line is `stale` naming the part with the previous stored key, the next verify runs the
    suite exactly once without `cached`, and stores a pass under a new key. Every control (no key change)
    is `cached` in status and verify doesn't run the suite."""
    cells = _sweep_cells(lab)
    stored = lab.first_pass()
    bad = []
    for label, part, action, extra in cells:
        problem = action()
        if problem:
            bad.append(f"{label}: {problem}")
        line = lab.verify_line(extra)
        rc, out, ran = lab.verify(extra=extra)
        now_key = lab.stored_key()
        if part is None:
            if not (line or "").startswith(f"cached, key {stored[:12]} (passed "):
                bad.append(f"{label}: status {line!r}, expected cached {stored[:12]}")
            if ran != 0 or f"cached, key {stored[:12]}" not in out:
                bad.append(f"{label}: verify ran the suite {ran}x / didn't say cached")
            continue
        m = STALE_RE.match(line or "")
        if not m:
            bad.append(f"{label}: status {line!r}, expected stale")
        elif part not in m.group(1).split(", ") or m.group(2) != stored[:12]:
            bad.append(f"{label}: status {line!r}, expected {part} changed from {stored[:12]}")
        if ran != 1 or "cached" in out:
            bad.append(f"{label}: verify ran the suite {ran}x (expected 1) / said cached={'cached' in out}")
        if now_key is None or now_key == stored:
            bad.append(f"{label}: no pass stored under a new key after the run ({now_key!r})")
        stored = now_key or stored
    n_changes = sum(1 for c in cells if c[1])
    differ = sorted({b.split(":", 2)[0] + ":" + b.split(":", 2)[1] for b in bad})
    summary = (f"{len(differ)} of {len(cells)} cells differ ({n_changes} key changes, {len(cells) - n_changes} "
               f"controls; {len(bad)} problems)")
    print(summary)
    assert not bad, summary + ":\n  " + "\n  ".join(bad[:8])
