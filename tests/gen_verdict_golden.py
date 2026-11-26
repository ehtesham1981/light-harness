# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""Write tests/golden/verdict_fresh_base.json: what the BASE-commit harness.py does on `verdict pass` (which
always ran fresh) for every cell in tests/verdict_scenarios.py (the BR-7 [sweep] oracle).

Run once, against the base-commit copy exported by
    light-harness baseline --task issue-39-verdict-reuse plugins/light-harness/scripts/harness.py
then
    python tests/gen_verdict_golden.py .light-harness-base/plugins/light-harness/scripts
The rest of the plugin (telemetry.py, the template, ...) is copied from the task folder: this task doesn't
change it. Tests never read .light-harness-base/ themselves; they read the golden file this writes.
"""
from __future__ import annotations

import json
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import verdict_lab as VL  # noqa: E402
import verdict_scenarios as VS  # noqa: E402


def main(argv):
    if len(argv) != 2:
        sys.exit("usage: gen_verdict_golden.py <scripts dir holding the base-commit harness.py>")
    base = Path(argv[1]).resolve() / "harness.py"
    if not base.is_file():
        sys.exit(f"no harness.py in {argv[1]}")
    sandbox = Path(tempfile.mkdtemp(prefix="lh39gold"))
    gitconfig = sandbox / "lh39.gitconfig"
    template = VL.build_template(sandbox / "tpl", sandbox, gitconfig, harness_file=base)
    out = {"source": "base-commit harness.py (89b0318, 1.9.0), plain `verdict pass`", "cells": {}}

    def run(i, cell):
        lab = VS.prepare(template, sandbox / f"cell{i}", cell)
        obs = VS.observe(lab, ())
        VL.rmtree(lab.root)
        return obs

    cells = VS.cells()
    with ThreadPoolExecutor(max_workers=6) as ex:        # cells are independent copies
        observed = list(ex.map(run, range(len(cells)), cells))
    for cell, obs in zip(cells, observed):
        out["cells"][VS.name(cell)] = obs
        print(f"{VS.name(cell)}: exit {obs['exit']}")
    dest = HERE / "golden" / "verdict_fresh_base.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    VL.rmtree(sandbox)
    print(f"wrote {len(out['cells'])} cells to {dest}")


if __name__ == "__main__":
    main(sys.argv)
