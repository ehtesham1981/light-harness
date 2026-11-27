#!/usr/bin/env sh
# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
# Light Harness hook launcher: finds a Python 3.9+ interpreter (python3, python or py -3, so it
# works on macOS, Linux and Windows/Git Bash) and runs hook_guard.py with the hook's stdin.
#
# The guard only matters in a repo where Light Harness has started a task (.git/light-harness/
# exists). Everywhere else it exits at once, so the plugin never gets in the way of other work.
# If no Python is found in such a repo, it fails CLOSED (blocks the call with a clear message).
#
# It runs before every Read, Edit, Write and Bash call, so it avoids work (issue #24):
# - the interpreter is the one session.sh (or an earlier search here) recorded for this session in
#   <common>/light-harness/py/<session id>. The search below runs only when there's no usable record,
#   or when the recorded one gives no judgement (any exit other than 0 or 2); then nothing it printed
#   is shown, and the search blocks when it finds no Python;
# - a Read is decided here, without Python, when it plainly can't be a .env file.
# Git finds the common dir, as before: nothing here reads .git itself.
# `pwd -W` gives a native path on Windows (Git Bash), so Windows Python can open the script even
# when MSYS path conversion is off. It's asked only on Git Bash/Cygwin for a folder that isn't already a
# native drive path, or for one written with backslashes (elsewhere it fails and `pwd` is used).
case $0 in */*) DIR=${0%/*};; *\\*) DIR=${0%\\*};; *) DIR=.;; esac
case $DIR in
  [A-Za-z]:/*) ;;
  *\\*) DIR=$(cd "$DIR" && { pwd -W 2>/dev/null || pwd; });;
  *) case ${OSTYPE:-} in msys*|cygwin*) DIR=$(cd "$DIR" && { pwd -W 2>/dev/null || pwd; });; esac;;
esac
PROJECT=${CLAUDE_PROJECT_DIR:-$PWD}

GD=$(git -C "$PROJECT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || exit 0
[ -d "$GD/light-harness" ] || exit 0
# A NUL byte becomes \001: the shell can't hold a NUL, and either one makes the JSON invalid, as at base.
IN=$(tr '\000' '\001')

SID=
case $IN in
  *'"session_id":"'*) t=${IN#*'"session_id":"'}; SID=${t%%'"'*};;
  *'"session_id": "'*) t=${IN#*'"session_id": "'}; SID=${t%%'"'*};;
esac
case $SID in ""|*[!A-Za-z0-9_-]*) SID=;; esac
[ ${#SID} -le 128 ] || SID=
# usable <path> <record>: an absolute path to an executable file, not changed since it was recorded
# (a path ending in CR fails -f)
usable() {
  case $1 in /*|[A-Za-z]:/*|[A-Za-z]:\\*) ;; *) return 1;; esac
  [ -f "$1" ] && [ -x "$1" ] && ! [ "$1" -nt "$2" ]
}
REC=; PY=
if [ -n "$SID" ]; then
  REC=$GD/light-harness/py/$SID
  if [ -f "$REC" ]; then { IFS= read -r PY < "$REC"; } 2>/dev/null || :; usable "$PY" "$REC" || PY=; fi
fi
# replay: copy stdin to stdout byte for byte (CRLF and a missing final newline kept), with builtins only
replay() { l=; while IFS= read -r l; do printf '%s\n' "$l"; done; printf '%s' "$l"; }

if [ -n "$PY" ]; then
  # Read fast path (BR-4): only Claude Code's plain shape, one tool_name/tool_input/file_path, top-level
  # tool_name, exactly one cwd, top-level before tool_name and absolute (so a file_path such as "." or
  # "C:" can't fold into the project folder or PWD), no \u escape (the only JSON escape that can spell
  # .env) and no ".env" anywhere. Anything else goes to hook_guard.py.
  fast=1
  case $IN in "{"*) ;; *) fast=;; esac
  case $IN in *'\u'*|*.[Ee][Nn][Vv]*|*'"notebook_path"'*) fast=;; esac
  for k in '"tool_name"' '"tool_input"' '"file_path"'; do
    case $IN in *"$k"*"$k"*) fast=;; *"$k"*) ;; *) fast=;; esac
  done
  case $IN in *'"tool_name":"Read","tool_input":{"file_path":"'*) ;; *) fast=;; esac
  if [ -n "$fast" ]; then
    pre=${IN%%'"tool_name"'*}
    case $pre in *,) ;; *) fast=;; esac
    r=${pre#*'{'}; case $r in *'{'*) fast=;; esac
    case $IN in *'"cwd"'*'"cwd"'*) fast=;; esac
    case $pre in *'"cwd":"/'*|*'"cwd":"\/'*|*'"cwd":"'[A-Za-z]':/'*|*'"cwd":"'[A-Za-z]':\/'*|*'"cwd":"'[A-Za-z]':\\'*) ;; *) fast=;; esac
  fi
  [ -n "$fast" ] && exit 0
  # The recorded run writes to files, shown only for a judgement (exit 0 or 2). The files are created
  # first with `true` (not `:`, a special builtin, whose failed redirection would end the shell), and the
  # group's 2>/dev/null drops the shell's own report of a child killed by a signal.
  # Named by session and pid: a pid alone can repeat across pid namespaces (WSL and Git Bash, containers).
  T=$GD/light-harness/py/.run.$SID.$$
  if { true >"$T.o" && true >"$T.e"; } 2>/dev/null; then
    { "$PY" "$DIR/hook_guard.py" >"$T.o" 2>"$T.e" <<LH_GUARD_STDIN_EOF_24
$IN
LH_GUARD_STDIN_EOF_24
    } 2>/dev/null
    rc=$?
    case $rc in 0|2) replay <"$T.e" >&2; replay <"$T.o"; exit $rc;; esac
  fi
fi

for W in python3 python "py -3"; do
  # shellcheck disable=SC2086  # "py -3" must word-split
  if OUT=$($W -c 'import sys; ok = sys.version_info >= (3, 9); ok and sys.stdout.write(sys.executable); sys.exit(0 if ok else 1)' 2>/dev/null); then
    if [ -n "$SID" ]; then   # record it for the rest of the session, if session.sh didn't
      cur=; [ -f "$REC" ] && { { IFS= read -r cur < "$REC"; } 2>/dev/null || :; }
      if ! usable "$cur" "$REC" && [ -f "$OUT" ]; then
        { mkdir -p "$GD/light-harness/py" && printf '%s' "$OUT" > "$REC"; } 2>/dev/null
      fi
    fi
    # shellcheck disable=SC2086
    printf '%s\n' "$IN" | $W "$DIR/hook_guard.py"
    exit $?
  fi
done

echo "[light-harness] Blocked: Python 3.9+ not found (tried python3, python, py -3), so the harness guard can't run. Install Python 3.9+ and make it available on PATH." >&2
exit 2
