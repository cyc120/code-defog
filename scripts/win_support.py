#!/usr/bin/env python3
"""Windows service plumbing for Code CCTV: Task Scheduler, process detection
and .bat launchers. Shared by scripts/manage_service.py and
scripts/chatgpt_lifecycle.py on win32.

The Windows model mirrors macOS launchd: a single "CodeCCTV-lifecycle"
onlogon task keeps the lifecycle watcher running, and the watcher's sync()
starts/stops the "CodeCCTV" daemon task (a far-future one-shot that only ever
fires via explicit /run). All tasks are user-level (/rl limited), so no
administrator rights are needed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


TASK_NAME = "CodeCCTV"
LIFECYCLE_TASK_NAME = "CodeCCTV-lifecycle"


def data_dir() -> Path:
    """Same resolution as daemon/paths.data_dir() without importing the daemon
    package (this module must work from scripts that run standalone)."""
    if sys.platform != "win32":
        return Path.home() / "Library" / "Application Support" / "CodeCCTV"
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "CodeCCTV"
    return Path.home() / "AppData" / "Roaming" / "CodeCCTV"


def launcher_bat() -> Path:
    return data_dir() / "start_daemon.bat"


def lifecycle_bat() -> Path:
    return data_dir() / "start_lifecycle.bat"


def _python() -> str:
    return os.environ.get("CODE_CCTV_PYTHON", sys.executable)


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=False, text=True, capture_output=True)


def write_launcher_bat(repo_root: Path) -> Path:
    """Write the daemon .bat launcher. The .bat (not a python wrapper) keeps
    schtasks /tr to a single path with no embedded args; all working-directory
    and redirect concerns live in batch, where quoting is trivial."""
    target = launcher_bat()
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_bat(target, repo_root, ["-X", "utf8", "-m", "daemon.serve"],
               data_dir() / "daemon.log", data_dir() / "daemon.error.log")
    return target


def write_lifecycle_bat(repo_root: Path) -> Path:
    target = lifecycle_bat()
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_bat(target, repo_root,
               [str(repo_root / "scripts" / "chatgpt_lifecycle.py")],
               data_dir() / "lifecycle.log", data_dir() / "lifecycle.error.log")
    return target


def _write_bat(target: Path, repo_root: Path, module_args: list[str],
               stdout_path: Path, stderr_path: Path) -> None:
    python = _python()
    args = f'"{python}" {" ".join(module_args)}'
    content = (
        "@echo off\r\n"
        f'cd /d "{repo_root}"\r\n'
        f'{args} >> "{stdout_path}" 2>> "{stderr_path}"\r\n'
    )
    target.write_text(content, encoding="utf-8")


def remove_launcher_bats() -> None:
    for path in (launcher_bat(), lifecycle_bat()):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _schtasks(verb: str, name: str, *extra: str) -> subprocess.CompletedProcess[str]:
    return _run(["schtasks", verb, "/tn", name, *extra])


def task_exists(name: str = TASK_NAME) -> bool:
    return _run(["schtasks", "/query", "/tn", name]).returncode == 0


def task_running(name: str = TASK_NAME) -> bool:
    result = _run(["schtasks", "/query", "/tn", name, "/fo", "CSV", "/v"])
    return result.returncode == 0 and "Running" in result.stdout


def create_task(name: str, tr_value: str, onlogon: bool = True) -> None:
    if onlogon:
        result = _run(["schtasks", "/create", "/tn", name, "/tr", tr_value,
                       "/sc", "onlogon", "/rl", "limited", "/f"])
    else:
        # Far-future one-shot so the daemon never self-fires; it is started
        # only via explicit /run from sync.
        result = _run(["schtasks", "/create", "/tn", name, "/tr", tr_value,
                       "/sc", "once", "/st", "00:00", "/sd", "01/01/2099",
                       "/rl", "limited", "/f"])
    if result.returncode != 0 and not task_exists(name):
        raise SystemExit(f"schtasks /create {name} failed: {result.stderr.strip()}")


def start_task(name: str = TASK_NAME) -> None:
    _schtasks("/run", name)


def stop_task(name: str = TASK_NAME) -> None:
    _schtasks("/end", name)


def delete_task(name: str = TASK_NAME) -> None:
    if task_exists(name):
        _schtasks("/delete", name, "/f")


def chatgpt_running() -> bool:
    """Detect the ChatGPT desktop app: probe the process table, then fall back
    to known install paths (the process may run under another image name).
    CODE_CCTV_WATCH_EXE overrides the executable to follow."""
    exe = os.environ.get("CODE_CCTV_WATCH_EXE")
    names = ["ChatGPT.exe"]
    if exe:
        names.append(Path(exe).name)
    for name in names:
        if _process_running(name):
            return True

    local = os.environ.get("LOCALAPPDATA")
    candidates: list[Path] = []
    if local:
        candidates.append(Path(local) / "Programs" / "OpenAI")
        candidates.append(Path(local) / "OpenAI")
    if exe:
        candidates.append(Path(exe).parent)
    return any((base / "ChatGPT.exe").exists() for base in candidates)


def _process_running(image_name: str) -> bool:
    result = _run(["tasklist", "/FI", f"IMAGENAME eq {image_name}"])
    # tasklist prints a header row containing the image name only when matched.
    return image_name in result.stdout
