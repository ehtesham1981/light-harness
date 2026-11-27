"""Issue #24 (guard latency): the guard keeps every decision. BR-1 [sweep] (AC-1), BR-2 [sweep] (AC-2) and BR-4
[sweep] (AC-4) run the live `guard.sh` end to end in fixture repos and compare with the base-commit outcomes in
tests/golden/guard_sh_base.json (written once by tests/gen_guard_sh_golden.py from `light-harness baseline`
copies; this file never reads .light-harness-base/).

Environment variables go through Python env dicts, never a shell command line (R-11).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import guard_sh_scenarios as G  # noqa: E402

SILENT = (0, "", "", "[]")
BLOCKED_NO_PY = (2, "", G.NOT_FOUND, "[]")
NUL_WARNING = "warning: command substitution: ignored null byte in input"


def table() -> dict:
    g = json.loads(G.GOLDEN.read_text(encoding="utf-8"))
    assert G.PLATFORM in g, f"tests/golden/guard_sh_base.json has no {G.PLATFORM} table; run gen_guard_sh_golden.py"
    return g[G.PLATFORM]


def decode(t: dict, o: list) -> tuple:
    s = t["strings"]
    return (o[0], s[o[1]], s[o[2]], s[o[3]])


def br1_golden():
    t = table()
    scs = G.br1_scenarios()
    assert t["br1"]["count"] == len(scs) == len(t["br1"]["outcomes"])
    assert t["br1"]["sha256"] == G.sha256(scs)
    return scs, {G.key(sc): decode(t, t["br1"]["outcomes"][i]) for i, sc in enumerate(scs)}


def files_key(tool, target, status="Build", shape="cc"):
    return G.key({"part": "files" if shape == "cc" else "shapes", "tool": tool, "target": target, "status": status,
                  "shape": shape})


def bash_key(cmd, cwd="task", shape="cc"):
    return G.key({"part": "bash", "cmd": cmd, "cwd": cwd, "shape": shape})


def strictness(o: tuple) -> int:
    """0 silent pass, 1 allow, 2 ask, 3 block (or a crash/other code)."""
    if o[0] == 0:
        try:
            d = json.loads(o[1])["hookSpecificOutput"]["permissionDecision"] if o[1] else None
        except (ValueError, KeyError, TypeError):
            d = None
        return {None: 0, "allow": 1, "ask": 2}.get(d, 2)
    return 3


def show(o: tuple) -> str:
    ev = json.loads(o[3])
    rules = [e.get("rule") for e in ev]
    return f"exit {o[0]}, stdout {o[1][:90]!r}, stderr {o[2][:300]!r}, events {rules}"


def make_pool(tmp_path_factory, name: str) -> G.Pool:
    tpl = G.template(tmp_path_factory)
    return G.Pool(tpl["root"], tmp_path_factory.mktemp(name))


# --- AC-1 / BR-1 -----------------------------------------------------------------------------------------

def test_ac1_br1_sweep_golden_in_step():
    """AC-1 BR-1 [sweep]: (the sweep's oracle is in step) the golden has one base outcome per BR-1 scenario, for
    exactly the scenarios guard_sh_scenarios builds, and known base facts sit at the right keys: EX-2 (`.env`,
    `.env.local` blocked as `env-file`), EX-3 (`.env.example` allowed), EX-4 (Edit in Draft blocked as `stage`),
    EX-5 (`git status` allowed with a JSON decision) and EX-7 (allowed at base)."""
    scs, at = br1_golden()
    assert len(G.scenarios_files()) == 6 * 23 * 5 + 6 * 23 * 9 == 1932
    n_cmds = len(G.bash_commands())
    assert n_cmds >= 195 and len(G.scenarios_bash()) == n_cmds * 3 * 2
    assert len({G.key(sc) for sc in scs}) == len(scs)
    for name in (".env", ".env.local"):
        tid = {".env": "env", ".env.local": "env_local"}[name]
        code, so, se, ev = at[files_key("Read", tid)]
        assert (code, so) == (2, ""), (name, code, se)
        assert se == f"[light-harness] Blocked: {name} holds secrets and isn't read during a task; ask the human " \
                     "for what you need."
        assert [(e["event"], e["decision"], e["rule"], e["tool"], e.get("session")) for e in json.loads(ev)] == \
            [("guard", "block", "env-file", "Read", "<S>")]
    for tid in ("env_example", "env_sample", "env_template", "ENV", "xenv", "envrc", "src", "space_unicode"):
        assert at[files_key("Read", tid)] == SILENT, tid
    assert at[files_key("Read", "env_example_local")][0] == 2
    code, so, se, ev = at[files_key("Edit", "src", "Draft")]
    assert code == 2 and "status is Draft" in se and json.loads(ev)[0]["rule"] == "stage"
    assert at[files_key("Edit", "src", "Build")] == SILENT
    code, so, se, ev = at[bash_key(G.EX5)]
    assert code == 0 and json.loads(so)["hookSpecificOutput"]["permissionDecision"] == "allow" and ev == "[]"
    for cwd in G.BASH_CWDS:
        for shape in G.BASH_SHAPES:
            assert at[bash_key(G.EX7, cwd, shape)] == SILENT, (cwd, shape)
    ref = at[bash_key(G.EX7_REFERENCE)]
    assert ref[0] == 2 and ref[2] == G.TELEMETRY_MSG and json.loads(ref[3])[0]["rule"] == "telemetry-settings"
    for status in G.STATUSES:
        for tool in G.FILE_TOOLS:
            assert at[files_key(tool, "outside", status)] == SILENT
            assert at[files_key(tool, "notask", status)] == SILENT
    # Q-6: the crashes the allowance is for exist only in malformed shapes, never in the Claude Code shape
    assert not [sc for sc in scs if sc.get("shape") == "cc" and at[G.key(sc)][0] not in (0, 2)]


def br1_expected(sc: dict, base: tuple, at: dict) -> set:
    """The outcomes BR-1 allows for a scenario: the base outcome, except EX-7 (now blocked exactly as the
    base blocked LIGHT_HARNESS_TELEMETRY=0 in the same place and shape) and Q-6 (a base crash may exit 0)."""
    if sc["part"] == "bash" and G.is_ex7(sc["cmd"]) and sc["cwd"] != "outside":
        return {at[bash_key(G.EX7_REFERENCE, sc["cwd"], sc["shape"])]}
    if base[0] == 1:
        return {base, SILENT}
    return {base}


def test_ac1_br1_sweep_matches_base(tmp_path_factory):
    """AC-1 BR-1 [sweep]: every BR-1 scenario keeps its base-commit outcome through the new guard.sh: exit code,
    decision JSON, stderr and the normalised `guard` telemetry event. The space: (1) tool {Read, Edit, Write,
    MultiEdit, NotebookEdit, Glob} x 23 targets (ordinary, every .env variant incl. `.ENV`, `x.env`, `.envrc`,
    `.env.example.local`, a spaced/unicode path, a relative path, the contract's owned and unowned parts,
    config.json, junit.xml, .light-harness-base/, a test, .claude/agents/, .git/light-harness/, the main
    checkout, outside any repo, a repo with no task) x 5 statuses, Claude Code shape (690); (2) the same in Build
    x 9 JSON shapes (spaced, reversed keys, pretty, `\\/`, `\\u002e`, other separators, no tool_input, null
    tool_input, truncated) (1,242); (3) ~210 Bash commands (selftest guard lists, EX-5, EX-7 and near misses,
    120 from the guard corpus with Random(24), 25 byte-fidelity commands) x cwd {task, main, outside} x
    {Claude Code, spaced}. Every scenario runs with a usable record; parts (1) and compact (3) run again with no
    record (the fallback search). Allowed changes: EX-7 (blocked as `telemetry-settings`) and Q-6 (a base crash
    may exit 0). Both directions are reported."""
    scs, at = br1_golden()
    pool = make_pool(tmp_path_factory, "br1pool")
    jobs = [(i, "record") for i in range(len(scs))]
    jobs += [(i, "none") for i, sc in enumerate(scs)
             if sc["part"] == "files" or (sc["part"] == "bash" and sc["shape"] == "cc")]

    def run(fx, job):
        i, mode = job
        if mode == "record":
            G.write_record(fx, G.SID, G.REAL_PY)
        else:
            G.clear_record(fx, G.SID)
        try:
            return G.run_br1(G.LIVE_SCRIPTS, fx, scs[i])
        finally:
            G.clear_record(fx, G.SID)

    t0 = time.time()
    results = pool.map(run, jobs)
    looser, stricter, other = [], [], []
    for (i, mode), now in zip(jobs, results):
        sc = scs[i]
        base = at[G.key(sc)]
        ok = br1_expected(sc, base, at)
        if now in ok:
            continue
        want = next(iter(ok))
        line = f"[{mode}] {G.key(sc)[:200]}\n    expected {show(want)}\n    got      {show(now)}"
        (looser if strictness(now) < strictness(want) else stricter if strictness(now) > strictness(want)
         else other).append(line)
    diffs = looser + stricter + other
    print(f"BR-1 sweep: {len(diffs)} of {len(jobs)} runs differ ({len(looser)} less strict than allowed, "
          f"{len(stricter)} stricter, {len(other)} other) in {time.time() - t0:.0f}s")
    assert not diffs, (f"{len(diffs)} of {len(jobs)} runs differ: {len(looser)} less strict, {len(stricter)} "
                       f"stricter, {len(other)} other; first 12:\n" + "\n".join(diffs[:12]))


# --- AC-1 / BR-1 part (4): unencodable paths (RV-9's class) ------------------------------------------------

def rv9_golden() -> tuple:
    g = json.loads(G.GOLDEN_RV9.read_text(encoding="utf-8"))
    assert G.PLATFORM in g, f"tests/golden/guard_sh_rv9_base.json has no {G.PLATFORM} table; run gen_guard_sh_golden.py"
    t = g[G.PLATFORM]
    scs = G.scenarios_unencodable()
    assert t["count"] == len(scs) == len(t["outcomes"]), (t["count"], len(scs))
    assert t["sha256"] == G.sha256(scs)
    return scs, {G.key(sc): t["outcomes"][i] for i, sc in enumerate(scs)}


def unenc(tool, status, char="0000", pos=None, cmd=None) -> dict:
    sc = {"part": "unenc", "tool": tool, "char": char, "status": status}
    sc.update({"cmd": cmd} if tool == "Bash" else {"pos": pos})
    return sc


def unenc_run(fx, sc, mode) -> list:
    if mode == "record":
        G.write_record(fx, G.SID, G.REAL_PY)
    else:
        G.clear_record(fx, G.SID)
    try:
        return G.run_unencodable(G.LIVE_SCRIPTS, fx, sc)
    finally:
        G.clear_record(fx, G.SID)


def test_ac1_br1_rv9_golden_in_step(tmp_path_factory):
    """AC-1 BR-1 (RV-9, F-15): the unencodable-path golden has one base outcome per scenario that
    scenarios_unencodable() builds (count and hash), and RV-9's base facts sit at the right keys: a Read of
    `.env.local\\u0000` is blocked as `env-file` (Draft and Build); a human-only `light-harness` command with
    `--task build` and a NUL in the cwd is blocked as `human-only`; `light-harness status --task build` there gets
    an explicit allow; `git status` there crashed base hook_guard.py (exit 1, kept: no allowed change). The new
    guard.sh gives exactly those outcomes for those cases, with a usable record and with none."""
    scs, at = rv9_golden()
    n_chars = 4 if G.PLATFORM == "posix" else 1
    assert len(scs) == n_chars * 2 * (3 * 5 + 5) and len({G.key(sc) for sc in scs}) == len(scs)
    facts = {}
    for status in G.UNENC_STATUSES:
        facts[G.key(unenc("Read", status, pos="after_env_local"))] = [2, None, "env-file"]
        facts[G.key(unenc("Bash", status, cmd=G.HUMAN_ONLY_CMD))] = [2, None, "human-only"]
        facts[G.key(unenc("Bash", status, cmd="light-harness status --task build"))] = [0, "allow", None]
        facts[G.key(unenc("Bash", status, cmd="git status"))] = [1, None, None]
    for k, want in facts.items():
        assert at[k] == want, (k, at[k], want)
    fx = G.copy_fixture(G.template(tmp_path_factory)["root"], tmp_path_factory.mktemp("rv9facts") / "fx")
    by_key = {G.key(sc): sc for sc in scs}
    got = {(k, mode): unenc_run(fx, by_key[k], mode) for k in facts for mode in ("record", "none")}
    wrong = [f"[{mode}] {k}: base {facts[k]}, now {v}" for (k, mode), v in got.items() if v != facts[k]]
    assert not wrong, f"{len(wrong)} of {len(got)} RV-9 fact runs differ:\n" + "\n".join(wrong)


def test_ac1_br1_sweep_unencodable_paths(tmp_path_factory):
    """AC-1 BR-1 [sweep] (part 4, RV-9's class, F-15, R-15): calls whose path or cwd can't be encoded keep their
    base-commit outcome (exit code, permissionDecision, rule). Full cross product (scenarios_unencodable()):
    Read, Edit and Write `file_path` x bad character {\\u0000 everywhere; \\ud800, \\udfff, \\udc80 on POSIX} x
    position {after `.env.local`, after `.env`, inside `src/a?.py`, in a folder `s?/a.py`, in a `.env?/x` folder}
    x status {Draft, Build}; Bash with cwd `<task>/s?` x {a human-only `light-harness` command with --task build,
    `git status`, `light-harness status --task build`, `git push`, `cp x .light-harness/contract.md`} x status
    {Draft, Build}. Every case runs with a usable record and again with none (fallback search): 80 runs on
    Windows, 320 on POSIX. Oracle: tests/golden/guard_sh_rv9_base.json (base guard.sh, from the baseline
    export). No allowed change: base's exit 1 crashes stay exit 1, base's blocks stay blocks. Both directions are
    reported."""
    scs, at = rv9_golden()
    pool = make_pool(tmp_path_factory, "rv9pool")
    jobs = [(i, mode) for i in range(len(scs)) for mode in ("record", "none")]
    t0 = time.time()
    results = pool.map(lambda fx, job: unenc_run(fx, scs[job[0]], job[1]), jobs)

    def strict3(o):   # exit 2 blocks; any other non-zero exit is a non-blocking error to Claude Code
        if o[0] == 2:
            return 3
        return {None: 0, "allow": 1, "ask": 2}.get(o[1], 2) if o[0] == 0 else 0

    looser, other = [], []
    for (i, mode), now in zip(jobs, results):
        base = at[G.key(scs[i])]
        if now != base:
            line = f"[{mode}] {G.key(scs[i])}: base {base}, now {now}"
            (looser if strict3(now) < strict3(base) else other).append(line)
    diffs = looser + other
    print(f"BR-1 part (4) sweep: {len(diffs)} of {len(jobs)} runs differ ({len(looser)} less strict) in "
          f"{time.time() - t0:.0f}s")
    assert not diffs, (f"{len(diffs)} of {len(jobs)} runs differ from base ({len(looser)} less strict); first 12:\n"
                       + "\n".join(diffs[:12]))


# --- AC-2 / BR-2 -----------------------------------------------------------------------------------------

BR2_TOOLS = ["Read", "Edit", "Bash", "Glob"]
BR2_TARGETS = ["task_file", "task_env", "notask", "outside"]
# not_executable, unreadable and py_readonly need POSIX file modes (Git Bash doesn't follow chmod for them)
BR2_STATES = (["usable", "deleted"] + (["not_executable"] if G.PLATFORM == "posix" else []) +
              ["stub_exit_1", "stub_exit_127", "stub_exit_3", "other_os", "not_recorded", "empty", "crlf", "modified"] +
              (["unreadable", "py_readonly"] if G.PLATFORM == "posix" else []) +
              ["stub_stdout_exit1", "stub_judge_exit1", "stub_killed"])
BR2_PATHS = ["py39", "old_only", "none"]
OTHER_OS = "/usr/bin/python3" if G.PLATFORM == "nt" else "C:\\Python39\\python.exe"


def br2_setup(fx: dict) -> dict:
    """Interpreters for BR-2's states, in the fixture (made once per fixture)."""
    d = fx["root"] / "br2"
    if not (d / "done").exists():
        G.recording_python(d / "ok" / "python3")
        G.recording_python(d / "newer" / "python3", mtime_offset=+1000)
        for code in (1, 127, 3):
            G.stub(d / f"stub{code}" / "python3", code)
        # RV-11: a record that writes to stdout and then fails; one that judges (runs the real hook_guard.py) and
        # then fails; one killed by SIGKILL after writing to stdout and stderr
        G.script(d / "junk1" / "python3", "printf 'junk\\n'\nexit 1\n")
        G.script(d / "judge1" / "python3", f'"{G.fwd(G.REAL_PY)}" "$@"\nexit 1\n')
        G.script(d / "killed" / "python3", "printf 'partial\\n'\nprintf 'noise\\n' >&2\nkill -9 $$\n")
        ne = d / "noexec" / "python3"
        ne.parent.mkdir(parents=True, exist_ok=True)
        ne.write_text("not an interpreter\n", encoding="utf-8")
        ne.chmod(0o644)
        G.wrap_dir(d / "wrap")
        G.old_python_dir(d / "old")
        (d / "done").write_text("", encoding="utf-8")
    return {"dir": d, "tools": G.tools_path(fx["root"])}


def br2_record(fx: dict, d: Path, state: str):
    content = {"usable": str(d / "ok" / "python3"), "deleted": str(d / "gone" / "python3"),
               "not_executable": str(d / "noexec" / "python3"), "stub_exit_1": str(d / "stub1" / "python3"),
               "stub_exit_127": str(d / "stub127" / "python3"), "stub_exit_3": str(d / "stub3" / "python3"),
               "other_os": OTHER_OS, "not_recorded": None, "empty": "", "crlf": str(d / "ok" / "python3") + "\r\n",
               "modified": str(d / "newer" / "python3"), "unreadable": str(d / "ok" / "python3"),
               "py_readonly": str(d / "ok" / "python3"), "stub_stdout_exit1": str(d / "junk1" / "python3"),
               "stub_judge_exit1": str(d / "judge1" / "python3"), "stub_killed": str(d / "killed" / "python3")}[state]
    br2_restore(fx)
    G.clear_record(fx, "br2")
    if content is not None:
        rec = G.write_record(fx, "br2", content)
        if state == "unreadable":    # RV-12: the record exists but can't be read (POSIX)
            rec.chmod(0)
        if state == "py_readonly":   # F-18: py/ is read-only, so the recorded run's .run.* files can't be made
            rec.parent.chmod(0o555)


def br2_restore(fx: dict):
    """Undo br2_record's file modes, so the next run in this fixture can write the record."""
    py = fx["state"] / "py"
    if py.is_dir():
        py.chmod(0o755)
        if (py / "br2").exists():
            (py / "br2").chmod(0o644)


def br2_stdin(fx: dict, tool: str, target: str) -> tuple:
    """(stdin, project dir, BR-1 golden key or None). Task targets reuse BR-1's Build scenarios byte for byte
    (apart from the session id), so their expected outcome is the golden one."""
    if target in ("task_file", "task_env"):
        if tool == "Bash":
            sc = {"part": "bash", "cmd": "git status" if target == "task_file" else "cat .env", "cwd": "task",
                  "shape": "cc"}
        else:
            sc = {"part": "files", "tool": tool, "target": "src" if target == "task_file" else "env",
                  "status": "Build", "shape": "cc"}
        return G.render(sc, fx, "br2"), fx["M"], G.key(sc)
    where = fx["N"] if target == "notask" else fx["O"]
    ti = {"command": "git status"} if tool == "Bash" else G.tool_input(tool, "src", str(where / "a.py"), "Build")
    if tool == "Glob":
        ti = {"file_path": str(where / "a.py")}
    return G.compact(G.cc_payload(fx, "br2", where, tool, ti)).encode("utf-8"), where, None


def events_set(o: tuple) -> frozenset:
    return frozenset(json.dumps(e, sort_keys=True, ensure_ascii=False) for e in json.loads(o[3]))


def br2_view(o: tuple, events: bool = True) -> tuple:
    """What BR-2's oracle compares: exit code, stdout and normalised stderr, and the guard events as a set (R-2's
    fallback may log a judged-then-failed call twice), or no events."""
    return (o[0], o[1], o[2], events_set(o) if events else None)


def br2_expected(tool, target, state, path, gkey, at) -> tuple:
    """(allowed outcomes, compare events?) for one BR-2 run in a task repo."""
    golden = at[gkey]
    if state == "usable" or path == "py39":
        return {br2_view(golden)}, True
    if state == "py_readonly":
        # a usable record whose run can't capture its output: the fast path still decides an ordinary Read;
        # anything else may fall back to the search (which finds no 3.9+ here) but never exits 2 unexplained
        if tool == "Read" and target == "task_file":
            return {br2_view(golden)}, True
        return {br2_view(golden), br2_view(BLOCKED_NO_PY)}, True
    if state.startswith("stub") and tool == "Read" and target == "task_file":
        return {br2_view(SILENT), br2_view(BLOCKED_NO_PY)}, True   # Q-5
    # no usable interpreter: exit 2, base's message, nothing on stdout. A record that judged before failing may
    # have logged its event, so events are compared only when the record can't have judged.
    events = state != "stub_judge_exit1"
    return {br2_view(BLOCKED_NO_PY, events)}, events


def test_ac2_br2_sweep_fail_closed(tmp_path_factory):
    """AC-2 BR-2 [sweep] EX-6 EX-8: the guard fails closed. Full cross product: tool {Read, Edit, Bash, Glob} x
    target {ordinary file in a task, `.env` in a task, a repo with no task, a folder outside any repo} x
    interpreter state {usable record, recorded then deleted, not executable (POSIX), stubs exiting 1 / 127 / 3,
    another OS's path, not recorded, empty record, record ending in CRLF, interpreter modified after the record;
    re-plan: record unreadable (mode 000, POSIX, RV-12), py/ read-only so the .run.* files can't be created
    (POSIX, F-18), a stub that prints `junk` and exits 1, a stub that runs the real hook_guard.py and then exits 1
    (RV-11), a stub killed by SIGKILL after writing stdout and stderr} x PATH {python3 3.9+, only old Pythons,
    none} (768 runs on POSIX, 624 on Windows). Oracle (property): in a task repo with no usable interpreter and no
    3.9+ on PATH, exit 2, base's exact message on stderr and empty stdout; outside a task repo, exit 0 with no
    output and no interpreter started; otherwise the BR-1 golden outcome, compared on exit code, stdout and
    normalised stderr (so a failing record never adds stdout or stderr), with events as a set. Allowed change
    (Q-5): an ordinary Read with an unchanged executable record that can't start (the stubs) may exit 0."""
    _, at = br1_golden()
    pool = make_pool(tmp_path_factory, "br2pool")
    jobs = [(tool, target, state, path) for tool in BR2_TOOLS for target in BR2_TARGETS for state in BR2_STATES
            for path in BR2_PATHS]
    assert len(BR2_STATES) == (16 if G.PLATFORM == "posix" else 13)
    assert len(jobs) == (768 if G.PLATFORM == "posix" else 624)

    def run(fx, job):
        tool, target, state, path = job
        s = br2_setup(fx)
        d = s["dir"]
        br2_record(fx, d, state)
        try:
            stdin, project, gkey = br2_stdin(fx, tool, target)
            front = {"py39": [d / "wrap"], "old_only": [d / "old"], "none": []}[path]
            env_path = G.path_with(*front) if path == "py39" else G.path_with(*front, rest=s["tools"])
            log = fx["root"] / "br2.log"
            log.unlink(missing_ok=True)
            env = G.guard_env(fx, project=project, PATH=env_path, FAKEPY_LOG=log)
            out = G.run_guard(G.LIVE_SCRIPTS, stdin, env, fx["events"], cwd=project)
            started = log.exists() and log.read_text(encoding="utf-8").strip() != ""
            return G.normalise(out, fx, "br2"), started, gkey
        finally:
            br2_restore(fx)

    t0 = time.time()
    results = pool.map(run, jobs)
    bad = []
    for (tool, target, state, path), (now, started, gkey) in zip(jobs, results):
        label = f"{tool} {target} {state} {path}"
        if gkey is None:
            if started:
                bad.append(f"{label}: an interpreter started outside a task repo")
            if now != SILENT:
                bad.append(f"{label}: expected {show(SILENT)}, got {show(now)}")
            continue
        ok, events = br2_expected(tool, target, state, path, gkey, at)
        if br2_view(now, events) not in ok:
            bad.append(f"{label}: expected one of {sorted(str(o[:3]) for o in ok)}, got {show(now)}")
    print(f"BR-2 sweep: {len(bad)} of {len(jobs)} runs wrong in {time.time() - t0:.0f}s")
    assert not bad, f"{len(bad)} of {len(jobs)} runs wrong; first 12:\n" + "\n".join(bad[:12])


# --- AC-4 / BR-4 -----------------------------------------------------------------------------------------

def test_ac4_br4_sweep_fast_path_is_safe(tmp_path_factory):
    """AC-4 BR-4 [sweep]: the shell's Read fast path decides only inputs the base hook_guard.py allows silently.
    3,000 hook stdins from random.Random(24) over the BR-4 input space (key order, whitespace, escapes `\\\\`
    `\\/` `\\"` `\\u002e` `\\u0065` and \\u-escaped non-ASCII in file_path/cwd, 24 basenames incl. `.ENV`, `.Env`,
    `x.env`, `.envrc`, `.env.`, `.env.example.local`, `.env.EXAMPLE`, unicode, spaces and quotes, tool_name
    {Read, read, Edit, Write, NotebookEdit, Bash, missing, duplicated either way, nested in tool_input}, value
    types of tool_input/file_path/cwd, notebook_path, truncation, appended/prepended text, BOM, NUL, garbage).
    The record points at a recording fake interpreter (and a recording python3 is first on PATH), so a run that
    starts no Python took the fast path. Oracle: base hook_guard.py per input (golden `br4` table). Property:
    fast path taken => base exit 0, no stdout, no event. Every input: outcome = base, except Q-6 (a base crash
    may exit 0; raw NUL input may now be judged, which may only block or ask, and bash's "ignored null byte"
    warning on stderr is ignored)."""
    t = table()
    specs = G.scenarios_shapes()
    assert t["br4"]["count"] == len(specs) == len(t["br4"]["outcomes"]) == G.N_BR4
    assert t["br4"]["sha256"] == G.sha256(specs)
    pool = make_pool(tmp_path_factory, "br4pool")

    def run(fx, spec):
        d = fx["root"] / "br4"
        if not (d / "fake" / "python3").exists():
            G.recording_python(d / "fake" / "python3")
            G.wrap_dir(d / "wrap")
        G.write_record(fx, G.SID, str(d / "fake" / "python3"))
        log = d / "mark"
        log.unlink(missing_ok=True)
        stdin = G.render_shape(spec, fx)
        env = G.guard_env(fx, PATH=G.path_with(d / "wrap"), FAKEPY_LOG=log)
        out = G.run_guard(G.LIVE_SCRIPTS, stdin, env, fx["events"], cwd=fx["M"])
        nul = b"\x00" in stdin
        if nul:   # bash (Git Bash) warns that $(cat) dropped the NUL: output noise, not a decision (Q-6)
            out.stderr = "\n".join(x for x in out.stderr.splitlines() if not x.endswith(NUL_WARNING))
        return G.normalise(out, fx, G.br4_sid(spec)), log.exists(), nul

    t0 = time.time()
    results = pool.map(run, specs)
    unsafe, diffs, taken = [], [], 0
    for spec, (now, started, nul) in zip(specs, results):
        base = decode(t, t["br4"]["outcomes"][spec["i"]])
        label = json.dumps({k: v for k, v in spec.items()}, ensure_ascii=False)[:220]
        if not started:
            taken += 1
            if base != SILENT or now != SILENT:
                unsafe.append(f"{label}\n    fast path taken; base {show(base)}; now {show(now)}")
                continue
        ok = now == base or (base[0] == 1 and now == SILENT) or (nul and strictness(now) >= max(2, strictness(base)))
        if not ok:
            diffs.append(f"{label}\n    base {show(base)}\n    now  {show(now)}")
    print(f"BR-4 sweep: fast path taken for {taken} of {len(specs)} inputs; {len(unsafe)} unsafe; "
          f"{len(diffs)} other differences from base; {time.time() - t0:.0f}s")
    assert not unsafe, f"{len(unsafe)} fast-path allows of inputs the base didn't allow silently; first 10:\n" + \
        "\n".join(unsafe[:10])
    assert not diffs, f"{len(diffs)} of {len(specs)} inputs differ from base; first 10:\n" + "\n".join(diffs[:10])
    assert taken >= 100, f"the fast path was taken for only {taken} inputs: the sweep doesn't exercise it"
