import SwiftUI
import AppKit
import Combine
import JARVISKit

@main
struct JARVISHUDApp: App {
    @NSApplicationDelegateAdaptor(HUDAppDelegate.self) var appDelegate

    var body: some Scene {
        WindowGroup {
            Text("JARVIS HUD")
                .frame(width: 0, height: 0)
                .hidden()
        }
        .windowStyle(.hiddenTitleBar)
    }
}

// MARK: - App Delegate

@MainActor
class HUDAppDelegate: NSObject, NSApplicationDelegate {
    var window: ClickThroughWindow?
    let appState = AppState()
    private var hudVisible = false

    // Menu bar
    private var statusItem: NSStatusItem?
    private var statusMenu: NSMenu?
    private var subs = Set<AnyCancellable>()
    private var pulseTimer: Timer?

    nonisolated func applicationDidFinishLaunching(_ notification: Notification) {
        Task { @MainActor in
            for w in NSApp.windows where w != self.window {
                w.orderOut(nil)
            }
            NSApp.setActivationPolicy(.accessory)

            self.createOverlayWindow()
            self.setupMenuBar()
            self.appState.boot()
        }
    }

    nonisolated func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    // MARK: - Window (created once, shown/hidden on demand)

    private func createOverlayWindow() {
        guard let screen = NSScreen.main else { return }

        let win = ClickThroughWindow(
            contentRect: screen.frame,
            styleMask: [.borderless, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        win.setFrame(screen.frame, display: true)
        window = win

        // Install the HUD view immediately (no loading screen)
        let hudView = HUDView(onQuit: { [weak self] in
            Task { @MainActor in self?.hideHUD() }
        })
        .environmentObject(appState)

        let hostingView = ClickThroughHostingView(rootView: hudView)
        hostingView.layer?.backgroundColor = .clear
        win.contentView = hostingView

        // Start hidden — HUD appears when JARVIS has something to show
        win.alphaValue = 0
        win.orderOut(nil)
        hudVisible = false
    }

    // MARK: - Menu Bar Arc Reactor

    private func setupMenuBar() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem = item
        updateMenuBarIcon(status: .disconnected, active: false)

        let menu = NSMenu()
        menu.addItem(withTitle: "JARVIS — Connecting...", action: nil, keyEquivalent: "")
            .tag = 100
        menu.addItem(.separator())

        let toggleItem = NSMenuItem(title: "Show HUD", action: #selector(toggleHUD), keyEquivalent: "j")
        toggleItem.keyEquivalentModifierMask = [.command, .shift]
        toggleItem.target = self
        toggleItem.tag = 200
        menu.addItem(toggleItem)

        let cmdItem = NSMenuItem(title: "Quick Command...", action: #selector(showQuickCommand), keyEquivalent: "k")
        cmdItem.keyEquivalentModifierMask = [.command, .shift]
        cmdItem.target = self
        menu.addItem(cmdItem)

        menu.addItem(.separator())

        let quitItem = NSMenuItem(title: "Quit JARVIS", action: #selector(quitApp), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)

        statusMenu = menu
        item.menu = menu

        // Observe connection status → update icon color + menu label
        appState.pythonBridge.$connectionStatus
            .receive(on: RunLoop.main)
            .sink { [weak self] status in
                let active = self?.appState.pythonBridge.isActive ?? false
                self?.updateMenuBarIcon(status: status, active: active)
                self?.updateStatusLabel(status: status)
            }
            .store(in: &subs)

        // Observe activity → summon/dismiss HUD + pulse icon
        appState.pythonBridge.$isActive
            .receive(on: RunLoop.main)
            .removeDuplicates()
            .sink { [weak self] active in
                guard let self else { return }
                let status = self.appState.pythonBridge.connectionStatus
                self.updateMenuBarIcon(status: status, active: active)

                if active {
                    self.showHUD()
                    self.startPulse()
                } else {
                    self.stopPulse()
                    self.hideHUD()
                }
            }
            .store(in: &subs)
    }

    // MARK: - Icon drawing

    private func updateMenuBarIcon(status: ConnectionStatus, active: Bool) {
        guard let button = statusItem?.button else { return }
        let image = drawArcReactorIcon(status: status, active: active)
        image.isTemplate = false
        button.image = image
        button.toolTip = "JARVIS — \(status.rawValue)"
    }

    private func updateStatusLabel(status: ConnectionStatus) {
        guard let statusLine = statusMenu?.item(withTag: 100) else { return }
        switch status {
        case .connected:  statusLine.title = "JARVIS — Online"
        case .connecting: statusLine.title = "JARVIS — Connecting..."
        case .disconnected: statusLine.title = "JARVIS — Offline"
        case .error:      statusLine.title = "JARVIS — Error"
        }
    }

    private func drawArcReactorIcon(status: ConnectionStatus, active: Bool) -> NSImage {
        let size = NSSize(width: 18, height: 18)
        return NSImage(size: size, flipped: false) { rect in
            let ctx = NSGraphicsContext.current!.cgContext
            let center = CGPoint(x: rect.midX, y: rect.midY)

            let coreColor: NSColor
            let ringColor: NSColor
            switch status {
            case .connected:
                let green = NSColor(red: 0, green: 1, blue: 0.255, alpha: 1)
                coreColor = active ? NSColor.white : green
                ringColor = green.withAlphaComponent(active ? 0.9 : 0.5)
            case .connecting:
                coreColor = NSColor.systemYellow
                ringColor = NSColor.systemYellow.withAlphaComponent(0.4)
            case .disconnected:
                coreColor = NSColor.systemGray
                ringColor = NSColor.systemGray.withAlphaComponent(0.3)
            case .error:
                coreColor = NSColor.systemRed
                ringColor = NSColor.systemRed.withAlphaComponent(0.4)
            }

            // Outer ring
            ctx.setStrokeColor(ringColor.cgColor)
            ctx.setLineWidth(active ? 2.0 : 1.5)
            ctx.addEllipse(in: rect.insetBy(dx: 1, dy: 1))
            ctx.strokePath()

            // Inner ring
            ctx.setStrokeColor(coreColor.cgColor)
            ctx.setLineWidth(1.0)
            ctx.addEllipse(in: rect.insetBy(dx: 4, dy: 4))
            ctx.strokePath()

            // Core (larger when active)
            ctx.setFillColor(coreColor.cgColor)
            let coreSize: CGFloat = active ? 5 : 4
            ctx.fillEllipse(in: CGRect(
                x: center.x - coreSize / 2, y: center.y - coreSize / 2,
                width: coreSize, height: coreSize
            ))

            // Three radial lines
            ctx.setStrokeColor(coreColor.withAlphaComponent(0.7).cgColor)
            ctx.setLineWidth(active ? 1.2 : 0.8)
            for angle in stride(from: 0.0, to: Double.pi * 2, by: Double.pi * 2 / 3) {
                let innerR: CGFloat = 4
                let outerR: CGFloat = 7
                ctx.move(to: CGPoint(
                    x: center.x + innerR * CGFloat(cos(angle)),
                    y: center.y + innerR * CGFloat(sin(angle))
                ))
                ctx.addLine(to: CGPoint(
                    x: center.x + outerR * CGFloat(cos(angle)),
                    y: center.y + outerR * CGFloat(sin(angle))
                ))
            }
            ctx.strokePath()

            return true
        }
    }

    // MARK: - Icon pulse animation

    private var pulseBright = true

    private func startPulse() {
        guard pulseTimer == nil else { return }
        pulseBright = true
        pulseTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self, let button = self.statusItem?.button else { return }
                button.alphaValue = self.pulseBright ? 1.0 : 0.5
                self.pulseBright.toggle()
            }
        }
    }

    private func stopPulse() {
        pulseTimer?.invalidate()
        pulseTimer = nil
        statusItem?.button?.alphaValue = 1.0
    }

    // MARK: - HUD show/hide

    private func showHUD() {
        guard let win = window, !hudVisible else { return }
        hudVisible = true
        win.orderFrontRegardless()
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = 0.3
            win.animator().alphaValue = 1.0
        }
        if let item = statusMenu?.item(withTag: 200) { item.title = "Hide HUD" }
    }

    private func hideHUD() {
        guard let win = window, hudVisible else { return }
        hudVisible = false
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 0.4
            win.animator().alphaValue = 0.0
        }, completionHandler: { [weak self] in
            self?.window?.orderOut(nil)
        })
        if let item = statusMenu?.item(withTag: 200) { item.title = "Show HUD" }
    }

    // MARK: - Menu actions

    @objc private func toggleHUD() {
        if hudVisible { hideHUD() } else { showHUD() }
    }

    @objc private func showQuickCommand() {
        let alert = NSAlert()
        alert.messageText = "JARVIS Command"
        alert.informativeText = "Type a command:"
        alert.alertStyle = .informational
        alert.addButton(withTitle: "Send")
        alert.addButton(withTitle: "Cancel")

        let input = NSTextField(frame: NSRect(x: 0, y: 0, width: 300, height: 24))
        input.placeholderString = "e.g., run ouroboros, what time is it..."
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
