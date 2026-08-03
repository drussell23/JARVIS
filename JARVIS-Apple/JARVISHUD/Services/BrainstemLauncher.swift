/// BrainstemLauncher — auto-starts the Python brainstem alongside the HUD.
///
/// When JARVIS HUD boots (Xcode Play), this spawns `python3 -m brainstem`
/// as a background subprocess. The brainstem connects to the same Vercel
/// SSE stream and handles action events (vision_task, ghost_hands, etc.)
/// that the HUD cannot execute directly.
///
/// Lifecycle: starts in applicationDidFinishLaunching, kills in quitApp().
/// Logs pipe to Xcode console via [Brainstem] prefix.
import Foundation
import Network

@MainActor
final class BrainstemLauncher {
    static let shared = BrainstemLauncher()

    private var process: Process?
    private var stdoutPipe: Pipe?
    private var stderrPipe: Pipe?

    /// TCP connection to the brainstem IPC server.
    private var connection: NWConnection?
    private let ipcPort: UInt16 = 8742
    /// HTTP port for the backend in HUD mode (separate from supervisor's 8010).
    let httpPort: UInt16 = 8011

    /// Called when the backend IPC is connected and ready for commands.
    /// AppState sets this to announce "JARVIS Online" and complete the loading screen.
    var onReady: (() -> Void)?
    private let ipcQueue = DispatchQueue(label: "com.jarvis.brainstem.ipc", qos: .userInitiated)

    /// The repo root, derived from the known brainstem .env path.
    /// Every Python on this machine that might run the brainstem, best first.
    ///
    /// NOT a hardcoded list. The previous version named three paths and leaned
    /// on `/usr/bin/env python3` for everything else — and that is a PATH
    /// LOOKUP, not an interpreter. A GUI app inherits Xcode's PATH, which has
    /// no pyenv on it, so `env python3` resolved to Homebrew's python3.14 (no
    /// uvicorn) while the only healthy interpreter on the machine —
    /// `~/.pyenv/versions/3.11.10/bin/python3`, the one every repo test already
    /// runs under — appeared in no candidate at all. Every candidate was
    /// correctly rejected; the list was simply wrong.
    ///
    /// Sources are ENUMERATED rather than named, so a Python installed
    /// tomorrow is found tomorrow:
    ///   0. `JARVIS_PYTHON` — an explicit operator override always wins
    ///   1. the repo's own venv
    ///   2. every pyenv version, newest first, plus the shim
    ///   3. every Homebrew `python3.N`, newest first
    ///   4. `/usr/local/bin` (Intel Homebrew / python.org)
    ///   5. the system Python, and finally a PATH lookup
    ///
    /// Ordering is by LIKELIHOOD OF BEING RIGHT, not by preference: a project
    /// venv is the intended environment, pyenv is what this repo's tooling
    /// actually uses, and the system Python is a last resort. Correctness does
    /// not depend on the order — `probeSucceeds` decides — but a good order
    /// means the first probe usually wins, and each probe costs a subprocess.
    private static func discoverPythons(repoRoot: String,
                                        environment: [String: String]) -> [String] {
        let fm = FileManager.default
        var out: [String] = []
        func add(_ path: String) {
            // Resolve so a shim, a symlink and a real binary are not probed
            // three times as if they were three different interpreters.
            let real = (try? fm.destinationOfSymbolicLink(atPath: path)) ?? path
            let key = real.hasPrefix("/") ? real : path
            if !out.contains(path) && !out.contains(key) { out.append(path) }
        }
        /// Newest-first: `python3.14` should be tried before `python3.9`, and a
        /// plain lexical sort puts 3.9 above 3.14.
        func versionedChildren(of dir: String, prefix: String) -> [String] {
            guard let names = try? fm.contentsOfDirectory(atPath: dir) else { return [] }
            return names
                .filter { $0.hasPrefix(prefix) && !$0.hasSuffix("-config") }
                .sorted { a, b in
                    a.compare(b, options: .numeric) == .orderedDescending
                }
                .map { dir + "/" + $0 }
        }

        if let explicit = environment["JARVIS_PYTHON"], !explicit.isEmpty {
            add(explicit)
        }
        add("\(repoRoot)/venv/bin/python3")
        for p in versionedChildren(of: "\(repoRoot)/venv/bin", prefix: "python3.") { add(p) }

        let home = NSHomeDirectory()
        let pyenvRoot = environment["PYENV_ROOT"] ?? "\(home)/.pyenv"
        for v in versionedChildren(of: "\(pyenvRoot)/versions", prefix: "3.") {
            add("\(v)/bin/python3")
        }
        add("\(pyenvRoot)/shims/python3")

        for dir in ["/opt/homebrew/bin", "/usr/local/bin"] {
            for p in versionedChildren(of: dir, prefix: "python3.") { add(p) }
            add("\(dir)/python3")
        }
        add("/usr/bin/python3")

        return out.filter { fm.isExecutableFile(atPath: $0) }
    }

    /// Can this interpreter actually import what the brainstem needs?
    ///
    /// Bounded on purpose: a hung probe would stall the HUD's startup, and an
    /// interpreter that cannot answer in a few seconds is not one to launch a
    /// backend with. A timeout counts as FAILURE — the candidate has to earn
    /// selection, not merely avoid disproving itself.
    private static func probeSucceeds(executable: String, arguments: [String],
                                      environment: [String: String],
                                      cwd: String,
                                      timeout: TimeInterval = 12) -> Bool {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: executable)
        p.arguments = arguments
        p.environment = environment
        p.currentDirectoryURL = URL(fileURLWithPath: cwd)
        // Discarded rather than piped: an unread pipe that fills would block
        // the child forever, which is the deadlock a health check must not add.
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return false }

        let deadline = Date().addingTimeInterval(timeout)
        while p.isRunning && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.05)
        }
        if p.isRunning {
            p.terminate()
            return false
        }
        return p.terminationStatus == 0
    }

    private let repoRoot: String = {
        // Same path the HUD uses to find brainstem/.env
        let home = NSHomeDirectory()
        let candidates = [
            home + "/Documents/repos/JARVIS-AI-Agent",
        ]
        for path in candidates {
            if FileManager.default.fileExists(atPath: path + "/brainstem/.env") {
                return path
            }
        }
        // Fallback: try relative to current directory
        let cwd = FileManager.default.currentDirectoryPath
        if FileManager.default.fileExists(atPath: cwd + "/brainstem/.env") {
            return cwd
        }
        // Last resort
        return home + "/Documents/repos/JARVIS-AI-Agent"
    }()

    private init() {}

    /// Spawn the brainstem. Safe to call multiple times — only starts once.
    /// Kills any stale Python processes on HUD ports from a previous Xcode run.
    func start() {
        guard process == nil else {
            print("[Brainstem] Already running (PID \(process?.processIdentifier ?? 0))")
            return
        }

        // EXTERNAL-BACKEND MODE (Phase 11): when the HUD is run against an
        // already-running unified_supervisor (localhost:8010), do NOT spawn
        // the HUD's own brainstem — that would collide on ports/creds and
        // fork the backend. The HUD just connects to the external backend
        // (SSE + /api/command). Set JARVIS_HUD_EXTERNAL_BACKEND=1 in the
        // Xcode scheme's environment to enable.
        if (ProcessInfo.processInfo.environment["JARVIS_HUD_EXTERNAL_BACKEND"] ?? "") == "1" {
            print("[Brainstem] JARVIS_HUD_EXTERNAL_BACKEND=1 — connecting to the external backend (localhost:8010), NOT spawning a local brainstem")
            return
        }

        // Kill stale processes from previous Xcode runs that didn't clean up.
        // When Xcode kills the HUD, the child Python process can survive as an orphan.
        killStaleProcesses()

        let brainstemEnv = repoRoot + "/brainstem/.env"
        guard FileManager.default.fileExists(atPath: brainstemEnv) else {
            print("[Brainstem] No brainstem/.env found at \(brainstemEnv) — skipping auto-launch")
            return
        }

        // Layer env files: root .env (API keys) → backend/.env → brainstem/.env (connection creds)
        // Later files override earlier ones, so brainstem-specific values always win.
        var env = ProcessInfo.processInfo.environment
        let envFiles = [
            repoRoot + "/.env",
            repoRoot + "/backend/.env",
            brainstemEnv,
        ]
        for path in envFiles {
            if let vars = loadEnvFile(path: path) {
                for (key, value) in vars {
                    env[key] = value
                }
            }
        }

        // Ensure PYTHONPATH includes the repo root AND Homebrew site-packages.
        // Xcode's subprocess environment may not include Homebrew's default paths.
        let sitePackages = "/opt/homebrew/lib/python3.12/site-packages"
        let existingPythonPath = env["PYTHONPATH"] ?? ""
        let pathParts = [repoRoot, sitePackages, existingPythonPath].filter { !$0.isEmpty }
        env["PYTHONPATH"] = pathParts.joined(separator: ":")

        // Ensure PATH includes Homebrew so Python 3.12 can find its packages/tools
        let existingPath = env["PATH"] ?? "/usr/bin:/bin"
        if !existingPath.contains("/opt/homebrew") {
            env["PATH"] = "/opt/homebrew/bin:/opt/homebrew/sbin:\(existingPath)"
        }

        // Remove PYTHONHOME if set — it breaks Homebrew Python's module search
        env.removeValue(forKey: "PYTHONHOME")

        // v351.0: HUD mode — full backend stack on separate port from supervisor
        env["JARVIS_MODE"] = "hud"
        env["JARVIS_HUD_PORT"] = String(httpPort)

        let proc = Process()
        // v351.0: Use the project's venv Python — it has uvicorn, FastAPI,
        // and all backend dependencies installed. The bare Homebrew python3.12
        // doesn't have them, causing "ModuleNotFoundError: No module named 'uvicorn'".
        //
        // Priority: venv/bin/python3.12 > /opt/homebrew/bin/python3.12 > /usr/bin/env python3

        // v286.0: CHOOSE AN INTERPRETER THAT WORKS, NOT ONE THAT EXISTS.
        //
        // This used to take the first candidate whose FILE was present. That is
        // the wrong predicate, and it failed in the field: `venv/` inside this
        // repo had 5,980 files whose originals were replaced by iCloud
        // duplicate-renames (`auto.py` -> `auto 2.py`), so
        // `uvicorn/protocols/http/` had no `__init__.py` and uvicorn could not
        // start. The venv binary existed, was selected, and every launch died
        // with `Could not import module "uvicorn.protocols.http.auto"` — while
        // a perfectly healthy pyenv interpreter sat two candidates further down
        // the list, never reached.
        //
        // So each candidate is PROBED with the exact import whose absence
        // killed the brainstem — a test of the failure actually hit rather than
        // a guess at what "healthy" means. Bounded, so a wedged interpreter
        // cannot hang startup, and every rejection is logged BY REASON: a
        // launcher that silently picks the third choice is undebuggable.
        var discovered = Self.discoverPythons(repoRoot: repoRoot, environment: env)
        // A PATH lookup last, never first: it is not an interpreter, it is
        // whatever this process's PATH happens to resolve — the very
        // indirection that hid the working Python behind a broken one.
        var candidates: [(String, [String])] = discovered.map { ($0, ["-m", "brainstem"]) }
        candidates.append(("/usr/bin/env", ["python3", "-m", "brainstem"]))
        print("[Brainstem] \(discovered.count) candidate interpreter(s) discovered")
        var chosen: (String, [String])?
        for (path, args) in candidates {
            if path != "/usr/bin/env",
               !FileManager.default.fileExists(atPath: path) {
                print("[Brainstem] skip \(path) — not present")
                continue
            }
            let probeArgs = Array(args.dropLast(2))
                + ["-c", "import uvicorn.protocols.http.auto"]
            if Self.probeSucceeds(executable: path, arguments: probeArgs,
                                  environment: env, cwd: repoRoot) {
                chosen = (path, args)
                break
            }
            print("[Brainstem] reject \(path) — cannot import "
                  + "uvicorn.protocols.http.auto (broken or incomplete install)")
        }

        guard let picked = chosen else {
            print("""
            [Brainstem] FATAL: no usable Python interpreter.
              Tried: \(candidates.map(\.0).joined(separator: ", "))
              Each failed to `import uvicorn.protocols.http.auto`.
              If `\(repoRoot)/venv` was touched by iCloud, files may have been
              renamed (`auto.py` -> `auto 2.py`); rebuild the venv or remove it
              so a healthy system interpreter is used instead.
            """)
            return
        }
        let python = picked.0
        let pythonArgs = picked.1
        print("[Brainstem] interpreter: \(python) (probe passed)")

        proc.executableURL = URL(fileURLWithPath: python)
        proc.arguments = pythonArgs
        proc.currentDirectoryURL = URL(fileURLWithPath: repoRoot)
        proc.environment = env

        // No stdin pipe needed — HUD communicates via TCP IPC

        // Pipe stdout/stderr to Xcode console
        let stdout = Pipe()
        let stderr = Pipe()
        proc.standardOutput = stdout
        proc.standardError = stderr
        self.stdoutPipe = stdout
        self.stderrPipe = stderr

        // Async read handlers — filter backend logs for readability.
        // Only show important events in Xcode console, not every startup detail.
        //
        // Shown (always):
        //   [HUD]      — HUD mode events (IPC, VLA dispatch)
        //   [IPC]      — IPC connection lifecycle
        //   [Dispatch]  — VLA step execution
        //   [JarvisCU] — Vision planning/execution
        //   [CUTaskPlanner] — Step planning
        //   [CUExec]   — Click/type execution
        //   ERROR/WARNING — All errors and warnings
        //   HUD MODE   — Mode banner
        //   INTERACTIVE — Ready signal
        //
        // Hidden: verbose startup (AI loader, Cloud SQL, memory, cost tracker, etc.)

        let importantPatterns = [
            "[HUD]", "[IPC]", "[Dispatch]", "[JarvisCU]", "[CUTaskPlanner]", "[CUExec]",
            "ERROR", "WARNING", "HUD MODE", "INTERACTIVE MODE", "JARVIS Online",
            "Application startup complete", "Uvicorn running", "[Boot]",
            "[SSE]", "[AgentRuntime]", "Ghost Hands", "FramePipeline",
            "VLA", "vision_task", "screenshot",
        ]

        stdout.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            for line in text.components(separatedBy: "\n") where !line.isEmpty {
                print("[Brainstem] \(line)")
            }
        }
        stderr.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            for line in text.components(separatedBy: "\n") where !line.isEmpty {
                // Only show important log lines — filter out verbose startup noise
                let isImportant = importantPatterns.contains { line.contains($0) }
                if isImportant {
                    print("[Backend] \(line)")
                }

                // Trigger IPC connection when brainstem's TCP server is actually listening.
                // This prevents connecting to a stale socket from a previous process.
                if line.contains("[IPC] TCP server listening") {
                    Task { @MainActor in
                        self?.connectToBrainstem(retriesLeft: 5)
                    }
                }
            }
        }

        // Handle unexpected termination — log but don't restart (user can re-run).
        // Use Task { @MainActor in } to hop back to the main actor for property mutation.
        proc.terminationHandler = { [weak self] p in
            let code = p.terminationStatus
            print("[Brainstem] Process exited with code \(code)")
            Task { @MainActor in
                self?.process = nil
                self?.stdoutPipe = nil
                self?.stderrPipe = nil
            }
        }

        do {
            try proc.run()
            self.process = proc
            print("[Brainstem] Started (PID \(proc.processIdentifier)) from \(repoRoot)")

            // IPC connection is deferred until the brainstem logs
            // "[IPC] TCP server listening" — see stderr handler above.
            // This prevents connecting to a stale socket from a previous
            // brainstem process and ensures the connection reaches the
            // correct server instance.
        } catch {
            print("[Brainstem] Failed to start: \(error)")
        }
    }

    /// Gracefully stop the brainstem subprocess.
    func stop() {
        // Tear down TCP connection first
        connection?.cancel()
        connection = nil

        guard let proc = process, proc.isRunning else {
            process = nil
            return
        }
        print("[Brainstem] Stopping (PID \(proc.processIdentifier))...")
        proc.interrupt()  // SIGINT — triggers graceful shutdown in brainstem

        // Give it 3 seconds to shut down gracefully, then force kill.
        // Task inherits @MainActor isolation from the enclosing context.
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            if proc.isRunning {
                print("[Brainstem] Force killing (PID \(proc.processIdentifier))")
                proc.terminate()  // SIGTERM
            }
            self?.stdoutPipe?.fileHandleForReading.readabilityHandler = nil
            self?.stderrPipe?.fileHandleForReading.readabilityHandler = nil
            self?.process = nil
            self?.stdoutPipe = nil
            self?.stderrPipe = nil
        }
    }

    /// Pending events queued while IPC is connecting (backend still booting).
    /// Replayed in order once IPC connects.
    private var pendingEvents: [(eventType: String, data: [String: Any])] = []
    private let maxPendingEvents = 20

    /// Send an action event to the brainstem via the TCP IPC connection.
    /// If IPC isn't connected yet (backend still booting), queues the event
    /// and replays it once the connection is established.
    func sendEvent(eventType: String, data: [String: Any]) {
        guard process?.isRunning == true else {
            print("[Brainstem] Cannot send event — process not running")
            return
        }
        guard let conn = connection, conn.state == .ready else {
            // Queue for replay when IPC connects
            if pendingEvents.count < maxPendingEvents {
                pendingEvents.append((eventType: eventType, data: data))
                print("[Brainstem] IPC not ready — queued event '\(eventType)' (\(pendingEvents.count) pending, backend still booting)")
            } else {
                print("[Brainstem] IPC not ready — queue full, dropping event '\(eventType)'")
            }
            return
        }
        do {
            let jsonData = try JSONSerialization.data(withJSONObject: [
                "event_type": eventType,
                "data": data,
            ])
            // Build a newline-terminated JSON line
            var line = jsonData
            line.append(0x0A) // newline
            let byteCount = line.count  // Capture for Sendable closure
            print("[Brainstem] sendEvent: \(eventType) (\(byteCount) bytes) via TCP")

            conn.send(content: line, completion: .contentProcessed { error in
                if let error = error {
                    print("[Brainstem] TCP send error for \(eventType): \(error)")
                } else {
                    print("[Brainstem] Forwarded event: \(eventType) (\(byteCount) bytes) via TCP")
                }
            })
        } catch {
            print("[Brainstem] Failed to serialize event: \(error)")
        }
    }

    // MARK: - TCP IPC Connection

    /// Connect to the brainstem's TCP IPC server with retry.
    /// The brainstem takes ~11s to boot before the IPC server binds,
    /// so we retry every 1s with enough headroom for slow starts.
    private func connectToBrainstem(retriesLeft: Int) {
        guard retriesLeft > 0, process?.isRunning == true else {
            if retriesLeft <= 0 {
                print("[Brainstem] IPC connection failed after all retries")
            }
            return
        }

        let host = NWEndpoint.Host("127.0.0.1")
        let port = NWEndpoint.Port(rawValue: ipcPort)!
        let conn = NWConnection(host: host, port: port, using: .tcp)

        conn.stateUpdateHandler = { [weak self] state in
            guard let self = self else { return }
            switch state {
            case .ready:
                print("[Brainstem] IPC connected to localhost:\(self.ipcPort)")
                Task { @MainActor in
                    self.connection = conn

                    // Start receiving events from the brainstem
                    self.startReceiveLoop(conn)

                    // Notify HUD that JARVIS is online and ready for commands
                    self.onReady?()

                    // Replay any events queued while backend was booting
                    if !self.pendingEvents.isEmpty {
                        print("[Brainstem] Replaying \(self.pendingEvents.count) queued event(s)")
                        let queued = self.pendingEvents
                        self.pendingEvents.removeAll()
                        for event in queued {
                            self.sendEvent(eventType: event.eventType, data: event.data)
                        }
                    }
                }
            case .failed(let error):
                print("[Brainstem] IPC connection failed: \(error) — retries left: \(retriesLeft - 1)")
                // THE STRUCTURAL CRASH-SAFETY. If the backend dies mid-utterance
                // its "stopped speaking" message never arrives — and with the
                // old `/tmp/jarvis_speaking` lockfile the flag survived the
                // process, leaving the mic deaf until somebody deleted the file
                // by hand. A socket cannot lie about this: the connection
                // dropping IS the notification, so the claim goes with it.
                Task { @MainActor in
                    SpeechGate.shared.release(.backend, reason: "IPC connection lost")
                }
                conn.cancel()
                self.ipcQueue.asyncAfter(deadline: .now() + 1.0) { [weak self] in
                    Task { @MainActor in
                        self?.connectToBrainstem(retriesLeft: retriesLeft - 1)
                    }
                }
            case .waiting(let error):
                // .waiting means the OS is still attempting — connection refused
                // during brainstem boot. Cancel and retry after a delay.
                print("[Brainstem] IPC connection waiting: \(error) — retries left: \(retriesLeft - 1)")
                conn.cancel()
                self.ipcQueue.asyncAfter(deadline: .now() + 1.0) { [weak self] in
                    Task { @MainActor in
                        self?.connectToBrainstem(retriesLeft: retriesLeft - 1)
                    }
                }
            case .cancelled:
                Task { @MainActor in
                    SpeechGate.shared.release(.backend, reason: "IPC cancelled")
                }
            default:
                break
            }
        }

        conn.start(queue: ipcQueue)
    }

    // MARK: - IPC Receive Loop

    /// Buffer for partial JSON lines received over TCP.
    private var receiveBuffer = Data()

    /// Recursively receive data from the brainstem IPC connection.
    /// Parses newline-delimited JSON IPC events. (The Hive event lane moved to
    /// the `ov` cockpit — hive-typed events fall through to the log line.)
    private func startReceiveLoop(_ conn: NWConnection) {
        conn.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] data, _, isComplete, error in
            guard let self = self else { return }

            if let data = data, !data.isEmpty {
                Task { @MainActor in
                    self.receiveBuffer.append(data)
                    self.processReceiveBuffer()
                }
            }

            if isComplete {
                print("[Brainstem] IPC connection closed by peer")
                return
            }

            if let error = error {
                print("[Brainstem] IPC receive error: \(error)")
                return
            }

            // Continue receiving. The receive handler runs on the background
            // ipcQueue, so hop to the MainActor natively (NWConnection is
            // Sendable) to re-enter the isolated loop.
            Task { @MainActor in
                self.startReceiveLoop(conn)
            }
        }
    }

    /// Process complete newline-delimited JSON lines from the receive buffer.
    private func processReceiveBuffer() {
        while let newlineIndex = receiveBuffer.firstIndex(of: 0x0A) {
            let lineData = receiveBuffer[receiveBuffer.startIndex..<newlineIndex]
            receiveBuffer = Data(receiveBuffer[receiveBuffer.index(after: newlineIndex)...])

            guard !lineData.isEmpty else { continue }

            do {
                guard let json = try JSONSerialization.jsonObject(with: lineData) as? [String: Any],
                      let eventType = json["event_type"] as? String else {
                    continue
                }

                print("[Brainstem] Received IPC event: \(eventType)")
                dispatchInbound(eventType: eventType, json: json)
            } catch {
                // Skip malformed lines silently
                continue
            }
        }
    }

    /// Route one inbound backend event to whoever handles it.
    ///
    /// The socket carried traffic in one direction for its whole life and this
    /// was a `print` with a comment about the future. That is why the consent
    /// gate was decorative: `SecureConsent` was complete and waiting for a
    /// challenge, `main.py` was handling the verdict, and the question in
    /// between was never delivered — so every gated capability on the backend
    /// resolved to "no approval provider available" and the operator was never
    /// asked anything.
    ///
    /// Unknown event types are IGNORED rather than treated as errors: the
    /// backend ships independently of this app, and a HUD that logged a warning
    /// for every event it had not learned about yet would be noisy about
    /// nothing.
    private func dispatchInbound(eventType: String, json: [String: Any]) {
        let data = (json["data"] as? [String: Any]) ?? [:]

        switch eventType {
        case "consent_request":
            // Fields are lifted to Strings HERE, before the hop. `[String: Any]`
            // is not Sendable, so capturing `data` in a `@MainActor` Task is a
            // Swift 6 concurrency error — and the fix is not to silence it. Only
            // these four values are needed, all of them Strings, all Sendable.
            let requestId = (data["request_id"] as? String) ?? ""
            let nonce = (data["nonce"] as? String) ?? ""
            let capability = (data["capability"] as? String) ?? "unknown"
            let detail = (data["detail"] as? String) ?? ""

            // `SecureConsent` is MainActor-isolated: LAContext must present its
            // dialog from the main thread, and this handler runs on the
            // connection's background queue.
            Task { @MainActor in
                // Parsing fails CLOSED on a malformed challenge — a request we
                // cannot bind to a nonce is one we must not answer at all,
                // because a verdict that cannot prove which question it answers
                // is replayable by anything that can write to the socket.
                guard let challenge = SecureConsent.Challenge([
                    "request_id": requestId,
                    "nonce": nonce,
                    "capability": capability,
                    "detail": detail,
                ]) else {
                    print("[Brainstem] consent_request rejected — malformed challenge")
                    return
                }
                SecureConsent.shared.request(challenge)
            }

        case "speech_state":
            // The backend is speaking (or has stopped). Values are lifted to
            // Sendable scalars before the actor hop, same as the consent case.
            let speaking = (data["speaking"] as? Bool) ?? false
            let deadlineMs = (data["deadline_ms"] as? Double) ?? 0
            let nowMs = (data["now_ms"] as? Double) ?? 0
            let source = (data["source"] as? String) ?? "backend"

            Task { @MainActor in
                if speaking && deadlineMs > nowMs {
                    // The deadline is trusted for its DURATION, never for its
                    // absolute value — the two processes do not share a clock,
                    // and a backend running five minutes fast must not be able
                    // to mute this microphone for five minutes.
                    SpeechGate.shared.claim(.backend,
                                            untilEpochMs: deadlineMs,
                                            nowEpochMs: nowMs,
                                            reason: "backend:\(source)")
                } else {
                    // Covers "stopped" AND a stale frame that arrived after its
                    // own deadline — both mean the backend is not speaking now.
                    SpeechGate.shared.release(.backend, reason: "backend idle")
                }
            }

        default:
            break
        }
    }

    /// Whether the brainstem is currently running.
    var isRunning: Bool {
        process?.isRunning ?? false
    }

    // MARK: - Stale process cleanup

    /// Kill orphaned Python processes from previous Xcode runs.
    /// When Xcode's Stop button kills the HUD, the child Python backend
    /// can survive as an orphan, holding ports 8011 and 8742.
    private func killStaleProcesses() {
        let ports = [httpPort, ipcPort]
        for port in ports {
            let task = Process()
            task.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            task.arguments = ["bash", "-c", "lsof -ti :\(port) | xargs kill -9 2>/dev/null"]
            try? task.run()
            task.waitUntilExit()
        }
        // Wait for ports to actually be released (SIGKILL + TIME_WAIT)
        for attempt in 1...5 {
            Thread.sleep(forTimeInterval: 0.5)
            // Check if ports are free
            var allFree = true
            for port in ports {
                let check = Process()
                let pipe = Pipe()
                check.executableURL = URL(fileURLWithPath: "/usr/bin/env")
                check.arguments = ["bash", "-c", "lsof -ti :\(port) 2>/dev/null"]
                check.standardOutput = pipe
                try? check.run()
                check.waitUntilExit()
                let output = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
                if !output.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    allFree = false
                }
            }
            if allFree {
                print("[Brainstem] Ports \(ports) cleared (attempt \(attempt))")
                return
            }
        }
        print("[Brainstem] Warning: ports may still be in use after cleanup")
    }

    // MARK: - Env file parser

    private func loadEnvFile(path: String) -> [String: String]? {
        guard let contents = try? String(contentsOfFile: path, encoding: .utf8) else { return nil }
        var env: [String: String] = [:]
        for line in contents.components(separatedBy: "\n") {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            guard !trimmed.isEmpty, !trimmed.hasPrefix("#"),
                  let eqIdx = trimmed.firstIndex(of: "=") else { continue }
            let key = String(trimmed[trimmed.startIndex..<eqIdx])
            let value = String(trimmed[trimmed.index(after: eqIdx)...])
                .trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
            env[key] = value
        }
        return env
    }
}
