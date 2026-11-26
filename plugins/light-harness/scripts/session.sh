#!/usr/bin/env sh
# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
# SessionStart hook: puts `light-harness` on the Bash tool's PATH for this session (through
# $CLAUDE_ENV_FILE, so nothing is written to the project), along with the session id
# (LIGHT_HARNESS_SESSION_ID) so harness telemetry events can be joined with the session, and, in a
# repo where Light Harness has started a task, adds the harness working rules to the session's context.
#
# In such a repo it also finds Python 3.9+ once for the session (issue #24) and records it:
#   <git common dir>/light-harness/py/<session id>  the interpreter's path (sys.executable), no newline. The
#       guard and telemetry hooks read it, since hooks don't see $CLAUDE_ENV_FILE. Agents can't change it: the
#       guard blocks edits inside .git and Bash commands naming .git/light-harness.
#   LIGHT_HARNESS_PY in $CLAUDE_ENV_FILE  for bin/light-harness in the Bash tool (`command -v` of the winner).
# Records older than 7 days are deleted.
DIR=$(cd "$(dirname "$0")" && pwd)
BIN=$(cd "$DIR/../bin" && pwd)
INPUT=""
[ -t 0 ] || INPUT=$(cat)
SID=$(printf '%s' "$INPUT" | sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([A-Za-z0-9_.-]*\)".*/\1/p' | head -n 1)
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  # shellcheck disable=SC2016  # $PATH must expand when the env file is sourced, not now
  printf 'export PATH="%s:$PATH"\n' "$BIN" >> "$CLAUDE_ENV_FILE"
  [ -n "$SID" ] && printf 'export LIGHT_HARNESS_SESSION_ID="%s"\n' "$SID" >> "$CLAUDE_ENV_FILE"
fi
PROJECT=${CLAUDE_PROJECT_DIR:-$(pwd)}
GD=$(git -C "$PROJECT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || exit 0
if [ -d "$GD/light-harness" ]; then
  cat "$DIR/../rules.md"
  # A record is named by the session id, so only a plain one is used
  case $SID in ""|*[!A-Za-z0-9_-]*) SID=;; esac
  [ ${#SID} -le 128 ] || SID=
  for W in python3 python "py -3"; do
    # shellcheck disable=SC2086  # "py -3" must word-split
    if OUT=$($W -c 'import sys; ok = sys.version_info >= (3, 9); ok and sys.stdout.write(sys.executable); sys.exit(0 if ok else 1)' 2>/dev/null); then
      case $OUT in /*|[A-Za-z]:*) ;; *) continue;; esac
      if [ -n "$SID" ]; then
        { mkdir -p "$GD/light-harness/py" && printf '%s' "$OUT" > "$GD/light-harness/py/$SID"; } 2>/dev/null
      fi
      R=$(command -v "${W%% *}")
      if [ -n "${CLAUDE_ENV_FILE:-}" ] && [ -n "$R" ]; then
        case $R in *"'"*) ;; *)
          printf "export LIGHT_HARNESS_PY='%s'\n" "$R" >> "$CLAUDE_ENV_FILE";;
        esac
      fi
      break
    fi
  done
  [ -d "$GD/light-harness/py" ] && find "$GD/light-harness/py" -type f -mtime +7 -exec rm -f {} + 2>/dev/null
fi
exit 0
