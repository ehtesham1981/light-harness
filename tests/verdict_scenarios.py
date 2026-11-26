# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""The BR-7 [sweep] cells for issue #39: a `verdict pass` that runs fresh, observed end to end on the toy lab
of tests/verdict_lab.py. Shared by tests/gen_verdict_golden.py (the base-commit harness, plain `verdict pass`,
which always ran fresh) and tests/test_verdict_fresh_preservation.py (the current harness, `verdict pass
--fresh`).

Cells: tree {unchanged since the Review verify, changed (same-behaviour edit of src/t.py after Review)} x
suite outcome {pass, fail (outside-the-repo fail switch read by the toy test)} x probes {none, one passing,
one failing (saved passing before Review; the outside fail switch flips it after)} = 12.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import verdict_lab as VL

TREES = ["unchanged", "changed"]
SUITES = ["pass", "fail"]
PROBES = ["none", "passing", "failing"]


def cells() -> list:
    return [{"tree": t, "suite": s, "probes": p} for t in TREES for s in SUITES for p in PROBES]


def name(cell: dict) -> str:
    return f"tree={cell['tree']}|suite={cell['suite']}|probes={cell['probes']}"


def prepare(template: VL.Lab, dest: Path, cell: dict) -> VL.Lab:
    """[a passing probe saved] -> verify (stores the pass) -> set-status Review -> the reviewer's §11 edits ->
    [src/t.py edited] -> [fail switches on]. Ready for `verdict pass`."""
    lab = template.clone(dest)
    lab.counter.write_text("", encoding="utf-8")
    lab.probe_counter.write_text("", encoding="utf-8")
    if cell["probes"] != "none":
        lab.probe_add("RV-1")
    rc, out, ran = lab.verify()
    assert rc == 0 and ran == 1, f"{name(cell)}: setup verify failed:\n{out}"
    lab.ok("set-status", "Review")
    lab.resolve_review()
    if cell["tree"] == "changed":
        lab.code(VL.IMPL2)
    if cell["suite"] == "fail":
        lab.fail_suite()
    if cell["probes"] == "failing":
        lab.fail_probes()
    return lab


def normalise(text: str, lab: VL.Lab) -> str:
    """Machine- and run-independent text: temp paths, timestamps (with this machine's time-zone name, as the
    harness's now() writes it), hashes, timings, path separators and pytest's warnings summary replaced or
    removed; trailing whitespace cut on every line."""
    roots = set()
    for p in (lab.root, lab.root.resolve()):
        roots |= {str(p), p.as_posix(), str(p).replace("\\", "\\\\")}
    for r in sorted(roots, key=len, reverse=True):
        text = re.sub(re.escape(r), "<LAB>", text, flags=re.I)
    tz = dt.datetime.now().astimezone().strftime("%Z")
    text = re.sub(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?" + (f"(?: {re.escape(tz)})?" if tz else ""), "<when>",
                  text)
    text = re.sub(r"\b[0-9a-f]{7,64}\b", "<hex>", text)
    text = re.sub(r"\d+(?:\.\d+)?s\b", "<t>s", text)
    text = re.sub(r"=+ warnings summary =+\n.*?\n-- Docs: [^\n]*\n", "", text, flags=re.S)
    text = re.sub(r", \d+ warnings?(?= in <t>s)", "", text)
    text = text.replace("\\", "/")
    return "\n".join(l.rstrip() for l in text.splitlines())


def observe(lab: VL.Lab, flags: tuple) -> dict:
    """Run `verdict pass <flags>` and record everything BR-7 compares."""
    subjects = lab.trail_subjects()
    r = lab.verdict("pass", *flags)
    text = lab.text()
    raw = lab.trail_file("baseline.json")
    log = VL.block_of(text, "status-log").splitlines()
    return {
        "exit": r["rc"],
        "suite_runs": r["runs"],
        "probe_runs": r["probe_runs"],
        "status": lab.stage(),
        "refusal": [normalise(l, lab) for l in r["out"].splitlines() if "refused" in l],
        "verify_block": normalise(VL.block_of(text, "verify"), lab),
        "verdict_block": normalise(VL.block_of(text, "verdict"), lab),
        "status_log_last": normalise(log[-1], lab) if log else "",
        "new_subjects": [normalise(s, lab) for s in lab.trail_subjects()[len(subjects):]],
        "baseline_passing": json.loads(raw)["passing"] if raw else None,
        "suite_events": [[s.get("gate"), s.get("cache"), s.get("result")] for s in r["suites"]],
    }
