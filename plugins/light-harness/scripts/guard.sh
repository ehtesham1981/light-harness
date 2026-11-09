#!/usr/bin/env sh
# Copyright 2026 Algomade Consulting. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
# Light Harness hook launcher: finds a Python 3.9+ interpreter (python3, python or py -3, so it
# works on macOS, Linux and Windows/Git Bash) and runs hook_guard.py with the hook's stdin.
#
# The guard only matters in a repo where Light Harness has started a task (.git/light-harness/
# exists). Everywhere else it exits at once, so the plugin never gets in the way of other work.
# If no Python is found in such a repo, it fails CLOSED (blocks the call with a clear message).
# `pwd -W` gives a native path on Windows (Git Bash), so Windows Python can open the script even
# when MSYS path conversion is off (MSYS_NO_PATHCONV=1); elsewhere it fails and `pwd` is used.
DIR=$(cd "$(dirname "$0")" && { pwd -W 2>/dev/null || pwd; })
PROJECT=${CLAUDE_PROJECT_DIR:-$(pwd)}

GD=$(git -C "$PROJECT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || exit 0
[ -d "$GD/light-harness" ] || exit 0

for PY in python3 python "py -3"; do
  # shellcheck disable=SC2086  # "py -3" must word-split
  if $PY -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    exec $PY "$DIR/hook_guard.py"
  fi
done

echo "[light-harness] Blocked: Python 3.9+ not found (tried python3, python, py -3), so the harness guard can't run. Install Python 3.9+ and make it available on PATH." >&2
exit 2
