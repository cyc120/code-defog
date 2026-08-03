import Foundation
import SwiftUI

private struct LiveIndicator: View {
    let status: String
    let active: Bool
    let updateID: String
    let bounceOnAppear: Bool

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var pulse = false
    @State private var bounce = false

    init(status: String, active: Bool, updateID: String, bounceOnAppear: Bool = false) {
        self.status = status
        self.active = active
        self.updateID = updateID
        self.bounceOnAppear = bounceOnAppear
    }

    private var color: Color {
        statusColor(status, active: active)
    }

    private func triggerBounce() {
        guard !updateID.isEmpty, !reduceMotion else { return }
        bounce = false
        DispatchQueue.main.async {
            bounce = true
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.26) {
            bounce = false
        }
    }

    var body: some View {
        ZStack {
            Circle()
                .fill(color)
                .frame(width: 8, height: 8)

            if active && !reduceMotion {
                Circle()
                    .stroke(color.opacity(0.65), lineWidth: 1)
                    .frame(width: 10, height: 10)
                    .scaleEffect(pulse ? 2.1 : 1)
                    .opacity(pulse ? 0 : 0.8)
                    .animation(
                        .easeOut(duration: 1.15).repeatForever(autoreverses: false),
                        value: pulse
                    )
            }
        }
        .frame(width: 18, height: 18)
        .scaleEffect(bounce ? 1.28 : 1)
        .offset(y: bounce ? -1.5 : 0)
        .animation(
            reduceMotion
                ? .linear(duration: 0.01)
                : .spring(response: 0.2, dampingFraction: 0.58, blendDuration: 0.04),
            value: bounce
        )
        .onAppear {
            pulse = active && !reduceMotion
            if bounceOnAppear {
                triggerBounce()
            }
        }
        .onChange(of: active) { isActive in
            pulse = false
            guard isActive && !reduceMotion else { return }
            DispatchQueue.main.async {
                pulse = true
            }
        }
        .onChange(of: updateID) { newID in
            guard !newID.isEmpty else { return }
            triggerBounce()
        }
        .accessibilityLabel(active ? "正在更新" : "已暂停")
    }
}

private struct IslandIconButton: View {
    let systemName: String
    let helpText: String
    let action: () -> Void

    @State private var isHovering = false

    var body: some View {
        Button(action: action) {
            Image(systemName: systemName)
                .font(.system(size: 11, weight: .bold))
                .foregroundStyle(.primary.opacity(isHovering ? 1 : 0.82))
                .frame(width: 28, height: 28)
                .background(
                    isHovering ? Color.primary.opacity(0.1) : .clear,
                    in: Circle()
                )
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .onHover { hovering in
            withAnimation(.easeOut(duration: 0.15)) {
                isHovering = hovering
            }
        }
        .help(helpText)
        .accessibilityLabel(helpText)
    }
}

private struct IslandTextButton: View {
    let title: String
    let systemName: String
    let action: () -> Void

    @State private var isHovering = false

    var body: some View {
        Button(action: action) {
            Label(title, systemImage: systemName)
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(.primary.opacity(isHovering ? 1 : 0.86))
                .padding(.horizontal, 11)
                .padding(.vertical, 7)
                .background(
                    isHovering ? Color.primary.opacity(0.14) : Color.primary.opacity(0.08),
                    in: Capsule()
                )
        }
        .buttonStyle(.plain)
        .onHover { hovering in
            withAnimation(.easeOut(duration: 0.15)) {
                isHovering = hovering
            }
        }
        .help(title)
    }
}

private struct CompactIslandView: View {
    @ObservedObject var store: StatusStore
    let onTap: () -> Void
    let onDoubleTap: () -> Void

    @State private var isHovering = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var activity: ActivitySummary? {
        store.latestActivity
    }

    private var status: String {
        activity?.status ?? store.state.projects.first?.status ?? ""
    }

    private var active: Bool {
        activity?.active ?? store.connected
    }

    var body: some View {
        HStack(spacing: 0) {
            Button(action: onTap) {
                HStack(spacing: 9) {
                    LiveIndicator(status: status, active: active, updateID: store.stateID)

                    VStack(alignment: .leading, spacing: 1) {
                        Text(activity?.projectName ?? "Code CCTV")
                            .font(.system(size: 12, weight: .semibold, design: .rounded))
                            .foregroundStyle(.primary)
                            .lineLimit(1)
                        Text(store.connected ? store.pillTitle : "CCTV 未连接")
                            .font(.system(size: 9, weight: .medium, design: .rounded))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }

                    Spacer(minLength: 2)
                }
                .padding(.leading, 14)
                .padding(.trailing, 8)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            .buttonStyle(.plain)
            .contentShape(Rectangle())
            .simultaneousGesture(TapGesture(count: 2).onEnded { onDoubleTap() })
            .accessibilityElement(children: .combine)
            .accessibilityAddTraits(.isButton)
            .accessibilityLabel("Code CCTV")
            .accessibilityValue(activity?.focus.isEmpty == false ? activity!.focus : store.pillTitle)
            .accessibilityHint("单击展开动态岛，双击打开全局预览，拖动移动浮窗")
            .help("单击展开动态岛，双击打开全局预览，拖动移动浮窗")

            Button(action: onTap) {
                Image(systemName: "chevron.down")
                    .font(.system(size: 11, weight: .bold))
                    .foregroundStyle(.primary.opacity(isHovering ? 1 : 0.78))
                    .frame(width: 28, height: 28)
            }
            .buttonStyle(.plain)
            .frame(width: 38, height: 44)
            .contentShape(Rectangle())
            .help("展开动态岛")
            .accessibilityLabel("展开动态岛")
        }
        .scaleEffect(isHovering && !reduceMotion ? 1.018 : 1)
        .animation(
            reduceMotion ? .linear(duration: 0.01) : .spring(response: 0.28, dampingFraction: 0.82),
            value: isHovering
        )
        .onHover { hovering in
            withAnimation(
                reduceMotion ? .linear(duration: 0.01) : .easeOut(duration: 0.16)
            ) {
                isHovering = hovering
            }
        }
    }
}

struct FloatingPanelView: View {
    @ObservedObject var controller: FloatingPanelController
    @ObservedObject var store: StatusStore
    let onOpen: () -> Void
    let onOpenGraph: () -> Void
    let onResize: (CGSize, Bool) -> Void

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var panelSize: CGSize {
        let size = controller.isExpanded ? FloatingPanelMetrics.expanded : FloatingPanelMetrics.collapsed
        return CGSize(width: size.width, height: size.height)
    }

    private var surfaceSize: CGSize {
        let size = controller.isExpanded ? FloatingPanelMetrics.expanded : FloatingPanelMetrics.collapsedVisual
        return CGSize(width: size.width, height: size.height)
    }

    private var surfaceCornerRadius: CGFloat {
        controller.isExpanded ? 22 : 18
    }

    private var islandAnimation: Animation {
        reduceMotion
            ? .linear(duration: 0.01)
            : FloatingPanelAnimation.swiftUI
    }

    var body: some View {
        ZStack(alignment: .center) {
            RoundedRectangle(cornerRadius: surfaceCornerRadius, style: .continuous)
                .fill(.ultraThinMaterial)
                .frame(width: surfaceSize.width, height: surfaceSize.height)
                .overlay(
                    RoundedRectangle(cornerRadius: surfaceCornerRadius, style: .continuous)
                        .stroke(Color.primary.opacity(0.14), lineWidth: 0.7)
                )
                .clipShape(RoundedRectangle(cornerRadius: surfaceCornerRadius, style: .continuous))
                .shadow(
                    color: .black.opacity(controller.isExpanded ? 0.1 : 0.08),
                    radius: controller.isExpanded ? 16 : 12,
                    y: controller.isExpanded ? 8 : 5
                )

            ZStack(alignment: .center) {
                CompactIslandView(
                    store: store,
                    onTap: { controller.presentBubble() },
                    onDoubleTap: onOpen
                )
                .opacity(controller.isExpanded ? 0 : 1)
                .scaleEffect(controller.isExpanded ? 0.72 : 1, anchor: .center)
                .allowsHitTesting(!controller.isExpanded)
                .accessibilityHidden(controller.isExpanded)

                ActivityBubble(
                    activity: store.latestActivity,
                    updateID: store.stateID,
                    onOpen: onOpen,
                    onOpenGraph: onOpenGraph,
                    onCollapse: controller.collapseBubble,
                    onDismiss: { controller.dismissBubble(stateID: store.stateID) },
                    onHidePanel: controller.hide
                )
                .opacity(controller.isExpanded ? 1 : 0)
                .scaleEffect(controller.isExpanded ? 1 : 0.92, anchor: .center)
                .allowsHitTesting(controller.isExpanded)
                .accessibilityHidden(!controller.isExpanded)
            }
            .frame(width: panelSize.width, height: panelSize.height, alignment: .center)
        }
        .frame(width: panelSize.width, height: panelSize.height, alignment: .center)
        .contentShape(Rectangle())
        .animation(islandAnimation, value: controller.isExpanded)
        .contextMenu {
            Button(action: onOpen) {
                Label("打开全局预览", systemImage: "rectangle.3.group")
            }
            Button(action: toggleBubble) {
                Label(
                    controller.isExpanded ? "收起动态岛" : "展开动态岛",
                    systemImage: controller.isExpanded ? "chevron.down" : "text.bubble"
                )
            }
            Divider()
            Button(action: controller.hide) {
                Label("隐藏浮窗", systemImage: "eye.slash")
            }
        }
        .onAppear {
            onResize(panelSize, false)
        }
        .onChange(of: controller.isExpanded) { _ in
            onResize(panelSize, true)
        }
        .onChange(of: store.lastStateReceivedAt) { receivedAt in
            // Drive the bubble from actual server state, not from local
            // preference changes: muting/unmuting alters stateID but must not
            // pop the bubble open.
            guard receivedAt != nil else { return }
            let stateID = store.stateID
            guard !stateID.isEmpty else { return }
            controller.presentBubble(for: stateID, autoCollapse: true)
        }
    }

    private func toggleBubble() {
        if controller.isExpanded {
            controller.dismissBubble(stateID: store.stateID)
        } else {
            controller.presentBubble()
        }
    }
}

private struct ActivityBubble: View {
    let activity: ActivitySummary?
    let updateID: String
    let onOpen: () -> Void
    let onOpenGraph: () -> Void
    let onCollapse: () -> Void
    let onDismiss: () -> Void
    let onHidePanel: () -> Void

    private var status: String {
        activity?.status ?? ""
    }

    private var active: Bool {
        activity?.active ?? false
    }

    @ViewBuilder
    private var summaryButton: some View {
        Button(action: onOpenGraph) {
            VStack(alignment: .leading, spacing: 11) {
                if let activity, !activity.phase.isEmpty {
                    HStack(spacing: 6) {
                        Text(activity.phase)
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(statusColor(activity.status, active: activity.active))
                            .lineLimit(1)
                        if activity.active {
                            Text("正在进行")
                                .font(.system(size: 9, weight: .medium))
                                .foregroundStyle(.secondary)
                        }
                    }
                }

                Text(activity?.focus.isEmpty == false ? activity!.focus : "暂无最新活动")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(.primary)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)

                if let activity, !activity.note.isEmpty {
                    Text(activity.note)
                        .font(.system(size: 11, weight: .regular))
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .buttonStyle(.plain)
        .contentShape(Rectangle())
        .help("打开全局监听图")
        .accessibilityLabel("打开全局监听图")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 11) {
            HStack(spacing: 8) {
                LiveIndicator(
                    status: status,
                    active: active,
                    updateID: updateID,
                    bounceOnAppear: true
                )
                VStack(alignment: .leading, spacing: 1) {
                    Text(activity?.projectName ?? "Code CCTV")
                        .font(.system(size: 13, weight: .bold, design: .rounded))
                        .foregroundStyle(.primary)
                        .lineLimit(1)
                    Text(active ? "实时工作状态" : "最近一次状态")
                        .font(.system(size: 9, weight: .medium, design: .rounded))
                        .foregroundStyle(.secondary)
                }

                Spacer()

                IslandIconButton(
                    systemName: "rectangle.3.group",
                    helpText: "打开全局监听图",
                    action: onOpenGraph
                )
                IslandIconButton(
                    systemName: "chevron.down",
                    helpText: "收起动态岛",
                    action: onCollapse
                )
                IslandIconButton(
                    systemName: "xmark",
                    helpText: "关闭消息泡泡",
                    action: onDismiss
                )
            }

            summaryButton

            Spacer(minLength: 0)

            HStack(spacing: 8) {
                Text(activity.map { shortActivityTime($0.timestamp) } ?? "等待后台状态")
                    .font(.system(size: 10, weight: .medium, design: .monospaced))
                    .foregroundStyle(.secondary)
                Spacer()
                IslandTextButton(
                    title: "全局监听图",
                    systemName: "rectangle.3.group",
                    action: onOpenGraph
                )
                IslandIconButton(
                    systemName: "eye.slash",
                    helpText: "隐藏浮窗",
                    action: onHidePanel
                )
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

func shortActivityTime(_ value: String) -> String {
    let input = ISO8601DateFormatter()
    guard let date = input.date(from: value) else { return value }
    let output = DateFormatter()
    output.locale = Locale(identifier: "zh_CN")
    output.dateFormat = "HH:mm:ss"
    return output.string(from: date)
}

struct PreviewView: View {
    @ObservedObject var store: StatusStore
    @State private var selectedProjectID: String?
    @State private var viewMode: PreviewMode

    init(store: StatusStore, initialMode: PreviewMode = .list) {
        self.store = store
        _viewMode = State(initialValue: initialMode)
    }

    private var selectedProject: ProjectSummary? {
        let selected = selectedProjectID ?? store.visibleProjects.first?.id
        return store.visibleProjects.first { $0.id == selected }
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            if viewMode == .graph {
                GlobalMonitorGraph(store: store)
            } else if viewMode == .management {
                ManagementView(store: store) { project in
                    selectedProjectID = project.id
                    viewMode = .list
                }
            } else {
                HStack(spacing: 0) {
                    projectList
                        .frame(width: 330)
                    Divider()
                    detail
                }
            }
        }
        .frame(minWidth: 900, minHeight: 600)
        .background(.regularMaterial)
    }

    private var header: some View {
        HStack(spacing: 20) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Code CCTV")
                    .font(.title2.weight(.bold))
                Text(store.connectionSummary)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Picker("视图", selection: $viewMode) {
                Text("项目详情").tag(PreviewMode.list)
                Text("监听图").tag(PreviewMode.graph)
                Text("管理").tag(PreviewMode.management)
            }
            .pickerStyle(.segmented)
            .frame(width: 260)
            Metric(title: "会话", value: "\(store.visibleSummary.totalProjects)")
            Metric(title: "活跃", value: "\(store.visibleSummary.activeProjects)")
            Metric(title: "阻塞", value: "\(store.visibleSummary.blockedProjects)")
            Circle()
                .fill(store.connected ? .green : .secondary)
                .frame(width: 9, height: 9)
                .help(store.connected ? "服务已连接" : "服务未连接")
            Button {
                PreviewWindowController.shared.close()
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 11, weight: .bold))
                    .frame(width: 28, height: 28)
            }
            .buttonStyle(.plain)
            .background(Color.primary.opacity(0.06), in: Circle())
            .help("退出全局预览")
            .accessibilityLabel("退出全局预览")
            .keyboardShortcut(.cancelAction)
        }
        .padding(20)
    }

    private var projectList: some View {
        ScrollView {
            LazyVStack(spacing: 8) {
                if store.visibleProjects.isEmpty {
                    EmptyStateView(title: "暂无监听会话", systemImage: "rectangle.dashed")
                        .padding(.top, 80)
                } else {
                    ForEach(store.visibleProjects) { project in
                        Button {
                            selectedProjectID = project.id
                        } label: {
                            ProjectRow(project: project, selected: selectedProject?.id == project.id)
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
            .padding(14)
        }
    }

    @ViewBuilder
    private var detail: some View {
        if let project = selectedProject {
            ProjectDetail(project: project)
        } else {
            EmptyStateView(title: "选择一个会话", systemImage: "cursorarrow.click")
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}

private struct GlobalMonitorGraph: View {
    @ObservedObject var store: StatusStore
    @State private var selectedProjectID: String?
    @State private var showingDetail = false

    private var projects: [ProjectSummary] {
        store.visibleProjects
    }

    private var selectedProject: ProjectSummary? {
        projects.first { $0.id == selectedProjectID }
    }

    var body: some View {
        Group {
            if projects.isEmpty {
                EmptyStateView(title: "暂无监听会话", systemImage: "point.3.connected.trianglepath.dotted")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                GeometryReader { proxy in
                    ZStack {
                        Canvas { context, size in
                            let center = CGPoint(x: size.width / 2, y: size.height / 2)
                            for scale in [0.46, 0.72] {
                                let diameter = min(size.width, size.height) * scale
                                let ringRect = CGRect(
                                    x: center.x - diameter / 2,
                                    y: center.y - diameter / 2,
                                    width: diameter,
                                    height: diameter
                                )
                                var ring = Path()
                                ring.addEllipse(in: ringRect)
                                context.stroke(
                                    ring,
                                    with: .color(Color.accentColor.opacity(0.08)),
                                    style: StrokeStyle(lineWidth: 0.8, dash: [3, 8])
                                )
                            }
                            for index in projects.indices {
                                var path = Path()
                                path.move(to: center)
                                path.addLine(to: nodePosition(index: index, count: projects.count, size: size))
                                context.stroke(
                                    path,
                                    with: .color(Color.accentColor.opacity(0.3)),
                                    style: StrokeStyle(lineWidth: 1.2, dash: [4, 5])
                                )
                            }
                        }
                        .allowsHitTesting(false)

                        ForEach(Array(projects.enumerated()), id: \.element.id) { item in
                            MonitorGraphNode(
                                project: item.element,
                                selected: selectedProjectID == item.element.id,
                                action: {
                                    selectedProjectID = item.element.id
                                    showingDetail = true
                                }
                            )
                            .position(
                                nodePosition(
                                    index: item.offset,
                                    count: projects.count,
                                    size: proxy.size
                                )
                            )
                        }

                        GraphCenterNode(summary: store.visibleSummary)
                            .position(x: proxy.size.width / 2, y: proxy.size.height / 2)

                        VStack {
                            HStack(alignment: .top, spacing: 12) {
                                VStack(alignment: .leading, spacing: 3) {
                                    Text("实时拓扑")
                                        .font(.system(size: 15, weight: .bold, design: .rounded))
                                    Text(store.connectionSummary)
                                        .font(.system(size: 10, weight: .medium))
                                        .foregroundStyle(.secondary)
                                }
                                Spacer(minLength: 12)
                                HStack(spacing: 7) {
                                    GraphMetricChip(title: "会话", value: "\(store.visibleSummary.totalProjects)")
                                    GraphMetricChip(title: "活跃", value: "\(store.visibleSummary.activeProjects)")
                                    GraphMetricChip(title: "事件", value: "\(store.visibleSummary.eventCount)")
                                }
                            }
                            .padding(.horizontal, 22)
                            .padding(.top, 18)

                            Spacer()
                            if let selectedProject {
                                HStack(spacing: 8) {
                                    Circle()
                                        .fill(statusColor(selectedProject.status, active: selectedProject.active))
                                        .frame(width: 8, height: 8)
                                    Text(selectedProject.displayName)
                                        .font(.system(size: 11, weight: .semibold))
                                    Text(selectedProject.focus.isEmpty ? selectedProject.status : selectedProject.focus)
                                        .font(.system(size: 11))
                                        .foregroundStyle(.secondary)
                                        .lineLimit(1)
                                    Spacer(minLength: 0)
                                    Text("\(selectedProject.eventCount) 条事件")
                                        .font(.system(size: 10, design: .monospaced))
                                        .foregroundStyle(.secondary)
                                }
                                .padding(.horizontal, 13)
                                .frame(height: 42)
                                .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                                .overlay(
                                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                                        .stroke(Color.primary.opacity(0.1), lineWidth: 0.7)
                                )
                                .padding(.horizontal, 20)
                                .padding(.bottom, 16)
                            }
                        }
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Color.primary.opacity(0.018))
            }
        }
        .sheet(isPresented: $showingDetail) {
            if let selectedProject {
                RealtimeProjectDetailView(project: selectedProject)
            }
        }
    }

    private func nodePosition(index: Int, count: Int, size: CGSize) -> CGPoint {
        let radius = min(size.width * 0.34, size.height * 0.31)
        let angle = -Double.pi / 2 + (Double(index) * 2 * Double.pi / Double(count))
        return CGPoint(
            x: size.width / 2 + CGFloat(cos(angle)) * radius,
            y: size.height / 2 + CGFloat(sin(angle)) * radius
        )
    }
}

private struct GraphMetricChip: View {
    let title: String
    let value: String

    var body: some View {
        VStack(alignment: .trailing, spacing: 1) {
            Text(value)
                .font(.system(size: 13, weight: .bold, design: .rounded))
            Text(title)
                .font(.system(size: 9, weight: .medium))
                .foregroundStyle(.secondary)
        }
        .frame(minWidth: 42)
        .padding(.horizontal, 8)
        .padding(.vertical, 6)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .stroke(Color.primary.opacity(0.08), lineWidth: 0.6)
        )
    }
}

private enum RealtimeDetailMode: Hashable {
    case topology
    case events
}

private struct RealtimeStatusBadge: View {
    let text: String
    let color: Color
    let systemName: String

    var body: some View {
        Label(text, systemImage: systemName)
            .font(.system(size: 10, weight: .semibold))
            .foregroundStyle(color)
            .padding(.horizontal, 9)
            .padding(.vertical, 5)
            .background(color.opacity(0.11), in: Capsule())
            .overlay(
                Capsule()
                    .stroke(color.opacity(0.24), lineWidth: 0.7)
            )
    }
}

private struct RealtimeProjectDetailView: View {
    let project: ProjectSummary
    @State private var mode: RealtimeDetailMode = .topology
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                Circle()
                    .fill(statusColor(project.status, active: project.active))
                    .frame(width: 9, height: 9)
                VStack(alignment: .leading, spacing: 2) {
                    Text(project.displayName)
                        .font(.title3.weight(.bold))
                    Text(project.focus.isEmpty ? project.status : project.focus)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                Spacer()
                RealtimeStatusBadge(
                    text: project.active ? "实时监听" : "最近状态",
                    color: statusColor(project.status, active: project.active),
                    systemName: project.active ? "waveform.path.ecg" : "clock"
                )
                Button {
                    dismiss()
                } label: {
                    Image(systemName: "xmark")
                        .font(.system(size: 11, weight: .bold))
                        .frame(width: 28, height: 28)
                }
                .buttonStyle(.plain)
                .background(Color.primary.opacity(0.06), in: Circle())
                .help("返回全局监听图")
                .accessibilityLabel("返回全局监听图")
                .keyboardShortcut(.cancelAction)
            }
            .padding(.horizontal, 20)
            .padding(.top, 18)
            .padding(.bottom, 12)

            HStack(spacing: 12) {
                Picker("详情视图", selection: $mode) {
                    Label("实时拓扑", systemImage: "point.3.connected.trianglepath.dotted")
                        .tag(RealtimeDetailMode.topology)
                    Label("事件流", systemImage: "waveform.path.ecg")
                        .tag(RealtimeDetailMode.events)
                }
                .pickerStyle(.segmented)
                .frame(width: 220)

                Spacer(minLength: 0)

                Label("\(project.eventCount) 条事件", systemImage: "clock.arrow.circlepath")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
                Text(shortActivityTime(project.updatedAt))
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                }
            .padding(.horizontal, 20)
            .padding(.bottom, 13)

            Divider()

            if mode == .topology {
                RealtimeTopologyView(project: project)
            } else {
                RealtimeEventStreamView(project: project)
            }
        }
        .frame(minWidth: 760, minHeight: 560)
        .background(.regularMaterial)
    }
}

private struct RealtimeTopologyView: View {
    let project: ProjectSummary

    var body: some View {
        ScrollView([.horizontal, .vertical]) {
            RealtimeGraphCanvas(project: project)
                .padding(18)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.primary.opacity(0.018))
    }
}

private struct RealtimeEventStreamView: View {
    let project: ProjectSummary

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 10) {
                HStack {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("实时事件")
                            .font(.headline)
                        Text("最近 \(min(project.recentEvents.count, 8)) 条")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    RealtimeStatusBadge(
                        text: project.active ? "监听中" : "最近状态",
                        color: statusColor(project.status, active: project.active),
                        systemName: project.active ? "dot.radiowaves.left.and.right" : "clock"
                    )
                }
                .padding(.bottom, 4)

                if project.recentEvents.isEmpty {
                    EmptyStateView(title: "暂无事件", systemImage: "clock")
                        .padding(.top, 80)
                } else {
                    ForEach(Array(project.recentEvents.enumerated()), id: \.element.id) { item in
                        RealtimeTimelineRow(
                            event: item.element,
                            isLast: item.offset == project.recentEvents.count - 1
                        )
                    }
                }
            }
            .padding(20)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.primary.opacity(0.018))
    }
}

private struct RealtimeTimelineRow: View {
    let event: EventSummary
    let isLast: Bool

    private var accent: Color {
        statusColor(event.status, active: true)
    }

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            VStack(spacing: 0) {
                Circle()
                    .fill(accent)
                    .frame(width: 8, height: 8)
                    .overlay(
                        Circle()
                            .stroke(accent.opacity(0.22), lineWidth: 4)
                    )
                    .padding(.top, 18)

                if !isLast {
                    Rectangle()
                        .fill(accent.opacity(0.2))
                        .frame(width: 1.5)
                        .frame(maxHeight: .infinity)
                }
            }
            .frame(width: 10)

            RealtimeEventCard(event: event)
        }
    }
}

private struct RealtimeEventCard: View {
    let event: EventSummary

    private var accent: Color {
        statusColor(event.status, active: true)
    }

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            ZStack {
                Circle()
                    .fill(accent.opacity(0.14))
                    .frame(width: 32, height: 32)
                Image(systemName: realtimeEventIcon(for: event.eventType))
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(accent)
            }

            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(event.phase.isEmpty ? event.eventType : event.phase)
                        .font(.system(size: 13, weight: .semibold))
                    if !event.status.isEmpty {
                        Text(event.status)
                            .font(.system(size: 10, weight: .medium))
                            .foregroundStyle(accent)
                    }
                    Spacer(minLength: 0)
                    Text(shortActivityTime(event.timestamp))
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.secondary)
                }

                if !event.focus.isEmpty {
                    Text(event.focus)
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(.primary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if !event.note.isEmpty {
                    Text(event.note)
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if !event.files.isEmpty {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 6) {
                            ForEach(event.files, id: \.self) { file in
                                Text(file)
                                    .font(.system(size: 10, design: .monospaced))
                                    .foregroundStyle(.secondary)
                                    .padding(.horizontal, 7)
                                    .padding(.vertical, 4)
                                    .background(Color.primary.opacity(0.06), in: Capsule())
                            }
                        }
                    }
                }
            }
        }
        .padding(13)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .stroke(accent.opacity(0.18), lineWidth: 0.8)
        )
        .overlay(alignment: .leading) {
            RoundedRectangle(cornerRadius: 2, style: .continuous)
                .fill(accent)
                .frame(width: 3, height: 42)
                .padding(.leading, 1)
        }
    }
}

private func realtimeEventIcon(for type: String) -> String {
    switch type.lowercased() {
    case "验证", "validation": return "checkmark.circle"
    case "修改", "edit": return "pencil"
    case "阻塞", "blocked": return "exclamationmark.triangle"
    case "完成", "complete": return "flag.checkered"
    case "开始", "start": return "play.circle"
    default: return "waveform.path.ecg"
    }
}

private struct RealtimeGraphNode: Identifiable {
    enum Kind {
        case workspace
        case context
        case event
        case file
    }

    let id: String
    let title: String
    let detail: String
    let kind: Kind
    let eventType: String
    let status: String
    let active: Bool
}

private struct RealtimeGraphEdge: Identifiable {
    let from: String
    let to: String
    let dashed: Bool

    var id: String {
        "\(from)->\(to)-\(dashed)"
    }
}

private struct RealtimeGraphModel {
    let nodes: [RealtimeGraphNode]
    let edges: [RealtimeGraphEdge]
    let lanes: [[String]]
    let fileLaneAnchor: Int?

    init(project: ProjectSummary) {
        let rootID = "workspace"
        var nodes: [RealtimeGraphNode] = [
            RealtimeGraphNode(
                id: rootID,
                title: project.displayName,
                detail: project.active ? "实时监听中" : project.status,
                kind: .workspace,
                eventType: "",
                status: project.status,
                active: project.active
            )
        ]
        var edges: [RealtimeGraphEdge] = []
        var contextIDs: [String] = []

        func addContext(_ id: String, title: String, detail: String) {
            guard !detail.isEmpty else { return }
            nodes.append(
                RealtimeGraphNode(
                    id: id,
                    title: title,
                    detail: detail,
                    kind: .context,
                    eventType: "",
                    status: project.status,
                    active: project.active
                )
            )
            contextIDs.append(id)
            edges.append(RealtimeGraphEdge(from: rootID, to: id, dashed: false))
        }

        addContext("phase", title: "当前阶段", detail: project.phase)
        addContext("focus", title: "当前关注", detail: project.focus)

        let events = Array(project.recentEvents.reversed())
        var eventIDs: [String] = []
        for (index, event) in events.enumerated() {
            let id = "event-\(index)-\(event.id)"
            let title = event.phase.isEmpty ? event.eventType : event.phase
            let detail = event.focus.isEmpty
                ? (event.note.isEmpty ? event.status : event.note)
                : event.focus
            nodes.append(
                RealtimeGraphNode(
                    id: id,
                    title: title,
                    detail: detail,
                    kind: .event,
                    eventType: event.eventType,
                    status: event.status,
                    active: index == events.count - 1
                )
            )
            eventIDs.append(id)
            if let previous = eventIDs.dropLast().last {
                edges.append(RealtimeGraphEdge(from: previous, to: id, dashed: false))
            } else if let contextID = contextIDs.last {
                edges.append(RealtimeGraphEdge(from: contextID, to: id, dashed: false))
            } else {
                edges.append(RealtimeGraphEdge(from: rootID, to: id, dashed: false))
            }
        }

        var fileIDs: [String] = []
        if let latestEvent = project.recentEvents.first {
            for (index, file) in latestEvent.files.prefix(8).enumerated() {
                let id = "file-\(index)-\(file)"
                nodes.append(
                    RealtimeGraphNode(
                        id: id,
                        title: file,
                        detail: "最近事件涉及文件",
                        kind: .file,
                        eventType: "",
                        status: project.status,
                        active: false
                    )
                )
                fileIDs.append(id)
                if let latestEventID = eventIDs.last {
                    edges.append(RealtimeGraphEdge(from: latestEventID, to: id, dashed: true))
                } else {
                    edges.append(RealtimeGraphEdge(from: rootID, to: id, dashed: true))
                }
            }
        }

        self.nodes = nodes
        self.edges = edges
        self.lanes = [[rootID], contextIDs, eventIDs, fileIDs].filter { !$0.isEmpty }
        self.fileLaneAnchor = fileIDs.isEmpty ? nil : max(0, eventIDs.count - 1)
    }
}

private struct RealtimeGraphRoute {
    let path: Path
    let end: CGPoint
    let angle: CGFloat
}

private struct RealtimeGraphCanvas: View {
    let project: ProjectSummary
    private let model: RealtimeGraphModel

    private let nodeSize = CGSize(width: 228, height: 88)
    private let laneGap: CGFloat = 36

    init(project: ProjectSummary) {
        self.project = project
        self.model = RealtimeGraphModel(project: project)
    }

    private var canvasSize: CGSize {
        let widestLane = model.lanes.enumerated().map { index, lane in
            let anchor = index == model.lanes.count - 1 ? (model.fileLaneAnchor ?? 0) : 0
            return anchor + lane.count
        }.max() ?? 1
        let width = max(720, CGFloat(widestLane) * (nodeSize.width + 18) + 48)
        let height = CGFloat(model.lanes.count) * (nodeSize.height + laneGap) + 36
        return CGSize(width: width, height: max(260, height))
    }

    private var positions: [String: CGPoint] {
        var result: [String: CGPoint] = [:]
        for (laneIndex, lane) in model.lanes.enumerated() {
            let laneAnchor = laneIndex == model.lanes.count - 1 ? (model.fileLaneAnchor ?? 0) : 0
            let startX = nodeSize.width / 2 + 78 + CGFloat(laneAnchor) * (nodeSize.width + 18)
            let y = 18 + nodeSize.height / 2 + CGFloat(laneIndex) * (nodeSize.height + laneGap)
            for (index, id) in lane.enumerated() {
                result[id] = CGPoint(
                    x: startX + CGFloat(index) * (nodeSize.width + 18),
                    y: y
                )
            }
        }
        return result
    }

    var body: some View {
        ZStack(alignment: .topLeading) {
            Canvas { context, _ in
                for laneIndex in model.lanes.indices {
                    let laneTop = CGFloat(laneIndex) * (nodeSize.height + laneGap) + 5
                    let laneRect = CGRect(
                        x: 12,
                        y: laneTop,
                        width: canvasSize.width - 24,
                        height: nodeSize.height + 18
                    )
                    var lanePath = Path()
                    lanePath.addRoundedRect(
                        in: laneRect,
                        cornerSize: CGSize(width: 14, height: 14)
                    )
                    context.fill(
                        lanePath,
                        with: .color(
                            Color.primary.opacity(laneIndex.isMultiple(of: 2) ? 0.032 : 0.016)
                        )
                    )
                    if laneIndex > 0 {
                        var divider = Path()
                        divider.move(
                            to: CGPoint(x: 22, y: laneTop - 18)
                        )
                        divider.addLine(
                            to: CGPoint(x: canvasSize.width - 22, y: laneTop - 18)
                        )
                        context.stroke(
                            divider,
                            with: .color(Color.primary.opacity(0.07)),
                            style: StrokeStyle(lineWidth: 0.7, dash: [2, 5])
                        )
                    }
                }

                for edge in model.edges {
                    guard let source = positions[edge.from], let target = positions[edge.to] else { continue }
                    let route = route(from: source, to: target)
                    let color = edge.dashed
                        ? Color.purple.opacity(0.32)
                        : Color.accentColor.opacity(0.48)
                    context.stroke(
                        route.path,
                        with: .color(color),
                        style: StrokeStyle(
                            lineWidth: edge.dashed ? 1 : 1.3,
                            lineCap: .round,
                            dash: edge.dashed ? [4, 4] : []
                        )
                    )

                    let length: CGFloat = 7
                    let width: CGFloat = 3.5
                    var arrow = Path()
                    arrow.move(to: route.end)
                    arrow.addLine(
                        to: CGPoint(
                            x: route.end.x - cos(route.angle) * length + sin(route.angle) * width,
                            y: route.end.y - sin(route.angle) * length - cos(route.angle) * width
                        )
                    )
                    arrow.addLine(
                        to: CGPoint(
                            x: route.end.x - cos(route.angle) * length - sin(route.angle) * width,
                            y: route.end.y - sin(route.angle) * length + cos(route.angle) * width
                        )
                    )
                    arrow.closeSubpath()
                    context.fill(arrow, with: .color(color))
                }
            }
            .frame(width: canvasSize.width, height: canvasSize.height)
            .allowsHitTesting(false)

            ForEach(Array(model.lanes.enumerated()), id: \.offset) { laneIndex, lane in
                Text(laneTitle(for: laneIndex, count: lane.count))
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 4)
                    .background(.thinMaterial, in: Capsule())
                    .overlay(
                        Capsule()
                            .stroke(Color.primary.opacity(0.08), lineWidth: 0.6)
                    )
                    .position(
                        x: 65,
                        y: 18 + CGFloat(laneIndex) * (nodeSize.height + laneGap)
                    )
            }

            ForEach(model.nodes) { node in
                RealtimeGraphNodeCard(node: node, size: nodeSize)
                    .position(positions[node.id] ?? .zero)
            }
        }
        .frame(width: canvasSize.width, height: canvasSize.height)
        .padding(12)
    }

    private func laneTitle(for index: Int, count: Int) -> String {
        switch index {
        case 0: return "工作区"
        case 1: return "实时上下文"
        case 2: return "事件轨迹 · \(count)"
        default: return "最近文件 · \(count)"
        }
    }

    private func route(from source: CGPoint, to target: CGPoint) -> RealtimeGraphRoute {
        let dx = target.x - source.x
        let dy = target.y - source.y
        if abs(dy) < 1 {
            let direction = dx >= 0 ? CGFloat(1) : -1
            let start = CGPoint(x: source.x + direction * nodeSize.width / 2, y: source.y)
            let end = CGPoint(x: target.x - direction * nodeSize.width / 2, y: target.y)
            var path = Path()
            path.move(to: start)
            path.addLine(to: end)
            return RealtimeGraphRoute(
                path: path,
                end: end,
                angle: atan2(end.y - start.y, end.x - start.x)
            )
        }

        let direction = dy >= 0 ? CGFloat(1) : -1
        let start = CGPoint(x: source.x, y: source.y + direction * nodeSize.height / 2)
        let end = CGPoint(x: target.x, y: target.y - direction * nodeSize.height / 2)
        let gutterY = (start.y + end.y) / 2
        let firstTurn = CGPoint(x: start.x, y: gutterY)
        let secondTurn = CGPoint(x: end.x, y: gutterY)
        var path = Path()
        path.move(to: start)
        path.addLine(to: firstTurn)
        path.addLine(to: secondTurn)
        path.addLine(to: end)
        return RealtimeGraphRoute(
            path: path,
            end: end,
            angle: atan2(end.y - secondTurn.y, end.x - secondTurn.x)
        )
    }
}

private struct RealtimeGraphNodeCard: View {
    let node: RealtimeGraphNode
    let size: CGSize

    private var accent: Color {
        switch node.kind {
        case .workspace: return .accentColor
        case .context: return .blue
        case .event: return statusColor(node.status, active: node.active)
        case .file: return .purple
        }
    }

    private var icon: String {
        switch node.kind {
        case .workspace: return "dot.radiowaves.left.and.right"
        case .context: return "scope"
        case .event: return realtimeEventIcon(for: node.eventType)
        case .file: return "doc.text"
        }
    }

    private var kindTitle: String {
        switch node.kind {
        case .workspace: return "工作区"
        case .context: return "上下文"
        case .event: return "事件"
        case .file: return "文件"
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Text(kindTitle)
                    .font(.system(size: 9, weight: .semibold))
                    .foregroundStyle(accent)
                Spacer(minLength: 0)
                if node.active {
                    HStack(spacing: 4) {
                        Circle()
                            .fill(accent)
                            .frame(width: 5, height: 5)
                        Text("实时")
                            .font(.system(size: 9, weight: .semibold))
                            .foregroundStyle(accent)
                    }
                } else if node.kind == .file {
                    Text("关联")
                        .font(.system(size: 9, weight: .medium))
                        .foregroundStyle(.secondary)
                }
            }

            HStack(spacing: 9) {
                ZStack {
                    Circle()
                        .fill(accent.opacity(0.14))
                        .frame(width: 30, height: 30)
                    Image(systemName: icon)
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(accent)
                }

                VStack(alignment: .leading, spacing: 3) {
                    Text(node.title)
                        .font(.system(size: 12, weight: .semibold))
                        .lineLimit(1)
                    Text(node.detail.isEmpty ? node.status : node.detail)
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
                Spacer(minLength: 0)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .frame(width: size.width, height: size.height, alignment: .leading)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 11, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 11, style: .continuous)
                .fill(accent.opacity(node.kind == .workspace ? 0.06 : 0.018))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 11, style: .continuous)
                .stroke(accent.opacity(node.active ? 0.58 : 0.22), lineWidth: node.active ? 1.2 : 0.8)
        )
        .overlay(alignment: .leading) {
            RoundedRectangle(cornerRadius: 2, style: .continuous)
                .fill(accent)
                .frame(width: 3, height: 44)
                .padding(.leading, 1)
        }
        .shadow(
            color: accent.opacity(node.active ? 0.18 : 0.05),
            radius: node.active ? 11 : 4,
            y: 3
        )
        .accessibilityLabel("\(node.title)，\(node.detail)")
    }
}

private struct MermaidChartCard: View {
    let chart: MermaidChart

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 7) {
                Image(systemName: "point.3.connected.trianglepath.dotted")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.tint)
                Text(chart.title)
                    .font(.system(size: 13, weight: .bold))
                Spacer()
                Text("\(chart.nodes.count) 个节点 · \(chart.edges.count) 条连接")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
            }

            MermaidGraphCanvas(chart: chart)
        }
        .padding(13)
        .frame(minWidth: 620, alignment: .leading)
        .background(Color.primary.opacity(0.045), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .stroke(Color.primary.opacity(0.08), lineWidth: 0.7)
        )
    }
}

private struct MermaidGraphCanvas: View {
    let chart: MermaidChart

    private var layout: MermaidGraphLayout {
        MermaidGraphLayout(chart: chart)
    }

    var body: some View {
        ScrollView([.horizontal, .vertical]) {
            ZStack(alignment: .topLeading) {
                Canvas { context, _ in
                    for edge in chart.edges {
                        guard
                            let source = layout.nodePositions[edge.from],
                            let target = layout.nodePositions[edge.to]
                        else { continue }

                        let (start, end) = edgePoints(from: source, to: target)
                        let color = Color.accentColor.opacity(edge.dashed ? 0.3 : 0.52)

                        var line = Path()
                        line.move(to: start)
                        line.addLine(to: end)
                        context.stroke(
                            line,
                            with: .color(color),
                            style: StrokeStyle(
                                lineWidth: edge.dashed ? 1 : 1.3,
                                lineCap: .round,
                                dash: edge.dashed ? [4, 4] : []
                            )
                        )

                        let angle = atan2(end.y - start.y, end.x - start.x)
                        let arrowLength: CGFloat = 7
                        let arrowWidth: CGFloat = 3.5
                        let left = CGPoint(
                            x: end.x - cos(angle) * arrowLength + sin(angle) * arrowWidth,
                            y: end.y - sin(angle) * arrowLength - cos(angle) * arrowWidth
                        )
                        let right = CGPoint(
                            x: end.x - cos(angle) * arrowLength - sin(angle) * arrowWidth,
                            y: end.y - sin(angle) * arrowLength + cos(angle) * arrowWidth
                        )
                        var arrow = Path()
                        arrow.move(to: end)
                        arrow.addLine(to: left)
                        arrow.addLine(to: right)
                        arrow.closeSubpath()
                        context.fill(arrow, with: .color(color))
                    }
                }
                .frame(width: layout.canvasSize.width, height: layout.canvasSize.height)
                .allowsHitTesting(false)

                ForEach(chart.nodes) { node in
                    MermaidNodeCard(node: node)
                        .position(layout.nodePositions[node.id] ?? .zero)
                }
            }
            .frame(width: layout.canvasSize.width, height: layout.canvasSize.height)
            .padding(12)
        }
        .frame(minHeight: 190, maxHeight: 620)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 7, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 7, style: .continuous)
                .stroke(Color.primary.opacity(0.08), lineWidth: 0.7)
        )
    }

    private func edgePoints(from source: CGPoint, to target: CGPoint) -> (CGPoint, CGPoint) {
        let dx = target.x - source.x
        let dy = target.y - source.y
        let horizontal = abs(dx) > abs(dy)
        let halfWidth = MermaidGraphLayout.nodeSize.width / 2
        let halfHeight = MermaidGraphLayout.nodeSize.height / 2

        if horizontal {
            let direction = dx >= 0 ? CGFloat(1) : -1
            return (
                CGPoint(x: source.x + direction * halfWidth, y: source.y),
                CGPoint(x: target.x - direction * halfWidth, y: target.y)
            )
        }

        let direction = dy >= 0 ? CGFloat(1) : -1
        return (
            CGPoint(x: source.x, y: source.y + direction * halfHeight),
            CGPoint(x: target.x, y: target.y - direction * halfHeight)
        )
    }
}

private struct MermaidNodeCard: View {
    let node: MermaidNode

    private var accent: Color {
        if node.label.hasPrefix("模块：") { return .accentColor }
        if node.label.hasPrefix("风险：") { return .orange }
        if node.label.hasPrefix("核对：") { return .green }
        if node.label.hasPrefix("依赖：") { return .purple }
        if node.label.hasPrefix("代码：") { return .blue }
        return .secondary
    }

    var body: some View {
        HStack(spacing: 9) {
            Capsule()
                .fill(accent)
                .frame(width: 3, height: 42)

            Text(node.label)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(.primary)
                .multilineTextAlignment(.leading)
                .lineLimit(3)
                .minimumScaleFactor(0.82)
                .fixedSize(horizontal: false, vertical: true)

            Spacer(minLength: 0)
        }
        .padding(.horizontal, 10)
        .frame(
            width: MermaidGraphLayout.nodeSize.width,
            height: MermaidGraphLayout.nodeSize.height,
            alignment: .leading
        )
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 9, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 9, style: .continuous)
                .stroke(accent.opacity(0.32), lineWidth: 0.8)
        )
        .shadow(color: .black.opacity(0.06), radius: 4, y: 2)
        .accessibilityLabel(node.label)
    }
}

private enum MermaidDirection {
    case topDown
    case leftRight
}

private struct MermaidNode: Identifiable, Hashable {
    let id: String
    let label: String
}

private struct MermaidEdge: Identifiable, Hashable {
    let from: String
    let to: String
    let dashed: Bool

    var id: String {
        "\(from)->\(to)-\(dashed)"
    }
}

private struct MermaidChart: Identifiable {
    let id: String
    let title: String
    let direction: MermaidDirection
    let nodes: [MermaidNode]
    let edges: [MermaidEdge]
}

private struct MermaidGraphLayout {
    static let nodeSize = CGSize(width: 220, height: 74)

    let nodePositions: [String: CGPoint]
    let canvasSize: CGSize

    init(chart: MermaidChart) {
        let components = Self.connectedComponents(chart: chart)
        let orderedComponents = components.map { Self.orderedNodes($0, chart: chart) }
        let gap: CGFloat = 26

        if chart.direction == .leftRight {
            let componentWidth = max(
                260,
                CGFloat(orderedComponents.map(\.count).max() ?? 1) * (Self.nodeSize.width + 18) + 30
            )
            let componentHeight = Self.nodeSize.height + 40
            let columns = min(3, max(1, orderedComponents.count))
            let rows = Int(ceil(Double(max(1, orderedComponents.count)) / Double(columns)))
            let canvasWidth = max(620, CGFloat(columns) * componentWidth + gap * CGFloat(columns - 1))
            let canvasHeight = max(180, CGFloat(rows) * componentHeight + gap * CGFloat(rows - 1))
            var positions: [String: CGPoint] = [:]

            for (index, component) in orderedComponents.enumerated() {
                let column = index % columns
                let row = index / columns
                let gridWidth = CGFloat(columns) * componentWidth + gap * CGFloat(columns - 1)
                let originX = (canvasWidth - gridWidth) / 2 + CGFloat(column) * (componentWidth + gap)
                let centerY = CGFloat(row) * (componentHeight + gap) + componentHeight / 2
                for (nodeIndex, nodeID) in component.enumerated() {
                    let centerX = originX + 15 + Self.nodeSize.width / 2
                        + CGFloat(nodeIndex) * (Self.nodeSize.width + 18)
                    positions[nodeID] = CGPoint(x: centerX, y: centerY)
                }
            }

            nodePositions = positions
            canvasSize = CGSize(width: canvasWidth, height: canvasHeight)
            return
        }

        let fanOut = orderedComponents.map { Self.isFanOut($0, chart: chart) }
        let tileWidth: CGFloat = fanOut.contains(true) ? 480 : 248
        let componentRows = orderedComponents.enumerated().map { index, component in
            if fanOut[index] {
                return 1 + Int(ceil(Double(max(0, component.count - 1)) / 2))
            }
            return component.count
        }
        let largestComponent = componentRows.max() ?? 1
        let tileHeight = CGFloat(largestComponent) * (Self.nodeSize.height + 16) + 34
        let columns = orderedComponents.count > 8
            ? 2
            : min(3, max(1, orderedComponents.count))
        let rows = Int(ceil(Double(max(1, orderedComponents.count)) / Double(columns)))
        let canvasWidth = max(620, CGFloat(columns) * tileWidth + gap * CGFloat(columns - 1))
        let canvasHeight = max(180, CGFloat(rows) * tileHeight + gap * CGFloat(rows - 1))
        let gridWidth = CGFloat(columns) * tileWidth + gap * CGFloat(columns - 1)
        let horizontalInset = (canvasWidth - gridWidth) / 2
        var positions: [String: CGPoint] = [:]

        for (index, component) in orderedComponents.enumerated() {
            let column = index % columns
            let row = index / columns
            let centerX = horizontalInset + CGFloat(column) * (tileWidth + gap) + tileWidth / 2
            let top = CGFloat(row) * (tileHeight + gap) + 17
            if fanOut[index] {
                if let root = component.first {
                    positions[root] = CGPoint(
                        x: centerX,
                        y: top + Self.nodeSize.height / 2
                    )
                }
                let childGap: CGFloat = 12
                let childWidth = Self.nodeSize.width * 2 + childGap
                let childOrigin = centerX - childWidth / 2
                for (childIndex, nodeID) in component.dropFirst().enumerated() {
                    let childColumn = childIndex % 2
                    let childRow = childIndex / 2
                    positions[nodeID] = CGPoint(
                        x: childOrigin + Self.nodeSize.width / 2
                            + CGFloat(childColumn) * (Self.nodeSize.width + childGap),
                        y: top + Self.nodeSize.height + 16 + Self.nodeSize.height / 2
                            + CGFloat(childRow) * (Self.nodeSize.height + 16)
                    )
                }
            } else {
                for (nodeIndex, nodeID) in component.enumerated() {
                    positions[nodeID] = CGPoint(
                        x: centerX,
                        y: top + Self.nodeSize.height / 2
                            + CGFloat(nodeIndex) * (Self.nodeSize.height + 16)
                    )
                }
            }
        }

        nodePositions = positions
        canvasSize = CGSize(width: canvasWidth, height: canvasHeight)
    }

    private static func isFanOut(_ component: [String], chart: MermaidChart) -> Bool {
        guard let root = component.first, component.count > 2 else { return false }
        let componentSet = Set(component)
        let outgoing = chart.edges.filter {
            $0.from == root && componentSet.contains($0.to)
        }
        guard Set(outgoing.map(\.to)).count == component.count - 1 else {
            return false
        }
        return !chart.edges.contains {
            componentSet.contains($0.from) && $0.from != root
        }
    }

    private static func connectedComponents(chart: MermaidChart) -> [[String]] {
        var neighbors: [String: Set<String>] = [:]
        for node in chart.nodes {
            neighbors[node.id] = []
        }
        for edge in chart.edges {
            neighbors[edge.from, default: []].insert(edge.to)
            neighbors[edge.to, default: []].insert(edge.from)
        }

        var remaining = Set(chart.nodes.map(\.id))
        var components: [[String]] = []
        for node in chart.nodes where remaining.contains(node.id) {
            var queue = [node.id]
            var component: [String] = []
            remaining.remove(node.id)
            while let current = queue.first {
                queue.removeFirst()
                component.append(current)
                for neighbor in neighbors[current, default: []] where remaining.contains(neighbor) {
                    remaining.remove(neighbor)
                    queue.append(neighbor)
                }
            }
            let order = Dictionary(uniqueKeysWithValues: chart.nodes.enumerated().map { ($0.element.id, $0.offset) })
            components.append(component.sorted { (order[$0] ?? 0) < (order[$1] ?? 0) })
        }
        return components
    }

    private static func orderedNodes(_ component: [String], chart: MermaidChart) -> [String] {
        let componentSet = Set(component)
        let incoming = Set(
            chart.edges
                .filter { componentSet.contains($0.from) && componentSet.contains($0.to) }
                .map(\.to)
        )
        let roots = component.filter { !incoming.contains($0) }
        var ordered: [String] = []
        var visited = Set<String>()
        var queue = roots.isEmpty ? [component[0]] : roots

        while let current = queue.first {
            queue.removeFirst()
            guard !visited.contains(current) else { continue }
            visited.insert(current)
            ordered.append(current)
            for edge in chart.edges where edge.from == current && componentSet.contains(edge.to) {
                if !visited.contains(edge.to) {
                    queue.append(edge.to)
                }
            }
        }

        for node in component where !visited.contains(node) {
            ordered.append(node)
        }
        return ordered
    }
}

private struct WorklogDocument {
    let charts: [MermaidChart]
    let missingFile: Bool

    static let empty = WorklogDocument(charts: [], missingFile: false)

    static func load(workspace: String) -> WorklogDocument {
        // RealtimeProjectDetailView is state-driven. Keep this compatibility
        // entry inert so legacy callers cannot reintroduce file parsing.
        _ = workspace
        return WorklogDocument(charts: [], missingFile: false)
    }
}

private enum MermaidChartParser {
    static func parse(lines: [String], title: String, index: Int) -> MermaidChart? {
        guard let header = lines.first(where: { !$0.trimmingCharacters(in: .whitespaces).isEmpty }) else {
            return nil
        }
        let parts = header.trimmingCharacters(in: .whitespaces).split(whereSeparator: { $0.isWhitespace })
        guard parts.count >= 2 else { return nil }
        let kind = parts[0].lowercased()
        guard kind == "flowchart" || kind == "graph" else { return nil }

        let direction: MermaidDirection
        switch parts[1].uppercased() {
        case "LR", "RL": direction = .leftRight
        case "TD", "TB": direction = .topDown
        default: return nil
        }

        let declaration = try? NSRegularExpression(
            pattern: #"([A-Za-z][A-Za-z0-9_]*)\s*\[\s*(?:\"([^\"]*)\"|'([^']*)')\s*\]"#,
            options: []
        )
        let bareEdge = try? NSRegularExpression(
            pattern: #"([A-Za-z][A-Za-z0-9_]*)\s*(-\.->|-->|==>|---)\s*([A-Za-z][A-Za-z0-9_]*)"#,
            options: []
        )
        guard let declaration, let bareEdge else { return nil }

        var nodes: [MermaidNode] = []
        var nodeIDs = Set<String>()
        var edges: [MermaidEdge] = []
        var edgeIDs = Set<String>()

        func text(in line: String, range: NSRange) -> String {
            guard let swiftRange = Range(range, in: line) else { return "" }
            return String(line[swiftRange])
        }

        func addNode(id: String, label: String) {
            guard !id.isEmpty else { return }
            if nodeIDs.insert(id).inserted {
                nodes.append(MermaidNode(id: id, label: label.isEmpty ? id : label))
            }
        }

        func addEdge(from: String, to: String, dashed: Bool) {
            guard !from.isEmpty, !to.isEmpty, from != to else { return }
            let edge = MermaidEdge(from: from, to: to, dashed: dashed)
            if edgeIDs.insert(edge.id).inserted {
                edges.append(edge)
            }
        }

        func addBareEdgeIfPresent(in line: String) {
            guard let match = bareEdge.firstMatch(in: line, range: NSRange(line.startIndex..., in: line)) else {
                return
            }
            let from = text(in: line, range: match.range(at: 1))
            let operatorText = text(in: line, range: match.range(at: 2))
            let to = text(in: line, range: match.range(at: 3))
            addNode(id: from, label: from)
            addNode(id: to, label: to)
            addEdge(from: from, to: to, dashed: operatorText.contains("."))
        }

        for line in lines.dropFirst() {
            let matches = declaration.matches(in: line, range: NSRange(line.startIndex..., in: line))
            if !matches.isEmpty {
                for match in matches {
                    let id = text(in: line, range: match.range(at: 1))
                    let doubleQuoted = text(in: line, range: match.range(at: 2))
                    let singleQuoted = text(in: line, range: match.range(at: 3))
                    addNode(id: id, label: doubleQuoted.isEmpty ? singleQuoted : doubleQuoted)
                }

                if matches.count > 1 {
                    for pair in zip(matches.dropLast(), matches.dropFirst()) {
                        let from = text(in: line, range: pair.0.range(at: 1))
                        let to = text(in: line, range: pair.1.range(at: 1))
                        let start = pair.0.range.location + pair.0.range.length
                        let end = pair.1.range.location
                        let operatorText = text(
                            in: line,
                            range: NSRange(location: start, length: max(0, end - start))
                        )
                        addEdge(from: from, to: to, dashed: operatorText.contains("."))
                    }
                }
                addBareEdgeIfPresent(in: line)
                continue
            }

            addBareEdgeIfPresent(in: line)
        }

        guard !nodes.isEmpty else { return nil }
        return MermaidChart(
            id: "\(title)-\(index)",
            title: title,
            direction: direction,
            nodes: nodes,
            edges: edges
        )
    }
}

private struct GraphCenterNode: View {
    let summary: StateSummary

    var body: some View {
        VStack(spacing: 4) {
            Image(systemName: "dot.radiowaves.left.and.right")
                .font(.system(size: 18, weight: .semibold))
                .foregroundStyle(.tint)
            Text("全局监听")
                .font(.system(size: 14, weight: .bold, design: .rounded))
            Text("\(summary.activeProjects)/\(summary.totalProjects) 活跃")
                .font(.system(size: 10, weight: .medium, design: .monospaced))
                .foregroundStyle(.secondary)
        }
        .frame(width: 148, height: 82)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .stroke(Color.accentColor.opacity(0.3), lineWidth: 1)
        )
        .shadow(color: .black.opacity(0.08), radius: 12, y: 5)
    }
}

private struct MonitorGraphNode: View {
    let project: ProjectSummary
    let selected: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 7) {
                    Circle()
                        .fill(statusColor(project.status, active: project.active))
                        .frame(width: 8, height: 8)
                    Text(project.displayName)
                        .font(.system(size: 12, weight: .bold, design: .rounded))
                        .lineLimit(1)
                    Spacer(minLength: 0)
                    Image(systemName: project.active ? "waveform.path.ecg" : "pause.fill")
                        .font(.system(size: 9, weight: .bold))
                        .foregroundStyle(.secondary)
                }
                Text(project.focus.isEmpty ? project.status : project.focus)
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                HStack {
                    Text(project.active ? "监听中" : "最近状态")
                    Spacer()
                    Text("\(project.eventCount) 条")
                }
                .font(.system(size: 9, design: .monospaced))
                .foregroundStyle(.secondary)
            }
            .padding(11)
            .frame(width: 164, height: 84, alignment: .topLeading)
        }
        .buttonStyle(.plain)
        .background(
            selected ? Color.accentColor.opacity(0.18) : Color.primary.opacity(0.06),
            in: RoundedRectangle(cornerRadius: 12, style: .continuous)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .stroke(
                    selected ? Color.accentColor.opacity(0.6) : Color.primary.opacity(0.1),
                    lineWidth: selected ? 1.2 : 0.7
                )
        )
        .contentShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}

private struct EmptyStateView: View {
    let title: String
    let systemImage: String

    var body: some View {
        VStack(spacing: 10) {
            Image(systemName: systemImage)
                .font(.system(size: 28))
                .foregroundStyle(.secondary)
            Text(title)
                .font(.headline)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
    }
}

private struct Metric: View {
    let title: String
    let value: String

    var body: some View {
        VStack(alignment: .trailing, spacing: 2) {
            Text(value)
                .font(.title3.weight(.semibold))
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .frame(minWidth: 44)
    }
}

private struct ProjectRow: View {
    let project: ProjectSummary
    let selected: Bool

    var body: some View {
        HStack(spacing: 10) {
            Circle()
                .fill(statusColor(project.status, active: project.active))
                .frame(width: 9, height: 9)
            VStack(alignment: .leading, spacing: 4) {
                Text(project.displayName)
                    .font(.headline)
                    .lineLimit(1)
                Text(project.focus.isEmpty ? project.status : project.focus)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            Spacer()
            Text("\(project.eventCount)")
                .font(.caption.monospacedDigit())
                .foregroundStyle(.secondary)
        }
        .padding(11)
        .background(selected ? Color.accentColor.opacity(0.16) : Color.primary.opacity(0.045), in: RoundedRectangle(cornerRadius: 8))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(selected ? Color.accentColor.opacity(0.45) : .clear, lineWidth: 1)
        )
    }
}

private struct ProjectDetail: View {
    let project: ProjectSummary

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 5) {
                        Text(project.displayName)
                            .font(.title2.weight(.bold))
                        Text(project.workspace)
                            .font(.caption.monospaced())
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                    }
                    Spacer()
                    Label(project.status, systemImage: project.active ? "bolt.fill" : "clock")
                        .foregroundStyle(statusColor(project.status, active: project.active))
                }

                if !project.phase.isEmpty {
                    DetailBlock(title: "阶段", text: project.phase)
                }
                if !project.focus.isEmpty {
                    DetailBlock(title: "当前关注", text: project.focus)
                }
                if !project.note.isEmpty {
                    DetailBlock(title: "最近摘要", text: project.note)
                }

                Text("最近事件")
                    .font(.headline)
                if project.recentEvents.isEmpty {
                    Text("暂无事件")
                        .foregroundStyle(.secondary)
                } else {
                    VStack(alignment: .leading, spacing: 0) {
                        ForEach(project.recentEvents) { event in
                            EventRow(event: event)
                            if event.id != project.recentEvents.last?.id {
                                Divider().padding(.leading, 28)
                            }
                        }
                    }
                    .background(Color.primary.opacity(0.045), in: RoundedRectangle(cornerRadius: 8))
                }
            }
            .padding(24)
        }
    }
}

private struct DetailBlock: View {
    let title: String
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title)
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            Text(text)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

private struct EventRow: View {
    let event: EventSummary

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: icon(for: event.eventType))
                .foregroundStyle(statusColor(event.status, active: true))
                .frame(width: 18)
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(event.phase.isEmpty ? event.eventType : event.phase)
                        .font(.subheadline.weight(.semibold))
                    Spacer()
                    Text(shortActivityTime(event.timestamp))
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
                if !event.note.isEmpty {
                    Text(event.note)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(12)
    }

    private func icon(for type: String) -> String {
        switch type {
        case "file-change": return "doc.text"
        case "validation": return "checkmark.seal"
        case "blocker": return "exclamationmark.triangle"
        default: return "circle.dotted"
        }
    }
}
