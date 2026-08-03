#!/usr/bin/env python3
"""Keep Code CCTV child agents aligned with the ChatGPT desktop app."""

from __future__ import annotations

import subprocess
import sys
import time
import os
from pathlib import Path


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
