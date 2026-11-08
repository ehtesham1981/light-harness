#!/usr/bin/env bash
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
telhook() { echo "$2" | CLAUDE_PROJECT_DIR="$1" sh "$PLUGIN/scripts/telemetry.sh"; }
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
         "env -u LIGHT_HARNESS_SESSION_ID light-harness verify" "cat .git/light-harness/telemetry/events.jsonl"; do
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
echo x > "$A/rogue.txt"; nok $H verify $X; rm "$A/rogue.txt"            # out-of-scope change
cp "$A/.light-harness/config.json" "$W/cfg.json"; sed -i 's/"-q"/"-q", "-x"/' "$A/.light-harness/config.json"
nok $H verify $X                                 # config changed after approval
cp "$W/cfg.json" "$A/.light-harness/config.json"
ok  $H verify $X
(cd "$A" && git add -A && git commit -qm "step 1-2: dedupe")
ok  $H set-status Review $X
edit "$C" "|----|----------|--------|------|-------------|------------------|
" "|----|----------|--------|------|-------------|------------------|
| RV-1 | Medium | Open | src | R-1 mitigation missing | R-1 |
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
kill "$OTLP_PID" 2>/dev/null; wait "$OTLP_PID" 2>/dev/null
check 'grep -q "^/v1/logs qa .*resourceLogs.*\"session.id\".*sess-alpha" "$W/otlp.body"' "events not exported over OTLP"
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
flow() {
  local X="--task $1" D="$T/$1" K="$T/$1/.light-harness/contract.md"
  edit "$K" "<one or two sentences: what outcome, for whom>" "$1" \
    "| EX-1 | <input> | <expected> | <happy path / edge / error> |" "| EX-1 | 3 | 9 | happy |" \
    "- AC-1: <Given … When … Then …>" "- AC-1: Given x, then x squared"
  ok  $H set-status Initialize $X
  edit "$K" "| R-1 | <risk> | <L/M/H> | <L/M/H> | <mitigation> |" "| R-1 | none | L | L | n/a |" \
    "| <area> | <change> | <who/what is affected> | <how to undo> |" "| src | new | none | delete |" \
    "- <src/path/or/glob>" "- src/**
- tests/**" "1. <step> (AC-1)" "1. square (AC-1)"
  ok  $H set-status Plan $X
  check "human \"$H approve --name Approver $X\" APPROVE" "approve ($1)"
  ok  $H set-status Build $X
  mkdir -p "$D/src" "$D/tests"; touch "$D/src/__init__.py"
  printf 'from src.%s import f\ndef test_%s():\n    """AC-1"""\n    assert f(3) == 9\n' "$1" "$1" > "$D/tests/test_$1.py"
  printf 'def f(x):\n    raise NotImplementedError\n' > "$D/src/$1.py"
  ok  $H redgate $X
  printf 'def f(x):\n    return x * x\n' > "$D/src/$1.py"
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

echo; echo "Self-test: $PASS passed, $FAIL failed"; [ "$FAIL" = 0 ]
