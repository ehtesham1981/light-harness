"""AC-6 (issue #20): the README's metrics docs describe the three new suite-run figures."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def section(text: str, title: str) -> str:
    lines = text.split("\n")
    start = next(i for i, l in enumerate(lines) if re.match(r"^#+\s+" + re.escape(title) + r"\s*$", l))
    level = len(lines[start]) - len(lines[start].lstrip("#"))
    end = next((i for i in range(start + 1, len(lines))
                if re.match(r"^#{1,%d}\s" % level, lines[i])), len(lines))
    return "\n".join(lines[start:end])


def test_ac6_readme_describes_figures():
    """AC-6 BR-3 BR-9 BR-2: the root README's "Telemetry and KPIs" section has a `suite` row in the events
    table and describes suite runs, cache hit rate and suite minutes, with --fresh/bypass kept out of the
    hit rate and reuse hits adding no runs or minutes."""
    sec = section((ROOT / "README.md").read_text(encoding="utf-8"), "Telemetry and KPIs")
    low = sec.lower()
    assert re.search(r"^\|\s*`suite`\s*\|", sec, re.M), "no `suite` row in the events table"
    for phrase in ("suite runs", "cache hit rate", "suite minutes", "bypass", "--fresh", "hit", "miss"):
        assert phrase in low, phrase
    for attr in ("`gate`", "`duration_s`", "`cache`"):
        assert attr in sec, attr


def test_ac6_plugin_readme_mentions_figures():
    """AC-6: the plugin README's summary of `metrics` mentions the three figures."""
    low = (ROOT / "plugins" / "light-harness" / "README.md").read_text(encoding="utf-8").lower()
    for phrase in ("suite runs", "cache hit rate", "suite minutes"):
        assert phrase in low, phrase
