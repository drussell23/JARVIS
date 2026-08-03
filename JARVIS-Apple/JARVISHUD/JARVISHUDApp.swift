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
    let wakeWord = WakeWordListener()
    private let tts = AVSpeechSynthesizer()

    // True while JARVIS TTS is playing — prevents the mic from restarting mid-speech
    // and feeding JARVIS's own voice back as a command.
    private var isTTSSpeaking = false

    private var borderWindow: LivingBorderWindow?
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
            // `AVSpeechSynthesizer.isSpeaking` is the authority for this synth;
            // a dropped delegate callback now costs one 250ms sweep, not the
            // whole estimated deadline.
            SpeechGate.shared.registerReconciler(.hudSynthesizer) { [weak self] in
                self?.tts.isSpeaking ?? false
            }
            BrainstemLauncher.shared.onReady = { [weak self] in
                guard let self = self else { return }
                self.appState.pythonBridge.onBackendReady()
            }
            BrainstemLauncher.shared.start()
            self.appState.boot()

            // Living Border setup — the halo is always visible (JARVIS is ambient:
            // just the green halo + the menu-bar icon; no panel to take over the screen).
            self.setupWindows()

            // Wire border color to backend connectivity. (The cognitive-state
            // tint rode the Hive relay, which was never live and now feeds the
            // `ov` cockpit instead — connectivity is the honest signal here.)
            Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
                guard let self else { return }
                Task { @MainActor in
                    let state: BorderState =
                        self.appState.pythonBridge.connectionStatus == .disconnected
                        ? .offline : .baseline
                    self.borderWindow?.updateState(state)
                }
            }

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

            // JARVIS is ambient — the halo + menu-bar icon are the whole HUD, there
            // is no panel to summon. "Show yourself" acknowledges presence by voice
            // rather than taking over the screen; everything operational lives in the
            // O+V CLI/TUI.
            let lower = command.lowercased().trimmingCharacters(in: .whitespaces)
            if lower.contains("show yourself") || lower.contains("appear")
                || lower.contains("are you there") || lower.contains("you there") {
                self.speak("I'm online and listening.")
                return
            }

            // ============================================================
            // UNIFIED COMMAND ROUTING (Phase 11): route the raw command to
            // whichever backend is actually live — the local brainstem IPC
            // when it's running, else the connected EXTERNAL backend over
            // the verified SSE/HTTP path (AppState.sendCommand → /api/
            // command). One command, whichever body is alive — no
            // "still starting" dead-end when running against
            // unified_supervisor. Swift stays a DUMB PIPE either way (no
            // Tier-0 logic, no app resolution, no hardcoded routing).
            // ============================================================
            guard BrainstemLauncher.shared.isRunning else {
                // External-backend mode: send over the SSE/HTTP path. The
                // response streams back via the SSE event channel and is
                // spoken by AppState's stream handler.
                if self.appState.pythonBridge.connectionStatus == .connected {
                    print("[JARVIS] → external backend (SSE/HTTP): \(command)")
                    Task { @MainActor in
                        do {
                            try await self.appState.pythonBridge.sendCommand(
                                command, intentHint: "voice")
                        } catch {
                            print("[JARVIS] external command failed: \(error)")
                            self.speak("I couldn't reach the backend.")
                        }
                    }
                } else {
                    print("[JARVIS] Backend not connected — cannot execute command")
                    self.speak("Backend is still starting. Try again in a moment.")
                }
                return
            }

            print("[JARVIS] → Brainstem: \(command)")

            Task {
                var actionPayload: [String: Any] = [
                    "goal": command,
                    "source": "voice_command",
                ]

                // Attach a fresh screenshot so the backend can see what's on screen
                if let b64 = await ScreenCaptureService.shared.captureFresh() {
                    actionPayload["screenshot"] = b64
                    print("[JARVIS] Screenshot attached (\(b64.count / 1024)KB)")
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

        // v285.0: mute the tap, do not tear the audio graph down.
        //
        // This used to call `wakeWord.stop()` — destroying a working
        // AVAudioEngine on every sentence — and `didFinish` then slept three
        // seconds before rebuilding, because AVSpeechSynthesizer holds the
        // output device for ~2-3s and rebuilding early throws -10877. The sleep
        // was a workaround for a teardown that never needed to happen, and it
        // left a three-second deaf window after every utterance.
        //
        // The claim is taken HERE rather than in `didStart` so the window
        // between queueing and the first sample is covered too; `didStart`
        // extends it with a real duration once speaking actually begins.
        isTTSSpeaking = true
        SpeechGate.shared.claim(.hudSynthesizer,
                                seconds: Self.estimatedSpeechSeconds(cleaned),
                                reason: "queued utterance")

        let utterance = AVSpeechUtterance(string: cleaned)
        utterance.voice = AVSpeechSynthesisVoice(identifier: "com.apple.voice.compact.en-GB.Daniel")
            ?? AVSpeechSynthesisVoice(language: "en-GB")
        utterance.rate = 0.52
        utterance.volume = 0.85

        tts.speak(utterance)
    }

    /// Roughly how long this text takes to say, deliberately OVER-estimated.
    ///
    /// Under-estimating opens the mic while JARVIS is still talking, which
    /// feeds the loop this exists to break. Over-estimating costs a fraction of
    /// a second of not being heard. The asymmetry is why this is not a tight
    /// fit — and it is only a CEILING regardless: `didFinish` releases the
    /// claim the instant speech actually ends.
    ///
    /// `rate = 0.52` is below AVSpeechUtterance's default, so ~12 chars/second.
    /// `nonisolated` because it is a pure function of its argument and is
    /// called from the delegate callbacks, which arrive on an unspecified
    /// queue. Nothing here touches actor state, so hopping to the MainActor
    /// just to do arithmetic would be ceremony.
    nonisolated static func estimatedSpeechSeconds(_ text: String) -> Double {
        min(1.5 + Double(text.count) / 12.0, 60.0)
    }

    // The utterance actually began. Extend the claim with a real duration now
    // that the synthesiser has committed to speaking.
    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didStart utterance: AVSpeechUtterance) {
        let seconds = Self.estimatedSpeechSeconds(utterance.speechString)
        Task { @MainActor in
            SpeechGate.shared.claim(.hudSynthesizer, seconds: seconds,
                                    reason: "speaking")
        }
    }

    // Speech ended normally. The mic is live again on THIS runloop turn —
    // nothing was torn down, so there is nothing to rebuild and no 3s wait.
    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        Task { @MainActor [weak self] in
            guard let self else { return }
            self.isTTSSpeaking = false
            SpeechGate.shared.release(.hudSynthesizer, reason: "didFinish")
        }
    }

    // THE CALLBACK THAT WAS MISSING.
    //
    // A cancelled utterance never fires `didFinish`. With the old code that
    // left `isTTSSpeaking == true` and the mic stopped, with nothing left to
    // restart it: permanent deafness, silent, and reproducible simply by
    // interrupting JARVIS mid-sentence — which `VoiceManager.speak` does on
    // purpose whenever a higher-priority utterance arrives.
    //
    // The gate's deadline would now expire this claim anyway; implementing the
    // callback means the mic returns immediately instead of at the deadline.
    // Both layers, because the one we thought of is not the only one.
    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) {
        Task { @MainActor [weak self] in
            guard let self else { return }
            self.isTTSSpeaking = false
            SpeechGate.shared.release(.hudSynthesizer, reason: "didCancel")
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

    // No app resolution, no command parsing, no Tier 0 logic in Swift.
    // The HUD is a dumb pipe — all intelligence lives in the Python
    // brainstem where Ouroboros + VLA handle everything per the Manifesto.

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

    // MARK: - Living Border (the ambient halo — the whole visible HUD)

    private func setupWindows() {
        // Border window — always on, always breathing. This is the only visible
        // surface; the operational UI lives in the O+V CLI/TUI.
        if borderWindow == nil {
            borderWindow = LivingBorderWindow()
        }
    }
}
