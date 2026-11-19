"""The guard's Bash `.light-harness/` rule (issue #23): READ_ONLY commands on the task's contract and config
are allowed, sed/awk/gawk/git on them stay blocked as at base (Q-9), every write stays blocked, and the block
message points at the Read tool and `light-harness show`. AC-1, AC-2, AC-3, AC-5; BR-1, BR-2, BR-3, BR-4,
BR-5, BR-7; EX-1..EX-16; R-1, R-2, R-4, R-6; Q-2, Q-3, Q-4, Q-5, Q-9.

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
]


@pytest.mark.parametrize("ex,cmd", EXAMPLE_READS, ids=[e for e, _ in EXAMPLE_READS])
def test_ac1_reads_allowed(ex, cmd):
    """AC-1 (EX-1..EX-3; BR-1, BR-2, BR-3): each example read is not blocked, and neither is the same read
    with its errors sent to /dev/null (BR-2 holds for every read)."""
    for c in (cmd, cmd + " 2>/dev/null"):
        out, msg = GS.judge(hook_guard, c)
        assert out in NOT_BLOCKED, f"{ex}: {c!r} gave {out}: {msg}"


# Q-9: sed, awk, gawk and git reads of .light-harness/ stay blocked, as at base (AC-2)
EXAMPLE_BLOCKED_READS = [
    ("EX-4", "sed -n '/8.1/,/8.2/p' .light-harness/contract.md"),
    ("EX-5", "awk '/8.1/' .light-harness/contract.md"),
    ("EX-6", "git diff -- .light-harness/contract.md"),
    ("EX-7a", "git log -p -- .light-harness/contract.md"),
    ("EX-7b", "git show HEAD:.light-harness/contract.md"),
    ("EX-8", "sed -n 1,20p .light-harness/contract.md 2>&1 | head"),
]


@pytest.mark.parametrize("ex,cmd", EXAMPLE_BLOCKED_READS, ids=[e for e, _ in EXAMPLE_BLOCKED_READS])
def test_ac2_sed_awk_git_reads_blocked(ex, cmd):
    """AC-2 (EX-4..EX-8; BR-3, BR-5; Q-9): each sed/awk/git read of the contract is blocked with rule
    harness-files and the BR-5 message pointing at the Read tool / `light-harness show`, and so is the same
    command with its errors sent to /dev/null."""
    for c in (cmd, cmd + " 2>/dev/null"):
        assert_blocked(c)


@pytest.mark.parametrize("task", ["t", "omega-3"])
def test_ac2_git_reads_blocked_before_preapproval(task):
    """AC-2 EX-6 EX-7 BR-3 (Q-9): `git diff|log|show` naming .light-harness/ are blocked by harness-files
    before the SAFE_SEGMENT `git (status|diff|log|show|add)` pre-approval, as at base; the message names the
    CTX task."""
    for cmd in ("git diff -- .light-harness/contract.md", "git log -p -- .light-harness/contract.md",
                "git show HEAD:.light-harness/config.json", "git diff -- .light-harness/contract.md 2>/dev/null"):
        assert_blocked(cmd, task)
    # The same git reads of a plain file keep the SAFE_SEGMENT pre-approval (control: not a blanket git block)
    assert GS.outcome(hook_guard, "git diff -- src/a.py", task) == ["allow"]
    assert GS.outcome(hook_guard, "git log -p -- src/a.py", task) == ["allow"]


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
    ("Q-3 quoted >", "grep '>' .light-harness/contract.md"),
    ("Q-3 escaped pipe", "grep a\\|b .light-harness/contract.md"),
    ("R-4 Windows path", "cat .light-harness\\contract.md 2>/dev/null"),
    ("task folder path", "grep -n AC .tasks/alpha/.light-harness/contract.md"),
]


@pytest.mark.parametrize("why,cmd", READ_VARIANTS, ids=[w for w, _ in READ_VARIANTS])
def test_ac1_read_variants_allowed(why, cmd):
    """AC-1 BR-1 BR-2 BR-3 (Q-3): quote-aware segments and /dev/null and fd-dup redirects on READ_ONLY reads
    are not blocked by harness-files."""
    out, msg = GS.judge(hook_guard, cmd)
    assert out in NOT_BLOCKED, f"{why}: {cmd!r} gave {out}: {msg}"


BLOCKED_READ_VARIANTS = [
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
]


@pytest.mark.parametrize("why,cmd", BLOCKED_READ_VARIANTS, ids=[w for w, _ in BLOCKED_READ_VARIANTS])
def test_ac2_sed_awk_git_read_variants_blocked(why, cmd):
    """AC-2 BR-3 BR-5 (Q-9): the sed, awk, gawk and git read shapes the first plan allowed stay blocked, as at
    base, with rule harness-files and the BR-5 message."""
    assert_blocked(cmd)


def test_ac2_sed_read_in_task_folder_blocked():
    """AC-2 BR-3 BR-5 (Q-9, Q-5): a sed read of a task folder's contract is blocked, and with no CTX task the
    BR-5 message names the task from the `.tasks/<name>/` path."""
    out, msg = GS.judge(hook_guard, "sed -n '/8.1/,/8.2/p' .tasks/alpha/.light-harness/contract.md", None)
    assert out == ["block", "harness-files"]
    assert msg == message("alpha")


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
    """BR-7 BR-1 AC-1: a lone `&` is a separator, not a write; two READ_ONLY reads either side of it stay
    allowed, and `&&`, `&>/dev/null` and `2>&1` are not mistaken for a lone `&`. AC-2 BR-3 (Q-9): sed/awk
    reads either side of a lone `&` are blocked with the BR-5 message, as at base."""
    for cmd in ("cat .light-harness/contract.md & grep x .light-harness/config.json 2>/dev/null",
                "head -1 .light-harness/contract.md & grep '/x/' .light-harness/config.json",
                "cat -n .light-harness/contract.md &>/dev/null && grep x .light-harness/contract.md 2>&1"):
        out, msg = GS.judge(hook_guard, cmd)
        assert out in NOT_BLOCKED, f"{cmd!r} gave {out}: {msg}"
    for cmd in ("sed -n 1p .light-harness/contract.md & awk '/x/' .light-harness/config.json",
                "sed -n 1p .light-harness/contract.md &>/dev/null && grep x .light-harness/contract.md 2>&1",
                "cat .light-harness/contract.md & awk '/x/' .light-harness/config.json"):
        assert_blocked(cmd)


# --- AC-2, RV-4: the write-hole classes review found, each generated over its dimensions ------------
# Each class test builds a deterministic cross product, asserts every command is blocked with harness-files
# and the BR-5 message, and reports how many differ out of how many. Each also checks controls (Q-9): the same
# template with a harmless value instead of the hole is still blocked, because sed/awk/gawk/git on
# .light-harness/ stay blocked as at base; and READ_ONLY reads of the same targets are NOT blocked, so blocking
# every command that names .light-harness/ can't pass these tests.

CFG = ".light-harness/config.json"
# READ_ONLY reads of both targets, with and without a /dev/null redirect: must never be blocked (Q-9 controls)
READ_ONLY_CONTROLS = [f"{r} {t}{s}" for r in ("grep -n print", "head -50") for t in (C, CFG)
                      for s in ("", " 2>/dev/null")]


def blocked_failures(cmds, task="t") -> list:
    """The commands among `cmds` that are not blocked with harness-files and the BR-5 message."""
    bad = []
    for cmd in cmds:
        out, msg = GS.judge(hook_guard, cmd, task)
        if out != ["block", "harness-files"] or msg != message(task):
            bad.append(f"{cmd!r} gave {out}")
    return bad


def allowed_failures(cmds) -> list:
    bad = []
    for cmd in cmds:
        out, msg = GS.judge(hook_guard, cmd)
        if out not in NOT_BLOCKED:
            bad.append(f"{cmd!r} gave {out}: {msg}")
    return bad


def report(kind, bad, total) -> str:
    return f"{len(bad)} of {total} {kind}; first 10:\n" + "\n".join(bad[:10])


# RV-1 / RV-10: shell expansion becomes an option. Per tool: (command name, script or subcommand, the option
# that writes or runs, a harmless option of the same shape, operands after the target position).
EXPANSION_TOOLS = [
    ("sed", "s/a/b/", "-i", "-n"),
    ("awk", "'{print}'", "-iinplace", "-F:"),
    ("gawk", "'{print}'", "-iinplace", "-F:"),
    ("git", "diff", "--output=" + C, "--stat"),
    ("git", "log", "--output=x", "-p"),
    ("git", "show", "--ext-diff", "--stat"),
]


def expansion_forms(opt: str, alt: str) -> list:
    """Words bash expands (braces, parameters, globs, extglob, ANSI-C/locale quotes) that can become `opt`."""
    return [
        "{%s,%s}" % (opt, alt), "{%s,%s}" % (alt, opt), "{,%s}" % opt, "{%s,}" % opt,
        "$X", '"$X"', "${X}", '"${X}"', "$1", '"$@"', "${X:-%s}" % opt, "$X$Y",
        "*", opt[:-1] + "?", "[" + opt[0] + "]" + opt[1:], opt[:2] + "*",
        "@(%s)" % opt, "+(%s)" % opt, "?(%s)" % opt, "*(%s)" % opt, "!(zz)",
        "$'%s'" % opt, '$"%s"' % opt,
    ]


def expansion_commands(word_of) -> list:
    """(position, command): each word placed before the script/subcommand (0), after it (1), after the file (2)."""
    cmds = []
    for name, script, opt, alt in EXPANSION_TOOLS:
        for w in word_of(opt, alt):
            for target in (C, CFG):
                file_part = f"-- {target}" if name == "git" else target
                cmds.append((0, f"{name} {w} {script} {file_part}"))
                cmds.append((1, f"{name} {script} {w} {file_part}"))
                cmds.append((2, f"{name} {script} {file_part} {w}"))
    return cmds


def test_ac2_rv1_rv10_expansion_class():
    """AC-2 BR-3 BR-4 RV-1 RV-10 RV-4: a word bash expands (brace `{…,…}`, `$X`, `"$X"`, `${X}`, `$1`, `"$@"`,
    globs `* ? [`, extglob `@( +( ?( *( !(`, `$'…'`, `$"…"`) can become -i / -iinplace / --output / --ext-diff at
    run time. Generated: 23 forms x sed, awk, gawk, git diff/log/show x 3 positions (before the script or
    subcommand, after it, after the file) x 2 targets = 828 commands, all blocked. Controls (Q-9): the same
    templates with the plain harmless option (`-n`, `-F:`, `--stat`, `-p`) are blocked too, as at base (36);
    READ_ONLY reads `cat -n <target>` and `grep -n AC <target>`, with and without ` 2>/dev/null`, pass (8)."""
    cmds = [c for _, c in expansion_commands(expansion_forms)]
    assert len(cmds) == 23 * 6 * 3 * 2 == 828
    bad = blocked_failures(cmds)
    print(f"RV-1/RV-10: {len(bad)} of {len(cmds)} not blocked")
    assert not bad, report("expansion commands not blocked", bad, len(cmds))
    controls = [c for _, c in expansion_commands(lambda opt, alt: [alt])]
    assert len(controls) == 6 * 3 * 2 == 36
    bad = blocked_failures(controls)
    assert not bad, report("sed/awk/git controls not blocked", bad, len(controls))
    reads = [f"{r} {t}{s}" for r in ("cat -n", "grep -n AC") for t in (C, CFG) for s in ("", " 2>/dev/null")]
    bad = allowed_failures(reads)
    assert not bad, report("READ_ONLY controls blocked", bad, len(reads))


# RV-2: awk programs that write or run
AWK_PAYLOADS = ['"}"', '";"', '"a}b;c"', '$0 "}"', '"{"', '"x" \\\n', '"a",\n"b"', '"#"']
AWK_SINKS = ['> FILENAME', '>> "f"', '> "' + C + '"', '>"f"', '| "sh"', '|"sh"', '|& "sh"']
AWK_WRAPPERS = ["{%s}", "BEGIN{%s}", "NR>0{x=1; %s}", "END{if (NR) %s}"]
AWK_EXEC = ['"sh" | getline', '"sh" | getline x', 'getline x < "f"', '"sh" |& getline', 'system("x")',
            'system ("x")', 'r = system("x")', 'f="system"; @f("x")', 'f="sys" "tem"; @f("x")',
            'f=sprintf("%s","system"); @f("x")']
AWK_DIRECTIVES = ['@include "inplace"; {print}', '@load "rwarray"; {print}', '@include "x"\n{print}']


def awk_statements(sink: bool) -> list:
    out = []
    for verb in ("print", "printf"):
        for paren in (False, True):
            for p in AWK_PAYLOADS:
                head = f"{verb}({p})" if paren else f"{verb} {p}"
                if sink:
                    out += [f"{head} {s}" for s in AWK_SINKS]
                else:
                    out.append(head)
    return out


def awk_commands(statements) -> list:
    cmds = []
    for tool in ("awk", "gawk"):
        for wrap in AWK_WRAPPERS:
            for st in statements:
                for suffix in ("", " 2>/dev/null"):
                    cmds.append(f"{tool} '{wrap % st}' {C}{suffix}")
    return cmds


def test_ac2_rv2_awk_program_class():
    """AC-2 BR-3 BR-4 RV-2 RV-4: an awk/gawk program that writes or runs, wherever the print ends. Generated:
    awk, gawk x 4 rule wrappers (`{…}`, `BEGIN{…}`, after `x=1;`, inside `if`) x {print, printf} x {plain,
    parenthesised} x 8 payloads holding `}` `;` `{` `#`, a continuation or a newline after a comma x 7 sinks
    (`>`, `>>`, `>` the contract, unspaced `>`, `|`, unspaced `|`, `|&`) x 2 suffixes (none, `2>/dev/null`)
    = 3,584 commands; plus 10 getline / `|&` getline / system() (3 spellings) / gawk `@f()` (built 3 ways)
    statements x the same wrappers, tools and suffixes = 160, and `@include`/`@load` directives (12): 3,756
    commands, all blocked. Controls (Q-9): the same 32 print statements with no sink (x 16 = 512) are blocked
    too, as at base; READ_ONLY reads `grep -n print <target>` and `head -50 <target>`, with and without
    ` 2>/dev/null`, pass (8)."""
    cmds = awk_commands(awk_statements(sink=True))
    cmds += awk_commands(AWK_EXEC)
    cmds += [f"{tool} '{d}' {C}{sfx}" for tool in ("awk", "gawk") for d in AWK_DIRECTIVES for sfx in ("", " 2>/dev/null")]
    assert len(cmds) == 2 * 4 * 2 * 2 * 8 * 7 * 2 + 2 * 4 * 10 * 2 + 2 * 3 * 2 == 3756
    bad = blocked_failures(cmds)
    print(f"RV-2: {len(bad)} of {len(cmds)} not blocked")
    assert not bad, report("awk programs not blocked", bad, len(cmds))
    controls = awk_commands(awk_statements(sink=False))
    assert len(controls) == 2 * 4 * 2 * 2 * 8 * 2 == 512
    bad = blocked_failures(controls)
    print(f"RV-2 controls: {len(bad)} of {len(controls)} awk reads not blocked")
    assert not bad, report("awk read controls not blocked", bad, len(controls))
    reads = READ_ONLY_CONTROLS
    bad = allowed_failures(reads)
    assert not bad, report("READ_ONLY controls blocked", bad, len(reads))


# RV-3: a long or padded sed program must be judged quickly (a timed-out hook fails open)
SED_PADDINGS = ["\n", "\n\n\n", " \n ", ";\n\n", "  ;  ", "\n \t\n"]
SED_UNITS = ["p", "/AC/p", "1,5p", "s/a/b/g"]
SED_LAST_WRITES = ["w " + C, "e touch x", "s/a/b/w x", "1r x", "W x"]
TIMING_CHILD = r"""
import json, sys, time
sys.path[:0] = [sys.argv[1], sys.argv[2]]
import hook_guard, guard_scenarios as GS
res = []
for cmd in json.loads(sys.stdin.read()):
    t = time.perf_counter()
    out, msg = GS.judge(hook_guard, cmd)
    res.append([out, msg, time.perf_counter() - t])
print(json.dumps(res))
"""
TIMING_SCRIPTS = SCRIPTS   # the guard under test, judged in a child process so a hang can't stall the suite


def judge_timed(cmds, limit=60):
    """(returncode, stdout, stderr) of judging `cmds` in a child process; returncode None when it timed out."""
    import json
    import subprocess
    try:
        p = subprocess.run([sys.executable, "-c", TIMING_CHILD, str(TIMING_SCRIPTS), str(HERE)],
                           input=json.dumps(cmds), capture_output=True, text=True, timeout=limit)
    except subprocess.TimeoutExpired:
        return None, "", f"timed out after {limit} s"
    return p.returncode, p.stdout, p.stderr


def sed_programs(lasts):
    """Padded sed programs of 50 and 500 commands (every padding x unit) and of 5,000 `p` commands."""
    out = []
    for k, units in ((50, SED_UNITS), (500, SED_UNITS), (5000, ["p"])):
        for pad in SED_PADDINGS:
            for unit in units:
                for last in lasts:
                    out.append(f"sed -n '{pad.join([unit] * k)}{pad}{last}' {C}")
    return out


def test_ac2_rv3_long_padded_sed_judged_quickly():
    """AC-2 BR-3 RV-3 RV-4 R-6: a sed program of 50 or 500 commands (4 command units) or 5,000 `p` commands,
    padded 6 ways (blank lines, spaces around newlines, `;` with padding), ending in a write or exec command
    (w, e, s///w, r, W): (2 x 6 x 4 + 6) x 5 = 270 commands, each blocked in well under a second (limit 1 s
    each, judged in a child process with a 60 s cap, so a hang fails instead of stalling). The same programs
    ending in `$p` (54) are blocked too (Q-9: sed on .light-harness/ stays blocked, as at base), and as quick;
    READ_ONLY reads `grep -n print <target>` / `head -50 <target>`, with and without ` 2>/dev/null`, pass."""
    import json
    writes = sed_programs(SED_LAST_WRITES)
    reads = sed_programs(["$p"])
    assert (len(writes), len(reads)) == ((2 * 6 * 4 + 6) * 5, 2 * 6 * 4 + 6) == (270, 54)
    rc, out, err = judge_timed(writes + reads)
    assert rc == 0, f"judging long sed programs did not finish: {err[-2000:]}"
    res = json.loads(out)
    slow = [f"{len(c)} chars ending {c[-40:]!r}: {t:.2f}s" for c, (_, _, t) in zip(writes + reads, res) if t >= 1.0]
    print(f"RV-3: slowest judgement {max(t for _, _, t in res):.3f} s of {len(res)}")
    assert not slow, report("judgements took 1 s or more", slow, len(res))
    bad = [f"{c[-50:]!r} gave {o}" for c, (o, m, _) in zip(writes, res[:len(writes)])
           if o != ["block", "harness-files"] or m != message("t")]
    assert not bad, report("padded sed writes not blocked", bad, len(writes))
    bad = [f"{c[-50:]!r} gave {o}" for c, (o, m, _) in zip(reads, res[len(writes):])
           if o != ["block", "harness-files"] or m != message("t")]
    assert not bad, report("padded sed reads not blocked", bad, len(reads))
    bad = allowed_failures(READ_ONLY_CONTROLS)
    assert not bad, report("READ_ONLY controls blocked", bad, len(READ_ONLY_CONTROLS))


# RV-7: another segment changes what the command word runs. {W} is the read's command word.
REDEFINERS = [
    "{W}(){{ :; }}", "{W} () {{ :; }}", "{W}() ( : )", "function {W} {{ :; }}", "function {W}() {{ :; }}",
    "{W}(){{ command {W} -i \"$@\"; }}", "eval '{W}(){{ :; }}'", "eval \"{W}(){{ :; }}\"", "eval \"$DEFS\"",
    "source d.sh", ". d.sh", ". ./d.sh", "alias {W}=frob", "alias {W}='frob -i'", "hash -p ./x {W}",
    "PATH=.:$PATH", "PATH=.", "PATH+=:.", "export PATH=.", "export \"PATH=.\"", "export 'PATH'=.",
    "declare -x PATH=.", "typeset -x PATH=.", "declare PATH=.", "readonly PATH=.",
]
RV7_READS = [("sed", "sed -n p {T}"), ("awk", "awk '/AC/' {T}"), ("gawk", "gawk -F: '{{print $1}}' {T}"),
             ("git", "git diff -- {T}"), ("git", "git log -p -- {T}")]
RV7_SEPARATORS = ["; ", "\n", " && "]


def test_ac2_rv7_command_word_redefined_class():
    """AC-2 BR-4 RV-7 RV-4: a segment before a sed/awk/gawk/git read that redefines what its command word runs
    (function definitions in 5 spellings, eval, source/`.`, alias, hash -p, PATH via assignment, `+=`,
    export, declare/typeset -x, readonly, quoted forms). Generated: 25 definers x 5 reads (sed, awk, gawk,
    git diff, git log) x 3 separators (`;`, newline, `&&`) x 2 targets = 750 commands, all blocked. Controls
    (Q-9): each sed/awk/git read alone, and after `cd x &&` or `echo x;`, is blocked too, as at base (30);
    `cat` and `grep -n AC` reads of the same targets alone and after `cd x &&` or `echo x;` pass (12)."""
    cmds = []
    for d in REDEFINERS:
        for w, read in RV7_READS:
            for sep in RV7_SEPARATORS:
                for target in (C, CFG):
                    cmds.append(d.format(W=w) + sep + read.format(T=target))
    assert len(cmds) == 25 * 5 * 3 * 2 == 750
    bad = blocked_failures(cmds)
    print(f"RV-7: {len(bad)} of {len(cmds)} not blocked")
    assert not bad, report("redefined command words not blocked", bad, len(cmds))
    controls = [pre + read.format(T=t) for pre in ("", "cd x && ", "echo x; ") for _, read in RV7_READS
                for t in (C, CFG)]
    assert len(controls) == 30
    bad = blocked_failures(controls)
    assert not bad, report("sed/awk/git read controls not blocked", bad, len(controls))
    reads = [pre + read.format(T=t) for pre in ("", "cd x && ", "echo x; ") for read in ("cat {T}", "grep -n AC {T}")
             for t in (C, CFG)]
    assert len(reads) == 12
    bad = allowed_failures(reads)
    assert not bad, report("READ_ONLY controls blocked", bad, len(reads))


# RV-8: a redirection word stands where the allow-list expects the program or an option's value
REDIRECT_WORDS = [">/dev/null", "2>/dev/null", "&>/dev/null", ">>/dev/null", "2>&1", ">&2", "1>&2", "</dev/null",
                  "< /dev/null", "<<<x", "<<< 'a b'", "0<&-", "3>&-", "2> /dev/null", ">&/dev/null", "<x",
                  "> /dev/null"]
AWK_HIDDEN = ["'{print \"X\" > FILENAME}'", "'BEGIN{system(\"touch zz\")}'", "'{print | \"sh\"}'"]
AWK_HIDDEN_READ = "'/AC/'"


def awk_redirect_commands(r, prog):
    return [f"{tool} {pos.format(R=r, P=prog)} {C}" for tool in ("awk", "gawk") for pos in (
        "{R} {P}", "-F {R} : {P}", "-v {R} a=1 {P}", "--field-separator {R} : {P}", "--assign {R} a=1 {P}",
        "-F: {R} {P}", "-- {R} {P}")]


def test_ac2_rv8_redirection_word_class():
    """AC-2 BR-2 BR-3 RV-8 RV-4: bash removes redirection words before the command runs, so one standing where
    the allow-list expects the awk program or an option's value hides the real program. Generated: 17
    redirection words (/dev/null forms, fd dups and closes, `<`, `<<<`, spaced forms) x [awk, gawk x 7
    positions (before the program; as the -F, -v, --field-separator, --assign value; after -F:; after --) x
    3 write/exec programs; sed x 4 positions x 2 write programs; git diff/log/show x 3 positions] = 17 x 59 =
    1,003 commands, all blocked. Controls (Q-9): the 238 awk/gawk templates with a read program are blocked
    too, as at base; `cat <word> <contract>` and `grep -n AC <word> <contract>` for all 17 words (34 READ_ONLY
    reads whose redirections are BR-2 non-writes) pass."""
    cmds = []
    for r in REDIRECT_WORDS:
        for prog in AWK_HIDDEN:
            cmds += awk_redirect_commands(r, prog)
        for prog in ("'w " + C + "'", "'1e touch x'"):
            cmds += [f"sed -n {r} {prog} {C}", f"sed -e {r} {prog} {C}", f"sed --expression {r} {prog} {C}",
                     f"sed -n -- {r} {prog} {C}"]
        for sub in ("diff", "log", "show"):
            cmds += [f"git {sub} {r} --output=x -- {C}", f"git {r} {sub} --output=x -- {C}",
                     f"git {sub} -- {C} {r} --ext-diff"]
    assert len(cmds) == 17 * (2 * 7 * 3 + 4 * 2 + 3 * 3) == 1003
    bad = blocked_failures(cmds)
    print(f"RV-8: {len(bad)} of {len(cmds)} not blocked")
    assert not bad, report("redirection-word commands not blocked", bad, len(cmds))
    controls = [c for r in REDIRECT_WORDS for c in awk_redirect_commands(r, AWK_HIDDEN_READ)]
    assert len(controls) == 17 * 14 == 238
    bad = blocked_failures(controls)
    print(f"RV-8 controls: {len(bad)} of {len(controls)} awk reads not blocked")
    assert not bad, report("awk read controls not blocked", bad, len(controls))
    reads = [f"{head} {r} {C}" for r in REDIRECT_WORDS for head in ("cat", "grep -n AC")]
    assert len(reads) == 34
    bad = allowed_failures(reads)
    assert not bad, report("READ_ONLY controls blocked", bad, len(reads))


# RV-9: a lone `&` and a write, in a command whose quoting sends the guard down its fallback path
FALLBACK_TRIGGERS = ["; echo $'x'", "; echo $\"x\"", "; echo ${HOME}", "; echo ${x:-y}", " # it's",
                     "\ncat <<EOF\nit's\nEOF", " '"]
AMP_HEADS = ["cat {T}", "grep -n AC {T}", "sed -n p {T}", "echo x", "ls"]
AMP_WRITES = ["sed -i s/a/b/ {T}", "tee {T}", "cp x {T}", "rm {T}", "python3 -c \"open('{T}','w')\"",
              "awk -i inplace '{{print}}' {T}", "echo x > {T}"]


def test_ac2_rv9_fallback_lone_ampersand_class():
    """AC-2 BR-7 RV-9 RV-4 R-1: BR-7's lone `&` separates segments on the fallback path too. Generated: 5 heads
    (cat, grep, sed reads, echo, ls) x 7 writes after the `&` (sed -i, tee, cp, rm, python -c, awk -i
    inplace, `>`) x 2 spacings (` & `, `&`) x 7 fallback triggers (`$'…'`, `$"…"`, `${HOME}`, `${x:-y}`, a `#`
    comment with an apostrophe, a heredoc, an unbalanced quote) = 490 commands, all blocked. Controls: the
    READ_ONLY heads with a READ_ONLY read after the `&` (2 x 2 x 5 triggers that leave the quoting balanced)
    pass."""
    cmds = []
    for head in AMP_HEADS:
        for w in AMP_WRITES:
            for amp in (" & ", "&"):
                for trig in FALLBACK_TRIGGERS:
                    cmds.append(head.format(T=C) + amp + w.format(T=C) + trig)
    assert len(cmds) == 5 * 7 * 2 * 7 == 490
    bad = blocked_failures(cmds)
    print(f"RV-9: {len(bad)} of {len(cmds)} not blocked")
    assert not bad, report("lone-& writes not blocked", bad, len(cmds))
    controls = [head.format(T=C) + " & grep x " + CFG + trig for head in AMP_HEADS[:2]
                for trig in FALLBACK_TRIGGERS[:5]]
    controls += [c.replace(" & ", "&") for c in controls]
    assert len(controls) == 20
    bad = allowed_failures(controls)
    assert not bad, report("controls blocked", bad, len(controls))


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
    """AC-5 (Q-6, Q-9): tests/selftest.sh runs every EX-1..EX-3 read in its `= 0` (not blocked) guard loop, and
    every EX-4..EX-8 sed/awk/git read and every EX-9..EX-16 write, with each listed variant, in its `= 2`
    (blocked) loop, end to end through guard.sh with `.tasks/alpha/` paths; none sits in the wrong loop; and
    each of those commands gets the same verdict from `bash_rules` in-process (the main checkout has no CTX
    task, so the BR-5 message names `alpha` from the path)."""
    raw = SELFTEST.read_bytes()
    assert b"\r" not in raw, "tests/selftest.sh must stay LF"
    loops = selftest_guard_loops()
    assert set(loops) == {"0", "2"}, sorted(loops)
    blocked = EXAMPLE_BLOCKED_READS + EXAMPLE_WRITES
    missing = [f"{ex} (allow loop): {in_task(c)}" for ex, c in EXAMPLE_READS if in_task(c) not in loops["0"]]
    missing += [f"{ex} (block loop): {in_task(c)}" for ex, c in blocked if in_task(c) not in loops["2"]]
    assert not missing, "selftest.sh guard loops lack:\n" + "\n".join(missing)
    misplaced = [c for ex, c in EXAMPLE_READS if in_task(c) in loops["2"]]
    misplaced += [c for ex, c in blocked if in_task(c) in loops["0"]]
    assert not misplaced, misplaced
    wrong = []
    for ex, c in EXAMPLE_READS:
        out, msg = GS.judge(hook_guard, in_task(c), None)
        if out not in NOT_BLOCKED:
            wrong.append(f"{ex}: {in_task(c)!r} gave {out}: {msg}")
    for ex, c in blocked:
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
