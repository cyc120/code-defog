"""System tray icon for the Windows app (mirrors the macOS menu-bar item)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon


def app_icon() -> QIcon:
    """Load the bundled Code CCTV logo; falls back to an empty icon."""
    logo = Path(__file__).resolve().parents[1] / "assets" / "code-cctv-logo.svg"
    if logo.exists():
        return QIcon(str(logo))
    return QIcon()


class SystemTrayIcon(QSystemTrayIcon):
    def __init__(self, app: QApplication, on_show: Any, client: Any) -> None:
        super().__init__(app_icon(), app)
        self._client = client
        self._menu = QMenu()
        show_action = QAction("显示窗口", self._menu)
        show_action.triggered.connect(on_show)
        self._menu.addAction(show_action)
        self._menu.addSeparator()
        quit_action = QAction("退出", self._menu)
        quit_action.triggered.connect(app.quit)
        self._menu.addAction(quit_action)
        self.setContextMenu(self._menu)
        self.activated.connect(self._on_activated)
        self.setToolTip("Code CCTV 未连接")
        self.show()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            # Double-click handled by the parent wiring via on_show.
            self._menu.show()

    def update_summary(self, connected: bool, active: int, total: int) -> None:
        if not connected:
            self.setToolTip("CCTV 未连接")
        else:
            self.setToolTip(f"CCTV {active}/{total}")
