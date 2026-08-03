import AppKit
import SwiftUI

struct ManagementView: View {
    @ObservedObject var store: StatusStore
    var onOpenSession: (ProjectSummary) -> Void = { _ in }

    @State private var autoCollapseSeconds = StatusStore.bubbleAutoCollapseSeconds
    @State private var sessionPendingClear: ProjectSummary?
    @State private var confirmingClearAll = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                serviceCard
                sessionsCard
                dataCard
                notificationsCard
                aboutCard
            }
            .padding(24)
            .frame(maxWidth: 780, alignment: .leading)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.primary.opacity(0.018))
        .task { await store.refreshManagementInfo() }
        .onChange(of: autoCollapseSeconds) { newValue in
            StatusStore.bubbleAutoCollapseSeconds = newValue
        }
        .confirmationDialog(
            "清除会话记录？",
            isPresented: Binding(
                get: { sessionPendingClear != nil },
                set: { if !$0 { sessionPendingClear = nil } }
            ),
            presenting: sessionPendingClear
        ) { project in
            Button("清除记录", role: .destructive) {
                Task { await store.clearSession(project) }
            }
            Button("取消", role: .cancel) {}
        } message: { project in
            Text("将删除 \(project.displayName) 的全部事件记录。")
        }
        .confirmationDialog(
            "清空全部监控数据？",
            isPresented: $confirmingClearAll,
            titleVisibility: .visible
        ) {
            Button("清空全部", role: .destructive) {
                Task { await store.clearAllSessions() }
            }
            Button("取消", role: .cancel) {}
        } message: {
            Text("所有会话和事件记录都会被删除，此操作不可撤销。")
        }
    }

    private var serviceCard: some View {
        sectionCard(title: "服务状态", systemImage: "server.rack", subtitle: "本地后台与数据存储") {
            HStack(spacing: 8) {
                Circle()
                    .fill(store.connected ? Color.green : Color.secondary)
                    .frame(width: 8, height: 8)
                Text(store.connectionSummary)
                    .font(.system(size: 13, weight: .semibold))
                Spacer()
                Button {
                    Task { await store.refreshManagementInfo() }
                } label: {
                    Label("刷新", systemImage: "arrow.clockwise")
                        .font(.system(size: 12, weight: .medium))
                }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
            }
            Divider()
            LazyVGrid(
                columns: [
                    GridItem(.flexible(), alignment: .leading),
                    GridItem(.flexible(), alignment: .leading),
                    GridItem(.flexible(), alignment: .leading),
                ],
                spacing: 12
            ) {
                statItem("进程", store.managementInfo.map { "\($0.pid)" } ?? "—")
                statItem("端口", store.managementInfo.map { "\($0.port)" } ?? "—")
                statItem("运行时长", store.managementInfo.map { formatUptime($0.uptimeSeconds) } ?? "—")
                statItem("保留上限", store.managementInfo.map { "\($0.retention) 条" } ?? "—")
                statItem("数据库", store.managementInfo.map { formatBytes($0.dbBytes) } ?? "—")
                statItem("事件总数", store.managementInfo.map { "\($0.totalEvents)" } ?? "—")
            }
        }
    }

    private var sessionsCard: some View {
        sectionCard(title: "监听会话", systemImage: "rectangle.stack", subtitle: "管理每个会话的提醒与记录") {
            if store.state.projects.isEmpty {
                Text("暂无监听会话")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .center)
                    .padding(.vertical, 18)
            } else {
                VStack(spacing: 8) {
                    ForEach(store.state.projects) { project in
                        sessionRow(project)
                    }
                }
            }
        }
    }

    private func sessionRow(_ project: ProjectSummary) -> some View {
        let muted = store.isMuted(project.id)
        return HStack(spacing: 10) {
            Circle()
                .fill(statusColor(project.status, active: project.active))
                .frame(width: 8, height: 8)
            VStack(alignment: .leading, spacing: 2) {
                Text(project.displayName)
                    .font(.system(size: 12, weight: .semibold))
                    .lineLimit(1)
                    .foregroundStyle(muted ? .secondary : .primary)
                Text("\(project.eventCount) 条事件 · \(shortActivityTime(project.updatedAt))")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if muted {
                Label("已静音", systemImage: "bell.slash")
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(.secondary)
            }
            Button {
                onOpenSession(project)
            } label: {
                Image(systemName: "arrow.up.right.square")
                    .font(.system(size: 11, weight: .semibold))
                    .frame(width: 26, height: 26)
            }
            .buttonStyle(.plain)
            .help("打开会话详情")
            Button {
                store.setMuted(!muted, for: project.id)
            } label: {
                Image(systemName: muted ? "bell" : "bell.slash")
                    .font(.system(size: 11, weight: .semibold))
                    .frame(width: 26, height: 26)
            }
            .buttonStyle(.plain)
            .help(muted ? "取消静音" : "静音该会话")
            Button {
                sessionPendingClear = project
            } label: {
                Image(systemName: "trash")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.red)
                    .frame(width: 26, height: 26)
            }
            .buttonStyle(.plain)
            .help("清除该会话记录")
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
    }

    private var dataCard: some View {
        sectionCard(title: "数据管理", systemImage: "externaldrive", subtitle: "本地 SQLite 状态库") {
            HStack(spacing: 10) {
                Button {
                    openDataFolder()
                } label: {
                    Label("打开数据目录", systemImage: "folder")
                        .font(.system(size: 12, weight: .medium))
                        .padding(.horizontal, 12)
                        .padding(.vertical, 7)
                        .background(Color.primary.opacity(0.08), in: Capsule())
                }
                .buttonStyle(.plain)
                Spacer()
                Button(role: .destructive) {
                    confirmingClearAll = true
                } label: {
                    Label("清空全部数据", systemImage: "trash")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(.red)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 7)
                        .background(Color.red.opacity(0.1), in: Capsule())
                }
                .buttonStyle(.plain)
                .disabled(!store.connected)
            }
            if let info = store.managementInfo {
                Text(info.statePath)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .textSelection(.enabled)
            }
        }
    }

    private var notificationsCard: some View {
        sectionCard(title: "提醒设置", systemImage: "bell.badge", subtitle: "控制消息泡泡与浮窗提醒") {
            Toggle(
                "全局静音（隐藏所有会话提醒）",
                isOn: Binding(
                    get: { store.isMuteAll },
                    set: { store.setMuteAll($0) }
                )
            )
            .toggleStyle(.switch)
            .font(.system(size: 13, weight: .medium))

            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text("消息泡泡自动收起")
                        .font(.system(size: 13, weight: .medium))
                    Spacer()
                    Text("\(Int(autoCollapseSeconds)) 秒")
                        .font(.system(size: 12, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
                Slider(value: $autoCollapseSeconds, in: 2...30, step: 1)
            }
        }
    }

    private var aboutCard: some View {
        sectionCard(title: "关于", systemImage: "info.circle", subtitle: "Code CCTV") {
            HStack(spacing: 24) {
                LabeledContent("版本", value: appVersion)
                LabeledContent("连接", value: store.connectionSummary)
                LabeledContent("会话", value: "\(store.state.summary.totalProjects)")
                Spacer()
            }
            .font(.system(size: 12))
        }
    }

    private var appVersion: String {
        let short = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0.1.0"
        let build = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "1"
        return "\(short) (\(build))"
    }

    private func sectionCard<Content: View>(
        title: String,
        systemImage: String,
        subtitle: String,
        @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 9) {
                Image(systemName: systemImage)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(.tint)
                VStack(alignment: .leading, spacing: 1) {
                    Text(title)
                        .font(.headline)
                    Text(subtitle)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
            }
            content()
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .stroke(Color.primary.opacity(0.1), lineWidth: 0.7)
        )
    }

    private func statItem(_ title: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(value)
                .font(.system(size: 14, weight: .bold, design: .rounded))
                .foregroundStyle(.primary)
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func openDataFolder() {
        let statePath = store.managementInfo?.statePath ?? store.defaultStatePath
        let url = URL(fileURLWithPath: statePath).deletingLastPathComponent()
        NSWorkspace.shared.open(url)
    }
}

private func formatBytes(_ value: Int) -> String {
    let units = ["B", "KB", "MB", "GB"]
    var amount = Double(max(value, 0))
    var unit = 0
    while amount >= 1024 && unit < units.count - 1 {
        amount /= 1024
        unit += 1
    }
    return String(format: "%.1f %@", amount, units[unit])
}

private func formatUptime(_ seconds: Double) -> String {
    let total = Int(seconds)
    if total < 60 { return "\(total) 秒" }
    let minutes = total / 60
    if minutes < 60 { return "\(minutes) 分钟" }
    return "\(minutes / 60) 小时 \(minutes % 60) 分"
}
