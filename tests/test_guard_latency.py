"""Issue #24 (guard latency): examples and behaviour tests. EX-1..EX-9, AC-2..AC-5, the BR-5 [sweep], the R-4
stdin fidelity test and Q-9's batched config read.

Every run happens in a temp fixture repo (never this repo: guard runs append telemetry), and every environment
variable goes through a Python env dict, never a shell command line (R-11).
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "plugins" / "light-harness" / "scripts"
BIN = HERE.parent / "plugins" / "light-harness" / "bin" / "light-harness"
for p in (str(SCRIPTS), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import guard_sh_scenarios as G  # noqa: E402
import hook_guard  # noqa: E402
import telemetry  # noqa: E402

SILENT = (0, "", "", "[]")
GIT_DISCOVERY = ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_CEILING_DIRECTORIES",
                 "GIT_DISCOVERY_ACROSS_FILESYSTEM")


@pytest.fixture
def fx(tmp_path_factory, tmp_path):
    """A private copy of the session's fixture (main checkout, five task worktrees, a no-task repo, outside)."""
    tpl = G.template(tmp_path_factory)
    return G.copy_fixture(tpl["root"], tmp_path / "fx")


def read_json(fx, sid, path, cwd=None, **extra) -> bytes:
    return G.compact(G.cc_payload(fx, sid, cwd or fx["T"]["Build"], "Read", {"file_path": str(path)},
                                  **extra)).encode("utf-8")


def edit_json(fx, sid, path, cwd=None) -> bytes:
    return G.compact(G.cc_payload(fx, sid, cwd or fx["T"]["Build"], "Edit",
                                  {"file_path": str(path), "old_string": "a", "new_string": "b"})).encode("utf-8")


def bash_json(fx, sid, cmd, cwd=None) -> bytes:
    return G.compact(G.cc_payload(fx, sid, cwd or fx["T"]["Build"], "Bash",
                                  {"command": cmd, "description": "x"})).encode("utf-8")


def session_json(fx, sid, cwd=None) -> bytes:
    return G.compact({"session_id": sid, "transcript_path": str(fx["root"] / "t.jsonl"),
                      "cwd": str(cwd or fx["M"]), "hook_event_name": "SessionStart", "source": "startup"}).encode()


def log_lines(log: Path) -> list:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def probes(log: Path) -> list:
    return [line for line in log_lines(log) if line.startswith("-c ")]


def guard(fx, stdin: bytes, env: dict, project=None) -> G.Outcome:
    return G.run_guard(G.LIVE_SCRIPTS, stdin, env, fx["events"], cwd=project or fx["M"])


def norm(fx, out, sid):
    return G.normalise(out, fx, sid)


def wrapped_env(fx, log: Path, **extra) -> dict:
    """The hook environment with a logging `python3` first on PATH (every probe and run is logged)."""
    wrap = G.wrap_dir(fx["root"] / "wrap")
    return G.guard_env(fx, PATH=G.path_with(wrap), FAKEPY_LOG=log, **extra)


def sh_command_v(env: dict, word="python3") -> str:
    r = subprocess.run([G.SH, "-c", f"command -v {word}"], env=env, capture_output=True, encoding="utf-8")
    assert r.returncode == 0 and r.stdout.strip(), f"no {word} on the test PATH"
    return r.stdout.strip()


def record_text(fx, sid) -> str:
    return (fx["state"] / "py" / sid).read_bytes().decode("utf-8")


# --- AC-4: the Read fast path (EX-1, EX-2, EX-3) ---------------------------------------------------------

def fast_env(fx):
    """A record pointing at a recording fake interpreter, and a recording python3 first on PATH."""
    fake = G.recording_python(fx["root"] / "fake" / "python3")
    G.write_record(fx, "fast", str(fake))
    log = fx["root"] / "mark"
    return wrapped_env(fx, log), log


def test_ac4_read_fast_path_examples(fx):
    """AC-4 EX-1 EX-2 EX-3 BR-4: with a recorded interpreter, a Read of `.tasks/build/README.md` exits 0 with no
    output and starts no Python (EX-1); `.env` and `.env.local` start Python and are blocked as `env-file` with
    the event recorded (EX-2); `.env.example` starts Python and exits 0 (EX-3). JSON the shell can't read
    plainly (spaced, `\\u002e`, nested or duplicated tool_name, notebook_path, `.ENV` in any case) goes to Python;
    other escapes (`\\/`, and `\\\\` in Windows paths) don't (Q-4)."""
    env, log = fast_env(fx)
    t = fx["T"]["Build"]

    def run(stdin):
        log.unlink(missing_ok=True)
        out = guard(fx, stdin, env)
        return norm(fx, out, "fast"), bool(log_lines(log))

    # EX-1, plus the same file in another task and an ordinary file with other escapes
    for path in (t / "README.md", fx["T"]["Draft"] / "README.md", t / "src" / "a.py"):
        assert run(read_json(fx, "fast", path)) == (SILENT, False), path
    slashed = read_json(fx, "fast", G.fwd(t / "README.md")).replace(b"/", b"\\/")
    assert run(slashed) == (SILENT, False)
    # EX-2
    for name in (".env", ".env.local"):
        (code, so, se, ev), started = run(read_json(fx, "fast", t / name))
        assert started and code == 2 and so == ""
        assert se == f"[light-harness] Blocked: {name} holds secrets and isn't read during a task; ask the human " \
                     "for what you need."
        assert [(e["event"], e["decision"], e["rule"], e["tool"], e["session"], e["repo"], e["actor"])
                for e in json.loads(ev)] == [("guard", "block", "env-file", "Read", "<S>", "proj", "agent")]
    # EX-3 and a name only Python can judge
    for name in (".env.example", ".ENV", "x.env"):
        assert run(read_json(fx, "fast", t / name)) == (SILENT, True), name
    # shapes the shell must hand to Python, all of an ordinary file (base allows each silently)
    readme = str(t / "README.md")
    plain = read_json(fx, "fast", readme).decode("utf-8")
    shapes = {
        "spaced": json.dumps(json.loads(plain)).encode(),
        "u_dot": plain.replace("README.md", "README\\u002emd").encode("utf-8"),
        "nested tool_name": plain.replace('"tool_input":{', '"tool_input":{"tool_name":"Read",').encode("utf-8"),
        "duplicated tool_name": plain.replace('"tool_use_id"', '"tool_name":"Read","tool_use_id"').encode("utf-8"),
        "notebook_path": plain.replace('"tool_input":{', '"tool_input":{"notebook_path":"x.ipynb",').encode(),
    }
    for name, stdin in shapes.items():
        assert run(stdin) == (SILENT, True), name
    # A duplicated tool_name that base reads as an Edit of a frozen file must still be blocked
    cfg = plain.replace(readme.replace("\\", "\\\\"), str(t / ".light-harness" / "config.json").replace("\\", "\\\\"))
    dup = cfg.replace('"tool_use_id"', '"tool_name":"Edit","tool_use_id"').encode("utf-8")
    (code, so, se, ev), started = run(dup)
    assert started and code == 2 and json.loads(ev)[0]["rule"] == "config-frozen"


# --- AC-2: EX-6 and EX-8 ---------------------------------------------------------------------------------

def test_ac2_ex6_no_python_blocks(fx):
    """AC-2 EX-6 BR-2: in a task repo with no Python on PATH and none recorded (or the recorded file deleted),
    a Read of README.md, an Edit and a Bash call each exit 2 with base's "Python 3.9+ not found" message. With a
    usable record and the same PATH, the same calls are judged by the recorded interpreter (BR-3)."""
    env = G.guard_env(fx, PATH=os.pathsep.join(G.tools_path(fx["root"])))
    t = fx["T"]["Build"]
    calls = [read_json(fx, "ex6", t / "README.md"), edit_json(fx, "ex6", t / "src" / "a.py"),
             bash_json(fx, "ex6", "git status")]
    gone = fx["root"] / "gone" / "python3"
    for record in (None, str(gone)):
        G.clear_record(fx, "ex6")
        if record:
            G.write_record(fx, "ex6", record)
        for stdin in calls:
            assert norm(fx, guard(fx, stdin, env), "ex6") == (2, "", G.NOT_FOUND, "[]"), (record, stdin[:200])
    G.write_record(fx, "ex6", G.REAL_PY)
    assert norm(fx, guard(fx, calls[0], env), "ex6") == SILENT
    assert norm(fx, guard(fx, calls[1], env), "ex6") == SILENT
    code, so, se, ev = norm(fx, guard(fx, calls[2], env), "ex6")
    assert code == 0 and json.loads(so)["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_ac2_ex8_no_task_exits_at_once(fx):
    """AC-2 EX-8 BR-2 BR-4: in a repo with no task, and in a folder outside any repo, every tool exits 0 with no
    output and no Python started, whatever is recorded. In the task repo the same Read (EX-1) also starts no
    Python, while an Edit does."""
    env, log = fast_env(fx)
    for where in (fx["N"], fx["O"]):
        G.write_record(fx, "fast", str(fx["root"] / "fake" / "python3"))
        for stdin in (read_json(fx, "fast", where / "a.py", cwd=where), edit_json(fx, "fast", where / "a.py", where),
                      bash_json(fx, "fast", "git reset --hard", where),
                      G.compact(G.cc_payload(fx, "fast", where, "Glob", {"pattern": "*"})).encode()):
            log.unlink(missing_ok=True)
            out = G.run_guard(G.LIVE_SCRIPTS, stdin, dict(env, CLAUDE_PROJECT_DIR=str(where)), None, cwd=where)
            assert (out.exit, out.stdout, out.stderr) == (0, "", ""), (where, stdin[:120])
            assert log_lines(log) == [], f"Python started outside a task repo: {log_lines(log)}"
    assert not (fx["N"] / ".git" / "light-harness").exists()
    log.unlink(missing_ok=True)
    assert norm(fx, guard(fx, read_json(fx, "fast", fx["T"]["Build"] / "README.md"), env), "fast") == SILENT
    assert log_lines(log) == []
    assert norm(fx, guard(fx, edit_json(fx, "fast", fx["T"]["Build"] / "src" / "a.py"), env), "fast") == SILENT
    assert len(log_lines(log)) == 1 and "hook_guard.py" in log_lines(log)[0]


# --- AC-3: the session's recorded interpreter (BR-3, EX-7, EX-9) --------------------------------------------

def run_session(fx, sid, env_file: Path, env: dict, project=None):
    project = project or fx["M"]
    return G.run_cmd([G.SH, G.fwd(G.LIVE_SCRIPTS / "session.sh")], session_json(fx, sid, project),
                     dict(env, CLAUDE_ENV_FILE=str(env_file), CLAUDE_PROJECT_DIR=str(project)), cwd=project)


def test_ac3_session_records_once(fx):
    """AC-3 EX-9 BR-3: session.sh, given compact SessionStart JSON in a task repo, probes once and records the
    winning interpreter's sys.executable in <common>/light-harness/py/<sid>, and appends `export
    LIGHT_HARNESS_PY='<command -v python3>'` to the env file (the PATH and session id lines stay). A later
    `light-harness status` with that variable and a guard call then run without another `python3 -c` probe.
    Outside a task repo: no record, no output, no probe, no LIGHT_HARNESS_PY. Unsafe session ids: no record."""
    log = fx["root"] / "pylog"
    env = wrapped_env(fx, log)
    env_file = fx["root"] / "envfile"
    out = run_session(fx, "abc-123", env_file, env)
    assert out.exit == 0
    assert out.stdout == (SCRIPTS.parent / "rules.md").read_bytes().decode("utf-8")   # the rules, as before
    assert len(probes(log)) == 1, log_lines(log)
    rec = record_text(fx, "abc-123")
    assert rec.rstrip("\n") == G.REAL_PY and "\r" not in rec
    lines = env_file.read_text(encoding="utf-8").splitlines()
    want_py = sh_command_v(env)
    assert f"export LIGHT_HARNESS_PY='{want_py}'" in lines, lines
    assert 'export LIGHT_HARNESS_SESSION_ID="abc-123"' in lines
    assert any(line.startswith("export PATH=") and "light-harness/bin:$PATH" in line for line in lines)
    assert not any("LIGHT_HARNESS_PY_ARGS" in line for line in lines)
    # EX-9: the CLI and the guard reuse it
    log.unlink()
    r = G.run_cmd([G.SH, G.fwd(BIN), "status"], b"", dict(env, LIGHT_HARNESS_PY=want_py), cwd=fx["T"]["Build"])
    assert r.exit == 0 and "Build" in r.stdout, r.stdout + r.stderr
    assert probes(log) == [] and len(log_lines(log)) == 1 and "harness.py status" in log_lines(log)[0]
    log.unlink()
    assert norm(fx, guard(fx, edit_json(fx, "abc-123", fx["T"]["Build"] / "src" / "a.py"), env), "abc-123") == SILENT
    assert log_lines(log) == []
    # Outside a task repo
    env_file2 = fx["root"] / "envfile2"
    out = run_session(fx, "abc-456", env_file2, env, project=fx["N"])
    assert (out.exit, out.stdout) == (0, "")
    assert probes(log) == [] and not (fx["N"] / ".git" / "light-harness").exists()
    assert not any("LIGHT_HARNESS_PY" in line for line in env_file2.read_text(encoding="utf-8").splitlines())
    # Unsafe session ids
    before = sorted(p.name for p in (fx["state"] / "py").iterdir())
    for sid in ("..", "a/b", 'x";rm', "a b", "x" * 129):
        out = run_session(fx, sid, fx["root"] / "envfile3", env)
        assert out.exit == 0
    assert sorted(p.name for p in (fx["state"] / "py").iterdir()) == before == ["abc-123"]
    assert not (fx["state"] / "a").exists() and not (fx["state"] / "b").exists()


def test_ac3_guard_uses_record(fx):
    """AC-3 BR-3 (Q-10): with a usable record, Read, Edit (allowed and blocked) and Bash run without any probe of
    python3/python/py; with none, the guard searches as before (a `-c` probe), writes the record for the
    session (sys.executable of the interpreter found), and the next call in that session doesn't probe."""
    log = fx["root"] / "pylog"
    env = wrapped_env(fx, log)
    G.write_record(fx, "g1", G.REAL_PY)
    t, draft = fx["T"]["Build"], fx["T"]["Draft"]
    assert norm(fx, guard(fx, read_json(fx, "g1", t / "README.md"), env), "g1") == SILENT
    assert norm(fx, guard(fx, edit_json(fx, "g1", t / "src" / "a.py"), env), "g1") == SILENT
    code, so, se, ev = norm(fx, guard(fx, edit_json(fx, "g1", draft / "src" / "a.py", draft), env), "g1")
    assert code == 2 and "status is Draft" in se and json.loads(ev)[0]["rule"] == "stage"
    code, so, se, ev = norm(fx, guard(fx, bash_json(fx, "g1", "git status"), env), "g1")
    assert code == 0 and json.loads(so)["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert log_lines(log) == [], f"the guard started python3 from PATH despite the record: {log_lines(log)}"
    # No record: the search, then a record for this session
    assert not (fx["state"] / "py" / "g2").exists()
    assert norm(fx, guard(fx, edit_json(fx, "g2", t / "src" / "a.py"), env), "g2") == SILENT
    assert len(probes(log)) == 1 and any("hook_guard.py" in line for line in log_lines(log)), log_lines(log)
    assert record_text(fx, "g2").rstrip("\n") == G.REAL_PY
    log.unlink()
    assert norm(fx, guard(fx, edit_json(fx, "g2", t / "src" / "a.py"), env), "g2") == SILENT
    assert log_lines(log) == []
    # An unsafe session id is never used as a file name
    assert norm(fx, guard(fx, edit_json(fx, "../g3", t / "src" / "a.py"), env), "../g3") == SILENT
    assert sorted(p.name for p in (fx["state"] / "py").iterdir()) == ["g1", "g2"]


def test_ac3_telemetry_uses_record(fx):
    """AC-3 BR-3: telemetry.sh with a usable record for the session starts no probe and runs no python3 from
    PATH; the events it writes equal those of a session with no record (which probes, as at base)."""
    log = fx["root"] / "pylog"
    env = wrapped_env(fx, log)
    G.write_record(fx, "tel-a", G.REAL_PY)

    def tel(sid):
        out = G.run_cmd([G.SH, G.fwd(G.LIVE_SCRIPTS / "telemetry.sh")], session_json(fx, sid), env, fx["events"],
                        cwd=fx["M"])
        assert (out.exit, out.stdout) == (0, "")
        return norm(fx, out, sid)[3]

    with_record = tel("tel-a")
    assert log_lines(log) == [], log_lines(log)
    without = tel("tel-b")
    assert len(probes(log)) == 1
    assert with_record == without
    ev = json.loads(with_record)
    assert [(e["event"], e["session"]) for e in ev] == [("session", "<S>")]


def test_ac3_cli_uses_record(tmp_path):
    """AC-3 BR-3 (Q-8, R-6): bin/light-harness with LIGHT_HARNESS_PY equal to `command -v python3` runs it with no
    probe; pointing elsewhere, or unset, it probes as base and runs PATH's python3 (never the variable's)."""
    a = G.wrap_dir(tmp_path / "a")
    other = G.script(tmp_path / "b" / "python3", 'printf \'B %s\\n\' "$*" >> "$FAKEPY_LOG"\n'
                                                 f'exec "{G.fwd(G.REAL_PY)}" "$@"\n')
    log = tmp_path / "pylog"
    env = G.git_env(tmp_path)
    env.update(PATH=G.path_with(a), FAKEPY_LOG=str(log))
    want = G.run_cmd([G.REAL_PY, str(SCRIPTS / "harness.py"), "version"], b"", env, cwd=tmp_path).stdout
    assert want.strip()
    r_word = sh_command_v(env)
    r_other = sh_command_v(dict(env, PATH=G.path_with(other.parent, rest=[])), "python3")

    def cli(extra):
        log.unlink(missing_ok=True)
        e = dict(env)
        e.pop("LIGHT_HARNESS_PY", None)
        e.update(extra)
        out = G.run_cmd([G.SH, G.fwd(BIN), "version"], b"", e, cwd=tmp_path)
        assert (out.exit, out.stdout) == (0, want), out.stderr
        return log_lines(log)

    lines = cli({"LIGHT_HARNESS_PY": r_word})
    assert len(lines) == 1 and lines[0].endswith("harness.py version") and not lines[0].startswith("-c"), lines
    for extra in ({"LIGHT_HARNESS_PY": r_word + "-elsewhere"}, {}, {"LIGHT_HARNESS_PY": r_other}):
        lines = cli(extra)
        assert len(lines) == 2 and lines[0].startswith("-c ") and lines[1].endswith("harness.py version"), (extra,
                                                                                                         lines)
        assert not any(line.startswith("B ") for line in lines)


def test_ac3_ex7_blocks_setting_py(fx):
    """AC-3 EX-7 BR-3: through guard.sh, `LIGHT_HARNESS_PY=/tmp/fake light-harness status` (and other commands that
    assign LIGHT_HARNESS_PY) exit 2 with the telemetry-settings message and a `telemetry-settings` guard event,
    in a task folder and in the main checkout; naming the variable without assigning it is judged as before."""
    env = G.guard_env(fx)
    G.write_record(fx, "ex7", G.REAL_PY)
    for cwd, task in ((fx["T"]["Build"], "build"), (fx["M"], None)):
        for cmd in G.EX7_CMDS:
            code, so, se, ev = norm(fx, guard(fx, bash_json(fx, "ex7", cmd, cwd), env), "ex7")
            assert (code, so, se) == (2, "", G.TELEMETRY_MSG), (cmd, code, se)
            e = json.loads(ev)
            assert [(x["event"], x["decision"], x["rule"], x["tool"], x["session"], x.get("task")) for x in e] == \
                [("guard", "block", "telemetry-settings", "Bash", "<S>", task)], (cmd, e)
    for cmd in ("echo LIGHT_HARNESS_PY", "LIGHT_HARNESS_PYTHON=x git status", "grep LIGHT_HARNESS_PY x.txt"):
        assert norm(fx, guard(fx, bash_json(fx, "ex7", cmd), env), "ex7") == SILENT, cmd


# --- AC-1 / R-4: stdin reaches hook_guard.py byte for byte --------------------------------------------------

def fidelity_corpus(fx, sid) -> list:
    t = str(fx["T"]["Build"])
    out = [bash_json(fx, sid, c) for c in G.FIDELITY_CMDS]
    out.append(G.compact(G.cc_payload(fx, sid, t, "Write", {"file_path": t + "/src/big.py",
                                                            "content": ("x = 'ü💥\\\\$`\"'\n" * 70_000)})).encode())
    head = f'{{"session_id":"{sid}","cwd":'.encode()
    out += [head + b'"\\\\server\\\\share","tool_name":"Edit","tool_input":{"file_path":"a\\\\b"}}\r\n',
            head + b'"x","raw":"tab\there cr\rhere $HOME `id` \\" \'q\' %s %n \\\\c"}',
            head + b'"x"}\nEOF\nEND\nJSON\nIN\n!\n',
            head + b'"x", "trailing": "newlines"}\n\n\n',
            head + "\"ü日本\",\" \":\"\U0001f4a5\"}".encode("utf-8"),
            head + b'"x","esc":"\\u0000\\u001f\\ud83d\\udca5\\b\\f"}']
    return out


def test_ac1_r4_stdin_fidelity(fx):
    """AC-1 R-4 BR-1: the bytes hook_guard.py receives equal the hook's stdin (up to trailing line ends, which
    `$(cat)` drops, with the CR before them on Git Bash, and JSON ignores): quotes, `$`, backticks, backslashes, `%`, `\\c`, CR, tab, unicode, U+2028,
    lines like EOF/END/JSON, an embedded newline, a 100 KB argument and a ~1.3 MB Write. Checked on the recorded
    run (heredoc) and on the fallback search (pipe)."""
    dump = fx["root"] / "dump"
    rec = G.script(fx["root"] / "dumper" / "python3", 'cat > "$FAKEPY_DUMP"\nexit 0\n')
    G.write_record(fx, "fid", str(rec))
    search = G.script(fx["root"] / "searchdir" / "python3",
                      f'if [ "$1" = "-c" ]; then exec "{G.fwd(G.REAL_PY)}" "$@"; fi\ncat > "$FAKEPY_DUMP"\nexit 0\n')
    bad = []
    for leg, sid, front in (("recorded", "fid", []), ("search", "fid-none", [search.parent])):
        env = G.guard_env(fx, PATH=G.path_with(*front), FAKEPY_DUMP=dump)
        for stdin in fidelity_corpus(fx, sid):
            dump.unlink(missing_ok=True)
            G.clear_record(fx, "fid-none")
            out = guard(fx, stdin, env)
            got = dump.read_bytes() if dump.exists() else None
            if out.exit != 0 or got is None or got.rstrip(b"\r\n") != stdin.rstrip(b"\r\n"):
                bad.append(f"{leg}: exit {out.exit}, {len(stdin)} bytes in, "
                           f"{'nothing' if got is None else len(got)} received; input {stdin[:100]!r}; "
                           f"received {(got or b'')[:100]!r}")
    assert not bad, f"{len(bad)} inputs changed on the way to hook_guard.py:\n" + "\n".join(bad[:10])


# --- AC-5: the common dir is passed, not looked up (BR-5); lazy urllib (BR-6); one config read (Q-9) ---------

class SubprocessLog:
    def __init__(self, monkeypatch):
        self.calls = []
        real = subprocess.run

        def run(args, *a, **k):
            self.calls.append([str(x) for x in (args if isinstance(args, (list, tuple)) else [args])])
            return real(args, *a, **k)

        monkeypatch.setattr(subprocess, "run", run)

    def common_lookups(self):
        return [c for c in self.calls if "--git-common-dir" in c]


def isolate_env(monkeypatch, root: Path):
    for k in list(os.environ):
        if k.startswith("GIT_") or k.startswith("LIGHT_HARNESS_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(root / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def git_run(*args, cwd, env=None):
    r = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_ac5_no_common_dir_lookup(fx, monkeypatch):
    """AC-5 BR-5: with LIGHT_HARNESS_GIT_COMMON set to the repo's common dir, `active()` on paths in that repo (a
    task file, a file in another task, a main-checkout file, a missing file) returns it without running `git
    rev-parse --git-common-dir`; for another repo's path, a nested repo in the task folder, or with the
    variable unset, it runs git as before and returns git's answer."""
    isolate_env(monkeypatch, fx["root"])
    nested = fx["T"]["Build"] / "nested"
    nested.mkdir()
    git_run("init", "-q", cwd=nested)
    (nested / ".git" / "light-harness").mkdir()
    log = SubprocessLog(monkeypatch)
    monkeypatch.setenv("LIGHT_HARNESS_GIT_COMMON", str(fx["common"]))
    t = fx["T"]["Build"]
    for p in (t / "src" / "a.py", fx["T"]["Draft"] / "README.md", fx["M"] / "src" / "x.py", t / "new" / "z.py", t):
        log.calls.clear()
        got = hook_guard.active(p)
        assert got is not None and hook_guard.H.same_path(got, fx["common"]), (p, got)
        assert log.common_lookups() == [], (p, log.calls)
    log.calls.clear()
    assert hook_guard.active(fx["N"] / "a.py") is None and log.common_lookups(), log.calls
    log.calls.clear()
    got = hook_guard.active(nested / "a.py")
    assert got is not None and hook_guard.H.same_path(got, nested / ".git") and log.common_lookups()
    monkeypatch.delenv("LIGHT_HARNESS_GIT_COMMON")
    log.calls.clear()
    got = hook_guard.active(t / "src" / "a.py")
    assert got is not None and hook_guard.H.same_path(got, fx["common"]) and log.common_lookups()


SITECUSTOMIZE = '''import json, os, subprocess
_log = os.environ.get("SUBPROC_LOG")
if _log:
    with open(_log, "a", encoding="utf-8") as f:
        f.write("started\\n")
    _init = subprocess.Popen.__init__
    def _logged(self, args, *a, **k):
        with open(_log, "a", encoding="utf-8") as f:
            f.write(json.dumps([str(x) for x in (args if isinstance(args, (list, tuple)) else [args])]) + "\\n")
        _init(self, args, *a, **k)
    subprocess.Popen.__init__ = _logged
'''


def test_ac5_guard_end_to_end_no_common_dir_lookup(fx):
    """AC-5 BR-5 (Q-7): end to end through guard.sh with a recorded interpreter, a Read of `.env` (blocked), an
    allowed Edit and Bash `git status` run no `git rev-parse --git-common-dir`, neither in the shell (a logging
    git first on PATH) nor in hook_guard.py (a sitecustomize logging every subprocess)."""
    sc = fx["root"] / "sc"
    sc.mkdir()
    (sc / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
    gitlog, pylog = fx["root"] / "gitlog", fx["root"] / "pylog"
    G.script(fx["root"] / "gitwrap" / "git", f'printf \'%s\\n\' "$*" >> "$GITLOG"\nexec "{G.fwd(shutil.which("git"))}" "$@"\n')
    env = G.guard_env(fx, PATH=G.path_with(fx["root"] / "gitwrap"), PYTHONPATH=sc, GITLOG=gitlog, SUBPROC_LOG=pylog)
    G.write_record(fx, "e2e", G.REAL_PY)
    t = fx["T"]["Build"]
    code, so, se, ev = norm(fx, guard(fx, read_json(fx, "e2e", t / ".env"), env), "e2e")
    assert code == 2 and json.loads(ev)[0]["rule"] == "env-file"
    assert norm(fx, guard(fx, edit_json(fx, "e2e", t / "src" / "a.py"), env), "e2e") == SILENT
    code, so, se, ev = norm(fx, guard(fx, bash_json(fx, "e2e", "git status"), env), "e2e")
    assert code == 0 and json.loads(so)["hookSpecificOutput"]["permissionDecision"] == "allow"
    py_lines = log_lines(pylog)
    assert "started" in py_lines, py_lines   # the logging sitecustomize ran
    lookups = [x for x in log_lines(gitlog) + py_lines if "--git-common-dir" in x]
    assert lookups == [], lookups


IMPORT_CHILD = r'''
import json, os, subprocess, sys
calls = []
_init = subprocess.Popen.__init__
def _logged(self, args, *a, **k):
    calls.append([str(x) for x in (args if isinstance(args, (list, tuple)) else [args])])
    _init(self, args, *a, **k)
subprocess.Popen.__init__ = _logged
before = sys.dont_write_bytecode
sys.path.insert(0, sys.argv[1])
import hook_guard
print(json.dumps({"calls": calls, "root_left": "HARNESS_ROOT" in os.environ, "before": before,
                  "after": sys.dont_write_bytecode}))
'''


def test_ac5_import_runs_no_git(fx, tmp_path):
    """AC-5 R-7 R-8: importing hook_guard (HARNESS_ROOT unset, cwd in a task worktree) runs no git at all, leaves
    HARNESS_ROOT unset and sys.dont_write_bytecode as it was, and with PYTHONDONTWRITEBYTECODE=1 still writes
    __pycache__/harness.*.pyc next to harness.py."""
    copy = tmp_path / "scripts"
    shutil.copytree(G.LIVE_SCRIPTS, copy, ignore=shutil.ignore_patterns("__pycache__"))
    env = G.git_env(fx["root"])
    assert env["PYTHONDONTWRITEBYTECODE"] == "1" and "HARNESS_ROOT" not in env
    r = subprocess.run([G.REAL_PY, "-c", IMPORT_CHILD, str(copy)], cwd=fx["T"]["Build"], env=env,
                       capture_output=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout.strip().splitlines()[-1])
    assert got == {"calls": [], "root_left": False, "before": True, "after": True}, got
    assert list((copy / "__pycache__").glob("harness.*.pyc")), "no harness .pyc written"


TELEMETRY_CHILD = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import telemetry
lazy = "urllib.request" not in sys.modules
if len(sys.argv) > 2:
    common = Path(sys.argv[2])
    telemetry.emit(common, "guard", cwd=str(common.parent), task="t", session="s1", actor="agent",
                   decision="block", rule="stage", tool="Edit")
print(json.dumps({"lazy": lazy, "loaded_after": "urllib.request" in sys.modules}))
'''


def telemetry_child(env, *args):
    r = subprocess.run([G.REAL_PY, "-c", TELEMETRY_CHILD, str(G.LIVE_SCRIPTS), *map(str, args)], env=env,
                       capture_output=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_ac5_br6_lazy_urllib(tmp_path):
    """AC-5 BR-6: a fresh `import telemetry` doesn't import urllib.request."""
    env = G.git_env(tmp_path)
    assert telemetry_child(env) == {"lazy": True, "loaded_after": False}


def test_ac5_br6_otlp_still_posts(fx):
    """AC-5 BR-6: with an OTLP endpoint set, `emit` still posts the event to <endpoint>/v1/logs (body, headers),
    and urllib.request is imported only then."""
    got = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            got.append((self.path, dict(self.headers), self.rfile.read(n)))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        env = {k: v for k, v in G.git_env(fx["root"]).items() if "proxy" not in k.lower()}
        env.update(NO_PROXY="127.0.0.1,localhost", LIGHT_HARNESS_OTLP_ENDPOINT=f"http://127.0.0.1:{srv.server_port}",
                   LIGHT_HARNESS_OTLP_HEADERS="X-Token=abc")
        res = telemetry_child(env, fx["common"])
    finally:
        srv.shutdown()
    assert res == {"lazy": True, "loaded_after": True}
    assert len(got) == 1, got
    path, headers, body = got[0]
    assert path == "/v1/logs" and headers.get("X-Token") == "abc"
    rec = json.loads(body)["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    attrs = {a["key"]: list(a["value"].values())[0] for a in rec["attributes"]}
    assert rec["body"] == {"stringValue": "guard"}
    assert attrs["light_harness.rule"] == "stage" and attrs["session.id"] == "s1" and attrs["light_harness.task"] == "t"


def test_ac5_q9_config_batch_equals_get(tmp_path, monkeypatch):
    """AC-5 (Q-9): `_config_all(cwd)` reads every lightharness.* key in one git call and agrees with `git config
    --get` per key (multi-valued keys: last wins; mixed-case names; a value from include.path; values with
    spaces and `=`); unscoped `git_config` reads from it with no further git call; no config gives {} and "";
    `set_engineer` clears the cache."""
    isolate_env(monkeypatch, tmp_path)
    (tmp_path / "gitconfig").write_text("", encoding="utf-8")
    repo = tmp_path / "r"
    repo.mkdir()
    git_run("init", "-q", cwd=repo)
    inc = tmp_path / "inc.cfg"
    inc.write_text("[lightharness]\n\totlpHeaders = a=b, c=d\n\ttelemetry = off\n", encoding="utf-8")
    git_run("config", "--add", "lightharness.engineer", "Alice", cwd=repo)
    git_run("config", "--add", "lightharness.engineer", "Bob Two", cwd=repo)
    git_run("config", "lightHarness.otlpEndpoint", "http://collector:4318", cwd=repo)
    git_run("config", "include.path", G.fwd(inc), cwd=repo)
    git_run("config", "lightharness.telemetry", "on", cwd=repo)
    git_run("config", "lightharness.humanGapCap", "90 m", cwd=repo)
    git_run("config", "other.key", "x", cwd=repo)
    keys = ["lightharness.engineer", "lightharness.otlpEndpoint", "lightHarness.OTLPHEADERS", "lightharness.telemetry",
            "lightharness.humanGapCap", "lightharness.missing"]
    want = {}
    for k in keys:
        r = subprocess.run(["git", "config", "--get", k], cwd=repo, capture_output=True, encoding="utf-8")
        want[k] = r.stdout.strip() if r.returncode == 0 else ""
    # the include comes after the [lightharness] section git config wrote, so its telemetry = off wins
    assert want["lightharness.engineer"] == "Bob Two" and want["lightharness.telemetry"] == "off"
    telemetry.git_config.cache_clear()
    telemetry._config_all.cache_clear()
    log = SubprocessLog(monkeypatch)
    allk = telemetry._config_all(str(repo))
    assert len(log.calls) == 1 and log.calls[0][:2] == ["git", "config"], log.calls
    assert {k: allk.get(k.lower(), "") for k in keys} == want
    assert not any(k.startswith("other.") for k in allk) and all(k == k.lower() for k in allk)
    assert [telemetry.git_config(k, str(repo)) for k in keys] == [want[k] for k in keys]
    assert len(log.calls) == 1, log.calls
    assert telemetry.engineer(repo) == "Bob Two" and telemetry.enabled(repo) is False
    # no config at all
    bare = tmp_path / "bare"
    bare.mkdir()
    git_run("init", "-q", cwd=bare)
    assert telemetry._config_all(str(bare)) == {} and telemetry.git_config("lightharness.engineer", str(bare)) == ""
    assert telemetry._config_all(str(tmp_path / "nowhere")) == {}
    # set_engineer clears both caches
    assert telemetry.set_engineer(bare, "Carol", repo_only=True) == ""
    assert telemetry.engineer(bare) == "Carol" and telemetry.engineer_scope(bare) == "this repo"


# --- BR-5 [sweep] ---------------------------------------------------------------------------------------

BR5_LOCATIONS = ["main", "task", "dotgit", "dotgit_state", "dotgit_worktree", "nested", "submodule", "broken",
                 "empty_dotgit", "outside"]


def br5_layout(root: Path) -> dict:
    env = G.git_env(root)
    (root / "gitconfig").write_text("[user]\n\tname = F\n\temail = f@x\n", encoding="utf-8")
    m = root / "m"
    m.mkdir(parents=True)
    git_run("init", "-q", cwd=m, env=env)
    (m / "a.py").write_text("x\n", encoding="utf-8")
    git_run("add", "-A", cwd=m, env=env)
    git_run("commit", "-qm", "i", cwd=m, env=env)
    git_run("worktree", "add", "-q", str(m / ".tasks" / "t"), "-b", "task/t", cwd=m, env=env)
    (m / ".git" / "light-harness").mkdir()
    t = m / ".tasks" / "t"
    git_run("init", "-q", str(t / "nested"), cwd=root, env=env)
    (root / "gitdirs").mkdir()
    git_run("init", "-q", "--separate-git-dir", str(root / "gitdirs" / "sub"), str(t / "sub"), cwd=root, env=env)
    (t / "sub" / ".git").unlink()   # git marks it hidden on Windows, which can't be opened for writing
    (t / "sub" / ".git").write_text("gitdir: " + os.path.relpath(root / "gitdirs" / "sub", t / "sub").replace("\\", "/")
                                    + "\n", encoding="utf-8")
    (t / "broken").mkdir()
    (t / "broken" / ".git").write_text("garbage\n", encoding="utf-8")
    (root / "empty" / ".git").mkdir(parents=True)
    (root / "outside").mkdir()
    other = root / "other"
    other.mkdir()
    git_run("init", "-q", cwd=other, env=env)
    dirs = {"main": m, "task": t, "dotgit": m / ".git", "dotgit_state": m / ".git" / "light-harness",
            "dotgit_worktree": m / ".git" / "worktrees" / "t", "nested": t / "nested", "submodule": t / "sub",
            "broken": t / "broken", "empty_dotgit": root / "empty", "outside": root / "outside"}
    for d in dirs.values():
        (d / "x.py").write_text("x\n", encoding="utf-8")
    return {"dirs": dirs, "other": other / ".git", "env": env}


def git_common_at(p: Path, env: dict):
    d = hook_guard.existing(p)
    r = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=d, env=env, capture_output=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0 or not r.stdout.strip():
        return None
    q = Path(r.stdout.strip())
    return (q if q.is_absolute() else d / q).resolve()


def test_ac5_br5_sweep_common_dir(tmp_path, monkeypatch):
    """AC-5 BR-5 [sweep]: whenever `common_from_env` takes the shortcut, its answer equals `git rev-parse
    --git-common-dir` at that path. Full cross product: location {main checkout, task worktree, .git/,
    .git/light-harness/, .git/worktrees/t/, a nested repo in the task folder, a submodule-style gitfile with a
    relative gitdir, a broken gitfile, an empty .git folder, outside any repo} x {existing, missing file} x
    passed value {correct (git's answer, or the nearest .git entry where git has none), another repo's, unset} x
    env {none, GIT_DIR set} (120). The shortcut must be taken for main-checkout and task files with the correct
    value, and never with the variable unset or GIT_DIR set."""
    root = tmp_path.resolve()
    lay = br5_layout(root)
    isolate_env(monkeypatch, root)
    combos = [(loc, exists, passed, genv) for loc in BR5_LOCATIONS for exists in (True, False)
              for passed in ("correct", "other", "unset") for genv in ("none", "GIT_DIR")]
    assert len(combos) == 120
    bad, taken = [], 0
    for loc, exists, passed, genv in combos:
        d = lay["dirs"][loc]
        p = d / ("x.py" if exists else "missing/zz.py")
        clean = dict(lay["env"])
        truth = git_common_at(p, clean)
        value = {"correct": truth or (d / ".git"), "other": lay["other"], "unset": None}[passed]
        monkeypatch.delenv("LIGHT_HARNESS_GIT_COMMON", raising=False)
        monkeypatch.delenv("GIT_DIR", raising=False)
        envd = dict(clean)
        if value is not None:
            monkeypatch.setenv("LIGHT_HARNESS_GIT_COMMON", str(value))
        if genv == "GIT_DIR":
            monkeypatch.setenv("GIT_DIR", str(lay["other"]))
            envd["GIT_DIR"] = str(lay["other"])
        got = hook_guard.common_from_env(p)
        label = f"{loc} {'existing' if exists else 'missing'} passed={passed} env={genv}"
        if got is None:
            if loc in ("main", "task") and passed == "correct" and genv == "none":
                bad.append(f"{label}: shortcut not taken")
            continue
        taken += 1
        if passed == "unset" or genv == "GIT_DIR":
            bad.append(f"{label}: shortcut taken ({got})")
            continue
        git_says = git_common_at(p, envd)
        if git_says is None or not hook_guard.H.same_path(got, git_says):
            bad.append(f"{label}: shortcut gave {got}, git gives {git_says}")
    print(f"BR-5 sweep: shortcut taken in {taken} of {len(combos)} cases; {len(bad)} wrong")
    assert not bad, f"{len(bad)} of {len(combos)} cases wrong:\n" + "\n".join(bad)


def test_ac5_br5_walk_common_layouts(tmp_path):
    """AC-5 BR-5: `walk_common` (no git) finds what git finds: a `.git` folder, a worktree's gitfile + commondir,
    a submodule-style relative gitdir with no commondir; None for a broken gitfile; outside any repo it finds no
    repo of its own."""
    root = tmp_path.resolve()
    lay = br5_layout(root)
    for loc in ("main", "task", "nested", "submodule"):
        for name in ("x.py", "missing/zz.py"):
            p = lay["dirs"][loc] / name
            got = hook_guard.walk_common(p)
            want = git_common_at(p, lay["env"])
            assert got is not None and hook_guard.H.same_path(got, want), (loc, name, got, want)
    assert hook_guard.walk_common(lay["dirs"]["broken"] / "x.py") is None
    outside = lay["dirs"]["outside"] / "x.py"
    assert git_common_at(outside, lay["env"]) is None
    assert hook_guard.walk_common(outside) is None


def test_ac7_selftest_keeps_guard_checks():
    """AC-7: tests/selftest.sh keeps LF endings and its guard loops, unsets the new LIGHT_HARNESS_PY variables
    (so a developer's session value can't leak into the run), and lists EX-7 among the commands the guard
    must block. The suite and the selftest themselves are AC-7's evidence; this pins their guard coverage."""
    raw = (HERE / "selftest.sh").read_bytes()
    assert b"\r\n" not in raw
    text = raw.decode("utf-8")
    unset = next(line for line in text.splitlines() if line.startswith("unset "))
    assert {"LIGHT_HARNESS_PY", "LIGHT_HARNESS_PY_ARGS"} <= set(unset.split())
    assert '"LIGHT_HARNESS_PY=/tmp/fake light-harness status"' in text
    assert 'check \'[ "$(hook "$M" "$(bash_json "$M" "$c")")" = 2 ]\' "guard allowed: $c"' in text
    assert 'check \'[ "$(hook "$M" "$(bash_json "$M" "$c")")" = 0 ]\' "guard blocked: $c"' in text
