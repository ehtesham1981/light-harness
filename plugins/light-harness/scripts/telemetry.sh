#!/usr/bin/env sh
# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
# Light Harness telemetry hook launcher (session, subagent, turn and permission events; see
# telemetry_hook.py): finds Python 3.9+ and runs telemetry_hook.py with the hook's stdin. Unlike
# the guard it fails OPEN: no Python, or a repo where Light Harness hasn't started a task, means
# no event and exit 0.
# All but SessionEnd run async (hooks.json), so none of this delays the session.
#   telemetry.sh tool   PostToolUse: runs on every tool call, so it leaves before starting Python
#                       unless this session has an open turn to account for
DIR=$(cd "$(dirname "$0")" && { pwd -W 2>/dev/null || pwd; })
PROJECT=${CLAUDE_PROJECT_DIR:-$(pwd)}
GD=$(git -C "$PROJECT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || exit 0
[ -d "$GD/light-harness" ] || exit 0
IN=$(cat)
if [ "$1" = "tool" ]; then
  SID=$(printf '%s' "$IN" | grep -o '"session_id"[[:space:]]*:[[:space:]]*"[A-Za-z0-9_-]*"' | head -n 1 |
        sed 's/.*"\([A-Za-z0-9_-]*\)"$/\1/')
  [ -n "$SID" ] && [ -f "$GD/light-harness/telemetry/sessions/$SID.json" ] || exit 0
fi
LIGHT_HARNESS_GIT_COMMON=$GD
export LIGHT_HARNESS_GIT_COMMON
for PY in python3 python "py -3"; do
  # shellcheck disable=SC2086  # "py -3" must word-split
  if $PY -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    printf '%s' "$IN" | $PY "$DIR/telemetry_hook.py" >/dev/null 2>&1
    exit 0
  fi
done
exit 0
