#!/usr/bin/env python3
"""Light harness: stage gates for contract-driven agentic development.

Stages: Draft -> Initialize -> Plan -> (human approval) -> Build -> Review -> Done | Rework

State lives in two places, both reviewable:
  * contract.md  - Status line and harness-owned blocks
  * git history  - commits whose subject starts with "harness:" (approval, red gate,
                   test unlocks, verdicts). Gates are recomputed from these, not from
                   a side file, so they can't be satisfied by editing a JSON blob.

Run `python3 scripts/harness.py --help` for commands.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# All harness text is UTF-8 (contract, config, tool output). Never rely on the locale default:
# on Windows that is cp1252, which can't encode ✓/✗/→ and misreads UTF-8 files.
UTF8 = "utf-8"
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding=UTF8, errors="replace")


def _find_root() -> Path:
    """Task repo root: $HARNESS_ROOT, else $CLAUDE_PROJECT_DIR, else nearest dir with contract.md
    or .git above the cwd. Works when the script is vendored in the repo or shipped in a plugin."""
    import os
    for var in ("HARNESS_ROOT", "CLAUDE_PROJECT_DIR"):
        if os.environ.get(var):
            return Path(os.environ[var]).resolve()
    here = Path.cwd().resolve()
    for d in (here, *here.parents):
        if (d / "contract.md").exists() or (d / ".git").exists():
            return d
    return here


ROOT = _find_root()
CONTRACT = ROOT / "contract.md"
CONFIG = ROOT / ".harness" / "config.json"
JUNIT = ROOT / ".harness" / "junit.xml"
REDGATE_FILE = ".harness/redgate.json"

TRANSITIONS = {
    "Draft": {"Initialize"},
    "Initialize": {"Initialize", "Plan"},
    "Plan": {"Plan", "Initialize", "Build"},
    "Build": {"Build", "Plan", "Review"},
    "Rework": {"Build", "Plan"},
    "Review": set(),          # leaves Review only through `verdict`
    "Done": {"Initialize"},   # reopen with new scope
}
MACHINERY = ["scripts/harness.py", "scripts/hook_guard.py", ".claude/settings.json",
             ".claude/agents/**", ".claude/commands/**", ".harness/config.json"]
OWNED_BLOCKS = ["approval", "redgate", "verify", "verdict", "status-log"]
BLOCKING_SEVERITIES = {"critical", "high", "medium"}

DEFAULT_CONFIG = {
    "test_cmd": ["python3", "-m", "pytest", "-q"],
    "junit_arg": "--junitxml={path}",
    "red_exit_codes": [1],
    "test_globs": ["tests/**", "test_*.py", "*_test.py", "*.test.*", "*.spec.*", "__tests__/**"],
    "protected_globs": ["conftest.py", "pytest.ini", "tox.ini", "setup.cfg",
                        "jest.config.*", "vitest.config.*", "samples/**"],
    "always_allowed": ["contract.md", ".harness/**", ".gitignore", "README.md"],
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


# --- small utilities ----------------------------------------------------------
def now() -> str:
    return dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")


def die(msg: str, code: int = 1):
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
def git(*args: str, check: bool = True, binary: bool = False):
    text_kw = {} if binary else {"encoding": UTF8, "errors": "replace"}
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, **text_kw)
    if check and r.returncode != 0:
        die(f"git {' '.join(args)} failed: {r.stderr}")
    return r.stdout


def ensure_repo():
    if not (ROOT / ".git").exists():
        git("init", "-q")


def commit(msg: str):
    ident = []
    if not git("config", "user.name", check=False).strip():
        ident = ["-c", "user.name=light-harness", "-c", "user.email=harness@localhost"]
    git("add", "-A")
    subprocess.run(["git", *ident, "commit", "-q", "--allow-empty", "--no-verify", "-m", msg],
                   cwd=ROOT, check=True)


def harness_log() -> list[tuple[int, str, str]]:
    """(index, sha, subject) for harness commits, newest first (index 0 = newest commit)."""
    if not (ROOT / ".git").exists():
        return []
    out = git("log", "--format=%H%x09%s", check=False)
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
        die("contract.md not found. Run `python3 scripts/harness.py init`.")
    return CONTRACT.read_text(encoding=UTF8)


def block_span(text: str, name: str):
    m = re.search(rf"<!-- harness:{re.escape(name)} -->\n?(.*?)<!-- /harness:{re.escape(name)} -->",
                  text, re.S)
    return m


def block(name: str, text: str | None = None) -> str:
    m = block_span(text if text is not None else contract(), name)
    return m.group(1) if m else ""


def set_block(name: str, body: str):
    text = contract()
    m = block_span(text, name)
    if not m:
        die(f"contract.md is missing the harness:{name} block")
    new = text[:m.start(1)] + body.rstrip() + "\n" + text[m.end(1):]
    CONTRACT.write_text(new, encoding=UTF8)


def get_status(text: str | None = None) -> str:
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
    return [i.strip("` ").split(" ")[0] for i in list_items("scope", text)]


def deviation_paths(text: str | None = None) -> list[str]:
    return [re.split(r"\s+[—–-]+\s+|\s*:\s+", i.strip("` "), maxsplit=1)[0].strip("` ")
            for i in list_items("deviations", text)]


# --- gate checks --------------------------------------------------------------
def check_init(text: str) -> list[str]:
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
    return p


def approval_state(text: str | None = None):
    """Return (ok, message, log_index)."""
    row = latest("harness: plan-approved")
    if not row:
        return False, "plan has not been approved by a human", None
    m = re.search(r"\[plan:([0-9a-f]+)\]", row[2])
    current = plan_hash(text)
    if not m or m.group(1) != current:
        return False, (f"plan changed after approval (approved {m.group(1) if m else '?'}, "
                       f"current {current}); re-approval required"), row[0]
    return True, row[2].removeprefix("harness: "), row[0]


def redgate_state():
    """Return (ok, message, sha)."""
    ok, msg, appr_idx = approval_state()
    rg = latest("harness: red-gate")
    if not rg:
        return False, "red gate has not been passed", None
    if appr_idx is None or rg[0] > appr_idx:
        return False, "red gate predates the current plan approval; run it again", None
    unlock = latest("harness: tests-unlocked")
    if unlock and unlock[0] < rg[0]:
        return False, "tests were unlocked after the red gate; re-run the red gate", None
    return True, f"red gate at {rg[1][:8]}", rg[1]


def test_files(cfg) -> list[str]:
    return [f for f in all_files() if matches(f, cfg["test_globs"]) and not matches(f, cfg["always_allowed"])]


def run_tests(cfg):
    cmd = list(cfg["test_cmd"])
    if cmd and cmd[0] in ("python", "python3", "py"):
        cmd[0] = sys.executable  # same interpreter as the harness (portable across OSes/venvs)
    junit_path = ROOT / cfg["junit_path"] if cfg.get("junit_path") else JUNIT
    junit_path.unlink(missing_ok=True)
    if cfg.get("junit_arg"):
        cmd.append(cfg["junit_arg"].format(path=str(junit_path)))
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, encoding=UTF8, errors="replace")
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
    return r.returncode, (r.stdout + r.stderr), results


def tail(s: str, n: int = 25) -> str:
    return "\n".join(s.strip().splitlines()[-n:])


def string_literals(lines: list[str]) -> set[str]:
    lits = set()
    for l in lines:
        for m in re.finditer(r"(['\"])((?:(?!\1).){6,})\1", l):
            lits.add(m.group(2))
    return lits


def verify(write: bool = True) -> bool:
    cfg = load_config()
    text = contract()
    fails, warns, info = [], [], []

    ok, msg, _ = approval_state(text)
    (info if ok else fails).append(f"Plan approval: {msg}")
    rg_ok, rg_msg, rg_sha = redgate_state()
    (info if rg_ok else fails).append(f"Red gate: {rg_msg}")

    if rg_ok:
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
        current = git("hash-object", "--", *present).split() if present else []
        for f, blob in zip(present, current):
            if blob != rg_blobs[f]:
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
        rg_meta = json.loads(git("show", f"{rg_sha}:{REDGATE_FILE}", check=False) or "{}")
        if results:
            for name in rg_meta.get("tests", []):
                outcome = results.get(name)
                if outcome is None:
                    fails.append(f"Green gate: red-gate test no longer runs: {name}")
                elif outcome != "passed":
                    fails.append(f"Green gate: red-gate test is {outcome}: {name}")

        # 3. anti-gaming scan of everything changed since the plan was approved
        #    (covers stubs written before the red gate as well as the implementation)
        base = latest("harness: plan-approved")[1]
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
                fails.append(f"Anti-gaming: harness machinery modified after approval: {f}")
                continue
            if matches(f, cfg["test_globs"] + cfg["protected_globs"]):
                continue
            if matches(f, scope):
                continue
            if f in devs or matches(f, devs):
                warns.append(f"Anti-drift: {f} is outside §8.1 scope (declared deviation; reviewer must assess)")
            else:
                fails.append(f"Anti-drift: {f} changed but is outside §8.1 scope and not declared in §10.3")

    # 5. traceability: every AC still exercised by tests
    alltests = "\n".join((ROOT / f).read_text(encoding=UTF8, errors="ignore") for f in test_files(cfg)
                         if (ROOT / f).is_file())
    missing = [a for a in acs(text) if not re.search(rf"\b{a}\b", alltests)]
    if missing:
        fails.append(f"Traceability: no test references {', '.join(missing)}")
    else:
        info.append(f"Traceability: all {len(acs(text))} ACs referenced by tests")

    passed = not fails
    report = [f"**Result: {'PASS' if passed else 'FAIL'}** — {now()} (plan {plan_hash(text)})", ""]
    report += [f"- ✓ {i}" for i in info]
    report += [f"- ✗ {f}" for f in fails]
    report += [f"- ⚠ {w}" for w in warns]
    print("\n".join(report))
    if write:
        set_block("verify", "\n".join(report))
    return passed


# --- commands -----------------------------------------------------------------
def cmd_init(args):
    ensure_repo()
    if not CONTRACT.exists():
        die("contract.md template missing; copy it from the harness template")
    t = contract()
    if args.name and "<task name>" in t:
        CONTRACT.write_text(t.replace("<task name>", args.name, 1), encoding=UTF8)
    print(f"✓ Harness ready. Status: {get_status()}. Next: /task-init")


def cmd_status(args):
    text = contract()
    st = get_status(text)
    ok, msg, _ = approval_state(text)
    rg_ok, rg_msg, _ = redgate_state()
    nxt = {"Draft": "/task-init", "Initialize": "/task-plan",
           "Plan": "human approval, then /task-implement" if not ok else "/task-implement",
           "Build": "finish build and `verify`, then /task-review",
           "Review": "reviewer records verdict", "Rework": "/task-implement (fix review issues)",
           "Done": "close out (archive or promote)"}[st]
    print(f"Status:    {st}\nPlan hash: {plan_hash(text)}\nApproval:  {'✓ ' if ok else '✗ '}{msg}\n"
          f"Red gate:  {'✓ ' if rg_ok else '✗ '}{rg_msg}\nACs:       {', '.join(acs(text)) or 'none'}\n"
          f"Next:      {nxt}")


def cmd_check(args):
    text = contract()
    probs = check_plan(text) if args.stage == "plan" else check_init(text)
    if probs:
        print("\n".join(f"✗ {p}" for p in probs))
        sys.exit(1)
    print(f"✓ contract satisfies the {args.stage} gate")


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
    elif target == "Review":
        probs = [] if verify() else ["verification failed (see report above)"]
    else:
        probs = []
    if probs:
        die("gate not satisfied:\n  - " + "\n  - ".join(probs))
    write_status(target, args.note or "")
    print(f"✓ Status: {cur} → {target}")


def require_human(action: str):
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        die(f"`{action}` must be run by a human in an interactive terminal, not by an agent.", 3)


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
    if input(f'Type APPROVE to approve this plan as "{args.name}": ').strip() != "APPROVE":
        die("not approved")
    set_block("approval", f"**Approved by:** {args.name}  \n**When:** {now()}  \n**Plan hash:** `{h}`\n\n"
                          f"_Editing §1–§8 after this point invalidates the approval._")
    write_status("Plan", f"approved by {args.name}")
    commit(f"harness: plan-approved by {args.name} [plan:{h}]")
    print(f"✓ Plan approved by {args.name}. Next: /task-implement")


def cmd_unlock_tests(args):
    require_human("unlock-tests")
    ok, msg, _ = redgate_state()
    if not ok:
        die(f"nothing to unlock: {msg}")
    note = block("redgate").rstrip()
    set_block("redgate", note + f"\n\n**Tests unlocked** by {args.name} at {now()}: {args.reason}")
    commit(f"harness: tests-unlocked by {args.name}: {args.reason}")
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
        die("no test files found (see test_globs in .harness/config.json)")
    alltests = "\n".join((ROOT / f).read_text(encoding=UTF8, errors="ignore")
                         for f in tests if (ROOT / f).is_file())
    missing = [a for a in acs(text) if not re.search(rf"\b{a}\b", alltests)]
    if missing:
        die(f"tests don't reference: {', '.join(missing)} (each AC needs at least one test)")
    rc, out, results = run_tests(cfg)
    if rc == 0:
        die("tests PASS before implementation — they don't prove anything yet. "
            "The test-author must write tests that fail against the stubs.")
    if rc not in cfg["red_exit_codes"]:
        die(f"test run exited {rc} (collection/usage error, not test failures). Make sure stubs "
            f"exist so tests import and fail on assertions.\n{tail(out)}")
    passing = sorted(n for n, o in results.items() if o == "passed")
    skipped = sorted(n for n, o in results.items() if o == "skipped")
    errors = sorted(n for n, o in results.items() if o == "error")
    if skipped:
        die("skipped tests at red gate: " + ", ".join(skipped))
    if passing and not args.allow_passing:
        die("these tests already pass against the stubs (they may be vacuous):\n  " + "\n  ".join(passing) +
            "\nFix them, or re-run with --allow-passing \"<reason>\" (recorded in the contract).")
    meta = {"tests": sorted(results), "passing_at_red": passing, "allow_passing": args.allow_passing,
            "plan_hash": plan_hash(text), "when": now()}
    (ROOT / ".harness").mkdir(exist_ok=True)
    (ROOT / REDGATE_FILE).write_text(json.dumps(meta, indent=2), encoding=UTF8)
    lines = [f"**Passed** {now()} — {len(results) or '?'} tests, all failing against stubs.",
             f"- Test files locked: {', '.join(tests)}",
             f"- ACs covered: {', '.join(acs(text))}"]
    if errors:
        lines.append(f"- ⚠ {len(errors)} tests errored (not failed) at red gate: {', '.join(errors)}")
    if passing:
        lines.append(f"- ⚠ Allowed passing at red gate ({args.allow_passing}): {', '.join(passing)}")
    set_block("redgate", "\n".join(lines))
    commit(f"harness: red-gate [plan:{plan_hash(text)}]")
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
    if illegal:
        die("issues table invalid:\n  - " + "\n  - ".join(illegal))

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
    commit(f"harness: verdict {args.result} by {args.by}")
    print(f"✓ Verdict {args.result.upper()} → Status {new}"
          + ("" if new == "Done" else ". Next: /task-implement to fix the issues, then /task-review."))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init", help="initialise git + contract"); s.add_argument("--name"); s.set_defaults(f=cmd_init)
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
    args = ap.parse_args()
    args.f(args)


if __name__ == "__main__":
    main()
