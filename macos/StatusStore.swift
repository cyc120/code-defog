import Foundation
import SwiftUI

struct ServiceConfiguration: Codable {
    let host: String
    let port: Int
    let token: String
    let statePath: String?
    let updatedAt: String?

    enum CodingKeys: String, CodingKey {
        case host, port, token
        case statePath = "state_path"
        case updatedAt = "updated_at"
    }
}

struct StateSummary: Codable {
    let totalProjects: Int
    let activeProjects: Int
    let blockedProjects: Int
    let eventCount: Int
}

struct EventSummary: Codable, Identifiable {
    let id: String
    let conversationId: String?
    let eventType: String
    let source: String
    let timestamp: String
    let phase: String
    let status: String
    let focus: String
    let note: String
    let evidence: String
    let files: [String]
}

struct ProjectSummary: Codable, Identifiable {
    let workspace: String
    let conversationId: String?
    let conversationName: String?
    let name: String
    let status: String
    let phase: String
    let focus: String
    let note: String
    let evidence: String
    let eventType: String
    let updatedAt: String
    let eventCount: Int
    let active: Bool
    let recentEvents: [EventSummary]

    var sessionID: String {
        conversationId?.isEmpty == false ? conversationId! : "default"
    }

    var conversationLabel: String {
        if let conversationName, !conversationName.isEmpty {
            return conversationName
        }
        guard sessionID != "default" else { return "默认会话" }
        return "对话 \(String(sessionID.suffix(8)))"
    }

    var displayName: String {
        sessionID == "default" ? name : "\(name) · \(conversationLabel)"
    }

    var id: String { "\(workspace)|\(sessionID)" }
}

struct GlobalState: Codable {
    let generatedAt: String
    let summary: StateSummary
    let projects: [ProjectSummary]

    static let empty = GlobalState(
        generatedAt: "",
        summary: StateSummary(totalProjects: 0, activeProjects: 0, blockedProjects: 0, eventCount: 0),
        projects: []
    )

    // generatedAt is recalculated by every /api/state response. Use event content
    // instead, so polling does not retrigger the floating bubble by itself.
    var contentID: String {
        GlobalState.contentID(for: projects)
    }

    static func contentID(for projects: [ProjectSummary]) -> String {
        projects.map { project in
            let eventID = project.recentEvents.first?.id ?? ""
            return [
                project.workspace,
                project.sessionID,
                project.conversationName ?? "",
                project.updatedAt,
                String(project.eventCount),
                project.status,
                project.phase,
                project.focus,
                project.note,
                project.evidence,
                project.eventType,
                project.active ? "1" : "0",
                eventID
            ]
            .map { "\($0.utf8.count):\($0)" }
            .joined(separator: "|")
        }
        .joined(separator: ";")
    }
}

struct ActivitySummary {
    let projectName: String
    let status: String
    let phase: String
    let focus: String
    let note: String
    let timestamp: String
    let active: Bool
}

struct StreamEnvelope: Codable {
    let type: String
    let state: GlobalState
}

struct ManagementInfo: Codable {
    let pid: Int
    let host: String
    let port: Int
    let retention: Int
    let statePath: String
    let totalSessions: Int
    let totalEvents: Int
    let dbBytes: Int
    let uptimeSeconds: Double
}

private enum StreamError: Error {
    case invalidResponse
}

@MainActor
final class StatusStore: ObservableObject {
    static let shared = StatusStore()

    @Published private(set) var state = GlobalState.empty
    @Published private(set) var connected = false
    @Published private(set) var isListening = false
    @Published private(set) var lastStateReceivedAt: Date?
    @Published private(set) var managementInfo: ManagementInfo?
    @Published private(set) var preferencesRevision = 0

    private var streamTask: Task<Void, Never>?
    private var pollingTask: Task<Void, Never>?
    private var streamConnected = false
    private var pollConnected = false
    private var cachedConfig: ServiceConfiguration?
    private var cachedConfigModificationDate: Date?
    private var streamRetryDelay: TimeInterval = 1

    init() {
        Task { @MainActor [weak self] in
            self?.startMonitoring()
        }
    }

    deinit {
        streamTask?.cancel()
        pollingTask?.cancel()
    }

    var stateID: String {
        GlobalState.contentID(for: visibleProjects)
    }

    var connectionSummary: String {
        if isListening { return "实时监听中" }
        if connected { return "轮询同步中" }
        return "等待后台服务"
    }

    var pillTitle: String {
        guard connected else { return "CCTV 未连接" }
        if isMuteAll { return "CCTV 已静音" }
        return "CCTV \(visibleSummary.activeProjects)/\(visibleSummary.totalProjects)"
    }

    var latestActivity: ActivitySummary? {
        guard let project = visibleProjects.max(by: { $0.updatedAt < $1.updatedAt }) else { return nil }
        let event = project.recentEvents.first
        return ActivitySummary(
            projectName: project.displayName,
            status: event.map { $0.status.isEmpty ? project.status : $0.status } ?? project.status,
            phase: event.map { $0.phase.isEmpty ? project.phase : $0.phase } ?? project.phase,
            focus: event.map { $0.focus.isEmpty ? project.focus : $0.focus } ?? project.focus,
            note: event.map { $0.note.isEmpty ? project.note : $0.note } ?? project.note,
            timestamp: event.map { $0.timestamp.isEmpty ? project.updatedAt : $0.timestamp } ?? project.updatedAt,
            active: project.active
        )
    }

    var visibleProjects: [ProjectSummary] {
        guard !isMuteAll else { return [] }
        let muted = mutedSessionIDs
        return state.projects.filter { !muted.contains($0.id) }
    }

    var visibleSummary: StateSummary {
        StateSummary(
            totalProjects: visibleProjects.count,
            activeProjects: visibleProjects.filter { $0.active }.count,
            blockedProjects: visibleProjects.filter {
                $0.status.contains("阻塞") || $0.status.lowercased().contains("blocked")
            }.count,
            eventCount: visibleProjects.reduce(0) { $0 + $1.eventCount }
        )
    }

    var defaultStatePath: String {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/CodeCCTV/state.sqlite3")
            .path
    }

    private static let mutedSessionsKey = "CodeCCTV.mutedSessions"
    private static let muteAllKey = "CodeCCTV.muteAll"
    static let bubbleAutoCollapseKey = "CodeCCTV.bubbleAutoCollapseSeconds"

    var isMuteAll: Bool {
        UserDefaults.standard.bool(forKey: Self.muteAllKey)
    }

    private var mutedSessionIDs: Set<String> {
        Set(UserDefaults.standard.stringArray(forKey: Self.mutedSessionsKey) ?? [])
    }

    func isMuted(_ projectID: String) -> Bool {
        isMuteAll || mutedSessionIDs.contains(projectID)
    }

    func setMuted(_ muted: Bool, for projectID: String) {
        var ids = mutedSessionIDs
        if muted {
            ids.insert(projectID)
        } else {
            ids.remove(projectID)
        }
        UserDefaults.standard.set(Array(ids).sorted(), forKey: Self.mutedSessionsKey)
        preferencesRevision += 1
    }

    func setMuteAll(_ muted: Bool) {
        UserDefaults.standard.set(muted, forKey: Self.muteAllKey)
        preferencesRevision += 1
    }

    static var bubbleAutoCollapseSeconds: TimeInterval {
        get {
            UserDefaults.standard.object(forKey: bubbleAutoCollapseKey) as? TimeInterval ?? 8
        }
        set {
            UserDefaults.standard.set(newValue, forKey: bubbleAutoCollapseKey)
        }
    }

    func refreshManagementInfo() async {
        guard let configuration = loadConfiguration(),
              let url = URL(string: "http://\(configuration.host):\(configuration.port)/api/management/info") else {
            return
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 6
        request.setValue(configuration.token, forHTTPHeaderField: "X-Code-CCTV-Token")
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse,
                  (200..<300).contains(http.statusCode) else { return }
            let decoder = JSONDecoder()
            decoder.keyDecodingStrategy = .convertFromSnakeCase
            managementInfo = try decoder.decode(ManagementInfo.self, from: data)
        } catch {
            managementInfo = nil
        }
    }

    func clearSession(_ project: ProjectSummary) async {
        await postManagement(action: "session/clear", payload: [
            "workspace": project.workspace,
            "conversation_id": project.sessionID,
        ])
    }

    func clearAllSessions() async {
        await postManagement(action: "clear-all", payload: [:])
    }

    private func postManagement(action: String, payload: [String: String]) async {
        guard let configuration = loadConfiguration(),
              let url = URL(string: "http://\(configuration.host):\(configuration.port)/api/management/\(action)") else {
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 6
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(configuration.token, forHTTPHeaderField: "X-Code-CCTV-Token")
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)
        do {
            _ = try await URLSession.shared.data(for: request)
        } catch {
            // State refresh arrives through SSE/polling; management failures are silent.
        }
    }

    private func startMonitoring() {
        streamTask?.cancel()
        pollingTask?.cancel()

        streamTask = Task { @MainActor [weak self] in
            guard let self else { return }
            while !Task.isCancelled {
                let connected = await self.consumeStreamOnce()
                if Task.isCancelled { break }
                if connected {
                    self.streamRetryDelay = 1
                } else {
                    self.streamRetryDelay = min(self.streamRetryDelay * 2, 10)
                }
                if !Task.isCancelled {
                    try? await Task.sleep(
                        nanoseconds: UInt64(self.streamRetryDelay * 1_000_000_000)
                    )
                }
            }
        }

        pollingTask = Task { @MainActor [weak self] in
            guard let self else { return }
            while !Task.isCancelled {
                // Polling is only the SSE fallback: skip it entirely while a
                // live stream is connected to avoid a request every 5 s.
                if !self.streamConnected {
                    await self.pollStateOnce()
                }
                if !Task.isCancelled {
                    try? await Task.sleep(nanoseconds: 5_000_000_000)
                }
            }
        }
    }

    private func consumeStreamOnce() async -> Bool {
        guard let configuration = loadConfiguration(),
              let url = URL(string: "http://\(configuration.host):\(configuration.port)/api/stream") else {
            setStreamConnected(false)
            return false
        }

        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.timeoutInterval = 30
        request.setValue(configuration.token, forHTTPHeaderField: "X-Code-CCTV-Token")

        do {
            let (bytes, response) = try await URLSession.shared.bytes(for: request)
            guard let httpResponse = response as? HTTPURLResponse,
                  (200..<300).contains(httpResponse.statusCode) else {
                throw StreamError.invalidResponse
            }

            setStreamConnected(true)
            for try await line in bytes.lines {
                if Task.isCancelled { break }
                // Heartbeat comments also prove that the SSE connection is alive.
                setStreamConnected(true)
                guard line.hasPrefix("data:") else { continue }
                let json = line.dropFirst(5).trimmingCharacters(in: .whitespaces)
                guard let data = json.data(using: .utf8) else { continue }
                let decoder = JSONDecoder()
                decoder.keyDecodingStrategy = .convertFromSnakeCase
                guard let envelope = try? decoder.decode(StreamEnvelope.self, from: data),
                      envelope.type == "state" else { continue }
                applyState(envelope.state)
            }
            setStreamConnected(false)
            return true
        } catch {
            setStreamConnected(false)
            return false
        }
    }

    private func pollStateOnce() async {
        guard let configuration = loadConfiguration(),
              let url = URL(string: "http://\(configuration.host):\(configuration.port)/api/state") else {
            setPollConnected(false)
            return
        }

        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.timeoutInterval = 6
        request.setValue(configuration.token, forHTTPHeaderField: "X-Code-CCTV-Token")

        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let httpResponse = response as? HTTPURLResponse,
                  (200..<300).contains(httpResponse.statusCode) else {
                throw StreamError.invalidResponse
            }
            let decoder = JSONDecoder()
            decoder.keyDecodingStrategy = .convertFromSnakeCase
            let nextState = try decoder.decode(GlobalState.self, from: data)
            setPollConnected(true)
            applyState(nextState)
        } catch {
            setPollConnected(false)
        }
    }

    private func applyState(_ nextState: GlobalState) {
        guard nextState.contentID != state.contentID else { return }
        state = nextState
        lastStateReceivedAt = Date()
    }

    private func setStreamConnected(_ value: Bool) {
        streamConnected = value
        isListening = value
        refreshConnection()
    }

    private func setPollConnected(_ value: Bool) {
        pollConnected = value
        refreshConnection()
    }

    private func refreshConnection() {
        let next = streamConnected || pollConnected
        guard connected != next else { return }
        connected = next
    }

    private func loadConfiguration() -> ServiceConfiguration? {
        let path = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/CodeCCTV/service.json")
        let attributes = try? FileManager.default.attributesOfItem(atPath: path.path)
        let modification = attributes?[.modificationDate] as? Date
        if let cachedConfig,
           modification == cachedConfigModificationDate,
           cachedConfigModificationDate != nil {
            return cachedConfig
        }
        guard let data = try? Data(contentsOf: path) else {
            cachedConfig = nil
            cachedConfigModificationDate = nil
            return nil
        }
        let configuration = try? JSONDecoder().decode(ServiceConfiguration.self, from: data)
        cachedConfig = configuration
        cachedConfigModificationDate = modification ?? Date()
        return configuration
    }
}

func statusColor(_ status: String, active: Bool = true) -> Color {
    if status.contains("阻塞") || status.localizedCaseInsensitiveContains("blocked") {
        return .red
    }
    if status.contains("风险") || status.localizedCaseInsensitiveContains("warning") {
        return .orange
    }
    if active || status.contains("监听") || status.localizedCaseInsensitiveContains("watch") {
        return .green
    }
    return .secondary
}
