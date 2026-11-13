#!/usr/bin/env bash
# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
# End-to-end self-test of the Light Harness plugin: runs toy tasks through every stage in temp
# repos, including gaming attempts that must be caught, and checks that the harness leaves
# nothing behind in the work repo (no files, settings, rules or commits on any branch).
# Needs python3, pytest, git and `script` (util-linux).
# usage: tests/selftest.sh            (PYTHON=/path/to/python to pick an interpreter)
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PLUGIN="$REPO/plugins/light-harness"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
unset HARNESS_ROOT CLAUDECODE LIGHT_HARNESS_SESSION_ID LIGHT_HARNESS_OTLP_ENDPOINT LIGHT_HARNESS_OTLP_HEADERS LIGHT_HARNESS_TELEMETRY
export GIT_CONFIG_GLOBAL="$W/gitconfig"   # telemetry opt-in writes the "global" git config: keep it here
PYBIN="${PYTHON:-python3}"
H="$PYBIN $PLUGIN/scripts/harness.py"
PASS=0; FAIL=0
ok()    { if "$@" >/dev/null 2>&1; then PASS=$((PASS+1)); else echo "  ✗ expected success: $*"; FAIL=$((FAIL+1)); fi; }
nok()   { local out; if out=$("$@" 2>&1); then echo "  ✗ expected failure: $*"; FAIL=$((FAIL+1)); else PASS=$((PASS+1))
          [ -n "${VERBOSE:-}" ] && echo "    (refused: $(echo "$out" | grep -m1 '✗' || echo "$out" | tail -1))"; fi; }   # VERBOSE=1 shows why
check() { if eval "$1"; then PASS=$((PASS+1)); else echo "  ✗ $2"; FAIL=$((FAIL+1)); fi; }
# hook <project dir> <json>: the guard's exit code; hookout: its stdout (JSON decisions)
hook()    { echo "$2" | CLAUDE_PROJECT_DIR="$1" sh "$PLUGIN/scripts/guard.sh" >/dev/null 2>&1; echo $?; }
hookout() { echo "$2" | CLAUDE_PROJECT_DIR="$1" sh "$PLUGIN/scripts/guard.sh" 2>/dev/null; }
bash_json() { "$PYBIN" -c 'import json,sys; print(json.dumps({"tool_name":"Bash","cwd":sys.argv[1],"tool_input":{"command":sys.argv[2]}}))' "$1" "$2"; }
file_json() { "$PYBIN" -c 'import json,sys; print(json.dumps({"tool_name":sys.argv[1],"cwd":sys.argv[2],"tool_input":{"file_path":sys.argv[3],"content":"x","old_string":sys.argv[4] if len(sys.argv)>4 else "a","new_string":"b"}}))' "$@"; }
human() { script -qec "$1" /dev/null <<< "$2" >/dev/null 2>&1; }   # a command run in a real terminal
edit() { "$PYBIN" - "$@" <<'EOF'
import sys; from pathlib import Path
p=Path(sys.argv[1]); t=p.read_text(encoding="utf-8")
for a,b in zip(sys.argv[2::2], sys.argv[3::2]):
    assert a in t, a; t=t.replace(a,b,1)
p.write_text(t, encoding="utf-8")
EOF
}
# evq <python expr over E, the telemetry events>: succeeds if it's true
evq() { "$PYBIN" -c 'import json,sys; E=[json.loads(l) for l in open(sys.argv[1],encoding="utf-8")]; sys.exit(0 if eval(sys.argv[2]) else 1)' "$EV" "$1"; }
# hook_json <event> <cwd> <session>: a session/subagent hook payload; telhook runs the telemetry hook
hook_json() { "$PYBIN" -c 'import json,sys; print(json.dumps({"hook_event_name":sys.argv[1],"cwd":sys.argv[2],"session_id":sys.argv[3],"agent_id":"a1","agent_type":"light-harness:reviewer","source":"startup"}))' "$@"; }
telhook() { echo "$2" | CLAUDE_PROJECT_DIR="$1" sh "$PLUGIN/scripts/telemetry.sh" ${3:-}; }
# turn_json <event> <cwd> <session> [tool] [duration_ms]: a turn/permission/tool hook payload
turn_json() { "$PYBIN" -c 'import json,sys; a=sys.argv; print(json.dumps({"hook_event_name":a[1],"cwd":a[2],"session_id":a[3],"prompt":"hi","tool_name":a[4] if len(a)>4 else None,"duration_ms":int(a[5]) if len(a)>5 else None,"tool_input":{"command":"x \"session_id\": \"spoof\""}}))' "$@"; }
newrepo() { mkdir -p "$1"; git -C "$1" init -q; git -C "$1" config user.name "Self Test"; git -C "$1" config user.email t@t; }
# the work repo's own state: tracked and untracked files (ignored ones too), branches' content
fingerprint() { (cd "$1" && git status --porcelain --ignored --untracked-files=all | grep -v '^!! .tasks/'; git ls-files; cat .gitignore CLAUDE.md .claude/settings.json 2>/dev/null) | md5sum; }

echo "Outside a harness repo the plugin is inert"
P="$W/plain"; newrepo "$P"; echo x > "$P/a"; git -C "$P" add -A; git -C "$P" commit -qm init
check '[ "$(hook "$P" "$(file_json Write "$P" "$P/x.py")")" = 0 ]' "guard blocked an edit outside a harness repo"
check '[ "$(hook "$P" "$(bash_json "$P" "git reset --hard")")" = 0 ] && [ -z "$(hookout "$P" "$(bash_json "$P" "git status")")" ]' "guard acted on Bash outside a harness repo"
check '[ -z "$(CLAUDE_PROJECT_DIR="$P" sh "$PLUGIN/scripts/session.sh" </dev/null)" ]' "session hook printed rules outside a harness repo"
check 'E="$W/envfile"; CLAUDE_ENV_FILE="$E" CLAUDE_PROJECT_DIR="$P" sh "$PLUGIN/scripts/session.sh" </dev/null >/dev/null; grep -q "light-harness/bin:\$PATH" "$E"' "session hook didn't put bin/ on PATH"
check 'E="$W/envfile2"; hook_json SessionStart "$P" "abc-123" | CLAUDE_ENV_FILE="$E" CLAUDE_PROJECT_DIR="$P" sh "$PLUGIN/scripts/session.sh" >/dev/null; grep -qx "export LIGHT_HARNESS_SESSION_ID=\"abc-123\"" "$E"' "session hook didn't pass the session id"
check 'E="$W/envfile3"; hook_json SessionStart "$P" "x\";rm -rf /" | CLAUDE_ENV_FILE="$E" CLAUDE_PROJECT_DIR="$P" sh "$PLUGIN/scripts/session.sh" >/dev/null; ! grep -q SESSION_ID "$E"' "session hook passed an unsafe session id"
check '[ -z "$(telhook "$P" "$(hook_json SessionStart "$P" s0)")" ] && [ ! -e "$P/.git/light-harness" ]' "telemetry hook acted outside a harness repo"

echo "Start a task: nothing is added to the repo"
M="$W/proj"; newrepo "$M"; cd "$M"
mkdir -p .claude src; echo '{"permissions":{"allow":["Bash(ls:*)"]}}' > .claude/settings.json
printf 'node_modules/\n' > .gitignore; touch src/__init__.py
mkdir samples; for i in $(seq 2000); do echo "$i" > "samples/fixture-data-record-$i.json"; done  # > Windows's command line
echo q > '"odd.test.txt'; echo n > "$(printf 'new\nline.test.txt')"   # names --stdin-paths can't take
git add -A; git commit -qm "existing project"
BEFORE="$(fingerprint "$M")"; HEAD0="$(git rev-parse HEAD)"
nok $H start-task ""                             # no usable name
ok  $H start-task "Alpha"
T="$M/.tasks"
check '[ "$(fingerprint "$M")" = "$BEFORE" ] && [ "$(git rev-parse HEAD)" = "$HEAD0" ]' "start-task changed the main checkout"
check '[ -f "$T/alpha/.light-harness/contract.md" ] && [ "$(git -C "$T/alpha" rev-parse --abbrev-ref HEAD)" = task/alpha ]' "task folder .tasks/alpha not created"
check '[ "$(git rev-parse task/alpha)" = "$HEAD0" ] && [ -z "$(git -C "$T/alpha" status --porcelain)" ]' "start-task committed to the task branch or left it dirty"
check 'git rev-parse -q --verify refs/light-harness/tasks/alpha >/dev/null && grep -q "light-harness" .git/info/exclude' "audit trail or local excludes missing"
check 'CLAUDE_PROJECT_DIR="$M" sh "$PLUGIN/scripts/session.sh" </dev/null | grep -q "Working Rules"' "session hook didn't add the rules in a harness repo"
# A trail written by 1.4.0 on Windows: the task-start record's file names end in "\r". Later
# records must still work and write clean names; the old entry stays in history.
R=refs/light-harness/tasks/alpha; S0="$(git rev-parse $R)"
CRT="$(git ls-tree "$S0" | sed 's/$/\r/' | git mktree)"
git update-ref $R "$(git log -1 --format=%B "$S0" | git commit-tree "$CRT" $(git log -1 --format=%P "$S0" | sed 's/[^ ]*/-p &/g'))" "$S0"
check '[ "$(git ls-tree --name-only -z $R | tr -cd "\r" | wc -c)" -ge 2 ]' "could not simulate a 1.4.0 Windows audit trail"
nok $H start-task "alpha"                        # duplicate name
nok $H status --task nosuch                      # unknown task
check '! (cd "$T/alpha" && $H start-task beta >/dev/null 2>&1)' "start-task ran inside a task folder"

echo "Guard"
C="$T/alpha/.light-harness/contract.md"
check '[ "$(hook "$M" "$(file_json Write "$M" "$M/src/x.py")")" = 2 ]' "guard allowed editing the main checkout while a task is in progress"
check '[ "$(hook "$M" "$(file_json Write "$M" "$T/alpha/src/x.py")")" = 2 ]' "guard allowed code in a Draft task"
check '[ "$(hook "$M" "$(file_json Edit "$M" "$C" "Status: Draft")")" = 2 ]' "guard allowed editing the contract's Status line"
check '[ "$(hook "$M" "$(file_json Write "$M" "$T/alpha/.light-harness/junit.xml")")" = 2 ]' "guard allowed editing harness files"
check '[ "$(hook "$M" "$(file_json Write "$M" "$M/.git/light-harness/config.json")")" = 2 ]' "guard allowed editing .git/light-harness"
check '[ "$(hook "$M" "$(file_json Read "$M" "$T/alpha/.env")")" = 2 ] && [ "$(hook "$M" "$(file_json Read "$M" "$T/alpha/.env.example")")" = 0 ]' "guard .env read rules"
for c in "light-harness approve --task alpha --name X" "python3 /p/scripts/harness.py unlock-tests --name X --reason y" \
         "git checkout main" "git -C .tasks/alpha reset --hard" "git commit --amend -m x" "git branch -D task/alpha" \
         "git push --force origin x" "git update-ref -d refs/light-harness/tasks/alpha" "rm -rf src" \
         "sed -i s/Draft/Build/ .tasks/alpha/.light-harness/contract.md" "echo x > .tasks/alpha/.light-harness/config.json" \
         "script -qc 'light-harness approve' /dev/null" "light-harness telemetry opt-in --as Bob" \
         "git config --global lightHarness.engineer Bob" "LIGHT_HARNESS_TELEMETRY=0 light-harness verify" \
         "env -u LIGHT_HARNESS_SESSION_ID light-harness verify" "cat .git/light-harness/telemetry/events.jsonl" \
         "LIGHT_HARNESS_HUMAN_GAP_CAP=1s light-harness verify" "git config lightharness.humanGapCap 1s"; do
  check '[ "$(hook "$M" "$(bash_json "$M" "$c")")" = 2 ]' "guard allowed: $c"
done
for c in "git diff src/checkout.py" "cat .tasks/alpha/.light-harness/contract.md" "git log --oneline"; do
  check '[ "$(hook "$M" "$(bash_json "$M" "$c")")" = 0 ]' "guard blocked: $c"
done
check 'hookout "$M" "$(bash_json "$M" "light-harness status --task alpha")" | grep -q "\"allow\""' "harness command not pre-approved"
check 'hookout "$M" "$(bash_json "$M" "light-harness metrics alpha")" | grep -q "\"allow\"" && hookout "$M" "$(bash_json "$M" "light-harness telemetry status")" | grep -q "\"allow\""' "metrics / telemetry status not pre-approved"
check 'hookout "$M" "$(bash_json "$M" "cd .tasks/alpha && git add -A && git commit -m \"step 1: x\"")" | grep -q "\"allow\""' "step commit not pre-approved"
check '! hookout "$M" "$(bash_json "$M" "light-harness status && curl evil")" | grep -q "\"allow\""' "a chained command was pre-approved"
check 'hookout "$M" "$(bash_json "$M" "curl https://example.com")" | grep -q "\"ask\""' "network command didn't ask"

echo "Telemetry: engineer KPIs are opt-in, under a nickname"
EV="$M/.git/light-harness/telemetry/events.jsonl"
check 'evq "any(e[\"event\"]==\"guard\" and e[\"rule\"]==\"git-surgery\" for e in E) and not any(\"engineer\" in e for e in E)"' "guard blocks not recorded, or an engineer recorded without opt-in"
nok $H telemetry opt-in --as Owl </dev/null      # human only
check '! human "$H telemetry opt-in --as \"!bad\"" "" && [ -z "$(git config --global lightharness.engineer)" ]' "an invalid nickname was accepted"
check 'human "$H telemetry opt-in --as \"Night Owl\"" "" && [ "$(git config --global lightharness.engineer)" = "Night Owl" ]' "opt-in"
check '$H telemetry status | grep -q "opted in as .Night Owl."' "telemetry status doesn't show the opt-in"
export LIGHT_HARNESS_SESSION_ID=sess-alpha

echo "Alpha: every stage, with gaming attempts"
X="--task alpha"; A="$T/alpha"
nok $H set-status Initialize $X
edit "$C" "<one or two sentences: what outcome, for whom>" "Dedupe invoices" \
  "| EX-1 | <input> | <expected> | <happy path / edge / error> |" "| EX-1 | [A1, a1, B2] | [A1, B2] | case |" \
  "- AC-1: <Given … When … Then …>" "- AC-1: Given case-variant duplicates, then first kept in order
- AC-2: Given empty list, then empty list"
ok  $H set-status Initialize $X
nok $H set-status Plan $X
edit "$C" "| R-1 | <risk> | <L/M/H> | <L/M/H> | <mitigation> |" "| R-1 | None ids | M | M | raise ValueError |" \
  "| <area> | <change> | <who/what is affected> | <how to undo> |" "| src/invoices.py | new | none | delete |" \
  "- <src/path/or/glob>" "- src/**
- tests/**" \
  "1. <step> (AC-1)" "1. dedupe by casefold (AC-1)
2. empty input (AC-2)"
ok  $H set-status Plan $X
nok $H approve --name Agent $X </dev/null
nok $H set-status Build $X
check 'human "$H approve --name \"Human Approver\" $X" APPROVE' "approve"
check '[ -z "$(git ls-tree --name-only -z $R | tr -cd "\r")" ] && git ls-tree --name-only $R | grep -qx contract.md' "approve left \r file names in the audit trail"
ok  $H set-status Build $X
check '[ "$(hook "$M" "$(file_json Write "$M" "$A/src/x.py")")" = 0 ]' "guard blocked code in Build"
check '[ "$(hook "$M" "$(file_json Write "$M" "$A/.light-harness/config.json")")" = 2 ]' "guard allowed config edit after approval"
mkdir -p "$A/tests"
cat > "$A/tests/test_inv.py" <<'EOF'
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
nok $H redgate $X                                # no stubs: collection error
printf 'def dedupe(v):\n    raise NotImplementedError\n' > "$A/src/invoices.py"
ok  $H redgate $X
check '[ "$(hook "$M" "$(file_json Edit "$M" "$A/tests/test_inv.py")")" = 2 ]' "guard allowed a locked-test edit"
check '[ "$(git rev-parse task/alpha)" = "$HEAD0" ]' "the red gate committed to the task branch"
printf 'import os\ndef dedupe(v):\n    if os.environ.get("PYTEST_CURRENT_TEST"): pass\n    s=set(); return [x for x in v if not (x.casefold() in s or s.add(x.casefold()))]\n' > "$A/src/invoices.py"
nok $H verify $X                                 # runtime sniffing
printf 'def dedupe(v):\n    s=set(); return [x for x in v if not (x.casefold() in s or s.add(x.casefold()))]\n' > "$A/src/invoices.py"
cp "$A/tests/test_inv.py" "$W/keep.py"; sed -i 's/== \[\]/is not None/' "$A/tests/test_inv.py"
nok $H verify $X                                 # weakened locked test
cp "$W/keep.py" "$A/tests/test_inv.py"
for f in samples/fixture-data-record-1999.json '"odd.test.txt' "$(printf 'new\nline.test.txt')"; do
  cp "$A/$f" "$W/keep.f"; echo changed > "$A/$f"; p="file modified: ${f%%$'\n'*}"  # grep a line at a time
  check '! $H verify $X > "$W/v.out" 2>&1 && grep -qF "$p" "$W/v.out"' "verify missed a change to a locked file: $f"
  cp "$W/keep.f" "$A/$f"
done
echo x > "$A/rogue.txt"; nok $H verify $X; rm "$A/rogue.txt"            # out-of-scope change
cp "$A/.light-harness/config.json" "$W/cfg.json"; sed -i 's/"-q"/"-q", "-x"/' "$A/.light-harness/config.json"
nok $H verify $X                                 # config changed after approval
cp "$W/cfg.json" "$A/.light-harness/config.json"
ok  $H verify $X
(cd "$A" && git add -A && git commit -qm "step 1-2: dedupe")
ok  $H set-status Review $X
edit "$C" "|----|----------|--------|------|-------|-------------|------------------|
" "|----|----------|--------|------|-------|-------------|------------------|
| RV-1 | Medium | Open | src | missing mitigation: R-1 | R-1 mitigation missing | R-1 |
" "- [ ] AC-1" "- [x] AC-1
- [x] AC-2"
nok $H verdict pass $X                           # open Medium blocks
ok  $H verdict fail $X
ok  $H set-status Build $X
edit "$C" "| RV-1 | Medium | Open" "| RV-1 | Medium | Fixed"
ok  $H set-status Review $X
nok $H verdict pass $X                           # Fixed is not Resolved
edit "$C" "| RV-1 | Medium | Fixed" "| RV-1 | Medium | Won't fix"
nok $H verdict pass $X                           # Won't fix illegal for Medium
edit "$C" "| RV-1 | Medium | Won't fix" "| RV-1 | Medium | Resolved"
ok  $H verdict pass $X
check 'grep -q "^Status: Done" "$C" && $H show alpha | grep -q "Verdict: PASS"' "not Done, or show doesn't print the contract"
check '$H log alpha | grep -q "plan-approved by Human Approver" && $H log alpha | grep -q "red-gate"' "audit trail incomplete"

echo "Telemetry: alpha's events and KPIs"
check '[ -z "$(telhook "$M" "$(hook_json SubagentStart "$M" sess-alpha)")" ] && [ -z "$(telhook "$M" "$(hook_json SubagentStop "$M" sess-alpha)")" ]' "telemetry hook printed something"
check 'evq "any(e[\"event\"]==\"command\" and e[\"command\"]==\"redgate\" and not e[\"ok\"] and e.get(\"reason\") for e in E)"' "red-gate refusal (with reason) not recorded"
check 'evq "[e[\"to\"] for e in E if e[\"event\"]==\"stage\" and e.get(\"task\")==\"alpha\"]==[\"Initialize\",\"Plan\",\"Build\",\"Review\",\"Rework\",\"Build\",\"Review\",\"Done\"]"' "stage events wrong"
check 'evq "any(e[\"event\"]==\"guard\" and e[\"rule\"]==\"locked-test\" and e.get(\"task\")==\"alpha\" for e in E)"' "locked-test block not recorded for alpha"
check 'evq "any(e.get(\"command\")==\"approve\" and e[\"actor\"]==\"human\" and e.get(\"engineer\")==\"Night Owl\" for e in E)"' "approval not attributed to the opted-in nickname"
check 'evq "any(e.get(\"fail_kinds\",{}).get(\"Anti-gaming\") for e in E) and any(e.get(\"issues\")=={\"medium\":1} for e in E)"' "verify failure kinds or review issues not recorded"
check 'evq "all(e.get(\"session\")==\"sess-alpha\" for e in E if e[\"event\"] in (\"command\",\"stage\") and e.get(\"task\")==\"alpha\" and e.get(\"command\") not in (\"approve\",\"start-task\"))"' "session id missing on alpha's events"
check '! grep -qE "Human Approver|Self Test|t@t" "$EV"' "telemetry holds a real name or email"
check '$H metrics alpha --json | "$PYBIN" -c "import json,sys; k=json.load(sys.stdin); assert (k[\"review_rounds\"],k[\"first_pass\"],k[\"approvals\"],len(k[\"approval_wait_s\"]))==(2,False,1,1),k; assert k[\"red_gate_fails\"]>=1 and k[\"verify_fails\"]>=4 and k[\"guard_blocks\"][\"locked-test\"]>=1,k; assert k[\"subagents\"][\"light-harness:reviewer\"][\"runs\"]==1,k; assert k[\"lead_time_s\"] is not None and k[\"time_in_stage_s\"][\"Build\"]>0,k"' "metrics for alpha are wrong"
check '$H metrics | grep -q "Night Owl" && $H metrics | grep -qE "^alpha +done"' "metrics summary missing alpha or the opted-in engineer"
check 'evq "any(e.get(\"command\")==\"verdict\" and e.get(\"target\")==\"pass\" and e.get(\"lead_time_s\",0)>0 and e.get(\"first_pass\") is False and e.get(\"review_round\")==2 for e in E)"' "final verdict lacks lead_time_s / first_pass / review_round"
check 'evq "any(e.get(\"command\")==\"approve\" and e.get(\"ok\") and e.get(\"wait_s\",-1)>=0 for e in E)"' "approve lacks wait_s"
check 'evq "any(e[\"event\"]==\"agent\" and e.get(\"phase\")==\"stop\" and e.get(\"run_s\",-1)>=0 for e in E)"' "subagent stop lacks run_s"

echo "Telemetry: turns, human waits and the agent/human/idle split"
SD="$M/.git/light-harness/telemetry/sessions"
ok  test -z "$(telhook "$M" "$(turn_json PostToolUse "$M" sess-none Bash 5)" tool)"
check '[ ! -e "$SD/sess-none.json" ] && ! grep -q sess-none "$EV"' "PostToolUse acted on a session with no open turn"
check '[ -z "$(telhook "$M" "$(turn_json UserPromptSubmit "$M" sess-t)")" ] && [ -f "$SD/sess-t.json" ]' "UserPromptSubmit printed something or kept no state"
telhook "$M" "$(turn_json PermissionRequest "$M" sess-t Bash)"; sleep 0.4
telhook "$M" "$(turn_json PostToolUse "$M" sess-t Bash 100)" tool
telhook "$M" "$(turn_json PostToolUse "$M" sess-t AskUserQuestion 250)" tool
telhook "$M" "$(turn_json PostToolUse "$M" sess-t Read 5)" tool
sleep 0.3; telhook "$M" "$(turn_json Stop "$M" sess-t)"
check 'evq "[(e[\"kind\"],e[\"tool\"]) for e in E if e[\"event\"]==\"wait\" and e.get(\"session\")==\"sess-t\"]==[(\"permission\",\"Bash\"),(\"question\",\"AskUserQuestion\")]"' "waits not recorded as one permission and one question"
check 'evq "[round(e[\"wait_s\"],2) for e in E if e[\"event\"]==\"wait\" and e.get(\"tool\")==\"AskUserQuestion\"]==[0.25] and all(0.2<e[\"wait_s\"]<5 for e in E if e[\"event\"]==\"wait\" and e.get(\"tool\")==\"Bash\")"' "wait durations wrong"
check 'evq "[(e[\"interrupted\"], abs(e[\"turn_s\"]-e[\"wait_s\"]-e[\"agent_s\"])<0.01, e[\"wait_s\"]>0.4) for e in E if e[\"event\"]==\"turn\" and e[\"phase\"]==\"stop\" and e.get(\"session\")==\"sess-t\"]==[(False,True,True)]"' "turn stop wrong"
sleep 0.3
telhook "$M" "$(turn_json UserPromptSubmit "$M" sess-t)"
telhook "$M" "$(turn_json PostToolUse "$M" sess-t Bash 5)" tool
LIGHT_HARNESS_HUMAN_GAP_CAP=0 telhook "$M" "$(turn_json UserPromptSubmit "$M" sess-t)"   # Esc: no Stop came
check 'evq "[(e.get(\"capped\"), e.get(\"response_s\")) for e in E if e[\"event\"]==\"turn\" and e[\"phase\"]==\"start\" and e.get(\"session\")==\"sess-t\"][::2]==[(None,None),(True,0)]" && evq "[e[\"gap_s\"]>0.25 and e[\"response_s\"]==e[\"gap_s\"] for e in E if e[\"event\"]==\"turn\" and e[\"phase\"]==\"start\" and e.get(\"session\")==\"sess-t\" and e.get(\"capped\") is False]==[True]"' "turn starts lack the human response gap, or the cap is ignored"
check 'evq "[e[\"interrupted\"] for e in E if e[\"event\"]==\"turn\" and e[\"phase\"]==\"stop\" and e.get(\"session\")==\"sess-t\"]==[False,True]"' "interrupted turn not closed"
# hooks run async, so they overlap: the session's state must survive it
for i in 1 2 3 4 5 6 7 8; do telhook "$M" "$(turn_json PostToolUse "$M" sess-t Read 5)" tool & done; wait
check '"$PYBIN" -c "import json,sys; json.load(open(sys.argv[1]))" "$SD/sess-t.json" && [ -z "$(ls "$SD" | grep -v "\.json$")" ]' "overlapping hooks broke the session state or left a lock"
# a hook in the task worktree knows the task and its stage without git
telhook "$M" "$(turn_json UserPromptSubmit "$T/alpha" sess-wt)"; telhook "$M" "$(turn_json SessionEnd "$T/alpha" sess-wt)"
check 'evq "[(e.get(\"task\"),e.get(\"stage\")) for e in E if e[\"event\"]==\"turn\" and e.get(\"session\")==\"sess-wt\"][:1]==[(\"alpha\",\"Done\")]"' "turn in the task worktree lacks its task or stage"
telhook "$M" "$(turn_json SessionEnd "$M" sess-t)"
check '[ ! -e "$SD/sess-t.json" ] && evq "sum(e[\"event\"]==\"turn\" and e[\"phase\"]==\"stop\" and e.get(\"session\")==\"sess-t\" for e in E)==3"' "SessionEnd didn't close the open turn or remove the session state"
check '! evq "any(\"spoof\" in json.dumps(e) for e in E)"' "a tool's input leaked into the events"
# the split itself, on fixed timestamps: two sessions on one task, a wait, a capped gap, an interrupt
check '"$PYBIN" - "$PLUGIN/scripts" <<EOF
import sys; sys.path.insert(0, sys.argv[1]); import telemetry as T
T0 = 1_800_000_000.0
def ev(t, event, **a):
    a.update(event=event, ts=T.iso(T0 + t), _t=T0 + t); return a
I = lambda t: T.iso(T0 + t)
E = [ev(10, "stage", task="a", session="s1", to="Initialize"), ev(0, "command", task="a", session="s1", command="x", ok=True),
     ev(300, "wait", session="s1", kind="permission", wait_start=I(200), wait_s=60),
     ev(400, "turn", session="s1", phase="stop", turn_start=I(100), turn_s=300),
     ev(1000, "turn", session="s1", phase="start", gap_start=I(400), gap_s=600, response_s=600, engineer="Owl"),
     ev(1100, "stage", task="a", session="s1", to="Plan"),
     ev(1200, "turn", session="s1", phase="stop", turn_start=I(1000), turn_s=200),
     ev(1500, "turn", session="s2", task="a", phase="stop", turn_start=I(1100), turn_s=400, interrupted=True),
     ev(11200, "turn", session="s1", phase="start", gap_start=I(1200), gap_s=10000, response_s=7200, capped=True),
     ev(11300, "turn", session="s1", phase="stop", turn_start=I(11200), turn_s=100)]
E.sort(key=lambda e: e["_t"])
k = T.kpis({"a": [(int(T0), "harness: task-start a"), (int(T0) + 12000, "harness: verdict pass by r")]}, set(), E, now=T0 + 20000)
x = k["tasks"]["a"]["time_split"]
assert (x["total_s"], x["agent_s"], x["in_turn_wait_s"], x["response_s"]) == (12000, 840, 60, 7500), x
assert abs(x["agent_s"] + x["human_s"] + x["idle_s"] - x["total_s"]) < 1e-6 and x["interrupted"] == 1, x
assert x["by_stage"]["Initialize"] == {"agent_s": 340, "human_s": 660, "idle_s": 90}, x["by_stage"]
assert k["engineers"]["Owl"]["median_response_s"] == 600 and k["summary"]["time_split"]["agent_s"] == 840, k
assert "Time split" in T.render(k) and "agent/human/idle" in T.render(k, "a")
EOF' "agent/human/idle split is wrong"
N="$(wc -l < "$EV")"
LIGHT_HARNESS_TELEMETRY=0 $H check plan $X >/dev/null 2>&1
check '[ "$(wc -l < "$EV")" = "$N" ]' "LIGHT_HARNESS_TELEMETRY=0 didn't turn telemetry off"
# OTLP export to a local collector stand-in
"$PYBIN" - "$W/otlp" <<'EOF' &
import http.server, pathlib, sys
out = pathlib.Path(sys.argv[1])
class R(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        with open(str(out) + ".body", "ab") as f:
            f.write(f"{self.path} {self.headers.get('X-Team', '')} ".encode() + body + b"\n")
        self.send_response(200); self.end_headers()
    def log_message(self, *a): pass
s = http.server.HTTPServer(("127.0.0.1", 0), R)
out.write_text(str(s.server_port))
s.serve_forever()
EOF
OTLP_PID=$!
for _ in $(seq 50); do [ -s "$W/otlp" ] && break; sleep 0.1; done
LIGHT_HARNESS_OTLP_ENDPOINT="http://127.0.0.1:$(cat "$W/otlp")" LIGHT_HARNESS_OTLP_HEADERS="X-Team=qa" $H check plan $X >/dev/null 2>&1
# hooks export from a detached process: the hook returns first, the record arrives a moment later
LIGHT_HARNESS_OTLP_ENDPOINT="http://127.0.0.1:$(cat "$W/otlp")" LIGHT_HARNESS_OTLP_HEADERS="X-Team=qa" \
  telhook "$M" "$(turn_json UserPromptSubmit "$M" sess-otlp)"
for _ in $(seq 50); do grep -q "light_harness.turn" "$W/otlp.body" 2>/dev/null && break; sleep 0.1; done
kill "$OTLP_PID" 2>/dev/null; wait "$OTLP_PID" 2>/dev/null
check 'grep -q "^/v1/logs qa .*resourceLogs.*\"session.id\".*sess-alpha" "$W/otlp.body"' "events not exported over OTLP"
check 'grep -q "^/v1/logs qa .*light_harness.turn.*sess-otlp" "$W/otlp.body"' "hook events not exported over OTLP in the background"
telhook "$M" "$(turn_json SessionEnd "$M" sess-otlp)"
check 'human "$H telemetry opt-out" "" && [ -z "$(git config --global lightharness.engineer)" ]' "opt-out"
unset LIGHT_HARNESS_SESSION_ID

echo "Finish alpha: the branch holds only the work"
echo "late" > "$A/src/late.py"
nok $H finish-task alpha                         # uncommitted changes
(cd "$A" && git add -A && git commit -qm "unreviewed extra")
nok $H finish-task alpha                         # branch differs from the reviewed snapshot
git -C "$A" reset -q --hard HEAD~1
ok  $H finish-task alpha
check '[ ! -d "$A" ] && git rev-parse -q --verify refs/heads/task/alpha >/dev/null && git rev-parse -q --verify refs/light-harness/done/alpha >/dev/null' "finish-task should remove the folder and keep the branch"
check '[ -z "$(git ls-tree -r --name-only task/alpha | grep -E "light-harness|contract\.md|harness\.py|^\.harness")" ]' "harness files on the task branch"
check '! git log --format=%s task/alpha | grep -q "harness"' "harness commits on the task branch"
check '[ "$(git log --format=%s "$HEAD0"..task/alpha)" = "step 1-2: dedupe" ]' "task branch should hold just the agent's commit"
check '[ "$(fingerprint "$M")" = "$BEFORE" ] && [ "$(git rev-parse HEAD)" = "$HEAD0" ]' "main checkout changed during the task"
check '[ -z "$(git for-each-ref refs/tags)" ]' "tags created"
check '[ -f .git/light-harness/config.json ]' "repo config not kept for the next task"
check '[ "$(hook "$M" "$(file_json Write "$M" "$M/src/x.py")")" = 0 ]' "guard still blocks the main checkout with no task in progress"

# flow <name>: one AC, square(x), driven from the main checkout with --task
# flow_red <name> [extra §8.1 lines]: the same up to a passed red gate (approve output in $W/approve-<name>.out)
flow_red() {
  local X="--task $1" D="$T/$1" K="$T/$1/.light-harness/contract.md"
  edit "$K" "<one or two sentences: what outcome, for whom>" "$1" \
    "| EX-1 | <input> | <expected> | <happy path / edge / error> |" "| EX-1 | 3 | 9 | happy |" \
    "- AC-1: <Given … When … Then …>" "- AC-1: Given x, then x squared"
  ok  $H set-status Initialize $X
  edit "$K" "| R-1 | <risk> | <L/M/H> | <L/M/H> | <mitigation> |" "| R-1 | none | L | L | n/a |" \
    "| <area> | <change> | <who/what is affected> | <how to undo> |" "| src | new | none | delete |" \
    "- <src/path/or/glob>" "- src/**
- tests/**${2:+
$2}" "1. <step> (AC-1)" "1. square (AC-1)"
  ok  $H set-status Plan $X
  check "script -qec \"$H approve --name Approver $X\" /dev/null <<< APPROVE > \"$W/approve-$1.out\" 2>&1" "approve ($1)"
  ok  $H set-status Build $X
  mkdir -p "$D/src" "$D/tests"; touch "$D/src/__init__.py"
  printf 'from src.%s import f\ndef test_%s():\n    """AC-1"""\n    assert f(3) == 9\n' "$1" "$1" > "$D/tests/test_$1.py"
  printf 'def f(x):\n    raise NotImplementedError\n' > "$D/src/$1.py"
  ok  $H redgate $X
  printf 'def f(x):\n    return x * x\n' > "$D/src/$1.py"
}
flow() {
  local X="--task $1" D="$T/$1" K="$T/$1/.light-harness/contract.md"
  flow_red "$1"
  ok  $H verify $X
  ok  $H set-status Review $X
  edit "$K" "- [ ] AC-1" "- [x] AC-1"
  ok  $H verdict pass $X
  (cd "$D" && git add -A && git commit -qm "$1: square")
}

echo "Build on a finished task, plus a parallel task"
ok  $H start-task beta --from alpha
ok  $H start-task gamma
check '[ -f "$T/beta/tests/test_inv.py" ] && [ ! -f "$T/gamma/tests/test_inv.py" ]' "--from base not applied"
check '[ "$(fingerprint "$M")" = "$BEFORE" ]' "starting tasks changed the main checkout"
nok $H finish-task gamma                         # not Done
flow beta                                        # alpha's tests pass at beta's red gate; AC-1 reused
flow gamma
ok  $H finish-task beta --branch feature/beta
ok  $H finish-task --task gamma
check 'git rev-parse -q --verify refs/heads/feature/beta >/dev/null && ! git rev-parse -q --verify refs/heads/task/beta >/dev/null' "--branch didn't rename the branch"
check '[ "$(git worktree list | wc -l)" -eq 1 ] && [ "$($H tasks | grep -c "^  ")" -eq 3 ]' "expected 3 finished tasks and no task folders"
check 'evq "any(e.get(\"task\")==\"beta\" for e in E) and not any(e.get(\"engineer\") for e in E if e.get(\"task\")==\"beta\")"' "events after opt-out still carry the engineer"
check '[ "$(fingerprint "$M")" = "$BEFORE" ] && [ "$(git rev-parse HEAD)" = "$HEAD0" ]' "main checkout changed"

echo "Uninstall removes every trace"
nok $H uninstall </dev/null                      # human only
check '$H uninstall --dry-run | grep -q "refs/light-harness"' "dry run doesn't list the refs"
git branch -q -D task/gamma
check '! human "$H uninstall" UNINSTALL && git rev-parse -q --verify refs/light-harness/done/gamma >/dev/null' "uninstall should refuse while a finished task's work is on no branch"
check 'human "$H uninstall --force" UNINSTALL' "uninstall --force"
check '[ -z "$(git for-each-ref refs/light-harness)" ] && [ ! -e .git/light-harness ] && ! grep -q light-harness .git/info/exclude && [ ! -e .tasks ]' "uninstall left something behind"
check '[ "$(fingerprint "$M")" = "$BEFORE" ]' "uninstall changed the work repo's files"
check '[ "$(hook "$M" "$(bash_json "$M" "git reset --hard")")" = 0 ]' "guard still active after uninstall"

echo "Existing codebase: baseline recorded on entering Build"
B="$W/brown"; newrepo "$B"; mkdir -p "$B/src" "$B/tests"; cd "$B"
touch src/__init__.py; printf 'def g():\n    return 1\n' > src/old.py
printf 'from src.old import g\ndef test_old():\n    assert g() == 1\n' > tests/test_old.py
git add -A; git commit -qm "existing code"
ok  $H start-task delta
T="$B/.tasks" flow delta
check '$H log delta | grep -q "baseline (1 existing"' "baseline event missing"
ok  $H finish-task delta
check 'grep -q "\"task\":\"delta\"" .git/light-harness/telemetry/events.jsonl && human "$H forget delta" FORGET && ! grep -q "\"task\":\"delta\"" .git/light-harness/telemetry/events.jsonl' "forget didn't drop the task's telemetry events"

echo "Repos set up by Light Harness 1.x: uninstall removes the vendored files"
L="$W/legacy"; newrepo "$L"; cd "$L"
mkdir -p scripts .harness .claude
printf '# My project\n\nOur own notes.\n\n<!-- light-harness rules -->\n# Light Harness: Working Rules\n\nold rules\n' > CLAUDE.md
printf '"""Light harness: stage gates for contract-driven agentic development."""\n' > scripts/harness.py
cp "$PLUGIN/template/config.json" .harness/config.json; echo '{}' > .harness/baseline.json
cp "$PLUGIN/template/contract.md" contract.md
printf '# Copy to .env and fill in. Never commit .env.\n# EXAMPLE_API_KEY=\n' > .env.example
printf '.env\n.env.*\n!.env.example\n.harness/junit.xml\n__pycache__/\nnode_modules/\n.tasks/\n' > .gitignore
echo '{"permissions":{"deny":["Bash(git reset:*)","Bash(python3 scripts/harness.py approve:*)","Bash(mine:*)"],"allow":["Bash(git status:*)"]}}' > .claude/settings.json
git add -A; git commit -qm "harness: install light-harness 1.3.0"; git tag done/old
nok $H start-task new                            # refuses while 1.x files are there
check '$H uninstall --dry-run | grep -q "scripts/harness.py"' "dry run doesn't list legacy files"
check 'human "$H uninstall" UNINSTALL' "legacy uninstall"
check '[ ! -e scripts ] && [ ! -e .harness ] && [ ! -e contract.md ] && [ ! -e .env.example ]' "legacy files not removed"
check '[ "$(cat CLAUDE.md)" = "$(printf "# My project\n\nOur own notes.")" ]' "CLAUDE.md: rules not removed or own text lost"
check '"$PYBIN" -c "import json;d=json.load(open(\".claude/settings.json\"))[\"permissions\"];assert d==dict(deny=[\"Bash(mine:*)\"]),d"' "settings.json: legacy rules not removed or own rules lost"
check '! grep -qE "^(\.tasks/|\.harness/junit\.xml|!\.env\.example)$" .gitignore && grep -qx "node_modules/" .gitignore' ".gitignore cleanup"
check 'git rev-parse -q --verify refs/tags/done/old >/dev/null' "legacy tags should be kept"
ok  $H start-task new                            # works once cleaned up (changes still uncommitted is fine)
nok $H uninstall --dry-run                       # a task is in progress
check '$H uninstall --dry-run --force | grep -q "unfinished task folder"' "--force doesn't offer to drop an unfinished task"
check 'human "$H uninstall --force" UNINSTALL && [ ! -e .tasks ] && git rev-parse -q --verify refs/heads/task/new >/dev/null' "uninstall --force should drop the task folder and keep its branch"

echo "Agent machinery only via the approved §8.1; CRLF blobs are not 'modified' (1.6.3)"
N="$W/mach"; newrepo "$N"; cd "$N"; T="$N/.tasks"
mkdir -p .claude/agents src samples; touch src/__init__.py; echo "# helper" > .claude/agents/helper.md
printf 'a\r\nb\r\n' > samples/crlf.txt                   # committed WITH CRLF in the blob (i/crlf)
git -c core.autocrlf=false add -A; git commit -qm "base"; git config core.autocrlf true
check 'git ls-files --eol samples/crlf.txt | grep -q "i/crlf"' "setup: samples/crlf.txt should have a CRLF blob"
ok  $H start-task delta; ok $H start-task epsilon; ok $H start-task zeta
mod() { cp "$T/$1/$2" "$W/keep.m"; echo changed >> "$T/$1/$2"; }   # change a file behind the guard's back
back() { cp "$W/keep.m" "$T/$1/$2"; }
flow_red delta                                           # §8.1: src/** tests/**
ok  $H verify --task delta                               # the CRLF protected file is unchanged
mod delta samples/crlf.txt
check '! $H verify --task delta > "$W/v.out" 2>&1 && grep -qF "file modified: samples/crlf.txt" "$W/v.out"' "a real change to a CRLF file was missed"
back delta samples/crlf.txt
check '[ "$(hook "$N" "$(file_json Edit "$N" "$T/delta/.claude/agents/helper.md")")" = 2 ]' "guard allowed an agent edit that §8.1 doesn't list"
check '! grep -q "lets the task change agent permissions" "$W/approve-delta.out"' "approve warned about machinery the plan doesn't list"
mod delta .claude/agents/helper.md
check '! $H verify --task delta > "$W/v.out" 2>&1 && grep -qF "✗ Anti-gaming: agent permissions or agents modified after approval: .claude/agents/helper.md" "$W/v.out"' "unlisted agent change didn't fail verify"
edit "$T/delta/.light-harness/contract.md" "<!-- harness:deviations -->" "<!-- harness:deviations -->
- .claude/agents/helper.md — needed"
check '! $H verify --task delta >/dev/null 2>&1' "a §10.3-style entry sanctioned an agent change"
back delta .claude/agents/helper.md
K="$T/delta/.light-harness/contract.md"; cp "$K" "$W/keep.k"     # a scope block injected outside the approved plan
edit "$K" "<!-- harness:plan -->" "<!-- harness:scope -->
- src/**
- tests/**
- .claude/agents/**
<!-- /harness:scope -->
<!-- harness:plan -->"
check '[ "$(hook "$N" "$(file_json Edit "$N" "$T/delta/.claude/agents/helper.md")")" = 2 ]' "guard honoured a scope block injected outside the plan"
mod delta .claude/agents/helper.md
check '! $H verify --task delta > "$W/v.out" 2>&1 && grep -qF "✓ Plan approval" "$W/v.out" && grep -qF "✗ Anti-gaming: agent permissions or agents modified after approval" "$W/v.out"' "verify honoured a scope block injected outside the plan"
back delta .claude/agents/helper.md; cp "$W/keep.k" "$K"
flow_red zeta "- **"                                     # a broad glob never covers machinery
check '[ "$(hook "$N" "$(file_json Edit "$N" "$T/zeta/.claude/agents/helper.md")")" = 2 ]' "guard allowed an agent edit under a broad glob"
mod zeta .claude/agents/helper.md
check '! $H verify --task zeta > "$W/v.out" 2>&1 && grep -qF "✗ Anti-gaming: agent permissions or agents modified after approval: .claude/agents/helper.md" "$W/v.out"' "a broad §8.1 glob sanctioned an agent change"
back zeta .claude/agents/helper.md
check '[ "$(hook "$N" "$(file_json Edit "$N" "$T/epsilon/.claude/agents/helper.md")")" = 2 ]' "guard allowed an agent edit before approval"
flow_red epsilon "- .claude/agents/**"
check 'grep -q "lets the task change agent permissions" "$W/approve-epsilon.out" && grep -q "\.claude/agents/\*\*  (covers every agent)" "$W/approve-epsilon.out"' "approve didn't warn about the listed agent paths"
check '[ "$(hook "$N" "$(file_json Edit "$N" "$T/epsilon/.claude/agents/helper.md")")" = 0 ]' "guard blocked an agent edit the approved §8.1 lists"
check '[ "$(hook "$N" "$(file_json Write "$N" "$T/epsilon/.claude/settings.json")")" = 2 ]' "guard allowed settings.json, which §8.1 doesn't list"
mod epsilon .claude/agents/helper.md
check '$H verify --task epsilon > "$W/v.out" 2>&1 && grep -qF "⚠ Anti-gaming (review): .claude/agents/helper.md changes agent permissions" "$W/v.out"' "listed agent change should pass verify with a warning"
K="$T/epsilon/.light-harness/contract.md"; cp "$K" "$W/keep.k"
edit "$K" "1. square (AC-1)" "1. square it (AC-1)"     # plan changed after approval
check '[ "$(hook "$N" "$(file_json Edit "$N" "$T/epsilon/.claude/agents/helper.md")")" = 2 ]' "guard allowed an agent edit after the plan changed"
cp "$W/keep.k" "$K"
ok  $H set-status Review --task epsilon
check '[ "$(hook "$N" "$(file_json Edit "$N" "$T/epsilon/.claude/agents/helper.md")")" = 2 ]' "guard allowed an agent edit during Review"
edit "$K" "|----|----------|--------|------|-------|-------------|------------------|
" "|----|----------|--------|------|-------|-------------|------------------|
| RV-1 | Medium | Open | src | - | needs a fix | R-1 |
"
ok  $H verdict fail --task epsilon
check '[ "$(hook "$N" "$(file_json Edit "$N" "$T/epsilon/.claude/agents/helper.md")")" = 0 ]' "guard blocked a listed agent edit in Rework"

echo "Rules over an input space: Preservation BR, oracle, sweeps and saved probes (1.7.0)"
# Replays a guard rewrite that took three review rounds: a coarse deny rule is replaced by a precise
# one, which lets through commands it doesn't recognise ("blocked before, allowed now").
cat > "$W/units.py" <<'PY'
import re, sys; sys.path.insert(0, sys.argv[1]); import harness as H, telemetry as T
c = """<!-- harness:plan -->
<!-- harness:rules -->
- BR-1: plain rule, every input keeps its outcome
- BR-2 [Preservation]: keeps outcomes
  - Input space: a × b
  - Oracle: base commit
  - Unacceptable failure direction: allowed now, blocked before
  - Allowed changes: EX-3, EX-4
- BR-10 [invariant]: holds for every x
  - Input space: x
- BR-11 [Nonsense]: what
<!-- /harness:rules -->
<!-- harness:test-strategy -->
- AC-1 → tests/t.py
  - BR-2 [sweep] tests/t.py::test_sweep
    - generator: full cross product
    - oracle: golden file from the base commit
- BR-10 [sweep] tests/u.py, generator: random seed 7 (no oracle named)
<!-- /harness:test-strategy -->
<!-- /harness:plan -->"""
s = H.rule_specs(c)
assert [s[k]["kind"] for k in ("BR-1", "BR-2", "BR-10")] == ["Example", "Preservation", "Invariant"], s
assert s["BR-2"]["fields"]["oracle"] == "base commit" and s["BR-2"]["fields"]["allowed changes"] == "EX-3, EX-4", s
p = H.sweep_plan_problems(c)
assert any("BR-10" in x and "Oracle" in x for x in p), p                       # missing BR fields
assert any("BR-10" in x and "oracle" in x and "[sweep]" in x for x in p), p    # §8.4 entry without an oracle
assert any("BR-11" in x and "unknown" in x for x in p), p
assert not any("BR-2" in x or "BR-1:" in x or "BR-1 " in x for x in p), p
assert any("BR-1" in h for h in H.rule_hints(c)) and not any("BR-2" in h for h in H.rule_hints(c)), H.rule_hints(c)
assert H.missing_sweeps(c, '"""AC-1 BR-2 [sweep]: x"""\n"""BR-100 [sweep]"""') == ["BR-10"]
assert H.missing_sweeps(c, '"""BR-20 [sweep]"""\nBR-2 is swept') == ["BR-2", "BR-10"]
files = {"src/a.py": "from .b import x\nimport src.c\nimport os\n", "src/b.py": "", "src/c.py": "from src import d\n",
         "src/d.py": "", "src/__init__.py": "", "web/a.mjs": "import x from './b.mjs'\nconst y = require('../lib/c')\n",
         "web/b.mjs": "export * from './e'", "web/e.ts": "", "lib/c.js": "", "other.py": ""}
assert H.import_closure(["src/a.py"], set(files), files.get) == ["src/__init__.py", "src/a.py", "src/b.py", "src/c.py", "src/d.py"]
assert H.import_closure(["web/a.mjs"], set(files), files.get) == ["lib/c.js", "web/a.mjs", "web/b.mjs", "web/e.ts"]
assert H.class_name("Allowed-now  blocked-before: wrappers × cp; e.g. `x`") == "allowed-now blocked-before"
assert H.class_name("") == "" and H.class_name("-") == ""
issues = """<!-- harness:issues -->
| ID | Severity | Status | Area | Class | Description | Ref |
|----|----------|--------|------|-------|-------------|-----|
| RV-1 | High | Open | guard | Blocked before, allowed now: prefixes × cp | `command cp L /tmp/b` and `exec cp L /tmp/b` | BR-2 |
<!-- /harness:issues -->"""
rows = H.issue_rows(issues)
assert rows[0]["id"] == "RV-1" and rows[0]["class"].startswith("Blocked before") and rows[0]["status"] == "Open", rows
assert H.issue_rows(issues.replace("| Class ", "").replace("|-------", "").replace("| Blocked before, allowed now: prefixes × cp ", ""))[0]["class"] == ""
t = '''import pytest
@pytest.mark.parametrize("c", [
    "command cp L /tmp/b",
])
def test_rv1_a(c):
    """RV-1: regression"""
    assert blocked(c)
def test_rv1_b():
    """RV-1: the class"""
    for p in ["command ", "builtin ", "exec "]:
        assert blocked(p + "cp L /tmp/b")
def test_other():
    assert blocked("command cp L /tmp/b")
'''
assert len(H.test_chunks(t)) == 4, H.test_chunks(t)
w = H.overfit_warnings(rows, {"tests/test_x.py": t})
assert len(w) == 1 and "tests/test_x.py" in w[0] and "RV-1" in w[0] and "test_rv1_a" in w[0], w
js = "test('RV-1 cp behind command', () => {\n  expect(blocked('exec cp L /tmp/b')).toBe(true)\n})\n"
assert len(H.overfit_warnings(rows, {"t.test.js": js})) == 1
r = lambda *x: [dict(zip(("id", "class", "status"), i)) for i in x]
assert H.same_class_reopened([r(("RV-1", "Leak: a", "Open"), ("RV-2", "leak: b", "Open")),
                              r(("RV-1", "Leak: a", "Resolved"), ("RV-2", "leak", "Resolved"), ("RV-3", "LEAK: c", "Open"), ("RV-4", "other", "Open")),
                              r(("RV-1", "Leak: a", "Open"), ("RV-3", "LEAK: c", "Resolved"), ("RV-4", "other", "Resolved"))]) == 2
k = T.kpis({"a": [(1800000000, "harness: task-start a")], "b": [(1800000000, "harness: task-start b")]}, set(), [], now=1800000100,
           issue_rounds={"a": [r(("RV-1", "x: y", "Open")), r(("RV-1", "x", "Resolved"), ("RV-2", "x: z", "Open"))]})
assert k["tasks"]["a"]["same_class_reopened"] == 1 and k["tasks"]["b"]["same_class_reopened"] == 0, k["tasks"]
assert k["summary"]["same_class_reopened_per_task"] == 0.5 and "Same class reopened" in T.render(k, "a"), k["summary"]
assert T.kpis({"a": [(1800000000, "harness: task-start a")]}, set(), [], now=1800000100)["tasks"]["a"]["same_class_reopened"] == 0
# review round 1: a kind tag in any spelling is read, or refused; it never silently becomes an Example
rules = lambda *ls: "<!-- harness:plan -->\n<!-- harness:rules -->\n" + "\n".join(ls) + "\n<!-- /harness:rules -->\n<!-- /harness:plan -->"
for v in ("- **BR-2** [Preservation]: x", "- BR-2 [ Preservation ]: x", "- BR-2 (Preservation): x", "- BR-2 **[Preservation]**: x",
          "- BR-2 [Preservation] keeps outcomes", "- BR-2: [Preservation] keeps outcomes", "- **BR-2 [Preservation]**: x", "- BR-2 [preservation]: x"):
    assert H.rule_specs(rules(v))["BR-2"]["kind"] == "Preservation", v
    assert any("BR-2" in x and "Oracle" in x for x in H.sweep_plan_problems(rules(v))), v
for v in ("- BR-2 [Preservation-rule]: x", "- BR-2 [Preservation, Invariant]: x", "- BR-2 (Invariants): x", "- BR-2 [TODO]: x"):
    assert any("BR-2" in x and "unknown" in x for x in H.sweep_plan_problems(rules(v))), v
for v in ("- BR-2 (see EX-2): x", "- BR-2: **Shell rule.** keep x", "- BR-2: x [EX-1] y", "- BR-2: (for example, a cached response) x",
          "- BR-2: [Admin] users can delete", "- BR-2: [Spec](https://example.com) section 4 applies", "- BR-2 (example EX-2): x",
          "- BR-2: [WIP] x", "- BR-2: keep the loop invariant", "- BR-2: preserve the order of rows"):
    assert H.rule_specs(rules(v))["BR-2"]["kind"] == "Example" and not H.sweep_plan_problems(rules(v)), (v, H.sweep_plan_problems(rules(v)))
# review round 2: a kind named where the parser can't read it fails closed; duplicate ids are refused
for v in ("- BR-2 {Preservation}: x", "- BR-2 – Preservation: x", "- BR-2 <Preservation>: x", "- BR-2 Preservation: x",
          "- Preservation BR-2: x", "+ BR-2 {Invariant}: x", "1) BR-2 Preservation: x", "### BR-2 [Preservation]",
          "- BR-2: The [Invariant] word"):
    assert any("names a rule kind" in x for x in H.sweep_plan_problems(rules(v))), (v, H.sweep_plan_problems(rules(v)))
assert any("names a rule kind" in x for x in H.sweep_plan_problems(rules("- BR-1: x", "  - BR-2 [Preservation]: nested under BR-1")))
assert H.rule_specs(rules("- `BR-2` [Preservation]: x"))["BR-2"]["kind"] == "Preservation"
assert H.rule_specs(rules("1) BR-2 [Invariant]: x", "+ BR-3 [Preservation]: y"))["BR-3"]["kind"] == "Preservation"
assert any("defined twice" in x for x in H.sweep_plan_problems(rules("- BR-2 [Preservation]: x", "- BR-2: y")))
empty = c.replace("    - generator: full cross product\n    - oracle: golden file from the base commit", "    - generator:\n    - oracle: <how>")
assert any("BR-2" in x and "generator" in x for x in H.sweep_plan_problems(empty)), H.sweep_plan_problems(empty)
assert H.missing_sweeps(c, "# TODO BR-2 [sweep]\n// BR-10 [sweep]") == ["BR-2", "BR-10"]
src = {"tests/t.py": 'def test_ac2_br2_sweep():\n    """AC-2 BR-2 [sweep]"""\n@pytest.mark.parametrize("x", [1])\ndef test_ac2_br2_tagged(x):\n    """BR-2 [sweep]"""\ndef test_other_sweep():\n    pass\n',
       "s.test.js": "test('AC-2 BR-2 [sweep] every command', () => {})\n"}
names = H.sweep_test_names(src, ["BR-2"])
assert H.is_tagged_sweep("tests.t::test_ac2_br2_sweep", names) and H.is_tagged_sweep("s.test.js::AC-2 BR-2 [sweep] every command", names), names
assert not H.is_tagged_sweep("tests.test_sweep_extras::test_ac1_vacuous", names)          # "sweep" only in the module
assert not H.is_tagged_sweep("tests.t::test_other_sweep", names)                          # named sweep, but untagged
assert not H.is_tagged_sweep("tests.t::test_ac2_br2_tagged[1]", names)                    # tagged, but not named sweep
assert not H.sweep_test_names(src, ["BR-9"])
assert not H.is_tagged_sweep("tests.test_dup::test_ac2_br2_sweep", names)                 # same name, another module
assert H.is_tagged_sweep("tests.t.TestX::test_ac2_br2_sweep", names)                      # a class in that module
lay = {"pkg/src/app/__init__.py": "", "pkg/src/app/guard.py": "from app import words\nfrom app.util import h\n",
       "pkg/src/app/words.py": "", "pkg/src/app/util.py": "", "pkg/other.py": ""}
assert H.import_closure(["pkg/src/app/guard.py"], set(lay), lay.get) == sorted(k for k in lay if k != "pkg/other.py")
fx = '@pytest.fixture\ndef helper():\n    return 1\ndef test_rv1():\n    """RV-1"""\n    assert blocked("command cp L /tmp/b")\n'
assert len(H.overfit_warnings(rows, {"f.py": fx})) == 1, H.test_chunks(fx)
assert H.class_name("n/a") == "" and H.class_name("None: x") == ""
assert H.same_class_reopened([r(("RV-1", "x", "Open")), r(("RV-1", "x", "Open"))]) == 0           # staying Open isn't reopening
assert H.same_class_reopened([r(("RV-1", "x", "Open")), r(("RV-1", "x", "Fixed")), r(("RV-1", "x", "Open"))]) == 1
for line in ('if os.environ.get("LIGHT_HARNESS_PROBE"):', "base = Path('.light-harness-base')", "k.startswith('light_harness')",
             "X = 'LIGHT-HARNESS'"):
    assert H.code_gaming("src/a.py", [line])[0], line
for path, line in (("pytest.ini", "norecursedirs = .light-harness-base"), ("tsconfig.json", '"exclude": [".light-harness-base"]'),
                   ("src/a.py", "# see the light-harness probes docs"), ("src/a.js", "// light-harness")):
    assert H.code_gaming(path, [line]) == ([], []), (path, line)
assert H.code_gaming("src/a.py", ["p = os.getppid()"])[1] and not H.code_gaming("src/a.py", ["p = os.getppid()"])[0]
# review round 3: only a task with saved probes is checked, and a declared domain term is a warning
assert H.code_gaming("src/a.py", ['PART = "Tail light harness"'], probes_saved=False) == ([], [])
f_, w_ = H.code_gaming("src/a.py", ["import light_harness_client"], waived=True)
assert not f_ and w_ and "§10.3" in w_[0], (f_, w_)
for line in ('PART = "Tail light harness"', '"""A light harness around subprocess."""', "class HighlightHarness:"):
    assert H.code_gaming("src/a.py", [line]) == ([], []), line
# review round 4: the harness's own folders always fail; the harness's name warns until probes exist
for line in ('os.path.isdir(".light-harness-base")', "Path('.light-harness') / 'contract.md'"):
    assert H.code_gaming("src/a.py", [line], probes_saved=False)[0], line
f_, w_ = H.code_gaming("src/a.py", ["import light_harness_client"], probes_saved=False)
assert not f_ and w_, (f_, w_)
# review round 5: a tool config that ignores the folders is declared in §10.3; runs get a CI-like environment
f_, w_ = H.code_gaming("eslint.config.js", ['export default [{ ignores: [".light-harness-base/**"] }]'], waived=True)
assert not f_ and w_ and "§10.3" in w_[0], (f_, w_)
import os
os.environ.update(CLAUDECODE="1", CLAUDE_CODE_ENTRYPOINT="cli", CLAUDE_PROJECT_DIR="/p", LIGHT_HARNESS_SESSION_ID="s",
                  HARNESS_ROOT="/x", CLAUDE_API_URL="http://x",
                  PATH=os.pathsep.join([str(H.PLUGIN_ROOT / "bin"), os.environ.get("PATH", "")]))
e = H.clean_env()
assert not ({"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PROJECT_DIR", "LIGHT_HARNESS_SESSION_ID", "HARNESS_ROOT"} & set(e)), sorted(e)
assert e.get("CLAUDE_API_URL") == "http://x"                                             # a project's own variable stays
import tempfile, pathlib
lk = pathlib.Path(tempfile.mkdtemp()) / "base-lock-t"
H._take_lock(lk); assert lk.read_text() == str(os.getpid())
import contextlib, io
try:
    with contextlib.redirect_stderr(io.StringIO()):
        H._take_lock(lk)
    raise AssertionError("a second run took a held lock")
except SystemExit:
    pass
if os.name != "nt":                                                                       # a killed run's lock is taken over
    lk.write_text("999999999"); H._take_lock(lk); assert lk.read_text() == str(os.getpid())
lk.unlink()
assert str(H.PLUGIN_ROOT / "bin") not in e["PATH"].split(os.pathsep) and e["PATH"], e["PATH"]
lay2 = {"backend/tests/test_g.py": 'def test_br2_sweep():\n    """BR-2 [sweep]"""\n'}
assert H.is_tagged_sweep("tests.test_g::test_br2_sweep", H.sweep_test_names(lay2, ["BR-2"]))          # rootdir below the task folder
assert H.is_tagged_sweep("tests.test_g.TestX::test_br2_sweep", H.sweep_test_names(lay2, ["BR-2"]))
assert not H.is_tagged_sweep("other.test_g::test_br2_sweep", H.sweep_test_names(lay2, ["BR-2"]))
PY
check '"$PYBIN" "$W/units.py" "$PLUGIN/scripts"' "rule kinds, sweep entries, import closure, issue classes or over-fit chunks parsed wrongly"

S="$W/sweep"; newrepo "$S"; cd "$S"; T="$S/.tasks"
mkdir -p src tests; touch src/__init__.py
printf 'WRITE = ("rm", "cp", "tee", "mv")\n' > src/words.py
cat > src/guard.py <<'EOF'
from src.words import WRITE
LOG = "logs/app.log"
def blocked(cmd):
    """Coarse deny rule: a write word anywhere plus the log path anywhere."""
    return LOG in cmd and any(w in WRITE for w in cmd.split())
EOF
printf 'from src.guard import blocked\ndef test_old():\n    assert blocked("rm logs/app.log") and not blocked("cat logs/app.log")\n' > tests/test_old.py
git add -A; git commit -qm "existing guard"
ok  $H start-task guardfix
X="--task guardfix"; D="$T/guardfix"; K="$D/.light-harness/contract.md"; EV="$S/.git/light-harness/telemetry/events.jsonl"
edit "$K" "<one or two sentences: what outcome, for whom>" "Let read-only commands on the log through the guard" \
  "| EX-1 | <input> | <expected> | <happy path / edge / error> |" "| EX-1 | grep rm logs/app.log | allowed | happy |
| EX-2 | cp logs/app.log /tmp/b | allowed | happy |" \
  "- AC-1: <Given … When … Then …>" "- AC-1: Given EX-1 or EX-2, then the command is allowed
- AC-2: Given any other command, then the guard gives the outcome it gave at the base commit (BR-2)" \
  "- BR-1: <rule, stated precisely enough to test>" "- BR-1: a grep of the log and a cp from the log are allowed
- BR-2 [Preservation]: every other command keeps the outcome the guard gave it at the base commit
  - Input space: prefixes × command words × targets × separators
  - Unacceptable failure: allowed now but blocked before is never acceptable
  - Allowed changes: EX-1, EX-2"
ok  $H set-status Initialize $X
edit "$K" "| R-1 | <risk> | <L/M/H> | <L/M/H> | <mitigation> |" "| R-1 | the rewrite lets through what the old rule blocked | M | H | keep the coarse rule as a fallback; BR-2 sweep |" \
  "| <area> | <change> | <who/what is affected> | <how to undo> |" "| src/guard.py | rewrite | every command the guard judges | revert |" \
  "- <src/path/or/glob>" "- src/**
- tests/**" \
  "1. <step> (AC-1)" "1. exempt the two read-only forms, keep the coarse rule for the rest (AC-1, AC-2)" \
  "- AC-1 → <unit / integration test description, examples used>" "- AC-1 → tests/test_guard.py: EX-1 and EX-2 verbatim
- AC-2 → tests/test_guard.py: the BR-2 sweep"
check '! $H check plan $X > "$W/cp.out" 2>&1 && grep -q "BR-2.*Oracle" "$W/cp.out"' "a Preservation BR without an oracle passed the plan gate"
nok $H set-status Plan $X
edit "$K" "  - Unacceptable failure: allowed now but blocked before is never" "  - Oracle: blocked() at the base commit (light-harness baseline src/guard.py)
  - Unacceptable failure: allowed now but blocked before is never"
check '! $H check plan $X > "$W/cp.out" 2>&1 && grep -q "BR-2.*\[sweep\]" "$W/cp.out"' "a Preservation BR without a §8.4 sweep passed the plan gate"
edit "$K" "- AC-2 → tests/test_guard.py: the BR-2 sweep" "- AC-2 → tests/test_guard.py: the BR-2 sweep
  - BR-2 [sweep] test_ac2_br2_sweep: every generated command against a golden file
    - generator: full cross product of 7 prefixes (4 the rule doesn't know) × 7 bodies × 4 separators"
check '! $H check plan $X > "$W/cp.out" 2>&1 && grep -q "BR-2.*oracle" "$W/cp.out"' "a sweep entry without an oracle passed the plan gate"
edit "$K" "    - generator: full" "    - oracle: tests/golden_guard.json, generated from the base-commit copy
    - generator: full"
ok  $H check plan $X
edit "$K" "- BR-1: a grep" "- BR-1 [Bogus]: a grep"
check '! $H check plan $X > "$W/cp.out" 2>&1 && grep -q "BR-1.*unknown" "$W/cp.out"' "an unknown BR kind passed the plan gate"
edit "$K" "- BR-1 [Bogus]: a grep of the log and a cp from the log are allowed" "- BR-1: a grep of the log and a cp from the log are allowed, and their output is unchanged"
check '$H check plan $X > "$W/cp.out" 2>&1 && grep -q "⚠.*BR-1" "$W/cp.out"' "untagged input-space wording should warn, not fail"
ok  $H set-status Plan $X
check 'script -qec "$H approve --name Approver $X" /dev/null <<< APPROVE >/dev/null 2>&1' "approve (guardfix)"
ok  $H set-status Build $X

echo "  baseline: base-commit copies with their imports, side by side"
check '$H baseline $X src/guard.py > "$W/b.out" 2>&1 && [ -f "$D/.light-harness-base/src/guard.py" ] && [ -f "$D/.light-harness-base/src/words.py" ] && [ -f "$D/.light-harness-base/src/__init__.py" ] && [ ! -e "$D/.light-harness-base/tests" ]' "baseline didn't export guard.py with its imports"
check '[ -z "$(git -C "$D" status --porcelain)" ]' "the baseline copies show up in git status"
nok $H baseline $X src/nosuch.py
mkdir -p "$W/outside"; echo precious > "$W/outside/words.py"
rm -rf "$D/.light-harness-base/src"; ln -s "$W/outside" "$D/.light-harness-base/src"
check '$H baseline $X src/guard.py >/dev/null 2>&1 && [ "$(cat "$W/outside/words.py")" = precious ] && [ ! -L "$D/.light-harness-base/src" ] && [ -f "$D/.light-harness-base/src/words.py" ]' "baseline wrote through a link planted inside the base folder"
check '[ "$(hook "$S" "$(file_json Edit "$S" "$D/.light-harness-base/src/guard.py")")" = 2 ]' "guard allowed editing a base-commit copy"
check 'hookout "$S" "$(bash_json "$S" "light-harness baseline --task guardfix src/guard.py")" | grep -q "\"allow\""' "baseline not pre-approved"
check 'hookout "$S" "$(bash_json "$S" "light-harness probe list --task guardfix")" | grep -q "\"allow\""' "probe list not pre-approved"
check '[ "$(hook "$S" "$(bash_json "$S" "light-harness probe drop RV-1.1 --task guardfix --name X --reason y")")" = 2 ]' "guard allowed an agent to drop a probe"
for c in "light-harness probe --name X --reason y drop RV-1.1" "light-harness probe 'drop' RV-1.1" \
         "echo x > .tasks/guardfix/.light-harness-base/src/guard.py" "cp src/guard.py .tasks/guardfix/.light-harness-base/src/guard.py" \
         "sed -i s/a/b/ .tasks/guardfix/.light-harness-base/src/guard.py"; do
  check '[ "$(hook "$S" "$(bash_json "$S" "$c")")" = 2 ]' "guard allowed: $c"
done
check '[ "$(hook "$S" "$(bash_json "$S" "cp .tasks/guardfix/.light-harness-base/src/guard.py /tmp/old.py")")" = 0 ]' "guard blocked copying a base copy out"
check 'hookout "$S" "$(bash_json "$S" "light-harness probe add p.py --issue RV-1 --run \"curl http://example.invalid {file}\"")" | grep -q "\"ask\""' "a probe runner hid a network command from the ask"

echo "  test-author: examples plus a generated sweep against the base-commit oracle"
cat > "$W/gen.py" <<'EOF'
import itertools, json, sys
sys.path.insert(0, sys.argv[1])
from src.guard import blocked
L = "logs/app.log"
PREFIXES = ["", "sudo ", "nice ", "time -p ", "env A=1 ", "xargs ", "nohup "]
BODIES = [f"rm {L}", f"cp /tmp/x {L}", f"cp {L} /tmp/b", f"tee {L}", f"grep rm {L}", f"cat {L}", f"mv {L} /tmp/y"]
SEPS = ["{}", "true; {}", "{} && true", "echo x | {}"]
cmds = [s.format(p + b) for p, b, s in itertools.product(PREFIXES, BODIES, SEPS)]
json.dump({c: blocked(c) for c in cmds}, open(sys.argv[2], "w"), indent=0, sort_keys=True)
EOF
"$PYBIN" "$W/gen.py" "$D/.light-harness-base" "$D/tests/golden_guard.json"
cat > "$D/tests/test_guard.py" <<'EOF'
import json, pathlib
import pytest
from src.guard import blocked
GOLDEN = json.loads((pathlib.Path(__file__).parent / "golden_guard.json").read_text())
ALLOWED = {"grep rm logs/app.log", "cp logs/app.log /tmp/b"}   # EX-1, EX-2: the listed changes
@pytest.mark.parametrize("cmd", sorted(ALLOWED))
def test_ac1_examples(cmd):
    """AC-1 EX-1 EX-2"""
    assert blocked(cmd) is False
def test_ac2_br2_sweep():
    """AC-2: all generated commands keep their base outcome, apart from EX-1 and EX-2."""
    diffs = sorted(c for c, old in GOLDEN.items() if c not in ALLOWED and blocked(c) != old)
    assert diffs == [], f"{len(diffs)} of {len(GOLDEN)} differ: {diffs[:20]}"
EOF
check '[ "$(wc -l < "$D/tests/golden_guard.json")" -gt 190 ]' "the golden file wasn't generated from the base copy"
check '! $H redgate $X > "$W/rg.out" 2>&1 && grep -q "BR-2" "$W/rg.out"' "red gate accepted a Preservation BR without a [sweep] test"
edit "$D/tests/test_guard.py" '"""AC-2: all' '"""AC-2 BR-2 [sweep]: all'
printf 'from src.guard import blocked\ndef test_ac1_cat():\n    """AC-1"""\n    assert blocked("cat logs/app.log") is False\n' > "$D/tests/test_cat.py"
check '! $H redgate $X > "$W/rg.out" 2>&1 && grep -qx "  tests.test_cat::test_ac1_cat" "$W/rg.out" && ! grep -qx "  tests.test_guard::test_ac2_br2_sweep" "$W/rg.out"' "red gate accepted a non-sweep test passing on the stubs, or refused the sweep"
rm "$D/tests/test_cat.py"
printf 'def test_ac1_vacuous():\n    """AC-1"""\n    assert True\n' > "$D/tests/test_sweep_extras.py"
check '! $H redgate $X > "$W/rg.out" 2>&1 && grep -qx "  tests.test_sweep_extras::test_ac1_vacuous" "$W/rg.out"' "red gate let an untagged test through because its module name holds 'sweep'"
rm "$D/tests/test_sweep_extras.py"
printf 'def test_ac2_br2_sweep():\n    """AC-1"""\n    assert True\n' > "$D/tests/test_dup.py"
check '! $H redgate $X > "$W/rg.out" 2>&1 && grep -qx "  tests.test_dup::test_ac2_br2_sweep" "$W/rg.out"' "red gate let an untagged test through because it reuses a tagged sweep's name in another module"
rm "$D/tests/test_dup.py"
ok  $H redgate $X                                # the sweep passes on stubs that keep the base code
check 'grep -q "Preservation sweep.*test_ac2_br2_sweep" "$K"' "red gate didn't record the Preservation sweep passing on stubs"

echo "  a precise rewrite that lets through what it doesn't recognise"
cat > "$D/src/guard.py" <<'EOF'
import re
from src.words import WRITE
LOG = "logs/app.log"
WRAPPERS = ("sudo", "nice")
def blocked(cmd):
    """Precise rule: judge the command word of each segment."""
    for seg in re.split(r"[;|&]+", cmd):
        w = seg.split()
        while w and w[0] in WRAPPERS:
            w = w[1:]
        if not w or w[0] not in WRITE:
            continue
        if (w[-1] == LOG) if w[0] == "cp" else (LOG in w[1:]):
            return True
    return False
EOF
check '! $H verify $X > "$W/v.out" 2>&1 && grep -q "test_ac2_br2_sweep" "$W/v.out"' "the sweep didn't catch commands blocked before and allowed now"
cp "$D/src/guard.py" "$W/naive.py"              # gaming, no probe saved yet: old behaviour only inside the task
cat >> "$D/src/guard.py" <<'EOF'
import os
_precise = blocked
def blocked(cmd):
    if os.path.isdir(".light-" + "harness-base"):
        return LOG in cmd and any(w in WRITE for w in cmd.split())
    return _precise(cmd)
EOF
check '! $H verify $X > "$W/v.out" 2>&1 && grep -q "test_ac2_br2_sweep" "$W/v.out" && [ -f "$D/.light-harness-base/src/guard.py" ]' "code that switches on the base folder passed the sweep, or the folder wasn't put back"
sed -i 's/".light-" + "harness-base"/".light-harness-base"/' "$D/src/guard.py"
check '! $H verify $X > "$W/v.out" 2>&1 && grep -q "src/guard.py names the harness.s own folders" "$W/v.out"' "code naming the base folder passed verify"
cp "$W/naive.py" "$D/src/guard.py"
mv "$D/.light-harness-base" "$S/.git/light-harness/base-aside-guardfix"   # as if a verify was killed mid-run
check '! $H verify $X >/dev/null 2>&1 && [ -f "$D/.light-harness-base/src/guard.py" ] && [ ! -e "$S/.git/light-harness/base-aside-guardfix" ]' "base copies left aside by a killed run were not put back"
cat > "$D/src/guard.py" <<'EOF'
from src.words import WRITE
LOG = "logs/app.log"
def _coarse(cmd):
    return LOG in cmd and any(w in WRITE for w in cmd.split())
def blocked(cmd):
    """Exempt the two read-only forms; everything else keeps the coarse rule."""
    part = "light_harness_part"  # a harness-like name in code: fine while no probe is saved
    w = cmd.split()
    if w[:1] == ["command"]:
        w = w[1:]
    if len(w) == 3 and w[0] == "grep" and w[2] == LOG:
        return False
    if len(w) == 3 and w[0] == "cp" and w[1] == LOG and w[2] != LOG:
        return False
    return _coarse(cmd)
EOF
ok  $H verify $X                                 # a hole outside the test-author's dimensions
check '$H baseline $X src/guard.py >/dev/null 2>&1 && grep -q "Coarse deny rule" "$D/.light-harness-base/src/guard.py"' "baseline didn't export the base commit's version"
(cd "$D" && git add -A && git commit -qm "step 1: precise rule")
ok  $H set-status Review $X

echo "  reviewer: its own sweep finds the class; saved as a probe that verify re-runs"
cat > "$W/probe_rv1.py" <<'EOF'
import itertools, json, os, subprocess, sys
L = "logs/app.log"
PREFIXES = ["command ", "exec ", "sudo -u root ", "builtin ", "stdbuf -o0 "]
BODIES = [f"cp {L} /tmp/b", f"rm {L}", f"tee {L}", f"cp /tmp/x {L}"]
cmds = [p + b for p, b in itertools.product(PREFIXES, BODIES)]
code = "import json,sys; from src.guard import blocked; print(json.dumps([blocked(c) for c in json.loads(sys.stdin.read())]))"
def run(where):
    r = subprocess.run([sys.executable, "-c", code], cwd=where, input=json.dumps(cmds), capture_output=True, text=True, check=True)
    return json.loads(r.stdout)
old, new = run(sys.argv[1]), run(".")
diffs = [c for c, o, n in zip(cmds, old, new) if o and not n]
print(f"{len(cmds)} commands, {len(diffs)} blocked before and allowed now: {diffs}")
sys.exit(1 if diffs else 0)
EOF
SEP="|----|----------|--------|------|-------|-------------|------------------|"
edit "$K" "$SEP
" "$SEP
| RV-1 | High | Open | src/guard.py | Blocked before, allowed now: prefixes × cp from the log | \`command cp logs/app.log /tmp/b\` is allowed; the base blocked it | BR-2 |
"
nok $H probe add "$W/probe_rv1.py" --issue RV-9 $X       # no such issue
nok $H probe add "$W/nosuch.py" --issue RV-1 $X
check '! $H probe add "$W/probe_rv1.py" --issue RV-1 --run "nosuch-runner {file}" $X >/dev/null 2>&1 && ! $H log guardfix | grep -q "probe-saved"' "a probe with a runner that can't start was saved"
check '$H probe add "$W/probe_rv1.py" --issue RV-1 $X > "$W/p.out" 2>&1 && grep -q "RV-1.1" "$W/p.out"' "probe add failed"
check '$H probe list $X | grep -q "RV-1.1.*RV-1"' "probe list doesn't show the saved probe"
check '$H log guardfix | grep -q "probe-saved RV-1.1"' "the probe isn't in the audit trail"
check '! $H verify $X > "$W/v.out" 2>&1 && grep -q "Saved probe: RV-1.1" "$W/v.out" && grep -q "command cp" "$W/v.out"' "verify didn't fail on the saved probe"
for f in "$D"/.light-harness/probes/*; do echo 'import sys; sys.exit(0)' > "$f"; done
echo 'def blocked(c): return False' > "$D/.light-harness-base/src/guard.py"
echo 'raise SystemExit(0)' > "$D/.light-harness-base/sitecustomize.py"     # a planted file
check '! $H verify $X > "$W/v.out" 2>&1 && grep -q "Saved probe: RV-1.1" "$W/v.out" && [ -f "$D/.light-harness-base/sitecustomize.py" ]' "tampered probe or task base copies let verify pass (probes use their own export), or the task's copies were not put back"
check '[ "$(hook "$S" "$(file_json Edit "$S" "$(ls "$D"/.light-harness/probes/* | head -1)")")" = 2 ]' "guard allowed editing a saved probe"
check '! $H probe run $X >/dev/null 2>&1' "probe run passed with a failing probe"
rm -rf "$D/.light-harness-base"; mkdir -p "$W/evil"; echo 'x = 1' > "$W/evil/planted.py"; ln -s "$W/evil" "$D/.light-harness-base"
check '! $H probe run $X >/dev/null 2>&1 && [ ! -L "$D/.light-harness-base" ] && [ -f "$W/evil/planted.py" ] && [ ! -e "$W/evil/src" ]' "a symlinked base folder was followed"
check '$H baseline $X src/guard.py >/dev/null 2>&1 && [ -f "$D/.light-harness-base/src/guard.py" ]' "baseline didn't recreate the base copies"
ok  $H verdict fail $X

echo "  rework: fix the class; an instance-only regression test is flagged"
ok  $H set-status Build $X
cp "$D/src/guard.py" "$W/v2.py"                  # gaming: keep the hole except while a probe runs
cat >> "$D/src/guard.py" <<'EOF'
import os
_blocked = blocked
def blocked(cmd):
    return _coarse(cmd) if os.environ.get("LIGHT_HARNESS_PROBE") or os.environ.get("LIGHT_HARNESS_BASELINE") else _blocked(cmd)
EOF
check '! $H verify $X > "$W/v.out" 2>&1 && grep -q "src/guard.py names the harness" "$W/v.out"' "code that detects a probe run passed verify"
cp "$W/v2.py" "$D/src/guard.py"                  # the same with the name built from pieces: no marker exists
cat >> "$D/src/guard.py" <<'EOF'
import os
_blocked = blocked
MARK = "LIGHT_" + "HARN" + "ESS_BASELINE"
def blocked(cmd):
    return _coarse(cmd) if any(os.environ.get(k) for k in (MARK, MARK.replace("BASELINE", "PROBE"))) else _blocked(cmd)
EOF
check '! $H verify $X > "$W/v.out" 2>&1 && grep -q "Saved probe: RV-1.1 (for RV-1) fails with exit 1: its defect class is still there" "$W/v.out"' "a probe run was told apart through its environment"
cat > "$D/src/guard.py" <<'EOF'
from src.words import WRITE
LOG = "logs/app.log"
def _coarse(cmd):
    return LOG in cmd and any(w in WRITE for w in cmd.split())
def blocked(cmd):
    """Exempt exactly the two read-only forms; anything else keeps the coarse rule."""
    w = cmd.split()
    if len(w) == 3 and w[0] == "grep" and w[2] == LOG:
        return False
    if len(w) == 3 and w[0] == "cp" and w[1] == LOG and w[2] != LOG:
        return False
    return _coarse(cmd)
EOF
printf 'from src.guard import blocked\ndef test_rv1_command_prefix():\n    """RV-1: a command prefix no longer lets cp through"""\n    assert blocked("command cp logs/app.log /tmp/b")\n' > "$D/tests/test_rv1.py"
check '$H probe run $X >/dev/null 2>&1' "probe run fails after the class fix"
check '$H verify $X > "$W/v.out" 2>&1 && grep -q "✓.*probe" "$W/v.out" && grep -q "⚠ Over-fit.*tests/test_rv1.py.*RV-1" "$W/v.out"' "verify after the fix: probe not passing, or the over-fit warning missing"
(cd "$D" && git add -A && git commit -qm "rework: keep the coarse rule for anything not exempt")
edit "$K" "| RV-1 | High | Open" "| RV-1 | High | Fixed"
ok  $H set-status Review $X
edit "$K" "| RV-1 | High | Fixed" "| RV-1 | High | Resolved" "$SEP
" "$SEP
| RV-2 | Medium | Open | src/guard.py | blocked before, allowed now: quoting | a quoted log path | BR-2 |
"
ok  $H verdict fail $X
ok  $H set-status Build $X
edit "$K" "| RV-2 | Medium | Open" "| RV-2 | Medium | Fixed"
ok  $H set-status Review $X
edit "$K" "| RV-2 | Medium | Fixed" "| RV-2 | Medium | Resolved" "- [ ] AC-1" "- [x] AC-1
- [x] AC-2"
ok  $H verdict pass $X
check '$H metrics guardfix --json | "$PYBIN" -c "import json,sys; k=json.load(sys.stdin); assert (k[\"review_rounds\"], k[\"same_class_reopened\"]) == (3, 1), k"' "metrics don't count the same class reopened"
check '$H metrics | grep -q "same class reopened"' "metrics summary lacks same class reopened"
check 'evq "any(e.get(\"command\")==\"verdict\" and e.get(\"same_class_reopened\")==1 for e in E)"' "verdict event lacks same_class_reopened"
nok $H probe drop RV-1.1 --name X --reason y $X </dev/null      # human only
check 'human "$H probe drop RV-1.1 --name \"Human Approver\" --reason \"checked by hand\" $X" "" && ! $H probe list $X | grep -q "RV-1.1" && $H log guardfix | grep -q "probe-dropped"' "a human couldn't drop a probe"

echo; echo "Self-test: $PASS passed, $FAIL failed"; [ "$FAIL" = 0 ]
