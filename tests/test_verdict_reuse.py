# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""Acceptance tests for issue #39: `verdict pass` reuses the stored verify pass when its reuse key is
unchanged and the pass is anchored in the audit trail; otherwise, or with `--fresh`, it runs fresh.
AC-1, AC-2, AC-3, AC-4, AC-5, AC-6, AC-8 and the BR-6 [sweep]. (AC-7 is the existing suite and selftest;
AC-3's BR-7 [sweep] is in test_verdict_fresh_preservation.py.)

End-to-end tests drive a COPY of plugins/light-harness against a toy repo in pytest's tmp_path (see
tests/verdict_lab.py). "Runs" = the toy suite's run counter delta, "probe runs" = the toy probes' counter
delta. The §11.4 line is `Suite and probes: reused verify <label> (key <12 hex>)` or `... fresh run`.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import verdict_lab as VL  # noqa: E402
from verdict_lab import IMPL2, SLUG, WRONG, source_lines  # noqa: E402

TASK_DIR = HERE.parent
SCRIPTS = TASK_DIR / "plugins" / "light-harness" / "scripts"
HARNESS_SRC = SCRIPTS / "harness.py"
TEST_ID = "tests.test_t::test_ac1"
FAKE_ID = "tests.test_t::test_forged"
REFUSED = "PASS refused: verification fails on the current tree"
VL_ANCHOR = "verify-pass.json"   # the anchor file in the audit-trail commit tree (§8.2 VERIFY_ANCHOR)


def _harness():
    """The task's harness.py, imported in-process (pure helpers only)."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import harness  # noqa: E402
    return harness


# --- fixtures ---------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def gitconfig(tmp_path_factory) -> Path:
    return tmp_path_factory.getbasetemp() / "lh39.gitconfig"


@pytest.fixture(scope="module")
def template(tmp_path_factory, gitconfig) -> VL.Lab:
    root = tmp_path_factory.mktemp("lh39tpl") / "lab"
    return VL.build_template(root, tmp_path_factory.getbasetemp(), gitconfig)


@pytest.fixture
def lab(template, tmp_path) -> VL.Lab:
    lab = template.clone(tmp_path / "lab")
    lab.counter.write_text("", encoding="utf-8")
    lab.probe_counter.write_text("", encoding="utf-8")
    assert not lab.cache.exists()
    assert lab.stage() == "Build"
    return lab


def review_ready(lab: VL.Lab, probe: bool = False, resolve: bool = True) -> dict:
    """[a passing probe saved] -> verify (a real run that stores the pass) -> set-status Review (a hit) ->
    [the reviewer's §11 edits]. Returns the stored entry."""
    if probe:
        lab.probe_add("RV-1")
    rc, out, ran = lab.verify()
    assert rc == 0 and ran == 1, f"the first verify ran the suite {ran}x (exit {rc}):\n{out}"
    e = lab.entry()
    assert e["result"] == "pass" and VL.HEX64.fullmatch(e["key"]), e
    n = lab.runs()
    lab.ok("set-status", "Review")
    assert lab.runs() == n, "set-status Review re-ran the suite on unchanged code"
    if resolve:
        lab.resolve_review()
    return e


def reused_line(e: dict) -> str:
    return f"reused verify {e['label']} (key {e['key'][:12]})"


def caches(r: dict) -> list:
    return [(s.get("gate"), s.get("cache"), s.get("result")) for s in r["suites"]]


def norm(text: str) -> str:
    """Output without timestamps, hashes and timings, for comparing two runs."""
    text = re.sub(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?", "<when>", text)
    text = re.sub(r"\b[0-9a-f]{7,64}\b", "<hex>", text)
    return re.sub(r"\d+(?:\.\d+)?s\b", "<t>s", text)


# --- AC-1 -------------------------------------------------------------------------------------------
def test_ac1_ex1_unchanged_review_verdict_reuses(lab, tmp_path):
    """AC-1 EX-1 BR-1 BR-3 BR-5: verify (miss) -> set-status Review (hit) -> the reviewer's verify (hit) ->
    §11 edits -> `baseline src/m.py` export -> `verdict pass`: no suite run, no probe run, status Done, the
    §11.4 line names the stored pass's label and key[:12], the suite event is ("review", "hit", 0), the verdict
    command event says reused, and baseline.json equals a `verdict pass --fresh` run's on a sibling copy."""
    e = review_ready(lab, probe=True, resolve=False)
    k = e["key"]
    rc, out, ran = lab.verify()
    assert rc == 0 and ran == 0 and f"cached, key {k[:12]}" in out, out
    lab.resolve_review()
    lab.ok("baseline", "src/m.py")
    sibling = lab.clone(tmp_path / "sibling")

    r = lab.verdict("pass")
    assert r["rc"] == 0, r["out"]
    assert (r["runs"], r["probe_runs"]) == (0, 0), f"verdict pass ran the suite {r['runs']}x and the probes " \
                                                   f"{r['probe_runs']}x on an unchanged, anchored key:\n{r['out']}"
    assert lab.stage() == "Done"
    block = lab.block("verdict")
    lines = [l.rstrip() for l in block.splitlines()]
    assert lines[0].startswith("**Verdict: PASS** by reviewer — "), block
    assert lines[1] == "Issues: 3 logged, 0 unresolved", block
    assert source_lines(block) == [f"reused verify {e['label']} (key {k[:12]})"], block
    assert len(lines) == 3, block
    assert [(s["gate"], s.get("cache"), s["duration_s"], s["result"]) for s in r["suites"]] == \
        [("review", "hit", 0, "pass")]
    cmd = lab.command_events("verdict")[-1]
    assert (cmd.get("ok"), cmd.get("reused")) == (True, True), cmd
    assert lab.trail_subjects()[-1] == "harness: verdict pass by reviewer"
    passing = json.loads(lab.trail_file("baseline.json"))["passing"]
    assert passing == [TEST_ID]

    rs = sibling.verdict("pass", "--fresh")
    assert rs["rc"] == 0 and rs["runs"] == 1, rs["out"]
    assert json.loads(sibling.trail_file("baseline.json"))["passing"] == passing


@pytest.mark.parametrize("age_s", [86400, 30 * 86400], ids=["1-day", "30-days"])
def test_ac1_ex3_day_old_pass_reused(lab, age_s):
    """AC-1 EX-3 BR-1: the stored pass's `when` is set a day / 30 days back (outside the anchor digest,
    Q-11); nothing else changed -> `verdict pass` still reuses it (no age limit)."""
    e = review_ready(lab)
    e["when"] = time.time() - age_s
    lab.write_entry(e)
    r = lab.verdict("pass")
    assert r["rc"] == 0 and r["runs"] == 0, f"a pass {age_s}s old wasn't reused:\n{r['out']}"
    assert source_lines(lab.block("verdict")) == [reused_line(e)]
    assert caches(r) == [("review", "hit", "pass")]
    assert lab.stage() == "Done"


def test_ac1_ex4_reuse_of_reuse(lab):
    """AC-1 EX-4 BR-1: the pass is stored by a real run in Build (its label then set to a distinct marker:
    the label is outside the anchor digest, Q-11); `set-status Review` and the reviewer's `verify` are both
    #14 hits; `verdict pass` reuses it, citing the label stored in Build, and its command event says reused."""
    rc, out, ran = lab.verify()
    assert rc == 0 and ran == 1, out
    e = lab.entry()
    e["label"] = "2001-02-03 04:05 Build-Pass"
    lab.write_entry(e)
    s0 = len(lab.suite_events())
    out = lab.ok("set-status", "Review")
    assert "passed 2001-02-03 04:05 Build-Pass" in out, out
    rc, out, ran = lab.verify()
    assert rc == 0 and ran == 0 and "passed 2001-02-03 04:05 Build-Pass" in out, out
    assert [(s["gate"], s.get("cache")) for s in lab.suite_events()[s0:]] == [("verify", "hit"), ("review", "hit")]
    lab.resolve_review()
    r = lab.verdict("pass")
    assert r["rc"] == 0 and r["runs"] == 0, r["out"]
    assert source_lines(lab.block("verdict")) == [f"reused verify 2001-02-03 04:05 Build-Pass (key {e['key'][:12]})"]
    assert lab.command_events("verdict")[-1].get("reused") is True


# --- AC-2 -------------------------------------------------------------------------------------------
def test_ac2_ex2_tracked_edit_runs_fresh(lab):
    """AC-2 EX-2 BR-2 BR-3 (Q-6): a same-behaviour edit of src/t.py after Review -> one suite run, PASS,
    `Suite and probes: fresh run`, suite event ("review", "miss", "pass"), command event not reused."""
    review_ready(lab)
    lab.code(IMPL2)
    r = lab.verdict("pass")
    assert r["rc"] == 0 and r["runs"] == 1, r["out"]
    assert source_lines(lab.block("verdict")) == ["fresh run"]
    assert caches(r) == [("review", "miss", "pass")]
    assert lab.command_events("verdict")[-1].get("reused") is False
    assert lab.stage() == "Done"


def test_ac2_reviewer_probe_forces_fresh(lab):
    """AC-2 BR-2: a probe the reviewer saves in Review changes the key -> the suite and the probe run, `fresh run`."""
    review_ready(lab)
    lab.probe_add("RV-2", note="reviewer")
    r = lab.verdict("pass")
    assert r["rc"] == 0, r["out"]
    assert (r["runs"], r["probe_runs"]) == (1, 1), r["out"]
    assert source_lines(lab.block("verdict")) == ["fresh run"]
    assert caches(r) == [("review", "miss", "pass")]


# --- AC-3 -------------------------------------------------------------------------------------------
def test_ac3_ex5_fresh_flag(lab):
    """AC-3 EX-5 BR-2 BR-7 (Q-6, R-11): unchanged key, `verdict pass --fresh` -> one suite run, the saved probe
    runs, `fresh run`, suite event ("review", "bypass", "pass"); the same digest adds no verify-pass anchor."""
    review_ready(lab, probe=True)
    anchors = len(lab.anchor_lines())
    r = lab.verdict("pass", "--fresh")
    assert r["rc"] == 0, r["out"]
    assert (r["runs"], r["probe_runs"]) == (1, 1), r["out"]
    assert source_lines(lab.block("verdict")) == ["fresh run"]
    assert caches(r) == [("review", "bypass", "pass")]
    assert lab.command_events("verdict")[-1].get("reused") is False
    assert lab.stage() == "Done"
    assert len(lab.anchor_lines()) == anchors


def test_ac3_fail_fresh_refused(lab):
    """AC-3 (Q-5): `verdict fail --fresh` is refused before any write: exit non-zero, the message, contract and
    audit trail unchanged, status Review; a plain `verdict fail` still records FAIL without a source line."""
    review_ready(lab, resolve=False)
    before, ref = lab.text(), lab.ref_sha()
    rc, out = lab.h("verdict", "fail", "--fresh")
    assert rc != 0 and "--fresh only applies to verdict pass" in out, out
    assert lab.text() == before
    assert lab.ref_sha() == ref
    assert lab.stage() == "Review"
    rc, out = lab.h("verdict", "fail")
    assert rc == 0, out
    assert lab.stage() == "Rework"
    block = lab.block("verdict")
    assert block.startswith("**Verdict: FAIL** by reviewer") and source_lines(block) == [], block


# --- AC-4 -------------------------------------------------------------------------------------------
def test_ac4_ex6_changed_key_failing_run_refused(lab, tmp_path):
    """AC-4 EX-6 BR-2 (Q-6): src/t.py set to wrong code after Review -> `verdict pass` runs once and is refused
    (exit 1, the message, status Review, §11.4 still pending, no verdict commit; event miss/fail); the same
    with `--fresh` (event bypass/fail); and on an unchanged tree with a stored pass, `--fresh` with the
    outside fail switch on is refused too."""
    review_ready(lab)
    sibling = lab.clone(tmp_path / "sibling")
    lab.code(WRONG)
    for flags, cache in (((), "miss"), (("--fresh",), "bypass")):
        r = lab.verdict("pass", *flags)
        assert r["rc"] == 1 and REFUSED in r["out"], r["out"]
        assert r["runs"] == 1, f"{flags}: {r['runs']} runs"
        assert lab.stage() == "Review"
        assert lab.block("verdict") == "_Pending._"
        assert not any(s.startswith("harness: verdict") for s in lab.trail_subjects())
        assert caches(r) == [("review", cache, "fail")], (flags, caches(r))

    sibling.fail_suite()
    r = sibling.verdict("pass", "--fresh")
    assert r["rc"] == 1 and REFUSED in r["out"] and r["runs"] == 1, r["out"]
    assert sibling.stage() == "Review" and sibling.block("verdict") == "_Pending._"
    assert caches(r) == [("review", "bypass", "fail")]


# --- AC-5 -------------------------------------------------------------------------------------------
def test_ac5_metrics_counts_reuse_as_hit(lab, tmp_path):
    """AC-5 BR-4 (Q-6): a reused verdict is one more cache hit and no suite run (suite minutes unchanged);
    a key-changed verdict is one more run and one more miss; `--fresh` is one more run (bypass), with
    hits, misses and the hit rate unchanged."""
    review_ready(lab)
    changed = lab.clone(tmp_path / "changed")
    forced = lab.clone(tmp_path / "forced")

    m0 = lab.metrics()
    r = lab.verdict("pass")
    m1 = lab.metrics()
    assert r["rc"] == 0, r["out"]
    assert [(s["gate"], s.get("cache"), s["duration_s"]) for s in r["suites"]] == [("review", "hit", 0)]
    assert (m1["cache_hits"], m1["cache_misses"], m1["suite_runs"]) == \
        (m0["cache_hits"] + 1, m0["cache_misses"], m0["suite_runs"]), (m0, m1)
    assert m1["suite_minutes"] == m0["suite_minutes"]

    changed.code(IMPL2)
    m0 = changed.metrics()
    r = changed.verdict("pass")
    m1 = changed.metrics()
    assert r["rc"] == 0 and caches(r) == [("review", "miss", "pass")], r["out"]
    assert (m1["cache_hits"], m1["cache_misses"], m1["suite_runs"]) == \
        (m0["cache_hits"], m0["cache_misses"] + 1, m0["suite_runs"] + 1), (m0, m1)

    m0 = forced.metrics()
    r = forced.verdict("pass", "--fresh")
    m1 = forced.metrics()
    assert r["rc"] == 0 and caches(r) == [("review", "bypass", "pass")], r["out"]
    assert (m1["cache_hits"], m1["cache_misses"], m1["suite_runs"], m1["cache_hit_rate"]) == \
        (m0["cache_hits"], m0["cache_misses"], m0["suite_runs"] + 1, m0["cache_hit_rate"]), (m0, m1)


@pytest.mark.parametrize("key,label", [("0123456789abcdef" * 4, "2026-11-26 10:22 Pakistan Standard Time"),
                                       ("fedcba9876543210" * 4, "1999-12-31 23:59 UTC")])
def test_ac5_verdict_source(key, label):
    """AC-5 AC-1 BR-3: verdict_source names a hit's label and key[:12]; miss, bypass, None and {} -> fresh run."""
    vs = _harness().verdict_source
    assert vs({"cache": "hit", "key": key, "label": label}) == f"reused verify {label} (key {key[:12]})"
    for d in ({"cache": "miss", "key": key, "label": None}, {"cache": "bypass", "key": key, "label": None},
              {"cache": None, "key": None, "label": None}, {}):
        assert vs(d) == "fresh run", d


# --- AC-6 -------------------------------------------------------------------------------------------
DOCS = ["plugins/light-harness/skills/task-review/SKILL.md", "plugins/light-harness/agents/reviewer.md",
        "plugins/light-harness/README.md", "README.md"]
BANNED = [r"verdict pass`?\**[^.|\n]{0,80}?\b(never reus\w*|always (runs?|does|re-?runs?))",
          r"never trusted by `?verdict pass", r"never reusing a stored run"]


@pytest.mark.parametrize("doc", DOCS)
def test_ac6_docs_describe_verdict_reuse(doc):
    """AC-6: the doc says `verdict pass` reuses the stored verify pass when the reuse key is unchanged and runs
    fresh otherwise (one line naming verdict pass, reuse, the key and fresh/otherwise), names
    `verdict pass --fresh`, and no longer says it always runs / never reuses / never trusts a stored run."""
    text = (TASK_DIR / doc).read_text(encoding="utf-8")
    assert re.search(r"verdict pass[^\n.]{0,40}?--fresh", text), f"{doc} doesn't name `verdict pass --fresh`"
    lines = [l for l in text.splitlines() if "verdict pass" in l and re.search(r"reus", l, re.I)
             and re.search(r"\bkey\b", l) and re.search(r"otherwise|(?<!-)\bfresh\b", l)]
    assert lines, f"{doc} has no line saying `verdict pass` reuses the stored pass on an unchanged key, fresh otherwise"
    stale = [m.group(0) for pat in BANNED for m in re.finditer(pat, text, re.I)]
    assert not stale, f"{doc} still says: {stale}"


def _functions(tree: ast.Module) -> dict:
    return {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _calls_to(node: ast.AST, name: str) -> list:
    return [n for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name]


def _names(node: ast.AST) -> set:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def test_ac6_single_decision_point_verdict():
    """AC-6 AC-8 BR-1 BR-6 (R-2): cmd_verdict calls verify once as verify(fresh=args.fresh, require_anchor=True),
    has no fresh=True, and never decides reuse itself; in verify, reuse_anchored is called once, in an `if`
    whose test names require_anchor and whose only statement is `cache, hit = "miss", None` (a downgrade only);
    anchor_reuse(key, results) is called once, as the statement right after store_reuse(...); load_reuse is
    still called only by verify_reuse_decision and reuse_report."""
    tree = ast.parse(HARNESS_SRC.read_text(encoding="utf-8"))
    fns = _functions(tree)
    cv, v = fns["cmd_verdict"], fns["verify"]

    calls = _calls_to(cv, "verify")
    assert len(calls) == 1, f"cmd_verdict calls verify {len(calls)}x"
    kw = {k.arg: k.value for k in calls[0].keywords}
    assert not calls[0].args and set(kw) == {"fresh", "require_anchor"}, ast.dump(calls[0])
    assert ast.dump(kw["fresh"]) == ast.dump(ast.parse("args.fresh", mode="eval").body), ast.dump(kw["fresh"])
    assert isinstance(kw["require_anchor"], ast.Constant) and kw["require_anchor"].value is True
    assert not [k for n in ast.walk(cv) if isinstance(n, ast.Call) for k in n.keywords
                if k.arg == "fresh" and isinstance(k.value, ast.Constant) and k.value.value is True]
    used = _names(cv) & {"load_reuse", "stored_reuse", "reuse_key", "reuse_parts", "reuse_anchored"}
    assert not used, f"cmd_verdict decides reuse itself: {used}"

    assert len(_calls_to(v, "reuse_anchored")) == 1
    ifs = [n for n in ast.walk(v) if isinstance(n, ast.If) and _calls_to(n.test, "reuse_anchored")]
    assert len(ifs) == 1, "reuse_anchored isn't called in an `if` test in verify"
    node = ifs[0]
    assert "require_anchor" in _names(node.test), ast.dump(node.test)
    assert not node.orelse and len(node.body) == 1, "the anchor branch does more than downgrade"
    assert ast.dump(node.body[0]) == ast.dump(ast.parse('cache, hit = "miss", None').body[0]), ast.dump(node.body[0])

    assert len(_calls_to(tree, "anchor_reuse")) == 1 and len(_calls_to(v, "anchor_reuse")) == 1
    bodies = [getattr(n, f) for n in ast.walk(v) for f in ("body", "orelse") if isinstance(getattr(n, f, None), list)]
    follows = []
    for body in bodies:
        for i, st in enumerate(body):
            if isinstance(st, ast.Expr) and _calls_to(st, "store_reuse"):
                follows.append(ast.dump(body[i + 1]) if i + 1 < len(body) else None)
    assert follows == [ast.dump(ast.parse("anchor_reuse(key, results)").body[0])], follows

    callers = {name for name, fn in fns.items() if _calls_to(fn, "load_reuse")}
    assert callers == {"verify_reuse_decision", "reuse_report"}, callers


# --- AC-8 -------------------------------------------------------------------------------------------
def _digest(e: dict) -> str:
    return hashlib.sha256(json.dumps({"key": e.get("key"), "result": e.get("result"), "results": e.get("results")},
                                     sort_keys=True).encode("utf-8")).hexdigest()


@pytest.mark.parametrize("seed", [0, 1])
def test_ac8_reuse_digest_fields(seed):
    """AC-8 BR-8 (Q-11): reuse_digest = sha256 of json.dumps({key, result, results}, sort_keys=True); changing
    when, label, out or parts keeps it; changing key, result or any results item changes it."""
    rd = _harness().reuse_digest
    base = {"key": ("ab" if seed else "c4") * 32, "result": "pass",
            "results": {"tests.a::t1": "passed", f"tests.b::t{seed}": "passed"},
            "when": 1700000000.5 + seed, "label": f"label {seed}", "out": f"out {seed}",
            "parts": {"tree": "1" * 64, "env": str(seed) * 64}}
    d = rd(base)
    assert d == _digest(base) and re.fullmatch(r"[0-9a-f]{64}", d)
    for field, value in (("when", 1.0), ("label", "other"), ("out", "other output"), ("parts", {})):
        assert rd({**base, field: value}) == d, field
    changes = [("key", "0" * 64), ("result", "fail"),
               ("results", {**base["results"], "tests.a::t1": "failed"}),
               ("results", {**base["results"], "tests.c::new": "passed"}),
               ("results", {"tests.a::t1": "passed"})]
    for field, value in changes:
        e = {**base, field: value}
        assert rd(e) == _digest(e) and rd(e) != d, (field, value)
    assert rd({}) == _digest({})


def test_ac8_ex7_forged_entry_runs_fresh(lab, tmp_path):
    """AC-8 EX-7 BR-1 BR-8 (Q-12): (1) after an anchored Review pass, a same-behaviour edit (new key K2) and a
    pass forged under K2 by a script importing harness.py: `status` shows it cached (#14 unchanged), but
    `verdict pass` runs once, `fresh run`, event miss. (2) a pass forged under the current, anchored key with
    one extra fake test (a digest the trail doesn't hold): also fresh."""
    e0 = review_ready(lab)
    sibling = lab.clone(tmp_path / "sibling")

    lab.code(IMPL2)
    forged = lab.forge(dict(e0["results"]))
    assert forged["key"] != e0["key"]
    assert lab.verify_line() == f"cached, key {forged['key'][:12]} (passed {forged['label']})"
    r = lab.verdict("pass")
    assert r["rc"] == 0 and r["runs"] == 1, f"a forged pass was reused:\n{r['out']}"
    assert source_lines(lab.block("verdict")) == ["fresh run"]
    assert caches(r) == [("review", "miss", "pass")]

    forged = sibling.forge({**e0["results"], FAKE_ID: "passed"})
    assert forged["key"] == e0["key"]
    assert sibling.verify_line() == f"cached, key {e0['key'][:12]} (passed {forged['label']})"
    r = sibling.verdict("pass")
    assert r["rc"] == 0 and r["runs"] == 1, f"a forged pass on the anchored key was reused:\n{r['out']}"
    assert source_lines(sibling.block("verdict")) == ["fresh run"]
    assert caches(r) == [("review", "miss", "pass")]


def test_ac8_real_run_anchors_without_changing_key(lab):
    """AC-8 BR-8 (R-11, R-12): the first passing verify adds exactly one `verify-pass (key <K12>)` event; the
    trail's verify-pass.json holds the stored entry's digest (computed here), key and label; status still says
    cached under K and the next verify is a 0-run hit. A hit, a same-result `verify --fresh`, a failing run and
    the passing run after it add no anchor. metrics' review rounds, approvals and unlocks don't change. With a
    probe saved, the new pass is anchored once more and is still a 0-run hit (the probes part is unchanged)."""
    fields = ("review_rounds", "approvals", "unlocks", "stage")
    m0 = lab.metrics()
    assert lab.anchor_lines() == []
    rc, out, ran = lab.verify()
    assert rc == 0 and ran == 1, out
    e = lab.entry()
    k = e["key"]
    anchors = lab.anchor_lines()
    assert len(anchors) == 1 and anchors[0].rstrip().endswith(f"verify-pass (key {k[:12]})"), anchors
    assert lab.trail_subjects()[-1] == f"harness: verify-pass (key {k[:12]})"
    anchor = json.loads(lab.trail_file(VL_ANCHOR))
    assert anchor == {"digest": _digest(e), "key": k, "label": e["label"]}, anchor
    assert lab.verify_line() == f"cached, key {k[:12]} (passed {e['label']})"
    rc, out, ran = lab.verify()
    assert rc == 0 and ran == 0 and f"cached, key {k[:12]}" in out, out

    rc, out, ran = lab.verify("--fresh")
    assert rc == 0 and ran == 1 and lab.entry()["key"] == k, out
    lab.fail_suite()                     # outside the key: only a fresh run sees it; it fails and drops the pass
    rc, out, ran = lab.verify("--fresh")
    assert rc != 0 and ran == 1, out
    assert lab.verify_line() == "no cached pass"
    lab.fail_suite(False)
    rc, out, ran = lab.verify()
    assert rc == 0 and ran == 1 and lab.entry()["key"] == k, out
    assert len(lab.anchor_lines()) == 1, lab.anchor_lines()
    m1 = lab.metrics()
    assert {f: m1[f] for f in fields} == {f: m0[f] for f in fields}

    lab.probe_add("RV-1")
    rc, out, ran = lab.verify()
    assert rc == 0 and ran == 1, out
    e3 = lab.entry()
    assert e3["key"] != k
    anchors = lab.anchor_lines()
    assert len(anchors) == 2 and anchors[0].rstrip().endswith(f"verify-pass (key {e3['key'][:12]})"), anchors
    assert json.loads(lab.trail_file(VL_ANCHOR))["digest"] == _digest(e3)
    assert lab.verify_line() == f"cached, key {e3['key'][:12]} (passed {e3['label']})"
    rc, out, ran = lab.verify()
    assert rc == 0 and ran == 0 and f"cached, key {e3['key'][:12]}" in out, out



@pytest.mark.parametrize("kind", ["raise", "exit"])
def test_ac8_anchor_write_failure_is_safe(lab, tmp_path, kind):
    """AC-8 BR-8 (Q-13, R-13): a verify whose anchor write fails (record raises / SystemExit for verify-pass
    subjects) still passes with the same output as a normal verify on a sibling copy, keeps the cache file and
    writes no anchor (the sibling writes one); the following `verdict pass` runs fresh."""
    sibling = lab.clone(tmp_path / "sibling")
    rc, out, ran = lab.verify_failing_anchor(kind)
    assert rc == 0 and ran == 1, out
    assert "simulated" not in out and "Traceback" not in out, out
    rc2, out2, ran2 = sibling.verify()
    assert rc2 == 0 and ran2 == 1, out2
    assert norm(out) == norm(out2)
    assert lab.stored_key() is not None and lab.stored_key() == sibling.stored_key()
    assert lab.anchor_lines() == []
    assert len(sibling.anchor_lines()) == 1
    n = lab.runs()
    lab.ok("set-status", "Review")
    assert lab.runs() == n
    lab.resolve_review()
    r = lab.verdict("pass")
    assert r["rc"] == 0 and r["runs"] == 1, r["out"]
    assert source_lines(lab.block("verdict")) == ["fresh run"]
    assert caches(r) == [("review", "miss", "pass")]


# --- BR-6 sweep -------------------------------------------------------------------------------------
CHANGES = ["none", "tracked file content", "new untracked file", "ignored names", "config value", "probe added",
           "gate record: red gate", "gate record: approval", "environment value", "script hash"]
STORED = ["none", "failed run", "passed run"]
ANCHOR = ["present", "absent"]
FRESH = [True, False]
SWEEP_ENV = {"LH39_SWEEP_EXTRA": "1"}
WORKERS = max(2, min(6, (os.cpu_count() or 2)))


def _setup(template: VL.Lab, dest: Path, change: str) -> tuple:
    """One setup per key change: verify stores and anchors C0; the change (gate records in Build/Plan); set-status
    Review; the other changes after it; §11 resolved; a pass forged under the then-current key with one extra
    fake test (anchor absent); then C0 is put back. Returns (lab, C0, forged entry, extra env for the verdict)."""
    lab = template.clone(dest)
    lab.counter.write_text("", encoding="utf-8")
    rc, out, ran = lab.verify()
    assert rc == 0 and ran == 1, f"{change}: setup verify failed:\n{out}"
    c0 = lab.entry()
    if change == "gate record: red gate":
        VL.new_red_gate(lab)
    elif change == "gate record: approval":
        VL.new_approval(lab)
    lab.ok("set-status", "Review")
    if change == "tracked file content":
        lab.code(IMPL2)
    elif change == "new untracked file":
        VL.write(lab.task / "src" / "extra.py", "X = 1\n")
    elif change == "ignored names":
        VL.write(lab.task / ".env", "SECRET=1\n")
    elif change == "config value":
        cfg = json.loads(lab.config.read_text(encoding="utf-8"))
        assert cfg["parallel"] == 3
        cfg["parallel"] = 2
        lab.config.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    elif change == "probe added":
        lab.probe_add("RV-2", note="sweep")
    elif change == "script hash":
        h = lab.scripts / "harness.py"
        h.write_bytes(h.read_bytes() + b"\n# a local edit (BR-6 sweep)\n")
    lab.resolve_review()
    extra = SWEEP_ENV if change == "environment value" else None
    forged = lab.forge({**c0["results"], FAKE_ID: "passed"}, extra=extra)
    lab.write_entry(c0)
    return lab, c0, forged, extra


def _run_cell(setup: tuple, dest: Path, stored: str, anchor: str, fresh: bool) -> dict:
    lab, c0, forged, extra = setup
    cell = lab.clone(dest)
    if stored == "none":
        cell.cache.unlink(missing_ok=True)
    else:
        e = dict(c0 if anchor == "present" else forged)
        if stored == "failed run":
            e["result"] = "fail"
        cell.write_entry(e)
    r = cell.verdict("pass", *(["--fresh"] if fresh else []), extra=extra)
    got = {"rc": r["rc"], "runs": r["runs"], "source": source_lines(cell.block("verdict")),
           "cache": [s.get("cache") for s in r["suites"]], "status": cell.stage()}
    VL.rmtree(cell.root)
    return {"got": got, "out": r["out"]}


def test_br6_sweep_verdict_reuse_iff_key_unchanged(template, tmp_path):
    """AC-2 AC-1 AC-8 BR-6 [sweep] BR-1 BR-2: full cross product, 3 stored states x 10 key changes x 2 trail
    anchors x 2 `--fresh` = 120 end-to-end `verdict pass` runs. Oracle, computed independently: reuse iff a
    passing run is stored, nothing in the key changed, its digest is anchored in the trail and no --fresh.
    Reuse = 0 runs, `reused verify <C0 label> (key <C0 key[:12]>)`, event hit, Done; otherwise exactly 1 run,
    event bypass (--fresh) or miss, `fresh run` and Done, except the config-value cells, which are refused by
    anti-gaming as at base (1 run, no source line, status Review)."""
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {c: ex.submit(_setup, template, tmp_path / f"setup{i}", c) for i, c in enumerate(CHANGES)}
        setups = {c: f.result() for c, f in futures.items()}
    cells = [(s, c, a, f) for s in STORED for c in CHANGES for a in ANCHOR for f in FRESH]
    assert len(cells) == 120
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = [ex.submit(_run_cell, setups[c], tmp_path / f"cell{i}", s, a, f)
                   for i, (s, c, a, f) in enumerate(cells)]
        results = [f.result() for f in futures]

    bad = []
    for (stored, change, anchor, fresh), res in zip(cells, results):
        c0 = setups[change][1]
        reuse = stored == "passed run" and change == "none" and anchor == "present" and not fresh
        passes = change != "config value"
        if reuse:
            want = {"rc": 0, "runs": 0, "source": [reused_line(c0)], "cache": ["hit"], "status": "Done"}
        else:
            want = {"rc": 0 if passes else 1, "runs": 1, "source": ["fresh run"] if passes else [],
                    "cache": ["bypass" if fresh else "miss"], "status": "Done" if passes else "Review"}
        got = res["got"]
        if got != want:
            diff = {k: (got[k], want[k]) for k in want if got[k] != want[k]}
            bad.append(f"stored={stored} | change={change} | anchor={anchor} | fresh={fresh}: (got, want) {diff}")
    summary = f"{len(bad)} of {len(cells)} cells differ"
    print(summary)
    assert not bad, summary + ":\n  " + "\n  ".join(bad[:10])
