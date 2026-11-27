# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""Issue #50: the `amend-config` classifier, `classify_config_change(approved, plan_approved, live)`.
AC-2 (BR-4 [sweep]), AC-2a (BR-4.1 position bypasses) and AC-3 (BR-4.2 `parallel`, BR-4.3 timeouts).

The classifier is called in-process. Its answer is compared with an independent oracle written here from the
text of BR-4 (no harness helper is used apart from DEFAULT_CONFIG, which BR-4 names). A refused field must be
named in one of the `refused` entries, and every refused entry must be about (name first) a field the oracle refuses;
an accepted test_cmd change must be named in one of the `changes`.

Sweep choices (spec ambiguities kept out of the generated space, see the report): only one concurrency unit
per list; no case where a key is added or removed with a value equal to its default (other than test_cmd and
the timeouts, whose BR-4 text fixes the default handling); no int/bool/float values that compare equal in
Python (1 vs True, 600 vs 600.0) on both sides.
"""
from __future__ import annotations

import copy
import random
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "plugins" / "light-harness" / "scripts"
for p in (str(SCRIPTS), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import harness as H  # noqa: E402

CONC = "{concurrency}"
DEFAULT_TEST_CMD = list(H.DEFAULT_CONFIG["test_cmd"])
TIMEOUT_DEFAULTS = {"suite_timeout": 3600, "probe_timeout": 600}
CAP = 3
MISSING = object()

# --- the oracle (BR-4) ---------------------------------------------------------------------------
_POS = re.compile(r"[1-9][0-9]*")
_NONNEG = re.compile(r"0|[1-9][0-9]*")
_PCT = re.compile(r"[1-9][0-9]*%")


def _value_ok(family: str, v: str) -> bool:
    if v == CONC:
        return True
    if family == "node":
        return bool(_POS.fullmatch(v))
    if family == "pytest":
        return bool(_NONNEG.fullmatch(v)) or v in ("auto", "logical")
    return bool(_POS.fullmatch(v)) or bool(_PCT.fullmatch(v))          # jest / vitest


def _basename(item: str) -> str:
    return re.split(r"[\\/]", item)[-1]


def _runner(tc: list):
    """(family, marker index) per BR-4.1, or (None, None)."""
    if tc and _basename(tc[0]) in ("node", "node.exe"):
        # node is the runner only with `--test` before its first non-option item; `node <script> ...` (even a
        # jest or vitest script) is not a recognised runner: nothing is removable (decision on review)
        first_non_option = next((i for i in range(1, len(tc)) if not tc[i].startswith("-")), len(tc))
        if "--test" in tc[1:first_non_option]:
            return "node", tc.index("--test", 1)
        return None, None
    for i, item in enumerate(tc):
        if _basename(item) in ("pytest", "py.test", "pytest.exe"):
            return "pytest", i
        if item == "-m" and i + 1 < len(tc) and tc[i + 1] == "pytest":
            return "pytest", i + 1
    for i, item in enumerate(tc):
        b = _basename(item)
        for ext in (".js", ".cmd", ".mjs"):
            if b.endswith(ext):
                b = b[: -len(ext)]
                break
        if b in ("jest", "vitest"):
            return "jest", i
    return None, None


def _unit_at(family: str, tc: list, i: int) -> int:
    """Length of a removable concurrency unit starting at tc[i] (0: none)."""
    if i >= len(tc):
        return 0
    t = tc[i]
    nxt = tc[i + 1] if i + 1 < len(tc) else None
    if family == "node":
        return 1 if t.startswith("--test-concurrency=") and _value_ok("node", t.split("=", 1)[1]) else 0
    if family == "pytest":
        if t in ("-n", "--numprocesses"):
            return 2 if nxt is not None and _value_ok("pytest", nxt) else 0
        if t.startswith("--numprocesses="):
            return 1 if _value_ok("pytest", t.split("=", 1)[1]) else 0
        if t.startswith("-n="):
            return 1 if _value_ok("pytest", t[3:]) else 0
        if t.startswith("-n") and len(t) > 2:
            return 1 if _value_ok("pytest", t[2:]) else 0
        return 0
    if t == "--maxWorkers":
        return 2 if nxt is not None and _value_ok("jest", nxt) else 0
    if t.startswith("--maxWorkers="):
        return 1 if _value_ok("jest", t.split("=", 1)[1]) else 0
    return 0


def strip_concurrency(tc: list) -> list:
    """tc without its runner's removable concurrency arguments (BR-4.1 with the position rule)."""
    family, m = _runner(tc)
    if family is None:
        return list(tc)
    drop = set()
    i = m + 1
    while True:                                   # immediately after the runner marker, one after another
        n = _unit_at(family, tc, i)
        if not n:
            break
        drop.update(range(i, i + n))
        i += n
    if family != "node" and "--" not in tc:       # or the last token / pair of a list without `--`
        if len(tc) >= 2 and tc[-2] in ("-n", "--numprocesses", "--maxWorkers") and _unit_at(family, tc, len(tc) - 2) == 2:
            drop.update((len(tc) - 2, len(tc) - 1))
        elif _unit_at(family, tc, len(tc) - 1) == 1:
            drop.add(len(tc) - 1)
    return [t for k, t in enumerate(tc) if k not in drop]


def _pos_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _eff_timeout(cfg: dict, key: str) -> int:
    v = cfg.get(key, TIMEOUT_DEFAULTS[key])
    return v if _pos_int(v) else TIMEOUT_DEFAULTS[key]


def oracle(approved: dict, plan_approved: dict, live: dict) -> dict:
    """{"ok", "refused": set of field names, "changed": set of field names, "raised": set of field names}."""
    refused, changed, raised = set(), set(), set()
    for key in sorted(set(approved) | set(live)):
        a, l = approved.get(key, MISSING), live.get(key, MISSING)
        if key == "test_cmd":
            a = DEFAULT_TEST_CMD if a is MISSING else a
            l = DEFAULT_TEST_CMD if l is MISSING else l
            if a == l:
                continue
            (changed if strip_concurrency(a) == strip_concurrency(l) else refused).add(key)
            continue
        if a is l or (a is not MISSING and l is not MISSING and type(a) is type(l) and a == l):
            continue
        if key == "parallel":
            if l is not MISSING and not (isinstance(l, int) and not isinstance(l, bool) and 1 <= l <= 4):
                refused.add(key)
            else:
                changed.add(key)
        elif key in TIMEOUT_DEFAULTS:
            if l is not MISSING and not _pos_int(l):
                refused.add(key)
                continue
            eff = TIMEOUT_DEFAULTS[key] if l is MISSING else l
            p = _eff_timeout(plan_approved, key)
            if eff > CAP * p:
                refused.add(key)
            else:
                changed.add(key)
                if eff > p:
                    raised.add(key)
        else:
            refused.add(key)
    return {"ok": not refused, "refused": refused, "changed": changed, "raised": raised}


def mismatch(approved: dict, plan_approved: dict, live: dict):
    """None when the classifier agrees with the oracle, else a description of the disagreement."""
    want = oracle(approved, plan_approved, live)
    got = H.classify_config_change(copy.deepcopy(approved), copy.deepcopy(plan_approved), copy.deepcopy(live))
    assert isinstance(got, dict) and {"ok", "changes", "raised", "refused"} <= set(got), got
    why = []
    if bool(got["ok"]) != want["ok"]:
        why.append(f"ok={got['ok']}, oracle says {want['ok']} (oracle refuses {sorted(want['refused'])})")
    refused_text = " ".join(map(str, got["refused"]))
    changes_text = " ".join(map(str, got["changes"]))
    if want["ok"]:
        if got["refused"]:
            why.append(f"refused entries on an allowed change: {got['refused']}")
        if not got["changes"]:
            why.append("no change listed")
        for f in want["changed"]:
            if f not in changes_text:
                why.append(f"change of {f} not named in changes {got['changes']}")
    else:
        if not got["refused"]:
            why.append("no refused entry")
        for f in want["refused"]:
            if f not in refused_text:
                why.append(f"refused field {f} not named in {got['refused']}")
        # each refused entry is about the first config key it names (explanations may mention other keys
        # later, e.g. "only concurrency, parallel and timeouts can be amended"): that key must be refused
        keys = set(H.DEFAULT_CONFIG) | set(approved) | set(live) | {"junit_path"}
        for entry in map(str, got["refused"]):
            hits = [(m.start(), f) for f in keys
                    for m in [re.search(rf"(?<![\w-]){re.escape(f)}(?![\w-])", entry)] if m]
            if hits and min(hits)[1] not in want["refused"]:
                why.append(f"refused entry {entry!r} is about {min(hits)[1]}, which the oracle doesn't refuse")
    if not why:
        return None
    return f"approved={approved!r}\n  live={live!r}\n  got={got!r}\n  " + "; ".join(why)


# --- the generated space ---------------------------------------------------------------------------
# (id, base test_cmd, family). The family's flag forms, in-set and out-of-set values are below.
BASES = [
    ("node", ["node", "--test"], "node"),
    ("node-args", ["node", "--test", "--test-reporter=dot", "test/"], "node"),
    ("python-m-pytest", ["python3", "-m", "pytest", "-q"], "pytest"),
    ("pytest", ["pytest"], "pytest"),
    ("pytest-path", ["python", "-m", "pytest", "-q", "tests/"], "pytest"),
    ("pytest-dashdash", ["python3", "-m", "pytest", "-q", "--", "tests/"], "pytest"),
    ("py.test", ["py.test", "-x"], "pytest"),
    ("npx-jest", ["npx", "jest"], "jest"),
    ("jest-args", ["npx", "jest", "--ci", "src/"], "jest"),
    ("bin-jest", ["./node_modules/.bin/jest"], "jest"),
    ("vitest-run", ["npx", "vitest", "run"], "jest"),
    ("vitest", ["vitest"], "jest"),
    ("node-jest-script", ["node", "node_modules/jest/bin/jest.js"], None),
    ("non-runner", ["python3", "run_toy.py"], None),
    ("npm-test", ["npm", "test"], None),
]
FORMS = {   # each form makes the unit from a value
    "node": [lambda v: [f"--test-concurrency={v}"]],
    "pytest": [lambda v: ["-n", v], lambda v: [f"-n{v}"], lambda v: [f"-n={v}"],
               lambda v: ["--numprocesses", v], lambda v: [f"--numprocesses={v}"]],
    "jest": [lambda v: ["--maxWorkers", v], lambda v: [f"--maxWorkers={v}"]],
}
VALUES = {
    "node": ["3", CONC, "0", "foo"],
    "pytest": ["2", "auto", "logical", CONC, "0", "tests/x.py"],
    "jest": ["2", "50%", CONC, "0", "tests/x.js"],
}
FOREIGN = {"node": [["-n", "2"], ["--maxWorkers=2"]], "pytest": [["--maxWorkers=2"], ["--test-concurrency=2"]],
           "jest": [["-n", "2"], ["--test-concurrency=2"], ["-w", "2"]]}
POSITIONS = ("after", "mid", "last", "before")


def _insert_at(base: list, family, pos: str):
    """Insertion index for a position, or None when the position doesn't exist for this base."""
    fam, m = _runner(base)
    if fam is None:
        return {"after": 1, "last": len(base), "mid": None, "before": None}[pos]
    if pos == "after":
        return m + 1
    if pos == "last":
        return len(base)
    if pos == "mid":
        return m + 2 if m + 2 < len(base) else None
    return m if m >= 1 else None


def _units(family):
    if family is None:
        out = []
        for fam in ("node", "pytest", "jest"):
            out += [f("2") for f in FORMS[fam]] + [f(CONC) for f in FORMS[fam][:1]]
        return out
    return [f(v) for f in FORMS[family] for v in VALUES[family]] + FOREIGN[family]


def variants(base: list, family) -> list:
    """base with one concurrency unit at each position (deduplicated), base itself excluded."""
    seen, out = {tuple(base)}, []
    for unit in _units(family):
        for pos in POSITIONS:
            at = _insert_at(base, family, pos)
            if at is None:
                continue
            v = base[:at] + unit + base[at:]
            if tuple(v) not in seen:
                seen.add(tuple(v))
                out.append(v)
    return out


def pairs_for(base: list, family) -> list:
    """(approved test_cmd, live test_cmd): every add and remove, plus a seeded sample of changes."""
    vs = variants(base, family)
    rnd = random.Random(f"issue50-{' '.join(base)}")
    out = [(base, v) for v in vs] + [(v, base) for v in vs]
    for v in vs:
        out.append((v, rnd.choice(vs)))
    return [(a, l) for a, l in out if a != l]


def _cfg(test_cmd=MISSING, **extra) -> dict:
    cfg = copy.deepcopy(H.DEFAULT_CONFIG)
    if test_cmd is MISSING:
        cfg.pop("test_cmd")
    else:
        cfg["test_cmd"] = list(test_cmd)
    cfg.update(extra)
    return cfg


def test_ac2_oracle_self_check():
    """AC-2 BR-4: the oracle itself on the plan's own examples (guards the sweep against a wrong oracle)."""
    assert strip_concurrency(["node", "--test", "--test-concurrency=2"]) == ["node", "--test"]
    assert strip_concurrency(["python3", "-m", "pytest", "-q", "-n", CONC]) == ["python3", "-m", "pytest", "-q"]
    assert strip_concurrency(["npx", "jest", "--maxWorkers=50%"]) == ["npx", "jest"]
    assert strip_concurrency(["npx", "--maxWorkers=2", "jest"]) == ["npx", "--maxWorkers=2", "jest"]
    assert strip_concurrency(["pytest", "-q", "--", "x", "-n", "2"]) == ["pytest", "-q", "--", "x", "-n", "2"]
    assert strip_concurrency(["pytest", "-n", "tests/x.py"]) == ["pytest", "-n", "tests/x.py"]
    assert strip_concurrency(["node", "run.mjs", "--test", "--test-concurrency=1"]) == \
        ["node", "run.mjs", "--test", "--test-concurrency=1"]
    assert oracle(_cfg(), _cfg(), _cfg(probe_timeout=1800))["ok"]
    assert not oracle(_cfg(), _cfg(), _cfg(probe_timeout=1801))["ok"]


@pytest.mark.parametrize("bid,base,family", BASES, ids=[b[0] for b in BASES])
def test_ac2_sweep_test_cmd_concurrency_pairs(bid, base, family):
    """AC-2 BR-4 [sweep]: for each runner base (node --test, python -m pytest, pytest, py.test, npx jest, a jest
    binary, vitest, a non-runner, npm test), every concurrency unit (each flag form of the runner and of other
    runners, in-set and out-of-set values) at each position (right after the runner marker, after a further
    argument, last, before the runner token) is added, removed and changed; the classifier accepts exactly the
    pairs the BR-4 oracle accepts and names `test_cmd` when refusing."""
    bad = []
    n = 0
    for a, l in pairs_for(base, family):
        for appr, live in ((_cfg(a), _cfg(l)),):
            n += 1
            m = mismatch(appr, appr, live)
            if m:
                bad.append(m)
    assert n > 0
    assert not bad, f"{len(bad)} of {n} cases disagree with the oracle, e.g.:\n" + "\n".join(bad[:8])


def test_ac2_sweep_both_outcomes_generated():
    """AC-2 BR-4 [sweep]: the generated space holds both accepted and refused test_cmd changes for every
    runner family (the sweep is not vacuous), and stays bounded."""
    total, seen = 0, {}
    for bid, base, family in BASES:
        for a, l in pairs_for(base, family):
            total += 1
            ok = oracle(_cfg(a), _cfg(a), _cfg(l))["ok"]
            seen.setdefault(family, set()).add(ok)
    assert seen["node"] == {True, False} and seen["pytest"] == {True, False} and seen["jest"] == {True, False}
    assert seen[None] == {False}
    assert 300 < total < 4000, total


def test_ac2_missing_test_cmd_means_default():
    """AC-2 BR-4: a missing test_cmd key is DEFAULT_CONFIG["test_cmd"] (`python3 -m pytest -q`): adding the key
    with `-n {concurrency}` appended (or removing it from there) is a concurrency change; any other test_cmd
    when the key was missing is refused."""
    with_n = DEFAULT_TEST_CMD + ["-n", CONC]
    cases = [(_cfg(), _cfg(with_n)), (_cfg(with_n), _cfg()),
             (_cfg(), _cfg(DEFAULT_TEST_CMD + ["-k", "foo"])), (_cfg(), _cfg(["node", "--test"]))]
    assert [oracle(a, a, l)["ok"] for a, l in cases] == [True, True, False, False]
    bad = [m for appr, live in cases if (m := mismatch(appr, appr, live))]
    assert not bad, "\n".join(bad)


# --- other keys ------------------------------------------------------------------------------------
KEY_MUTATIONS = [
    ("test_globs", lambda c: c.__setitem__("test_globs", c["test_globs"] + ["spec/**"])),
    ("test_globs-replaced", lambda c: c.__setitem__("test_globs", ["tests/**"])),
    ("protected_globs", lambda c: c.__setitem__("protected_globs", c["protected_globs"][:-1])),
    ("red_exit_codes", lambda c: c.__setitem__("red_exit_codes", [1, 2])),
    ("junit_arg", lambda c: c.__setitem__("junit_arg", "--junit={path}")),
    ("junit_path", lambda c: c.__setitem__("junit_path", "out/junit.xml")),
    ("always_allowed", lambda c: c.__setitem__("always_allowed", c["always_allowed"] + ["src/**"])),
    ("worktree_dir", lambda c: c.__setitem__("worktree_dir", ".wt")),
    ("unknown-key", lambda c: c.__setitem__("retries", 2)),
]
TEST_CMD_MUTATIONS = [   # applied to test_cmd: refused, naming test_cmd
    ("runner-swapped", lambda t: ["nodejs"] + t[1:] if t[0] == "node" else ["python"] + t[1:]),
    ("reporter", lambda t: t + ["--reporter=dot"]),
    ("filter-k", lambda t: t + ["-k", "fast"]),
    ("filter-grep", lambda t: t + ["--grep", "fast"]),
    ("filter-name-pattern", lambda t: t + ["--test-name-pattern", "fast"]),
    ("file-path", lambda t: t + ["tests/test_a.py"]),
    ("option-dropped", lambda t: t[:-1] if len(t) > 2 else t + ["-x"]),
]
# Approved test_cmds the key mutations are applied on, and an allowed concurrency change for each.
KEY_BASES = [
    (["node", "--test"], ["node", "--test", "--test-concurrency=" + CONC]),
    (["python3", "-m", "pytest", "-q"], ["python3", "-m", "pytest", "-q", "-n", CONC]),
    (["npx", "jest", "--ci"], ["npx", "jest", "--maxWorkers=" + CONC, "--ci"]),
    (["npx", "vitest", "run"], ["npx", "vitest", "--maxWorkers", "2", "run"]),
    (["python3", "run_toy.py"], ["python3", "run_toy.py"]),
]


@pytest.mark.parametrize("mid,mutate", KEY_MUTATIONS, ids=[m[0] for m in KEY_MUTATIONS])
def test_ac2_sweep_other_key_mutations_refused(mid, mutate):
    """AC-2 BR-4 [sweep]: a change to any other key (test_globs, protected_globs, red_exit_codes, junit_arg,
    junit_path, always_allowed, worktree_dir, an unknown key), alone or together with an allowed concurrency,
    `parallel` or timeout change, refuses the whole amendment and names the key."""
    bad, n = [], 0
    for appr_tc, conc_tc in KEY_BASES:
        appr = _cfg(appr_tc)
        for extra in ({}, {"test_cmd": conc_tc}, {"parallel": 2}, {"probe_timeout": 300}):
            live = copy.deepcopy(appr)
            live.update(copy.deepcopy(extra))
            mutate(live)
            n += 1
            assert not oracle(appr, appr, live)["ok"]
            m = mismatch(appr, appr, live)
            if m:
                bad.append(m)
    assert not bad, f"{len(bad)} of {n} disagree:\n" + "\n".join(bad[:6])


@pytest.mark.parametrize("mid,mutate", TEST_CMD_MUTATIONS, ids=[m[0] for m in TEST_CMD_MUTATIONS])
def test_ac2_sweep_test_cmd_mutations_refused(mid, mutate):
    """AC-2 BR-4 [sweep]: test_cmd[0], reporters, filters (`-k`, `--grep`, `--test-name-pattern`), file paths
    and dropped options are not concurrency arguments: refused (alone or next to an allowed concurrency
    change), naming test_cmd."""
    bad, n = [], 0
    for appr_tc, conc_tc in KEY_BASES:
        appr = _cfg(appr_tc)
        for start in (appr_tc, conc_tc):
            live = _cfg(mutate(list(start)))
            if oracle(appr, appr, live)["ok"]:
                continue                       # (a mutation that happens to be a concurrency unit)
            n += 1
            m = mismatch(appr, appr, live)
            if m:
                bad.append(m)
    assert n >= len(KEY_BASES), n
    assert not bad, f"{len(bad)} of {n} disagree:\n" + "\n".join(bad[:6])


def test_ac2_combined_allowed_changes():
    """AC-2 BR-4: a concurrency change, a `parallel` change and a lowered timeout together are accepted, each
    one listed in `changes`."""
    appr = _cfg(["node", "--test"])
    live = _cfg(["node", "--test", "--test-concurrency=" + CONC], parallel=2, suite_timeout=1800)
    got = H.classify_config_change(appr, appr, live)
    assert got["ok"] is True and got["refused"] == [], got
    text = " ".join(got["changes"])
    for f in ("test_cmd", "parallel", "suite_timeout"):
        assert f in text, (f, got)
    assert "--test-concurrency={concurrency}" in text, got
    assert got["raised"] == [], got


# --- AC-2a: position bypasses -----------------------------------------------------------------------
BYPASSES = [
    ("between-option-and-value", ["node", "--test", "--test-name-pattern", "foo"],
     ["node", "--test", "--test-name-pattern", "--test-concurrency=2", "foo"]),
    ("after-dashdash-last", ["python3", "-m", "pytest", "-q", "--", "tests/"],
     ["python3", "-m", "pytest", "-q", "--", "tests/", "-n", "2"]),
    ("right-after-dashdash", ["python3", "-m", "pytest", "-q", "--", "tests/"],
     ["python3", "-m", "pytest", "-q", "--", "-n", "2", "tests/"]),
    ("jest-after-dashdash", ["npx", "jest", "--", "src/"], ["npx", "jest", "--", "src/", "--maxWorkers=2"]),
    ("before-runner-token", ["npx", "jest"], ["npx", "--maxWorkers=2", "jest"]),
    ("before-pytest-token", ["python3", "-m", "pytest", "-q"], ["python3", "-n", "2", "-m", "pytest", "-q"]),
    ("after-node-script", ["node", "run.mjs", "--test"], ["node", "run.mjs", "--test", "--test-concurrency=1"]),
    ("node-last-after-path", ["node", "--test", "test/"], ["node", "--test", "test/", "--test-concurrency=2"]),
    ("value-outside-set", ["pytest", "-q"], ["pytest", "-n", "tests/x.py", "-q"]),
    ("value-outside-set-last", ["pytest", "-q"], ["pytest", "-q", "-n", "tests/x.py"]),
    ("pytest-wedged-in-k", ["pytest", "-k", "foo"], ["pytest", "-k", "-n", "2", "foo"]),
    ("node-flag-on-pytest", ["pytest", "-q"], ["pytest", "--test-concurrency=2", "-q"]),
    ("pytest-flag-on-node", ["node", "--test"], ["node", "--test", "-n", "2"]),
    ("jest-short-w", ["npx", "jest"], ["npx", "jest", "-w", "2"]),
    ("npm-wrapper", ["npm", "test"], ["npm", "test", "--", "--maxWorkers=2"]),
    ("non-runner", ["python3", "run_toy.py"], ["python3", "run_toy.py", "-n", "2"]),
    ("node-zero", ["node", "--test"], ["node", "--test", "--test-concurrency=0"]),
    ("jest-zero", ["npx", "jest"], ["npx", "jest", "--maxWorkers=0"]),
    ("swap-value-for-path", ["pytest", "-n", "2", "tests/a.py"], ["pytest", "-n", "tests/a.py", "2"]),
    ("node-jest-script", ["node", "node_modules/jest/bin/jest.js"],
     ["node", "node_modules/jest/bin/jest.js", "--maxWorkers=2"]),
    ("node-jest-script-mid", ["node", "node_modules/jest/bin/jest.js", "--ci"],
     ["node", "node_modules/jest/bin/jest.js", "--maxWorkers=2", "--ci"]),
]


@pytest.mark.parametrize("bid,appr_tc,live_tc", BYPASSES, ids=[b[0] for b in BYPASSES])
def test_ac2a_position_bypasses_refused(bid, appr_tc, live_tc):
    """AC-2a BR-4.1: a concurrency-looking token wedged between an option and its value, after `--`, before
    the runner token, after a node script path, with a value outside V, or a flag of another runner (or
    jest's `-w`, or behind an npm wrapper) is not removable: the amendment is refused, naming test_cmd."""
    appr, live = _cfg(appr_tc), _cfg(live_tc)
    assert not oracle(appr, appr, live)["ok"], "oracle accepts a listed bypass"
    got = H.classify_config_change(appr, appr, live)
    assert got["ok"] is False, got
    assert any("test_cmd" in str(r) for r in got["refused"]), got


# --- AC-3: timeouts and parallel ----------------------------------------------------------------------
TIMEOUT_CASES = [   # (id, key, plan-approved value or MISSING, approved value or MISSING, live value or MISSING, ok, raised)
    ("probe-lower", "probe_timeout", 600, 600, 300, True, False),
    ("probe-lower-to-1", "probe_timeout", 600, 600, 1, True, False),
    ("suite-lower", "suite_timeout", 3600, 3600, 60, True, False),
    ("probe-raise-2x", "probe_timeout", 600, 600, 1200, True, True),
    ("probe-raise-3x", "probe_timeout", 600, 600, 1800, True, True),
    ("probe-raise-3x+1", "probe_timeout", 600, 600, 1801, False, False),
    ("suite-raise-3x", "suite_timeout", 3600, 3600, 10800, True, True),
    ("suite-raise-3x+1", "suite_timeout", 3600, 3600, 10801, False, False),
    ("probe-missing-default", "probe_timeout", MISSING, MISSING, 1800, True, True),
    ("probe-missing-default+1", "probe_timeout", MISSING, MISSING, 1801, False, False),
    ("suite-missing-default", "suite_timeout", MISSING, MISSING, 10800, True, True),
    ("suite-removed-to-default-3x", "suite_timeout", 1200, 1200, MISSING, True, True),
    ("suite-removed-to-default-over", "suite_timeout", 1000, 1000, MISSING, False, False),
    ("probe-removed-same-default", "probe_timeout", 600, 600, MISSING, True, False),
    ("chained-over-cap", "probe_timeout", 600, 1800, 2400, False, False),
    ("chained-lower-still-raised", "probe_timeout", 600, 1800, 1500, True, True),
    ("chained-back-to-plan", "probe_timeout", 600, 1800, 600, True, False),
    ("chained-suite-over-cap", "suite_timeout", 900, 2700, 2701, False, False),
    ("added-key-plan-small", "probe_timeout", 100, 100, 301, False, False),
]


@pytest.mark.parametrize("cid,key,plan_v,appr_v,live_v,ok,raised", TIMEOUT_CASES, ids=[c[0] for c in TIMEOUT_CASES])
def test_ac3_timeouts(cid, key, plan_v, appr_v, live_v, ok, raised):
    """AC-3 BR-4.3: lowering a timeout is accepted by any amount; raising is accepted up to 3x the
    plan-approved effective value (a missing key is the default 3600 / 600) and refused above it with
    `re-approval` in the refusal; the cap is measured against the plan-approved snapshot, not the last
    amendment, so chained amendments can't walk past it; a raised timeout is listed in `raised`."""
    def build(v):
        c = _cfg(["node", "--test"])
        c.pop(key, None)
        if v is not MISSING:
            c[key] = v
        return c

    plan, appr, live = build(plan_v), build(appr_v), build(live_v)
    want = oracle(appr, plan, live)
    assert want["ok"] is ok and (key in want["raised"]) is raised, "case table disagrees with the oracle"
    got = H.classify_config_change(appr, plan, live)
    assert got["ok"] is ok, got
    if ok:
        assert got["refused"] == [] and any(key in c for c in got["changes"]), got
        assert any(key in r for r in got["raised"]) is raised, got
    else:
        text = " ".join(got["refused"])
        assert key in text and "re-approval" in text, got


def test_ac3_raised_entry_names_values():
    """AC-3 BR-5 BR-9: a raised timeout is reported as `<key> <plan-approved> → <new>`."""
    appr = _cfg(["node", "--test"])
    got = H.classify_config_change(appr, appr, dict(appr, probe_timeout=1200))
    assert got["ok"] is True, got
    assert any("probe_timeout" in r and "600" in r and "1200" in r for r in got["raised"]), got
    assert any("probe_timeout" in c and "600" in c and "1200" in c for c in got["changes"]), got


@pytest.mark.parametrize("key", ["probe_timeout", "suite_timeout"])
@pytest.mark.parametrize("value", [0, -5, True, False, "600", 600.5, 1200.0, None, [600]],
                         ids=["zero", "negative", "true", "false", "string", "float", "whole-float", "null", "list"])
def test_ac3_invalid_timeout_values_refused(key, value):
    """AC-3 BR-4.3: a live timeout that is present must be a positive int (not bool): 0, negative, bool,
    string, float, null and list are refused, naming the key."""
    appr = _cfg(["node", "--test"])
    live = dict(appr, **{key: value})
    assert not oracle(appr, appr, live)["ok"]
    got = H.classify_config_change(appr, appr, live)
    assert got["ok"] is False and any(key in r for r in got["refused"]), got


@pytest.mark.parametrize("live_v,ok", [(1, True), (2, True), (4, True), (MISSING, True), (0, False), (5, False),
                                       (True, False), ("2", False), (2.0, False), (-1, False), (None, False)],
                         ids=["1", "2", "4", "removed", "0", "5", "bool", "string", "float", "negative", "null"])
def test_ac3_parallel(live_v, ok):
    """AC-3 BR-4.2: `parallel` may be changed or removed in any direction when the live value is an int 1..4
    (not bool); 0, 5, bool, string, float, negative and null are refused, naming `parallel`."""
    appr = _cfg(["node", "--test"], parallel=3)
    live = copy.deepcopy(appr)
    if live_v is MISSING:
        live.pop("parallel")
    else:
        live["parallel"] = live_v
    assert oracle(appr, appr, live)["ok"] is ok
    got = H.classify_config_change(appr, appr, live)
    assert got["ok"] is ok, got
    if ok:
        assert any("parallel" in c for c in got["changes"]), got
    else:
        assert any("parallel" in r for r in got["refused"]), got


def test_ac3_parallel_added_and_removed_summary():
    """AC-3 BR-4.2: adding `parallel` where it was missing is accepted; removing it is summarised with the
    effective value (probes one at a time)."""
    appr = _cfg(["node", "--test"], parallel=3)
    no_par = copy.deepcopy(appr)
    no_par.pop("parallel")
    got = H.classify_config_change(no_par, no_par, appr)
    assert got["ok"] is True and any("parallel" in c for c in got["changes"]), got
    got = H.classify_config_change(appr, appr, no_par)
    assert got["ok"] is True, got
    assert any("parallel" in c and "one at a time" in c for c in got["changes"]), got


# --- malformed test_cmd -------------------------------------------------------------------------------
@pytest.mark.parametrize("live_tc", [
    "node --test --test-concurrency=2",
    ["node", "--test", 2],
    ["node", "--test", None],
    ["node", "--test", ["--test-concurrency=2"]],
    {"cmd": ["node", "--test"]},
    None,
], ids=["string", "int-item", "null-item", "list-item", "object", "null"])
def test_ac2_malformed_test_cmd_refused(live_tc):
    """AC-2 BR-4: a live test_cmd that is not a list of strings (a string, an object, null, a list with a
    non-string item) is not a concurrency change: refused, naming test_cmd (and nothing raises)."""
    appr = _cfg(["node", "--test"])
    live = dict(appr, test_cmd=live_tc)
    got = H.classify_config_change(copy.deepcopy(appr), copy.deepcopy(appr), live)
    assert got["ok"] is False, got
    assert any("test_cmd" in str(r) for r in got["refused"]), got
