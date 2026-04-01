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
        let venvPython = "\(repoRoot)/venv/bin/python3.12"
        let brewPython = "/opt/homebrew/bin/python3.12"
        let python: String
        let pythonArgs: [String]

        if FileManager.default.fileExists(atPath: venvPython) {
            python = venvPython
            pythonArgs = ["-m", "brainstem"]
        } else if FileManager.default.fileExists(atPath: brewPython) {
            python = brewPython
            pythonArgs = ["-m", "brainstem"]
        } else {
            python = "/usr/bin/env"
            pythonArgs = ["python3", "-m", "brainstem"]
        }

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
                break
            default:
                break
            }
        }

        conn.start(queue: ipcQueue)
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
        // Brief pause to let the OS release the ports
        Thread.sleep(forTimeInterval: 0.3)
        print("[Brainstem] Cleaned up stale processes on ports \(ports)")
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
