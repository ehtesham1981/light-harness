"""AC-6 / BR-7 (issue #24): time one guarded call, base against new, in a throwaway fixture repo.

    python tests/bench_guard.py --base <scripts dir of a base export> [--n 10]

e.g. `--base .light-harness-base/plugins/light-harness/scripts` after `light-harness baseline` (step 1). It builds
its own temp fixture (a task in Build and one in Draft, tests/guard_sh_scenarios.fixture), records this
session's interpreter with the new session.sh, then times `sh guard.sh < input` for: a Read of an ordinary file,
an allowed Edit (Build), a blocked Edit (Draft, `stage`) and Bash `git status`, base and new interleaved, in the
hook environment Claude Code gives (PYTHONDONTWRITEBYTECODE=1, CLAUDECODE=1, CLAUDE_PROJECT_DIR). Calls run one at a
time, never in parallel, after one untimed warm-up call per case and version; base and new alternate call by
call. Prints a Markdown table: mean (BR-7's measure), median and min milliseconds per case. Targets: Read under 100 ms, Edit and Bash under ~200 ms.

Not collected by pytest (timing is machine-dependent); run it by hand. It never runs a guard in a repo it didn't
create (R-12): guard calls append telemetry events to the repo they judge.
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import guard_sh_scenarios as G  # noqa: E402

MARK = ".bench-guard-fixture"
SID = "bench-1"


def own_fixture(root: Path) -> dict:
    fx = G.fixture(root / "fx")
    (fx["root"] / MARK).write_text("created by tests/bench_guard.py\n", encoding="utf-8")
    return fx


def refuse_foreign(fx: dict, tmp: Path):
    """Run only in the fixture this script just built, under its own temp folder."""
    root = Path(fx["root"]).resolve()
    if not (root / MARK).is_file() or Path(tmp).resolve() not in root.parents:
        sys.exit(f"refusing to run the guard in {root}: not a fixture tests/bench_guard.py created")
    if G.REPO.resolve() in root.parents or root == G.REPO.resolve():
        sys.exit("refusing to run the guard inside this repo")


def cases(fx: dict) -> list:
    b, d = fx["T"]["Build"], fx["T"]["Draft"]

    def payload(cwd, tool, ti):
        return G.compact(G.cc_payload(fx, SID, cwd, tool, ti)).encode("utf-8")

    return [
        ("Read (ordinary file)", payload(b, "Read", {"file_path": str(b / "README.md")})),
        ("Edit (allowed, Build)", payload(b, "Edit", {"file_path": str(b / "src" / "a.py"), "old_string": "x = 1",
                                                      "new_string": "x = 2"})),
        ("Edit (blocked, Draft)", payload(d, "Edit", {"file_path": str(d / "src" / "a.py"), "old_string": "x = 1",
                                                      "new_string": "x = 2"})),
        ("Bash `git status`", payload(b, "Bash", {"command": "git status", "description": "status"})),
    ]


def time_one(scripts: Path, stdin: bytes, env: dict, cwd: Path) -> float:
    t0 = time.perf_counter()
    r = subprocess.run([G.SH, G.fwd(scripts / "guard.sh")], input=stdin, env=env, cwd=cwd, capture_output=True)
    dt = (time.perf_counter() - t0) * 1000
    if r.returncode not in (0, 2):
        sys.exit(f"guard.sh in {scripts} exited {r.returncode}: {r.stderr.decode('utf-8', 'replace')}")
    return dt


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", required=True, help="scripts folder of a base-commit export (guard.sh and friends)")
    ap.add_argument("--n", type=int, default=10, help="calls per case and version (default 10)")
    args = ap.parse_args(argv)
    base = Path(args.base).resolve()
    if not (base / "guard.sh").is_file() or base == G.LIVE_SCRIPTS:
        sys.exit(f"{base} holds no base-commit guard.sh")
    tmp = Path(tempfile.mkdtemp(prefix="benchguard"))
    try:
        fx = own_fixture(tmp)
        refuse_foreign(fx, tmp)
        env = G.guard_env(fx)
        rec = subprocess.run([G.SH, G.fwd(G.LIVE_SCRIPTS / "session.sh")],
                             input=json.dumps({"session_id": SID, "cwd": str(fx["M"]),
                                               "hook_event_name": "SessionStart", "source": "startup"}).encode(),
                             env=env, cwd=fx["M"], capture_output=True)
        recorded = (fx["state"] / "py" / SID).is_file()
        if rec.returncode != 0 or not recorded:
            print(f"note: the new session.sh recorded no interpreter for {SID}; the new guard will search",
                  file=sys.stderr)
        times = {(name, which): [] for name, _ in cases(fx) for which in ("base", "new")}
        for name, stdin in cases(fx):       # warm-up: one untimed call each
            for scripts in (base, G.LIVE_SCRIPTS):
                time_one(scripts, stdin, env, fx["M"])
        for _ in range(args.n):
            for name, stdin in cases(fx):
                times[(name, "base")].append(time_one(base, stdin, env, fx["M"]))
                times[(name, "new")].append(time_one(G.LIVE_SCRIPTS, stdin, env, fx["M"]))
        print(f"Guard latency over {args.n} calls each (ms), {G.PLATFORM}, record {'yes' if recorded else 'no'}\n")
        print("| Call | Base mean | Base median | Base min | New mean | New median | New min | Target (mean) |")
        print("|------|-----------|-------------|----------|----------|------------|---------|---------------|")
        for name, _ in cases(fx):
            target = "< 100" if name.startswith("Read") else "~200"
            cols = []
            for which in ("base", "new"):
                xs = times[(name, which)]
                cols += [f"{statistics.mean(xs):.0f}", f"{statistics.median(xs):.0f}", f"{min(xs):.0f}"]
            print(f"| {name} | " + " | ".join(cols) + f" | {target} |")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
