#!/usr/bin/env python3
"""Install and control the Code CCTV launchd user agent."""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.code-cctv.daemon"
DESKTOP_LABEL = "com.code-cctv.floating"
LEGACY_DESKTOP_LABEL = "com.code-cctv.desktop"
LIFECYCLE_LABEL = "com.code-cctv.lifecycle"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
APP_SUPPORT = Path.home() / "Library" / "Application Support" / "CodeCCTV"
PLIST_PATH = LAUNCH_AGENTS / f"{LABEL}.plist"
DESKTOP_PLIST_PATH = LAUNCH_AGENTS / f"{DESKTOP_LABEL}.plist"
LEGACY_DESKTOP_PLIST_PATH = LAUNCH_AGENTS / f"{LEGACY_DESKTOP_LABEL}.plist"
LIFECYCLE_PLIST_PATH = LAUNCH_AGENTS / f"{LIFECYCLE_LABEL}.plist"
APP_PATH = PLUGIN_ROOT / "dist" / "CodeCCTV.app"
APP_BINARY = APP_PATH / "Contents" / "MacOS" / "CodeCCTV"
LIFECYCLE_SCRIPT = PLUGIN_ROOT / "scripts" / "chatgpt_lifecycle.py"


def domain() -> str:
    return f"gui/{os.getuid()}"


def run(command: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=check, text=True, capture_output=True)


def plist_payload() -> dict[str, object]:
    return {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "daemon.serve"],
        "WorkingDirectory": str(PLUGIN_ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Interactive",
        "ThrottleInterval": 5,
        "StandardOutPath": str(APP_SUPPORT / "daemon.log"),
        "StandardErrorPath": str(APP_SUPPORT / "daemon.error.log"),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }


def desktop_plist_payload() -> dict[str, object]:
    return {
        "Label": DESKTOP_LABEL,
        "ProgramArguments": [str(APP_BINARY)],
        "WorkingDirectory": str(PLUGIN_ROOT),
        "RunAtLoad": True,
        "ProcessType": "Interactive",
        "StandardOutPath": str(APP_SUPPORT / "desktop.log"),
        "StandardErrorPath": str(APP_SUPPORT / "desktop.error.log"),
    }


def lifecycle_plist_payload() -> dict[str, object]:
    return {
        "Label": LIFECYCLE_LABEL,
        "ProgramArguments": [sys.executable, str(LIFECYCLE_SCRIPT)],
        "WorkingDirectory": str(PLUGIN_ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Interactive",
        "StandardOutPath": str(APP_SUPPORT / "lifecycle.log"),
        "StandardErrorPath": str(APP_SUPPORT / "lifecycle.error.log"),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }


def loaded(label: str = LABEL) -> bool:
    result = run(["/bin/launchctl", "print", f"{domain()}/{label}"], check=False)
    return result.returncode == 0


def chatgpt_running() -> bool:
    result = run(["/bin/ps", "-axo", "command="], check=False)
    executable = "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"
    return any(
        command.strip() == executable or command.strip().startswith(f"{executable} ")
        for command in result.stdout.splitlines()
    )


def bootout(label: str = LABEL) -> None:
    run(["/bin/launchctl", "bootout", f"{domain()}/{label}"], check=False)
    deadline = time.monotonic() + 3
    while loaded(label) and time.monotonic() < deadline:
        time.sleep(0.1)


def remove_legacy_desktop_agent() -> None:
    bootout(LEGACY_DESKTOP_LABEL)
    if LEGACY_DESKTOP_PLIST_PATH.exists():
        LEGACY_DESKTOP_PLIST_PATH.unlink()


def write_plists() -> None:
    with PLIST_PATH.open("wb") as handle:
        plistlib.dump(plist_payload(), handle, sort_keys=False)
    with DESKTOP_PLIST_PATH.open("wb") as handle:
        plistlib.dump(desktop_plist_payload(), handle, sort_keys=False)
    with LIFECYCLE_PLIST_PATH.open("wb") as handle:
        plistlib.dump(lifecycle_plist_payload(), handle, sort_keys=False)


def bootstrap(label: str, plist_path: Path) -> None:
    result = run(["/bin/launchctl", "bootstrap", domain(), str(plist_path)], check=False)
    if result.returncode != 0 and not loaded(label):
        raise SystemExit(result.stderr.strip() or f"{label} launchctl bootstrap failed")


def start_children() -> None:
    for label, plist_path in (
        (LABEL, PLIST_PATH),
        (DESKTOP_LABEL, DESKTOP_PLIST_PATH),
    ):
        if not plist_path.exists():
            write_plists()
        if not loaded(label):
            bootstrap(label, plist_path)
        run(["/bin/launchctl", "kickstart", "-k", f"{domain()}/{label}"], check=False)


def stop_children() -> None:
    if loaded():
        bootout()
    if loaded(DESKTOP_LABEL):
        bootout(DESKTOP_LABEL)


def install() -> None:
    if not APP_PATH.exists():
        subprocess.run([str(PLUGIN_ROOT / "scripts" / "build_macos_app.sh")], check=True, text=True)
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    APP_SUPPORT.mkdir(parents=True, exist_ok=True)
    stop_children()
    remove_legacy_desktop_agent()
    if loaded(LIFECYCLE_LABEL):
        bootout(LIFECYCLE_LABEL)
    write_plists()
    bootstrap(LIFECYCLE_LABEL, LIFECYCLE_PLIST_PATH)
    sync()
    print(f"Installed {LIFECYCLE_LABEL}; child services follow ChatGPT")
    print(f"LaunchAgents: {LIFECYCLE_PLIST_PATH}, {PLIST_PATH}, {DESKTOP_PLIST_PATH}")


def uninstall() -> None:
    stop_children()
    if loaded(LIFECYCLE_LABEL):
        bootout(LIFECYCLE_LABEL)
    remove_legacy_desktop_agent()
    if PLIST_PATH.exists():
        PLIST_PATH.unlink()
    if DESKTOP_PLIST_PATH.exists():
        DESKTOP_PLIST_PATH.unlink()
    if LIFECYCLE_PLIST_PATH.exists():
        LIFECYCLE_PLIST_PATH.unlink()
    print(f"Uninstalled {LIFECYCLE_LABEL}, {LABEL} and {DESKTOP_LABEL}; local state remains in {APP_SUPPORT}")


def status() -> int:
    daemon_result = run(["/bin/launchctl", "print", f"{domain()}/{LABEL}"], check=False)
    desktop_result = run(["/bin/launchctl", "print", f"{domain()}/{DESKTOP_LABEL}"], check=False)
    legacy_result = run(["/bin/launchctl", "print", f"{domain()}/{LEGACY_DESKTOP_LABEL}"], check=False)
    lifecycle_result = run(["/bin/launchctl", "print", f"{domain()}/{LIFECYCLE_LABEL}"], check=False)
    chatgpt = chatgpt_running()
    print(f"{LABEL}: {'loaded' if daemon_result.returncode == 0 else 'not loaded'}")
    print(f"{DESKTOP_LABEL}: {'loaded' if desktop_result.returncode == 0 else 'not loaded'}")
    print(f"{LEGACY_DESKTOP_LABEL}: {'legacy loaded' if legacy_result.returncode == 0 else 'not loaded'}")
    print(f"{LIFECYCLE_LABEL}: {'loaded' if lifecycle_result.returncode == 0 else 'not loaded'}")
    print(f"ChatGPT: {'running' if chatgpt else 'not running'}")
    children_match = (daemon_result.returncode == 0 and desktop_result.returncode == 0) == chatgpt
    return 0 if lifecycle_result.returncode == 0 and children_match and legacy_result.returncode != 0 else 1


def start() -> None:
    if not loaded(LIFECYCLE_LABEL) or not LIFECYCLE_PLIST_PATH.exists():
        install()
        return
    sync()
    print(f"Synchronized Code CCTV with ChatGPT ({'running' if chatgpt_running() else 'not running'})")


def stop() -> None:
    stop_children()
    if loaded(LIFECYCLE_LABEL):
        bootout(LIFECYCLE_LABEL)
    print(f"Stopped {LIFECYCLE_LABEL}, {LABEL} and {DESKTOP_LABEL}")


def sync() -> None:
    if chatgpt_running():
        start_children()
    else:
        stop_children()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage the Code CCTV macOS background service.")
    parser.add_argument("action", choices=["install", "uninstall", "start", "stop", "sync", "status"])
    return parser.parse_args()


def main() -> None:
    if sys.platform != "darwin":
        raise SystemExit("Code CCTV desktop service requires macOS")
    action = parse_args().action
    if action == "install":
        install()
    elif action == "uninstall":
        uninstall()
    elif action == "start":
        start()
    elif action == "stop":
        stop()
    elif action == "sync":
        sync()
    else:
        raise SystemExit(status())


if __name__ == "__main__":
    main()
