"""AC-4 / BR-6 (issue #23): outside the listed changes, `bash_rules` keeps its base-commit outcome for every
command in the BR-6 input space. The oracle is tests/golden/guard_base.json, written once by
tests/gen_guard_golden.py from the base-commit hook_guard.py; this test never reads .light-harness-base/."""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "plugins" / "light-harness" / "scripts"
for p in (str(SCRIPTS), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import hook_guard  # noqa: E402
import guard_scenarios as GS  # noqa: E402

GOLDEN = HERE / "golden" / "guard_base.json"


def golden():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_ac4_br6_sweep_golden_in_step():
    """AC-4 BR-6 [sweep]: (the sweep's oracle is in step) the golden has one base outcome per scenario, for exactly the commands scenarios() builds
    (R-5), in the compact Q-8 shape, and a few known base outcomes sit at the right index."""
    g = golden()
    scs = GS.scenarios()
    assert len(scs) == 3 * 27 * 7 * 3 * 8 == 13608
    assert len({sc["cmd"] for sc in scs}) == len(scs)
    assert g["count"] == len(scs) == len(g["outcomes"])
    assert g["commands_sha256"] == GS.commands_sha256(scs)
    assert set(g["outcomes"]) <= {"p", "a", "b:harness-files", "b:git-surgery"}
    at = {sc["cmd"]: g["outcomes"][i] for i, sc in enumerate(scs)}
    # Facts about the base guard, independent of the new code
    assert at["cat .light-harness/contract.md"] == "p"
    assert at["cat .light-harness/contract.md 2>/dev/null"] == "b:harness-files"
    assert at["sed -i '1,5p' .light-harness/contract.md"] == "b:harness-files"
    assert at["git checkout src/a.py"] == "b:git-surgery"
    assert at["git diff -- src/a.py"] == "a"
    assert at["frob src/a.py"] == "p"
    assert at["FOO=1 grep .light-harness/config.json"] == "b:harness-files"
    assert at["cd x && tee -n src/a.py > .light-harness/contract.md"] == "b:harness-files"


def test_ac4_br6_sweep_matches_base():
    """AC-4 BR-6 [sweep]: every command in the BR-6 cross product (prefixes x command words x flags x targets
    x suffixes, 13,608 commands, including an unknown word `frob` and flags the rule doesn't know) keeps its
    base outcome, except BR-6's allowed change: a listed read shape (`is_listed_read`) that the base blocked
    with `harness-files` must now be allowed or pass. Both directions are checked: base-blocked non-reads
    now allowed, and base-allowed commands now blocked."""
    g = golden()
    scs = GS.scenarios()
    assert g["count"] == len(scs)
    diffs = []
    for i, sc in enumerate(scs):
        base = g["outcomes"][i]
        now = GS.code(GS.outcome(hook_guard, sc["cmd"]))
        if GS.is_listed_read(sc) and base == "b:harness-files":
            ok = now in ("p", "a")
            want = "p|a"
        else:
            ok = now == base
            want = base
        if not ok:
            diffs.append(f"{sc['cmd']!r}: base {base}, expected {want}, got {now}")
    print(f"BR-6 sweep: {len(diffs)} of {len(scs)} commands differ")
    assert not diffs, f"{len(diffs)} of {len(scs)} commands differ; first 15:\n" + "\n".join(diffs[:15])


def test_ac4_br6_sweep_has_listed_reads():
    """AC-4 BR-6 [sweep]: (the sweep exercises the allowed change) the allowed change is real in this input space; many listed reads were blocked at base
    (so the sweep exercises the new read path, not only unchanged outcomes)."""
    g = golden()
    scs = GS.scenarios()
    changed = [sc["cmd"] for i, sc in enumerate(scs)
               if GS.is_listed_read(sc) and g["outcomes"][i] == "b:harness-files"]
    assert len(changed) > 500
    assert "sed '1,5p' .light-harness/contract.md" in changed
    assert "cd x && git log -i inplace -- .light-harness/config.json | head" in changed
