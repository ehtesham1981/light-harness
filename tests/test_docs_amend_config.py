# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""Issue #50 AC-8: the docs and agent prompts describe `amend-config`, what `lightharness.slots` does versus
`{concurrency}`, the warning, and the reviewer's check of raised timeouts."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "light-harness"


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def paragraphs(text: str) -> list:
    """Blank-line separated blocks, and each list item on its own."""
    out = []
    for block in re.split(r"\n\s*\n", text):
        out.append(block)
        out += [l for l in block.splitlines() if re.match(r"\s*([-*+]|\d+[.)])\s", l)]
    return out


def test_ac8_readmes_mention_amend_config():
    """AC-8 BR-1 BR-5: the main README and the plugin README document the human-only `amend-config`."""
    for p in (ROOT / "README.md", PLUGIN / "README.md"):
        text = read(p)
        assert "amend-config" in text, p
    main = read(ROOT / "README.md")
    paras = [b for b in paragraphs(main) if "amend-config" in b]
    assert any(re.search(r"human", b, re.I) for b in paras), "README doesn't say amend-config is human-only"
    assert any("--reason" in b or "reason" in b.lower() for b in paras), "README doesn't mention the reason"


def test_ac8_readme_slots_vs_concurrency_and_warning():
    """AC-8 BR-10: the main README explains, in one place, that `lightharness.slots` limits suite runs sharing
    the machine while `{concurrency}` caps test files inside one run, and mentions the warning."""
    paras = paragraphs(read(ROOT / "README.md"))
    hits = [b for b in paras if "lightharness.slots" in b and "{concurrency}" in b and re.search(r"\bwarn", b, re.I)]
    assert hits, "no README paragraph explains lightharness.slots vs {concurrency} together with the warning"


def test_ac8_reviewer_checks_raised_timeouts():
    """AC-8 BR-9: the reviewer prompt tells the reviewer to check probes/tests that rely on a timeout raised
    by `amend-config`."""
    text = read(PLUGIN / "agents" / "reviewer.md")
    paras = [b for b in paragraphs(text) if re.search(r"timeout", b, re.I) and re.search(r"raised", b, re.I)]
    assert paras, "reviewer.md says nothing about raised timeouts"
    assert re.search(r"amend", "\n".join(paras), re.I), "reviewer.md doesn't tie the raised timeout to an amendment"


def test_ac8_explorer_asks_for_concurrency():
    """AC-8 BR-10: the explorer prompt asks for `{concurrency}` in test_cmd."""
    text = read(PLUGIN / "agents" / "explorer.md")
    assert "{concurrency}" in text and "test_cmd" in text


def test_ac8_rules_or_implement_skill_mention_amend_config():
    """AC-8 BR-1: rules.md or the task-implement skill tell the agent to ask the human to edit config.json and
    run `amend-config` when the suite only passes with capped concurrency."""
    texts = [read(PLUGIN / "rules.md"), read(PLUGIN / "skills" / "task-implement" / "SKILL.md")]
    hits = [b for t in texts for b in paragraphs(t) if "amend-config" in b]
    assert hits, "neither rules.md nor task-implement/SKILL.md mentions amend-config"
    assert any(re.search(r"human", b, re.I) for b in hits), hits
