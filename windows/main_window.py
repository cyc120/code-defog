"""PySide6 main window (global preview) for the Windows app.

Three tabs mirroring the macOS preview: session list, project detail with the
recent-events timeline, and management (mute / clear session / clear-all /
service stats). All UI text is Chinese.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, QSettings, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from status_client import StatusClient, session_id


def status_color(status: str, active: bool = True) -> QColor:
    if "阻塞" in status or "blocked" in status.lower():
        return QColor("#d43d3d")
    if "风险" in status or "warning" in status.lower():
        return QColor("#e08a2e")
    if active or "监听" in status or "watch" in status.lower():
        return QColor("#2e9e5b")
    return QColor("#8a8a8a")


class StatusClientBridge(QObject):
    """Thread-safe bridge from StatusClient callbacks (background threads) to
    Qt signals (queued connections)."""

    state_changed = Signal(object)
    connection_changed = Signal(bool)

    def __init__(self, client: StatusClient) -> None:
        super().__init__()
        client.on_state = self.state_changed.emit
        client.on_connection = self.connection_changed.emit


class MuteStore:
    """Per-app preferences (muted sessions, mute-all), replacing UserDefaults."""

    def __init__(self) -> None:
        self._settings = QSettings("CodeCCTV", "CodeCCTV")

    def muted(self) -> set[str]:
        return set(self._settings.value("mutedSessions", [], type=list))

    def set_muted(self, project_id: str, muted: bool) -> None:
        ids = self.muted()
        if muted:
            ids.add(project_id)
        else:
            ids.discard(project_id)
        self._settings.setValue("mutedSessions", sorted(ids))

    def is_mute_all(self) -> bool:
        return bool(self._settings.value("muteAll", False, type=bool))

    def set_mute_all(self, value: bool) -> None:
        self._settings.setValue("muteAll", value)


class MainWindow(QMainWindow):
    def __init__(self, client: StatusClient, mutes: MuteStore) -> None:
        super().__init__()
        self._client = client
        self._mutes = mutes
        self._bridge = StatusClientBridge(client)
        self._projects: list[dict[str, Any]] = []
        self._info: dict[str, Any] | None = None

        self.setWindowTitle("Code CCTV General")
        self.resize(940, 640)
        self._tabs = QTabWidget()
        self._session_table = QTableWidget(0, 4)
        self._session_table.setHorizontalHeaderLabels(["项目", "状态", "阶段", "焦点"])
        self._session_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._session_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._session_table.itemSelectionChanged.connect(self._on_selection)

        self._events_list = QListWidget()
        self._status_label = QLabel("等待后台服务")
        self._info_label = QLabel("—")

        self._build_tabs()
        self.setCentralWidget(self._tabs)

        self._bridge.state_changed.connect(self._on_state)
        self._bridge.connection_changed.connect(self._on_connection)

    # -- tab construction ---------------------------------------------------

    def _build_tabs(self) -> None:
        sessions_tab = QWidget()
        layout = QVBoxLayout(sessions_tab)
        layout.addWidget(self._session_table)

        detail_tab = QWidget()
        detail = QVBoxLayout(detail_tab)
        detail.addWidget(QLabel("事件流（最近 8 条）"))
        detail.addWidget(self._events_list)

        management_tab = self._build_management_tab()

        self._tabs.addTab(sessions_tab, "项目详情")
        self._tabs.addTab(detail_tab, "事件流")
        self._tabs.addTab(management_tab, "管理")

    def _build_management_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.addWidget(self._status_label)

        service_box = QGroupBox("服务状态")
        service_layout = QVBoxLayout(service_box)
        service_layout.addWidget(self._info_label)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self._refresh_info)
        service_layout.addWidget(refresh)
        layout.addWidget(service_box)

        data_box = QGroupBox("数据管理")
        data_layout = QHBoxLayout(data_box)
        clear_all = QPushButton("清空全部数据")
        clear_all.setStyleSheet("color: #d43d3d;")
        clear_all.clicked.connect(self._clear_all)
        data_layout.addWidget(clear_all)
        data_layout.addStretch(1)
        layout.addWidget(data_box)
        layout.addStretch(1)
        return widget

    # -- state updates ------------------------------------------------------

    def _on_state(self, state: dict[str, Any]) -> None:
        self._projects = state.get("projects") or []
        self._render_sessions()
        self._refresh_info()

    def _on_connection(self, connected: bool) -> None:
        if connected:
            self._status_label.setText("实时监听中")
        else:
            self._status_label.setText("等待后台服务")

    def _refresh_info(self) -> None:
        info = self._client.refresh_management_info()
        if info is not None:
            self._info = info
            self._info_label.setText(
                f"进程 {info.get('pid', '—')} · 端口 {info.get('port', '—')} · "
                f"保留上限 {info.get('retention', '—')} 条 · "
                f"会话 {info.get('total_sessions', '—')} · "
                f"事件 {info.get('total_events', '—')} · "
                f"数据库 {info.get('db_bytes', 0) // 1024} KB"
            )

    def _clear_all(self) -> None:
        self._client.clear_all()

    # -- session list -------------------------------------------------------

    def visible_projects(self) -> list[dict[str, Any]]:
        if self._mutes.is_mute_all():
            return []
        muted = self._mutes.muted()
        return [p for p in self._projects if f"{p.get('workspace')}|{session_id(p)}" not in muted]

    def _render_sessions(self) -> None:
        projects = self.visible_projects()
        self._session_table.setRowCount(len(projects))
        for row, project in enumerate(projects):
            self._session_table.setItem(row, 0, self._cell(project.get("name", "")))
            self._session_table.setItem(row, 1, self._cell(project.get("status", "")))
            self._session_table.setItem(row, 2, self._cell(project.get("phase", "")))
            self._session_table.setItem(row, 3, self._cell(project.get("focus", "")))

    @staticmethod
    def _cell(text: str) -> QTableWidgetItem:
        return QTableWidgetItem(text or "")

    def _on_selection(self) -> None:
        row = self._session_table.currentRow()
        projects = self.visible_projects()
        if not 0 <= row < len(projects):
            return
        self._render_events(projects[row])

    def _render_events(self, project: dict[str, Any]) -> None:
        self._events_list.clear()
        events = project.get("recent_events") or []
        for event in events:
            phase = event.get("phase") or event.get("event_type") or ""
            status = event.get("status", "")
            focus = event.get("focus", "")
            note = event.get("note", "")
            text = f"[{status}] {phase} — {focus}" + (f" · {note}" if note else "")
            item = QListWidgetItem(text)
            item.setForeground(status_color(status))
            self._events_list.addItem(item)
