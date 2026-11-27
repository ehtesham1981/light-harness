# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""Issue #50: the human-only `amend-config` command, end to end. AC-1, AC-3 (verify's ⚠ for a raised
timeout), AC-4 (refusals and the guard), AC-5 (re-approval, verify's reference, cache), AC-7a (telemetry).

Every task here is a copy of a snapshot from tests/amend_lab.py: a toy task whose approved test_cmd is
`node --test`, driven through a child that fakes `node` (no real node is started). `amend-config` runs with
`require_human` no-op'd (only inside pytest's temp folder) and the `AMEND` answer on stdin, except where the
test is that a non-terminal caller is refused (exit 3). "Records nothing" means the task's audit-trail ref
and the contract are byte-identical before and after.
"""
from __future__ import annotations

import json
import subprocess
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "plugins" / "light-harness" / "scripts"
for p in (str(SCRIPTS), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import amend_lab as AL  # noqa: E402
import guard_scenarios as GS  # noqa: E402
import harness as H  # noqa: E402
import hook_guard  # noqa: E402
import telemetry  # noqa: E402

NAME = "Pat Human"
REASON = "suite is flaky at full parallelism"
CHANGED_MSG = "config.json changed after the plan was approved"
HUMAN_ONLY_MSG = ("approve, unlock-tests, forget, uninstall, telemetry opt-in/opt-out and probe drop are human-only. "
                  "Ask the human to run it in their own terminal.")


@pytest.fixture(scope="module")
def snaps(tmp_path_factory):
    return AL.chain(tmp_path_factory.getbasetemp())


@pytest.fixture
def make(snaps, tmp_path, tmp_path_factory):
    n = [0]

    def make(name: str = "green") -> AL.Lab:
        n[0] += 1
        return AL.clone(snaps[name], tmp_path / f"lab{n[0]}", tmp_path_factory.getbasetemp())
    return make


def _gate_lines(lab: AL.Lab) -> tuple:
    out = lab.must("status")
    appr = re.search(r"^Approval:\s*(.*)$", out, re.M).group(1)
    rg = re.search(r"^Red gate:\s*(.*)$", out, re.M).group(1)
    return appr, rg


def _amend_rows(lab: AL.Lab) -> list:
    return [(sha, s) for sha, s in lab.trail_rows() if s.startswith(H.AMEND_EVENT)]


def assert_refused(lab: AL.Lab, rc: int, out: str, before_ref: str, before_contract: str, *words: str):
    """Non-zero exit, nothing recorded (audit ref and contract unchanged), each word in the output."""
    assert rc != 0, f"expected a refusal:\n{out}"
    assert lab.ref_sha() == before_ref, f"the audit trail changed on a refusal:\n{out}"
    assert lab.text() == before_contract, f"the contract changed on a refusal:\n{out}"
    assert "not implemented" not in out, out
    low = out.lower()
    for w in words:
        assert w.lower() in low, f"{w!r} not in the refusal:\n{out}"


# --- AC-1 ------------------------------------------------------------------------------------------------
def test_ac1_amend_concurrency_end_to_end(make):
    """AC-1 BR-1 BR-2 BR-5 BR-6 BR-7: in Build, a human adds `--test-concurrency={concurrency}` to a node
    test_cmd; verify fails anti-gaming before; `amend-config` records one `harness: config-amended by NAME`
    event naming the change and the reason, carrying the new config.json (the checked bytes) and
    config-amend.json; the approval block gets the appended note; verify then passes its config check
    (running `node --test --test-concurrency=<n>`), with approval and red gate unchanged and no new
    plan-approved or red-gate event."""
    lab = make("green")
    gates_before = _gate_lines(lab)
    approval_before = lab.trail_rows()
    lab.change_cfg(test_cmd=AL.NODE_CONC)
    live_bytes = lab.config.read_text(encoding="utf-8")
    rc, out = lab.drive("verify")
    assert rc == 1 and CHANGED_MSG in out, out

    rc, out = lab.amend(NAME, REASON)
    assert rc == 0, out
    rows = _amend_rows(lab)
    assert len(rows) == 1, lab.subjects()
    sha, subject = rows[0]
    assert subject.startswith(f"harness: config-amended by {NAME}"), subject
    assert "--test-concurrency={concurrency}" in subject and REASON in subject, subject
    assert lab.subjects()[0] == subject, "the amendment is not the newest event"
    assert lab.trail_file(sha, "config.json").replace("\r\n", "\n") == live_bytes.replace("\r\n", "\n")
    meta = json.loads(lab.trail_file(sha, "config-amend.json"))
    assert meta["name"] == NAME and meta["reason"] == REASON and meta.get("when"), meta
    assert any("--test-concurrency={concurrency}" in c for c in meta["changes"]), meta
    assert meta["raised_timeouts"] == [], meta

    status = lab.status_out()
    assert [l for l in status.splitlines() if f"config-amended by {NAME}" in l], status

    approval = lab.block("approval")
    assert "**Approved by:** Human Approver" in approval, approval
    assert f"**Config amended** by {NAME}" in approval and REASON in approval, approval

    rc, out = lab.drive("verify")
    assert rc == 0 and "**Result: PASS**" in out, out
    assert CHANGED_MSG not in out
    # the suite ran with the amended command (in this verify or the one before the amendment: same key)
    last = lab.suite_argvs()[-1]
    assert last[:2] == ["node", "--test"] and re.fullmatch(r"--test-concurrency=[1-9][0-9]*", last[2]), last
    assert not [l for l in out.splitlines() if "lightharness.slots" in l], "warning despite {concurrency}"
    assert _gate_lines(lab) == gates_before
    after = [s for _, s in lab.trail_rows()]
    for prefix in ("harness: plan-approved", "harness: red-gate"):
        assert [s for s in after if s.startswith(prefix)] == [s for _, s in approval_before if s.startswith(prefix)]


def test_ac1_amend_in_review(make):
    """AC-1 BR-1: amend-config also runs in Review (approval valid), and `verify` there passes afterwards."""
    lab = make("green")
    lab.must("set-status", "Review")
    lab.change_cfg(parallel=2)
    rc, out = lab.amend()
    assert rc == 0, out
    assert len(_amend_rows(lab)) == 1
    rc, out = lab.drive("verify")
    assert rc == 0 and CHANGED_MSG not in out, out


# --- AC-3 (verify ⚠) ----------------------------------------------------------------------------------------
def test_ac3_verify_flags_raised_timeout(make):
    """AC-3 BR-9: after a probe_timeout raise (600 -> 1200) is amended, verify passes and has a ⚠ line naming
    the raised timeout and the reviewer; config-amend.json lists it under raised_timeouts."""
    lab = make("green")
    lab.change_cfg(probe_timeout=1200)
    rc, out = lab.amend()
    assert rc == 0, out
    sha, subject = _amend_rows(lab)[0]
    assert "probe_timeout" in subject and "1200" in subject, subject
    meta = json.loads(lab.trail_file(sha, "config-amend.json"))
    assert any("probe_timeout" in r for r in meta["raised_timeouts"]), meta
    rc, out = lab.drive("verify")
    assert rc == 0, out
    lines = [l for l in out.splitlines() if l.startswith("- ⚠") and "probe_timeout" in l]
    assert lines, out
    assert "600" in lines[0] and "1200" in lines[0] and "reviewer" in lines[0].lower(), lines


def test_ac3_no_raised_line_for_concurrency_or_lower(make):
    """AC-3 BR-9: a concurrency change plus a lowered timeout gives no raised-timeout ⚠ line."""
    lab = make("green")
    lab.change_cfg(test_cmd=AL.NODE_CONC, suite_timeout=1800)
    rc, out = lab.amend()
    assert rc == 0, out
    rc, out = lab.drive("verify")
    assert rc == 0, out
    assert not [l for l in out.splitlines() if "raised" in l.lower() and "reviewer" in l.lower()], out
    assert not _timeout_warn_lines(out), out


def test_ac3_raise_over_cap_refused_end_to_end(make):
    """AC-3 AC-4 BR-4.3: probe_timeout 600 -> 1801 is refused with `re-approval`, recording nothing."""
    lab = make("green")
    ref, text = lab.ref_sha(), lab.text()
    lab.change_cfg(probe_timeout=1801)
    rc, out = lab.amend()
    assert_refused(lab, rc, out, ref, text, "probe_timeout", "re-approval")


def _timeout_warn_lines(out: str) -> list:
    return [l for l in out.splitlines() if l.startswith("- ⚠") and ("probe_timeout" in l or "suite_timeout" in l)]


def test_ac3_chained_amendments_capped_by_plan(make):
    """AC-3 BR-4.3 BR-5 BR-9: amendments stack (each note appended to the approval block); a later amendment
    that changes only `parallel` keeps verify's ⚠ for the still-raised `probe_timeout 600 → 1800`; amending
    probe_timeout back to 600 removes the ⚠; no amendment can raise probe_timeout beyond 3x the plan-approved
    value although it is within 3x of an earlier amendment."""
    lab = make("green")
    lab.change_cfg(probe_timeout=1800)
    assert lab.amend(NAME, "first")[0] == 0
    lab.change_cfg(parallel=2)
    rc, out = lab.amend("Sam Human", "second")
    assert rc == 0, out
    approval = lab.block("approval")
    assert f"**Config amended** by {NAME}" in approval and "**Config amended** by Sam Human" in approval, approval
    assert approval.index(NAME) < approval.index("Sam Human")
    rc, out = lab.drive("verify")
    assert rc == 0, out
    lines = _timeout_warn_lines(out)
    assert any("probe_timeout 600 → 1800" in l for l in lines), out
    ref, text = lab.ref_sha(), lab.text()
    lab.change_cfg(probe_timeout=2400)
    rc, out = lab.amend("Sam Human", "third")
    assert_refused(lab, rc, out, ref, text, "probe_timeout", "re-approval")
    lab.change_cfg(probe_timeout=600)
    rc, out = lab.amend("Sam Human", "back to the approved timeout")
    assert rc == 0, out
    rc, out = lab.drive("verify")
    assert rc == 0, out
    assert not _timeout_warn_lines(out), out


# --- AC-4: refusals -----------------------------------------------------------------------------------------
def test_ac4_not_a_terminal_exit_3(make):
    """AC-4 BR-1: run by an agent (stdin/stdout not a terminal, no patches) amend-config exits 3, even with
    AMEND piped in, and records nothing."""
    lab = make("green")
    ref, text = lab.ref_sha(), lab.text()
    lab.change_cfg(parallel=2)
    rc, out = lab.real("amend-config", "--name", NAME, "--reason", REASON, stdin="AMEND\n")
    assert rc == 3, out
    assert "interactive terminal" in out
    assert lab.ref_sha() == ref and lab.text() == text


def test_ac4_refused_in_plan(make):
    """AC-4 BR-1: in Plan (approved) it refuses and says to edit config.json and run `approve` again."""
    lab = make("plan")
    ref, text = lab.ref_sha(), lab.text()
    lab.change_cfg(parallel=2)
    rc, out = lab.amend()
    assert_refused(lab, rc, out, ref, text, "config.json")
    assert "`approve`" in out or "approve again" in out.lower(), out


def test_ac4_refused_in_initialize(make):
    """AC-4 BR-1: before Plan (Initialize: nothing approved yet for this plan round) it refuses."""
    lab = make("plan")
    lab.must("set-status", "Initialize")
    ref, text = lab.ref_sha(), lab.text()
    lab.change_cfg(parallel=2)
    rc, out = lab.amend()
    assert_refused(lab, rc, out, ref, text, "Initialize")


def test_ac4_refused_in_draft(make):
    """AC-4 BR-1: in Draft (a contract whose status line says Draft) it refuses."""
    lab = make("plan")
    AL.T.write(lab.contract, re.sub(r"^Status:\s*\S+", "Status: Draft", lab.text(), count=1, flags=re.M))
    ref, text = lab.ref_sha(), lab.text()
    lab.change_cfg(parallel=2)
    rc, out = lab.amend()
    assert_refused(lab, rc, out, ref, text, "Draft")


def test_ac1_amend_in_rework(make):
    """AC-1 BR-1: amend-config runs in Rework (after `verdict fail`, approval still valid)."""
    lab = make("green")
    lab.must("set-status", "Review")
    lab.must("verdict", "fail")
    assert lab.status() == "Rework"
    lab.change_cfg(parallel=2)
    rc, out = lab.amend()
    assert rc == 0, out
    assert len(_amend_rows(lab)) == 1


def test_ac4_refused_in_plan_before_approval(make):
    """AC-4 BR-1: in Plan with no valid approval (the plan edited after approving) it refuses."""
    lab = make("plan")
    AL.T.edit(lab.contract, "Triple numbers", "Triple numbers quickly")
    ref, text = lab.ref_sha(), lab.text()
    lab.change_cfg(parallel=2)
    rc, out = lab.amend()
    assert_refused(lab, rc, out, ref, text, "approv")


def test_ac4_refused_without_valid_approval_in_build(make):
    """AC-4 BR-1: in Build, when the plan changed after approval (approval_state not ok), it refuses."""
    lab = make("green")
    AL.T.edit(lab.contract, "Triple numbers", "Triple numbers quickly")
    ref, text = lab.ref_sha(), lab.text()
    lab.change_cfg(parallel=2)
    rc, out = lab.amend()
    assert_refused(lab, rc, out, ref, text, "approv")


@pytest.mark.parametrize("how", ["unchanged", "reformatted"])
def test_ac4_nothing_to_amend(make, how):
    """AC-4 BR-3: a live config that parses to the same JSON value as the effective approved one (unchanged,
    or only re-indented) is refused with `nothing to amend`."""
    lab = make("green")
    ref, text = lab.ref_sha(), lab.text()
    if how == "reformatted":
        AL.T.write(lab.config, json.dumps(lab.cfg(), indent=4, sort_keys=True) + "\n\n")
    rc, out = lab.amend()
    assert_refused(lab, rc, out, ref, text, "nothing to amend")


@pytest.mark.parametrize("answer", ["", "amend", "yes", "AMEN"])
def test_ac4_not_typing_amend(make, answer):
    """AC-4 BR-1: the prompt asks the human to type AMEND; any other answer refuses and records nothing."""
    lab = make("green")
    ref, text = lab.ref_sha(), lab.text()
    lab.change_cfg(parallel=2)
    rc, out = lab.amend(answer=answer)
    assert_refused(lab, rc, out, ref, text, "not amended")


@pytest.mark.parametrize("kind", ["missing", "invalid-json", "array", "string", "number"])
def test_ac4_live_config_unreadable(make, kind):
    """AC-4 BR-2a: a missing live config.json, one that isn't valid JSON, or one that isn't a JSON object is
    refused, naming config.json."""
    lab = make("green")
    ref, text = lab.ref_sha(), lab.text()
    if kind == "missing":
        lab.config.unlink()
    else:
        AL.T.write(lab.config, {"invalid-json": '{"test_cmd": ["node", "--test"],', "array": "[1, 2]\n",
                                "string": '"node --test"\n', "number": "3\n"}[kind])
    rc, out = lab.amend()
    assert_refused(lab, rc, out, ref, text, "config.json")


@pytest.mark.parametrize("field,value", [
    ("name", "Pat\nHuman"), ("name", "Pat <!-- x"), ("name", "Pat --> x"),
    ("reason", "flaky\nsuite"), ("reason", "flaky <!-- hidden"), ("reason", "flaky --> out"),
    ("reason", ""), ("reason", "   "),
], ids=["name-newline", "name-open", "name-close", "reason-newline", "reason-open", "reason-close",
        "reason-empty", "reason-blank"])
def test_ac4_bad_name_or_reason(make, field, value):
    """AC-4 BR-1 BR-5: --name / --reason with a newline, `<!--` or `-->`, and an empty --reason, are refused
    (naming the option) and record nothing."""
    lab = make("green")
    ref, text = lab.ref_sha(), lab.text()
    lab.change_cfg(parallel=2)
    kw = {"name": NAME, "reason": REASON, field: value}
    rc, out = lab.amend(**kw)
    assert_refused(lab, rc, out, ref, text, field)


def test_ac4_config_changed_while_waiting(make, tmp_path):
    """AC-4 BR-5: if config.json changes while the AMEND prompt waits, the command refuses (`changed`) and
    records nothing, although the answer is AMEND."""
    lab = make("green")
    ref, text = lab.ref_sha(), lab.text()
    lab.change_cfg(parallel=2)
    other = dict(lab.cfg(), parallel=1)
    swap = tmp_path / "swapped.json"
    swap.write_text(json.dumps(other, indent=2) + "\n", encoding="utf-8")
    rc, out = lab.amend(swap=swap)
    assert_refused(lab, rc, out, ref, text, "changed")
    assert lab.cfg()["parallel"] == 1, "the swap did not happen (the prompt was never reached)"


def test_ac4_config_reindented_while_waiting(make, tmp_path):
    """AC-4 BR-5: a swap during the prompt to the same JSON value with different bytes (re-indented) is still
    a change of the checked bytes: refused (`changed`), nothing recorded."""
    lab = make("green")
    ref, text = lab.ref_sha(), lab.text()
    cfg = lab.change_cfg(parallel=2)
    swap = tmp_path / "swapped.json"
    swap.write_text(json.dumps(cfg, indent=4, sort_keys=True) + "\n", encoding="utf-8")
    rc, out = lab.amend(swap=swap)
    assert_refused(lab, rc, out, ref, text, "changed")


def test_ac1_recorded_config_is_the_checked_bytes(make, tmp_path):
    """AC-1 AC-4 BR-5: the config.json in the config-amended event is the bytes that were checked, even if
    the file changes between the check and the write of the event (simulated just before record())."""
    lab = make("green")
    lab.change_cfg(parallel=2)
    checked = lab.config.read_bytes()
    swap = tmp_path / "late.json"
    swap.write_text(json.dumps(dict(lab.cfg(), parallel=1, red_exit_codes=[1, 2])) + "\n", encoding="utf-8")
    rc, out = lab.amend(swap_at_record=swap)
    assert lab.config.read_bytes() == swap.read_bytes(), "the late swap did not happen"
    assert rc == 0, out
    rows = _amend_rows(lab)
    assert len(rows) == 1, rows
    recorded = lab.trail_file(rows[0][0], "config.json")
    assert recorded is not None, "no config.json in the event"
    assert recorded == checked.decode("utf-8").replace("\r\n", "\n"), recorded


@pytest.mark.parametrize("field,change", [
    ("test_globs", {"test_globs": ["tests/**", "spec/**"]}),
    ("protected_globs", {"protected_globs": []}),
    ("red_exit_codes", {"red_exit_codes": [1, 2]}),
    ("test_cmd", {"test_cmd": ["node", "--test", "--test-name-pattern", "fast"]}),
    ("test_cmd", {"test_cmd": ["node", "--test", "--test-concurrency=0"]}),
    ("parallel", {"parallel": 9}),
    ("worktree_dir", {"worktree_dir": ".wt", "parallel": 2}),
    ("test_cmd", {"test_cmd": "node --test --test-concurrency=2"}),
    ("test_cmd", {"test_cmd": ["node", "--test", 2]}),
], ids=["test_globs", "protected_globs", "red_exit_codes", "test_cmd-filter", "test_cmd-zero", "parallel-9",
        "worktree_dir-with-allowed", "test_cmd-string", "test_cmd-int-item"])
def test_ac4_refused_field_named(make, field, change):
    """AC-2 AC-4 BR-4: a change outside the allowed ones is refused end to end, naming the field and pointing
    back at Plan / re-approval; nothing is recorded."""
    lab = make("green")
    ref, text = lab.ref_sha(), lab.text()
    lab.change_cfg(**change)
    rc, out = lab.amend()
    assert_refused(lab, rc, out, ref, text, field, "approv")


def test_ac4_approved_snapshot_without_config(make):
    """AC-4 BR-2a: when the plan-approved snapshot holds no config.json (an audit trail written before the
    task had one; made here by re-writing that event's tree without it), amend-config refuses, naming
    config.json, and records nothing."""
    lab = make("plan")
    newest_sha, newest = lab.trail_rows()[0]
    assert newest.startswith("harness: plan-approved"), newest
    lab.drop_from_newest_event("config.json")
    assert lab.trail_file(lab.ref_sha(), "config.json") is None
    lab.must("set-status", "Build")
    lab.change_cfg(parallel=2)
    ref, text = lab.ref_sha(), lab.text()
    rc, out = lab.amend()
    assert_refused(lab, rc, out, ref, text, "config.json")


@pytest.mark.parametrize("content", ["[1, 2]\n", '"node --test"\n', "null\n", "{not json\n"],
                         ids=["array", "string", "null", "invalid"])
def test_ac4_approved_snapshot_config_not_object(make, content):
    """AC-4 BR-2a: when the plan-approved snapshot's config.json is not a JSON object (or not JSON), amend-config
    refuses, naming config.json, and records nothing."""
    lab = make("plan")
    assert lab.trail_rows()[0][1].startswith("harness: plan-approved")
    lab.replace_in_newest_event("config.json", content)
    lab.must("set-status", "Build")
    lab.change_cfg(parallel=2)
    ref, text = lab.ref_sha(), lab.text()
    rc, out = lab.amend()
    assert_refused(lab, rc, out, ref, text, "config.json")


AGENT_CMDS = [
    "light-harness amend-config --task x --name A --reason b",
    "python3 /p/scripts/harness.py amend-config --name A --reason b",
    'python3 "$CLAUDE_PLUGIN_ROOT/scripts/harness.py" amend-config --task x --name "A B" --reason "c d"',
    "cd x && light-harness amend-config --name A --reason b",
    "sh /p/bin/light-harness amend-config --name A --reason b",
]


@pytest.mark.parametrize("cmd", AGENT_CMDS)
def test_ac4_guard_blocks_agent(cmd):
    """AC-4 BR-1 BR-11: the guard hook blocks `amend-config` through light-harness or harness.py from an
    agent with rule id `human-only`."""
    assert GS.outcome(hook_guard, cmd) == ["block", "human-only"], cmd


def test_ac4_guard_existing_human_only_message_unchanged():
    """AC-4 BR-11: the existing human-only BLOCK_BASH entry keeps its message (a golden embeds it), and
    amend-config is blocked by an entry of its own with the same rule id."""
    entries = [e for e in hook_guard.BLOCK_BASH if e[3] == "human-only"]
    orig = [e for e in entries if e[1] == HUMAN_ONLY_MSG]
    assert len(orig) == 1, entries
    cmd = "light-harness amend-config --name A --reason b"
    own = [e for e in entries if e is not orig[0] and re.search(e[0], cmd) and (e[2] is None or re.search(e[2], cmd))]
    assert own, "no BLOCK_BASH entry of its own for amend-config"


# --- AC-5 --------------------------------------------------------------------------------------------------
def test_ac5_reapproval_discards_amendments(make):
    """AC-5 BR-2 BR-6: after an amendment, going back to Plan and approving again makes the new plan-approved
    config the reference: a live config equal to the new approval passes verify's config check, one equal to
    the older amendment fails it."""
    lab = make("green")
    lab.change_cfg(probe_timeout=300)
    assert lab.amend()[0] == 0
    assert "config-amended by" in lab.status_out()
    amended = lab.config.read_text(encoding="utf-8")
    lab.must("set-status", "Plan")
    lab.change_cfg(probe_timeout=450)
    lab.must("approve", "--name", "Human Approver", human=True, stdin="APPROVE\n")
    status = lab.status_out()
    assert "config-amended" not in status, status
    lab.must("set-status", "Build")
    rc, out = lab.drive("verify")
    assert CHANGED_MSG not in out, out
    AL.T.write(lab.config, amended)
    rc, out = lab.drive("verify")
    assert rc == 1 and CHANGED_MSG in out, out


def test_ac5_live_differs_from_latest_amendment(make):
    """AC-5 BR-6: after an amendment, a live config that differs from it (here: the plan-approved config
    again, or a further unamended edit) fails verify's anti-gaming check; one equal to it passes."""
    lab = make("green")
    approved = lab.config.read_text(encoding="utf-8")
    lab.change_cfg(probe_timeout=300)
    assert lab.amend()[0] == 0
    amended = lab.config.read_text(encoding="utf-8")
    for text in (approved, json.dumps(dict(json.loads(amended), probe_timeout=200))):
        AL.T.write(lab.config, text)
        rc, out = lab.drive("verify")
        assert rc == 1 and CHANGED_MSG in out, out
    AL.T.write(lab.config, json.dumps(json.loads(amended), indent=4))      # same JSON value, other bytes
    rc, out = lab.drive("verify")
    assert rc == 0 and CHANGED_MSG not in out, out


def test_ac5_cached_pass_not_reused_after_amendment(make):
    """AC-5 BR-8: a verify pass stored before the amendment is not reused after it: the next verify runs the
    suite (and its output doesn't say `cached`)."""
    lab = make("green")
    rc, out = lab.drive("verify")
    assert rc == 0
    n = lab.suite_runs()
    rc, out = lab.drive("verify")
    assert rc == 0 and "cached" in out and lab.suite_runs() == n, out
    lab.change_cfg(parallel=2)
    assert lab.amend()[0] == 0
    rc, out = lab.drive("verify")
    assert rc == 0, out
    assert lab.suite_runs() == n + 1 and "cached" not in out, out


def test_ac5_verdict_pass_after_amendment(make):
    """AC-5 BR-7: after an amendment the task goes to Review and `verdict pass` works as before."""
    lab = make("green")
    lab.change_cfg(parallel=2)
    assert lab.amend()[0] == 0
    AL.T.edit(lab.contract, "- [ ] AC-1", "- [x] AC-1", "| RV-1 | Low | Open", "| RV-1 | Low | Resolved",
              "| RV-2 | Low | Open", "| RV-2 | Low | Resolved", "| RV-3 | Low | Open", "| RV-3 | Low | Resolved")
    lab.must("set-status", "Review")
    rc, out = lab.drive("verdict", "pass")
    assert rc == 0, out


# --- AC-7a: telemetry --------------------------------------------------------------------------------------
def test_ac7a_human_commands_in_step():
    """AC-7a BR-5: telemetry.HUMAN_COMMANDS holds `amend-config`, in step with harness HUMAN_ONLY."""
    assert "amend-config" in telemetry.HUMAN_COMMANDS
    assert "amend-config" in H.HUMAN_ONLY and "amend-config" in H.TRACKED
    assert H.HUMAN_ONLY <= telemetry.HUMAN_COMMANDS


def test_ac7a_command_event_human_without_name(make):
    """AC-7a BR-5: a successful amend-config (run from an agent-marked environment, CLAUDECODE=1) is one
    `command` event with actor "human" and ok true; the --name appears nowhere in any telemetry event."""
    lab = make("green")
    name = "Zelda Quixote-Unique"
    lab.change_cfg(parallel=2)
    rc, out = lab.amend(name, REASON, extra={"CLAUDECODE": "1"})
    assert rc == 0, out
    evs = lab.command_events("amend-config")
    assert len(evs) == 1, evs
    e, line = evs[0]
    assert e.get("actor") == "human" and e.get("ok") is True, e
    assert name not in lab.events.read_text(encoding="utf-8")
    assert "Zelda" not in line


# --- review CR-7 (Codex P1): an amendment must not move the code baseline -----------------------------------
def test_br6_amendment_keeps_plan_approved_code_baseline(make):
    """BR-6 BR-7 (review CR-7): an amendment changes only the config verify compares with. The anti-gaming and
    anti-drift scans still diff against the plan-approved snapshot, so an out-of-scope file committed before the
    amendment still fails verify afterwards (the same class as committing it after the amendment)."""
    lab = make("green")
    (lab.task / "outside_scope.txt").write_text("not in scope\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(lab.task), check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                    "commit", "-qm", "out of scope"], cwd=str(lab.task), check=True, capture_output=True)
    rc, out = lab.drive("verify")
    assert rc == 1 and "outside_scope.txt" in out, out
    lab.change_cfg(parallel=2)
    rc, out = lab.amend()
    assert rc == 0, out
    rc, out = lab.drive("verify")
    assert rc == 1, out
    assert "Anti-drift" in out and "outside_scope.txt" in out, out
    assert CHANGED_MSG not in out, out
