# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""Deterministic scenarios for the BR-7 [Preservation] sweep of task issue-25-16-30 (issues #16, #25, #30):
a `test_cmd` without `{concurrency}`, run with a free slot and finishing within `suite_timeout`, gives the
same argv, gate outcomes, records and messages as the base-commit harness.

Two levels, both written to tests/golden/run_base.json by tests/gen_run_golden.py from the base-commit
harness (tests/base_harness/) and checked against the new harness by tests/test_run_preservation.py:

1. `run_tests` level (`CASES`): every `test_cmd` shape x `junit_arg` x fake suite result. A child process
   (`python run_scenarios.py run-tests <scripts dir> <work dir>`) imports the harness under test, points it at
   a throwaway git repo, and replaces the `subprocess` module the harness sees with a proxy that fakes the
   suite process (both `run`, used at base, and `Popen`, used by the new code) and lets git through. For each
   case it records the spawned argv, cwd, decoding, environment (compared with `clean_env()` of the same
   moment), the per-run TEMP folder seen at spawn, and what `run_tests` returned.
2. gate level (`GATE_CASES`): gates x suite results, end to end in a toy task (tests/lh_toy.py), with the
   exit code, normalised output, the contract's redgate/verify/verdict blocks, the status and the suite-run
   records (minus their envelope).

Allowed differences (BR-7): the load fields of suite-run records (BR-12), the leftover-files warning
(BR-10) and the per-run TEMP/TMP/TMPDIR (BR-8). Everything is a fixed cross product: no randomness.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent

TEMP_VARS = ("TEMP", "TMP", "TMPDIR")
LOAD_FIELDS = ("slots", "running", "load_avg", "wait_s")
FAKE_PID = 2147483644  # never a real process: proc_start() of it is None on every OS

# --- run_tests level --------------------------------------------------------------------------------
SHAPES = {
    "python3 -m pytest -q": ["python3", "-m", "pytest", "-q"],
    "python -m pytest -q -k 'not slow'": ["python", "-m", "pytest", "-q", "-k", "not slow"],
    "py -m pytest": ["py", "-m", "pytest"],
    "node --test": ["node", "--test"],
    "sh -c wrapper": ["sh", "-c", 'pytest "$@"', "sh"],
    "npx jest --ci": ["npx", "jest", "--ci"],
    "braces {x}": ["python3", "-m", "pytest", "-k", "{x}"],
    "empty braces {}": ["node", "--test", "--reporter={}"],
    "placeholder {path}": ["python3", "run.py", "--out={path}"],
    "unknown placeholder {concurrency_x}": ["node", "--test", "--test-concurrency={concurrency_x}"],
    "near misses of the placeholder": ["pytest", "-n", "{Concurrency}", "{ concurrency }", "{concurrency",
                                       "concurrency}", "$concurrency", "%concurrency%"],
    "empty argument": ["python3", "-m", "pytest", "", "-q"],
    "non-ASCII": ["python3", "-m", "pytest", "-k", "naïve ü 测试"],
}
JUNIT_ARGS = {"default": "--junitxml={path}", "empty": ""}
# rc, junit test cases (None: the runner writes no junit file)
RESULTS = {
    "pass + junit": (0, [("tests.test_a", "test_one", "passed"), ("tests.test_a", "test_two", "passed")]),
    "fail + junit": (1, [("tests.test_a", "test_one", "passed"), ("tests.test_b", "test_two", "failed"),
                         ("tests.test_b", "test_three", "error")]),
    "collection error rc 2": (2, None),
    "all skipped": (0, [("tests.test_a", "test_one", "skipped"), ("tests.test_a", "test_two", "skipped")]),
    "no tests rc 5": (5, []),
    "killed by signal rc -9": (-9, None),
}


def cases() -> list[dict]:
    """The full cross product, in a fixed order."""
    out = []
    for s, cmd in SHAPES.items():
        for j, junit in JUNIT_ARGS.items():
            for r in RESULTS:
                out.append({"id": f"{s} | junit {j} | {r}", "test_cmd": cmd, "junit_arg": junit, "result": r})
    return out


def _junit_xml(tcs) -> str:
    from xml.sax.saxutils import quoteattr
    body = []
    for cls, name, outcome in tcs:
        child = {"failed": "<failure message=\"x\"/>", "error": "<error message=\"x\"/>",
                 "skipped": "<skipped message=\"x\"/>"}.get(outcome, "")
        body.append(f"<testcase classname={quoteattr(cls)} name={quoteattr(name)}>{child}</testcase>")
    return f'<?xml version="1.0" encoding="utf-8"?>\n<testsuites><testsuite name="s">{"".join(body)}</testsuite></testsuites>\n'


def _is_suite(cmd) -> bool:
    if not isinstance(cmd, (list, tuple)) or not cmd:
        return False
    first = os.path.basename(str(cmd[0])).lower()
    return first not in ("git", "git.exe", "taskkill", "taskkill.exe", "ps")


class _FakePopen:
    def __init__(self, result, text):
        self._rc, self._out, self._err = result
        self._text = text
        self.pid = FAKE_PID
        self.returncode = None
        self.stdin = self.stdout = self.stderr = None

    def communicate(self, input=None, timeout=None):
        self.returncode = self._rc
        if self._text:
            return self._out, self._err
        return self._out.encode("utf-8"), self._err.encode("utf-8")

    def wait(self, timeout=None):
        self.returncode = self._rc
        return self._rc

    def poll(self):
        return self.wait()

    def kill(self):
        pass

    terminate = kill

    def send_signal(self, sig):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.wait()
        return False


class FakeSubprocess(types.ModuleType):
    """Stands in for `subprocess` inside the harness: fakes the suite process, passes everything else on."""

    def __init__(self, real, harness, case_ref: dict):
        super().__init__("subprocess")
        self._real, self._h, self._case = real, harness, case_ref
        self.calls: list = []

    def __getattr__(self, name):
        return getattr(self._real, name)

    def _spawn(self, via, cmd, kw):
        h, case = self._h, self._case["case"]
        env = kw.get("env")
        clean = h.clean_env()
        rec = {"via": via, "argv": [norm_arg(a, h) for a in cmd], "cwd": norm_path(kw.get("cwd"), h),
               "encoding": kw.get("encoding"), "errors": kw.get("errors"),
               "env": env_relation(env, clean)}
        if env is not None:
            vals = [env.get(k) for k in TEMP_VARS]
            root = os.environ.get("LIGHT_HARNESS_TEMP_ROOT", "")
            t = vals[0] or ""
            rec["temp"] = {"same": len(set(vals)) == 1 and bool(t), "exists": os.path.isdir(t),
                           "empty": os.path.isdir(t) and not os.listdir(t),
                           "under_root": bool(root) and _under(t, root)}
        self.calls.append(rec)
        rc, tcs = RESULTS[case["result"]]
        junit = str(h.JUNIT)
        if tcs is not None and any(junit in str(a) for a in cmd):
            Path(junit).parent.mkdir(parents=True, exist_ok=True)
            Path(junit).write_text(_junit_xml(tcs), encoding="utf-8")
        return rc, f"stdout of [{case['id']}]: ✓ ünïcode\n", f"stderr of [{case['id']}]\n"

    def run(self, cmd, *a, **kw):
        if not _is_suite(cmd):
            return self._real.run(cmd, *a, **kw)
        rc, out, err = self._spawn("run", cmd, kw)
        return self._real.CompletedProcess(cmd, rc, out, err)

    def Popen(self, cmd, *a, **kw):  # noqa: N802 - the real name
        if not _is_suite(cmd):
            return self._real.Popen(cmd, *a, **kw)
        text = bool(kw.get("encoding") or kw.get("errors") or kw.get("text") or kw.get("universal_newlines"))
        return _FakePopen(self._spawn("Popen", cmd, kw), text)


def _under(p, root) -> bool:
    try:
        Path(os.path.realpath(p)).relative_to(Path(os.path.realpath(root)))
        return True
    except ValueError:
        return False


def norm_arg(a, h) -> str:
    a = str(a)
    if a == sys.executable:
        return "<PY>"
    return a.replace(str(h.JUNIT), "<JUNIT>")


def norm_path(p, h):
    if p is None:
        return None
    return "<ROOT>" if os.path.realpath(str(p)) == os.path.realpath(str(h.ROOT)) else str(p)


def env_relation(env, clean) -> dict:
    if env is None:
        return {"inherited": True}
    return {"extra": sorted(set(env) - set(clean)), "missing": sorted(set(clean) - set(env)),
            "changed": sorted(k for k in env if k in clean and env[k] != clean[k])}


def _mini_repo(work: Path) -> Path:
    repo = work / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "start"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    return repo


def run_tests_child(scripts: str, work: str) -> None:
    """In a child process: run every case through the harness in `scripts`; write work/result.json."""
    work_p = Path(work)
    repo = _mini_repo(work_p)
    os.chdir(repo)
    sys.path.insert(0, scripts)
    import harness  # noqa: E402 - the harness under test (base or new), first on sys.path
    harness.set_root(repo)
    ref: dict = {}
    fake = FakeSubprocess(subprocess, harness, ref)
    harness.subprocess = fake
    root = os.environ.get("LIGHT_HARNESS_TEMP_ROOT", "")
    out = {}
    for case in cases():
        ref["case"] = case
        fake.calls = []
        cfg = dict(harness.DEFAULT_CONFIG)
        cfg["test_cmd"] = list(case["test_cmd"])
        cfg["junit_arg"] = case["junit_arg"]
        rec = {}
        try:
            rc, text, results = harness.run_tests(cfg)
            rec["returned"] = {"rc": rc, "out": text, "results": results}
        except BaseException as e:  # noqa: BLE001 - recorded, compared with the base
            rec["raised"] = f"{type(e).__name__}: {e}"
        rec["last_results"] = dict(harness.LAST_RESULTS)
        rec["spawns"] = fake.calls
        rec["temp_root_left"] = sorted(os.listdir(root)) if root and os.path.isdir(root) else []
        rec["lock_left"] = sorted(p.name for p in harness.state_dir().glob("base-lock-*"))
        out[case["id"]] = rec
    (work_p / "result.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


def run_tests_level(scripts: Path, work: Path, env: dict) -> dict:
    """Run run_tests_child in a `sys.executable` child with `env`; its per-case records."""
    work.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([sys.executable, str(Path(__file__).resolve()), "run-tests", str(scripts), str(work)],
                       env=env, capture_output=True, timeout=600, stdin=subprocess.DEVNULL)
    res = work / "result.json"
    assert r.returncode == 0 and res.exists(), \
        f"run_tests child failed (exit {r.returncode}):\n{(r.stdout + r.stderr).decode('utf-8', 'replace')[-4000:]}"
    return json.loads(res.read_text(encoding="utf-8"))


def comparable_spawn(s: dict) -> dict:
    """A spawn record without the allowed BR-8 differences (TEMP/TMP/TMPDIR) and the spawn API used."""
    env = s.get("env") or {}
    if "inherited" not in env:
        env = {k: [x for x in v if x.upper() not in TEMP_VARS] for k, v in env.items()}
    return {"argv": s["argv"], "cwd": s["cwd"], "encoding": s["encoding"], "errors": s["errors"], "env": env}


def comparable_run(rec: dict) -> dict:
    return {"returned": rec.get("returned"), "raised": rec.get("raised"), "last_results": rec["last_results"],
            "spawns": [comparable_spawn(s) for s in rec["spawns"]], "temp_root_left": rec["temp_root_left"],
            "lock_left": rec["lock_left"]}


# --- gate level -------------------------------------------------------------------------------------
GATES = {
    "redgate": ("build", ["redgate"]),
    "verify": ("green", ["verify"]),
    "verdict pass": ("review", ["verdict", "pass", "--by", "Reviewer"]),
    "baseline (set-status Build)": ("plan", ["set-status", "Build"]),
    "probe run": ("probe", ["probe", "run"]),
}
GATE_RESULTS = ["pass", "fail", "collection error", "skipped"]
GATE_CASES = [(g, r) for g in GATES for r in GATE_RESULTS]
ENVELOPE = {"v", "ts", "harness", "session", "actor", "engineer", "cwd"}
WARN_BR10 = re.compile(r"Suite cleanup:")
_WHEN = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?: [A-Za-z+\-][A-Za-z0-9+\- ]*?(?= ?(?:\(|—|\)|;|,|\n|$)))?", re.M)


def normalise(text: str, roots=()) -> str:
    t = (text or "").replace("\r\n", "\n")
    variants = set()
    for r in roots:
        for p in (str(r), os.path.realpath(str(r))):
            variants |= {p, p.replace("\\", "/")}
    for v in sorted(variants, key=len, reverse=True):
        if v:
            t = t.replace(v, "<ROOT>")
    t = t.replace(sys.executable, "<PY>").replace("\\", "/")
    t = _WHEN.sub("<WHEN>", t)
    t = re.sub(r"\b[0-9a-f]{7,64}\b", "<HEX>", t)
    return t


def drop_br10(text: str) -> str:
    return "\n".join(l for l in text.split("\n") if not WARN_BR10.search(l))


def norm_record(e: dict) -> dict:
    r = {k: v for k, v in e.items() if k not in ENVELOPE and k not in LOAD_FIELDS}
    if "duration_s" in r:
        d = r["duration_s"]
        r["duration_s"] = "<D>" if isinstance(d, (int, float)) and not isinstance(d, bool) and d >= 0 else d
    if isinstance(r.get("key"), str):
        r["key"] = "<KEY>"
    return r


def run_gate_case(snaps: dict, gate: str, result: str, work: Path, sandbox: Path) -> dict:
    import lh_toy as T
    snap, args = GATES[gate]
    toy = T.clone(snaps[snap], work, sandbox)
    toy.ctl(T.PROBE_CTL[result] if gate == "probe run" else T.SUITE_CTL[result])
    n0 = len(toy.suite_records())
    rc, out = toy.h(*args)
    roots = [toy.root]
    return {"args": args, "snapshot": snap, "exit": rc, "out": normalise(out, roots), "status": toy.status(),
            "blocks": {b: normalise(toy.block(b), roots) for b in ("redgate", "verify", "verdict")},
            "suite": [norm_record(e) for e in toy.suite_records()[n0:]],
            "lock_left": toy.lock.exists()}


def gates_level(snaps: dict, work: Path, sandbox: Path, workers: int = 4) -> dict:
    sys.path.insert(0, str(HERE))
    work.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {f"{g} | {r}": ex.submit(run_gate_case, snaps, g, r, work / f"g{i}", sandbox)
                for i, (g, r) in enumerate(GATE_CASES)}
        return {k: f.result() for k, f in futs.items()}


def comparable_gate(rec: dict) -> dict:
    return {"exit": rec["exit"], "out": drop_br10(rec["out"]), "status": rec["status"],
            "blocks": {k: drop_br10(v) for k, v in rec["blocks"].items()}, "suite": rec["suite"],
            "lock_left": rec["lock_left"]}


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "run-tests":
    run_tests_child(sys.argv[2], sys.argv[3])
