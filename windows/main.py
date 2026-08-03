"""PySide6 entry point for the Code CCTV Windows app.

Run from the repo root: ``python -m windows.main`` (or ``python windows/main.py``).
Shows a tray icon plus a main window that mirrors the macOS global preview.
"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from main_window import MainWindow, MuteStore
from status_client import StatusClient
from system_tray import SystemTrayIcon


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Code CCTV General")
    app.setQuitOnLastWindowClosed(False)

    client = StatusClient()
    mutes = MuteStore()
    window = MainWindow(client, mutes)
    tray = SystemTrayIcon(app, on_show=window.show, client=client)

    def on_connection(connected: bool) -> None:
        active = sum(1 for p in client.state.get("projects", []) if p.get("active"))
        total = len(client.state.get("projects", []))
        tray.update_summary(connected, active, total)

    client.on_connection = on_connection
    client.start()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
