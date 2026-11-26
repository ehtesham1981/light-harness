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
# - the git common dir comes from $PROJECT/.git with shell builtins, and from git only when that
#   doesn't find a task repo (or a GIT_* variable changes how git would find it);
# - the interpreter is the one session.sh (or an earlier search here) recorded for this session in
#   <common>/light-harness/py/<session id>; the search below runs only when there's no usable record,
#   or the recorded one can't start (any exit other than 0 or 2). It's never `exec`ed, so a broken
#   record falls back to the search, which blocks when it finds nothing;
# - a Read is decided here, without Python, when it plainly can't be a .env file;
# - hook_guard.py gets the common dir in LIGHT_HARNESS_GIT_COMMON instead of asking git again.
# `pwd -W` gives a native path on Windows (Git Bash), so Windows Python can open the script even
# when MSYS path conversion is off (MSYS_NO_PATHCONV=1).
case $0 in */*) DIR=${0%/*};; *) DIR=.;; esac
case $DIR in /*) if [ -n "${MSYSTEM:-}" ] && [ -n "${MSYS_NO_PATHCONV:-}" ]; then DIR=$(cd "$DIR" && pwd -W); fi;; esac
PROJECT=${CLAUDE_PROJECT_DIR:-$PWD}
CR=$(printf '\r')

GD=
if [ -z "${GIT_DIR:-}${GIT_COMMON_DIR:-}${GIT_WORK_TREE:-}${GIT_CEILING_DIRECTORIES:-}${GIT_DISCOVERY_ACROSS_FILESYSTEM:-}" ]; then
  if [ -d "$PROJECT/.git" ]; then
    GD=$PROJECT/.git
  elif [ -f "$PROJECT/.git" ]; then   # a worktree: `gitdir: <dir>`, and <dir>/commondir names the common dir
    l=; IFS= read -r l < "$PROJECT/.git" || :
    case $l in "gitdir: "*)
      g=${l#gitdir: }; g=${g%"$CR"}
      case $g in /*|[A-Za-z]:*) ;; *) g=$PROJECT/$g;; esac
      c=; [ -f "$g/commondir" ] && { IFS= read -r c < "$g/commondir" || :; }
      c=${c%"$CR"}
      case $c in "") GD=$g;; /*|[A-Za-z]:*) GD=$c;; *) GD=$g/$c;; esac;;
    esac
  fi
  [ -n "$GD" ] && [ -d "$GD/light-harness" ] || GD=
fi
if [ -z "$GD" ]; then
  GD=$(git -C "$PROJECT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || exit 0
fi
[ -d "$GD/light-harness" ] || exit 0
IN=$(cat)

SID=
case $IN in
  *'"session_id":"'*) t=${IN#*'"session_id":"'}; SID=${t%%'"'*};;
  *'"session_id": "'*) t=${IN#*'"session_id": "'}; SID=${t%%'"'*};;
esac
case $SID in ""|*[!A-Za-z0-9_-]*) SID=;; esac
[ ${#SID} -le 128 ] || SID=
# usable <path> <record>: an absolute path to an executable file, not changed since it was recorded
usable() {
  case $1 in /*|[A-Za-z]:/*|[A-Za-z]:\\*) ;; *) return 1;; esac
  case $1 in *"$CR"*) return 1;; esac
  [ -f "$1" ] && [ -x "$1" ] && ! [ "$1" -nt "$2" ]
}
REC=; PY=
if [ -n "$SID" ]; then
  REC=$GD/light-harness/py/$SID
  if [ -f "$REC" ]; then IFS= read -r PY < "$REC" || :; usable "$PY" "$REC" || PY=; fi
fi

if [ -n "$PY" ]; then
  # Read fast path (BR-4): only Claude Code's plain shape, one tool_name/tool_input/file_path, top-level
  # tool_name, string cwd, no \u escape (the only JSON escape that can spell .env) and no ".env" anywhere.
  # Anything else goes to hook_guard.py.
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
    t=$IN
    while [ -n "$fast" ]; do
      case $t in *'"cwd":'*) t=${t#*'"cwd":'}; case $t in '"'*) ;; *) fast=;; esac;; *) break;; esac
    done
  fi
  [ -n "$fast" ] && exit 0
  LIGHT_HARNESS_GIT_COMMON=$GD "$PY" "$DIR/hook_guard.py" <<LH_GUARD_STDIN_EOF_24
$IN
LH_GUARD_STDIN_EOF_24
  rc=$?
  case $rc in 0|2) exit $rc;; esac
fi

for W in python3 python "py -3"; do
  # shellcheck disable=SC2086  # "py -3" must word-split
  if OUT=$($W -c 'import sys; ok = sys.version_info >= (3, 9); ok and sys.stdout.write(sys.executable); sys.exit(0 if ok else 1)' 2>/dev/null); then
    if [ -n "$SID" ]; then   # record it for the rest of the session, if session.sh didn't
      cur=; [ -f "$REC" ] && { IFS= read -r cur < "$REC" || :; }
      if ! usable "$cur" "$REC" && [ -f "$OUT" ]; then
        mkdir -p "$GD/light-harness/py" 2>/dev/null && printf '%s' "$OUT" > "$REC" 2>/dev/null
      fi
    fi
    # shellcheck disable=SC2086
    printf '%s\n' "$IN" | LIGHT_HARNESS_GIT_COMMON=$GD $W "$DIR/hook_guard.py"
    exit $?
  fi
done

echo "[light-harness] Blocked: Python 3.9+ not found (tried python3, python, py -3), so the harness guard can't run. Install Python 3.9+ and make it available on PATH." >&2
exit 2
