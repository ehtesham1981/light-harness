"""Write tests/golden/guard_base.json: the outcome of every BR-6 [sweep] command (tests/guard_scenarios.py)
under the BASE-commit `bash_rules` (issue #23, the BR-6 oracle).

Run once, in its own process, against the base-commit copy exported by
    light-harness baseline --task issue-23-guard-reads plugins/light-harness/scripts/hook_guard.py
then
    python tests/gen_guard_golden.py .light-harness-base/plugins/light-harness/scripts
Tests never read .light-harness-base/ themselves; they read the golden file this writes.

The golden is compact (Q-8): {"source", "count", "commands_sha256", "outcomes": [code per scenario index]},
where a code is p (pass), a (allow), b:<rule> (block) or k:<rule> (ask).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIVE_SCRIPTS = (HERE.parent / "plugins" / "light-harness" / "scripts").resolve()
SOURCE = "base-commit hook_guard.py (8490fc7, 1.9.0)"


def load_guard(scripts_dir: Path):
    # Base hook_guard does `import harness`: its own folder must come first, before anything else
    # could load the new harness.py (R-5). This runs in its own process for that reason.
    sys.path.insert(0, str(scripts_dir))
    spec = importlib.util.spec_from_file_location("hook_guard_base", scripts_dir / "hook_guard.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    h = sys.modules.get("harness")
    if h is None or Path(h.__file__).resolve().parent != scripts_dir:
        sys.exit(f"harness was not loaded from {scripts_dir}; refusing to write a golden")
    return mod


def main(argv):
    if len(argv) != 2:
        sys.exit("usage: gen_guard_golden.py <scripts dir holding the base-commit hook_guard.py>")
    scripts_dir = Path(argv[1]).resolve()
    if scripts_dir == LIVE_SCRIPTS or ".light-harness-base" not in scripts_dir.parts and \
            (HERE.parent in scripts_dir.parents):
        sys.exit(f"{scripts_dir} is not a base-commit export (.light-harness-base/ or a baseline --out folder)")
    if not (scripts_dir / "hook_guard.py").is_file():
        sys.exit(f"no hook_guard.py in {scripts_dir}")
    guard = load_guard(scripts_dir)
    sys.path.insert(0, str(HERE))
    import guard_scenarios as GS  # noqa: E402  (after the base harness is loaded)

    scs = GS.scenarios()
    outcomes = [GS.code(GS.outcome(guard, sc["cmd"])) for sc in scs]
    out = {"source": SOURCE, "count": len(scs), "commands_sha256": GS.commands_sha256(scs), "outcomes": outcomes}
    dest = HERE / "golden" / "guard_base.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(out, ensure_ascii=False, indent=0)  # one outcome code per line
    dest.write_text(body + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {len(scs)} outcomes to {dest}")


if __name__ == "__main__":
    main(sys.argv)
