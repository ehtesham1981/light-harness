#!/usr/bin/env sh
# Light Harness telemetry hook launcher (SessionStart/End, SubagentStart/Stop): finds Python 3.9+
# and runs telemetry_hook.py with the hook's stdin. Unlike the guard it fails OPEN: no Python, or
# a repo where Light Harness hasn't started a task, means no event and exit 0.
DIR=$(cd "$(dirname "$0")" && { pwd -W 2>/dev/null || pwd; })
PROJECT=${CLAUDE_PROJECT_DIR:-$(pwd)}
GD=$(git -C "$PROJECT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || exit 0
[ -d "$GD/light-harness" ] || exit 0
for PY in python3 python "py -3"; do
  # shellcheck disable=SC2086  # "py -3" must word-split
  if $PY -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    $PY "$DIR/telemetry_hook.py" >/dev/null 2>&1
    exit 0
  fi
done
exit 0
