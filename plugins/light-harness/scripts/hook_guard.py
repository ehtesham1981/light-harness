#!/usr/bin/env python3
# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""PreToolUse hook: keeps agents inside the harness stages, without any settings in the work repo.

Active only in a repo where Light Harness has started a task (it has .git/light-harness/); inert
everywhere else. Exit code 2 blocks the tool call and shows stderr to the agent; a JSON
`permissionDecision` asks the human or skips the prompt.

File edits (Edit, Write, MultiEdit, NotebookEdit):
  1. The task's harness files (.light-harness/, including saved probes) are off limits, except the
     unowned sections of contract.md, and config.json before the plan is approved. So are the
     base-commit copies in .light-harness-base/. Agent permissions and agents
     (harness.MACHINERY) are too, unless the approved §8.1 lists them under .claude/.
  2. In contract.md, the Status line and harness-owned blocks are never edited by hand.
  3. Tests and protected files are read-only once the red gate has passed.
  4. Implementation files can only change in Build or Rework.
  5. While tasks are in progress, the main checkout isn't edited (work happens in .tasks/<name>/).
Reads: .env files are not read.
Bash: writes to the task's .light-harness/ files (reads with grep, sed -n, awk or git diff|log|show
  pass; quoting is honoured), human-only harness commands, history rewrites, branch/worktree surgery,
  tampering with the audit trail or the telemetry settings, and `rm -rf` are blocked; network and push
  commands ask; read-only and harness commands run without a prompt.
Every block and ask is also a `guard` telemetry event (rule id, tool, task, session, agent type).
"""
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H  # noqa: E402

PRE_APPROVAL = ("Draft", "Initialize", "Plan")
OWNED_RE = re.compile(r"harness:(" + "|".join(map(re.escape, H.OWNED_BLOCKS)) + r")\b|^Status:", re.M)


CTX: dict = {}  # this tool call: common dir, task, session, tool, agent type (for telemetry)


def note(decision: str, rule: str):
    c = CTX.get("common")
    if c:
        H.telemetry.emit(c, "guard", cwd=c, task=CTX.get("task"), session=CTX.get("session"), actor="agent",
                         decision=decision, rule=rule, tool=CTX.get("tool"), agent_type=CTX.get("agent_type"))


def block(msg: str, rule: str):
    note("block", rule)
    print(f"[light-harness] Blocked: {msg}", file=sys.stderr)
    sys.exit(2)


def decide(decision: str, reason: str, rule: str = ""):
    if decision != "allow":
        note(decision, rule)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "permissionDecision": decision,
                                             "permissionDecisionReason": f"[light-harness] {reason}"}}))
    sys.exit(0)


def existing(p: Path) -> Path:
    d = p if p.is_dir() else p.parent
    while not d.exists() and d != d.parent:
        d = d.parent
    return d


def git_common(d: Path):
    d = existing(d)
    try:
        r = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=d, capture_output=True,
                           encoding=H.UTF8, errors="replace")
    except OSError:
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    p = Path(r.stdout.strip())
    return (p if p.is_absolute() else d / p).resolve()


def active(d: Path):
    """The repo's git common dir if Light Harness has started a task in it, else None."""
    c = git_common(d)
    return c if c and (c / H.STATE).is_dir() else None


def task_of(root):
    """The task (slug) whose worktree is `root`, for telemetry."""
    if not root:
        return None
    H.set_root(root)
    return H.quiet_slug()


def owned_spans(text: str):
    spans = [m.span() for name in H.OWNED_BLOCKS if (m := H.block_span(text, name))]
    if (m := re.search(r"^Status:.*$", text, re.M)):
        spans.append(m.span())
    return spans


def owned_snapshot(text: str):
    return [H.get_status(text)] + [H.block(name, text) for name in H.OWNED_BLOCKS]


# --- Bash ---------------------------------------------------------------------
GIT = r"\bgit(?:\s+-[cC]\s+\S+|\s+--[\w-]+(?:=\S+)?)*\s+"   # `git`, its global options, then the subcommand
BLOCK_BASH = [  # (pattern, why, only if this also matches, rule id for telemetry)
    (r"\b(approve|unlock-tests|forget|uninstall|opt-in|opt-out)\b"
     r"|\bprobe\b[^\n;&|]*?(?:^|\s)['\"\\]*d[\\'\"]*r[\\'\"]*o[\\'\"]*p['\"]*(?=\s|$)",
     "approve, unlock-tests, forget, uninstall, telemetry opt-in/opt-out and probe drop are human-only. Ask the "
     "human to run it in their own terminal.", r"light-harness|harness\.py", "human-only"),
    (r"\bLIGHT_HARNESS_(OTLP\w*|TELEMETRY|SESSION_ID|HUMAN_GAP_CAP|GIT_COMMON)=|(^|[\s;&|(])(unset|-u)\s+[^\n;&|]*\bLIGHT_HARNESS_|"
     + GIT + r"config\b[^\n;&|]*\b(?i:lightharness)\.",
     "telemetry settings and the engineer's opt-in belong to the human. Ask them to change it.", None,
     "telemetry-settings"),
    (r"(^|[\s;&|(])(script|unbuffer|winpty|expect)(\s|$)", "faking an interactive terminal is not allowed.", None,
     "fake-terminal"),
    (GIT + r"(rebase|reset|filter-branch|filter-repo|checkout|switch|merge|worktree|update-ref|symbolic-ref|"
     r"replace|commit-tree|mktree|read-tree|gc|prune)\b|" + GIT + r"hash-object\b[^\n;&|]*\s-w\b|"
     + GIT + r"reflog\s+(delete|expire)\b",
     "this git command could switch branches, rewrite history or touch other tasks. Stay on your task's "
     "branch; tasks are started and finished with `light-harness start-task` / `finish-task`.", None, "git-surgery"),
    (GIT + r"commit\b[^\n;&|]*--amend", "amending commits is not allowed.", None, "amend"),
    (GIT + r"push\b[^\n;&|]*(\s--force\S*|\s-f\b|\s\+)", "force-pushing is not allowed.", None, "force-push"),
    (GIT + r"branch\b[^\n;&|]*\s(-d|-D|--delete|-m|-M|--move|-c|-C|--copy|-f|--force)\b",
     "deleting, moving or resetting branches is not allowed.", None, "branch-change"),
    (GIT + r"tag\b[^\n;&|]*\s(-d|--delete|-f|--force)\b", "deleting or moving tags is not allowed.", None,
     "tag-change"),
    (r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r|-r\s+-f|-f\s+-r)", "`rm -rf` is not allowed.", None,
     "rm-rf"),
    (r"\.git[/\\]+light-harness|refs/light-harness", "the task's audit trail and telemetry are maintained by the "
     "harness only. Read them with `light-harness show` / `log` / `metrics`.", None, "audit-trail"),
]
ASK_BASH = r"(^|[\s;&|('\"])(curl|wget|ssh|scp|psql|mysql)(\s|$|['\"])|" + GIT + r"push\b"
LH_SAFE = (r"(status|check|set-status|redgate|verify|verdict|start-task|finish-task|tasks|show|log|metrics|"
           r"telemetry\s+status|baseline|probe\s+(?:add|list|run)|version)\b.*")
SAFE_SEGMENT = [
    r"(\S*[/\\])?light-harness\s+" + LH_SAFE,
    r"sh\s+(\"[^\"]*light-harness\"|'[^']*light-harness')\s+" + LH_SAFE,
    r"(python3?|py\s+-3)\s+-m\s+pytest\b.*",
    r"git\s+(status|diff|log|show|add)\b.*",
    r"git\s+commit\s+(-q\s+)?-m\s+(\"[^\"]*\"|'[^']*')",
    r"cd\s+\S+",
]
READ_ONLY = {"cat", "head", "tail", "less", "more", "grep", "rg", "wc", "ls", "diff", "file", "stat"}


COPY_CMDS = {"cp", "mv", "ln", "install", "rsync"}          # the destination is the last argument (or -t DIR)
TOUCH_CMDS = {"rm", "rmdir", "unlink", "tee", "touch", "truncate", "patch", "chmod", "chown", "mkdir", "shred"}
WRAPPERS = {"sudo", "env", "command", "nohup", "nice", "time", "exec", "xargs", "stdbuf", "timeout"}


def base_writes(cmd: str):
    """Block Bash writes into the base-commit copies (.light-harness-base/). Judged by each segment's
    command word and its destination, so reads (`grep rm .light-harness-base/x`, a script given the
    folder as an argument) stay allowed. Best effort: probes get a fresh export from git outside the task
    folder, and the suite runs with this folder moved aside, so a write this misses never reaches a check."""
    in_base = False
    for seg in re.split(r"&&|\|\||[;|\n]", cmd):
        words = seg.split()
        while words and (words[0] in WRAPPERS or re.fullmatch(r"\w+=\S*|-\S+", words[0])):
            words = words[1:]
        if not words:
            continue
        verb, args = words[0].rsplit("/", 1)[-1], [w.strip("'\"") for w in words[1:]]
        if verb == "popd" or (verb == "cd" and args[:1] == ["-"]):
            in_base = False
            continue
        if verb in ("cd", "pushd"):
            inside = bool(args) and H.BASE_DIR in args[0] and not args[0].rstrip("/\\").endswith("..")
            in_base = inside or (in_base and bool(args) and not args[0].startswith(("/", "~", "..")))
            continue
        paths = [a for a in args if not a.startswith("-")]
        target = ""
        redirects = [t for t in re.findall(r"(?:^|[^<&>])\d*>{1,2}\s*([^\s&>]\S*)", seg) if not t.startswith("/dev/")]
        if redirects:
            target = redirects[-1]
        elif verb in COPY_CMDS:
            t = next((args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-t", "--target-directory")), None)
            t = t or next((a.split("=", 1)[1] for a in args if a.startswith("--target-directory=")), None)
            target = t or (paths[-1] if len(paths) >= 2 else "")
        elif verb in TOUCH_CMDS or (verb == "sed" and any(a.startswith("-i") for a in args)) or verb == "dd":
            hits = [p for p in (paths if verb != "dd" else [a[3:] for a in args if a.startswith("of=")])
                    if H.BASE_DIR in p or (in_base and not p.startswith(("/", "~", "..")))]
            target = hits[0] if hits else ""
        if target and (H.BASE_DIR in target or (in_base and not target.startswith(("/", "~", "..")))):
            block(f"{H.BASE_DIR}/ holds the base-commit copies, the oracle for unchanged behaviour. "
                  "`light-harness baseline` writes it; read or run it, never change it.", "harness-files")


BASE_SPLIT = r"&&|\|\||[;|\n]"
# Unquoted separators: && || |& | ; newline, and a lone & (not part of &&, &>, >&, <&, |&, or 2>&1)
LIVE_SPLIT = re.compile(r"&&|\|\||\|&|(?<!>)\||[;\n]|(?<![&<>|])&(?![&>])")
SUBSTITUTION = re.compile(r"\$\(|`|<\(|>\(")


def unquoted_mask(cmd: str):
    """(text, mask) for `cmd` as bash reads its quoting. `text` is `cmd` with line continuations (an
    unescaped backslash-newline outside single quotes) removed. `mask` is `text` with every quoted or
    backslash-escaped character replaced by `_` (same length), so separators and redirects are looked for
    in unquoted text only. None when the quoting is beyond this parser: unbalanced quotes, a trailing
    backslash."""
    text, out, state, i = [], [], "", 0
    while i < len(cmd):
        c = cmd[i]
        nxt = cmd[i + 1] if i + 1 < len(cmd) else ""
        if state == "'":
            state = "" if c == "'" else state
            text.append(c)
            out.append("_")
        elif c == "\\":
            if not nxt:
                return None
            if nxt != "\n":
                text.append(c + nxt)
                out.append("__")
            i += 1
        elif state == '"':
            state = "" if c == '"' else state
            text.append(c)
            out.append("_")
        elif c in "'\"":
            state = c
            text.append(c)
            out.append("_")
        else:
            text.append(c)
            out.append(c)
        i += 1
    return None if state else ("".join(text), "".join(out))


def live_segments(cmd: str) -> list:
    """The command's (segment, mask) pairs, split only on unquoted separators (BR-1, BR-7). None when the
    quoting is beyond this parser (see unquoted_mask), or the command has a heredoc or a comment: the caller
    then judges the command as the base guard did. ANSI-C and locale quotes (`$'…'`, `$"…"`) and `${…}`,
    which can nest quotes, fall back wherever they appear, even inside quotes."""
    parsed = None if re.search(r"\$['\"{]", cmd) else unquoted_mask(cmd)
    if parsed is None:
        return None
    cmd, mask = parsed
    if re.search(r"(?<!<)<<(?!<)|(?:^|[\s;&|(])#", mask):
        return None
    segs, start = [], 0
    for m in LIVE_SPLIT.finditer(mask):
        segs.append((cmd[start:m.start()], mask[start:m.start()]))
        start = m.end()
    segs.append((cmd[start:], mask[start:]))
    return segs


def writes_by_redirect(mask: str) -> bool:
    """True when the segment's unquoted text redirects output anywhere but /dev/null. Duplicating or
    closing a file descriptor (2>&1, >&2, 3>&-) isn't a write (BR-2)."""
    if "<>" in mask:
        return True
    for m in re.finditer(r"(?:\d*|&)(>>|>\||>&|>)", mask):
        rest = mask[m.end():]
        if m.group(1) == ">|":
            return True
        if m.group(1) == ">&":   # a descriptor (2>&1, >&-) or, as bash's `>&word`, /dev/null
            if not re.match(r"\s*(\d+|-|/dev/null)(?=$|[\s;&|)])", rest):
                return True
        elif not re.match(r"\s*/dev/null(?=$|[\s;&|)])", rest):
            return True
    return False


SED_OPTS = {"-n", "--quiet", "--silent", "-E", "-r", "--regexp-extended", "-s", "--separate", "-z",
            "--null-data", "-u", "--unbuffered", "--posix", "--debug", "--sandbox"}


def _sed_delimited(s: str, i: int) -> int:
    """Index just past the `/`-delimited text starting at s[i] == "/", or -1. Backslash escapes a char."""
    if i >= len(s) or s[i] != "/":
        return -1
    i += 1
    while i < len(s) and s[i] != "/":
        if s[i] == "\n":
            return -1
        i += 2 if s[i] == "\\" else 1
    return i + 1 if i < len(s) else -1


def _sed_address(s: str, i: int) -> int:
    """Index past one address (N, N~M, $, /re/[IM]) at s[i], or i when there is none, or -1 if malformed."""
    if i < len(s) and s[i] == "$":
        return i + 1
    if i < len(s) and s[i] == "/":
        j = _sed_delimited(s, i)
        while 0 <= j < len(s) and s[j] in "IM":
            j += 1
        return j
    m = re.match(r"\d+(?:~\d+)?", s[i:])
    return i + m.end() if m else i


def sed_script_reads(s: str) -> bool:
    """A sed script made only of addresses and the p P = l q Q d D n N g G h H x { } and s/re/repl/[gpiImM0-9]
    commands (no w, W, e, r, R, y, and no other delimiter). A single left-to-right scan, so its cost is linear
    in the script's length whatever the input."""
    i, n = 0, len(s)
    while i < n:
        if s[i] in " \t\n;":
            i += 1
            continue
        i = _sed_address(s, i)
        if i < 0:
            return False
        while i < n and s[i] in " \t":
            i += 1
        if i < n and s[i] == ",":
            j = _sed_address(s, i + 1)
            if j <= i + 1:
                return False
            i = j
        while i < n and s[i] in " \t!":
            i += 1
        if i >= n:
            return False
        c = s[i]
        if c in "pP=lgGhHxdDnN{}":
            i += 1
        elif c in "qQ":
            i += 1
            while i < n and (s[i] in " \t" or s[i].isdigit()):
                i += 1
        elif c == "s":
            i = _sed_delimited(s, i + 1)
            if i < 0:
                return False
            i = _sed_delimited(s, i - 1)   # the replacement shares the middle `/`
            if i < 0:
                return False
            while i < n and s[i] in "gpiImM0123456789":
                i += 1
        else:
            return False
    return True


def sed_reads(args: list) -> bool:
    """sed that only prints: every option on the read allow-list (no -i in any form, no -f) and every script
    made of addresses and the p P = l q Q d D n N g G h H x { } s///[gpiImM0-9] commands (no w, W, e, r, R)."""
    scripts, operands, i = [], [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            operands += args[i + 1:]
            break
        if a == "-e" or a == "--expression":
            if i + 1 == len(args):
                return False
            scripts.append(args[i + 1])
            i += 1
        elif a.startswith("--expression="):
            scripts.append(a.split("=", 1)[1])
        elif a.startswith("-e") and not a.startswith("--"):
            scripts.append(a[2:])
        elif a.startswith("--"):
            if a not in SED_OPTS:
                return False
        elif a.startswith("-") and len(a) > 1:
            if not set(a[1:]) <= set("nErszu"):
                return False
        else:
            operands.append(a)
        i += 1
    if not scripts:
        if not operands:
            return False
        scripts.append(operands[0])
    return all(sed_script_reads(s) for s in scripts)


def awk_program_writes(prog: str) -> bool:
    """True unless the awk program provably can't write or run anything. awk can only write a file through
    `>`/`>>`/`|` after print or printf, and only run a command through a pipe, system() or (gawk) an
    indirect `@f()` call or `@include`/`@load`. So any `|` other than `||`, any `@`, system, getline, or any
    `>` after the first print/printf counts as a write. Comparisons such as `NR>5` before any print pass."""
    if "@" in prog or "|" in prog.replace("||", "") or re.search(r"\b(?:system|getline)\b", prog):
        return True
    m = re.search(r"\bprintf?\b", prog)
    return bool(m) and ">" in prog[m.end():]


def awk_reads(args: list) -> bool:
    """awk/gawk that only prints: options on the allow-list (-F, -v, --field-separator, --assign, --posix,
    -S/--sandbox; no -i, -f, -l, -E) and a program with no system, getline, @include/@load, |&, or
    print/printf redirected to a file or a command."""
    program, i = None, 0
    while i < len(args):
        a = args[i]
        if a == "--":
            program = args[i + 1] if i + 1 < len(args) else None
            break
        if not a.startswith("-") or a == "-":
            program = a
            break
        if a in ("-F", "-v", "--field-separator", "--assign"):
            i += 1
        elif not (a.startswith(("-F", "-v", "--field-separator=", "--assign="))
                  or a in ("--posix", "-S", "--sandbox")):
            return False
        i += 1
    return program is not None and not awk_program_writes(program)


def git_reads(args: list) -> bool:
    """git [--no-pager] diff|log|show with no option that writes a file or runs a program: nothing starting
    `--ou` or `--ext` (BR-3), nor git's shorter abbreviations of --output and --ext-diff (`--o`, `--e`, `--ex`)."""
    if args[:1] == ["--no-pager"]:
        args = args[1:]
    if not args or args[0] not in ("diff", "log", "show"):
        return False
    for a in args[1:]:
        opt = a.split("=", 1)[0]
        if opt.startswith(("--ou", "--ext")) or (len(opt) > 2 and ("--output".startswith(opt)
                                                                  or "--ext-diff".startswith(opt))):
            return False
    return True


def expands(seg: str) -> bool:
    """True when bash would expand part of the segment before the command sees it: `$` outside single
    quotes (parameters, also inside double quotes), or an unquoted `{`, `*`, `?` or `[` (braces, globs).
    The words shlex returns are then not the words the command gets, so an option can hide in them."""
    parsed = unquoted_mask(seg)
    if parsed is None:
        return True
    text, mask = parsed
    state, i = "", 0
    while i < len(text):
        c = text[i]
        if state == "'":
            state = "" if c == "'" else state
        elif c == "\\":
            i += 1                      # an escaped character, `\$` included, is literal
        elif c == "'" and not state:
            state = "'"
        elif c == '"':
            state = "" if state == '"' else '"'
        elif c == "$":
            return True
        i += 1
    return bool(re.search(r"[{*?\[]", mask))


# Commands that can run beside a sed/awk/git read without changing what that read runs: they can't define
# a function or alias, change PATH or the environment, or source a file (RV-7). `printf -v` assigns, so no printf.
SIDE_OK = READ_ONLY | {"cd", "echo", "true", "false", "pwd"}


def harmless(seg: str, mask: str) -> bool:
    """A segment that can't redefine the command another segment runs: empty, a recognised read, or a
    SIDE_OK word with no write redirect."""
    words = seg.split()
    if not words:
        return True
    if words[0] in SIDE_OK:
        return not writes_by_redirect(mask)
    return live_read(seg, mask)


def live_read(seg: str, mask: str) -> bool:
    """A segment naming .light-harness/ is a read (BR-2, BR-3): no write redirect, and a read-only command
    word (judged raw, as before), or sed, awk/gawk or git in a read-only form."""
    words = seg.split()
    if not words or writes_by_redirect(mask):
        return False
    if words[0] in READ_ONLY:
        return True
    check = {"sed": sed_reads, "awk": awk_reads, "gawk": awk_reads, "git": git_reads}.get(words[0])
    if check is None or expands(seg):
        return False
    try:
        args = shlex.split(seg)[1:]
    except ValueError:
        return False
    return check(args)


def names_live(text: str) -> bool:
    return f"{H.LIVE}/" in text or f"{H.LIVE}\\" in text


def live_rules(cmd: str):
    """Block Bash commands that change the task's contract or config; reads pass (issue #23)."""
    if not names_live(cmd):
        return
    task = CTX.get("task")
    if not task:
        m = re.search(r"\.tasks[/\\]([^/\\\s'\"]+)[/\\]" + re.escape(H.LIVE), cmd)
        task = m.group(1) if m else "<name>"
    why = (f"{H.LIVE}/ holds the task's contract and config. Read the contract with the Read tool or "
           f"`light-harness show --task {task}`. Edit contract.md with the Edit tool, and change status "
           "with `light-harness set-status`.")
    if SUBSTITUTION.search(cmd):
        block(why, "harness-files")
    segs = live_segments(cmd)
    if segs is None:   # quoting this parser can't follow: judge it as the base guard did
        for seg in re.split(BASE_SPLIT, cmd):
            words = seg.split()
            if names_live(seg) and (">" in seg or not words or words[0] not in READ_ONLY):
                block(why, "harness-files")
        return
    for seg, mask in segs:
        if names_live(seg) and not live_read(seg, mask):
            block(why, "harness-files")
    # sed, awk and git reads are new (the base blocked them all). Accept one only when nothing else in the
    # command could make that word run something else: a function, alias, eval, source, PATH change, … (RV-7)
    if any(names_live(seg) and seg.split()[0] not in READ_ONLY for seg, _ in segs) and not all(
            harmless(seg, mask) for seg, mask in segs):
        block(why, "harness-files")


def bash_rules(cmd: str):
    for pat, why, needs, rule in BLOCK_BASH:
        if re.search(pat, cmd) and (needs is None or re.search(needs, cmd)):
            block(why, rule)
    live_rules(cmd)
    base_writes(cmd)
    if re.search(ASK_BASH, cmd):
        decide("ask", "network access and pushes need the human's OK during a task.", "network")
    # Skip the prompt only for a plain chain (`a && b`) of known-safe commands
    parts = cmd.strip().split("&&")
    if not re.search(r"[`$<>;|&\n]", cmd.replace("&&", " ")) and all(
            any(re.fullmatch(p, s.strip()) for p in SAFE_SEGMENT) for s in parts):
        decide("allow", "harness or read-only command")
    sys.exit(0)


# --- file edits ---------------------------------------------------------------
def edit_rules(tool: str, ti: dict, p: Path, common: Path):
    if H.is_within(p, common):
        block("files inside .git are maintained by git and the harness.", "git-dir")
    root = H.task_root_for(p)
    if root is None:
        # The main checkout while tasks are in progress: their work belongs in their own folders
        top = H._toplevel(existing(p))
        if not top:
            sys.exit(0)
        H.set_root(top)
        try:
            main, busy = H.main_checkout(), H.tasks_in_progress()
        except SystemExit:
            sys.exit(0)
        if busy and H.same_path(top, main):
            block(f"tasks are in progress ({', '.join(s for s, _ in busy)}); their work happens in their "
                  "own folders (.tasks/<name>/), not in the main checkout.", "main-checkout")
        sys.exit(0)

    CTX["task"] = task_of(root)
    H.set_root(root)
    rel = p.resolve().relative_to(H.ROOT).as_posix()
    current = H.CONTRACT.read_text(encoding=H.UTF8)
    status = H.get_status(current)

    if rel == f"{H.LIVE}/config.json":
        if status in PRE_APPROVAL:
            sys.exit(0)  # stack configuration is part of planning, frozen by approval
        block(f"{rel} is frozen once the plan is approved.", "config-frozen")
    if rel.startswith(f"{H.LIVE}/") and rel != f"{H.LIVE}/contract.md":
        block(f"{rel} is harness machinery. Use `light-harness ...` commands instead of editing it.", "harness-files")
    if rel.startswith(f"{H.BASE_DIR}/"):
        block(f"{rel} is a base-commit copy, the oracle for unchanged behaviour. `light-harness baseline` "
              "writes it; read or run it, never edit it.", "harness-files")
    if H.matches(rel, H.MACHINERY):
        # Allowed only when the human approved it: a .claude/ entry in §8.1 of the current, approved plan
        if not (status in ("Build", "Rework") and H.approval_state(current)[0]
                and H.machinery_allowed(rel, current)):
            block(f"{rel} controls agent permissions or agents; it can't change during a task unless the "
                  "approved plan lists it under .claude/ in §8.1.", "machinery")

    cfg = H.load_config()

    if rel == f"{H.LIVE}/contract.md":
        if tool == "Write":
            if owned_snapshot(current) != owned_snapshot(ti.get("content", "")):
                block("this rewrite changes the Status line or a harness-owned block. "
                      "Change status with `light-harness set-status`; leave owned blocks alone.", "contract-owned")
        else:
            edits = ti.get("edits") or [ti]
            for e in edits:
                old, new = e.get("old_string", ""), e.get("new_string", "")
                if OWNED_RE.search(old) or OWNED_RE.search(new):
                    block("edit touches the Status line or a harness marker. Use `light-harness` commands.",
                          "contract-owned")
                pos = current.find(old) if old else -1
                if pos >= 0 and any(s <= pos < t for s, t in owned_spans(current)):
                    block("edit falls inside a harness-owned block (approval/red gate/verify/verdict/status log).",
                          "contract-owned")
        sys.exit(0)

    is_test = H.matches(rel, cfg["test_globs"])
    is_protected = H.matches(rel, cfg["protected_globs"])

    if (is_test or is_protected) and H.redgate_state()[0]:
        block(f"{rel} is locked by the red gate. Make the implementation satisfy the tests; never edit them. "
              "If a test is genuinely wrong, log it in contract §10.3 and ask a human to run "
              "`light-harness unlock-tests`.", "locked-test")

    if is_protected and status in PRE_APPROVAL:
        sys.exit(0)  # e.g. sample data gathered during /task-init

    if status not in ("Build", "Rework"):
        block(f"status is {status}. Code and tests may only change in Build/Rework "
              f"(after the plan is approved and /task-implement starts).", "stage")
    sys.exit(0)


def main():
    try:
        # Claude Code sends UTF-8; decode explicitly so contract.md rewrites compare correctly
        # against the file (read as UTF-8) instead of being misread with the locale's codepage.
        data = json.loads(sys.stdin.buffer.read().decode(H.UTF8))
    except (json.JSONDecodeError, UnicodeDecodeError):
        sys.exit(0)
    tool = data.get("tool_name", "")
    ti = data.get("tool_input", {}) or {}
    cwd = Path(data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    CTX.update(session=data.get("session_id"), tool=tool, agent_type=data.get("agent_type"))

    if tool == "Bash":
        common = active(cwd)
        if common:
            cmd = ti.get("command", "") or ""
            m = re.search(r"--task[\s=]+[\"']?([^\s\"';&|]+)", cmd)
            CTX.update(common=common, task=re.sub(r"[^a-z0-9]+", "-", m.group(1).lower()).strip("-") if m
                       else task_of(H.task_root_for(cwd)))
            bash_rules(cmd)
        sys.exit(0)

    fp = ti.get("file_path") or ti.get("notebook_path")
    if not fp:
        sys.exit(0)
    p = Path(fp)
    if not p.is_absolute():
        p = cwd / p
    common = active(p)
    if not common:
        sys.exit(0)  # not a repo where Light Harness runs tasks (hooks fire everywhere)
    CTX["common"] = common

    if tool == "Read":
        n = p.name
        if n == ".env" or (n.startswith(".env.") and n.split(".")[-1] not in ("example", "sample", "template")):
            block(f"{n} holds secrets and isn't read during a task; ask the human for what you need.", "env-file")
        sys.exit(0)
    edit_rules(tool, ti, p, common)


if __name__ == "__main__":
    main()
