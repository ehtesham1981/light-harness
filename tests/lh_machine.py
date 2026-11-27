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


def relink_worktrees(main: Path, worktrees, env: dict | None = None) -> None:
    """Re-link linked worktrees copied (with their main checkout) from another folder, so the copy is independent.

    A copied worktree's `.git` file still names the original's admin folder `<orig>/.git/worktrees/<name>`, which
    exists, so `git worktree repair` alone does the wrong thing: it rewrites the ORIGINAL admin folder's `gitdir`
    to point at the copy and leaves the copy using the original's common dir (its refs, index and harness state).
    Point both links at the copy first, let git check them, then assert the worktree resolves to the copy."""
    import subprocess

    main = Path(main)
    common = main / ".git"
    wts = [Path(w) for w in worktrees]

    def git(*args, cwd):
        r = subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True, encoding="utf-8",
                           errors="replace")
        assert r.returncode == 0, f"git {' '.join(args)} failed in {cwd}:\n{r.stdout}{r.stderr}"
        return r.stdout

    for wt in wts:
        link = wt / ".git"
        text = link.read_text(encoding="utf-8").strip()
        assert text.startswith("gitdir:"), f"{link} is not a worktree link: {text!r}"
        name = Path(text[len("gitdir:"):].strip().replace("\\", "/")).name
        admin = common / "worktrees" / name
        assert admin.is_dir(), f"no worktree admin folder {admin}"
        link.write_text(f"gitdir: {admin.as_posix()}\n", encoding="utf-8")
        (admin / "gitdir").write_text(f"{link.as_posix()}\n", encoding="utf-8")
    if wts:
        git("worktree", "repair", *[str(w) for w in wts], cwd=main)
    want = Path(os.path.realpath(common))
    for wt in wts:
        got = git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=wt).strip()
        assert Path(os.path.realpath(got)) == want, f"worktree {wt} not re-linked: common dir {got}, want {want}"
        top = git("rev-parse", "--show-toplevel", cwd=wt).strip()
        assert Path(os.path.realpath(top)) == Path(os.path.realpath(wt)), f"worktree {wt} resolves to {top}"
