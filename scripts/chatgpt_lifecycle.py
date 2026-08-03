#!/usr/bin/env python3
"""Keep Code CCTV child agents aligned with the ChatGPT desktop app.

Platform model: on macOS the watcher runs under launchd and drives launchd
children via manage_service.py sync; on Windows it runs as a Task Scheduler
onlogon task and drives the daemon task via the same sync() (which dispatches
per platform). The 2s poll and 15s uninstall grace are platform-neutral.
"""

from __future__ import annotations

import subprocess
import sys
import time
import os
from pathlib import Path

if sys.platform not in ("darwin", "win32"):
    raise SystemExit("Code CCTV lifecycle watcher supports macOS and Windows")

if sys.platform == "win32":
    from win_support import (  # noqa: E402
        TASK_NAME,
        delete_task,
        remove_launcher_bats,
    )
    from win_support import chatgpt_running as windows_chatgpt_running


ROOT = Path(__file__).resolve().parents[1]
MANAGER = ROOT / "scripts" / "manage_service.py"
CODEX_HOME = Path.home() / ".codex"
CACHE_ROOT = CODEX_HOME / "plugins" / "cache" / "personal" / "code-cctv"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
CHILD_LABELS = ("com.code-cctv.daemon", "com.code-cctv.floating")
LIFECYCLE_LABEL = "com.code-cctv.lifecycle"
POLL_INTERVAL = 2.0
PLUGIN_MISSING_GRACE = 15.0


def chatgpt_running() -> bool:
    if sys.platform == "win32":
        return windows_chatgpt_running()
    result = subprocess.run(
        ["/bin/ps", "-axo", "command="],
        check=False,
        capture_output=True,
        text=True,
    )
    executable = "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"
    return any(
        command.strip() == executable or command.strip().startswith(f"{executable} ")
        for command in result.stdout.splitlines()
    )


def plugin_installed() -> bool:
    if not ROOT.exists() or not CACHE_ROOT.is_dir():
        return False
    return any(CACHE_ROOT.iterdir())


def sync_services() -> None:
    subprocess.run(
        [sys.executable, str(MANAGER), "sync"],
        cwd=ROOT,
        check=False,
    )


def bootout(label: str) -> None:
    subprocess.run(
        ["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def cleanup_after_uninstall() -> None:
    if sys.platform == "win32":
        delete_task(TASK_NAME)
        delete_task("CodeCCTV-lifecycle")
        remove_launcher_bats()
        return
    for label in (*CHILD_LABELS, LIFECYCLE_LABEL):
        plist_path = LAUNCH_AGENTS / f"{label}.plist"
        try:
            plist_path.unlink()
        except FileNotFoundError:
            pass
    for label in (*CHILD_LABELS, LIFECYCLE_LABEL):
        bootout(label)


def main() -> None:
    previous: bool | None = None
    missing_since: float | None = None
    while True:
        if not plugin_installed():
            if missing_since is None:
                missing_since = time.monotonic()
            elif time.monotonic() - missing_since >= PLUGIN_MISSING_GRACE:
                cleanup_after_uninstall()
                return
        else:
            missing_since = None
        current = chatgpt_running()
        if current != previous:
            sync_services()
            previous = current
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
