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


def scenarios() -> list:
    """The full deterministic BR-6 cross product: 3 x 27 x 7 x 3 x 8 = 13,608 commands, in a fixed order."""
    out = []
    for prefix in PREFIXES:
        for word, name, body in WORDS:
            for flag in FLAGS:
                for target in TARGETS:
                    for suffix in SUFFIXES:
                        cmd = prefix + " ".join(p for p in (name, flag, body, target) if p) + suffix
                        out.append({"cmd": cmd, "prefix": prefix, "word": word, "flag": flag,
                                    "target": target, "suffix": suffix})
    return out


def commands_sha256(scs) -> str:
    """A fingerprint of the scenario commands in order, so a golden can't drift from scenarios() (R-5)."""
    return hashlib.sha256("\n".join(sc["cmd"] for sc in scs).encode("utf-8")).hexdigest()


def is_listed_read(sc: dict) -> bool:
    """BR-6 allowed change: a read shape per BR-1..BR-3 that may change from blocked to not blocked.

    True when the prefix isn't `FOO=1`; the word is a READ_ONLY member, git diff/log/show (any flag),
    sed with no flag or `-n`, or awk/gawk with no flag; and the suffix is none, `| head`, `2>/dev/null`,
    `>/dev/null 2>&1`, `; echo` or `"a|b"`."""
    if sc["prefix"] == "FOO=1 ":
        return False
    if sc["suffix"] not in READ_SUFFIXES:
        return False
    w, f = sc["word"], sc["flag"]
    return (w in READ_ONLY_WORDS or w in ("git diff", "git log", "git show")
            or (w == "sed" and f in ("", "-n")) or (w in ("awk", "gawk") and f == ""))
