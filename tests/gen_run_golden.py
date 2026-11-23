# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""Write tests/golden/run_base.json: what the BASE-commit harness does for every scenario in
tests/run_scenarios.py (the BR-7 [sweep] oracle of task issue-25-16-30).

The base harness is tests/base_harness/{harness,telemetry}.py (commit 89b0318, harness 1.9.0), exported with
    light-harness baseline --task issue-25-16-30 --out <folder> plugins/light-harness/scripts/harness.py
and committed unchanged. Run once:
    python tests/gen_run_golden.py
It works in a new folder under the system temp folder, with the run slots, slot count and temp root inside
it (tests/lh_machine.py). Tests never run the base harness for BR-7; they read the golden file this writes.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import lh_toy as T  # noqa: E402
import run_scenarios as RS  # noqa: E402
from lh_machine import machine_env  # noqa: E402


def child_env(machine: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in T.DROP_ENV and not k.upper().startswith(T.DROP_PREFIXES)}
    env.update(machine_env(machine))
    env.update(PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    return env


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="lh-br7-golden-")).resolve()
    run_tests = RS.run_tests_level(T.BASE_HARNESS, work / "rt", child_env(work / "rt-machine"))
    snaps = T.chain(work, "base")
    gates = RS.gates_level(snaps, work / "gates", work)
    out = {"source": "base-commit harness.py/telemetry.py (89b0318, 1.9.0) in tests/base_harness",
           "run_tests": run_tests, "gates": gates}
    dest = HERE / "golden" / "run_base.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {len(run_tests)} run_tests cases and {len(gates)} gate runs to {dest} (work folder {work})")


if __name__ == "__main__":
    main()
