import SwiftUI
import AppKit
import Combine
import JARVISKit

@main
struct JARVISHUDApp: App {
    @NSApplicationDelegateAdaptor(HUDAppDelegate.self) var appDelegate

    var body: some Scene {
        // Hidden default window — the real UI is a transparent overlay window
        WindowGroup {
            Text("JARVIS HUD")
                .frame(width: 0, height: 0)
                .hidden()
        }
        .windowStyle(.hiddenTitleBar)
    }
}

@MainActor
class HUDAppDelegate: NSObject, NSApplicationDelegate {
    var window: ClickThroughWindow?
    let appState = AppState()
    private var isShowingHUD = false
    private var hudVisible = false

    // Menu bar
    private var statusItem: NSStatusItem?
    private var statusMenu: NSMenu?
    private var connectionSub: AnyCancellable?

    nonisolated func applicationDidFinishLaunching(_ notification: Notification) {
        Task { @MainActor in
            // Hide the default SwiftUI window
            for w in NSApp.windows where w != self.window {
                w.orderOut(nil)
            }

            // Make this a menu-bar-only app (no dock icon)
            NSApp.setActivationPolicy(.accessory)

            self.setupMenuBar()
            self.createOverlayWindow()
            self.appState.boot()
        }
    }

    nonisolated func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    // MARK: - Menu bar Arc Reactor

    private func setupMenuBar() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem = item

        // Draw the Arc Reactor icon
        updateMenuBarIcon(status: .disconnected)

        // Build the dropdown menu
        let menu = NSMenu()
        menu.addItem(withTitle: "JARVIS — Offline", action: nil, keyEquivalent: "")
            .tag = 100 // status line tag
        menu.addItem(.separator())

        let toggleItem = NSMenuItem(title: "Show HUD", action: #selector(toggleHUD), keyEquivalent: "j")
        toggleItem.keyEquivalentModifierMask = [.command, .shift]
        toggleItem.target = self
        toggleItem.tag = 200
        menu.addItem(toggleItem)

        menu.addItem(.separator())

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

        // Observe connection status to update the icon + menu label
        connectionSub = appState.pythonBridge.$connectionStatus
            .receive(on: RunLoop.main)
            .sink { [weak self] status in
                self?.updateMenuBarIcon(status: status)
                self?.updateMenuBarStatus(status: status)
            }
    }

    private func updateMenuBarIcon(status: ConnectionStatus) {
        guard let button = statusItem?.button else { return }
        let image = drawArcReactorIcon(status: status)
        image.isTemplate = false // We handle coloring ourselves
        button.image = image
        button.toolTip = "JARVIS — \(status.rawValue)"
    }

    private func updateMenuBarStatus(status: ConnectionStatus) {
        guard let menu = statusMenu,
              let statusLine = menu.item(withTag: 100) else { return }

        switch status {
        case .connected:
            statusLine.title = "JARVIS — Online"
        case .connecting:
            statusLine.title = "JARVIS — Connecting..."
        case .disconnected:
            statusLine.title = "JARVIS — Offline"
        case .error:
            statusLine.title = "JARVIS — Connection Error"
        }
    }

    /// Draws a 18x18 arc reactor icon for the menu bar.
    private func drawArcReactorIcon(status: ConnectionStatus) -> NSImage {
        let size = NSSize(width: 18, height: 18)
        let image = NSImage(size: size, flipped: false) { rect in
            let ctx = NSGraphicsContext.current!.cgContext
            let center = CGPoint(x: rect.midX, y: rect.midY)

            // Color based on connection state
            let coreColor: NSColor
            let ringColor: NSColor
            switch status {
            case .connected:
                coreColor = NSColor(red: 0, green: 1, blue: 0.255, alpha: 1)   // JARVIS green
                ringColor = NSColor(red: 0, green: 1, blue: 0.255, alpha: 0.5)
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
            ctx.setLineWidth(1.5)
            ctx.addEllipse(in: rect.insetBy(dx: 1, dy: 1))
            ctx.strokePath()

            // Inner ring
            ctx.setStrokeColor(coreColor.cgColor)
            ctx.setLineWidth(1.0)
            ctx.addEllipse(in: rect.insetBy(dx: 4, dy: 4))
            ctx.strokePath()

            // Core dot
            ctx.setFillColor(coreColor.cgColor)
            let coreSize: CGFloat = 4
            ctx.fillEllipse(in: CGRect(
                x: center.x - coreSize / 2,
                y: center.y - coreSize / 2,
                width: coreSize,
                height: coreSize
            ))

            // Three radial lines (reactor segments)
            ctx.setStrokeColor(coreColor.withAlphaComponent(0.7).cgColor)
            ctx.setLineWidth(0.8)
            for angle in stride(from: 0.0, to: Double.pi * 2, by: Double.pi * 2 / 3) {
                let innerR: CGFloat = 4
                let outerR: CGFloat = 7
                let start = CGPoint(
                    x: center.x + innerR * CGFloat(cos(angle)),
                    y: center.y + innerR * CGFloat(sin(angle))
                )
                let end = CGPoint(
                    x: center.x + outerR * CGFloat(cos(angle)),
                    y: center.y + outerR * CGFloat(sin(angle))
                )
                ctx.move(to: start)
                ctx.addLine(to: end)
            }
            ctx.strokePath()

            return true
        }
        return image
    }

    // MARK: - Menu actions

    @objc private func toggleHUD() {
        if hudVisible {
            hideHUDOverlay()
        } else {
            showHUDOverlay()
        }
        // Update menu item title
        if let item = statusMenu?.item(withTag: 200) {
            item.title = hudVisible ? "Hide HUD" : "Show HUD"
        }
    }

    @objc private func showQuickCommand() {
        // Simple input dialog for quick commands
        let alert = NSAlert()
        alert.messageText = "JARVIS Command"
        alert.informativeText = "Type a command:"
        alert.alertStyle = .informational
        alert.addButton(withTitle: "Send")
        alert.addButton(withTitle: "Cancel")

        let input = NSTextField(frame: NSRect(x: 0, y: 0, width: 300, height: 24))
        input.placeholderString = "e.g., run ouroboros, what time is it..."
        alert.accessoryView = input

        // Bring app to front for the dialog
        NSApp.activate(ignoringOtherApps: true)

        if alert.runModal() == .alertFirstButtonReturn {
            let command = input.stringValue
            guard !command.isEmpty else { return }
            Task {
                try? await self.appState.pythonBridge.sendCommand(command)
            }
        }
    }

    @objc private func quitApp() {
        appState.pythonBridge.shutdown()
        NSApp.terminate(nil)
    }

    // MARK: - HUD visibility

    private func showHUDOverlay() {
        guard let win = window else { return }
        hudVisible = true
        win.orderFrontRegardless()
        NSAnimationContext.runAnimationGroup { context in
            context.duration = 0.3
            win.animator().alphaValue = 1.0
        }
    }

    private func hideHUDOverlay() {
        guard let win = window else { return }
        hudVisible = false
        NSAnimationContext.runAnimationGroup({ context in
            context.duration = 0.3
            win.animator().alphaValue = 0.0
        }, completionHandler: { [weak self] in
            self?.window?.orderOut(nil)
        })
    }

    // MARK: - Window creation

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

        showLoadingView()

        win.orderFrontRegardless()
        hudVisible = true
    }

    private func showLoadingView() {
        guard let win = window else { return }

        let loadingView = LoadingHUDView(onComplete: { [weak self] in
            Task { @MainActor in
                self?.showHUDView()
            }
        })
        .environmentObject(appState)

        let hostingView = ClickThroughHostingView(rootView: loadingView)
        hostingView.layer?.backgroundColor = .clear
        win.contentView = hostingView
    }

    private func showHUDView() {
        guard let win = window, !isShowingHUD else { return }
        isShowingHUD = true

        let hudView = HUDView(onQuit: { [weak self] in
            Task { @MainActor in
                self?.hideHUDOverlay()
            }
        })
        .environmentObject(appState)

        let hostingView = ClickThroughHostingView(rootView: hudView)
        hostingView.layer?.backgroundColor = .clear

        // Animate the transition
        NSAnimationContext.runAnimationGroup { context in
            context.duration = 0.3
            win.contentView?.animator().alphaValue = 0.0
        } completionHandler: { [weak self] in
            win.contentView = hostingView
            hostingView.alphaValue = 0.0
            NSAnimationContext.runAnimationGroup { context in
                context.duration = 0.5
                hostingView.animator().alphaValue = 1.0
            }
            self?.appState.isLoadingComplete = true

            // Update toggle menu item now that HUD is ready
            if let item = self?.statusMenu?.item(withTag: 200) {
                item.title = "Hide HUD"
            }
        }
    }
}
