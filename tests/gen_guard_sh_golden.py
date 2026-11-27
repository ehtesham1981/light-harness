"""Write this platform's table of tests/golden/guard_sh_base.json: the outcome of every BR-1 [sweep] scenario under
the BASE-commit `guard.sh` (and the hook_guard.py, harness.py and telemetry.py beside it), and of every BR-4
[sweep] stdin under the base-commit `hook_guard.py` run directly (issue #24's oracles).

Run once per platform family, in its own process, against the base-commit copies exported by
    light-harness baseline --task issue-24-guard-latency plugins/light-harness/scripts/guard.sh \
        plugins/light-harness/scripts/hook_guard.py plugins/light-harness/scripts/harness.py \
        plugins/light-harness/scripts/telemetry.py
then
    python tests/gen_guard_sh_golden.py .light-harness-base/plugins/light-harness/scripts
on Windows (Git Bash: the `nt` table) and in WSL or Linux (the `posix` table). Each run rewrites only its own
platform's table and keeps the other. Tests never read .light-harness-base/; they read the golden this writes.

It also writes this platform's table of tests/golden/guard_sh_rv9_base.json: the base guard.sh outcome of every
BR-1 [sweep] part (4) scenario (guard_sh_scenarios.scenarios_unencodable(), RV-9's class) as [exit code,
permissionDecision or null, the first guard event's rule or null]. With `--rv9-only` it writes only that file,
leaving guard_sh_base.json as it is:
    python tests/gen_guard_sh_golden.py --rv9-only .light-harness-base/plugins/light-harness/scripts
RV-9 golden shape: {"source", "<platform>": {"count", "sha256", "outcomes"}}.

Golden shape: {"source", "<platform>": {"strings": [...], "br1": {"count", "sha256", "outcomes"},
"br4": {"count", "sha256", "outcomes"}}}. An outcome is [exit code, stdout, stderr, events] with the three
texts as indexes into "strings" (normalised by guard_sh_scenarios.normalise: fixture paths <F>, session <S>,
no `ts`, tracebacks <traceback>).
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import guard_sh_scenarios as G  # noqa: E402

SOURCE = "base-commit guard.sh, hook_guard.py, harness.py, telemetry.py (89b0318, 1.9.0)"


class Strings:
    def __init__(self):
        self.items, self.index = [], {}

    def __call__(self, s: str) -> int:
        if s not in self.index:
            self.index[s] = len(self.items)
            self.items.append(s)
        return self.index[s]


def encode(norm, strings) -> list:
    code, so, se, ev = norm
    return [code, strings(so), strings(se), strings(ev)]


def check_base(scripts_dir: Path):
    if scripts_dir == G.LIVE_SCRIPTS or (".light-harness-base" not in scripts_dir.parts
                                         and G.REPO in scripts_dir.parents):
        sys.exit(f"{scripts_dir} is not a base-commit export (.light-harness-base/ or a baseline --out folder)")
    for name in ("guard.sh", "hook_guard.py", "harness.py", "telemetry.py"):
        if not (scripts_dir / name).is_file():
            sys.exit(f"no {name} in {scripts_dir}")


def br4_base(scripts_dir: Path, fx: dict, spec: dict):
    out = G.run_hook_guard(scripts_dir, G.render_shape(spec, fx), G.guard_env(fx), fx["events"], cwd=fx["M"])
    return G.normalise(out, fx, G.br4_sid(spec))


def write(dest: Path, golden: dict):
    """One outcome per line, so a regenerated table diffs line by line."""
    lines = ["{", f'"source": {json.dumps(golden["source"])},']
    plats = [p for p in ("nt", "posix") if p in golden]
    for pi, plat in enumerate(plats):
        t = golden[plat]
        lines.append(f'"{plat}": {{')
        lines.append('"strings": [')
        lines.append(",\n".join(json.dumps(s, ensure_ascii=False) for s in t["strings"]))
        lines.append("],")
        for ti, part in enumerate(("br1", "br4")):
            p = t[part]
            lines.append(f'"{part}": {{"count": {p["count"]}, "sha256": "{p["sha256"]}", "outcomes": [')
            lines.append(",\n".join(json.dumps(o) for o in p["outcomes"]))
            lines.append("]}" + ("," if ti == 0 else ""))
        lines.append("}" + ("," if pi < len(plats) - 1 else ""))
    lines.append("}")
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def write_rv9(table: dict):
    golden = json.loads(G.GOLDEN_RV9.read_text(encoding="utf-8")) if G.GOLDEN_RV9.exists() else {}
    golden["source"] = SOURCE
    golden[G.PLATFORM] = table
    lines = ["{", f'"source": {json.dumps(golden["source"])},']
    plats = [p for p in ("nt", "posix") if p in golden]
    for pi, plat in enumerate(plats):
        t = golden[plat]
        lines.append(f'"{plat}": {{"count": {t["count"]}, "sha256": "{t["sha256"]}", "outcomes": [')
        lines.append(",\n".join(json.dumps(o) for o in t["outcomes"]))
        lines.append("]}" + ("," if pi < len(plats) - 1 else ""))
    lines.append("}")
    G.GOLDEN_RV9.parent.mkdir(parents=True, exist_ok=True)
    G.GOLDEN_RV9.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote the {G.PLATFORM} table to {G.GOLDEN_RV9}")


def rv9_table(scripts_dir: Path, pool) -> dict:
    scs = G.scenarios_unencodable()
    res = pool.map(lambda f, sc: G.run_unencodable(scripts_dir, f, sc), scs)
    return {"count": len(scs), "sha256": G.sha256(scs), "outcomes": res}


def main(argv):
    rv9_only = "--rv9-only" in argv[1:]
    args = [a for a in argv[1:] if a != "--rv9-only"]
    if len(args) != 1:
        sys.exit("usage: gen_guard_sh_golden.py [--rv9-only] <scripts dir holding the base-commit guard.sh and "
                 "hook_guard.py>")
    scripts_dir = Path(args[0]).resolve()
    check_base(scripts_dir)
    tmp = Path(tempfile.mkdtemp(prefix="gshgold"))
    try:
        t0 = time.time()
        fx = G.fixture(tmp / "fx")
        pool = G.Pool(fx["root"], tmp / "pool")
        rv9 = rv9_table(scripts_dir, pool)
        print(f"BR-1 part (4): {rv9['count']} unencodable-path scenarios in {time.time() - t0:.0f}s")
        if rv9_only:
            write_rv9(rv9)
            return
        strings = Strings()
        br1 = G.br1_scenarios()
        res1 = pool.map(lambda f, sc: G.run_br1(scripts_dir, f, sc), br1)
        print(f"BR-1: {len(br1)} scenarios in {time.time() - t0:.0f}s")
        br4 = G.scenarios_shapes()
        res4 = pool.map(lambda f, spec: br4_base(scripts_dir, f, spec), br4)
        print(f"BR-4: {len(br4)} stdins, {time.time() - t0:.0f}s in all")
        table = {"strings": None,
                 "br1": {"count": len(br1), "sha256": G.sha256(br1), "outcomes": [encode(r, strings) for r in res1]},
                 "br4": {"count": len(br4), "sha256": G.sha256(br4), "outcomes": [encode(r, strings) for r in res4]}}
        table["strings"] = strings.items
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    write_rv9(rv9)
    golden = json.loads(G.GOLDEN.read_text(encoding="utf-8")) if G.GOLDEN.exists() else {}
    golden["source"] = SOURCE
    golden[G.PLATFORM] = table
    G.GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    write(G.GOLDEN, golden)
    print(f"wrote the {G.PLATFORM} table to {G.GOLDEN}")


if __name__ == "__main__":
    main(sys.argv)
