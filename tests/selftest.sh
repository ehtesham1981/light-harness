#!/usr/bin/env bash
# End-to-end self-test of the Light Harness plugin: installs it into a temp repo, then runs a toy
# task through every stage, including gaming attempts that must be caught.
# Needs python3, pytest, git and `script` (util-linux).
# usage: tests/selftest.sh            (PYTHON=/path/to/python to pick an interpreter)
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PLUGIN="$REPO/plugins/light-harness"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT; cd "$W"
export HARNESS_ROOT="$W" CLAUDE_PROJECT_DIR="$W"
PYBIN="${PYTHON:-python3}"

echo "Install (non-harness repo first)"
NONH=$(printf '{"tool_name":"Write","tool_input":{"file_path":"%s/x.py","content":"x"}}' "$W" | sh "$PLUGIN/scripts/guard.sh" >/dev/null 2>&1; echo $?)
[ "$NONH" = 0 ] && R0=1 || R0=0
mkdir -p .claude && echo '{"permissions":{"allow":["Bash(ls:*)"]}}' > .claude/settings.json
"$PYBIN" "$PLUGIN/scripts/install.py" "$PLUGIN" "$W" >/dev/null
"$PYBIN" "$PLUGIN/scripts/install.py" "$PLUGIN" "$W" >/dev/null   # idempotent
git config user.name "Self Test"; git config user.email t@t; git add -A; git commit -qm installed
H="$PYBIN scripts/harness.py"; PASS=0; FAIL=0
[ "$R0" = 1 ] && PASS=$((PASS+1)) || { echo "  ✗ guard blocked an edit outside a harness repo"; FAIL=$((FAIL+1)); }
"$PYBIN" -c "import json;d=json.load(open('.claude/settings.json'));p=d['permissions'];assert 'Bash(ls:*)' in p['allow'] and p['deny']" && PASS=$((PASS+1)) || { echo "  ✗ settings merge"; FAIL=$((FAIL+1)); }
ok()   { if "$@" >/dev/null 2>&1; then PASS=$((PASS+1)); else echo "  ✗ expected success: $*"; FAIL=$((FAIL+1)); fi; }
nok()  { local out; if out=$("$@" 2>&1); then echo "  ✗ expected failure: $*"; FAIL=$((FAIL+1)); else PASS=$((PASS+1))
         [ -n "${VERBOSE:-}" ] && echo "    (refused: $(echo "$out" | grep -m1 '✗' || echo "$out" | tail -1))"; fi; }   # VERBOSE=1 shows why
hook() { echo "$1" | sh "$PLUGIN/scripts/guard.sh" >/dev/null 2>&1; echo $?; }
edit() { "$PYBIN" - "$@" <<'EOF'
import sys; from pathlib import Path
p=Path(sys.argv[1]); t=p.read_text(encoding="utf-8")
for a,b in zip(sys.argv[2::2], sys.argv[3::2]):
    assert a in t, a; t=t.replace(a,b,1)
p.write_text(t, encoding="utf-8")
EOF
}

echo "Initialize"
ok  $H init --name "Selftest"
nok $H set-status Initialize
edit contract.md "<one or two sentences: what outcome, for whom>" "Dedupe invoices" \
  "| EX-1 | <input> | <expected> | <happy path / edge / error> |" "| EX-1 | [A1, a1, B2] | [A1, B2] | case |" \
  "- AC-1: <Given … When … Then …>" "- AC-1: Given case-variant duplicates, then first kept in order
- AC-2: Given empty list, then empty list"
ok  $H set-status Initialize

echo "Plan"
nok $H set-status Plan
[ "$(hook '{"tool_name":"Write","tool_input":{"file_path":"src/x.py","content":"x"}}')" = 2 ] && PASS=$((PASS+1)) || { echo "  ✗ hook allowed code in Initialize"; FAIL=$((FAIL+1)); }
edit contract.md "| R-1 | <risk> | <L/M/H> | <L/M/H> | <mitigation> |" "| R-1 | None ids | M | M | raise ValueError |" \
  "| <area> | <change> | <who/what is affected> | <how to undo> |" "| src/invoices.py | new | none | delete |" \
  "- <src/path/or/glob>" "- src/**
- tests/**" \
  "1. <step> (AC-1)" "1. dedupe by casefold (AC-1)
2. empty input (AC-2)"
ok  $H set-status Plan
nok $H approve --name Agent </dev/null
nok $H set-status Build
ok  script -qec "$PYBIN scripts/harness.py approve --name 'Human Approver'" /dev/null <<< "APPROVE"

echo "Build"
ok  $H set-status Build
mkdir -p src tests; touch src/__init__.py
cat > tests/test_inv.py <<'EOF'
import pytest
from src.invoices import dedupe
@pytest.mark.parametrize("i,e", [(["A1","a1","B2"],["A1","B2"]), (["x9","X9","y"],["x9","y"])])
def test_ac1(i, e):
    """AC-1"""
    assert dedupe(i) == e
def test_ac2():
    """AC-2"""
    assert dedupe([]) == []
EOF
nok $H redgate                                   # no stubs: collection error
printf 'def dedupe(v):\n    raise NotImplementedError\n' > src/invoices.py
ok  $H redgate
[ "$(hook '{"tool_name":"Edit","tool_input":{"file_path":"tests/test_inv.py","old_string":"a","new_string":"b"}}')" = 2 ] && PASS=$((PASS+1)) || { echo "  ✗ hook allowed locked-test edit"; FAIL=$((FAIL+1)); }

echo "Gaming attempts"
printf 'import os\ndef dedupe(v):\n    if os.environ.get("PYTEST_CURRENT_TEST"): pass\n    s=set(); return [x for x in v if not (x.casefold() in s or s.add(x.casefold()))]\n' > src/invoices.py
nok $H verify                                    # runtime sniffing
printf 'def dedupe(v):\n    s=set(); return [x for x in v if not (x.casefold() in s or s.add(x.casefold()))]\n' > src/invoices.py
sed -i 's/== \[\]/is not None/' tests/test_inv.py
nok $H verify                                    # weakened locked test
git checkout -q tests/test_inv.py
echo x > rogue.txt; nok $H verify; rm rogue.txt  # out-of-scope change
echo "# tamper" >> scripts/harness.py; nok $H verify; git checkout -q scripts/harness.py
ok  $H verify

echo "Review"
ok  $H set-status Review
edit contract.md "|----|----------|--------|------|-------------|------------------|
" "|----|----------|--------|------|-------------|------------------|
| RV-1 | Medium | Open | src | R-1 mitigation missing | R-1 |
" "- [ ] AC-1" "- [x] AC-1
- [x] AC-2"
nok $H verdict pass                              # open Medium blocks
ok  $H verdict fail
ok  $H set-status Build
edit contract.md "| RV-1 | Medium | Open" "| RV-1 | Medium | Fixed"
ok  $H set-status Review
nok $H verdict pass                              # Fixed is not Resolved
edit contract.md "| RV-1 | Medium | Fixed" "| RV-1 | Medium | Won't fix"
nok $H verdict pass                              # Won't fix illegal for Medium
edit contract.md "| RV-1 | Medium | Won't fix" "| RV-1 | Medium | Resolved"
ok  $H verdict pass
grep -q "^Status: Done" contract.md && PASS=$((PASS+1)) || { echo "  ✗ not Done"; FAIL=$((FAIL+1)); }
check() { if eval "$1"; then PASS=$((PASS+1)); else echo "  ✗ $2"; FAIL=$((FAIL+1)); fi; }
check '[ -f .harness/baseline.json ] && grep -q test_ac2 .harness/baseline.json' "verdict pass didn't record the test baseline"

approve() { script -qec "$PYBIN scripts/harness.py approve --name 'Human Approver' $*" /dev/null <<< "APPROVE" >/dev/null 2>&1; }

echo "Next task in the same checkout (earlier tests pass, AC ids reused)"
ok  $H set-status Initialize
ok  $H set-status Plan
nok $H set-status Build                          # task 1's approval doesn't carry over
edit contract.md "- AC-1: Given case-variant duplicates, then first kept in order
- AC-2: Given empty list, then empty list" "- AC-1: Given blanks, then they are dropped
- AC-2: Given no blanks, then the list is unchanged" "2. empty input (AC-2)" "2. no blanks (AC-2)"
ok  $H set-status Plan
check approve "approve (task 2)"
ok  $H set-status Build
cat > tests/test_blank.py <<'EOF'
from src.invoices import drop_blanks
def test_blank():
    """AC-1"""
    assert drop_blanks(["A", "", "B"]) == ["A", "B"]
EOF
printf 'def dedupe(v):\n    s=set(); return [x for x in v if not (x.casefold() in s or s.add(x.casefold()))]\ndef drop_blanks(v):\n    raise NotImplementedError\n' > src/invoices.py
nok $H redgate                                   # AC-2 only referenced by task 1's tests
printf 'def test_keep():\n    """AC-2"""\n    assert True\n' >> tests/test_blank.py
nok $H redgate                                   # new vacuous test passes on stubs
sed -i 's/assert True/assert drop_blanks(["A"]) == ["A"]/' tests/test_blank.py
ok  $H redgate                                   # task 1's passing tests are fine
printf 'def dedupe(v):\n    s=set(); return [x for x in v if not (x.casefold() in s or s.add(x.casefold()))]\ndef drop_blanks(v):\n    return [x for x in v if x]\n' > src/invoices.py
ok  $H verify
ok  $H set-status Review
ok  $H verdict pass

echo "Task folders: one per task in .tasks/, tagged when done"
M="$W/proj"; mkdir -p "$M"; cd "$M"
export HARNESS_ROOT="$M" CLAUDE_PROJECT_DIR="$M"
"$PYBIN" "$PLUGIN/scripts/install.py" "$PLUGIN" "$M" >/dev/null
git config user.name "Self Test"; git config user.email t@t
ok  $H start-task "Alpha"
T="$M/.tasks"
check '[ -f "$T/alpha/contract.md" ] && [ "$(git -C "$T/alpha" rev-parse --abbrev-ref HEAD)" = task/alpha ]' "task folder .tasks/alpha not created"
check 'grep -q "^Status: Draft" contract.md && grep -q "<task name>" contract.md' "main checkout contract isn't a blank template"
check 'grep -qx ".tasks/" .gitignore && [ -z "$(git status --porcelain)" ]' ".tasks/ not ignored, or main checkout left dirty"
nok $H start-task "alpha"                        # duplicate name
nok $H status --task nosuch                      # unknown task
check '! (cd "$T/alpha" && HARNESS_ROOT="$T/alpha" CLAUDE_PROJECT_DIR="$T/alpha" $H start-task beta >/dev/null 2>&1)' "start-task ran inside a task folder"
[ "$(hook "{\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"$M/contract.md\",\"old_string\":\"a\",\"new_string\":\"b\"}}")" = 2 ] && PASS=$((PASS+1)) || { echo "  ✗ hook allowed editing the main checkout's template contract"; FAIL=$((FAIL+1)); }
[ "$(hook "{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$T/alpha/src/x.py\",\"content\":\"x\"}}")" = 2 ] && PASS=$((PASS+1)) || { echo "  ✗ hook allowed code in a Draft task folder"; FAIL=$((FAIL+1)); }

# flow <task dir> <name> [main]: one AC, square(x). "main" drives it from the main checkout with
# --task, as a single Claude Code session does; otherwise from inside the task folder.
flow() {
  local X=""; if [ "${3:-}" = main ]; then X="--task $2"; cd "$M"; export HARNESS_ROOT="$M" CLAUDE_PROJECT_DIR="$M"
  else cd "$1"; export HARNESS_ROOT="$1" CLAUDE_PROJECT_DIR="$1"; fi
  edit "$1/contract.md" "<one or two sentences: what outcome, for whom>" "$2" \
    "| EX-1 | <input> | <expected> | <happy path / edge / error> |" "| EX-1 | 3 | 9 | happy |" \
    "- AC-1: <Given … When … Then …>" "- AC-1: Given x, then x squared"
  ok  $H set-status Initialize $X
  edit "$1/contract.md" "| R-1 | <risk> | <L/M/H> | <L/M/H> | <mitigation> |" "| R-1 | none | L | L | n/a |" \
    "| <area> | <change> | <who/what is affected> | <how to undo> |" "| src | new | none | delete |" \
    "- <src/path/or/glob>" "- src/**
- tests/**" "1. <step> (AC-1)" "1. square (AC-1)"
  ok  $H set-status Plan $X
  check "approve $X" "approve ($2)"
  ok  $H set-status Build $X
  [ "$(hook "{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$1/src/$2.py\",\"content\":\"x\"}}")" = 0 ] && PASS=$((PASS+1)) || { echo "  ✗ hook blocked code in a Build task folder ($2)"; FAIL=$((FAIL+1)); }
  mkdir -p "$1/src" "$1/tests"; touch "$1/src/__init__.py"
  printf 'from src.%s import f\ndef test_%s():\n    """AC-1"""\n    assert f(3) == 9\n' "$2" "$2" > "$1/tests/test_$2.py"
  printf 'def f(x):\n    raise NotImplementedError\n' > "$1/src/$2.py"
  ok  $H redgate $X
  printf 'def f(x):\n    return x * x\n' > "$1/src/$2.py"
  ok  $H verify $X
  ok  $H set-status Review $X
  edit "$1/contract.md" "- [ ] AC-1" "- [x] AC-1"
  ok  $H verdict pass $X
}
flow "$T/alpha" alpha
ok  $H finish-task                               # run inside the task folder: tags only
check 'git rev-parse -q --verify refs/tags/done/alpha >/dev/null && [ -d "$T/alpha" ]' "finish-task inside the task folder should tag and keep the folder"
cd "$M"; export HARNESS_ROOT="$M" CLAUDE_PROJECT_DIR="$M"
ok  $H finish-task alpha
check '[ ! -d "$T/alpha" ] && ! git rev-parse -q --verify refs/heads/task/alpha >/dev/null && git rev-parse -q --verify refs/tags/done/alpha >/dev/null' "finish-task should remove folder + branch and keep the tag"
check '! git log --oneline HEAD | grep -q "harness: verdict"' "task work leaked into main"

echo "One session in the main checkout: build on a finished task, plus a parallel task"
ok  $H start-task beta --from done/alpha
ok  $H start-task gamma                          # parallel, from main
check '[ -f "$T/beta/tests/test_alpha.py" ] && [ ! -f "$T/gamma/tests/test_alpha.py" ]' "--from base not applied"
check 'grep -q "^Status: Draft" "$T/beta/contract.md"' "beta didn't get a fresh contract"
nok $H finish-task gamma                         # not Done
nok "$PYBIN" "$PLUGIN/scripts/install.py" "$PLUGIN" "$T"   # the folder holding the task folders isn't a project
check '[ ! -e "$T/.git" ] && [ ! -e "$T/contract.md" ]' "install created a repo in the tasks folder"
flow "$T/beta" beta main                         # alpha's test passes at beta's red gate; AC-1 reused
flow "$T/gamma" gamma main
check 'grep -q "^Status: Draft" "$M/contract.md"' "main checkout contract changed by --task commands"
ok  $H finish-task beta
ok  $H finish-task --task gamma
check '[ "$(git tag -l "done/*" | wc -l)" -eq 3 ] && [ "$(git worktree list | wc -l)" -eq 1 ]' "expected 3 done tags and no task folders"

echo "Existing codebase: baseline recorded on entering Build"
B="$W/brown"; mkdir -p "$B/src" "$B/tests"; cd "$B"; git init -q; git config user.name "Self Test"; git config user.email t@t
touch src/__init__.py; printf 'def g():\n    return 1\n' > src/old.py
printf 'from src.old import g\ndef test_old():\n    assert g() == 1\n' > tests/test_old.py
git add -A; git commit -qm "existing code"
export HARNESS_ROOT="$B" CLAUDE_PROJECT_DIR="$B"
"$PYBIN" "$PLUGIN/scripts/install.py" "$PLUGIN" "$B" >/dev/null
ok  $H start-task delta
M="$B" flow "$B/.tasks/delta" delta main
check 'git -C "$B/.tasks/delta" log --format=%s | grep -q "harness: baseline (1 existing"' "baseline commit missing"

echo; echo "Self-test: $PASS passed, $FAIL failed"; [ "$FAIL" = 0 ]
