# Copyright 2026 Ehtesham Ahmed. Part of Light Harness (see NOTICE).
# SPDX-License-Identifier: Apache-2.0
"""Keep the harness's machine-wide state inside pytest's temp folder (issues #16, #25, #30).

The harness keeps machine-wide state outside any repo: run slots in a per-user cache folder, the slot
count in the user-level git config (`lightharness.slots`), and per-run temp folders under a short temp
root. Tests must never touch the real ones, so every test that runs harness code points them at a
throwaway folder:

- `machine_env(tmp, slots)` returns the environment overrides for a child process: HOME, USERPROFILE,
  LOCALAPPDATA and XDG_CACHE_HOME (where the slot folder is found), GIT_CONFIG_GLOBAL (where the slot
  count is read) and LIGHT_HARNESS_TEMP_ROOT (the per-run temp root; the harness's one test override).
- `isolate(monkeypatch, tmp, slots)` applies the same overrides to the current process.

This is a plain module, not conftest.py (a protected file), imported by the tests that need it.
"""
from __future__ import annotations

import os
from pathlib import Path

TEMP_ROOT_ENV = "LIGHT_HARNESS_TEMP_ROOT"
GITCONFIG_BASE = ("[user]\n\tname = Self Test\n\temail = t@t\n[core]\n\tautocrlf = false\n"
                  "[init]\n\tdefaultBranch = main\n")


def machine_paths(tmp: Path) -> dict:
    """The throwaway folders under `tmp` that stand in for the user's machine-wide ones."""
    tmp = Path(tmp)
    return {
        "home": tmp / "home",
        "localappdata": tmp / "home" / "AppData" / "Local",
        "xdg_cache": tmp / "home" / ".cache",
        "gitconfig": tmp / "home" / "gitconfig",
        "temp_root": tmp / "lh-tmp",
    }


def write_gitconfig(path: Path, slots=None, base: str = GITCONFIG_BASE) -> Path:
    """A git config file with the test identity and, when given, `lightharness.slots`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = base
    if slots is not None:
        text += f"[lightharness]\n\tslots = {slots}\n"
    path.write_text(text, encoding="utf-8")
    return path


def machine_env(tmp: Path, slots=None, gitconfig: Path | None = None) -> dict:
    """Environment overrides that keep a harness child's slots, slot count and temp root under `tmp`.

    `gitconfig`: an existing git config file to use as the user-level config (its `lightharness.slots`
    is then whatever it holds); otherwise one is written under `tmp` with `slots` when given."""
    p = machine_paths(tmp)
    for key in ("home", "localappdata", "xdg_cache", "temp_root"):
        p[key].mkdir(parents=True, exist_ok=True)
    if gitconfig is None:
        gitconfig = p["gitconfig"]
        if slots is not None or not gitconfig.exists():
            write_gitconfig(gitconfig, slots)
    return {
        "HOME": str(p["home"]),
        "USERPROFILE": str(p["home"]),
        "LOCALAPPDATA": str(p["localappdata"]),
        "XDG_CACHE_HOME": str(p["xdg_cache"]),
        "GIT_CONFIG_GLOBAL": str(gitconfig),
        TEMP_ROOT_ENV: str(p["temp_root"]),
    }


def isolate(monkeypatch, tmp: Path, slots=None, gitconfig: Path | None = None) -> dict:
    """Apply `machine_env` to this process (undone by monkeypatch after the test)."""
    env = machine_env(tmp, slots, gitconfig)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return env


def under(path, root) -> bool:
    try:
        Path(os.path.realpath(path)).relative_to(Path(os.path.realpath(root)))
        return True
    except ValueError:
        return False
