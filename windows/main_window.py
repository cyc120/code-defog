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
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
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
    case_event = Signal(object)

    def __init__(self, client: StatusClient) -> None:
        super().__init__()
        client.on_state = self.state_changed.emit
        client.on_connection = self.connection_changed.emit
        client.on_case_event = self.case_event.emit


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
        self._cases: list[dict[str, Any]] = []
        self._case_status_filter: str = ""
        self._selected_case_id: str | None = None

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
        self._bridge.case_event.connect(self._on_case_event)

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
        cases_tab = self._build_cases_tab()

        self._tabs.addTab(sessions_tab, "项目详情")
        self._tabs.addTab(detail_tab, "事件流")
        self._tabs.addTab(cases_tab, "Case 队列")
        self._tabs.addTab(management_tab, "管理")

        # Prefetch cases on startup so the Case tab isn't empty
        self._refresh_cases()

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

    # -- Case tab ------------------------------------------------------------

    _CASE_STATUSES = [
        "RECEIVED", "TRIAGED", "DIAGNOSED", "PLAN_APPROVAL", "REPAIRING",
        "VERIFYING", "PATCH_REJECTED", "RELEASE_APPROVAL", "RELEASED",
        "ROLLED_BACK", "ESCALATED", "CLOSED",
    ]

    def _build_cases_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # Filter row
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("状态筛选:"))
        self._case_filter_combo = QComboBox()
        self._case_filter_combo.addItem("全部", "")
        for state in self._CASE_STATUSES:
            self._case_filter_combo.addItem(state, state)
        self._case_filter_combo.currentIndexChanged.connect(self._on_case_filter)
        filter_row.addWidget(self._case_filter_combo)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self._refresh_cases)
        filter_row.addWidget(refresh)
        filter_row.addStretch(1)
        layout.addLayout(filter_row)

        # Splitter: case list (left) + evidence detail (right)
        splitter = QSplitter()

        self._case_table = QTableWidget(0, 5)
        self._case_table.setHorizontalHeaderLabels(["标题", "状态", "优先级", "仓库", "更新时间"])
        self._case_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._case_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._case_table.itemSelectionChanged.connect(self._on_case_selection)
        self._case_table.setColumnWidth(0, 160)
        self._case_table.setColumnWidth(3, 140)
        splitter.addWidget(self._case_table)

        detail_widget = QWidget()
        detail_layout = QVBoxLayout(detail_widget)
        self._case_detail_label = QLabel("选中一个 Case 查看详情")
        self._case_detail_label.setWordWrap(True)
        detail_layout.addWidget(self._case_detail_label)
        self._case_detail_text = QTextEdit()
        self._case_detail_text.setReadOnly(True)
        detail_layout.addWidget(self._case_detail_text)

        # Approval buttons (enabled when case is in an approval state)
        self._approve_button = QPushButton("批准")
        self._approve_button.clicked.connect(self._on_approve)
        self._reject_button = QPushButton("拒绝")
        self._reject_button.clicked.connect(self._on_reject)
        self._cancel_button = QPushButton("取消 Case")
        self._cancel_button.clicked.connect(self._on_cancel_case)
        self._approve_button.setEnabled(False)
        self._reject_button.setEnabled(False)
        self._cancel_button.setEnabled(False)
        button_row = QHBoxLayout()
        button_row.addWidget(self._approve_button)
        button_row.addWidget(self._reject_button)
        button_row.addWidget(self._cancel_button)
        button_row.addStretch(1)
        detail_layout.addLayout(button_row)
        splitter.addWidget(detail_widget)
        splitter.setSizes([340, 560])

        layout.addWidget(splitter)
        return widget

    def _on_case_filter(self) -> None:
        self._case_status_filter = self._case_filter_combo.currentData() or ""
        self._refresh_cases()

    def _refresh_cases(self) -> None:
        cases = self._client.list_cases(status=self._case_status_filter or None)
        if cases is None:
            return
        self._cases = cases
        self._render_cases()

    def _render_cases(self) -> None:
        self._case_table.setRowCount(0)
        for case in self._cases:
            row = self._case_table.rowCount()
            self._case_table.insertRow(row)
            title = case.get("title") or case.get("case_id", "—")
            status = case.get("status", "—")
            priority = case.get("priority", "—")
            repo = case.get("repository_ref") or "—"
            updated = case.get("updated_at", "—")
            items = [
                QTableWidgetItem(str(title)),
                QTableWidgetItem(str(status)),
                QTableWidgetItem(str(priority)),
                QTableWidgetItem(str(repo)),
                QTableWidgetItem(str(updated)),
            ]
            # Color the status cell by approval / terminal state
            color = QColor("#e08a2e") if status in ("PLAN_APPROVAL", "RELEASE_APPROVAL") \
                else QColor("#d43d3d") if status in ("PATCH_REJECTED", "ROLLED_BACK", "ESCALATED") \
                else QColor("#2e9e5b") if status not in ("CLOSED",) else QColor("#8a8a8a")
            items[1].setForeground(color)
            for col, item in enumerate(items):
                self._case_table.setItem(row, col, item)
        self._render_case_detail()

    def _on_case_selection(self) -> None:
        selected = self._case_table.selectionModel().selectedRows()
        if not selected:
            self._selected_case_id = None
            self._render_case_detail()
            return
        row = selected[0].row()
        case = self._cases[row] if 0 <= row < len(self._cases) else None
        self._selected_case_id = case["case_id"] if case else None
        self._render_case_detail()

    def _render_case_detail(self) -> None:
        if not self._selected_case_id:
            self._case_detail_label.setText("选中一个 Case 查看详情")
            self._case_detail_text.setPlainText("")
            self._approve_button.setEnabled(False)
            self._reject_button.setEnabled(False)
            self._cancel_button.setEnabled(False)
            return
        case = next((c for c in self._cases if c["case_id"] == self._selected_case_id), None)
        if case is None:
            return
        status = case.get("status", "")
        self._case_detail_label.setText(
            f"{case.get('title') or case.get('case_id')} · {status} · "
            f"{case.get('priority', '—')} · 来源 {case.get('source_count', 0)}")
        evidence = self._client.get_case_evidence(self._selected_case_id)
        self._case_detail_text.setPlainText(self._format_evidence(evidence) if evidence else "无证据")
        # Approval buttons only in approval states
        plan_approval = status == "PLAN_APPROVAL"
        release_approval = status == "RELEASE_APPROVAL"
        self._approve_button.setEnabled(plan_approval or release_approval)
        self._reject_button.setEnabled(plan_approval or release_approval)
        self._cancel_button.setEnabled(status not in ("CLOSED", "RELEASED", "ROLLED_BACK"))

    def _format_evidence(self, evidence: dict[str, Any]) -> str:
        lines: list[str] = []
        sources = evidence.get("sources") or []
        lines.append(f"来源 Sources ({len(sources)})")
        for src in sources:
            lines.append(
                f"  {src.get('source_type')} | {src.get('source_uri')} | "
                f"{src.get('association_state')} | {src.get('received_at')}")
        runs = evidence.get("agent_runs") or []
        lines.append(f"\nAgent 运行 ({len(runs)})")
        for run in runs:
            lines.append(f"  {run.get('agent_id')} | {run.get('status')} | {run.get('started_at')}")
        tools = evidence.get("tool_runs") or []
        lines.append(f"\n工具链 ({len(tools)})")
        for tool in tools:
            lines.append(
                f"  [{tool.get('chain_sequence')}] {tool.get('tool_name')} | "
                f"exit {tool.get('exit_code')} | in {str(tool.get('input_sha256'))[:12]} → "
                f"out {str(tool.get('output_sha256'))[:12]}")
        approvals = evidence.get("approvals") or []
        lines.append(f"\n审批 ({len(approvals)})")
        for appr in approvals:
            lines.append(
                f"  {appr.get('action')} → {appr.get('decision')} | "
                f"{appr.get('approver')} | {appr.get('resolved_at')}")
        artifacts = evidence.get("artifacts") or []
        lines.append(f"\n制品 ({len(artifacts)})")
        for art in artifacts:
            lines.append(f"  {art.get('kind')} | {art.get('uri')} | sha {str(art.get('sha256'))[:12]}")
        knowledge = evidence.get("knowledge_records") or []
        lines.append(f"\n知识条目 ({len(knowledge)})")
        for rec in knowledge:
            lines.append(
                f"  {rec.get('status')} | tags={rec.get('reuse_tags')} | "
                f"{rec.get('created_at')}")
        retro = evidence.get("retrospective")
        if retro:
            report = retro.get("report") or {}
            content = report.get("content")
            if content:
                lines.append("\n复盘报告\n" + str(content))
        return "\n".join(lines)

    def _target_ref_for(self, case: dict[str, Any], action: str) -> str:
        """PLAN actions bind to base_commit; RELEASE actions bind to patch_ref."""
        if action in ("approve_plan", "reject_plan"):
            return str(case.get("base_commit") or "")
        return str(case.get("patch_ref") or "")

    def _on_approve(self) -> None:
        self._run_approval(approve=True)

    def _on_reject(self) -> None:
        self._run_approval(approve=False)

    def _run_approval(self, approve: bool) -> None:
        case = next((c for c in self._cases if c["case_id"] == self._selected_case_id), None)
        if case is None:
            return
        status = case.get("status", "")
        if status == "PLAN_APPROVAL":
            action = "approve_plan" if approve else "reject_plan"
            action_label = "批准计划" if approve else "拒绝计划"
        elif status == "RELEASE_APPROVAL":
            action = "approve_release" if approve else "reject_release"
            action_label = "批准发布" if approve else "拒绝发布"
        else:
            QMessageBox.warning(self, "无法审批", f"Case 不在可审批状态：{status}")
            return
        target_ref = self._target_ref_for(case, action)
        if not target_ref:
            QMessageBox.warning(self, "无法审批", "缺少 target_ref（base_commit 或 patch_ref）")
            return
        approver = __import__("getpass").getuser()
        if QMessageBox.question(
            self, "确认审批",
            f"{action_label}\n目标: {target_ref}\n审批人: {approver}\n\n确认继续？",
        ) != QMessageBox.StandardButton.Yes:
            return
        grant = self._client.request_approval_grant(
            case["case_id"], action, target_ref, approver)
        if grant is None:
            QMessageBox.warning(self, "签发失败", "无法签发审批凭证")
            return
        result = self._client.post_case_action(
            case["case_id"], action, grant["approval_token"], target_ref,
            reason=f"{action_label} by {approver}")
        if result is None:
            QMessageBox.warning(self, "审批失败", "消费审批凭证失败")
            return
        self._refresh_cases()

    def _on_cancel_case(self) -> None:
        if not self._selected_case_id:
            return
        if QMessageBox.question(
            self, "取消 Case", "确认取消此 Case（将转为 ESCALATED）？",
        ) != QMessageBox.StandardButton.Yes:
            return
        result = self._client.post_case_action(
            self._selected_case_id, "cancel", "", "", reason="cancelled from UI")
        if result is None:
            QMessageBox.warning(self, "取消失败", "无法取消 Case")
            return
        self._refresh_cases()

    def _on_case_event(self, _envelope: dict[str, Any]) -> None:
        # Any case SSE event invalidates the cached list; refresh lazily.
        self._refresh_cases()

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
