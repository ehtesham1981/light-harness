"""Write tests/golden/metrics_base.json: what the BASE-commit telemetry.py computes and prints for every
scenario in tests/metrics_scenarios.py (the BR-7 [sweep] oracle).

Run once, against the base-commit copy exported by
    light-harness baseline --task issue-20-suite-metrics plugins/light-harness/scripts/telemetry.py
then
    python tests/gen_metrics_golden.py .light-harness-base/plugins/light-harness/scripts
Tests never read .light-harness-base/ themselves; they read the golden file this writes.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import metrics_scenarios as MS  # noqa: E402


def load_telemetry(scripts_dir: Path):
    spec = importlib.util.spec_from_file_location("telemetry_base", scripts_dir / "telemetry.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._iso = MS.fixed_iso  # machine-independent `started` (R-6)
    return mod


def main(argv):
    if len(argv) != 2:
        sys.exit("usage: gen_metrics_golden.py <scripts dir holding the base-commit telemetry.py>")
    tel = load_telemetry(Path(argv[1]).resolve())
    out = {"source": "base-commit telemetry.py (86f9af9, 1.8.1)", "now": MS.NOW, "scenarios": {}}
    for sc in MS.scenarios():
        out["scenarios"][sc["name"]] = MS.run(tel, sc)
    dest = HERE / "golden" / "metrics_base.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {len(out['scenarios'])} scenarios to {dest}")


if __name__ == "__main__":
    main(sys.argv)
