#!/usr/bin/env python3
# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""Light harness: stage gates for contract-driven agentic development.

Stages: Draft -> Initialize -> Plan -> (human approval) -> Build -> Review -> Done | Rework

The harness leaves nothing in the work repo. It runs from the plugin, and its state lives inside
git, outside the working tree, so it never reaches a commit, a branch or a pull request:
  * .tasks/<name>/              the task's git worktree on branch task/<name> (ignored through
                                .git/info/exclude, removed when the task is finished)
  * .tasks/<name>/.light-harness/   the task's live contract.md and config.json, and saved probes
                                copied out to probes/ when they run (ignored)
  * .tasks/<name>/.light-harness-base/  base-commit copies written by `baseline` (ignored)
  * refs/light-harness/tasks/<name>  the audit trail: one commit per gate event ("harness: ..."),
                                each holding the contract and gate records (and saved probes),
                                with a snapshot of the task's code as its second parent. Gates are recomputed from
                                this history, not from a side file.
  * refs/light-harness/done/<name>   the reviewed work of a finished task (for --from)
  * .git/light-harness/         repo defaults (the last task's config.json), scratch files and the
                                telemetry event log (telemetry/events.jsonl; see telemetry.py)
The task branch holds only the work itself: the agent's own commits of tests and code. A finished
task keeps its branch for you to merge or open a PR from. `uninstall` removes everything above.

Run `light-harness --help` (or `python3 <plugin>/scripts/harness.py --help`) for commands.
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import datetime as dt
import fnmatch
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import telemetry  # noqa: E402

__version__ = "1.7.0"
telemetry.VERSION = __version__

# All harness text is UTF-8 (contract, config, tool output). Never rely on the locale default:
# on Windows that is cp1252, which can't encode ✓/✗/→ and misreads UTF-8 files.
UTF8 = "utf-8"
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding=UTF8, errors="replace")

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = PLUGIN_ROOT / "template"
LIVE = ".light-harness"                      # per-task folder inside the task worktree (ignored)
STATE = "light-harness"                      # folder inside the git common dir
REF_TASKS, REF_DONE = "refs/light-harness/tasks/", "refs/light-harness/done/"
TASK_PREFIX = "task/"
EXCLUDE_BEGIN = "# >>> light-harness (local only, never committed)"
EXCLUDE_END = "# <<< light-harness"


def _toplevel(p: Path):
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=p, capture_output=True,
                           encoding=UTF8, errors="replace")
    except (OSError, NotADirectoryError):
        return None
    out = r.stdout.strip()
    return Path(out).resolve() if r.returncode == 0 and out else None


def _find_root() -> Path:
    """Default root: $HARNESS_ROOT, else the git checkout around the cwd (the main checkout or a
    task worktree), else $CLAUDE_PROJECT_DIR. Commands pick a task with --task <name> (set_root),
    and the guard hook picks the task that contains the edited file."""
    if os.environ.get("HARNESS_ROOT"):
        return Path(os.environ["HARNESS_ROOT"]).resolve()
    top = _toplevel(Path.cwd())
    if top:
        return top
    if os.environ.get("CLAUDE_PROJECT_DIR"):
        return Path(os.environ["CLAUDE_PROJECT_DIR"]).resolve()
    return Path.cwd().resolve()


def set_root(path: Path):
    """Point every command at one checkout (the main checkout or a task worktree)."""
    global ROOT, CONTRACT, CONFIG, JUNIT
    ROOT = Path(path).resolve()
    CONTRACT = ROOT / LIVE / "contract.md"
    CONFIG = ROOT / LIVE / "config.json"
    JUNIT = ROOT / LIVE / "junit.xml"


def is_task_checkout(d: Path) -> bool:
    return (d / ".git").is_file() and (d / LIVE / "contract.md").is_file()


def task_root_for(path: Path):
    """The task worktree that owns `path` (nearest checkout above it with a live contract), or None."""
    p = Path(path).resolve()
    for d in (p, *p.parents):
        if (d / ".git").exists():
            return d if is_task_checkout(d) else None
    return None


ROOT = CONTRACT = CONFIG = JUNIT = None  # set just below
TEL: dict = {}  # attributes of the running command's telemetry event (see main())
set_root(_find_root())
LAST_RESULTS: dict = {}  # test outcomes from the most recent run_tests()

TRANSITIONS = {
    "Draft": {"Initialize"},
    "Initialize": {"Initialize", "Plan"},
    "Plan": {"Plan", "Initialize", "Build"},
    "Build": {"Build", "Plan", "Review"},
    "Rework": {"Build", "Plan"},
    "Review": set(),          # leaves Review only through `verdict`
    "Done": {"Initialize"},   # reopen with new scope
}
# Files in the work repo an agent must not change during a task (its own permissions and agents)
MACHINERY = [".claude/settings.json", ".claude/settings.local.json", ".claude/agents/**",
             ".claude/commands/**", ".claude/skills/**"]
OWNED_BLOCKS = ["approval", "redgate", "verify", "verdict", "status-log"]
BLOCKING_SEVERITIES = {"critical", "high", "medium"}

DEFAULT_CONFIG = {
    "test_cmd": ["python3", "-m", "pytest", "-q"],
    "junit_arg": "--junitxml={path}",
    "red_exit_codes": [1],
    "test_globs": ["tests/**", "test_*.py", "*_test.py", "*.test.*", "*.spec.*", "__tests__/**"],
    "protected_globs": ["conftest.py", "pytest.ini", "tox.ini", "setup.cfg",
                        "jest.config.*", "vitest.config.*", "samples/**"],
    "always_allowed": [".gitignore", "README.md"],
    "worktree_dir": ".tasks",
}

# --- anti-gaming patterns ----------------------------------------------------
FAIL_PATTERNS = [
    (r"PYTEST_CURRENT_TEST", "detects the pytest runtime (behaves differently under test)"),
    (r"['\"]pytest['\"]\s+in\s+sys\.modules", "detects the pytest runtime"),
    (r"JEST_WORKER_ID|process\.env\.VITEST", "detects the JS test runtime"),
    (r"NODE_ENV\s*===?\s*['\"]test['\"]", "branches on NODE_ENV=test"),
    (r"@pytest\.mark\.(skip|skipif|xfail)|pytest\.(skip|xfail)\(", "skips/xfails tests"),
    (r"\b(it|test|describe)\.(skip|only|todo)\(|\bx(it|describe)\(", "skips or focuses JS tests"),
    (r"@unittest\.(skip|expectedFailure)", "skips unittest tests"),
]
WARN_PATTERNS = [
    (r"^\s*except\s*(Exception|BaseException)?\s*(as\s+\w+)?\s*:\s*(pass)?\s*$", "broad or silent exception handler"),
    (r"catch\s*(\(\s*\w*\s*\))?\s*\{\s*\}", "empty catch block"),
    (r"pragma:\s*no cover|istanbul ignore", "coverage exclusion"),
    (r"\bunittest\.mock\b|\bjest\.mock\(", "mocking library used in implementation code"),
]
# Implementation code must not be able to tell a probe run from a normal one. Probes get no marker in
# their environment (the base folder is an argument), so the code under test would have to name the
# harness or look at the process that started it. Checked in code files only, outside comments, so a
# config file that ignores .light-harness-base/ is fine.
CODE_EXT = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".mts", ".cts", ".rb", ".go", ".java", ".kt",
            ".cs", ".php", ".rs", ".sh", ".bash", ".ps1", ".pl", ".lua", ".swift", ".scala", ".c", ".cc", ".cpp", ".h"}
HARNESS_NAME = re.compile(r"(?<![A-Za-z0-9])light[-_]?harness(?![A-Za-z0-9])|\bLightHarness\b", re.I)
HARNESS_DIRS = re.compile(r"\.light-harness(?:-base)?(?![A-Za-z0-9])")  # task-only folders, never a domain term
PARENT_PEEK = re.compile(r"\bgetppid\b|/proc/|\bpsutil\b|\bparent_process\b|\bprocess\.ppid\b")


def code_gaming(path: str, lines: list[str], probes_saved: bool = True,
                waived: bool = False) -> tuple[list[str], list[str]]:
    """(failures, warnings) for implementation code that could tell it is being checked. Naming the
    harness's own folders fails (they exist only inside a task). Naming the harness is a warning until
    the task has saved probes, then a failure. A §10.3 deviation that lists the file (`waived`: a tool
    config that ignores the folders, a product named after the harness) keeps either a warning."""
    if Path(path).suffix.lower() not in CODE_EXT:
        return [], []
    code = [l for l in lines if not l.lstrip().startswith(("#", "//", "*", "/*", "--"))]
    fails, warns = [], []
    if any(HARNESS_DIRS.search(l) for l in code):
        (warns if waived else fails).append(
            f"Anti-gaming{' (review)' if waived else ''}: {path} names the harness's own folders (.light-harness-base/ "
            "or .light-harness/), which exist only inside a task, so it could behave differently while it is checked"
            + ("; declared in §10.3, reviewer must assess" if waived else
               "; if it only ignores them (a tool's config), declare the file in §10.3 with the reason"))
    elif any(HARNESS_NAME.search(l) for l in code):
        hard = probes_saved and not waived
        (fails if hard else warns).append(
            f"Anti-gaming{'' if hard else ' (review)'}: {path} names the harness, so it could tell a check apart"
            + ("; declared in §10.3, reviewer must assess" if waived else
               "; if it's a domain term, declare the file in §10.3 with the reason" if hard else ""))
    if any(PARENT_PEEK.search(l) for l in code):
        warns.append(f"Anti-gaming (review): {path} inspects its parent process; check it can't tell a probe run apart")
    return fails, warns


# --- small utilities ----------------------------------------------------------
def now() -> str:
    return dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")


def die(msg: str, code: int = 1):
    if str(msg).strip():
        TEL.setdefault("reason", str(msg).strip().splitlines()[0][:200])
    print(f"✗ {msg}", file=sys.stderr)
    sys.exit(code)


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG.exists():
        cfg.update(json.loads(CONFIG.read_text(encoding=UTF8)))
    return cfg


def matches(path: str, globs: list[str]) -> bool:
    name = path.rsplit("/", 1)[-1]
    for g in globs:
        if fnmatch.fnmatch(path, g) or ("/" not in g and fnmatch.fnmatch(name, g)):
            return True
    return False


def filled(s: str) -> bool:
    """True if text has content beyond <placeholders>."""
    s = re.sub(r"<[^>]*>", "", s)
    s = re.sub(r"\b(AC|BR|EX|R|RV)-\d+\b", "", s)  # bare IDs alone are not content
    return bool(s.strip(" \t-|*:`_()[],→>"))


# --- git ----------------------------------------------------------------------
def git(*args: str, check: bool = True, cwd: Path | None = None, input: str | None = None,
        env: dict | None = None):
    # Bytes in and out: in text mode on Windows, Python turns "\n" written to stdin into "\r\n",
    # and `git mktree` keeps that "\r" in file names. Output is decoded as before (UTF-8, CRLF -> LF).
    r = subprocess.run(["git", *args], cwd=cwd or ROOT, capture_output=True,
                       input=None if input is None else input.encode(UTF8), env=env)
    out = r.stdout.decode(UTF8, "replace").replace("\r\n", "\n")
    err = r.stderr.decode(UTF8, "replace").replace("\r\n", "\n")
    if check and r.returncode != 0:
        die(f"git {' '.join(args)} failed: {err}")
    return out


def hash_paths(paths: list[str], *flags: str) -> dict[str, str]:
    """Blob id of each working-tree file, as `git hash-object [flags]` computes it.
    Paths go on stdin: one argument each overflows Windows's 32,767-character command line in a
    repo with a few hundred test files. --stdin-paths C-unquotes a line that starts with '"' and
    splits on newlines, so those rare names (impossible on Windows) go one at a time."""
    odd = {f for f in paths if f.startswith('"') or "\n" in f or "\r" in f}
    plain = [f for f in paths if f not in odd]
    ids = dict(zip(plain, git("hash-object", *flags, "--stdin-paths",
                              input="".join(f + "\n" for f in plain)).split())) if plain else {}
    ids.update((f, git("hash-object", *flags, "--", f).strip()) for f in odd)
    return ids


def ident(cwd: Path | None = None) -> list[str]:
    if git("config", "user.name", check=False, cwd=cwd).strip() and \
       git("config", "user.email", check=False, cwd=cwd).strip():
        return []
    return ["-c", "user.name=light-harness", "-c", "user.email=harness@localhost"]


def ref_sha(ref: str, cwd: Path | None = None) -> str:
    return git("rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False, cwd=cwd).strip()


def common_dir(cwd: Path | None = None) -> Path:
    out = git("rev-parse", "--git-common-dir", cwd=cwd).strip()
    p = Path(out)
    return (p if p.is_absolute() else (cwd or ROOT) / p).resolve()


def state_dir(create: bool = False) -> Path:
    d = common_dir() / STATE
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def worktrees(cwd: Path | None = None) -> list[dict]:
    """`git worktree list --porcelain` as dicts; the first entry is the main checkout."""
    items, cur = [], {}
    for line in git("worktree", "list", "--porcelain", cwd=cwd).splitlines() + [""]:
        if not line:
            if cur:
                items.append(cur)
            cur = {}
            continue
        k, _, v = line.partition(" ")
        cur[k] = v
    return items


def main_checkout() -> Path:
    return Path(worktrees()[0]["worktree"]).resolve()


def same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))


def is_within(child: Path, parent: Path) -> bool:
    c, p = (os.path.normcase(str(Path(x).resolve())) for x in (child, parent))
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60].strip("-")
    if not s:
        die(f"can't make a task name from {name!r}; use letters or digits")
    return s


def task_worktree(slug: str):
    for w in worktrees():
        if w.get("branch") == f"refs/heads/{TASK_PREFIX}{slug}":
            return Path(w["worktree"]).resolve()
    return None


def tasks_in_progress() -> list[tuple[str, Path]]:
    out = []
    for w in worktrees():
        b = w.get("branch", "")
        if b.startswith(f"refs/heads/{TASK_PREFIX}") and is_task_checkout(Path(w["worktree"])):
            out.append((b[len("refs/heads/" + TASK_PREFIX):], Path(w["worktree"]).resolve()))
    return out


def task_slug():
    if not is_task_checkout(ROOT):
        return None
    b = git("rev-parse", "--abbrev-ref", "HEAD", check=False).strip()
    return b[len(TASK_PREFIX):] if b.startswith(TASK_PREFIX) else None


# --- telemetry (telemetry.py: local event log, opt-in engineer nickname, optional OTLP export) --
def quiet_common():
    """The git common dir, or None. Never dies: telemetry must not change a command's outcome."""
    try:
        out = git("rev-parse", "--git-common-dir", check=False).strip()
    except (OSError, ValueError):
        return None
    if not out:
        return None
    p = Path(out)
    return (p if p.is_absolute() else ROOT / p).resolve()


def quiet_slug():
    try:
        return task_slug()
    except (OSError, ValueError, SystemExit):
        return None


def emit(event: str, common=None, task=None, **attrs):
    common = common or quiet_common()
    if common:
        telemetry.emit(common, event, cwd=common, task=task or quiet_slug(), **attrs)


# --- the audit trail: refs/light-harness/tasks/<name> --------------------------
def state_ref(slug: str | None = None) -> str:
    slug = slug or task_slug()
    if not slug:
        die("not in a task; pass --task <name> (see `light-harness tasks`)")
    return REF_TASKS + slug


def snapshot() -> str:
    """A commit of the task worktree as it is now (tracked + untracked, honouring ignores), made
    with a scratch index so the branch, the index and the working tree are left untouched."""
    head = ref_sha("HEAD")
    idx = state_dir(create=True) / f"index-{os.getpid()}"
    env = {**os.environ, "GIT_INDEX_FILE": str(idx)}
    try:
        git("read-tree", head, env=env)
        git("add", "-A", env=env)
        tree = git("write-tree", env=env).strip()
    finally:
        idx.unlink(missing_ok=True)
    if tree == git("rev-parse", f"{head}^{{tree}}").strip():
        return head
    return git(*ident(), "commit-tree", tree, "-p", head, "-m", "light-harness: snapshot").strip()


def snap_of(state_sha: str) -> str:
    """The code snapshot recorded with a harness event (its second parent)."""
    return git("rev-parse", f"{state_sha}^2").strip()


def record(msg: str, extra: dict | None = None, slug: str | None = None) -> str:
    """Append a harness event to the task's audit trail. The commit's tree holds the contract,
    the config and any gate records; its parents are the previous event and a code snapshot."""
    ref = state_ref(slug)
    prev = ref_sha(ref)
    if not prev:
        empty = git("mktree", input="").strip()
        prev_new = git(*ident(), "commit-tree", empty, "-m", f"light-harness: audit trail for {ref}").strip()
    entries = {}
    if prev:
        for e in git("ls-tree", "-z", prev).split("\0"):
            if e:
                meta, _, name = e.partition("\t")
                name = name.rstrip("\r\n")  # entries written by 1.4.0 on Windows end in "\r"
                entries[name] = meta.split()[2]
    files = {"contract.md": CONTRACT.read_text(encoding=UTF8)}
    if CONFIG.exists():
        files["config.json"] = CONFIG.read_text(encoding=UTF8)
    files.update(extra or {})
    for name, content in files.items():
        if content is None:
            entries.pop(name, None)
        else:
            entries[name] = git("hash-object", "-w", "--stdin", input=content).strip()
    tree = git("mktree", input="".join(f"100644 blob {b}\t{n}\n" for n, b in sorted(entries.items()))).strip()
    sha = git(*ident(), "commit-tree", tree, "-p", prev or prev_new, "-p", snapshot(), "-m", msg).strip()
    git("update-ref", "-m", msg, ref, sha, prev or "0" * 40)
    return sha


def harness_log() -> list[tuple[int, str, str]]:
    """(index, sha, subject) for this task's harness events, newest first (index 0 = newest)."""
    slug = task_slug()
    if not slug or not ref_sha(REF_TASKS + slug):
        return []
    out = git("log", "--first-parent", "--format=%H%x09%s", REF_TASKS + slug, check=False)
    rows = []
    for i, line in enumerate(out.splitlines()):
        sha, _, subj = line.partition("\t")
        if subj.startswith("harness:"):
            rows.append((i, sha, subj))
    return rows


def latest(prefix: str):
    for row in harness_log():
        if row[2].startswith(prefix):
            return row
    return None


def state_file(sha: str, name: str) -> str:
    return git("show", f"{sha}:{name}", check=False)


def task_base_row():
    """The harness event the current task started from: `task-start`, or the task's own earlier
    `verdict pass` when a Done task was reopened. Once the plan is approved, the newest one older
    than the approval, so the task's own PASS verdict doesn't count."""
    pre_approval = get_status() in ("Draft", "Initialize", "Plan")
    appr = None if pre_approval else latest("harness: plan-approved")
    for row in harness_log():
        if appr and row[0] <= appr[0]:
            continue
        if row[2].startswith(("harness: task-start", "harness: verdict pass")):
            return row
    return None


def _baseline_at(sha: str):
    raw = state_file(sha, "baseline.json")
    if not raw.strip():
        return None
    try:
        return set(json.loads(raw).get("passing", []))
    except json.JSONDecodeError:
        return None


def baseline_tests():
    """Tests that passed before this task started (earlier tasks or existing code). They must
    keep passing, and are allowed to pass at the red gate. None if nothing was recorded."""
    base = task_base_row()
    for row in harness_log():
        if base and row[0] >= base[0]:
            break
        if row[2].startswith("harness: baseline"):
            return _baseline_at(row[1])
    return _baseline_at(base[1]) if base else None


def task_test_files(cfg, base=None) -> list[str]:
    """Test files added or changed by this task (all test files when there is no task base)."""
    base = base if base is not None else task_base_row()
    tests = test_files(cfg)
    if not base:
        return tests
    changed = set(changed_files(snap_of(base[1])))
    return [f for f in tests if f in changed]


def changed_files(base: str) -> list[str]:
    tracked = git("diff", "--name-only", base).splitlines()
    untracked = git("ls-files", "--others", "--exclude-standard").splitlines()
    return sorted({f for f in tracked + untracked if f})


def all_files() -> list[str]:
    return [f for f in git("ls-files", "-co", "--exclude-standard").splitlines() if f]


def added_lines(base: str, path: str) -> list[str]:
    if path in git("ls-files", "--others", "--exclude-standard").splitlines():
        try:
            return (ROOT / path).read_text(encoding=UTF8, errors="ignore").splitlines()
        except (IsADirectoryError, FileNotFoundError):
            return []
    diff = git("diff", "-U0", base, "--", path)
    return [l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]


# --- contract parsing ---------------------------------------------------------
def contract() -> str:
    if not CONTRACT.exists():
        names = ", ".join(s for s, _ in tasks_in_progress()) or "none"
        die(f"no task contract here ({ROOT}). Pass --task <name> (in progress: {names}), "
            "or start one with `light-harness start-task <name>`.")
    return CONTRACT.read_text(encoding=UTF8)


def block_span(text: str, name: str):
    return re.search(rf"<!-- harness:{re.escape(name)} -->\n?(.*?)<!-- /harness:{re.escape(name)} -->",
                     text, re.S)


PLAN_BLOCKS = {"goal", "rules", "examples", "acs", "risks", "impact", "scope", "steps", "test-strategy"}


def block(name: str, text: str | None = None) -> str:
    text = text if text is not None else contract()
    if name in PLAN_BLOCKS:
        # Read §1–§8 only from the plan block, the part the plan hash covers. Otherwise a copy of a
        # marker placed elsewhere (say, above the plan) would be parsed instead of what was approved.
        text = block("plan", text)
    m = block_span(text, name)
    return m.group(1) if m else ""


def set_block(name: str, body: str):
    text = contract()
    m = block_span(text, name)
    if not m:
        die(f"contract.md is missing the harness:{name} block")
    new = text[:m.start(1)] + body.rstrip() + "\n" + text[m.end(1):]
    CONTRACT.write_text(new, encoding=UTF8)


def get_status(text: str | None = None) -> str:
    if text is None and not CONTRACT.exists():
        return "Draft"
    m = re.search(r"^Status:\s*(\S+)", text if text is not None else contract(), re.M)
    return m.group(1) if m else "Draft"


def write_status(new: str, note: str = ""):
    text = contract()
    old = get_status(text)
    text = re.sub(r"^Status:\s*\S+", f"Status: {new}", text, count=1, flags=re.M)
    CONTRACT.write_text(text, encoding=UTF8)
    log = block("status-log").rstrip()
    entry = f"- {now()}: {old} → {new}" + (f" ({note})" if note else "")
    set_block("status-log", (log + "\n" if log else "") + entry)
    if old != new:
        emit("stage", **{"from": old, "to": new})


def plan_hash(text: str | None = None) -> str:
    body = block("plan", text)
    norm = "\n".join(l.rstrip() for l in body.strip().splitlines())
    return hashlib.sha256(norm.encode()).hexdigest()[:16]


def list_items(name: str, text: str | None = None) -> list[str]:
    items = []
    for line in block(name, text).splitlines():
        m = re.match(r"\s*(?:[-*]|\d+\.)\s+(.*)", line)
        if m and filled(m.group(1)):
            items.append(m.group(1).strip())
    return items


def table_rows(name: str, text: str | None = None) -> list[list[str]]:
    rows = []
    for line in block(name, text).splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        rows.append(cells)
    rows = rows[1:]  # drop header
    return [r for r in rows if any(filled(c) for c in r[1:])]


def acs(text: str | None = None) -> dict[str, str]:
    out = {}
    for item in list_items("acs", text):
        m = re.match(r"(AC-\d+)\s*[:\-–]\s*(.+)", item)
        if m and filled(m.group(2)):
            out[m.group(1)] = m.group(2)
    return out


def scope_globs(text: str | None = None) -> list[str]:
    return [g for g in ((i.strip("` ").split() or [""])[0].strip("`") for i in list_items("scope", text)) if g]


# A path in each kind of MACHINERY, to tell which kinds a §8.1 entry such as `.claude/*` covers
# (fnmatch's * crosses "/", so that one covers them all).
MACHINERY_KINDS = {".claude/settings.json": "settings.json", ".claude/settings.local.json": "settings.local.json",
                   ".claude/agents/x/y.md": "every agent", ".claude/commands/x/y.md": "every command",
                   ".claude/skills/x/y.md": "every skill"}


def machinery_kinds(glob: str) -> list[str]:
    return [kind for path, kind in MACHINERY_KINDS.items() if fnmatch.fnmatch(path, glob)]


def approved_machinery(text: str | None = None) -> list[str]:
    """§8.1 entries that name agent machinery under .claude/ explicitly. Only these let a task change
    MACHINERY files: a broad entry such as `**` never does, so the approver sees exactly what's allowed."""
    return [g for g in scope_globs(text)
            if g.startswith(".claude/") and (matches(g, MACHINERY) or machinery_kinds(g))]


def machinery_allowed(path: str, text: str | None = None) -> bool:
    return matches(path, MACHINERY) and matches(path, approved_machinery(text))


def deviation_paths(text: str | None = None) -> list[str]:
    return [re.split(r"\s+[—–-]+\s+|\s*:\s+", i.strip("` "), maxsplit=1)[0].strip("` ")
            for i in list_items("deviations", text)]


# --- rules over an input space: kinds, sweeps, oracles, probes, issue classes ---------------------
# A business rule is an Example rule (the default), an Invariant (a property of every input) or a
# Preservation rule (outcomes unchanged from the base commit, apart from listed changes). The last two
# cover an open-ended input space, so a few examples can't prove them: the plan names a generated
# sweep and an oracle for each, and the tests and the reviewer sweep the space.
RULE_KINDS = {"example": "Example", "invariant": "Invariant", "preservation": "Preservation"}
SPACE_KINDS = ("Invariant", "Preservation")
RULE_FIELDS = {"input space": "Input space", "oracle": "Oracle", "unacceptable failure": "Unacceptable failure",
               "allowed changes": "Allowed changes"}
_BULLET = re.compile(r"^([ \t]*)(?:[-*+]|\d+[.)])\s+(.*)$")
_EMPH = r"(?:\*\*|__|\*|_|`)?"
_RULE_ID = re.compile(rf"{_EMPH}(BR-\d+){_EMPH}\s*(.*)$")
_TAG = re.compile(rf"{_EMPH}([\[(])([^\])\n]*)[\])]{_EMPH}\s*")
_FIELD = re.compile(r"(input space|oracle|unacceptable failure(?: direction)?|allowed changes?)\s*:\s*(.*)", re.I)
_HINT = re.compile(r"\b(every|for all|all inputs|any input|unchanged|keeps? (?:their|its|the same) "
                   r"(?:outcomes?|results?|output|behaviou?r)|existing (?:outcomes?|behaviou?r|results?)|"
                   r"regardless of|never (?:lets?|allows?))\b", re.I)


def _indent(s: str) -> int:
    return len(s.expandtabs(4))


def _tag_kind(bracket: str, raw: str, before_sep: bool):
    """The kind a bracketed or parenthesised tag names; the tag as written when it tries to name a kind
    but isn't exactly one (so the plan gate reports it instead of the rule silently becoming an
    Example); None when it is prose, such as `(see EX-2)` or `[Admin] users …` after the colon."""
    word = raw.strip().strip("*_` ").lower()
    if word in RULE_KINDS:
        return RULE_KINDS[word]
    if re.search(r"invariant|preserv", word) or (before_sep and bracket == "[" and re.fullmatch(r"[A-Za-z-]+", word)):
        return raw.strip()
    return None


def _rule_head(body: str):
    """(id, kind, text) for a rule line such as `BR-2 [Preservation]: …`, `**BR-2** (Invariant) …` or
    `BR-2: [Preservation] …`; None if the line doesn't start with a BR id."""
    m = _RULE_ID.match(body)
    if not m:
        return None
    rest, kind = m.group(2), None
    for before_sep in (True, False):
        t = _TAG.match(rest)
        if t and kind is None and not rest[t.end(2) + 1:].startswith("(") and \
                (k := _tag_kind(t.group(1), t.group(2), before_sep)) is not None:  # not a [markdown](link)
            kind, rest = k, rest[t.end():]
        if before_sep:
            rest = re.sub(r"^[:\-–—]\s*", "", rest)
    return m.group(1), kind or "Example", rest.strip()


# A rules-block line that names a kind where a tag would go (before the rule's colon, or wrapped on its
# own in brackets, braces, angle brackets, backticks or emphasis) must be read as that kind.
_KIND_WORD = re.compile(r"\b(invariants?|preservation)\b", re.I)
_KIND_WRAPPED = re.compile(r"[\[({<`*_]\s*(invariants?|preservation)\s*[\])}>`*_]", re.I)


def _parse_rules(text: str | None):
    """(specs, problems) for §2. Problems: duplicate ids, and lines that name a kind but don't parse as a
    rule of that kind (`- BR-2 {Preservation}: …`, `### BR-2 [Invariant]`, `Preservation BR-2: …`), so a
    spelling the parser doesn't know fails closed instead of quietly becoming an Example rule."""
    specs, probs, cur, cur_ind = {}, [], None, 0
    for line in block("rules", text).splitlines():
        m = _BULLET.match(line)
        head = _rule_head(m.group(2).strip()) if m else None
        if m and head and (cur is None or _indent(m.group(1)) <= cur_ind):
            br, kind, rest = head
            if br in specs:
                probs.append(f"{br} is defined twice in §2; give each rule its own id")
            cur, cur_ind = {"kind": kind, "text": rest, "fields": {}}, _indent(m.group(1))
            specs[br] = cur
            if kind == "Example" and re.search(r"\bBR-\d+", line) and (
                    _KIND_WORD.search(line.split(":", 1)[0]) or _KIND_WRAPPED.search(line)):
                probs.append(f"{br}: its line names a rule kind but isn't read as one ({line.strip()[:60]!r}). "
                             "Write it as `- BR-n [Preservation]: …` or `- BR-n [Invariant]: …`, or reword it.")
            continue
        f = _FIELD.match(m.group(2).strip()) if m else None
        if f and cur is not None and _indent(m.group(1)) > cur_ind:
            # a field's value may be just ids ("Allowed changes: EX-3, EX-4"), unlike filled()'s content
            if re.sub(r"<[^>]*>", "", f.group(2)).strip(" \t-|*:`_()[],→>"):
                key = f.group(1).lower()
                key = next(k for k in RULE_FIELDS if key.startswith(k.split()[0]))
                cur["fields"][key] = f.group(2).strip()
            continue
        if re.search(r"\bBR-\d+", line) and (_KIND_WORD.search(line.split(":", 1)[0]) or _KIND_WRAPPED.search(line)):
            probs.append(f"a line in §2 names a rule kind but isn't read as a rule ({line.strip()[:60]!r}). "
                         "Write it as a bullet: `- BR-n [Preservation]: …` or `- BR-n [Invariant]: …`.")
    return specs, probs


def rule_specs(text: str | None = None) -> dict:
    """{BR id: {"kind", "text", "fields"}} from §2. A rule is `- BR-n [Kind]: text`; its fields are
    more-indented sub-bullets (`- Oracle: …`). No tag means Example; a tag that tries to name a kind but
    isn't one is kept as written, so the plan gate reports it."""
    return _parse_rules(text)[0]


def sweep_entries(text: str | None, br: str) -> list[str]:
    """§8.4 bullets that hold `[sweep]` and the rule's id, each with its more-indented sub-lines."""
    lines, out = block("test-strategy", text).splitlines(), []
    for i, line in enumerate(lines):
        m = _BULLET.match(line)
        if not m or "[sweep]" not in line or not re.search(rf"\b{re.escape(br)}\b", line):
            continue
        ind, chunk = _indent(m.group(1)), [line]
        for nxt in lines[i + 1:]:
            if nxt.strip() and _indent(nxt[:len(nxt) - len(nxt.lstrip())]) <= ind:
                break
            chunk.append(nxt)
        out.append("\n".join(chunk))
    return out


def sweep_plan_problems(text: str | None = None) -> list[str]:
    """Plan-gate problems: §2 lines that name a kind but don't parse as one, duplicate ids, unknown rule
    kinds, and Invariant/Preservation rules without their four parts or without a §8.4 sweep entry
    naming a generator and an oracle."""
    specs, probs = _parse_rules(text)
    for br, s in specs.items():
        kind = s["kind"]
        if kind not in RULE_KINDS.values():
            probs.append(f"{br} has an unknown kind [{kind}]: use [Invariant] or [Preservation], or no tag for an Example rule")
            continue
        if kind not in SPACE_KINDS:
            continue
        miss = [name for key, name in RULE_FIELDS.items() if key not in s["fields"]]
        if miss:
            probs.append(f"{br} [{kind}] doesn't state: {', '.join(miss)} (indented sub-bullets under the rule, "
                         f"e.g. `  - {miss[0]}: …`; Allowed changes may be `none`)")
        entries = sweep_entries(text, br)
        if not entries:
            probs.append(f"{br} [{kind}] has no [sweep] entry in §8.4: a bullet with `{br} [sweep]` and the test, "
                         "naming its `generator:` (dimensions, deterministic) and `oracle:`")
            continue
        def named(key: str, s: str):  # the key with a value after it, not a bare `oracle:`
            return re.search(rf"\b{key}\s*:[ \t]*[^\s<]", s, re.I)
        if not any(named("generator", e) and named("oracle", e) for e in entries):
            have = "\n".join(entries)
            need = [w for w in ("generator", "oracle") if not named(w, have)] or ["generator", "oracle"]
            probs.append(f"{br}: its §8.4 [sweep] entry names no {' and no '.join(w + ':' for w in need)} "
                         "(on the entry's line or its sub-bullets)")
    return probs


def rule_hints(text: str | None = None) -> list[str]:
    """Untagged rules whose wording suggests they hold for every input (a warning, not a failure)."""
    out = []
    for br, s in rule_specs(text).items():
        m = _HINT.search(s["text"]) if s["kind"] == "Example" else None
        if m:
            out.append(f"{br} reads like a rule over an input space ({m.group(0)!r}). If it holds for every input, "
                       "tag it [Invariant] or [Preservation] with its input space and oracle; otherwise ignore this.")
    return out


def sweep_tag_lines(src: str) -> list[str]:
    """Lines holding a `[sweep]` tag, leaving out comments (a TODO doesn't count as a sweep)."""
    return [l for l in src.splitlines() if "[sweep]" in l and not l.lstrip().startswith(("#", "//"))]


def missing_sweeps(text: str | None, tests_text: str) -> list[str]:
    """Invariant/Preservation rules with no line in the tests holding both their id and `[sweep]`."""
    tagged = sweep_tag_lines(tests_text)
    return [br for br, s in rule_specs(text).items() if s["kind"] in SPACE_KINDS
            and not any(re.search(rf"\b{re.escape(br)}\b", l) for l in tagged)]


def sweep_test_names(files: dict, brs) -> set:
    """(module, name) of each test whose own chunk carries `<BR> [sweep]` for one of `brs`: the test file's
    dotted module path (`tests/test_x.py` → `tests.test_x`) with its Python test function names, and
    with the titles of its it()/test() calls."""
    names = set()
    for path, src in files.items():
        module = re.sub(r"\.[^./]+$", "", path).replace("/", ".")
        for chunk in test_chunks(src):
            if any(re.search(rf"\b{re.escape(b)}\b", l) for l in sweep_tag_lines(chunk) for b in brs):
                names.update((module, n) for n in re.findall(r"def\s+(test\w*)", chunk))
                names.update((module, n) for n in re.findall(r"\b(?:it|test)(?:\.\w+)?\s*\(\s*['\"`]([^'\"`]+)", chunk))
    return names


def is_tagged_sweep(junit_name: str, names: set) -> bool:
    """A reported test (`module[.Class]::test[params]`) that is one of the tagged sweeps: the same
    function in the same module, or, for JS, a title that itself holds the tag. The test's own name
    must say `sweep`; a module or class name doesn't count."""
    cls, _, leaf = junit_name.rpartition("::")
    leaf = leaf.split("[")[0] if "[sweep]" not in leaf else leaf
    for module, name in names:
        if "[sweep]" in name:  # a JS title holding the tag itself: a copy elsewhere is tagged too
            if name in junit_name:
                return True
        elif name == leaf and re.search(r"sweep", leaf, re.I):
            # the module, optionally followed by a Test class; a rootdir below the task folder drops the
            # module path's leading folders (tests.test_g for backend/tests/test_g.py)
            mod = re.sub(r"\.Test\w*$", "", cls)
            if mod == module or module.endswith("." + mod):
                return True
    return False


# Base-commit copies ("light-harness baseline"): the files, plus their local imports, as they were at
# the task's base, side by side with the new code. Never committed (local exclude), never edited by
# agents (guard), always rewritten from git.
BASE_DIR = ".light-harness-base"
JS_EXT = (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".mts", ".cts")
_PY_IMPORT = re.compile(r"^[ \t]*import[ \t]+([\w.]+(?:[ \t]+as[ \t]+\w+)?(?:[ \t]*,[ \t]*[\w.]+(?:[ \t]+as[ \t]+\w+)?)*)", re.M)
_PY_FROM = re.compile(r"^[ \t]*from[ \t]+(\.*)([\w.]*)[ \t]+import[ \t]+(\([^)]*\)|[^\n#]+)", re.M)
_JS_SPEC = re.compile(r"""(?:\bfrom|\bimport|\brequire)\s*\(?\s*['"](\.{1,2}/[^'"\n]+)['"]""")


def _py_imports(path: str, src: str, exists) -> list[str]:
    here = posixpath.dirname(path)
    roots, d = [], here  # absolute imports: the repo root and every folder above the file (src layouts)
    while d:
        roots.append(d)
        d = posixpath.dirname(d)
    roots.append("")

    def mod(dotted: str, base: str) -> list[str]:
        stem = posixpath.join(base, *[p for p in dotted.split(".") if p]) if (base or dotted) else ""
        return [c for c in (stem + ".py", posixpath.join(stem, "__init__.py")) if stem and c in exists]

    out = []
    for m in _PY_IMPORT.finditer(src):
        for name in m.group(1).split(","):
            parts = name.split()[0].split(".")
            for base in roots:
                for i in range(1, len(parts) + 1):
                    out += mod(".".join(parts[:i]), base)
    for m in _PY_FROM.finditer(src):
        dots, dotted, names = m.groups()
        bases = [here] if dots else roots
        if dots:
            for _ in range(len(dots) - 1):
                bases = [posixpath.dirname(bases[0])]
        parts = [p for p in dotted.split(".") if p]
        for base in bases:
            for i in range(1, len(parts) + 1):
                out += mod(".".join(parts[:i]), base)
            pkg = posixpath.join(base, *parts) if (base or parts) else ""
            for n in re.split(r"[\s,()]+", names):
                n = n.strip()
                if n and n != "*" and n != "as" and re.fullmatch(r"\w+", n):
                    out += mod(n, pkg)
    return out


def _js_imports(path: str, src: str, exists) -> list[str]:
    out = []
    for spec in _JS_SPEC.findall(src):
        t = posixpath.normpath(posixpath.join(posixpath.dirname(path), spec))
        cands = [t] + [t + e for e in JS_EXT] + [f"{t}/index{e}" for e in JS_EXT]
        if t.endswith(".js"):  # TypeScript ESM imports name the compiled .js
            cands += [t[:-3] + e for e in (".ts", ".tsx", ".mts")]
        out += [c for c in cands if c in exists][:1]
    return out


def import_closure(paths, exists, read) -> list[str]:
    """`paths` plus the local files they import, transitively (Python: absolute and relative imports
    and the package __init__.py files; JS/TS: relative specifiers). `exists` is the set of files in the
    tree, `read(path)` returns a file's text."""
    todo, seen = list(paths), set()
    while todo:
        p = todo.pop()
        if p in seen or p not in exists:
            continue
        seen.add(p)
        if p.endswith(".py"):
            d = posixpath.dirname(p)
            while d:
                if f"{d}/__init__.py" in exists:
                    todo.append(f"{d}/__init__.py")
                d = posixpath.dirname(d)
            todo += _py_imports(p, read(p) or "", exists)
        elif p.endswith(JS_EXT):
            todo += _js_imports(p, read(p) or "", exists)
    return sorted(seen)


def base_commit() -> tuple[str, str]:
    """(sha, label) of the code this task started from: its task-start, or a reopened task's PASS."""
    base = task_base_row()
    if not base:
        die("this task has no recorded base (no task-start event); run it in a task started by light-harness")
    return snap_of(base[1]), base[2].removeprefix("harness: ")


def base_tree(sha: str) -> set[str]:
    out = set()
    for e in git("ls-tree", "-r", "-z", sha).split("\0"):
        meta, _, path = e.partition("\t")
        if path and meta.split()[1:2] == ["blob"]:
            out.add(path)
    return out


def blob(sha: str, path: str) -> bytes:
    return subprocess.run(["git", "cat-file", "blob", f"{sha}:{path}"], cwd=ROOT, capture_output=True).stdout


def export_base(files, sha: str, root: Path | None = None) -> list[str]:
    """Write the base-commit version of each file under `root` (default BASE_DIR in the task folder);
    files not in the base are skipped. Never writes through a link, never outside `root`."""
    if root is None:
        reset_base_dir()
        root = ROOT / BASE_DIR
    tree, out = base_tree(sha), []
    for f in files:
        if f not in tree:
            continue
        cur = root
        for part in Path(f).parts[:-1]:  # a link or file planted on the way is removed, not followed
            cur = cur / part
            if os.path.lexists(cur) and (_is_link(cur) or not cur.is_dir()):
                _unlink_entry(cur)
        dest = root / f
        dest.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(dest) and (_is_link(dest) or dest.is_dir()):
            _rm_tree(dest)
        if not is_within(Path(os.path.realpath(dest.parent)), Path(os.path.realpath(root))):
            die(f"refusing to write {f} outside {root}")
        tmp = dest.with_name(f".{dest.name}.lh-{os.getpid()}")
        tmp.write_bytes(blob(sha, f))
        os.replace(tmp, dest)  # a new file: a hard link planted at `dest` is replaced, not written through
        out.append(f)
    return out


@contextlib.contextmanager
def base_hidden():
    """While tests or probes run, keep the base-commit copies out of the task folder, as in CI. Code
    that behaves differently when .light-harness-base/ exists then gains nothing; probes get their own
    copies from probe_base(), outside the task folder."""
    d = ROOT / BASE_DIR
    slug = task_slug() or "task"
    aside = state_dir(create=True) / f"base-aside-{slug}"
    lock = state_dir() / f"base-lock-{slug}"
    _take_lock(lock)
    try:
        if not os.path.lexists(d) and aside.is_dir():
            _move_dir(aside, d)  # an earlier run was killed before it could put the copies back
        with _hidden(d, aside):
            yield
    finally:
        lock.unlink(missing_ok=True)


def _take_lock(lock: Path):
    """One test or probe run per task at a time, so a second run can't mistake the first's moved-aside
    copies for a crashed run's. A lock left by a killed run is taken over: its process is gone (POSIX),
    or it is over two hours old (Windows, where a process can't be probed safely)."""
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return
        except FileExistsError:
            try:
                pid = int(lock.read_text().strip() or 0)
            except (OSError, ValueError):
                pid = 0
            alive = False
            if os.name != "nt" and pid > 0:
                try:
                    os.kill(pid, 0)
                    alive = True
                except ProcessLookupError:
                    alive = False
                except PermissionError:
                    alive = True
            elif os.name == "nt":
                try:
                    alive = time.time() - lock.stat().st_mtime < 7200
                except OSError:
                    alive = False
            if alive:
                die(f"another test or probe run of this task is in progress (pid {pid}); wait for it to finish. "
                    f"If none is running (it was killed), a human can delete {lock}.")
            lock.unlink(missing_ok=True)
    die(f"can't take the run lock {lock}")


@contextlib.contextmanager
def _hidden(d: Path, aside: Path):
    """Move the task's base copies aside for the duration, and put them back."""
    moved = False
    if os.path.lexists(d) and (_is_link(d) or not d.is_dir()):
        _unlink_entry(d)  # a link or a file in its place: regenerable, just drop it
    elif d.is_dir():
        if os.path.lexists(aside):
            _rm_tree(aside)  # safe: the task folder holds the current copies
        _move_dir(d, aside)
        moved = True
    try:
        yield
    finally:
        if moved:
            if os.path.lexists(d):
                _rm_tree(d)  # something recreated it during the run
            _move_dir(aside, d)


def _move_dir(src: Path, dst: Path):
    """Move a folder as one rename. Across drives (worktree_dir elsewhere), copy and then delete, and if
    the delete fails, put everything back. Never leaves the copies split between two places."""
    try:
        os.rename(src, dst)
        return
    except OSError as e:
        if getattr(e, "errno", None) != 18:  # not EXDEV: e.g. a shell is working inside it (Windows)
            die(f"can't move {src} to {dst} ({e}). If a shell or program is working inside "
                f"{BASE_DIR}/, leave it and retry; nothing was changed.")
    shutil.copytree(src, dst, symlinks=True)
    try:
        _rm_tree(src)
    except OSError as e:
        shutil.copytree(dst, src, symlinks=True, dirs_exist_ok=True)
        _rm_tree(dst)
        die(f"can't move {src} aside ({e}). If a shell or program is working inside {BASE_DIR}/, leave it "
            "and retry; its files were put back.")


AGENT_ENV = {"CLAUDECODE", "CLAUDE_PROJECT_DIR", "CLAUDE_ENV_FILE", "CLAUDE_PLUGIN_ROOT", "HARNESS_ROOT"}
AGENT_ENV_PREFIXES = ("CLAUDE_CODE_", "LIGHT_HARNESS_")


def clean_env() -> dict:
    """The environment for test and probe runs, as in CI: without the agent session's and the harness's
    own variables (AGENT_ENV, CLAUDE_CODE_*, LIGHT_HARNESS_*) and without harness folders on PATH, so
    code under test can't tell it is being checked from its environment. A project's own variables,
    such as CLAUDE_API_URL, are kept."""
    env = {k: v for k, v in os.environ.items()
           if not (k.upper() in AGENT_ENV or k.upper().startswith(AGENT_ENV_PREFIXES))}
    if "PATH" in env:
        env["PATH"] = os.pathsep.join(p for p in env["PATH"].split(os.pathsep)
                                      if p and not is_within(Path(p), PLUGIN_ROOT))
    return env


def probe_base(entries: list[dict]) -> Path:
    """A fresh export of the files the probes recorded, outside the task folder (in the harness's own
    state, which agents can't write), for the probes to compare against."""
    d = state_dir(create=True) / f"probe-base-{task_slug() or 'task'}"
    if os.path.lexists(d):
        _rm_tree(d)
    d.mkdir(parents=True)
    files = sorted({f for e in entries for f in e.get("baseline") or []})
    if files:
        export_base(files, base_commit()[0], root=d)
    return d


def base_copies() -> list[str]:
    d = ROOT / BASE_DIR
    return sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file()) if d.is_dir() else []


def cmd_baseline(args):
    contract()
    sha, label = base_commit()
    tree = base_tree(sha)
    wanted, missing = [], []
    for raw in args.paths:
        p = Path(raw)
        if p.is_absolute():
            try:
                p = p.resolve().relative_to(ROOT)
            except ValueError:
                die(f"{raw} is outside the task folder {ROOT}")
        p = p.as_posix().strip("/")
        hits = sorted(tree) if p in ("", ".") else \
            sorted(f for f in tree if f == p or f.startswith(p + "/") or fnmatch.fnmatch(f, p))
        (wanted.extend(hits) if hits else missing.append(raw))
    if missing:
        die(f"not in the base commit {sha[:8]} ({label}): {', '.join(missing)}. Paths are relative to the task "
            "folder; a file this task created has no base version.")
    ensure_excludes([])  # tasks started before 1.7 lack the BASE_DIR line
    files = export_base(import_closure(wanted, tree, lambda f: blob(sha, f).decode(UTF8, "replace")), sha)
    TEL.update(files=len(files))
    shown = "".join(f"\n  {f}" for f in files[:40]) + (f"\n  … {len(files) - 40} more" if len(files) > 40 else "")
    print(f"✓ {len(files)} base-commit file(s) from {sha[:8]} ({label}) in {ROOT / BASE_DIR}:{shown}\n"
          "Run the old and the new code side by side, e.g. the same snippet in a separate process with its working "
          f"directory at {BASE_DIR}/ (a saved probe gets a fresh export of these files as the argument after the "
          f"script). Tests must not read {BASE_DIR}/ at run time (it isn't in CI, and the harness moves it out of "
          "the task folder while the suite runs): generate a golden file under the test folder from it instead.")


# Saved probes: a reviewer's sweep that found a defect, kept in the audit trail (agents can't change
# it) and re-run by every `verify`, so a finding becomes a permanent check.
PROBES = "probes.json"
PROBE_TIMEOUT = 600
PROBE_RUNNERS = {".py": ["python", "{file}", "{base}"], ".sh": ["sh", "{file}", "{base}"],
                 ".js": ["node", "{file}", "{base}"], ".mjs": ["node", "{file}", "{base}"],
                 ".cjs": ["node", "{file}", "{base}"]}


def probes() -> list[dict]:
    raw = state_file(state_ref(), PROBES)
    try:
        data = json.loads(raw) if raw.strip() else []
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def probe_cmd(entry: dict, path: Path, base: Path | None = None) -> list[str]:
    """The probe's command: `{file}` is the probe script, `{base}` the base-commit copies' folder (both
    appended, in that order, when --run leaves them out). The base path is an argument, never an
    environment variable, so the code under test inherits no marker of a probe run."""
    run = list(entry.get("run") or PROBE_RUNNERS.get(Path(entry["file"]).suffix.lower(), ["{file}", "{base}"]))
    for key in ("{file}", "{base}"):
        if not any(key in a for a in run):
            run.append(key)
    cmd = [a.replace("{file}", str(path)).replace("{base}", str(base or ROOT / BASE_DIR)) for a in run]
    if cmd[0] in ("python", "python3", "py"):
        cmd[0] = sys.executable
    return cmd


def _is_link(p: Path) -> bool:
    """A symlink or a Windows junction (on every Python version: its real path isn't under its parent's)."""
    if p.is_symlink():
        return True
    if not os.path.lexists(p):
        return False
    real, expect = os.path.realpath(p), os.path.join(os.path.realpath(p.parent), p.name)
    return os.path.normcase(real) != os.path.normcase(expect)


def _unlink_entry(p: Path):
    """Remove a file, a symlink or a junction itself, never what a link points to."""
    if os.path.isfile(p) and not _is_link(p):
        os.chmod(p, stat.S_IWRITE | stat.S_IREAD)  # a read-only file on Windows
    try:
        p.unlink()
    except (IsADirectoryError, PermissionError, OSError):
        os.rmdir(p)  # a junction, or a link to a folder on Windows


def _rm_tree(p: Path):
    """Delete a folder without ever following a symlink or junction inside it."""
    if _is_link(p) or not p.is_dir():
        _unlink_entry(p)
        return
    for child in p.iterdir():
        _rm_tree(child)
    p.rmdir()


def reset_base_dir():
    """BASE_DIR must be a real folder in the task folder: remove a link or a file standing in its place."""
    d = ROOT / BASE_DIR
    if os.path.lexists(d) and (_is_link(d) or not d.is_dir()):
        _unlink_entry(d)


def run_probes(entries: list[dict]) -> list[tuple[dict, int, str]]:
    """Run saved probes against fresh base copies outside the task folder, with the task's own
    .light-harness-base/ moved aside: [(entry, exit code, output)]."""
    if not entries:
        return []
    with base_hidden():
        base = probe_base(entries)
        return [(e, *run_probe(e, base)) for e in entries]


def run_probe(entry: dict, base: Path) -> tuple[int, str]:
    """Run a saved probe from its audit-trail copy (working directory: the task folder; the base-commit
    copies' folder `base` as its argument after the script). Exit 0: its defect class is gone; 1: it
    found differences; anything else: the probe broke."""
    content = state_file(state_ref(), entry["file"])
    pdir = ROOT / LIVE / "probes"
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / entry["file"]
    path.write_bytes(content.encode(UTF8))
    cmd = probe_cmd(entry, path, base)
    try:  # nothing in the environment marks a probe run (no session or harness markers either)
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, encoding=UTF8, errors="replace", env=clean_env(),
                           stdin=subprocess.DEVNULL, timeout=PROBE_TIMEOUT)
    except FileNotFoundError as e:
        return 127, f"can't run {cmd[0]}: {e}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {PROBE_TIMEOUT}s"
    return r.returncode, r.stdout + r.stderr


def cmd_probe(args):
    text = contract()
    if args.action == "list":
        ps = probes()
        if not ps:
            print("No saved probes for this task.")
        for p in ps:
            run = " ".join(p.get("run") or PROBE_RUNNERS.get(Path(p["file"]).suffix.lower(), ["?"]))
            print(f"{p['id']:<10} {p['issue']:<8} {p['file']:<28} {run:<22} saved {p.get('added', '?')}")
        return
    if args.action == "run":
        ps, bad = probes(), 0
        if not ps:
            print("No saved probes for this task.")
        for p, rc, out in run_probes(ps):
            bad += rc != 0
            print(f"{'✗' if rc else '✓'} {p['id']} ({p['issue']}): exit {rc}" + (f"\n{tail(out, 15)}" if rc else ""))
        TEL.update(probes=len(ps), failing=bad)
        sys.exit(1 if bad else 0)
    if args.action == "drop":
        require_human("probe drop")
        if not (args.name and args.reason):
            die("probe drop needs --name \"Full Name\" and --reason \"...\"")
        ps = probes()
        hit = next((p for p in ps if p["id"].lower() == (args.target or "").lower()), None)
        if not hit:
            die(f"no saved probe {args.target!r} (see `light-harness probe list`)")
        rest = [p for p in ps if p is not hit]
        record(f"harness: probe-dropped {hit['id']} by {args.name}: {args.reason}",
               {PROBES: json.dumps(rest, indent=2) + "\n", hit["file"]: None})
        TEL.update(reason_given=args.reason[:200])
        print(f"✓ Probe {hit['id']} dropped; `verify` no longer runs it.")
        return
    # add
    st = get_status(text)
    if st not in ("Build", "Review", "Rework"):
        die(f"status is {st}; probes are saved in Build, Review or Rework")
    iid = (args.issue or "").strip().upper()
    if not iid:
        die("name the issue the probe checks: --issue RV-n")
    if not any(r["id"].upper() == iid for r in issue_rows(text)):
        die(f"no issue {iid} in §11.2; log the issue, with its Class, before saving its probe")
    src = Path(args.target) if args.target else None
    if not src or not src.is_file():
        die(f"probe script not found: {args.target}")
    data = src.read_bytes()
    if len(data) > 512 * 1024:
        die("the probe is larger than 512 KB; keep a probe a small, self-contained script")
    try:
        content = data.decode(UTF8)
    except UnicodeDecodeError:
        die("the probe must be a UTF-8 text script")
    ext = src.suffix.lower()
    run = shlex.split(args.run, posix=os.name != "nt") if args.run else None
    if run and os.name == "nt":
        run = [a[1:-1] if len(a) > 1 and a[0] == a[-1] and a[0] in "\"'" else a for a in run]
    if not run and ext not in PROBE_RUNNERS:
        die(f"no default runner for {ext or 'a file without an extension'}; pass --run \"<command> {{file}}\"")
    n = 1 + sum(s.startswith(f"harness: probe-saved {iid}.") for _, _, s in harness_log())
    pid = f"{iid}.{n}"
    entry = {"id": pid, "issue": iid, "file": f"probe-{pid.lower()}{ext}", "run": run, "baseline": base_copies(),
             "added": now(), "stage": st}
    exe = probe_cmd(entry, src)[0]
    if not (shutil.which(exe) or Path(exe).is_file()):
        die(f"can't find the probe's runner {exe!r}; fix --run (nothing was saved)")
    record(f"harness: probe-saved {pid} for {iid}",
           {PROBES: json.dumps(probes() + [entry], indent=2) + "\n", entry["file"]: content})
    _, rc, out = run_probes([entry])[0]
    TEL.update(issue=iid, failing=int(rc != 0))
    print(f"✓ Probe {pid} saved for {iid} ({entry['file']}, kept in the task's audit trail). `verify` re-runs it "
          f"from now on and fails while it fails.\n  Now: {'fails' if rc else 'passes'} (exit {rc})\n{tail(out, 10)}"
          + ("" if entry["baseline"] else f"\n  Note: no base-commit copies were in {BASE_DIR}/ when it was saved, so "
             "verify restores none for it. If it reads them, run `light-harness baseline` first and save it again."))


# Review issues and their defect class
def issue_rows(text: str | None = None) -> list[dict]:
    """§11.2 rows as dicts keyed by the header's first word, lowercased (id, severity, status, area,
    class, description, ref). Older contracts have no Class column: their class is ""."""
    rows, head = [], None
    for line in block("issues", text).splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        body = line[1:-1] if line.endswith("|") and not line.endswith("\\|") else line[1:]
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", body)]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        if head is None:
            head = [m.group(0) if (m := re.match(r"\w+", c.lower())) else f"col{i}" for i, c in enumerate(cells)]
            continue
        if not any(filled(c) for c in cells[1:]):
            continue
        row = {k: (cells[i] if i < len(cells) else "") for i, k in enumerate(head)}
        for k in ("id", "severity", "status", "class"):
            row.setdefault(k, "")
        rows.append(row)
    return rows


class_name = telemetry.class_name
same_class_reopened = telemetry.same_class_reopened


def issue_rounds(slug: str) -> list[list[dict]]:
    """The §11.2 issues each time a review started and at each verdict, oldest first (audit trail)."""
    out = git("log", "--first-parent", "--reverse", "--format=%H%x09%s", REF_TASKS + slug, check=False)
    return [issue_rows(state_file(sha, "contract.md")) for sha, _, subj in (l.partition("\t") for l in out.splitlines())
            if subj.startswith(("harness: verdict", "harness: review-requested"))]


# Over-fit check: a rework regression test whose inputs are all quoted from its issue
_TEST_DEF = re.compile(r"\s*(?:async\s+)?def\s+test|\s*class\s+Test|\s*(?:it|test)(?:\.\w+)?\s*\(")
_TRIPLE = re.compile(r'("""|\'\'\')(.*?)\1', re.S)
_LITERAL = re.compile(r"""(['"`])((?:\\.|(?!\1).)*?)\1""")
_IDS = re.compile(r"\b(?:AC|BR|EX|RV|R)-\d+\b")


def test_chunks(src: str) -> list[str]:
    """A test file split into one chunk per test (a test function with its decorators, or an it()/test()
    call); text before the first test is a chunk of its own."""
    chunks, cur, in_deco = [], [], False
    for line in src.splitlines():
        if line.strip().startswith("@"):
            if not in_deco:
                if cur:
                    chunks.append(cur)
                cur, in_deco = [], True
        elif in_deco and re.match(r"\s*(?:async\s+)?(?:def|class)\s", line):
            in_deco = False  # the decorated function, test or helper (a fixture), stays with its decorators
        elif _TEST_DEF.match(line):
            if cur:
                chunks.append(cur)
            cur = []
        cur.append(line)
    if cur:
        chunks.append(cur)
    return ["\n".join(c) for c in chunks]


def overfit_warnings(issues: list[dict], files: dict) -> list[str]:
    texts = {r["id"].upper(): " ".join(r.values()) for r in issues if r.get("id")}
    out = []
    for path, src in files.items():
        for chunk in test_chunks(src):
            ids = sorted({i.upper() for i in re.findall(r"\bRV-\d+\b", chunk)} & set(texts))
            if not ids:
                continue
            lits = [m.group(2) for m in _LITERAL.finditer(_TRIPLE.sub("", chunk))]
            lits = [l for l in lits if len(l.strip()) >= 4 and not _IDS.search(l)]
            m = re.search(r"def\s+(test\w*)", chunk) or re.search(r"\b(?:it|test)\s*\(\s*['\"`]([^'\"`]*)", chunk)
            name = m.group(1) if m else "a test"
            for iid in ids:
                if lits and all(l in texts[iid] for l in lits):
                    out.append(f"Over-fit (review): {path} {name} for {iid} only uses inputs quoted in the issue "
                               f"({len(lits)} value(s)); a regression test should cover the class's dimensions")
    return out


# --- gate checks --------------------------------------------------------------
def check_init(text: str) -> list[str]:
    if not block_span(text, "plan"):
        return ["contract.md is missing the harness:plan block that holds §1–§8"]
    p = []
    if not filled(re.sub(r"\*\*[^*]+\*\*", "", block("goal", text))):
        p.append("§1 Task is empty")
    if not table_rows("examples", text):
        p.append("§3 needs at least one example")
    if not acs(text):
        p.append("§4 needs at least one acceptance criterion (AC-n: ...)")
    return p


def check_plan(text: str) -> list[str]:
    p = check_init(text)
    if not table_rows("risks", text):
        p.append("§7.2 has no risks (if truly none, add a row saying so and why)")
    if not table_rows("impact", text):
        p.append("§7.3 impact analysis is empty")
    if not scope_globs(text):
        p.append("§8.1 files-in-scope is empty")
    steps = list_items("steps", text)
    if not steps:
        p.append("§8.3 has no implementation steps")
    plan_refs = block("steps", text) + block("test-strategy", text)
    missing = [a for a in acs(text) if not re.search(rf"\b{a}\b", plan_refs)]
    if missing:
        p.append(f"ACs not covered by §8.3 steps / §8.4 test strategy: {', '.join(missing)}")
    return p + sweep_plan_problems(text)


def approval_state(text: str | None = None):
    """Return (ok, message, log_index)."""
    row = latest("harness: plan-approved")
    # A Done task that was reopened starts again at its PASS verdict; an older approval is stale.
    markers = ("harness: task-start",) + (() if get_status(text) == "Done" else ("harness: verdict pass",))
    start = next((r for r in harness_log() if r[2].startswith(markers)), None)
    if not row or (start and start[0] < row[0]):
        return False, "plan has not been approved by a human", None
    m = re.search(r"\[plan:([0-9a-f]+)\]", row[2])
    current = plan_hash(text)
    if not m or m.group(1) != current:
        return False, (f"plan changed after approval (approved {m.group(1) if m else '?'}, "
                       f"current {current}); re-approval required"), row[0]
    return True, row[2].removeprefix("harness: "), row[0]


def redgate_state():
    """Return (ok, message, state sha of the red-gate event)."""
    ok, msg, appr_idx = approval_state()
    rg = latest("harness: red-gate")
    if not rg:
        return False, "red gate has not been passed", None
    if appr_idx is None or rg[0] > appr_idx:
        return False, "red gate predates the current plan approval; run it again", None
    unlock = latest("harness: tests-unlocked")
    if unlock and unlock[0] < rg[0]:
        return False, "tests were unlocked after the red gate; re-run the red gate", None
    return True, f"red gate at {snap_of(rg[1])[:8]}", rg[1]


def test_files(cfg) -> list[str]:
    return [f for f in all_files() if matches(f, cfg["test_globs"]) and not matches(f, cfg["always_allowed"])]


def run_tests(cfg):
    cmd = list(cfg["test_cmd"])
    if cmd and cmd[0] in ("python", "python3", "py"):
        cmd[0] = sys.executable  # same interpreter as the harness (portable across OSes/venvs)
    junit_path = ROOT / cfg["junit_path"] if cfg.get("junit_path") else JUNIT
    junit_path.parent.mkdir(parents=True, exist_ok=True)
    junit_path.unlink(missing_ok=True)
    if cfg.get("junit_arg"):
        cmd.append(cfg["junit_arg"].format(path=str(junit_path)))
    with base_hidden():  # the suite never sees the base-commit copies or session markers, just as in CI
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, encoding=UTF8, errors="replace", env=clean_env())
    results = {}
    if junit_path.exists():
        for tc in ET.parse(junit_path).getroot().iter("testcase"):
            name = f"{tc.get('classname', '')}::{tc.get('name', '')}"
            outcome = "passed"
            for child, label in (("failure", "failed"), ("error", "error"), ("skipped", "skipped")):
                if tc.find(child) is not None:
                    outcome = label
                    break
            results[name] = outcome
    LAST_RESULTS.clear()
    LAST_RESULTS.update(results)
    return r.returncode, (r.stdout + r.stderr), results


def tail(s: str, n: int = 25) -> str:
    return "\n".join(s.strip().splitlines()[-n:])


def string_literals(lines: list[str]) -> set[str]:
    lits = set()
    for l in lines:
        for m in re.finditer(r"(['\"])((?:(?!\1).){6,})\1", l):
            lits.add(m.group(2))
    return lits


def _json_or_raw(s: str):
    try:
        return json.loads(s) if s.strip() else None
    except json.JSONDecodeError:
        return s


def verify(write: bool = True) -> bool:
    cfg = load_config()
    text = contract()
    fails, warns, info = [], [], []

    ok, msg, _ = approval_state(text)
    (info if ok else fails).append(f"Plan approval: {msg}")
    rg_ok, rg_msg, rg_state = redgate_state()
    (info if rg_ok else fails).append(f"Red gate: {rg_msg}")

    appr = latest("harness: plan-approved")
    if ok and appr:
        approved_cfg = _json_or_raw(state_file(appr[1], "config.json"))
        live_cfg = _json_or_raw(CONFIG.read_text(encoding=UTF8)) if CONFIG.exists() else None
        if approved_cfg != live_cfg:
            fails.append(f"Anti-gaming: {LIVE}/config.json changed after the plan was approved")

    if rg_ok:
        rg_sha = snap_of(rg_state)
        # 1. test integrity: red-gate tests and protected files byte-identical
        rg_blobs = {}  # path -> blob id at the red gate (-z: paths are never quoted)
        for entry in git("ls-tree", "-r", "-z", rg_sha).split("\0"):
            if entry:
                meta, _, path = entry.partition("\t")
                rg_blobs[path] = meta.split()[2]
        rg_tree = set(rg_blobs)
        guarded = [f for f in rg_tree if matches(f, cfg["test_globs"] + cfg["protected_globs"])
                   and not matches(f, cfg["always_allowed"])]
        present = sorted(f for f in guarded if (ROOT / f).is_file())
        for f in sorted(set(guarded) - set(present)):
            fails.append(f"Anti-gaming: red-gate file deleted: {f}")
        # Compare blob ids, not raw bytes: hash-object applies the same EOL/clean filters as
        # `git add`, so an unchanged file checked out with CRLF (core.autocrlf=true, the Git for
        # Windows default) still matches, while any real content change does not.
        current = hash_paths(present)
        # Without an index, hash-object can't apply git's "the committed blob already has CRLF, leave
        # it alone" rule, so such a file hashes as its LF form. A file whose raw bytes are exactly the
        # red-gate blob is unchanged, so re-check only the mismatches that hold a CR without filters.
        differ = [f for f in present if current.get(f) != rg_blobs[f]]
        crlf = [f for f in differ if b"\r" in (ROOT / f).read_bytes()]
        raw = hash_paths(crlf, "--no-filters")
        for f in differ:
            if raw.get(f) != rg_blobs[f]:
                fails.append(f"Anti-gaming: red-gate test/protected file modified: {f}")
        for f in all_files():
            if f in rg_tree:
                continue
            if matches(f, cfg["protected_globs"]):
                fails.append(f"Anti-gaming: protected file added after red gate: {f}")
            elif matches(f, cfg["test_globs"]) and not matches(f, cfg["always_allowed"]):
                warns.append(f"Test file added after red gate (not part of the gate): {f}")

        # 2. green: full suite passes and every red-gate test now passes
        rc, out, results = run_tests(cfg)
        if rc != 0:
            fails.append(f"Green gate: test suite failed (exit {rc})\n```\n{tail(out)}\n```")
        else:
            info.append(f"Green gate: test suite passed ({len(results) or '?'} tests)")
        rg_meta = json.loads(state_file(rg_state, "redgate.json") or "{}")
        if results:
            for name in rg_meta.get("tests", []):
                outcome = results.get(name)
                if outcome is None:
                    fails.append(f"Green gate: red-gate test no longer runs: {name}")
                elif outcome != "passed":
                    fails.append(f"Green gate: red-gate test is {outcome}: {name}")

        # 2b. saved probes: every sweep a reviewer saved still passes (run from the audit-trail copy)
        saved = probes()
        failing = []
        for p, prc, pout in run_probes(saved):
            if prc:
                failing.append(p["id"])
                why = "its defect class is still there" if prc == 1 else "the probe itself broke (see its output)"
                fails.append(f"Saved probe: {p['id']} (for {p['issue']}) fails with exit {prc}: {why}"
                             f"\n```\n{tail(pout, 15)}\n```")
        if saved and not failing:
            info.append(f"Saved probes: all {len(saved)} pass")
        TEL.update(probes=len(saved), probes_failing=len(failing))

        # 3. anti-gaming scan of everything changed since the plan was approved
        #    (covers stubs written before the red gate as well as the implementation)
        base = snap_of(appr[1])
        changed = [f for f in changed_files(base)
                   if matches(f, MACHINERY) or not matches(f, cfg["always_allowed"])]
        test_lits = set()
        for f in test_files(cfg):
            try:
                lines = (ROOT / f).read_text(encoding=UTF8, errors="ignore").splitlines()
            except (IsADirectoryError, FileNotFoundError):
                continue
            test_lits |= string_literals([l for l in lines if re.search(r"assert|expect", l)])
        for f in changed:
            if not (ROOT / f).is_file() or matches(f, MACHINERY):
                continue
            lines = added_lines(base, f)
            is_test = matches(f, cfg["test_globs"])
            for pat, why in FAIL_PATTERNS:
                if is_test and "runtime" in why:
                    continue
                if any(re.search(pat, l) for l in lines):
                    fails.append(f"Anti-gaming: {f} {why}")
            if is_test:
                continue
            cg_fails, cg_warns = code_gaming(f, lines, probes_saved=bool(saved),
                                             waived=matches(f, deviation_paths(text)))
            fails += cg_fails
            warns += cg_warns
            for pat, why in WARN_PATTERNS:
                if any(re.search(pat, l) for l in lines):
                    warns.append(f"Anti-gaming (review): {f} has {why}")
            for l in lines:
                if re.search(r"\b(if|elif|case|when|switch)\b|==", l):
                    hit = [x for x in test_lits if x in l]
                    if hit:
                        warns.append(f"Anti-gaming (review): {f} compares against a test literal "
                                     f"{hit[0]!r}: `{l.strip()[:90]}`")

        # 4. anti-drift: changed files vs. approved scope
        scope = scope_globs(text)
        devs = deviation_paths(text)
        for f in changed:
            if matches(f, MACHINERY):
                # Only a .claude/ entry in the approved §8.1 sanctions this; §10.3 deviations never do
                if ok and machinery_allowed(f, text):
                    warns.append(f"Anti-gaming (review): {f} changes agent permissions or agents "
                                 "(approved in §8.1; reviewer must assess)")
                else:
                    fails.append(f"Anti-gaming: agent permissions or agents modified after approval: {f} "
                                 "(only a .claude/ path listed in the approved §8.1 may change)")
                continue
            if matches(f, cfg["test_globs"] + cfg["protected_globs"]):
                continue
            if matches(f, scope):
                continue
            if f in devs or matches(f, devs):
                warns.append(f"Anti-drift: {f} is outside §8.1 scope (declared deviation; reviewer must assess)")
            else:
                fails.append(f"Anti-drift: {f} changed but is outside §8.1 scope and not declared in §10.3")

    # 5. traceability: every AC exercised by this task's tests (earlier tasks may reuse AC ids), and
    #    every rule over an input space swept by a [sweep]-tagged test
    texts = {f: (ROOT / f).read_text(encoding=UTF8, errors="ignore") for f in task_test_files(cfg)
             if (ROOT / f).is_file()}
    alltests = "\n".join(texts.values())
    missing = [a for a in acs(text) if not re.search(rf"\b{a}\b", alltests)]
    if missing:
        fails.append(f"Traceability: no test added or changed in this task references {', '.join(missing)}")
    else:
        info.append(f"Traceability: all {len(acs(text))} ACs referenced by this task's tests")
    specs = rule_specs(text)
    swept = [b for b, s in specs.items() if s["kind"] in SPACE_KINDS]
    for b in missing_sweeps(text, alltests):
        fails.append(f"Traceability: {b} [{specs[b]['kind']}] has no generated sweep test tagged `{b} [sweep]` "
                     "in this task's tests")
    if swept and not missing_sweeps(text, alltests):
        info.append(f"Traceability: {', '.join(swept)} swept by [sweep]-tagged tests")

    # 6. over-fit: a rework regression test whose inputs all come from its issue's text
    warns += overfit_warnings(issue_rows(text), texts)

    passed = not fails
    TEL.update(failures=len(fails), warnings=len(warns),
               fail_kinds=dict(collections.Counter(f.split(":", 1)[0] for f in fails)))
    report = [f"**Result: {'PASS' if passed else 'FAIL'}** — {now()} (plan {plan_hash(text)})", ""]
    report += [f"- ✓ {i}" for i in info]
    report += [f"- ✗ {f}" for f in fails]
    report += [f"- ⚠ {w}" for w in warns]
    print("\n".join(report))
    if write:
        set_block("verify", "\n".join(report))
    return passed


def baseline_json(source: str):
    passing = sorted(n for n, o in LAST_RESULTS.items() if o == "passed")
    return json.dumps({"source": source, "when": now(), "passing": passing}, indent=2) + "\n", passing


def ensure_baseline():
    """Record which tests already pass before this task changes any code (an existing codebase).
    Runs once per task, on entering Build; skipped when a baseline came with the task's base
    (the finished task it builds on) or when there are no tests yet (new development)."""
    cfg = load_config()
    if baseline_tests() is not None or not test_files(cfg):
        return
    rc, out, results = run_tests(cfg)
    if not results:
        print(f"⚠ Couldn't read per-test results from the existing suite (exit {rc}); no baseline "
              f"recorded. Existing tests that pass at the red gate will need --allow-passing.\n{tail(out, 10)}")
        return
    body, passing = baseline_json("existing tests before this task")
    record(f"harness: baseline ({len(passing)} existing tests passing)", {"baseline.json": body})
    print(f"✓ Baseline: {len(passing)} existing tests pass before this task (they must keep passing)")


# --- local ignore rules (.git/info/exclude, never committed) -------------------
def exclude_file() -> Path:
    return common_dir() / "info" / "exclude"


def ensure_excludes(extra: list[str]):
    ex = exclude_file()
    text = ex.read_text(encoding=UTF8) if ex.exists() else ""
    # The harness's own folders (the task's contract and config, and base-commit copies), plus the
    # caches its Python test runs create, so they never land in a snapshot or a commit. Local to this clone; `uninstall` removes the block.
    want = [f"/{LIVE}/", f"/{BASE_DIR}/", "__pycache__/", "*.py[cod]", ".pytest_cache/"] + extra
    m = re.search(rf"{re.escape(EXCLUDE_BEGIN)}\n(.*?){re.escape(EXCLUDE_END)}\n?", text, re.S)
    have = m.group(1).splitlines() if m else []
    lines = have + [w for w in want if w not in have]
    if m and lines == have:
        return
    blk = f"{EXCLUDE_BEGIN}\n" + "".join(l + "\n" for l in lines) + f"{EXCLUDE_END}\n"
    text = (text[:m.start()] + blk + text[m.end():]) if m else (text.rstrip("\n") + ("\n" if text.strip() else "") + blk)
    ex.parent.mkdir(parents=True, exist_ok=True)
    ex.write_text(text, encoding=UTF8)


def strip_excludes() -> bool:
    ex = exclude_file()
    if not ex.exists():
        return False
    text = ex.read_text(encoding=UTF8)
    new = re.sub(rf"{re.escape(EXCLUDE_BEGIN)}\n.*?{re.escape(EXCLUDE_END)}\n?", "", text, flags=re.S)
    if new != text:
        ex.write_text(new, encoding=UTF8)
    return new != text


# --- tasks: one worktree + branch per task -------------------------------------
def resolve_base(ref: str):
    """(sha, finished-task slug or None) for --from: a finished task (`done/<name>` or `<name>`),
    or any branch, tag or commit."""
    for slug in ([ref[5:]] if ref.startswith("done/") else []) + [ref]:
        sha = ref_sha(REF_DONE + slug)
        if sha:
            return sha, slug
    return ref_sha(ref), None


def legacy_files(root: Path) -> list[str]:
    """Light Harness 1.x files committed into the work repo (it vendored itself before 1.4)."""
    found = []
    c = root / "contract.md"
    if c.is_file() and "<!-- harness:plan -->" in c.read_text(encoding=UTF8, errors="replace"):
        found.append("contract.md")
    for f in ("scripts/harness.py", ".harness"):
        if (root / f).exists():
            found.append(f)
    cm = root / "CLAUDE.md"
    if cm.is_file() and LEGACY_MARKER in cm.read_text(encoding=UTF8, errors="replace"):
        found.append("CLAUDE.md (harness rules)")
    return found


def cmd_start_task(args):
    main_wt = main_checkout()
    if not same_path(ROOT, main_wt):
        die(f"run start-task from the main checkout ({main_wt}), not from a task worktree")
    if legacy_files(ROOT):
        die("this repo still has files from Light Harness 1.x, which copied itself into the repo: "
            f"{', '.join(legacy_files(ROOT))}. Light Harness no longer keeps anything in the work "
            "repo. A human should run `light-harness uninstall` here first; it removes those files "
            "and leaves the changes for review.")
    if not ref_sha("HEAD"):
        die("this repository has no commits yet, and a task folder is made from a commit. "
            "Make a first commit (for example `git commit --allow-empty -m \"Initial commit\"`), then retry.")
    slug = slugify(args.name)
    branch = TASK_PREFIX + slug
    if ref_sha(f"refs/heads/{branch}"):
        die(f"branch {branch} already exists; pick another task name")
    if ref_sha(REF_TASKS + slug) or ref_sha(REF_DONE + slug):
        die(f"task {slug!r} already exists or is finished (see `light-harness tasks`); pick another name")
    TEL.update(task=slug)  # only now: a refused name belongs to no task

    ref = args.from_ref or "HEAD"
    sha, from_slug = resolve_base(ref)
    if not sha:
        die(f"unknown ref {ref!r} (use a finished task's name, done/<task>, a branch or a commit)")
    TEL.update(built_on=from_slug or None)

    # Starting config: the task it builds on, else the repo's last task, else the plugin template
    cfg_text = state_file(REF_TASKS + from_slug, "config.json") if from_slug else ""
    repo_cfg = state_dir() / "config.json"
    if not cfg_text.strip() and repo_cfg.exists():
        cfg_text = repo_cfg.read_text(encoding=UTF8)
    if not cfg_text.strip():
        cfg_text = (TEMPLATE / "config.json").read_text(encoding=UTF8)
    baseline = state_file(REF_TASKS + from_slug, "baseline.json") if from_slug else ""

    wdir = Path(json.loads(cfg_text).get("worktree_dir", DEFAULT_CONFIG["worktree_dir"]).format(repo=ROOT.name))
    wroot = (wdir if wdir.is_absolute() else ROOT / wdir).resolve()
    state_dir(create=True)
    ensure_excludes([f"/{wroot.relative_to(ROOT).as_posix()}/"] if is_within(wroot, ROOT) and not same_path(wroot, ROOT) else [])

    path = wroot / slug
    if path.exists():
        die(f"{path} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "-q", "-b", branch, str(path), sha)
    (path / LIVE).mkdir(parents=True, exist_ok=True)
    tpl = (TEMPLATE / "contract.md").read_text(encoding=UTF8)
    (path / LIVE / "contract.md").write_text(tpl.replace("<task name>", args.name, 1), encoding=UTF8)
    (path / LIVE / "config.json").write_text(cfg_text, encoding=UTF8)
    set_root(path)
    record(f"harness: task-start {slug} [base:{sha[:12]}]",
           {"baseline.json": baseline} if baseline.strip() else None, slug=slug)
    print(f"✓ Task '{slug}' started\n  folder:   {path}\n  branch:   {branch} (from {ref} @ {sha[:8]})\n"
          f"  contract: {path / LIVE / 'contract.md'}\n"
          f"Run every harness command for it with --task {slug}, e.g. `light-harness status --task {slug}`")


def cmd_finish_task(args):
    name = args.name or args.task
    if name:
        slug = slugify(name)
    else:
        slug = task_slug()
    if not slug:
        die("not in a task worktree; pass the task name: finish-task <name>")
    TEL.update(task=slug)
    main_wt = main_checkout()
    branch = TASK_PREFIX + slug
    st = ref_sha(REF_TASKS + slug)
    done = ref_sha(REF_DONE + slug)
    wt = task_worktree(slug)
    if not st:
        die(f"no task {slug!r} (see `light-harness tasks`)")
    if done and not wt:
        print(f"✓ Task '{slug}' is already finished")
        return
    if wt:
        set_root(wt)
    status = get_status(CONTRACT.read_text(encoding=UTF8) if wt else state_file(st, "contract.md"))
    if status != "Done":
        die(f"task {slug!r} is at {status}; only a task with a PASS verdict (Done) can be finished")
    if wt and git("status", "--porcelain").strip():
        die(f"{wt} has uncommitted changes. Commit the task's work on {branch} first "
            f"(`git add -A && git commit -m \"<summary>\"` in the task folder).")
    tip = ref_sha(f"refs/heads/{branch}", cwd=main_wt)
    if not tip:
        die(f"branch {branch} is gone, so there is nothing to finish")
    vrow = next((r for r in (harness_log() if wt else []) if r[2].startswith("harness: verdict pass")), None)
    if wt and not vrow:
        die("no PASS verdict recorded for this task")
    if vrow:
        reviewed = git("rev-parse", f"{snap_of(vrow[1])}^{{tree}}").strip()
        if git("rev-parse", f"{tip}^{{tree}}").strip() != reviewed:
            die(f"{branch} doesn't match the work the reviewer passed (snapshot {snap_of(vrow[1])[:8]}). "
                "Commit exactly the reviewed work, or reopen the task and review it again.")
    git("update-ref", "-m", f"light-harness: task {slug} done", REF_DONE + slug, tip, cwd=main_wt)
    if wt and CONFIG.exists():  # the next task in this repo starts with this task's stack config
        (common_dir(main_wt) / STATE).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(CONFIG, common_dir(main_wt) / STATE / "config.json")
    if wt and is_within(Path.cwd(), wt):  # can't delete the folder we're running in
        print(f"✓ Task '{slug}' finished. To remove its folder, run\n"
              f"  light-harness finish-task {slug}\nfrom the main checkout ({main_wt}).")
        return
    if wt:
        git("worktree", "remove", str(wt), cwd=main_wt)
    final = branch
    if args.branch and args.branch != branch:
        git("branch", "-m", branch, args.branch, cwd=main_wt)
        final = args.branch
    base_line = git("log", "--first-parent", "--format=%s", REF_TASKS + slug, cwd=main_wt).splitlines()
    base = next((re.search(r"\[base:([0-9a-f]+)\]", s).group(1) for s in reversed(base_line)
                 if s.startswith("harness: task-start") and "[base:" in s), None)
    ahead = git("rev-list", "--count", f"{base}..{tip}", cwd=main_wt, check=False).strip() if base else "?"
    print(f"✓ Task '{slug}' finished: its folder is removed, and the reviewed work is on branch {final} "
          f"({ahead} commit(s) on top of {base[:8] if base else 'its base'}).\n"
          f"  Merge it or open a pull request from it as you would any branch; it carries no harness files.\n"
          f"  Contract and audit trail: `light-harness show {slug}` and `light-harness log {slug}` "
          f"(kept locally in refs/light-harness, never pushed by a normal push).")


def cmd_tasks(args):
    open_ = tasks_in_progress()
    print(f"In progress ({len(open_)}):")
    for slug, p in open_:
        st = get_status((p / LIVE / "contract.md").read_text(encoding=UTF8))
        print(f"  {slug:<30} {st:<10} {p}")
    legacy = [w for w in worktrees() if w.get("branch", "").startswith(f"refs/heads/{TASK_PREFIX}")
              and not is_task_checkout(Path(w["worktree"]))]
    if legacy:
        print(f"Other task/ worktrees, not run by this harness version ({len(legacy)}):")
        for w in legacy:
            print(f"  {w['branch'][len('refs/heads/'):]:<30} {w['worktree']}")
    done = git("for-each-ref", "--sort=-committerdate", "--format=%(refname)\t%(committerdate:short)",
               REF_DONE).splitlines()
    print(f"Done ({len(done)}){', newest first' if done else ''}:")
    for line in done[: args.limit]:
        ref, _, when = line.partition("\t")
        slug = ref[len(REF_DONE):]
        on = TASK_PREFIX + slug if ref_sha(f"refs/heads/{TASK_PREFIX}{slug}") else "branch renamed or removed"
        print(f"  {slug:<30} {when}  ({on})")
    if len(done) > args.limit:
        print(f"  … {len(done) - args.limit} more")


def _slug_arg(args) -> str:
    slug = slugify(args.name) if args.name else (slugify(args.task) if args.task else task_slug())
    if not slug:
        die("pass the task name")
    return slug


def cmd_show(args):
    slug = _slug_arg(args)
    wt = task_worktree(slug)
    if wt and (wt / LIVE / "contract.md").exists():
        sys.stdout.write((wt / LIVE / "contract.md").read_text(encoding=UTF8))
        return
    if not ref_sha(REF_TASKS + slug):
        die(f"no task {slug!r}")
    sys.stdout.write(state_file(REF_TASKS + slug, "contract.md"))


def cmd_log(args):
    slug = _slug_arg(args)
    if not ref_sha(REF_TASKS + slug):
        die(f"no task {slug!r}")
    out = git("log", "--first-parent", "--format=%ad%x09%p%x09%s", "--date=format:%Y-%m-%d %H:%M",
              REF_TASKS + slug)
    print("When              Code      Event")
    for line in out.splitlines():
        when, parents, subj = line.split("	", 2)
        if subj.startswith("harness:"):
            snap = parents.split()[1] if len(parents.split()) > 1 else "?"
            print(f"{when}  {snap[:8]:<8}  {subj.removeprefix('harness: ')}")


def require_human(action: str):
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        die(f"`{action}` must be run by a human in an interactive terminal, not by an agent.", 3)


def confirm(word: str, prompt: str):
    if input(f"{prompt} Type {word} to continue: ").strip() != word:
        die("cancelled")


def cmd_forget(args):
    require_human("forget")
    slug = _slug_arg(args)
    if task_worktree(slug):
        die(f"task {slug!r} is in progress; finish it first")
    refs = [r for r in (REF_TASKS + slug, REF_DONE + slug) if ref_sha(r)]
    if not refs:
        die(f"no task {slug!r}")
    confirm("FORGET", f"This deletes the contract and audit trail of task {slug!r} (its branch is kept).")
    for r in refs:
        git("update-ref", "-d", r)
    n = telemetry.drop_task(common_dir(), slug)
    print(f"✓ Forgot task '{slug}'" + (f" (and its {n} telemetry events)" if n else ""))


# --- uninstall -----------------------------------------------------------------
LEGACY_MARKER = "<!-- light-harness rules -->"
LEGACY_ENV_EXAMPLE = "# Copy to .env and fill in. Never commit .env.\n# EXAMPLE_API_KEY=\n"
LEGACY_GITIGNORE_OWN = {".harness/junit.xml", ".tasks/", "/.tasks/"}
LEGACY_PERMISSIONS = {
    "deny": ["Bash(script:*)", "Bash(git commit --amend:*)", "Bash(git rebase:*)", "Bash(git reset:*)",
             "Bash(git push --force:*)", "Bash(git filter-branch:*)", "Bash(git checkout:*)",
             "Bash(git switch:*)", "Bash(git merge:*)", "Bash(git worktree:*)", "Bash(git branch -d:*)",
             "Bash(git branch -D:*)", "Bash(git branch --delete:*)", "Bash(git tag -d:*)",
             "Bash(git tag --delete:*)", "Bash(git update-ref:*)", "Bash(rm -rf:*)", "Read(.env)",
             "Read(.env.*)"],
    "ask": ["Bash(curl:*)", "Bash(wget:*)", "Bash(ssh:*)", "Bash(scp:*)", "Bash(psql:*)",
            "Bash(mysql:*)", "Bash(git push:*)"],
    "allow": ["Bash(python3 -m pytest:*)", "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
              "Bash(git add:*)", "Bash(git commit -m:*)"],
}


def legacy_cleanup(root: Path, dry: bool) -> list[str]:
    """Undo what Light Harness 1.x's installer put into the work repo. Only the harness's own
    content is removed; anything else in a shared file (CLAUDE.md, settings.json, .gitignore) stays.
    Changes are left uncommitted for review."""
    done = []

    def rm(p: Path, why: str):
        done.append(f"delete   {p.relative_to(root).as_posix()}{why}")
        if not dry:
            shutil.rmtree(p) if p.is_dir() else p.unlink()

    c = root / "contract.md"
    if c.is_file() and "<!-- harness:plan -->" in (t := c.read_text(encoding=UTF8, errors="replace")):
        if get_status(t) in ("Draft",):
            rm(c, "")
        else:
            done.append(f"keep     contract.md (status {get_status(t)}: an unfinished in-place task; "
                        "delete it yourself when you no longer need it)")
    h = root / "scripts" / "harness.py"
    if h.is_file() and "Light harness: stage gates" in h.read_text(encoding=UTF8, errors="replace"):
        rm(h, "")
        if not dry and not any((root / "scripts").iterdir()):
            (root / "scripts").rmdir()
    hd = root / ".harness"
    if hd.is_dir():
        own = {"config.json", "redgate.json", "baseline.json", "junit.xml"}
        names = {p.name for p in hd.iterdir()}
        if names <= own:
            rm(hd, "/")
        else:
            for n in sorted(names & own):
                rm(hd / n, "")
            done.append(f"keep     .harness/ (also holds: {', '.join(sorted(names - own))})")
    env = root / ".env.example"
    env_removed = False
    if env.is_file() and env.read_text(encoding=UTF8, errors="replace").replace("\r\n", "\n") == LEGACY_ENV_EXAMPLE:
        rm(env, "")
        env_removed = True

    cm = root / "CLAUDE.md"
    if cm.is_file():
        t = cm.read_text(encoding=UTF8, errors="surrogateescape")
        i = t.find(LEGACY_MARKER)
        if i >= 0:
            rest = t[:i].rstrip()
            done.append("edit     CLAUDE.md (removed the Light Harness rules block from "
                        f"'{LEGACY_MARKER}' to the end of the file; check the diff)")
            if not dry:
                if rest:
                    cm.write_text(rest + "\n", encoding=UTF8, errors="surrogateescape")
                else:
                    cm.unlink()

    sp = root / ".claude" / "settings.json"
    if sp.is_file():
        try:
            s = json.loads(sp.read_text(encoding=UTF8))
        except json.JSONDecodeError:
            s = None
        perms = s.get("permissions") if isinstance(s, dict) else None
        if isinstance(perms, dict):
            removed = 0
            for key in ("deny", "ask", "allow"):
                cur = perms.get(key)
                if not isinstance(cur, list):
                    continue
                keep = [r for r in cur if r not in LEGACY_PERMISSIONS.get(key, []) and "scripts/harness.py" not in r]
                removed += len(cur) - len(keep)
                if keep:
                    perms[key] = keep
                else:
                    perms.pop(key)
            if removed:
                if not perms:
                    s.pop("permissions")
                done.append(f"delete   .claude/settings.json (it held only {removed} Light Harness permission rules)" if not s
                            else f"edit     .claude/settings.json (removed {removed} Light Harness permission rules)")
                if not dry:
                    if s:
                        sp.write_text(json.dumps(s, indent=2) + "\n", encoding=UTF8)
                    else:
                        sp.unlink()
                        if not any(sp.parent.iterdir()):
                            sp.parent.rmdir()

    gi = root / ".gitignore"
    if gi.is_file():
        lines = gi.read_text(encoding=UTF8).splitlines()
        own = set(LEGACY_GITIGNORE_OWN) | ({"!.env.example"} if env_removed else set())
        if any(w.get("branch", "").startswith(f"refs/heads/{TASK_PREFIX}") for w in worktrees(cwd=root)[1:]):
            own -= {".tasks/", "/.tasks/"}  # a 1.x task folder is still there; keep it out of git status
        keep = [l for l in lines if l.strip() not in own]
        if keep != lines:
            done.append(f"edit     .gitignore (removed {', '.join(sorted(own & {l.strip() for l in lines}))})")
            if not dry:
                if any(l.strip() for l in keep):
                    gi.write_text("\n".join(keep) + "\n", encoding=UTF8)
                else:
                    gi.unlink()
    tags = git("tag", "-l", "done/*", cwd=root).split()
    if tags:
        done.append(f"keep     {len(tags)} tag(s) from 1.x ({', '.join(tags[:5])}{', …' if len(tags) > 5 else ''}): "
                    "delete them with `git tag -d <tag>` once you no longer need them")
    return done


def cmd_uninstall(args):
    if not args.dry_run:
        require_human("uninstall")
    main_wt = main_checkout()
    if not same_path(ROOT, main_wt):
        die(f"run uninstall from the main checkout ({main_wt})")
    busy = tasks_in_progress()
    if busy and not args.force:
        die("tasks still in progress: " + ", ".join(f"{s} ({p})" for s, p in busy) +
            ". Finish them (`finish-task`) first, or re-run with --force to drop their folders "
            "(their branches and committed work are kept).")
    dirty = [s for s, p in busy if git("status", "--porcelain", cwd=p).strip()]
    if dirty:
        die(f"these unfinished tasks have uncommitted changes, which --force would lose: {', '.join(dirty)}. "
            "Commit or discard them first.")
    refs = git("for-each-ref", "--format=%(refname)", "refs/light-harness/").split()
    orphans = []
    for r in refs:
        if r.startswith(REF_DONE):
            sha = ref_sha(r)
            if not git("for-each-ref", "--contains", sha, "--count=1", "refs/heads/", "refs/tags/").strip():
                orphans.append(r[len(REF_DONE):])
    if orphans and not args.force:
        die("these finished tasks' work isn't on any branch or tag any more, so uninstalling would lose "
            f"it: {', '.join(orphans)}. Recover it (`git branch <name> refs/light-harness/done/<task>`) "
            "or re-run with --force.")
    plan = [f"remove   unfinished task folder {p} (branch {TASK_PREFIX}{s} is kept)" for s, p in busy]
    if refs:
        plan.append(f"delete   {len(refs)} ref(s) under refs/light-harness/ (task contracts and audit trails)")
    sd = common_dir() / STATE
    if sd.exists():
        plan.append(f"delete   {sd}")
    ex = exclude_file()
    if ex.exists() and EXCLUDE_BEGIN in ex.read_text(encoding=UTF8):
        plan.append("edit     .git/info/exclude (remove the light-harness block)")
    td = ROOT / DEFAULT_CONFIG["worktree_dir"]
    if td.is_dir() and all(any(same_path(c, p) for _, p in busy) for c in td.iterdir()):
        plan.append(f"delete   {DEFAULT_CONFIG['worktree_dir']}/")
    legacy = legacy_cleanup(ROOT, dry=True)
    legacy += [f"keep     {w['worktree']} (a Light Harness 1.x task on {w['branch'][len('refs/heads/'):]}; "
               "it carries its own copy of the 1.x harness. Finish it with that copy, then merge or delete it)"
               for w in worktrees()[1:] if w.get("branch", "").startswith(f"refs/heads/{TASK_PREFIX}")
               and not is_task_checkout(Path(w["worktree"]))]
    if not plan and not legacy:
        print("✓ Nothing to remove: Light Harness has left nothing in this repo.")
        return
    print("Light Harness uninstall" + (" (dry run, nothing changed)" if args.dry_run else "") + ":")
    for line in plan + legacy:
        print(f"  {line}")
    if args.dry_run:
        return
    confirm("UNINSTALL", "\nContracts and audit trails are deleted for good (`light-harness show <task>` "
                         "prints one if you want to keep it).")
    for _, p in busy:
        git("worktree", "remove", "--force", str(p))
    for r in refs:
        git("update-ref", "-d", r)
    if sd.exists():
        shutil.rmtree(sd)
    strip_excludes()
    if td.is_dir() and not any(td.iterdir()):
        td.rmdir()
    legacy_cleanup(ROOT, dry=False)
    print("✓ Light Harness removed." + (" Review and commit the file changes above (`git status`)."
                                         if any(not l.startswith("keep") for l in legacy) else ""))


# --- commands -----------------------------------------------------------------
def cmd_status(args):
    if not CONTRACT.exists():
        open_ = tasks_in_progress()
        print(f"Checkout:  {ROOT} (main checkout, no task here)")
        print("Tasks:     " + (", ".join(f"{s} ({get_status((p / LIVE / 'contract.md').read_text(encoding=UTF8))})"
                                         for s, p in open_) or "none in progress"))
        print("Next:      " + ("pass --task <name>" if open_ else "/light-harness:task-init <name>"))
        return
    text = contract()
    st = get_status(text)
    ok, msg, _ = approval_state(text)
    rg_ok, rg_msg, _ = redgate_state()
    nxt = {"Draft": "/task-init", "Initialize": "/task-plan",
           "Plan": "human approval, then /task-implement" if not ok else "/task-implement",
           "Build": "finish build and `verify`, then /task-review",
           "Review": "reviewer records verdict", "Rework": "/task-implement (fix review issues)",
           "Done": "commit the reviewed work, then finish-task (keeps it on its branch)"}[st]
    slug = task_slug()
    base = task_base_row()
    print(f"Task:      {slug} (folder {ROOT}, branch {TASK_PREFIX}{slug})")
    if base:
        print(f"Base:      {snap_of(base[1])[:8]} {base[2].removeprefix('harness: ')}")
    print(f"Status:    {st}\nPlan hash: {plan_hash(text)}\nApproval:  {'✓ ' if ok else '✗ '}{msg}\n"
          f"Red gate:  {'✓ ' if rg_ok else '✗ '}{rg_msg}\nACs:       {', '.join(acs(text)) or 'none'}\n"
          f"Contract:  {CONTRACT}\nNext:      {nxt}")


def cmd_check(args):
    text = contract()
    probs = check_plan(text) if args.stage == "plan" else check_init(text)
    hints = rule_hints(text)
    TEL.update(problems=len(probs), hints=len(hints))
    if probs:
        TEL["reason"] = probs[0][:200]
        print("\n".join([f"✗ {p}" for p in probs] + [f"⚠ {h}" for h in hints]))
        sys.exit(1)
    print("\n".join([f"✓ contract satisfies the {args.stage} gate"] + [f"⚠ {h}" for h in hints]))


def cmd_set_status(args):
    target = args.status
    text = contract()
    cur = get_status(text)
    if target in ("Done", "Rework"):
        die("Done/Rework are set only by `verdict`")
    if target not in TRANSITIONS.get(cur, set()):
        die(f"illegal transition {cur} → {target}. Allowed: {sorted(TRANSITIONS.get(cur, set())) or 'verdict only'}")
    if target == "Initialize":
        probs = check_init(text)
    elif target == "Plan":
        probs = check_plan(text)
    elif target == "Build":
        ok, msg, _ = approval_state(text)
        probs = [] if ok else [msg]
        if ok and cur == "Plan":
            ensure_baseline()
    elif target == "Review":
        probs = [] if verify() else ["verification failed (see report above)"]
    else:
        probs = []
    TEL.update(**{"from": cur}, problems=len(probs))
    if probs:
        TEL["reason"] = probs[0][:200]
        die("gate not satisfied:\n  - " + "\n  - ".join(probs))
    write_status(target, args.note or "")
    if target == "Review":
        # the issues table as the implementer hands it back (issues set to Fixed), so a reviewer's
        # reopening of a Fixed issue is visible to `metrics` (same class reopened)
        record("harness: review-requested")
    print(f"✓ Status: {cur} → {target}")


def cmd_approve(args):
    require_human("approve")
    text = contract()
    if get_status(text) != "Plan":
        die(f"status is {get_status(text)}; approval happens in Plan")
    probs = check_plan(text)
    if probs:
        die("plan is incomplete:\n  - " + "\n  - ".join(probs))
    h = plan_hash(text)
    print(f"Plan hash {h}\nScope: {', '.join(scope_globs(text))}\n"
          f"Steps: {len(list_items('steps', text))}  Risks: {len(table_rows('risks', text))}  "
          f"ACs: {', '.join(acs(text))}\n")
    mach = approved_machinery(text)
    if mach:
        print("⚠ This plan lets the task change agent permissions or agents (Claude Code settings, agents,\n"
              "  commands or skills). Approve only if that is the point of the task:\n"
              + "".join(f"    {g}" + (f"  (covers {', '.join(machinery_kinds(g))})" if machinery_kinds(g) else "")
                        + "\n" for g in mach))
    if input(f'Type APPROVE to approve this plan as "{args.name}": ').strip() != "APPROVE":
        die("not approved")
    set_block("approval", f"**Approved by:** {args.name}  \n**When:** {now()}  \n**Plan hash:** `{h}`\n\n"
                          f"_Editing §1–§8 after this point invalidates the approval._")
    write_status("Plan", f"approved by {args.name}")
    record(f"harness: plan-approved by {args.name} [plan:{h}]")
    TEL.update(wait_s=plan_wait())
    print(f"✓ Plan approved by {args.name}. Next: /task-implement")


def cmd_unlock_tests(args):
    require_human("unlock-tests")
    ok, msg, _ = redgate_state()
    if not ok:
        die(f"nothing to unlock: {msg}")
    note = block("redgate").rstrip()
    set_block("redgate", note + f"\n\n**Tests unlocked** by {args.name} at {now()}: {args.reason}")
    record(f"harness: tests-unlocked by {args.name}: {args.reason}")
    TEL.update(unlock_reason=args.reason[:200])  # the reason only, never the --name
    print("✓ Tests unlocked. The test-author may revise tests; the red gate must be passed again.")


def cmd_redgate(args):
    cfg = load_config()
    text = contract()
    if get_status(text) != "Build":
        die(f"status is {get_status(text)}; the red gate runs in Build")
    ok, msg, _ = approval_state(text)
    if not ok:
        die(msg)
    if redgate_state()[0]:
        die("red gate already passed for this plan. A human must run `unlock-tests` to redo it.")
    tests = test_files(cfg)
    if not tests:
        die(f"no test files found (see test_globs in {LIVE}/config.json)")
    base = task_base_row()
    mine = task_test_files(cfg, base)
    if not mine:
        die("no test files were added or changed in this task; write this task's tests first")
    alltests = "\n".join((ROOT / f).read_text(encoding=UTF8, errors="ignore")
                         for f in mine if (ROOT / f).is_file())
    missing = [a for a in acs(text) if not re.search(rf"\b{a}\b", alltests)]
    if missing:
        die(f"this task's tests don't reference: {', '.join(missing)} (each AC needs at least one test)")
    specs = rule_specs(text)
    unswept = missing_sweeps(text, alltests)
    if unswept:
        die("these rules hold for every input, but this task's tests have no generated sweep for them: "
            + ", ".join(f"{b} [{specs[b]['kind']}]" for b in unswept)
            + ". Write the sweep from the §8.4 entry and tag it with `BR-n [sweep]` in its docstring or title.")
    preserve = [b for b, s in specs.items() if s["kind"] == "Preservation"]
    rc, out, results = run_tests(cfg)
    if rc == 0:
        die("tests PASS before implementation — they don't prove anything yet. "
            "The test-author must write tests that fail against the stubs.")
    if rc not in cfg["red_exit_codes"]:
        die(f"test run exited {rc} (collection/usage error, not test failures). Make sure stubs "
            f"exist so tests import and fail on assertions.\n{tail(out)}")
    # Tests that passed before this task (earlier tasks, existing code) are expected to pass;
    # only this task's new tests must fail against the stubs.
    existing = (baseline_tests() or set()) & set(results)
    passing = sorted(n for n, o in results.items() if o == "passed" and n not in existing)
    # A Preservation sweep compares the new code with the base outcomes, so it passes on stubs that keep
    # the base code. It is recognised by its tag (`BR-n [sweep]` for a Preservation rule, on that test in
    # that module) and its own name (`…sweep…`); listed for the reviewer instead of refused.
    names = sweep_test_names({f: (ROOT / f).read_text(encoding=UTF8, errors="ignore") for f in mine
                              if (ROOT / f).is_file()}, preserve) if preserve else set()
    sweeps = [n for n in passing if is_tagged_sweep(n, names)]
    passing = [n for n in passing if n not in sweeps]
    broken = sorted(n for n in existing if results[n] != "passed")
    skipped = sorted(n for n, o in results.items() if o == "skipped")
    errors = sorted(n for n, o in results.items() if o == "error")
    TEL.update(new_tests=len(set(results) - existing), passing_new=len(passing), passing_sweeps=len(sweeps),
               skipped=len(skipped), errored=len(errors), existing_broken=len(broken))
    if skipped:
        die("skipped tests at red gate: " + ", ".join(skipped))
    if passing and not args.allow_passing:
        die("these new tests already pass against the stubs (they may be vacuous):\n  " + "\n  ".join(passing) +
            "\nFix them, or re-run with --allow-passing \"<reason>\" (recorded in the contract)."
            + ("\nA Preservation sweep that passes on stubs keeping the base code is accepted without it when "
               "the test's own name contains `sweep` and its docstring or title holds `BR-n [sweep]` for a "
               "[Preservation] rule." if preserve else ""))
    meta = {"tests": sorted(results), "existing": sorted(existing), "passing_at_red": passing,
            "passing_sweeps": sweeps, "allow_passing": args.allow_passing, "base": snap_of(base[1]) if base else None,
            "plan_hash": plan_hash(text), "when": now()}
    new_count = len(results) - len(existing)
    lines = [f"**Passed** {now()} — {new_count if results else '?'} new tests, all failing against stubs"
             + (f" apart from {len(sweeps)} Preservation sweep(s)." if sweeps else "."),
             f"- Test files locked: {', '.join(tests)}",
             f"- ACs covered: {', '.join(acs(text))}"]
    if existing:
        lines.append(f"- Existing tests (passed before this task; must keep passing): {len(existing)}")
    if broken:
        lines.append(f"- ⚠ Existing tests failing against the stubs (must pass again at verify): {', '.join(broken)}")
    if errors:
        lines.append(f"- ⚠ {len(errors)} tests errored (not failed) at red gate: {', '.join(errors)}")
    if sweeps:
        lines.append(f"- Preservation sweeps passing on stubs that keep the base code (expected; "
                     f"{', '.join(preserve)}): {', '.join(sweeps)}")
    if passing:
        lines.append(f"- ⚠ Allowed passing at red gate ({args.allow_passing}): {', '.join(passing)}")
    set_block("redgate", "\n".join(lines))
    record(f"harness: red-gate [plan:{plan_hash(text)}]", {"redgate.json": json.dumps(meta, indent=2) + "\n"})
    print("✓ Red gate passed; tests are locked. Implement until green, then run `verify`.")


def cmd_verify(args):
    st = get_status()
    if st not in ("Build", "Review"):
        die(f"status is {st}; verify runs in Build/Review")
    sys.exit(0 if verify() else 1)


def cmd_verdict(args):
    text = contract()
    if get_status(text) != "Review":
        die(f"status is {get_status(text)}; verdicts are recorded in Review")
    rows = table_rows("issues", text)
    unresolved, blocking, illegal = [], [], []
    for r in rows:
        r += [""] * (6 - len(r))
        iid, sev, st = r[0], r[1].lower(), r[2].lower()
        if sev not in ("critical", "high", "medium", "low"):
            illegal.append(f"{iid}: unknown severity '{r[1]}'")
        if st not in ("open", "fixed", "resolved", "won't fix", "wont fix"):
            illegal.append(f"{iid}: unknown status '{r[2]}'")
        if st.startswith("won") and sev != "low":
            illegal.append(f"{iid}: Won't fix is only allowed for Low severity")
        if st != "resolved" and not st.startswith("won"):
            unresolved.append(iid)
            if sev in BLOCKING_SEVERITIES:
                blocking.append(f"{iid} ({r[1]}, {r[2]})")
        elif st.startswith("won") and sev in BLOCKING_SEVERITIES:
            blocking.append(f"{iid} ({r[1]}, {r[2]})")
    TEL.update(issues=dict(collections.Counter(r[1].strip().lower() or "?" for r in rows)),
               unresolved=len(unresolved), blocking=len(blocking),
               same_class_reopened=same_class_reopened(issue_rounds(task_slug()) + [issue_rows(text)]))
    if illegal:
        die("issues table invalid:\n  - " + "\n  - ".join(illegal))

    extra = None
    if args.result == "pass":
        problems = []
        if blocking:
            problems.append("unresolved Critical/High/Medium issues: " + ", ".join(blocking))
        checked = set(re.findall(r"-\s*\[[xX]\]\s*(AC-\d+)", block("ac-check", text)))
        unchecked = [a for a in acs(text) if a not in checked]
        if unchecked:
            problems.append("ACs not ticked in §11.1: " + ", ".join(unchecked))
        if problems:
            die("PASS refused:\n  - " + "\n  - ".join(problems))
        if not verify():
            die("PASS refused: verification fails on the current tree")
        # Everything passing now is the baseline for the next task built on this one
        extra = {"baseline.json": baseline_json(f"verdict pass ({task_slug()})")[0]}
    elif not unresolved:
        die("FAIL needs at least one unresolved issue in §11.2 so the implementer knows what to fix")

    new = "Done" if args.result == "pass" else "Rework"
    open_low = [i for i in unresolved if i not in " ".join(blocking)]
    body = (f"**Verdict: {args.result.upper()}** by {args.by} — {now()}  \n"
            f"Issues: {len(rows)} logged, {len(unresolved)} unresolved"
            + (f" (blocking: {', '.join(blocking)})" if blocking else "")
            + (f"; open Low: {', '.join(open_low)}" if open_low and args.result == "pass" else ""))
    set_block("verdict", body)
    write_status(new, f"verdict {args.result} by {args.by}")
    earlier = trail()
    record(f"harness: verdict {args.result} by {args.by}", extra)
    rounds = sum(s.startswith("harness: verdict") for _, s in earlier)
    TEL.update(review_round=rounds + 1)
    if args.result == "pass":  # the task is done: its lead time and first-pass result, for OTel dashboards
        start = next((t for t, s in earlier if s.startswith("harness: task-start")), earlier[0][0] if earlier else None)
        TEL.update(lead_time_s=round(time.time() - start, 3) if start else None, first_pass=rounds == 0)
    print(f"✓ Verdict {args.result.upper()} → Status {new}"
          + (". Next: commit the reviewed work on the task branch, then `finish-task`." if new == "Done"
             else ". Next: /task-implement to fix the issues, then /task-review."))


def trail(slug: str | None = None) -> list:
    """A task's audit trail as [(epoch, subject)], oldest first (harness events only)."""
    rows = git("log", "--first-parent", "--reverse", "--format=%ct%x09%s", state_ref(slug), check=False).splitlines()
    return [(int(t), s) for t, _, s in (r.partition("\t") for r in rows) if t.isdigit() and s.startswith("harness:")]


def plan_wait():
    """Seconds since this task's plan was put up for approval (its last successful
    `set-status Plan`), or None if the event log doesn't say."""
    try:
        slug = task_slug()
        asked = [e["_t"] for e in telemetry.read_events(common_dir()) if e["event"] == "command"
                 and e.get("command") == "set-status" and e.get("target") == "Plan" and e.get("ok")
                 and e.get("task") == slug]
    except (OSError, ValueError, SystemExit):
        return None
    return round(time.time() - asked[-1], 3) if asked else None


def cmd_metrics(args):
    common = common_dir()
    trails = {}
    for ref in git("for-each-ref", "--format=%(refname)", REF_TASKS).split():
        trails[ref[len(REF_TASKS):]] = trail(ref[len(REF_TASKS):])
    finished = {r[len(REF_DONE):] for r in git("for-each-ref", "--format=%(refname)", REF_DONE).split()}
    name = args.name or args.task
    slug = slugify(name) if name else None
    if slug and slug not in trails:
        die(f"no task {slug!r} (see `light-harness tasks`)")
    data = telemetry.kpis(trails, finished, telemetry.read_events(common),
                          issue_rounds={s: issue_rounds(s) for s in trails})
    if args.json:
        print(json.dumps(data["tasks"][slug] if slug else data, indent=2, default=str))
    else:
        print(telemetry.render(data, slug))


def cmd_telemetry(args):
    cwd = main_checkout()
    if args.action == "status":
        common = common_dir()
        on = telemetry.enabled(cwd)
        log = telemetry.log_path(common)
        nick, scope = telemetry.engineer(cwd), telemetry.engineer_scope(cwd)
        url = telemetry.otlp_endpoint(cwd)
        n = len(log.read_text(encoding=UTF8).splitlines()) if log.is_file() else 0
        print(f"Telemetry: {'on' if on else 'off (LIGHT_HARNESS_TELEMETRY or git config lightharness.telemetry)'}\n"
              f"Event log: {log} ({n} events; local, inside .git, never committed or pushed)\n"
              f"Export:    {url or 'none (set LIGHT_HARNESS_OTLP_ENDPOINT to send events to an OTLP collector)'}\n"
              f"Engineer:  " + (f"opted in as {nick!r} ({scope})" if nick else
                                "not opted in: events carry no engineer identity"))
        return
    require_human(f"telemetry {args.action}")
    if args.action == "opt-in":
        if not args.nickname:
            die("pick the name your events carry: telemetry opt-in --as <nickname>")
        err = telemetry.set_engineer(cwd, args.nickname.strip(), args.repo)
        if err:
            die(err)
        real = git("config", "user.name", check=False, cwd=cwd).strip()
        print(f"✓ Opted in as {args.nickname.strip()!r} for {'this repo' if args.repo else 'all your repos'}. "
              "Your harness events now carry this nickname and nothing else about you.")
        if real and real.casefold() == args.nickname.strip().casefold():
            print("  Note: this is your git user name. Pick a nickname instead if you'd rather stay pseudonymous.")
        print("  Opt out at any time with `telemetry opt-out`" + (" --repo" if args.repo else "") + ".")
    else:
        err = telemetry.set_engineer(cwd, None, args.repo)
        if err:
            die(err)
        print(f"✓ Opted out ({'this repo' if args.repo else 'all your repos'}). New events carry no engineer identity.")
        if telemetry.engineer(cwd):
            print(f"  Still opted in as {telemetry.engineer(cwd)!r} through {telemetry.engineer_scope(cwd)}; "
                  "run opt-out " + ("without --repo" if args.repo else "with --repo") + " too.")


# Commands whose outcome is a telemetry event: gates, stage moves and task lifecycle
TRACKED = {"check", "set-status", "approve", "unlock-tests", "redgate", "verify", "verdict",
           "start-task", "finish-task", "telemetry", "probe"}
HUMAN_ONLY = {"approve", "unlock-tests", "telemetry"}


def run_tracked(args):
    """Run a command and record its outcome (ok or refused, why, how long) as a `command` event."""
    common, slug, t0, code = quiet_common(), quiet_slug(), time.monotonic(), 0
    try:
        args.f(args)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        raise
    except BaseException:
        code = 1
        raise
    finally:
        # read-only views and diagnostics aren't gate outcomes (a failing `probe run` isn't a refusal)
        if not (args.cmd == "telemetry" and args.action == "status") and \
                not (args.cmd == "probe" and args.action in ("list", "run")):
            target = {"set-status": "status", "check": "stage", "verdict": "result",
                      "telemetry": "action", "probe": "action"}.get(args.cmd)
            attrs = {k: v for k, v in TEL.items() if k not in ("task", "action")}
            attrs.update(ok=code == 0, exit=code, ms=int((time.monotonic() - t0) * 1000))
            if code == 0:
                attrs.pop("reason", None)
            human = args.cmd in HUMAN_ONLY or (args.cmd == "probe" and args.action == "drop")
            emit("command", common, TEL.get("task") or slug, command=args.cmd,
                 target=getattr(args, target) if target else None, actor="human" if human else None, **attrs)


def main():
    ap = argparse.ArgumentParser(prog="light-harness", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    task_opt = argparse.ArgumentParser(add_help=False)
    task_opt.add_argument("--task", metavar="NAME", help="act on this task's folder (run from the main checkout)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    _add = sub.add_parser
    sub.add_parser = lambda *a, **k: _add(*a, parents=[task_opt], **k)
    s = sub.add_parser("status", help="show stage, gates and next step"); s.set_defaults(f=cmd_status)
    s = sub.add_parser("check", help="check the contract against a gate")
    s.add_argument("stage", choices=["init", "plan"]); s.set_defaults(f=cmd_check)
    s = sub.add_parser("set-status", help="move to a stage (gates enforced)")
    s.add_argument("status", choices=["Initialize", "Plan", "Build", "Review", "Done", "Rework"])
    s.add_argument("--note"); s.set_defaults(f=cmd_set_status)
    s = sub.add_parser("approve", help="HUMAN ONLY: approve the plan")
    s.add_argument("--name", required=True); s.set_defaults(f=cmd_approve)
    s = sub.add_parser("unlock-tests", help="HUMAN ONLY: unlock red-gate tests for revision")
    s.add_argument("--name", required=True); s.add_argument("--reason", required=True); s.set_defaults(f=cmd_unlock_tests)
    s = sub.add_parser("redgate", help="run the red gate and lock the tests")
    s.add_argument("--allow-passing", metavar="REASON", default=""); s.set_defaults(f=cmd_redgate)
    s = sub.add_parser("verify", help="green gate + anti-gaming + anti-drift + traceability"); s.set_defaults(f=cmd_verify)
    s = sub.add_parser("verdict", help="record the review verdict (pass is gated)")
    s.add_argument("result", choices=["pass", "fail"]); s.add_argument("--by", default="reviewer")
    s.set_defaults(f=cmd_verdict)
    s = sub.add_parser("start-task", help="start a task in its own worktree and branch (run in the main checkout)")
    s.add_argument("name"); s.add_argument("--from", dest="from_ref", metavar="REF",
                                           help="build on a finished task (its name or done/<name>), a branch or a commit")
    s.set_defaults(f=cmd_start_task)
    s = sub.add_parser("finish-task", help="finish a Done task: remove its folder, keep its work on its branch")
    s.add_argument("name", nargs="?")
    s.add_argument("--branch", metavar="NAME", help="rename the task branch, e.g. feature/invoice-dedupe")
    s.set_defaults(f=cmd_finish_task)
    s = sub.add_parser("tasks", help="list tasks in progress and finished")
    s.add_argument("--limit", type=int, default=20); s.set_defaults(f=cmd_tasks)
    s = sub.add_parser("show", help="print a task's contract (in progress or finished)")
    s.add_argument("name", nargs="?"); s.set_defaults(f=cmd_show)
    s = sub.add_parser("log", help="print a task's audit trail")
    s.add_argument("name", nargs="?"); s.set_defaults(f=cmd_log)
    s = sub.add_parser("forget", help="HUMAN ONLY: delete a finished task's contract and audit trail")
    s.add_argument("name"); s.set_defaults(f=cmd_forget)
    s = sub.add_parser("uninstall", help="HUMAN ONLY: remove every trace of Light Harness from this repo "
                                         "(including files 1.x copied into it)")
    s.add_argument("--dry-run", action="store_true", help="only list what would be removed")
    s.add_argument("--force", action="store_true", help="also drop unfinished task folders (their branches are "
                                                       "kept), and finished tasks whose work is on no branch")
    s.set_defaults(f=cmd_uninstall)
    s = sub.add_parser("metrics", help="KPIs for tasks, agents and opted-in engineers")
    s.add_argument("name", nargs="?", help="one task (default: the repo summary and every task)")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(f=cmd_metrics)
    s = sub.add_parser("telemetry", help="telemetry status; engineer opt-in/opt-out (HUMAN ONLY)")
    s.add_argument("action", choices=["status", "opt-in", "opt-out"])
    s.add_argument("--as", dest="nickname", metavar="NICKNAME",
                   help="opt-in: the pseudonym your events carry (any nickname you like)")
    s.add_argument("--repo", action="store_true", help="this repo only (default: all your repos)")
    s.set_defaults(f=cmd_telemetry)
    s = sub.add_parser("baseline", help="write base-commit copies of files (with their local imports) to "
                                        f"{BASE_DIR}/ in the task folder, to run old and new side by side")
    s.add_argument("paths", nargs="+", metavar="PATH", help="files, folders or globs, relative to the task folder")
    s.set_defaults(f=cmd_baseline)
    s = sub.add_parser("probe", help="saved probes: add (a sweep that found a defect), list, run; "
                                     "drop is HUMAN ONLY")
    s.add_argument("action", choices=["add", "list", "run", "drop"])
    s.add_argument("target", nargs="?", metavar="SCRIPT_OR_ID", help="add: the probe script; drop: the probe id")
    s.add_argument("--issue", metavar="RV-n", help="add: the §11.2 issue whose defect class the probe checks")
    s.add_argument("--run", metavar="CMD", help="add: how to run it, e.g. \"deno run {file} {base}\" ({base} is the base copies' folder; default by extension: "
                                                ".py, .sh, .js/.mjs/.cjs)")
    s.add_argument("--name", help="drop: your full name"); s.add_argument("--reason", help="drop: why")
    s.set_defaults(f=cmd_probe)
    s = sub.add_parser("version", help="print the harness version")
    s.set_defaults(f=lambda a: print(__version__))
    args = ap.parse_args()
    if args.cmd != "version" and not _toplevel(ROOT):
        die(f"{ROOT} isn't inside a git repository. Light Harness runs tasks in git worktrees; "
            "run `git init` and make a first commit, then retry.")
    if args.task and args.cmd not in ("start-task", "finish-task", "tasks", "show", "log", "forget", "uninstall",
                                      "metrics", "telemetry"):
        wt = task_worktree(slugify(args.task))
        if not wt:
            die(f"no task {args.task!r} in progress (see `light-harness tasks`)")
        set_root(wt)
    if args.cmd in TRACKED:
        run_tracked(args)
    else:
        args.f(args)


if __name__ == "__main__":
    main()
