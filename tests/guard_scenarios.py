"""Test support for the `.light-harness/` Bash rule (issue #23): run `bash_rules` in-process, the BR-6
[sweep] input space, and the spec-level predicate for BR-6's allowed changes.

Used by tests/test_guard_reads.py, tests/test_guard_preservation.py and tests/gen_guard_golden.py.
"""
from __future__ import annotations

import hashlib

# --- running the guard -----------------------------------------------------------------------------


class Blocked(Exception):
    def __init__(self, msg, rule):
        super().__init__(msg)
        self.msg, self.rule = msg, rule


class Decided(Exception):
    def __init__(self, decision, reason, rule):
        super().__init__(reason)
        self.decision, self.reason, self.rule = decision, reason, rule


def judge(mod, cmd: str, task="t"):
    """(outcome, block message or None) for `mod.bash_rules(cmd)` with `CTX["task"] = task`.

    `block` and `decide` are patched to raise, so nothing is printed and nothing exits; telemetry is
    off (`CTX["common"]` is None). Any other exception propagates to the caller."""
    def fake_block(msg, rule):
        raise Blocked(msg, rule)

    def fake_decide(decision, reason, rule=""):
        raise Decided(decision, reason, rule)

    saved = (mod.block, mod.decide, dict(mod.CTX))
    mod.block, mod.decide = fake_block, fake_decide
    mod.CTX.clear()
    mod.CTX.update(task=task, common=None)
    try:
        mod.bash_rules(cmd)
    except Blocked as e:
        return ["block", e.rule], e.msg
    except Decided as e:
        return (["allow"] if e.decision == "allow" else [e.decision, e.rule]), None
    except SystemExit as e:
        if e.code in (0, None):
            return ["pass"], None
        return ["exit", e.code], None
    finally:
        mod.block, mod.decide = saved[0], saved[1]
        mod.CTX.clear()
        mod.CTX.update(saved[2])
    return ["returned"], None  # bash_rules must never return normally


def outcome(mod, cmd: str, task="t") -> list:
    """One of ["block", rule], ["ask", rule], ["allow"] or ["pass"]."""
    return judge(mod, cmd, task)[0]


def code(out: list) -> str:
    """The compact golden code of an outcome (Q-8): p, a, b:<rule>, k:<rule>."""
    if out == ["pass"]:
        return "p"
    if out == ["allow"]:
        return "a"
    if out[0] == "block":
        return "b:" + out[1]
    if out[0] == "ask":
        return "k:" + out[1]
    return "?:" + ":".join(map(str, out))


# --- the BR-6 input space --------------------------------------------------------------------------

READ_ONLY_WORDS = ["cat", "head", "tail", "less", "more", "grep", "rg", "wc", "ls", "diff", "file", "stat"]

PREFIXES = ["", "cd x && ", "FOO=1 "]
# (word id, command name, body). The flag goes between the command name and the body.
WORDS = [(w, w, "") for w in READ_ONLY_WORDS] + [
    ("sed", "sed", "'1,5p'"),
    ("awk", "awk", "'/AC/'"),
    ("gawk", "gawk", "'/AC/'"),
    ("git diff", "git diff", "--"),
    ("git log", "git log", "--"),
    ("git show", "git show", "--"),
    ("git add", "git add", ""),
    ("git checkout", "git checkout", ""),
    ("tee", "tee", ""),
    ("cp x", "cp", "x"),
    ("mv x", "mv", "x"),
    ("python -c", "python", '-c "print(1)"'),
    ("rm", "rm", ""),
    ("touch", "touch", ""),
    ("frob", "frob", ""),  # a command word no rule knows
]
FLAGS = ["", "-n", "-i", "-i.bak", "-ni", "--in-place", "-i inplace"]
TARGETS = [".light-harness/contract.md", ".light-harness/config.json", "src/a.py"]
SUFFIXES = ["", " | head", " 2>/dev/null", " >/dev/null 2>&1", " > out", " > .light-harness/contract.md",
            "; echo", ' "a|b"']

READ_SUFFIXES = {"", " | head", " 2>/dev/null", " >/dev/null 2>&1", "; echo", ' "a|b"'}

# --- part 2 of the BR-6 input space: sed/awk/gawk/git argument shapes (RV-4) ------------------------
# The flags part above only ever gives sed/awk/git the fixed bodies '1,5p', '/AC/' and `--`. This part varies
# the arguments themselves, with the shapes review found (RV-1/RV-10 expansion, RV-2 awk programs, RV-3
# padding, RV-5 git options, RV-7 definitions in another segment, RV-8 redirection words, RV-9 a lone `&`
# after a fallback trigger), and crosses them with prefixes, targets and suffixes.

# Prefixes: the three above, then segments that change what a later command word runs (RV-7). A sed/awk/git
# read after one of these is not a listed read (BR-6: it may stay blocked, as at base).
SHAPE_PREFIXES = PREFIXES + [
    "X=-i; ",
    "sed(){ command sed -i \"$@\"; }; ",
    "function awk { :; }; ",
    "alias git=frob; ",
    "PATH=.:$PATH; ",
    "export PATH=.; ",
    "eval x; ",
    ". ./d.sh; ",
]

# Shapes: (id, command name, args before the target, args after the target, read?). `read?` is the spec-level
# judgement per BR-1..BR-3 (and RV-1..RV-10): True when the command, as bash runs it, is a read on the
# allow-list; False when it can write or run something, or uses a form the allow-list doesn't recognise.
SHAPES = [
    # READ_ONLY members and an unknown word, so the RV-7 prefixes are seen beside them too
    ("cat --", "cat", "--", "", True),
    ("grep -n", "grep", "-n AC", "", True),
    ("frob -x", "frob", "-x", "", False),
    # sed reads
    ("sed -n range", "sed", "-n '/AC/,/8.2/p'", "", True),
    ("sed -n -e -e", "sed", "-n -e p -e '$p'", "", True),
    ("sed -E s///gp", "sed", "-E -n 's/a/b/gp'", "", True),
    ("sed 20q", "sed", "20q", "", True),
    ("sed padded", "sed", "-n 'p\n\n\n p ;\n\n$p'", "", True),
    ("sed /dev/null word", "sed", "-n >/dev/null p", "", True),
    # sed: shell expansion becomes an option (RV-1, RV-10), before the script, after it, after the file
    ("sed brace", "sed", "{-i,-s} '1,5p'", "", False),
    ("sed brace after file", "sed", "'1,5p'", "{-i,-s}", False),
    ("sed $X", "sed", "'1,5p' $X", "", False),
    ("sed \"$X\" after file", "sed", "-n '1,5p'", '"$X"', False),
    ("sed ${X}", "sed", "${X} '1,5p'", "", False),
    ("sed glob *", "sed", "'1,5p'", "*", False),
    ("sed glob ?", "sed", "-n p", "-?", False),
    ("sed glob [", "sed", "[-]i p", "", False),
    ("sed extglob @(", "sed", "-n p", "@(-i)", False),
    ("sed extglob +(", "sed", "-n p", "+(-i)", False),
    ("sed $'…'", "sed", "-n p", "$'-i'", False),
    # sed programs that write or run, padded (RV-3) or plain
    ("sed padded w", "sed", "-n 'p\n\n\n p ;\n\nw x'", "", False),
    ("sed 1~2w", "sed", "-n '1~2w x'", "", False),
    ("sed s///w", "sed", "'s/a/b/w x'", "", False),
    ("sed e", "sed", "'1e touch x'", "", False),
    ("sed -e word w", "sed", "-n -e >/dev/null 'w x'", "", False),
    # awk/gawk reads
    ("awk NR<=5", "awk", "'NR<=5'", "", True),
    ("awk -F: print", "awk", "-F: '{print $1}'", "", True),
    ("gawk -v NR>n", "gawk", "-v n=2 'NR>n'", "", True),
    ("gawk --posix", "gawk", "--posix '/AC/ {print}'", "", True),
    # awk/gawk: expansion (RV-1, RV-10)
    ("awk brace", "awk", "{-iinplace,-F:} '{print}'", "", False),
    ("gawk $X", "gawk", "$X '{print}'", "", False),
    ("awk \"$P\"", "awk", '"$P"', "", False),
    ("awk glob", "awk", "-? '{print}'", "", False),
    ("gawk extglob", "gawk", "@(-iinplace) '{print}'", "", False),
    # awk/gawk programs that write or run (RV-2)
    ("awk print \"}\" >", "awk", "'{print \"}\" > FILENAME}'", "", False),
    ("awk print \";\" |", "awk", "'{print \";\" | \"sh\"}'", "", False),
    ("awk continuation >", "awk", "'{print \"x\" \\\n> FILENAME}'", "", False),
    ("awk newline after comma", "awk", "'{print \"a\",\n\"b\" > FILENAME}'", "", False),
    ("gawk @f()", "gawk", "'BEGIN{f=\"sys\" \"tem\"; @f(\"x\")}'", "", False),
    ("gawk |&", "gawk", "'{print |& \"sh\"}'", "", False),
    ("awk getline", "awk", "'BEGIN{\"date\" | getline d}'", "", False),
    ("gawk @include", "gawk", "'@include \"inplace\"; {print}'", "", False),
    # awk/gawk: a redirection word before the program or an option's value (RV-8)
    ("awk >/dev/null word", "awk", ">/dev/null '{print > FILENAME}'", "", False),
    ("awk <<< word", "awk", "<<<x '{print > FILENAME}'", "", False),
    ("awk -F word", "awk", "-F >/dev/null : '{print > FILENAME}'", "", False),
    ("gawk -v word", "gawk", "-v 2>/dev/null a=1 'BEGIN{system(\"x\")}'", "", False),
    ("gawk 2>&1 word", "gawk", "2>&1 'BEGIN{system(\"x\")}'", "", False),
    # git reads
    ("git diff HEAD --", "git", "diff HEAD --", "", True),
    ("git --no-pager log", "git", "--no-pager log -p --", "", True),
    ("git show --stat", "git", "show --stat HEAD --", "", True),
    # git: expansion, write/exec options and redirection words (RV-1, RV-5, RV-8, RV-10)
    ("git brace", "git", "diff {--output=.light-harness/contract.md,--stat} --", "", False),
    ("git $X", "git", "diff $X --", "", False),
    ("git \"$X\" after file", "git", "log --", '"$X"', False),
    ("git glob", "git", "show --ou* --", "", False),
    ("git extglob", "git", "diff @(--output=x) --", "", False),
    ("git --output", "git", "diff --output=x --", "", False),
    ("git --outp", "git", "log --outp=x --", "", False),
    ("git --ext-diff", "git", "diff --ext-diff --", "", False),
    ("git --output-indicator", "git", "diff --output-indicator-new=x --", "", False),
    ("git word then --output", "git", "diff >/dev/null --output=x --", "", False),
]
SHAPE_SUFFIXES = SUFFIXES + [
    # A lone `&` and a write, after a fallback trigger (RV-9). BR-7: always blocked.
    " & sed -i s/a/b/ .light-harness/contract.md # it's",
    " & tee .light-harness/config.json; echo ${HOME}",
]
LONE_AMP_SUFFIXES = set(SHAPE_SUFFIXES[len(SUFFIXES):])


def _flag_scenarios() -> list:
    out = []
    for prefix in PREFIXES:
        for word, name, body in WORDS:
            for flag in FLAGS:
                for target in TARGETS:
                    for suffix in SUFFIXES:
                        cmd = prefix + " ".join(p for p in (name, flag, body, target) if p) + suffix
                        out.append({"cmd": cmd, "part": "flags", "prefix": prefix, "word": word, "flag": flag,
                                    "target": target, "suffix": suffix})
    return out


def _shape_scenarios() -> list:
    out = []
    for prefix in SHAPE_PREFIXES:
        for sid, name, before, after, read in SHAPES:
            for target in TARGETS:
                for suffix in SHAPE_SUFFIXES:
                    cmd = prefix + " ".join(p for p in (name, before, target, after) if p) + suffix
                    out.append({"cmd": cmd, "part": "shapes", "prefix": prefix, "word": sid, "flag": "",
                                "target": target, "suffix": suffix, "name": name, "read": read})
    return out


FLAG_COUNT = 3 * 27 * 7 * 3 * 8                                                    # 13,608
SHAPE_COUNT = len(SHAPE_PREFIXES) * len(SHAPES) * len(TARGETS) * len(SHAPE_SUFFIXES)


def scenarios() -> list:
    """The full deterministic BR-6 input space, in a fixed order: the flags cross product (prefixes x command
    words x flags x targets x suffixes, 13,608 commands) and then the shapes cross product (prefixes incl. RV-7
    definitions x sed/awk/gawk/git argument shapes x targets x suffixes incl. RV-9 lone-& writes)."""
    return _flag_scenarios() + _shape_scenarios()


def commands_sha256(scs) -> str:
    """A fingerprint of the scenario commands in order, so a golden can't drift from scenarios() (R-5)."""
    return hashlib.sha256("\n".join(sc["cmd"] for sc in scs).encode("utf-8")).hexdigest()


def is_listed_read(sc: dict) -> bool:
    """BR-6 allowed change: a READ_ONLY read shape per BR-1..BR-3 that may change from blocked to not blocked.

    True when the prefix isn't `FOO=1`; the word is a READ_ONLY member; and the suffix is none, `| head`,
    `2>/dev/null`, `>/dev/null 2>&1`, `; echo` or `"a|b"`.

    In the shapes part: the prefix isn't `FOO=1`, the suffix is one of those, and the shape is a read per
    BR-1..BR-3 (its `read` flag) whose command is a READ_ONLY member. sed, awk, gawk and git are never listed
    reads (Q-9): their outcome must equal the base exactly."""
    if sc["prefix"] == "FOO=1 ":
        return False
    if sc["suffix"] not in READ_SUFFIXES:
        return False
    if sc.get("part") == "shapes":
        return bool(sc["read"]) and sc["name"] in READ_ONLY_WORDS
    return sc["word"] in READ_ONLY_WORDS


def is_listed_block(sc: dict) -> bool:
    """BR-6 allowed change from BR-7: a command naming .light-harness/ with a lone `&` before a write is blocked
    with harness-files, whatever the base guard did (the RV-9 suffixes, which also trigger the fallback)."""
    return sc["suffix"] in LONE_AMP_SUFFIXES


def expected(sc: dict, base: str) -> tuple:
    """(set of acceptable new outcome codes, label) for scenario `sc` whose base-commit code is `base`."""
    if is_listed_block(sc):
        return {"b:harness-files"}, "b:harness-files (BR-7)"
    if is_listed_read(sc) and base == "b:harness-files":
        return {"p", "a"}, "p|a (listed read)"
    return {base}, base
