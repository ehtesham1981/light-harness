#!/usr/bin/env python3
"""Install the one-off harness into a project (used by the plugin's /task-init).

Copies the contract template, team rules and harness script into the project without
overwriting anything, and merges the harness permission rules into .claude/settings.json
(plugins can't ship permission rules themselves). Safe to run repeatedly.

usage: python3 install.py <plugin_root> <project_dir>
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

UTF8 = "utf-8"  # never the locale default (cp1252 on Windows can't print ✓)
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding=UTF8, errors="replace")

plugin = Path(sys.argv[1]).resolve()
project = Path(sys.argv[2]).resolve()
tpl = plugin / "template"

copies = {
    "contract.md": "contract.md",
    ".harness/config.json": ".harness/config.json",
    "gitignore": ".gitignore",
    "env.example": ".env.example",
    "../scripts/harness.py": "scripts/harness.py",
}
for src, dst in copies.items():
    d = project / dst
    if d.exists():
        print(f"  kept     {dst}")
        continue
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(tpl / src, d)
    print(f"  created  {dst}")

# Harness rules: CLAUDE.md in a plugin isn't loaded, so add them to the project's CLAUDE.md
rules = (tpl / "CLAUDE.md").read_text(encoding=UTF8)
marker = "<!-- light-harness rules -->"
cm = project / "CLAUDE.md"
# surrogateescape round-trips any bytes in a user's existing CLAUDE.md that aren't valid UTF-8
existing = cm.read_text(encoding=UTF8, errors="surrogateescape") if cm.exists() else ""
if marker not in existing:
    cm.write_text((existing.rstrip() + "\n\n" if existing.strip() else "") + marker + "\n" + rules,
                  encoding=UTF8, errors="surrogateescape")
    print("  updated  CLAUDE.md (harness rules)")
else:
    print("  kept     CLAUDE.md")

# Merge permission rules
sp = project / ".claude" / "settings.json"
settings = json.loads(sp.read_text(encoding=UTF8)) if sp.exists() else {}
perms = settings.setdefault("permissions", {})
add = json.loads((tpl / "permissions.json").read_text(encoding=UTF8))
added = 0
for key, rules_ in add.items():
    cur = perms.setdefault(key, [])
    for r in rules_:
        if r not in cur:
            cur.append(r)
            added += 1
sp.parent.mkdir(parents=True, exist_ok=True)
sp.write_text(json.dumps(settings, indent=2) + "\n", encoding=UTF8)
print(f"  merged   .claude/settings.json (+{added} permission rules)")

if not (project / ".git").exists():
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    print("  created  git repository")
print("✓ harness installed")
