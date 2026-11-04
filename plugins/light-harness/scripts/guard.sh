#!/usr/bin/env sh
# Light Harness hook launcher: finds a Python 3.9+ interpreter (python3, python or py -3, so it
# works on macOS, Linux and Windows/Git Bash) and runs hook_guard.py with the hook's stdin.
#
# If no Python is found: inside a harness task repo it fails CLOSED (blocks the edit with a clear
# message); everywhere else it does nothing, so the plugin never gets in the way of other work.
DIR=$(cd "$(dirname "$0")" && pwd)
PROJECT=${CLAUDE_PROJECT_DIR:-$(pwd)}

for PY in python3 python "py -3"; do
  # shellcheck disable=SC2086  # "py -3" must word-split
  if $PY -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    exec $PY "$DIR/hook_guard.py"
  fi
done

if [ -f "$PROJECT/contract.md" ] && grep -q '<!-- harness:plan -->' "$PROJECT/contract.md" 2>/dev/null; then
  echo "[light-harness] Blocked: Python 3.9+ not found (tried python3, python, py -3), so the harness guard can't run. Install Python 3.9+ and make it available on PATH." >&2
  exit 2
fi
exit 0
