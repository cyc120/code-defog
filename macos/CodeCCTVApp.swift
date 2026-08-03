import AppKit
import QuartzCore
import SwiftUI

enum FloatingPanelMetrics {
    static let collapsed = NSSize(width: 196, height: 52)
    // Keep the larger collapsed frame as the interaction target while making
    // the glass surface visually lighter and less dominant.
    static let collapsedVisual = NSSize(width: 184, height: 44)
    static let expanded = NSSize(width: 344, height: 224)
}

enum PreviewMode: Hashable {
    case list
    case graph
    case management
}

enum FloatingPanelAnimation {
    static let duration = 0.42
    static let swiftUI = Animation.timingCurve(0.2, 0.8, 0.2, 1.0, duration: duration)
    static let timingFunction = CAMediaTimingFunction(controlPoints: 0.2, 0.8, 0.2, 1.0)
}

private final class FloatingDragHandler: NSObject, NSGestureRecognizerDelegate {
    weak var hostView: NSView?
    let onStart: () -> Void
    let onMove: (CGSize) -> Void
    let onEnd: () -> Void
    private var lastTranslation: NSPoint = .zero

    init(onStart: @escaping () -> Void, onMove: @escaping (CGSize) -> Void, onEnd: @escaping () -> Void) {
        self.onStart = onStart
        self.onMove = onMove
        self.onEnd = onEnd
    }

    @objc func handlePan(_ recognizer: NSPanGestureRecognizer) {
        guard let hostView else { return }
        switch recognizer.state {
        case .began:
            lastTranslation = recognizer.translation(in: hostView)
            onStart()
        case .changed:
            let translation = recognizer.translation(in: hostView)
            let delta = CGSize(
                width: translation.x - lastTranslation.x,
                height: translation.y - lastTranslation.y
            )
            lastTranslation = translation
            onMove(delta)
        case .ended, .cancelled, .failed:
            lastTranslation = .zero
            onEnd()
        default:
            break
        }
    }

    func gestureRecognizer(
        _ gestureRecognizer: NSGestureRecognizer,
        shouldRecognizeSimultaneouslyWith otherGestureRecognizer: NSGestureRecognizer
    ) -> Bool {
        true
    }
}

private final class FloatingHostingView<Content: View>: NSHostingView<Content> {
    private let dragHandler: FloatingDragHandler

    init(
        rootView: Content,
        onDragStart: @escaping () -> Void,
        onDrag: @escaping (CGSize) -> Void,
        onDragEnd: @escaping () -> Void
    ) {
        self.dragHandler = FloatingDragHandler(
            onStart: onDragStart,
            onMove: onDrag,
            onEnd: onDragEnd
        )
        super.init(rootView: rootView)

        dragHandler.hostView = self
        let pan = NSPanGestureRecognizer(
            target: dragHandler,
            action: #selector(FloatingDragHandler.handlePan(_:))
        )
        // Let SwiftUI buttons receive the mouse-down immediately. The pan
        // recognizer still takes over once the pointer actually moves.
        pan.delaysPrimaryMouseButtonEvents = false
        pan.delaysSecondaryMouseButtonEvents = false
        pan.delegate = dragHandler
        addGestureRecognizer(pan)
    }

    required convenience init(rootView: Content) {
        self.init(rootView: rootView, onDragStart: {}, onDrag: { _ in }, onDragEnd: {})
    }

    required init?(coder: NSCoder) {
        fatalError("FloatingHostingView does not support storyboard decoding")
    }
}

@MainActor
final class PreviewWindowController {
    static let shared = PreviewWindowController()

    private var window: NSWindow?

    func show(store: StatusStore, mode: PreviewMode = .list) {
        let hostingView = NSHostingView(rootView: PreviewView(store: store, initialMode: mode))
        if let window {
            window.contentView = hostingView
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 940, height: 640),
            styleMask: [.titled, .closable, .resizable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Code CCTV 全局预览"
        window.setFrameAutosaveName("CodeCCTV.Preview")
        window.isRestorable = true
        window.contentView = hostingView
        window.isReleasedWhenClosed = false
        window.center()
        self.window = window
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func close() {
        window?.orderOut(nil)
    }
}

@MainActor
final class FloatingPanelController: NSObject, ObservableObject {
    private let panel: NSPanel
    private let store: StatusStore
    private let originKey = "CodeCCTV.floatingPanelOrigin"
    private let collapsedSize = FloatingPanelMetrics.collapsed
    @Published private(set) var isExpanded = false
    private var collapseWorkItem: DispatchWorkItem?
    private var dismissedStateID = ""
    private var moveObserver: NSObjectProtocol?
    private var persistWorkItem: DispatchWorkItem?
    private var dragOrigin: NSPoint?
    private var pendingResize: (size: NSSize, animated: Bool)?

    init(store: StatusStore) {
        self.store = store
        self.panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: collapsedSize.width, height: collapsedSize.height),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        super.init()

        let hostingView = FloatingHostingView(
            rootView: FloatingPanelView(
                controller: self,
                store: store,
            onOpen: {
                PreviewWindowController.shared.show(store: store)
            },
            onOpenGraph: {
                PreviewWindowController.shared.show(store: store, mode: .graph)
            },
            onResize: { [weak self] size, animated in
                    self?.resize(to: size, animated: animated)
                }
            ),
            onDragStart: { [weak self] in
                self?.beginMoving()
            },
            onDrag: { [weak self] translation in
                self?.move(to: translation)
            },
            onDragEnd: { [weak self] in
                self?.endMoving()
            }
        )
        hostingView.wantsLayer = true
        hostingView.layer?.backgroundColor = NSColor.clear.cgColor
        hostingView.layer?.isOpaque = false
        hostingView.layer?.cornerRadius = 22
        hostingView.layer?.masksToBounds = true
        panel.contentView = hostingView
        panel.isFloatingPanel = true
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient]
        panel.hidesOnDeactivate = false
        // The AppKit pan recognizer on FloatingHostingView moves the window
        // without causing SwiftUI to rebuild the material surface.
        panel.isMovableByWindowBackground = false
        panel.backgroundColor = .clear
        panel.isOpaque = false
        // NSPanel's native shadow follows the rectangular window frame and leaves a square halo.
        // The SwiftUI surface below owns the rounded glass shadow instead.
        panel.hasShadow = false
        moveObserver = NotificationCenter.default.addObserver(
            forName: NSWindow.didMoveNotification,
            object: panel,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.schedulePersistOrigin()
            }
        }
    }

    func show() {
        collapseWorkItem?.cancel()
        collapseWorkItem = nil
        persistWorkItem?.cancel()
        dragOrigin = nil
        pendingResize = nil
        isExpanded = false
        guard let screen = NSScreen.main else { return }
        let frame = screen.visibleFrame
        let size = panel.frame.size
        if let saved = UserDefaults.standard.array(forKey: originKey) as? [Double], saved.count == 2 {
            let savedFrame = NSRect(origin: NSPoint(x: saved[0], y: saved[1]), size: size)
            if savedFrame.intersects(frame) {
                panel.setFrameOrigin(savedFrame.origin)
            } else {
                placeAtDefault(frame: frame, size: size)
            }
        } else {
            placeAtDefault(frame: frame, size: size)
        }
        panel.orderFrontRegardless()
    }

    func hide() {
        collapseWorkItem?.cancel()
        collapseWorkItem = nil
        dragOrigin = nil
        pendingResize = nil
        setExpanded(false, animated: false)
        panel.orderOut(nil)
    }

    func presentBubble(for stateID: String? = nil, autoCollapse: Bool = false) {
        if let stateID, stateID == dismissedStateID {
            return
        }
        setExpanded(true)
        collapseWorkItem?.cancel()
        collapseWorkItem = nil
        guard autoCollapse else { return }
        let workItem = DispatchWorkItem { [weak self] in
            self?.collapseBubble()
        }
        collapseWorkItem = workItem
        let delay = max(2, StatusStore.bubbleAutoCollapseSeconds)
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: workItem)
    }

    func collapseBubble() {
        collapseWorkItem?.cancel()
        collapseWorkItem = nil
        setExpanded(false)
    }

    func dismissBubble(stateID: String) {
        dismissedStateID = stateID
        collapseBubble()
    }

    deinit {
        collapseWorkItem?.cancel()
        persistWorkItem?.cancel()
        dragOrigin = nil
        pendingResize = nil
        if let moveObserver {
            NotificationCenter.default.removeObserver(moveObserver)
        }
    }

    private func setExpanded(_ expanded: Bool, animated: Bool = true) {
        guard isExpanded != expanded else { return }
        if animated {
            withAnimation(FloatingPanelAnimation.swiftUI) {
                isExpanded = expanded
            }
        } else {
            isExpanded = expanded
        }
    }

    private func resize(to size: CGSize, animated: Bool) {
        let nextSize = NSSize(width: size.width, height: size.height)
        let oldFrame = panel.frame
        guard oldFrame.size != nextSize else { return }

        if dragOrigin != nil {
            pendingResize = (nextSize, animated)
            return
        }

        let center = NSPoint(x: oldFrame.midX, y: oldFrame.midY)
        let nextOrigin = NSPoint(
            x: center.x - nextSize.width / 2,
            y: center.y - nextSize.height / 2
        )
        let nextFrame = NSRect(origin: nextOrigin, size: nextSize)
        if animated {
            NSAnimationContext.runAnimationGroup { context in
                context.duration = FloatingPanelAnimation.duration
                context.timingFunction = FloatingPanelAnimation.timingFunction
                context.allowsImplicitAnimation = true
                self.panel.animator().setFrame(nextFrame, display: true)
            }
        } else {
            panel.setFrame(nextFrame, display: true, animate: false)
        }
        UserDefaults.standard.set([nextOrigin.x, nextOrigin.y], forKey: originKey)
    }

    private func placeAtDefault(frame: NSRect, size: NSSize) {
        panel.setFrameOrigin(NSPoint(x: frame.maxX - size.width - 18, y: frame.maxY - size.height - 18))
    }

    private func beginMoving() {
        dragOrigin = panel.frame.origin
    }

    private func move(to delta: CGSize) {
        guard dragOrigin != nil else { return }
        let current = panel.frame.origin
        panel.setFrameOrigin(
            NSPoint(
                x: current.x + delta.width,
                y: current.y - delta.height
            )
        )
    }

    private func endMoving() {
        guard dragOrigin != nil else { return }
        dragOrigin = nil
        schedulePersistOrigin()
        snapIntoVisibleFrame()

        guard let pendingResize else { return }
        self.pendingResize = nil
        resize(
            to: CGSize(width: pendingResize.size.width, height: pendingResize.size.height),
            animated: pendingResize.animated
        )
    }

    private func snapIntoVisibleFrame() {
        let frame = panel.frame
        let visible = NSScreen.screens
            .map(\.visibleFrame)
            .first { $0.intersects(frame) } ?? NSScreen.main?.visibleFrame
        guard let visible, !visible.intersects(frame) else { return }

        // If the capsule was dropped completely off-screen, bring back a
        // comfortable sliver instead of losing it.
        let minVisibleWidth: CGFloat = 48
        let minVisibleHeight: CGFloat = 28
        let size = frame.size
        let lowerX = visible.minX - size.width + minVisibleWidth
        let upperX = visible.maxX - minVisibleWidth
        let lowerY = visible.minY - size.height + minVisibleHeight
        let upperY = visible.maxY - minVisibleHeight
        let origin = NSPoint(
            x: min(max(frame.origin.x, min(lowerX, upperX)), max(lowerX, upperX)),
            y: min(max(frame.origin.y, min(lowerY, upperY)), max(lowerY, upperY))
        )
        guard origin != frame.origin else { return }
        panel.setFrameOrigin(origin)
    }

    private func schedulePersistOrigin() {
        persistWorkItem?.cancel()
        let workItem = DispatchWorkItem { [weak self] in
            self?.persistOrigin()
        }
        persistWorkItem = workItem
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.12, execute: workItem)
    }

    private func persistOrigin() {
        let origin = panel.frame.origin
        UserDefaults.standard.set([origin.x, origin.y], forKey: originKey)
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private let store = StatusStore.shared
    private var floatingPanel: FloatingPanelController?
    private var statusItem: NSStatusItem?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)

        floatingPanel = FloatingPanelController(store: store)
        floatingPanel?.show()

        let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.image = NSImage(systemSymbolName: "video.fill", accessibilityDescription: "Code CCTV")
        let menu = NSMenu()
        menu.addItem(menuItem(title: "打开全局预览", action: #selector(openPreview), image: "rectangle.3.group"))
        menu.addItem(menuItem(title: "显示浮窗", action: #selector(showFloatingPanel), image: "eye"))
        menu.addItem(.separator())
        menu.addItem(menuItem(title: "退出 Code CCTV", action: #selector(quitApp), image: "power"))
        statusItem.menu = menu
        self.statusItem = statusItem
    }

    private func menuItem(title: String, action: Selector, image: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.target = self
        item.image = NSImage(systemSymbolName: image, accessibilityDescription: title)
        return item
    }

    @objc private func openPreview() {
        PreviewWindowController.shared.show(store: store)
    }

    @objc private func showFloatingPanel() {
        floatingPanel?.show()
    }

    @objc private func quitApp() {
        NSApp.terminate(nil)
    }
}

@main
struct CodeCCTVApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        Settings {
            EmptyView()
        }
    }
}
