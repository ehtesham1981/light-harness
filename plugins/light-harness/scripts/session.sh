#!/usr/bin/env sh
# SessionStart hook: puts `light-harness` on the Bash tool's PATH for this session (through
# $CLAUDE_ENV_FILE, so nothing is written to the project), along with the session id
# (LIGHT_HARNESS_SESSION_ID) so harness telemetry events can be joined with the session, and, in a
# repo where Light Harness has started a task, adds the harness working rules to the session's context.
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
fi
exit 0
