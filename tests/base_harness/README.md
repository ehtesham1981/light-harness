# Base-commit harness (test oracle)

`harness.py` and `telemetry.py` here are the harness scripts at base commit `89b0318` (Light Harness
1.9.0), committed unchanged. They are the oracle for the BR-11 sweep of task `issue-25-16-30`
(issues #16, #25, #30): the verify reuse key's parts other than `version` must equal what this base
harness computes for the same task, whatever the per-run temp folder, slot count, core count or slot wait.

Exported with:

    light-harness baseline --task issue-25-16-30 --out <folder outside the repo> plugins/light-harness/scripts/harness.py

Don't edit these files. They are not product code and are never imported by the plugin.
