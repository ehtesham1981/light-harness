# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""Docs for slow suites and machine-wide runs (issues #16, #25, #30): AC-9 (BR-13) and the README."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

TASK_DIR = Path(__file__).resolve().parents[1]
PLUGIN = TASK_DIR / "plugins" / "light-harness"
GUIDES = ["skills/task-implement/SKILL.md", "skills/task-review/SKILL.md", "agents/reviewer.md"]


def _paragraphs(text: str) -> list:
    """Blank-line separated blocks, list items kept with their paragraph."""
    return [p for p in re.split(r"\n\s*\n", text) if p.strip()]


@pytest.mark.parametrize("doc", GUIDES)
def test_ac9_guides_say_run_gates_in_background(doc):
    """AC-9 BR-13: the task-implement and task-review skills and the reviewer agent each have a passage that
    names both `redgate` and `verify`, tells the agent to run them in the background (`run_in_background`
    or "background") when the suite is slow, and gives the Bash `timeout` alternative."""
    text = (PLUGIN / doc).read_text(encoding="utf-8")
    hits = [p for p in _paragraphs(text)
            if re.search(r"\bredgate\b", p) and re.search(r"\bverify\b", p)
            and re.search(r"run_in_background|\bbackground\b", p, re.I)
            and re.search(r"\btimeout\b", p, re.I)]
    assert hits, f"{doc}: no passage tells agents to run redgate/verify in the background or with a long Bash timeout"
    assert any(re.search(r"\bslow\b|minutes?\b|\bmin\b|\blong\b", p, re.I) for p in hits), hits[0][:300]


@pytest.mark.parametrize("term", ["suite_timeout", "{concurrency}", "lightharness.slots", "LIGHT_HARNESS_TEMP_ROOT",
                                  "124"])
def test_ac9_readme_documents_settings(term):
    """AC-9 AC-1 AC-3 AC-4 AC-5: README.md documents `suite_timeout` (and exit 124), `{concurrency}`,
    `lightharness.slots` and the temp-root override LIGHT_HARNESS_TEMP_ROOT."""
    text = (TASK_DIR / "README.md").read_text(encoding="utf-8")
    assert term in text, f"README.md doesn't mention {term}"
