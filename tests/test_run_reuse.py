# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""The verify reuse key ignores the per-run TEMP, the slot and `{concurrency}` (issues #16, #30): AC-7
(BR-11, EX-10) and the BR-11 [sweep].

The BR-11 oracle is the base-commit harness itself (tests/base_harness/, commit 89b0318), run live in a
child process with that folder first on sys.path: the reuse parts depend on the machine (interpreter path,
environment, tree hashes), so a golden file written once wouldn't hold on another machine."""
from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import lh_toy as T  # noqa: E402
from lh_machine import GITCONFIG_BASE, TEMP_ROOT_ENV, machine_env, machine_paths  # noqa: E402

PART_NAMES = ["tree", "tests", "probes", "config", "version", "approval", "base", "python", "env"]
SAME_PARTS = [p for p in PART_NAMES if p != "version"]
LOAD_FIELDS = ("slots", "running", "load_avg", "wait_s")


@pytest.fixture(scope="module")
def snaps(tmp_path_factory):
    return T.chain(tmp_path_factory.getbasetemp(), "new")


def test_ac7_ex10_reuse_under_other_temp_root_and_slot_count(snaps, tmp_path, tmp_path_factory):
    """AC-7 EX-10 BR-11 AC-8 BR-12: a passing verify (its suite ran in a run folder under temp root A, slot
    setting unset) is reused by the next verify of the unchanged tree run under temp root B with
    lightharness.slots=1: `cached, key ...`, the suite doesn't run. The run's suite record carries the load
    fields (slots, running, wait_s); the reused (hit) record carries none of them."""
    toy = T.clone(snaps["green"], tmp_path / "lab", tmp_path_factory.getbasetemp())
    temps = tmp_path / "temps.txt"
    toy.ctl(f"import os\nif TEST == 't':\n    open({str(temps)!r}, 'a').write((os.environ.get('TEMP') or '') + '\\n')\n")
    rc, out = toy.h("verify")
    assert rc == 0 and "cached" not in out, out
    seen = temps.read_text().split()
    assert len(seen) == 1 and T.under(seen[0], T.temp_root_of(toy.machine)), seen
    T.set_slots(toy.machine, 1)
    root_b = tmp_path / "root B"
    root_b.mkdir()
    rc, out = toy.h("verify", extra={TEMP_ROOT_ENV: str(root_b)})
    assert rc == 0, out
    assert "cached, key " in out, out
    assert len(temps.read_text().split()) == 1, "the suite ran again"
    recs = toy.suite_records()
    run, hit = recs[-2], recs[-1]
    assert run.get("cache") == "miss" and hit.get("cache") == "hit", recs[-2:]
    assert isinstance(run.get("slots"), int) and run["slots"] >= 1, run
    assert isinstance(run.get("running"), int) and run["running"] >= 1, run
    assert isinstance(run.get("wait_s"), (int, float)) and run["wait_s"] >= 0, run
    assert not any(k in hit for k in LOAD_FIELDS), hit


# --- BR-11 sweep ------------------------------------------------------------------------------------
CHILD = r'''
import json, os, sys, time
scripts, task, combos_path, out_path, flavour = sys.argv[1:6]
sys.path.insert(0, scripts)
import harness as H


def put(path, text):
    """Replace the file in one step (the other child's git may be reading it)."""
    tmp = path + ".%d.tmp" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    for _ in range(500):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.01)
    os.replace(tmp, path)


combos = json.load(open(combos_path, encoding="utf-8"))
real_cpu = os.cpu_count
H.set_root(task)
cfg = H.load_config()
rg = H.redgate_state()[2]
appr = H.latest("harness: plan-approved")
a = appr[1] if appr else ""
res = []
for c in combos:
    os.environ["LIGHT_HARNESS_TEMP_ROOT"] = c["temp_root"]
    if flavour == "new":  # the base harness never reads lightharness.slots
        put(os.environ["GIT_CONFIG_GLOBAL"], c["gitconfig"])
    os.cpu_count = (lambda n=c["cpus"]: n)
    H.set_root(task)
    if flavour == "new":
        H.SLOT_POLL = 0.05
        with H.run_slot("verify"):
            if c["wait"]:
                H.RUN_LOAD["wait_s"] = float(c["wait"])  # a faked wait for a slot
            with H.run_temp():
                key = H.reuse_key(cfg, H.probes(), rg, a)
    else:
        key = H.reuse_key(cfg, H.probes(), rg, a)
    res.append({"key": key, "parts": H.LAST_REUSE_PARTS[key]})
os.cpu_count = real_cpu
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(res, f)
'''


def test_br11_reuse_key_sweep(snaps, tmp_path, tmp_path_factory):
    """AC-7 BR-11 [sweep]: full cross product of temp root {the default test root, a path with spaces, a
    non-ASCII path} x lightharness.slots {unset, 1, 2, 4, "abc"} x os.cpu_count() {1, 8, 24} x slot wait
    {0 s, 2 s faked} = 90 combinations, in one fixed toy task. One child per harness loops over all of
    them (both with the same environment): the base child (the base harness.py/telemetry.py from
    tests/base_harness/ first on sys.path, in a plugin-shaped copy) computes reuse_key/parts; the new child
    writes the combination's git config, then does the same while holding a slot and a run temp folder
    (the base harness never reads lightharness.slots, so only the new child rewrites that file). Oracle: for every combination the new
    harness's parts tree, tests, probes, config, approval, base, python, env equal the base harness's
    (version is the only part allowed to differ); the new part names are exactly the base's nine; and the
    new key is the same across all 90 combinations."""
    toy = T.clone(snaps["green"], tmp_path / "lab", tmp_path_factory.getbasetemp())
    machine = tmp_path / "machine"
    menv = machine_env(machine)
    roots = [machine_paths(machine)["temp_root"], tmp_path / "temp root with spaces", tmp_path / "tëmp-röot-测试"]
    for r in roots:
        r.mkdir(parents=True, exist_ok=True)
    slot_values = [None, 1, 2, 4, "abc"]
    combos = [{"temp_root": str(r), "cpus": cpus, "wait": wait,
               "gitconfig": GITCONFIG_BASE + (f"[lightharness]\n\tslots = {s}\n" if s is not None else "")}
              for r, s, cpus, wait in itertools.product(roots, slot_values, (1, 8, 24), (0, 2))]
    assert len(combos) == 90
    cpath = T.write_json(tmp_path / "combos.json", combos)
    env = toy.env()
    env.update(menv)
    # the base harness hashes its own plugin folder (PLUGIN_ROOT/scripts), so it runs from a plugin copy
    # whose harness.py and telemetry.py are the committed base files
    base_scripts = T.make_plugin(tmp_path / "base-plugin", "base") / "scripts"
    procs = {}
    for flavour, scripts in (("base", base_scripts), ("new", T.PLUGIN_SRC / "scripts")):
        procs[flavour] = subprocess.Popen(
            [sys.executable, "-c", CHILD, str(scripts), str(toy.task), str(cpath), str(tmp_path / f"{flavour}.json"),
             flavour], cwd=str(toy.task), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT)
    res = {}
    for flavour, p in procs.items():
        out, _ = p.communicate(timeout=600)
        assert p.returncode == 0, f"{flavour} child failed:\n{out.decode('utf-8', 'replace')[-3000:]}"
        res[flavour] = json.loads((tmp_path / f"{flavour}.json").read_text(encoding="utf-8"))
    base, new = res["base"], res["new"]
    assert len(base) == len(new) == 90
    diffs = []
    for c, b, n in zip(combos, base, new):
        label = f"root {Path(c['temp_root']).name!r}, slots {c['gitconfig'].split('slots = ')[-1].strip() if 'slots' in c['gitconfig'] else 'unset'}, cpus {c['cpus']}, wait {c['wait']}"
        if list(n["parts"]) != list(b["parts"]) or sorted(n["parts"]) != sorted(PART_NAMES):
            diffs.append(f"{label}: part names {list(n['parts'])} != {list(b['parts'])}")
            continue
        changed = [p for p in SAME_PARTS if n["parts"][p] != b["parts"][p]]
        if changed:
            diffs.append(f"{label}: parts differ from the base harness: {changed}")
    keys = {n["key"] for n in new}
    if len(keys) != 1:
        diffs.append(f"the new key takes {len(keys)} values across the 90 combinations")
    print(f"BR-11 sweep: {len(diffs)} of {len(combos)} combinations differ from the base harness")
    assert not diffs, f"{len(diffs)} differ; first ones:\n  " + "\n  ".join(diffs[:8])
