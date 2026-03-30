import SwiftUI
import AppKit
import Combine
import JARVISKit

@main
struct JARVISHUDApp: App {
    @NSApplicationDelegateAdaptor(HUDAppDelegate.self) var appDelegate

    var body: some Scene {
        WindowGroup {
            Text("")
                .frame(width: 0, height: 0)
                .hidden()
        }
        .windowStyle(.hiddenTitleBar)
    }
}

// MARK: - App Delegate — Menu bar only, no overlay on launch

@MainActor
class HUDAppDelegate: NSObject, NSApplicationDelegate {
    let appState = AppState()
    let wakeWord = WakeWordListener()

    // Overlay window — created lazily on first toggle
    private var overlayWindow: ClickThroughWindow?
    private var hudVisible = false

    // Menu bar
    private var statusItem: NSStatusItem?
    private var statusMenu: NSMenu?
    private var subs = Set<AnyCancellable>()

    nonisolated func applicationDidFinishLaunching(_ notification: Notification) {
        Task { @MainActor in
            // Hide the default SwiftUI window
            for w in NSApp.windows { w.orderOut(nil) }

            // Menu-bar-only app (no dock icon)
            NSApp.setActivationPolicy(.accessory)

            self.setupMenuBar()
            self.setupVoice()
            self.appState.boot()
        }
    }

    nonisolated func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    // MARK: - Menu Bar

    private func setupMenuBar() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem = item
        updateIcon(status: .disconnected, active: false)

        let menu = NSMenu()
        menu.addItem(withTitle: "JARVIS — Connecting...", action: nil, keyEquivalent: "").tag = 100

        menu.addItem(.separator())

        let toggle = NSMenuItem(title: "Show HUD", action: #selector(toggleHUD), keyEquivalent: "j")
        toggle.keyEquivalentModifierMask = [.command, .shift]
        toggle.target = self
        toggle.tag = 200
        menu.addItem(toggle)

        let cmd = NSMenuItem(title: "Quick Command...", action: #selector(showQuickCommand), keyEquivalent: "k")
        cmd.keyEquivalentModifierMask = [.command, .shift]
        cmd.target = self
        menu.addItem(cmd)

        menu.addItem(.separator())

        let quit = NSMenuItem(title: "Quit JARVIS", action: #selector(quitApp), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)

        statusMenu = menu
        item.menu = menu

        // Observe connection state → icon color + label
        appState.pythonBridge.$connectionStatus
            .receive(on: RunLoop.main)
            .sink { [weak self] status in
                self?.updateIcon(status: status, active: self?.appState.pythonBridge.isActive ?? false)
                self?.updateLabel(status: status)
            }
            .store(in: &subs)

        // Observe activity → pulse icon (but do NOT auto-show overlay)
        appState.pythonBridge.$isActive
            .receive(on: RunLoop.main)
            .removeDuplicates()
            .sink { [weak self] active in
                guard let self else { return }
                self.updateIcon(status: self.appState.pythonBridge.connectionStatus, active: active)
            }
            .store(in: &subs)
    }

    // MARK: - Icon

    private func updateIcon(status: ConnectionStatus, active: Bool) {
        guard let button = statusItem?.button else { return }
        button.image = drawReactor(status: status, active: active)
        button.image?.isTemplate = false
    }

    private func updateLabel(status: ConnectionStatus) {
        guard let line = statusMenu?.item(withTag: 100) else { return }
        switch status {
        case .connected:    line.title = "JARVIS — Online"
        case .connecting:   line.title = "JARVIS — Connecting..."
        case .disconnected: line.title = "JARVIS — Offline"
        case .error:        line.title = "JARVIS — Error"
        }
    }

    private func drawReactor(status: ConnectionStatus, active: Bool) -> NSImage {
        let s = NSSize(width: 18, height: 18)
        return NSImage(size: s, flipped: false) { r in
            let ctx = NSGraphicsContext.current!.cgContext
            let c = CGPoint(x: r.midX, y: r.midY)

            let core: NSColor, ring: NSColor
            switch status {
            case .connected:
                let green = NSColor(red: 0, green: 1, blue: 0.255, alpha: 1)
                core = active ? .white : green
                ring = green.withAlphaComponent(active ? 0.9 : 0.5)
            case .connecting:
                core = .systemYellow; ring = .systemYellow.withAlphaComponent(0.4)
            case .disconnected:
                core = .systemGray; ring = .systemGray.withAlphaComponent(0.3)
            case .error:
                core = .systemRed; ring = .systemRed.withAlphaComponent(0.4)
            }

            // Outer ring
            ctx.setStrokeColor(ring.cgColor); ctx.setLineWidth(active ? 2.0 : 1.5)
            ctx.addEllipse(in: r.insetBy(dx: 1, dy: 1)); ctx.strokePath()
            // Inner ring
            ctx.setStrokeColor(core.cgColor); ctx.setLineWidth(1.0)
            ctx.addEllipse(in: r.insetBy(dx: 4, dy: 4)); ctx.strokePath()
            // Core
            ctx.setFillColor(core.cgColor)
            let sz: CGFloat = active ? 5 : 4
            ctx.fillEllipse(in: CGRect(x: c.x - sz/2, y: c.y - sz/2, width: sz, height: sz))
            // Radials
            ctx.setStrokeColor(core.withAlphaComponent(0.7).cgColor); ctx.setLineWidth(0.8)
            for a in stride(from: 0.0, to: .pi * 2, by: .pi * 2 / 3) {
                ctx.move(to: CGPoint(x: c.x + 4*cos(a), y: c.y + 4*sin(a)))
                ctx.addLine(to: CGPoint(x: c.x + 7*cos(a), y: c.y + 7*sin(a)))
            }
            ctx.strokePath()
            return true
        }
    }

    // MARK: - Voice (wake word)

    private func setupVoice() {
        wakeWord.onCommand = { [weak self] command in
            guard let self else { return }
            print("[JARVIS Voice] Sending: \"\(command)\"")
            Task {
                try? await self.appState.pythonBridge.sendCommand(command)
            }
        }

        // Update menu bar status label when voice state changes
        wakeWord.$state
            .receive(on: RunLoop.main)
            .sink { [weak self] voiceState in
                guard let self, let line = self.statusMenu?.item(withTag: 100) else { return }
                let connStatus = self.appState.pythonBridge.connectionStatus
                switch voiceState {
                case .listening:
                    if connStatus == .connected {
                        line.title = "JARVIS — Listening..."
                    }
                case .capturing:
                    line.title = "JARVIS — Hearing you..."
                default:
                    self.updateLabel(status: connStatus)
                }
            }
            .store(in: &subs)

        // Start listening once connected
        appState.pythonBridge.$connectionStatus
            .receive(on: RunLoop.main)
            .sink { [weak self] status in
                guard let self else { return }
                if status == .connected && self.wakeWord.state == .off {
                    self.wakeWord.start()
                }
            }
            .store(in: &subs)
    }

    // MARK: - HUD Overlay (lazy, manual toggle only)

    private func ensureOverlayWindow() {
        guard overlayWindow == nil, let screen = NSScreen.main else { return }

        let win = ClickThroughWindow(
            contentRect: screen.frame,
            styleMask: [.borderless, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        win.setFrame(screen.frame, display: true)

        let hudView = HUDView(onQuit: { [weak self] in
            Task { @MainActor in self?.hideHUD() }
        })
        .environmentObject(appState)

        let hosting = ClickThroughHostingView(rootView: hudView)
        hosting.layer?.backgroundColor = .clear
        win.contentView = hosting
        win.alphaValue = 0
        overlayWindow = win
    }

    private func showHUD() {
        ensureOverlayWindow()
        guard let win = overlayWindow, !hudVisible else { return }
        hudVisible = true
        win.orderFrontRegardless()
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = 0.3; win.animator().alphaValue = 1.0
        }
        statusMenu?.item(withTag: 200)?.title = "Hide HUD"
    }

    private func hideHUD() {
        guard let win = overlayWindow, hudVisible else { return }
        hudVisible = false
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 0.3; win.animator().alphaValue = 0.0
        }, completionHandler: { [weak self] in
            self?.overlayWindow?.orderOut(nil)
        })
        statusMenu?.item(withTag: 200)?.title = "Show HUD"
    }

    // MARK: - Menu Actions

    @objc private func toggleHUD() {
        if hudVisible { hideHUD() } else { showHUD() }
    }

    @objc private func showQuickCommand() {
        let alert = NSAlert()
        alert.messageText = "JARVIS"
        alert.informativeText = "Command:"
        alert.alertStyle = .informational
        alert.addButton(withTitle: "Send")
        alert.addButton(withTitle: "Cancel")

        let input = NSTextField(frame: NSRect(x: 0, y: 0, width: 300, height: 24))
        input.placeholderString = "Ask JARVIS anything..."
        alert.accessoryView = input
        NSApp.activate(ignoringOtherApps: true)

        if alert.runModal() == .alertFirstButtonReturn, !input.stringValue.isEmpty {
            Task { try? await self.appState.pythonBridge.sendCommand(input.stringValue) }
        }
    }

    @objc private func quitApp() {
        appState.pythonBridge.shutdown()
        NSApp.terminate(nil)
    }
}
