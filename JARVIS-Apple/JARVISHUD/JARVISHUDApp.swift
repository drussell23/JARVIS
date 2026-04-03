import SwiftUI
import AppKit
import Combine
import AVFoundation
import JARVISKit

@main
struct JARVISHUDApp: App {
    @NSApplicationDelegateAdaptor(HUDAppDelegate.self) var appDelegate
    var body: some Scene {
        WindowGroup { Text("").frame(width: 0, height: 0).hidden() }
            .windowStyle(.hiddenTitleBar)
    }
}

@MainActor
class HUDAppDelegate: NSObject, NSApplicationDelegate, AVSpeechSynthesizerDelegate {
    let appState = AppState()
    let hiveStore = HiveStore()
    let wakeWord = WakeWordListener()
    private let tts = AVSpeechSynthesizer()

    // True while JARVIS TTS is playing — prevents the mic from restarting mid-speech
    // and feeding JARVIS's own voice back as a command.
    private var isTTSSpeaking = false

    private var overlayWindow: ClickThroughWindow?
    private var hudVisible = false
    private var statusItem: NSStatusItem?
    private var statusMenu: NSMenu?
    private var subs = Set<AnyCancellable>()

    nonisolated func applicationDidFinishLaunching(_ notification: Notification) {
        Task { @MainActor in
            for w in NSApp.windows { w.orderOut(nil) }
            NSApp.setActivationPolicy(.accessory)
            self.terminateOlderInstancesIfNeeded()
            self.setupMenuBar()
            self.setupVoice()
            // Request Screen Recording permission early, then start persistent stream.
            // The stream shows the macOS purple recording indicator — JARVIS's eyes are open.
            ScreenCaptureService.shared.requestPermission()
            ScreenCaptureService.shared.startStream()
            // Auto-start the Python brainstem — full backend in HUD mode.
            // The onReady callback fires when IPC connects (backend fully booted),
            // telling the HUD to announce "JARVIS Online" to the user.
            BrainstemLauncher.shared.onReady = { [weak self] in
                guard let self = self else { return }
                self.appState.pythonBridge.onBackendReady()
            }
            BrainstemLauncher.shared.hiveStore = self.hiveStore
            BrainstemLauncher.shared.start()
            self.appState.boot()

            // Smoke test: verify screenshot capture works from Swift
            Task {
                try? await Task.sleep(for: .seconds(3))
                print("[SMOKE TEST] Testing screenshot capture...")
                if let b64 = await ScreenCaptureService.shared.captureBase64() {
                    print("[SMOKE TEST] SUCCESS — captured \(b64.count / 1024)KB screenshot")
                    // Save to disk so we can visually verify
                    if let data = Data(base64Encoded: b64) {
                        let path = "/tmp/jarvis_smoke_test.jpg"
                        try? data.write(to: URL(fileURLWithPath: path))
                        print("[SMOKE TEST] Saved to \(path) — open in Finder to verify")
                    }
                } else {
                    print("[SMOKE TEST] FAILED — no screenshot captured")
                    print("[SMOKE TEST] Check: System Settings > Privacy > Screen Recording > JARVISHUD")
                }
            }
        }
    }

    nonisolated func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    // MARK: - Voice: wake word + TTS responses

    private func setupVoice() {
        // Wire TTS delegate so we know when speech finishes → restart mic
        tts.delegate = self

        // When JARVIS finishes a response → Daniel speaks it
        appState.pythonBridge.onSpeak = { [weak self] text, _ in
            self?.speak(text)
        }

        // ALL commands route through local IPC (brainstem backend).
        // Vercel cloud is disabled (402). IPC is the PRIMARY and ONLY path.
        wakeWord.onCommand = { [weak self] command in
            guard let self else { return }
            print("[JARVIS] Voice command: \"\(command)\"")

            guard BrainstemLauncher.shared.isRunning else {
                print("[JARVIS] Backend not running — cannot execute command")
                self.speak("Backend is still starting. Try again in a moment.")
                return
            }

            self.speak("On it.")

            // Tier 0: If an app launch is detected, open it immediately from Swift
            let appName = Self.extractAppName(command)
            if let app = appName {
                print("[JARVIS] Tier 0: launching '\(app)' via macOS open")
                let proc = Process()
                proc.executableURL = URL(fileURLWithPath: "/usr/bin/open")
                proc.arguments = ["-a", app]
                try? proc.run()
                proc.waitUntilExit()
                print("[JARVIS] Tier 0: '\(app)' \(proc.terminationStatus == 0 ? "launched" : "failed")")
            }

            // Route everything to the local backend via IPC
            let remainder = appName != nil ? Self.extractRemainder(command) : nil
            let goal = remainder ?? command
            print("[JARVIS] IPC: \(goal)")

            var actionPayload: [String: Any] = [
                "goal": goal,
                "source": "local_fast_path",
            ]
            if let app = appName {
                actionPayload["app_context"] = app
            }

            Task {
                // If an app was launched, switch to it and wait for it to render
                if let targetApp = appName {
                    let script = "tell application \"\(targetApp)\" to activate"
                    let proc = Process()
                    proc.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
                    proc.arguments = ["-e", script]
                    try? proc.run()
                    proc.waitUntilExit()
                    print("[JARVIS] VLA: activated \(targetApp)")
                    try? await Task.sleep(for: .seconds(3))
                }

                // Fresh screenshot for the VLA planner
                if let b64 = await ScreenCaptureService.shared.captureFresh() {
                    actionPayload["screenshot"] = b64
                    print("[JARVIS] VLA: screenshot (\(b64.count / 1024)KB)")
                }

                BrainstemLauncher.shared.sendEvent(
                    eventType: "action",
                    data: [
                        "action_type": "vision_task",
                        "payload": actionPayload,
                    ]
                )
            }
        }

        // Start wake word listening once cloud connects.
        // Guard isTTSSpeaking so reconnect events don't turn the mic on mid-speech.
        appState.pythonBridge.$connectionStatus
            .receive(on: RunLoop.main)
            .sink { [weak self] status in
                guard let self else { return }
                if status == .connected && self.wakeWord.state == .off && !self.isTTSSpeaking {
                    print("[JARVIS] Cloud connected — starting wake word listener")
                    self.wakeWord.start()
                }
            }
            .store(in: &subs)

        // Update menu label based on voice state — only on meaningful transitions
        wakeWord.$state
            .removeDuplicates()
            .receive(on: RunLoop.main)
            .sink { [weak self] voiceState in
                guard let self else { return }
                switch voiceState {
                case .capturing:
                    self.statusMenu?.item(withTag: 100)?.title = "JARVIS — Hearing you..."
                case .listening:
                    if self.appState.pythonBridge.connectionStatus == .connected {
                        self.statusMenu?.item(withTag: 100)?.title = "JARVIS — Online (listening)"
                    }
                case .cooldown, .off:
                    // Don't update label during cooldown/restart — keeps "Online (listening)" stable
                    break
                }
            }
            .store(in: &subs)
    }

    private func speak(_ text: String) {
        guard !text.isEmpty else { return }
        var cleaned = text
            .replacingOccurrences(of: "**", with: "")
            .replacingOccurrences(of: "`", with: "")
            .replacingOccurrences(of: "### ", with: "")
            .replacingOccurrences(of: "## ", with: "")
            .replacingOccurrences(of: "# ", with: "")
        if cleaned.count > 400 { cleaned = String(cleaned.prefix(400)) + "..." }

        // Stop the mic BEFORE TTS starts so JARVIS can't hear itself and echo.
        // isTTSSpeaking flag blocks the connection-status subscription from
        // accidentally restarting the mic while we're speaking.
        isTTSSpeaking = true
        wakeWord.stop()

        let utterance = AVSpeechUtterance(string: cleaned)
        utterance.voice = AVSpeechSynthesisVoice(identifier: "com.apple.voice.compact.en-GB.Daniel")
            ?? AVSpeechSynthesisVoice(language: "en-GB")
        utterance.rate = 0.52
        utterance.volume = 0.85

        tts.speak(utterance)
    }

    // Restart the mic once JARVIS finishes speaking — this is the gate that prevents echo.
    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        Task { @MainActor [weak self] in
            guard let self else { return }
            self.isTTSSpeaking = false
            // Only restart if cloud is still connected
            guard self.appState.pythonBridge.connectionStatus == .connected else { return }
            // Wait 3s for CoreAudio output hardware to fully release before opening input.
            // AVSpeechSynthesizer holds the output device for ~2-3s after didFinish fires.
            // Without this delay, beginListening() throws -10877 repeatedly while the audio
            // subsystem transitions from output to input mode.
            try? await Task.sleep(for: .seconds(3.0))
            // Re-check connection state and TTS flag after the sleep — a new TTS may have started
            guard !self.isTTSSpeaking,
                  self.appState.pythonBridge.connectionStatus == .connected else { return }
            print("[JARVIS Voice] TTS done — resuming mic")
            self.wakeWord.start()
        }
    }

    // MARK: - VLA Intent Detection (Tier 0)

    /// Mirrors Vercel's ACTION_INTENT_PATTERN from intent-router.ts.
    /// When matched, the command routes directly to the brainstem without
    /// a Vercel cloud round-trip — saving 2-3 seconds of latency.
    private static let vlaPattern: NSRegularExpression? = {
        try? NSRegularExpression(
            pattern: #"\b(click|tap|press|open|launch|type|enter|scroll|drag|swipe|select|close|minimize|maximize|switch to|go to|navigate to|move to|send|submit|toggle|check|uncheck|expand|collapse|message|text|reply|respond|write|compose|search|find|look up)\b"#,
            options: .caseInsensitive
        )
    }()

    private static func isVLAIntent(_ command: String) -> Bool {
        guard let regex = vlaPattern else { return false }
        let range = NSRange(command.startIndex..., in: command)
        return regex.firstMatch(in: command, range: range) != nil
    }

    /// Extract app name from "open WhatsApp and ..." or "open WhatsApp"
    private static func extractAppName(_ command: String) -> String? {
        let patterns = [
            #"^(?:open|launch|start)\s+(?:the\s+)?(.+?)\s+and\s+.+"#,
            #"^(?:open|launch|start)\s+(?:the\s+)?(.+?)(?:\s+app)?$"#,
        ]
        for pattern in patterns {
            guard let regex = try? NSRegularExpression(pattern: pattern, options: .caseInsensitive) else { continue }
            let range = NSRange(command.startIndex..., in: command)
            if let match = regex.firstMatch(in: command, range: range),
               let appRange = Range(match.range(at: 1), in: command) {
                return String(command[appRange])
            }
        }
        return nil
    }

    /// Extract remainder after "open <app> and ..." → "..."
    private static func extractRemainder(_ command: String) -> String? {
        let pattern = #"^(?:open|launch|start)\s+(?:the\s+)?.+?\s+and\s+(.+)$"#
        guard let regex = try? NSRegularExpression(pattern: pattern, options: .caseInsensitive) else { return nil }
        let range = NSRange(command.startIndex..., in: command)
        if let match = regex.firstMatch(in: command, range: range),
           let remRange = Range(match.range(at: 1), in: command) {
            return String(command[remRange])
        }
        return nil
    }

    // MARK: - Menu Bar

    private func terminateOlderInstancesIfNeeded() {
        guard let bundleIdentifier = Bundle.main.bundleIdentifier else { return }

        let currentPID = ProcessInfo.processInfo.processIdentifier
        let duplicates = NSRunningApplication.runningApplications(withBundleIdentifier: bundleIdentifier)
            .filter { $0.processIdentifier != currentPID }

        guard !duplicates.isEmpty else { return }

        for app in duplicates {
            app.forceTerminate()
        }
    }

    private func setupMenuBar() {
        guard statusItem == nil else { return }

        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem = item
        updateIcon(status: .disconnected, active: false)

        let menu = NSMenu()
        menu.addItem(withTitle: "JARVIS — Connecting...", action: nil, keyEquivalent: "").tag = 100
        menu.addItem(.separator())

        let cmd = NSMenuItem(title: "Quick Command...", action: #selector(showQuickCommand), keyEquivalent: "k")
        cmd.keyEquivalentModifierMask = [.command, .shift]
        cmd.target = self
        menu.addItem(cmd)

        let toggle = NSMenuItem(title: "Show HUD", action: #selector(toggleHUD), keyEquivalent: "j")
        toggle.keyEquivalentModifierMask = [.command, .shift]
        toggle.target = self
        toggle.tag = 200
        menu.addItem(toggle)

        menu.addItem(.separator())

        let quit = NSMenuItem(title: "Quit JARVIS", action: #selector(quitApp), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)

        statusMenu = menu
        item.menu = menu

        appState.pythonBridge.$connectionStatus
            .receive(on: RunLoop.main)
            .sink { [weak self] status in
                self?.updateIcon(status: status, active: self?.appState.pythonBridge.isActive ?? false)
                self?.updateLabel(status: status)
            }
            .store(in: &subs)

        appState.pythonBridge.$isActive
            .receive(on: RunLoop.main)
            .removeDuplicates()
            .sink { [weak self] active in
                guard let self else { return }
                self.updateIcon(status: self.appState.pythonBridge.connectionStatus, active: active)
            }
            .store(in: &subs)
    }

    // MARK: - Menu Actions

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

    @objc private func toggleHUD() { if hudVisible { hideHUD() } else { showHUD() } }
    @objc private func quitApp() { BrainstemLauncher.shared.stop(); ScreenCaptureService.shared.stopStream(); appState.pythonBridge.shutdown(); NSApp.terminate(nil) }

    // MARK: - Icon

    private func updateIcon(status: ConnectionStatus, active: Bool) {
        guard let button = statusItem?.button else { return }
        button.image = drawReactor(status: status, active: active)
        button.image?.isTemplate = false
    }

    private func updateLabel(status: ConnectionStatus) {
        guard let line = statusMenu?.item(withTag: 100) else { return }
        switch status {
        case .connected:    line.title = "JARVIS — Online (listening)"
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
                let g = NSColor(red: 0, green: 1, blue: 0.255, alpha: 1)
                core = active ? .white : g; ring = g.withAlphaComponent(active ? 0.9 : 0.5)
            case .connecting: core = .systemYellow; ring = .systemYellow.withAlphaComponent(0.4)
            case .disconnected: core = .systemGray; ring = .systemGray.withAlphaComponent(0.3)
            case .error: core = .systemRed; ring = .systemRed.withAlphaComponent(0.4)
            }
            ctx.setStrokeColor(ring.cgColor); ctx.setLineWidth(active ? 2.0 : 1.5)
            ctx.addEllipse(in: r.insetBy(dx: 1, dy: 1)); ctx.strokePath()
            ctx.setStrokeColor(core.cgColor); ctx.setLineWidth(1.0)
            ctx.addEllipse(in: r.insetBy(dx: 4, dy: 4)); ctx.strokePath()
            ctx.setFillColor(core.cgColor)
            let sz: CGFloat = active ? 5 : 4
            ctx.fillEllipse(in: CGRect(x: c.x-sz/2, y: c.y-sz/2, width: sz, height: sz))
            ctx.setStrokeColor(core.withAlphaComponent(0.7).cgColor); ctx.setLineWidth(0.8)
            for a in stride(from: 0.0, to: .pi*2, by: .pi*2/3) {
                ctx.move(to: CGPoint(x: c.x+4*cos(a), y: c.y+4*sin(a)))
                ctx.addLine(to: CGPoint(x: c.x+7*cos(a), y: c.y+7*sin(a)))
            }
            ctx.strokePath(); return true
        }
    }

    // MARK: - HUD Overlay

    private func ensureOverlayWindow() {
        guard overlayWindow == nil, let screen = NSScreen.main else { return }
        let win = ClickThroughWindow(contentRect: screen.frame,
            styleMask: [.borderless, .fullSizeContentView], backing: .buffered, defer: false)
        win.setFrame(screen.frame, display: true)
        let hudView = HUDView(hiveStore: hiveStore).environmentObject(appState)
        let hosting = ClickThroughHostingView(rootView: hudView)
        hosting.layer?.backgroundColor = .clear
        win.contentView = hosting; win.alphaValue = 0; overlayWindow = win
    }

    private func showHUD() {
        ensureOverlayWindow()
        guard let win = overlayWindow, !hudVisible else { return }
        hudVisible = true; win.orderFrontRegardless()
        NSAnimationContext.runAnimationGroup { $0.duration = 0.3; win.animator().alphaValue = 1.0 }
        statusMenu?.item(withTag: 200)?.title = "Hide HUD"
    }

    private func hideHUD() {
        guard let win = overlayWindow, hudVisible else { return }
        hudVisible = false
        let fadeDuration = 0.3
        NSAnimationContext.runAnimationGroup({ $0.duration = fadeDuration; win.animator().alphaValue = 0 })
        Task { @MainActor [weak self] in
            try? await Task.sleep(for: .seconds(fadeDuration))
            guard let self, !self.hudVisible, self.overlayWindow === win else { return }
            self.overlayWindow?.orderOut(nil)
        }
        statusMenu?.item(withTag: 200)?.title = "Show HUD"
    }
}
