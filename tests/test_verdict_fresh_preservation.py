# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""AC-3 / BR-7 (issue #39): with `--fresh`, `verdict pass` behaves exactly as `verdict pass` did at the base
commit. The oracle is tests/golden/verdict_fresh_base.json, written once by tests/gen_verdict_golden.py from the
base-commit harness.py on the cells of tests/verdict_scenarios.py. This test never reads .light-harness-base/.

Allowed changes (BR-7, Q-10): the §11.4 line `Suite and probes: fresh run`, and the BR-8 anchor commit
(`harness: verify-pass …`) when the passing run's digest is new. Both are taken out of the current side before
comparing; the anchor commits are checked on their own: exactly one in a passing cell whose tree changed, none in
a passing cell with an unchanged tree (the Review verify already anchored the same digest), none in a failing cell.
"""
from __future__ import annotations

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import verdict_lab as VL  # noqa: E402
import verdict_scenarios as VS  # noqa: E402

GOLDEN = HERE / "golden" / "verdict_fresh_base.json"
FRESH_LINE = "Suite and probes: fresh run"
ANCHOR_PREFIX = "harness: verify-pass"
WORKERS = max(2, min(6, (os.cpu_count() or 2)))


@pytest.fixture(scope="module")
def template(tmp_path_factory) -> VL.Lab:
    base = tmp_path_factory.getbasetemp()
    return VL.build_template(tmp_path_factory.mktemp("lh39br7") / "lab", base, base / "lh39.gitconfig")


def _run(template: VL.Lab, dest: Path, cell: dict) -> dict:
    lab = VS.prepare(template, dest, cell)
    obs = VS.observe(lab, ("--fresh",))
    VL.rmtree(lab.root)
    return obs


def strip_allowed(obs: dict) -> tuple[dict, int]:
    """(the observation without BR-7's allowed changes, how many verify-pass anchor commits it had)."""
    obs = dict(obs)
    obs["verdict_block"] = "\n".join(l for l in obs["verdict_block"].split("\n") if l != FRESH_LINE)
    anchors = [s for s in obs["new_subjects"] if s.startswith(ANCHOR_PREFIX)]
    obs["new_subjects"] = [s for s in obs["new_subjects"] if not s.startswith(ANCHOR_PREFIX)]
    return obs, len(anchors)


SUITE_OUTPUT = re.compile(r"^```\n.*?^```$", re.M | re.S)


def without_suite_output(block: str) -> str:
    """The §10.4 block with the fenced test-runner output replaced by a placeholder. That output is pytest's own
    (separator widths follow the terminal, the short summary differs between pytest versions), so it differs
    between the machine that wrote the golden file and the one running the test; the harness's own lines around
    it are still compared."""
    return SUITE_OUTPUT.sub("<suite output>", block)


def test_br7_sweep_fresh_verdict_matches_base(template, tmp_path):
    """AC-3 BR-7 [sweep] BR-2 BR-5: 2 trees x 2 suite outcomes x 3 probe states = 12 cells; per cell the current
    `verdict pass --fresh` matches the base commit's `verdict pass` in suite runs, probe runs, exit code,
    status, refusal message, the normalised §10.4 and §11.4 blocks, the status-log entry, the new audit-trail
    subjects, baseline.json and the suite events (gate, cache, result), apart from the allowed changes; the
    BR-8 anchor commits are exactly one per passing cell with a changed tree and none elsewhere."""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))["cells"]
    cells = VS.cells()
    assert sorted(golden) == sorted(VS.name(c) for c in cells) and len(cells) == 12
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = [ex.submit(_run, template, tmp_path / f"cell{i}", c) for i, c in enumerate(cells)]
        observed = [f.result() for f in futures]

    bad = []
    for cell, obs in zip(cells, observed):
        name = VS.name(cell)
        want = golden[name]
        got, anchors = strip_allowed(obs)
        for k in want:
            if k == "verify_block":
                if without_suite_output(got.get(k) or "") != without_suite_output(want[k]):
                    bad.append(f"{name}: {k}: got {got.get(k)!r}, base {want[k]!r}")
                continue
            if got.get(k) != want[k]:
                bad.append(f"{name}: {k}: got {got.get(k)!r}, base {want[k]!r}")
        want_anchors = 1 if (want["exit"] == 0 and cell["tree"] == "changed") else 0
        if anchors != want_anchors:
            bad.append(f"{name}: {anchors} verify-pass anchor commits, expected {want_anchors}")
    differ = sorted({b.split(":", 1)[0] for b in bad})
    summary = f"{len(differ)} of {len(cells)} cells differ ({len(bad)} differences)"
    print(summary)
    assert not bad, summary + ":\n  " + "\n  ".join(bad[:10])
