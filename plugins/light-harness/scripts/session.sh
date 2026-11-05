#!/usr/bin/env sh
# SessionStart hook: puts `light-harness` on the Bash tool's PATH for this session (through
# $CLAUDE_ENV_FILE, so nothing is written to the project), and, in a repo where Light Harness has
# started a task, adds the harness working rules to the session's context.
DIR=$(cd "$(dirname "$0")" && pwd)
BIN=$(cd "$DIR/../bin" && pwd)
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  # shellcheck disable=SC2016  # $PATH must expand when the env file is sourced, not now
  printf 'export PATH="%s:$PATH"\n' "$BIN" >> "$CLAUDE_ENV_FILE"
fi
PROJECT=${CLAUDE_PROJECT_DIR:-$(pwd)}
GD=$(git -C "$PROJECT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || exit 0
if [ -d "$GD/light-harness" ]; then
  cat "$DIR/../rules.md"
fi
exit 0
