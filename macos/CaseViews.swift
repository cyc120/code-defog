// Code CCTV DevLoop — Case queue + detail views (P5).
//
// CaseListView shows the DevLoop Case queue with a status filter; selecting a
// row loads the full evidence bundle and, when the Case is in an approval
// state, exposes Approve / Reject buttons that issue a one-shot approval
// grant (service token) and consume it (approval token type).

import SwiftUI

// ── Case list ────────────────────────────────────────────────────────────

struct CaseListView: View {
    @ObservedObject var store: StatusStore
    @State private var selectedCaseID: String?
    @State private var statusFilter: String?

    private let statuses = [
        "RECEIVED", "TRIAGED", "DIAGNOSED", "PLAN_APPROVAL", "REPAIRING",
        "VERIFYING", "PATCH_REJECTED", "RELEASE_APPROVAL", "RELEASED",
        "ROLLED_BACK", "ESCALATED", "CLOSED",
    ]

    var body: some View {
        VStack(spacing: 0) {
            filterBar
            Divider()
            HStack(spacing: 0) {
                caseList
                    .frame(width: 360)
                Divider()
                if let id = selectedCaseID, let caseSummary = store.cases.first(where: { $0.id == id }) {
                    CaseDetailView(store: store, caseSummary: caseSummary)
                } else {
                    emptyDetail
                }
            }
        }
        .task {
            await store.loadCases(status: statusFilter)
        }
        .onChange(of: store.casesRevision) {
            Task { await store.loadCases(status: statusFilter) }
        }
        .onChange(of: statusFilter) { _, newValue in
            Task { await store.loadCases(status: newValue) }
        }
    }

    private var filterBar: some View {
        HStack(spacing: 12) {
            Text("状态筛选")
                .font(.subheadline)
                .foregroundStyle(.secondary)
            Picker("状态", selection: $statusFilter) {
                Text("全部").tag(String?.none)
                ForEach(statuses, id: \.self) { status in
                    Text(status).tag(String?.some(status))
                }
            }
            .frame(width: 160)
            Button {
                Task { await store.loadCases(status: statusFilter) }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .help("刷新")
            Spacer()
            if store.casesLoading {
                ProgressView().controlSize(.small)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
    }

    private var caseList: some View {
        ScrollView {
            LazyVStack(spacing: 0) {
                ForEach(store.cases) { caseSummary in
                    CaseRow(caseSummary: caseSummary, isSelected: caseSummary.id == selectedCaseID)
                        .contentShape(Rectangle())
                        .onTapGesture {
                            selectedCaseID = caseSummary.id
                        }
                }
            }
        }
        .overlay {
            if store.cases.isEmpty && !store.casesLoading {
                Text("没有 Case")
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var emptyDetail: some View {
        VStack {
            Spacer()
            Text("选择一个 Case 查看详情")
                .foregroundStyle(.secondary)
            Spacer()
        }
        .frame(maxWidth: .infinity)
    }
}

private struct CaseRow: View {
    let caseSummary: CaseSummary
    let isSelected: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Circle()
                    .fill(caseStatusColor(caseSummary.status))
                    .frame(width: 8, height: 8)
                Text(caseSummary.title ?? caseSummary.id)
                    .font(.system(size: 13, weight: .semibold))
                    .lineLimit(1)
                Spacer()
                Text(caseSummary.status)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
            }
            HStack {
                Text("\(caseSummary.priority) · \(caseSummary.sourceCount) 来源")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                Spacer()
                Text(caseSummary.updatedAt)
                    .font(.system(size: 10))
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(isSelected ? Color.accentColor.opacity(0.12) : Color.clear)
    }
}

// ── Case detail ───────────────────────────────────────────────────────────

struct CaseDetailView: View {
    @ObservedObject var store: StatusStore
    let caseSummary: CaseSummary
    @State private var evidence: EvidenceBundle?
    @State private var errorMessage: String?
    @State private var approver = NSUserName()
    @State private var approving = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                overviewCard
                if requiresApproval {
                    approvalCard
                }
                evidenceSections
            }
            .padding(16)
        }
        .task {
            evidence = await store.getCaseEvidence(caseSummary.id)
        }
        .onChange(of: store.casesRevision) {
            Task { evidence = await store.getCaseEvidence(caseSummary.id) }
        }
    }

    private var requiresApproval: Bool {
        caseSummary.status == "PLAN_APPROVAL" || caseSummary.status == "RELEASE_APPROVAL"
    }

    private var overviewCard: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(caseSummary.title ?? caseSummary.id)
                    .font(.title3.weight(.bold))
                Spacer()
                Text(caseSummary.status)
                    .font(.caption.weight(.semibold))
                    .padding(.horizontal, 8).padding(.vertical, 3)
                    .background(caseStatusColor(caseSummary.status).opacity(0.2))
                    .clipShape(Capsule())
            }
            DetailRow(label: "Case ID", value: caseSummary.id)
            DetailRow(label: "优先级", value: caseSummary.priority)
            DetailRow(label: "风险", value: caseSummary.riskLevel)
            DetailRow(label: "仓库", value: caseSummary.repositoryRef)
            if let baseCommit = caseSummary.baseCommit {
                DetailRow(label: "base_commit", value: baseCommit)
            }
            if let patchRef = caseSummary.patchRef {
                DetailRow(label: "patch_ref", value: patchRef)
            }
            if let pendingAction = caseSummary.pendingAction {
                DetailRow(label: "待审批", value: pendingAction)
            }
            if let traceId = caseSummary.traceId {
                DetailRow(label: "trace", value: traceId)
            }
            DetailRow(label: "创建", value: caseSummary.createdAt)
            if let closedAt = caseSummary.closedAt {
                DetailRow(label: "关闭", value: closedAt)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(.ultraThinMaterial)
        .clipShape(RoundedRectangle(cornerRadius: 10))
    }

    private var approvalCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("人工审批")
                .font(.headline)
            Text("签发一次性审批凭证并消费：目标 \(approvalTargetRef) · 审批人")
                .font(.caption)
                .foregroundStyle(.secondary)
            HStack(spacing: 8) {
                TextField("审批人", text: $approver)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 180)
                Spacer()
                Button {
                    Task { await runApproval(approve: true) }
                } label: {
                    Label("批准", systemImage: "checkmark.circle")
                }
                .disabled(approving)
                Button {
                    Task { await runApproval(approve: false) }
                } label: {
                    Label("拒绝", systemImage: "xmark.circle")
                }
                .disabled(approving)
            }
            if let errorMessage {
                Text(errorMessage)
                    .font(.caption)
                    .foregroundStyle(.red)
            }
        }
        .padding(14)
        .background(.ultraThinMaterial)
        .clipShape(RoundedRectangle(cornerRadius: 10))
    }

    private var approvalAction: (approve: String, reject: String) {
        caseSummary.status == "PLAN_APPROVAL"
            ? ("approve_plan", "reject_plan")
            : ("approve_release", "reject_release")
    }

    private var approvalTargetRef: String {
        caseSummary.status == "PLAN_APPROVAL"
            ? (caseSummary.baseCommit ?? "")
            : (caseSummary.patchRef ?? "")
    }

    private func runApproval(approve: Bool) async {
        approving = true
        defer { approving = false }
        errorMessage = nil
        let (approveAction, rejectAction) = approvalAction
        let action = approve ? approveAction : rejectAction
        let targetRef = approvalTargetRef
        guard !targetRef.isEmpty else {
            errorMessage = "缺少 target_ref（base_commit 或 patch_ref）"
            return
        }
        let approverName = approver.isEmpty ? NSUserName() : approver
        do {
            let grant = try await store.requestApprovalGrant(
                caseID: caseSummary.id, action: action, targetRef: targetRef,
                approver: approverName)
            _ = try await store.postCaseAction(
                caseID: caseSummary.id, action: action,
                approvalToken: grant.approvalToken, targetRef: targetRef,
                reason: "\(approve ? "approve" : "reject") by \(approverName)")
        } catch {
            errorMessage = "审批失败：\(error)"
        }
    }

    @ViewBuilder
    private var evidenceSections: some View {
        if let evidence {
            if !evidence.sources.isEmpty {
                SectionCard(title: "来源 Sources (\(evidence.sources.count))") {
                    ForEach(evidence.sources, id: \.sourceUri) { source in
                        Text("\(source.sourceType ?? "?") | \(source.sourceUri ?? "?") | \(source.receivedAt ?? "")")
                            .font(.system(size: 11))
                    }
                }
            }
            if !evidence.agentRuns.isEmpty {
                SectionCard(title: "Agent 运行 (\(evidence.agentRuns.count))") {
                    ForEach(evidence.agentRuns, id: \.startedAt) { run in
                        Text("\(run.agentId ?? "?") | \(run.status ?? "?") | \(run.startedAt ?? "")")
                            .font(.system(size: 11))
                    }
                }
            }
            if !evidence.toolRuns.isEmpty {
                SectionCard(title: "工具链 (\(evidence.toolRuns.count))") {
                    ForEach(evidence.toolRuns, id: \.chainSequence) { tool in
                        Text("[\(tool.chainSequence ?? 0)] \(tool.toolName ?? "?") | exit \(tool.exitCode.map(String.init) ?? "?")")
                            .font(.system(size: 11))
                    }
                }
            }
            if !evidence.approvals.isEmpty {
                SectionCard(title: "审批 (\(evidence.approvals.count))") {
                    ForEach(evidence.approvals, id: \.resolvedAt) { approval in
                        Text("\(approval.action ?? "?") → \(approval.decision ?? "?") | \(approval.approver ?? "?")")
                            .font(.system(size: 11))
                    }
                }
            }
            if !evidence.artifacts.isEmpty {
                SectionCard(title: "制品 (\(evidence.artifacts.count))") {
                    ForEach(evidence.artifacts, id: \.uri) { artifact in
                        Text("\(artifact.kind ?? "?") | \(artifact.uri ?? "?")")
                            .font(.system(size: 11))
                    }
                }
            }
            if !evidence.knowledgeRecords.isEmpty {
                SectionCard(title: "知识条目 (\(evidence.knowledgeRecords.count))") {
                    ForEach(evidence.knowledgeRecords, id: \.recordId) { record in
                        Text("\(record.status ?? "?") | \(record.reuseTags?.joined(separator: ", ") ?? "")")
                            .font(.system(size: 11))
                    }
                }
            }
            if let retrospective = evidence.retrospective,
               let content = retrospective.report?.content {
                SectionCard(title: "复盘报告") {
                    Text(content)
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                }
            }
        } else {
            Text("加载证据中…")
                .foregroundStyle(.secondary)
        }
    }
}

private struct DetailRow: View {
    let label: String
    let value: String
    var body: some View {
        HStack(alignment: .top) {
            Text(label)
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .frame(width: 90, alignment: .leading)
            Text(value)
                .font(.system(size: 12))
            Spacer()
        }
    }
}

private struct SectionCard<Content: View>: View {
    let title: String
    @ViewBuilder let content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title)
                .font(.subheadline.weight(.semibold))
            VStack(alignment: .leading, spacing: 3) {
                content
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(.ultraThinMaterial)
        .clipShape(RoundedRectangle(cornerRadius: 10))
    }
}

// ── Status colour ─────────────────────────────────────────────────────────

func caseStatusColor(_ status: String) -> Color {
    switch status {
    case "PLAN_APPROVAL", "RELEASE_APPROVAL":
        return .orange
    case "PATCH_REJECTED", "ROLLED_BACK", "ESCALATED":
        return .red
    case "CLOSED":
        return .gray
    case "RECEIVED":
        return .secondary
    default:
        return .green  // active agent states
    }
}
