"""HTTP contract tests for the local service-discovery dashboard."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import urlopen

from daemon.dashboard import DashboardServer
from daemon.service_discovery import LocalServiceDiscoveryAgent


class DashboardServerTests(unittest.TestCase):
    def test_discovery_dashboard_serves_tokenless_picker_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = LocalServiceDiscoveryAgent(Path(directory) / "services")
            server = DashboardServer(("127.0.0.1", 0), agent)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with urlopen(f"{base_url}/ui/config", timeout=1) as response:
                    config = json.loads(response.read())
                    self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))
                self.assertEqual(config, {"ok": True, "mode": "discovery"})

                with urlopen(f"{base_url}/ui/services", timeout=1) as response:
                    payload = json.loads(response.read())
                    self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))
                self.assertEqual(payload["agent"], "local-service-discovery")
                self.assertEqual(payload["services"], [])
                self.assertNotIn("token", json.dumps(payload))

                with urlopen(f"{base_url}/ui", timeout=1) as response:
                    page = response.read().decode("utf-8")
                self.assertIn('id="service-list"', page)
                self.assertIn('id="connect-btn"', page)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=1)


class ConsoleVisualFoundationTests(unittest.TestCase):
    def test_console_uses_semantic_status_tones_and_action_primary(self) -> None:
        console = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("--action-primary-bg", console)
        self.assertIn(".button.primary { border-color: var(--action-primary-bg)", console)
        self.assertIn('.state-badge[data-tone="success"]', console)
        self.assertIn('REPAIRING: { label: "修复中", color: "var(--blue)", tone: "info"', console)
        self.assertIn('RELEASED: { label: "已放行", color: "var(--green)", tone: "success"', console)

        render_case = console[console.index("function renderCase()"):console.index("function renderStateMachine")]
        self.assertIn('$("case-state").dataset.tone = meta.tone || "neutral"', render_case)
        self.assertNotIn('$("case-state").style.background', render_case)

    def test_case_audit_prioritizes_the_current_decision(self) -> None:
        console = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(
            encoding="utf-8"
        )
        audit_markup = console[console.index('<div id="view-audit"'):console.index('aria-labelledby="phase-title"')]
        self.assertLess(audit_markup.index('id="action-desk"'), audit_markup.index('aria-label="Case 摘要"'))
        self.assertIn('$("action-desk").dataset.tone = decision.tone', console)
        self.assertIn('renderAction(null);', console)

    def test_audit_empty_state_is_scoped_to_the_selected_project(self) -> None:
        console = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('id="workspace-empty-state"', console)
        self.assertIn('data-audit-content', console)
        self.assertIn('function clearSelectedProject()', console)
        self.assertIn('$("workspace-empty-cta").addEventListener', console)

        load_cases = console[console.index('async function loadCases'):console.index('let lastEvidenceKey')]
        self.assertIn('if (!repo)', load_cases)
        self.assertIn('resetAuditCaseData();', load_cases)
        self.assertNotIn('"/api/cases?limit=500"', load_cases)

    def test_overview_prioritizes_project_scope_and_review_decision(self) -> None:
        console = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(
            encoding="utf-8"
        )
        overview = console[console.index('<section id="view-overview"'):console.index('<section id="view-code-map"')]
        self.assertIn('id="overview-empty-state"', overview)
        self.assertIn('data-overview-content', overview)
        self.assertLess(overview.index('class="review-command"'), overview.index('id="overview-hero"'))
        self.assertLess(overview.index('id="overview-hero"'), overview.index('class="review-execution"'))
        self.assertIn('id="review-command-state"', overview)

        summary_loader = console[console.index('async function loadProjectSummary'):console.index('function renderOverviewStats')]
        self.assertIn('if (!selectedProject)', summary_loader)
        self.assertIn('query.set("workspace", selectedProject.workspace);', summary_loader)

    def test_root_route_opens_the_overview_without_forcing_project_picker(self) -> None:
        console = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(
            encoding="utf-8"
        )
        router_start = console.index("function applyHash()")
        router = console[router_start:console.index('window.addEventListener("hashchange"', router_start)]
        self.assertIn('setView(hash === "audit" ? "audit"', router)
        self.assertIn(': "overview");', router)
        self.assertIn('<div id="view-audit" hidden>', console)
        self.assertIn('<section id="view-overview" class="overview-view" aria-labelledby="overview-title">', console)
        self.assertIn('id="view-overview-btn"', console)
        self.assertIn('aria-current="page">项目审查</button>', console)

        boot = console[console.index("async function boot()"):console.index("// ── 监控项目")]
        no_projects = boot[boot.index("if (!monitoredProjects.length)"):boot.index("const saved")]
        self.assertIn('history.replaceState(null, "", "#/overview");', no_projects)
        self.assertNotIn("openProjectPicker(true);", no_projects)

    def test_discovery_auto_connects_only_a_unique_verified_loopback_service(self) -> None:
        console = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(
            encoding="utf-8"
        )
        verified = console[
            console.index("function verifiedServiceCandidates"):
            console.index("async function hasHealthyService", console.index("function verifiedServiceCandidates"))
        ]
        self.assertIn('service.status === "ready"', verified)
        self.assertIn("serviceUrlSafe(service)", verified)
        self.assertNotIn('service.status === "ready" || service.status === "legacy"', verified)

        helper = console[
            console.index("async function autoOpenOnlyDiscoveredService"):
            console.index("function showConnectionPicker", console.index("async function autoOpenOnlyDiscoveredService"))
        ]
        self.assertIn('fetch("/ui/services", { cache: "no-store" })', helper)
        self.assertIn("const candidates = verifiedServiceCandidates(payload.services);", helper)
        self.assertIn("if (candidates.length !== 1) return false;", helper)
        self.assertIn("location.replace(candidates[0].ui_url);", helper)

        init = console[console.index("async function initConnection"):console.index('$("manual-connect-form")')]
        self.assertIn("if (await autoOpenOnlyDiscoveredService()) return false;", init)
        self.assertIn("showConnectionPicker(true);", init)
        self.assertIn('if (location.protocol === "file:" && await autoOpenRememberedService()) return false;', init)
        file_branch = init[init.index("// The service token is a bearer credential:"):]
        self.assertNotIn("/ui/config", file_branch)

        remembered = console[
            console.index("async function autoOpenRememberedService"):
            console.index("function serviceState", console.index("async function autoOpenRememberedService"))
        ]
        self.assertIn("const saved = rememberedConnectionService();", remembered)
        self.assertIn("await hasHealthyService(candidate)", remembered)
        self.assertIn("location.replace(candidate.ui_url);", remembered)

    def test_file_preview_guides_to_the_no_configuration_launch_path(self) -> None:
        console = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('id="connection-start"', console)
        self.assertIn('id="start-command">python3 -m daemon.serve</code>', console)
        self.assertIn('id="copy-start-command"', console)
        self.assertIn("async function copyStartCommand()", console)
        self.assertIn("function startCommandForCurrentCheckout()", console)
        self.assertIn("cd ${shellQuote(root)} && python3 -m daemon.serve", console)
        self.assertIn('$("manual-connect").open = false;', console)
        self.assertIn("高级：手动连接", console)

        manual = console[console.index('$("manual-connect-form").addEventListener'):console.index('$("connect-btn").addEventListener')]
        self.assertIn('new URL("/api/management/info", candidate.ui_url)', manual)
        self.assertIn('submit.textContent = "正在验证…";', manual)
        self.assertIn('localStorage.setItem("cc-conn", JSON.stringify({ host: config.host, port: config.port, user: config.user }));', manual)

    def test_project_workspace_and_code_map_preserve_readable_context(self) -> None:
        console = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('class="projects-workspace"', console)
        self.assertIn('id="monitored-projects-summary"', console)
        self.assertIn('function projectStatusTone(status)', console)
        self.assertIn('`code-map-node ${node.type}', console)
        self.assertIn('svg.dataset.hasSelection', console)
        self.assertIn('code-map-edge.is-related', console)
        self.assertIn('aria-current="page"', console)

    def test_overlays_are_mutually_exclusive_and_keyboard_operable(self) -> None:
        console = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('role="dialog" aria-modal="true" aria-labelledby="connect-title"', console)
        self.assertIn('role="dialog" aria-modal="true" aria-labelledby="proj-picker-title"', console)
        self.assertIn('const OVERLAY_PANELS = {', console)
        self.assertIn('if (activeOverlay && activeOverlay !== name) closeActiveOverlay(false, true);', console)
        self.assertIn('app.inert = true;', console)
        self.assertIn('document.body.classList.add("modal-open");', console)
        self.assertIn('if (event.key === "Escape")', console)
        self.assertIn('if (event.key !== "Tab") return;', console)
        self.assertIn('closeConnectionPicker(restoreFocus, force);', console)
        self.assertIn('<form id="manual-connect-form">', console)

    def test_project_picker_keeps_valid_checkbox_semantics_and_recovery(self) -> None:
        console = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(
            encoding="utf-8"
        )
        picker = console[console.index("function selectedProjectEntries()"):console.index('$("open-project-picker-btn")')]
        self.assertIn('create("label", "proj-option")', picker)
        self.assertIn('box.type = "checkbox";', picker)
        self.assertIn('new Map(selected.filter((item) => item.workspace)', picker)
        self.assertIn('const projectsLoaded = await loadProjects(dataEpoch, true);', picker)
        self.assertIn('监控已登记，但项目列表暂时无法刷新', picker)
        self.assertNotIn('create("button", "proj-option")', picker)

    def test_project_switch_and_review_events_are_scoped_to_current_workspace(self) -> None:
        console = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(
            encoding="utf-8"
        )
        switcher = console[console.index("async function selectProject"):console.index("function resetDriveUI")]
        self.assertIn('if (!changed) {', switcher)
        self.assertIn('projectSwitchBusy = true;', switcher)
        self.assertIn('const epoch = ++dataEpoch;', switcher)
        self.assertIn('if (!activeOverlay)', switcher)
        self.assertIn('const VIEW_HEADING_IDS =', console)
        self.assertIn('focusCurrentViewHeading();', console)

        drive = console[console.index("async function startDrive"):console.index("function driveReportItem")]
        self.assertIn('const workspace = selectedProject.workspace;', drive)
        self.assertIn('const epoch = dataEpoch;', drive)
        self.assertIn('selectedProject.workspace !== workspace', drive)
        self.assertIn('const workspace = run.workspace || event?.workspace;', drive)
        self.assertIn('workspace !== selectedProject.workspace', drive)
        self.assertIn('event.review_run_id === reviewRun.run_id', drive)
        self.assertIn('setReviewStartBusy(true);', drive)

    def test_code_map_selection_has_a_clear_keyboard_safe_path(self) -> None:
        console = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(
            encoding="utf-8"
        )
        code_map = console[console.index("function clearCodeMapSelection"):console.index("// Refresh only the data")]
        self.assertIn('id="code-map-clear-selection"', console)
        self.assertIn('svg.setAttribute("role", "group");', code_map)
        self.assertIn('group.dataset.nodeId = node.id;', code_map)
        self.assertIn('function handleCodeMapFilter()', code_map)
        self.assertIn('clearCodeMapSelection();', code_map)
        self.assertIn('event.key !== "Escape" || activeOverlay', code_map)

    def test_code_map_canvas_has_local_view_controls_and_node_robot_summary(self) -> None:
        console = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('id="code-map-zoom-in"', console)
        self.assertNotIn('id="code-map-expand"', console)
        self.assertNotIn('code-map-inspector', console)
        self.assertIn('robot.id = "code-map-robot";', console)
        self.assertIn('function zoomCodeMap(nextZoom, focus = null)', console)
        self.assertIn('function bindCodeMapViewport(svg)', console)
        self.assertIn('function createCodeMapRobot()', console)
        self.assertIn('svg.addEventListener("wheel",', console)
        self.assertIn('stage.classList.add("is-panning");', console)
        self.assertIn('handle.addEventListener("pointermove",', console)
        self.assertIn('codeMapRobotPosition.x = pointer.startX', console)
        self.assertIn('await loadCodeMapDossier();', console)
        self.assertIn('include_preview: false, include_source: false, interpret: true', console)
        self.assertIn('LLM 已生成', console)


if __name__ == "__main__":
    unittest.main()
