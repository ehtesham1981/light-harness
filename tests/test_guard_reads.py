"""The guard's Bash `.light-harness/` rule (issue #23): read-only commands on the task's contract and config
are allowed, every write stays blocked, and the block message points at the Read tool and
`light-harness show`. AC-1, AC-2, AC-3; BR-1, BR-2, BR-3, BR-4, BR-5, BR-7; EX-1..EX-16; R-1, R-2, R-4, R-6;
Q-2, Q-3, Q-4, Q-5.

`bash_rules` is run in-process with `block`/`decide` patched to raise (tests/guard_scenarios.py)."""
from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "plugins" / "light-harness" / "scripts"
for p in (str(SCRIPTS), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import hook_guard  # noqa: E402
import guard_scenarios as GS  # noqa: E402

C = ".light-harness/contract.md"
NOT_BLOCKED = (["pass"], ["allow"])


def message(name: str) -> str:
    """The BR-5 / §8.2 block message for the task `name`."""
    return (".light-harness/ holds the task's contract and config. Read the contract with the Read tool or "
            f"`light-harness show --task {name}`. Edit contract.md with the Edit tool, and change status with "
            "`light-harness set-status`.")


def assert_blocked(cmd: str, task="t"):
    out, msg = GS.judge(hook_guard, cmd, task)
    assert out == ["block", "harness-files"], f"{cmd!r} gave {out}, expected a harness-files block"
    assert msg == message(task), f"{cmd!r}: block message {msg!r}"


# --- AC-1: reads are allowed ---------------------------------------------------------------------

EXAMPLE_READS = [
    ("EX-1", 'grep -n "AC-1" .light-harness/contract.md'),
    ("EX-2", 'grep -nE "AC-1|AC-2" .light-harness/contract.md'),
    ("EX-3", "grep -n AC .light-harness/contract.md 2>/dev/null"),
    ("EX-4", "sed -n '/8.1/,/8.2/p' .light-harness/contract.md"),
    ("EX-5", "awk '/8.1/' .light-harness/contract.md"),
    ("EX-6", "git diff -- .light-harness/contract.md"),
    ("EX-7a", "git log -p -- .light-harness/contract.md"),
    ("EX-7b", "git show HEAD:.light-harness/contract.md"),
    ("EX-8", "sed -n 1,20p .light-harness/contract.md 2>&1 | head"),
]


@pytest.mark.parametrize("ex,cmd", EXAMPLE_READS, ids=[e for e, _ in EXAMPLE_READS])
def test_ac1_reads_allowed(ex, cmd):
    """AC-1 (EX-1..EX-8; BR-1, BR-2, BR-3): each example read is not blocked, and neither is the same read
    with its errors sent to /dev/null (BR-2 holds for every read)."""
    for c in (cmd, cmd + " 2>/dev/null"):
        out, msg = GS.judge(hook_guard, c)
        assert out in NOT_BLOCKED, f"{ex}: {c!r} gave {out}: {msg}"


def test_ac1_git_reads_preapproved():
    """AC-1 EX-6 EX-7: `git diff|log -- .light-harness/…` now pass harness-files and so match the existing
    SAFE_SEGMENT `git (status|diff|log|show|add)` pre-approval (§7.1), exactly as for any other file."""
    assert GS.outcome(hook_guard, "git diff -- .light-harness/contract.md") == ["allow"]
    assert GS.outcome(hook_guard, "git log -p -- .light-harness/contract.md") == ["allow"]
    assert GS.outcome(hook_guard, "git show HEAD:.light-harness/config.json") == ["allow"]
    # A redirect or pipe in the command still keeps the prompt (unchanged prompt-skip rule)
    assert GS.outcome(hook_guard, "git diff -- .light-harness/contract.md 2>/dev/null") == ["pass"]


READ_VARIANTS = [
    ("BR-1 single-quoted pipe", "grep -n 'AC-1|AC-2' .light-harness/contract.md"),
    ("BR-1 quoted ;", 'grep -n "a;b" .light-harness/contract.md'),
    ("BR-1 quoted &&", "grep -n 'a && b' .light-harness/contract.md"),
    ("BR-1 quoted ||", 'grep -n "a || b" .light-harness/config.json'),
    ("BR-1 quoted newline", "grep -n 'a\nb' .light-harness/contract.md"),
    ("BR-1 escaped quote in dquotes", 'grep -n "a\\"|b" .light-harness/contract.md'),
    ("BR-1 two reads chained", "cat .light-harness/contract.md && grep x .light-harness/config.json 2>/dev/null"),
    ("BR-2 &>/dev/null", "grep -q AC .light-harness/contract.md &>/dev/null"),
    ("BR-2 >>/dev/null", "grep AC .light-harness/contract.md >>/dev/null"),
    ("BR-2 >&2", "grep AC .light-harness/contract.md >&2"),
    ("BR-2 1>&2", "grep AC .light-harness/contract.md 1>&2"),
    ("BR-2 >/dev/null 2>&1", "grep -q AC .light-harness/contract.md >/dev/null 2>&1"),
    ("BR-2 2>/dev/null then pipe", "grep -c AC .light-harness/contract.md 2>/dev/null | head -1"),
    ("BR-3 sed -n -e p", "sed -n -e p .light-harness/contract.md"),
    ("BR-3 sed -E -n range", "sed -E -n '/a/,/b/p' .light-harness/contract.md"),
    ("BR-3 sed --quiet $p", "sed --quiet '$p' .light-harness/contract.md"),
    ("BR-3 sed s///gp", "sed -n 's/AC-/ac-/gp' .light-harness/contract.md"),
    ("BR-3 sed several commands", "sed -n '1,5p;$p' .light-harness/contract.md"),
    ("BR-3 sed q", "sed 20q .light-harness/contract.md"),
    ("BR-3 sed --expression=", "sed -n --expression=1p .light-harness/config.json"),
    ("BR-3 gawk -F:", "gawk -F: '/x/' .light-harness/contract.md"),
    ("BR-3 awk -v print", "awk -v n=3 'NR<=n {print $1}' .light-harness/contract.md"),
    ("BR-3 awk --posix", "awk --posix '/AC/ {print}' .light-harness/config.json"),
    ("BR-3 git --no-pager log", "git --no-pager log -p -- .light-harness/contract.md"),
    ("BR-3 git show HEAD:", "git show HEAD:.light-harness/contract.md | head -50"),
    ("BR-3 git diff stat", "git diff --stat HEAD~1 -- .light-harness/config.json"),
    ("Q-3 quoted >", "grep '>' .light-harness/contract.md"),
    ("Q-3 escaped pipe", "grep a\\|b .light-harness/contract.md"),
    ("R-4 Windows path", "cat .light-harness\\contract.md 2>/dev/null"),
    ("task folder path", "sed -n '/8.1/,/8.2/p' .tasks/alpha/.light-harness/contract.md"),
]


@pytest.mark.parametrize("why,cmd", READ_VARIANTS, ids=[w for w, _ in READ_VARIANTS])
def test_ac1_read_variants_allowed(why, cmd):
    """AC-1 BR-1 BR-2 BR-3 (Q-3): quote-aware segments, /dev/null and fd-dup redirects, and the sed, awk and
    git read shapes are not blocked by harness-files."""
    out, msg = GS.judge(hook_guard, cmd)
    assert out in NOT_BLOCKED, f"{why}: {cmd!r} gave {out}: {msg}"


# --- AC-2: writes stay blocked -------------------------------------------------------------------

EXAMPLE_WRITES = [
    ("EX-9", "sed -i 's/a/b/' .light-harness/contract.md"),
    ("EX-9 -ni", "sed -ni 's/a/b/' .light-harness/contract.md"),
    ("EX-9 -i.bak", "sed -i.bak 's/a/b/' .light-harness/contract.md"),
    ("EX-9 --in-place", "sed --in-place 's/a/b/' .light-harness/contract.md"),
    ("EX-10", "awk -i inplace '{print}' .light-harness/contract.md"),
    ("EX-11 >", "echo x > .light-harness/contract.md"),
    ("EX-11 >>", "echo x >> .light-harness/contract.md"),
    ("EX-12", "cat x | tee .light-harness/contract.md"),
    ("EX-13 cp", "cp x .light-harness/contract.md"),
    ("EX-13 mv", "mv x .light-harness/"),
    ("EX-14", "python -c \"open('.light-harness/contract.md','w')\""),
    ("EX-15", "awk '{print > \".light-harness/contract.md\"}' .light-harness/contract.md"),
    ("EX-16", "sed -n p .light-harness/contract.md > out.txt"),
]


@pytest.mark.parametrize("ex,cmd", EXAMPLE_WRITES, ids=[e for e, _ in EXAMPLE_WRITES])
def test_ac2_writes_blocked(ex, cmd):
    """AC-2 (EX-9..EX-16; BR-4, BR-5): each write is blocked with rule harness-files and the BR-5 message."""
    assert_blocked(cmd)


WRITE_HOLES = [
    # R-2: sed options in any form or position, and sed commands that write or run
    ("sed -i after the file", "sed -n p .light-harness/contract.md -i"),
    ("sed quoted -i", 'sed -n "-i" .light-harness/contract.md'),
    ("sed quoted -i with script", "sed '-i' s/a/b/ .light-harness/contract.md"),
    ("sed --in=", "sed --in=bak s/a/b/ .light-harness/contract.md"),
    ("sed -in", "sed -in p .light-harness/contract.md"),
    ("sed -s -i", "sed -s -i s/a/b/ .light-harness/contract.md"),
    ("sed -e then -i", "sed -e p -i .light-harness/contract.md"),
    ("sed w command", "sed -n 'w .light-harness/contract.md' x"),
    ("sed s///w", "sed 's/a/b/w x' .light-harness/contract.md"),
    ("sed W command", "sed -n 'W x' .light-harness/contract.md"),
    ("sed e command", "sed '1e touch x' .light-harness/contract.md"),
    ("sed s///e", "sed 's/a/b/e' .light-harness/contract.md"),
    ("sed r command", "sed '1r x' .light-harness/contract.md"),
    ("sed -f", "sed -f s.sed .light-harness/contract.md"),
    ("sed non-/ delimiter", "sed 's|a|b|w x' .light-harness/contract.md"),
    ("sed unknown option", "sed --follow-symlinks -n p .light-harness/contract.md"),
    # R-2: awk options and programs that write or run
    ("awk -iinplace", "awk -iinplace '{print}' .light-harness/contract.md"),
    ("awk --include=inplace", "awk --include=inplace '{print}' .light-harness/contract.md"),
    ("awk -l inplace", "awk -l inplace '{print}' .light-harness/contract.md"),
    ("awk -f", "awk -f p.awk .light-harness/contract.md"),
    ("gawk -E", "gawk -E p.awk .light-harness/contract.md"),
    ("awk @include", "awk '@include \"inplace\"; {print}' .light-harness/contract.md"),
    ("awk @load", "gawk '@load \"filefuncs\"; {print}' .light-harness/contract.md"),
    ("awk printf >", "awk '{printf \"x\" > \"f\"}' .light-harness/contract.md"),
    ("awk print >>", "awk '{print >> \"f\"}' .light-harness/contract.md"),
    ("awk getline", "awk 'BEGIN{\"sh\" | getline}' .light-harness/contract.md"),
    ("awk print |", "awk '{print | \"sh\"}' .light-harness/contract.md"),
    ("awk |&", "gawk '{print |& \"sh\"}' .light-harness/contract.md"),
    ("awk system", "awk 'BEGIN{system(\"x\")}' .light-harness/contract.md"),
    # R-2: git options that write or run
    ("git diff --output=", "git diff --output=.light-harness/contract.md"),
    ("git log --outp=", "git log --outp=x -- .light-harness/contract.md"),
    ("git diff --ext-diff", "git diff --ext-diff -- .light-harness/contract.md"),
    ("git -c diff.external", "git -c diff.external=x diff -- .light-harness/contract.md"),
    ("GIT_EXTERNAL_DIFF=", "GIT_EXTERNAL_DIFF=x git diff -- .light-harness/contract.md"),
    ("git -C before diff", "git -C . diff -- .light-harness/contract.md"),
    ("git add", "git add .light-harness/contract.md"),
    ("git apply", "git apply .light-harness/patch.diff"),
    ("git show redirected", "git show HEAD:.light-harness/contract.md > .light-harness/contract.md"),
    # BR-2: redirects that write
    (">|", "cat x >| .light-harness/contract.md"),
    ("&> file", "cat x &> .light-harness/contract.md"),
    ("&>> file", "cat x &>> .light-harness/contract.md"),
    ("2> file", "grep x .light-harness/contract.md 2> err.txt"),
    ("2>/dev/nullx", "grep x .light-harness/contract.md 2>/dev/nullx"),
    ("<>", "cat .light-harness/contract.md <> x"),
    ("> >(tee …)", "cat x > >(tee .light-harness/contract.md)"),
    ("exec 3>", "exec 3>.light-harness/contract.md"),
    ("bash -c", "bash -c \"sed -i s/a/b/ .light-harness/contract.md\""),
    ("sh -c", "sh -c 'cat x > .light-harness/contract.md'"),
    ("python3 -", "python3 - .light-harness/contract.md"),
    ("dd of=", "dd if=x of=.light-harness/contract.md"),
    ("R-4 Windows path write", "sed -i s/a/b/ .light-harness\\contract.md"),
    # R-1: quoting a naive splitter would misread, hiding a write after a real separator
    ("escaped dquotes around a real pipe",
     'grep \\" .light-harness/contract.md | sed -i s/a/b/ .light-harness/contract.md \\"'),
    ("unbalanced dquote", 'grep \\" .light-harness/contract.md | sed -i s/a/b/ .light-harness/contract.md "'),
    ("$'\\'' ansi-c quote",
     "grep $'\\'' .light-harness/contract.md | sed -i s/a/b/ .light-harness/contract.md # '"),
    ("$\"…\" locale quote", 'grep $"x" .light-harness/contract.md; sed -i s/a/b/ .light-harness/contract.md'),
    ("apostrophe in a comment",
     "cat .light-harness/contract.md # it's\nsed -i s/a/b/ .light-harness/contract.md # '"),
    ("apostrophe in a heredoc body",
     "cat <<EOF\nit's\nEOF\nsed -i s/a/b/ .light-harness/contract.md # '"),
    ("${x:-…} with quotes",
     "cat ${x:-'\"'} .light-harness/contract.md | sed -i s/a/b/ .light-harness/contract.md '\"'"),
    ("backslash-newline before -i", "sed -n p \\\n-i .light-harness/contract.md"),
    ("backslash-newline in the option", "sed -\\\ni s/a/b/ .light-harness/contract.md"),
    # Q-4: the command word is judged raw
    ("Q-4 env prefix", "FOO=1 grep x .light-harness/contract.md"),
    ("Q-4 path to cat", "/bin/cat .light-harness/contract.md"),
    ("Q-4 quoted word", '"cat" .light-harness/contract.md'),
    ("Q-4 escaped word", "\\cat .light-harness/contract.md"),
    ("Q-4 subshell", "(cat .light-harness/contract.md)"),
    ("unknown word", "frob .light-harness/contract.md"),
]


@pytest.mark.parametrize("why,cmd", WRITE_HOLES, ids=[w for w, _ in WRITE_HOLES])
def test_ac2_write_holes(why, cmd):
    """AC-2 BR-3 BR-4 (R-1, R-2, Q-4): sed/awk/git forms that write or run a program, write redirects,
    interpreters, and quoting a naive splitter would misread are all blocked with harness-files and the BR-5
    message."""
    assert_blocked(cmd)


SUBSTITUTION_HOLES = [
    # Allowed at base and they run a write (Q-2)
    ("$(…)", 'grep "$(sed -i s/a/b/ .light-harness/contract.md)" .light-harness/contract.md'),
    ("backticks", "cat `sed -i s/a/b/ .light-harness/contract.md`"),
    ("<(…)", "cat <(sed -i s/a/b/ .light-harness/contract.md)"),
    (">(…)", "grep x .light-harness/contract.md >(sed -i s/a/b/ .light-harness/contract.md)"),
    ("lone &", "cat .light-harness/contract.md & sed -i s/a/b/ .light-harness/contract.md"),
    ("lone & without spaces", "cat .light-harness/contract.md&sed -i s/a/b/ .light-harness/contract.md"),
    # Newly blocked shapes BR-6 lists as an allowed change
    ("$(…) in a read path", "cat $(git rev-parse --show-toplevel)/.light-harness/contract.md"),
    ("$(…) around the path", "grep x $(echo .light-harness/contract.md)"),
    ("lone & then a non-read", "cat .light-harness/contract.md & echo .light-harness/x"),
]


@pytest.mark.parametrize("why,cmd", SUBSTITUTION_HOLES, ids=[w for w, _ in SUBSTITUTION_HOLES])
def test_ac2_br7_substitution_and_lone_ampersand_blocked(why, cmd):
    """AC-2 BR-7 (Q-2): a command naming .light-harness/ with command or process substitution is blocked, and
    a lone `&` separates segments like `;`, so a write after it is judged on its own."""
    assert_blocked(cmd)


def test_br7_lone_ampersand_between_reads_allowed():
    """BR-7 BR-1 AC-1: a lone `&` is a separator, not a write; two reads either side of it stay allowed, and
    `&&`, `&>/dev/null` and `2>&1` are not mistaken for a lone `&`."""
    for cmd in ("cat .light-harness/contract.md & grep x .light-harness/config.json 2>/dev/null",
                "sed -n 1p .light-harness/contract.md & awk '/x/' .light-harness/config.json",
                "sed -n 1p .light-harness/contract.md &>/dev/null && grep x .light-harness/contract.md 2>&1"):
        out, msg = GS.judge(hook_guard, cmd)
        assert out in NOT_BLOCKED, f"{cmd!r} gave {out}: {msg}"


# --- AC-5: the end-to-end self-test carries every example ----------------------------------------

SELFTEST = HERE / "selftest.sh"
LOOP_RE = re.compile(r"^for c in (.*?); do\n  check '\[ \"\$\(hook \"\$M\" \"\$\(bash_json \"\$M\" \"\$c\"\)\"\)\" = ([02]) \]'",
                     re.M | re.S)


def selftest_guard_loops() -> dict:
    """{exit code: [commands]} for the Bash guard loops in tests/selftest.sh (`= 2` block, `= 0` allow),
    decoded as bash would decode the double-quoted words."""
    text = SELFTEST.read_text(encoding="utf-8")
    loops = {}
    for m in LOOP_RE.finditer(text):
        words = shlex.split(m.group(1).replace("\\\n", " "))
        loops.setdefault(m.group(2), []).extend(w.replace("\\$", "$") for w in words)
    return loops


def in_task(cmd: str) -> str:
    return cmd.replace(".light-harness/", ".tasks/alpha/.light-harness/")


def test_ac5_selftest_carries_every_example():
    """AC-5 (Q-6): tests/selftest.sh runs every EX-1..EX-8 read in its `= 0` (not blocked) guard loop and every
    EX-9..EX-16 write, with each listed variant, in its `= 2` (blocked) loop, end to end through guard.sh with
    `.tasks/alpha/` paths; and each of those commands gets the same verdict from `bash_rules` in-process (the
    main checkout has no CTX task, so the BR-5 message names `alpha` from the path)."""
    raw = SELFTEST.read_bytes()
    assert b"\r" not in raw, "tests/selftest.sh must stay LF"
    loops = selftest_guard_loops()
    assert set(loops) == {"0", "2"}, sorted(loops)
    missing = [f"{ex} (allow loop): {in_task(c)}" for ex, c in EXAMPLE_READS if in_task(c) not in loops["0"]]
    missing += [f"{ex} (block loop): {in_task(c)}" for ex, c in EXAMPLE_WRITES if in_task(c) not in loops["2"]]
    assert not missing, "selftest.sh guard loops lack:\n" + "\n".join(missing)
    misplaced = [c for ex, c in EXAMPLE_READS if in_task(c) in loops["2"]]
    misplaced += [c for ex, c in EXAMPLE_WRITES if in_task(c) in loops["0"]]
    assert not misplaced, misplaced
    wrong = []
    for ex, c in EXAMPLE_READS:
        out, msg = GS.judge(hook_guard, in_task(c), None)
        if out not in NOT_BLOCKED:
            wrong.append(f"{ex}: {in_task(c)!r} gave {out}: {msg}")
    for ex, c in EXAMPLE_WRITES:
        out, msg = GS.judge(hook_guard, in_task(c), None)
        if out != ["block", "harness-files"] or msg != message("alpha"):
            wrong.append(f"{ex}: {in_task(c)!r} gave {out}: {msg}")
    assert not wrong, "\n".join(wrong)


# --- R-6: malformed input never crashes the hook ---------------------------------------------------

def test_malformed_no_crash():
    """AC-2 R-6: malformed commands give a block or an exit, never another exception (a crash would fail
    open). Those naming .light-harness/ with a non-read word are blocked with the BR-5 message."""
    no_live = ['"', "'", "\\", "||", "", "|||", "&&", ";;", "&", " \n ", "cat x \\"]
    results = {}
    for cmd in no_live:
        results[cmd] = GS.outcome(hook_guard, cmd)
    assert all(o in NOT_BLOCKED for o in results.values()), results
    writes = ["sed -i s/a/b/ .light-harness/contract.md '",
              'sed -n "p .light-harness/contract.md',
              "sed -i s/a/b/ .light-harness/contract.md \\",
              "|| sed -i s/a/b/ .light-harness/contract.md",
              "||| tee .light-harness/contract.md",
              "; ; > .light-harness/contract.md",
              "awk '{print}\" .light-harness/contract.md $'",
              ".light-harness/contract.md"]
    for cmd in writes:
        assert_blocked(cmd)
    # Odd but harmless reads naming the contract still judge without raising
    for cmd in ("cat .light-harness/contract.md ;", "| cat .light-harness/contract.md", "cat .light-harness/contract.md &"):
        out = GS.outcome(hook_guard, cmd)
        assert out in (["pass"], ["allow"], ["block", "harness-files"]), (cmd, out)


# --- AC-3: the block message ---------------------------------------------------------------------

@pytest.mark.parametrize("task", ["alpha", "gamma-2"])
def test_ac3_message(task):
    """AC-3 BR-5 (Q-5): with CTX["task"] set, blocking EX-9 gives the §8.2 message naming that task, with the
    Read tool / `light-harness show --task <task>` hint and the existing Edit / set-status hint."""
    out, msg = GS.judge(hook_guard, "sed -i 's/a/b/' .light-harness/contract.md", task)
    assert out == ["block", "harness-files"]
    assert f"Read the contract with the Read tool or `light-harness show --task {task}`." in msg
    assert "Edit contract.md with the Edit tool" in msg
    assert msg == message(task)


@pytest.mark.parametrize("name", ["beta", "delta-7"])
def test_ac3_message_task_from_path(name):
    """AC-3 BR-5 (Q-5): with no CTX task, the name comes from a `.tasks/<name>/.light-harness/` path."""
    out, msg = GS.judge(hook_guard, f"sed -i s/a/b/ .tasks/{name}/.light-harness/contract.md", None)
    assert out == ["block", "harness-files"]
    assert msg == message(name)


def test_ac3_message_ctx_task_wins_over_path():
    """AC-3 BR-5 (Q-5): CTX["task"] comes before a task name in the command."""
    out, msg = GS.judge(hook_guard, "echo x > .tasks/beta/.light-harness/contract.md", "alpha")
    assert out == ["block", "harness-files"]
    assert msg == message("alpha")


def test_ac3_message_literal_name():
    """AC-3 BR-5 (Q-5): with neither a CTX task nor a task path, the message has the literal `<name>`."""
    out, msg = GS.judge(hook_guard, "cp x .light-harness/contract.md", None)
    assert out == ["block", "harness-files"]
    assert msg == message("<name>")
    assert "`light-harness show --task <name>`" in msg


def test_ac3_base_copy_message_unchanged():
    """AC-3 BR-5 §5: the `.light-harness-base/` write message (base_writes) is out of scope and unchanged."""
    out, msg = GS.judge(hook_guard, "sed -i s/a/b/ .light-harness/contract.md", "alpha")
    assert msg == message("alpha")
    out, msg = GS.judge(hook_guard, "rm .light-harness-base/plugins/x.py", "alpha")
    assert out == ["block", "harness-files"]
    assert msg == (".light-harness-base/ holds the base-commit copies, the oracle for unchanged behaviour. "
                   "`light-harness baseline` writes it; read or run it, never change it.")
