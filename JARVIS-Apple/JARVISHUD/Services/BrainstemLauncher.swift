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

final class BrainstemLauncher {
    static let shared = BrainstemLauncher()

    private var process: Process?
    private var stdoutPipe: Pipe?
    private var stderrPipe: Pipe?

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
    func start() {
        guard process == nil else {
            print("[Brainstem] Already running (PID \(process?.processIdentifier ?? 0))")
            return
        }

        let envFilePath = repoRoot + "/brainstem/.env"
        guard FileManager.default.fileExists(atPath: envFilePath) else {
            print("[Brainstem] No brainstem/.env found at \(envFilePath) — skipping auto-launch")
            return
        }

        // Load env vars from brainstem/.env for the subprocess
        var env = ProcessInfo.processInfo.environment
        if let envVars = loadEnvFile(path: envFilePath) {
            for (key, value) in envVars {
                env[key] = value
            }
        }

        // Ensure PYTHONPATH includes the repo root so `backend.*` imports work
        let existingPythonPath = env["PYTHONPATH"] ?? ""
        env["PYTHONPATH"] = existingPythonPath.isEmpty ? repoRoot : "\(repoRoot):\(existingPythonPath)"

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        proc.arguments = ["python3", "-m", "brainstem"]
        proc.currentDirectoryURL = URL(fileURLWithPath: repoRoot)
        proc.environment = env

        // Pipe stdout/stderr to Xcode console
        let stdout = Pipe()
        let stderr = Pipe()
        proc.standardOutput = stdout
        proc.standardError = stderr
        self.stdoutPipe = stdout
        self.stderrPipe = stderr

        // Async read handlers — prefix all output with [Brainstem]
        stdout.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            for line in text.components(separatedBy: "\n") where !line.isEmpty {
                print("[Brainstem] \(line)")
            }
        }
        stderr.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            for line in text.components(separatedBy: "\n") where !line.isEmpty {
                print("[Brainstem:err] \(line)")
            }
        }

        // Handle unexpected termination — log but don't restart (user can re-run)
        proc.terminationHandler = { [weak self] p in
            let code = p.terminationStatus
            print("[Brainstem] Process exited with code \(code)")
            DispatchQueue.main.async {
                self?.process = nil
                self?.stdoutPipe = nil
                self?.stderrPipe = nil
            }
        }

        do {
            try proc.run()
            self.process = proc
            print("[Brainstem] Started (PID \(proc.processIdentifier)) from \(repoRoot)")
        } catch {
            print("[Brainstem] Failed to start: \(error)")
        }
    }

    /// Gracefully stop the brainstem subprocess.
    func stop() {
        guard let proc = process, proc.isRunning else {
            process = nil
            return
        }
        print("[Brainstem] Stopping (PID \(proc.processIdentifier))...")
        proc.interrupt()  // SIGINT — triggers graceful shutdown in brainstem

        // Give it 3 seconds to shut down gracefully, then force kill
        DispatchQueue.global().asyncAfter(deadline: .now() + 3.0) { [weak self] in
            if proc.isRunning {
                print("[Brainstem] Force killing (PID \(proc.processIdentifier))")
                proc.terminate()  // SIGTERM
            }
            self?.process = nil
            self?.stdoutPipe?.fileHandleForReading.readabilityHandler = nil
            self?.stderrPipe?.fileHandleForReading.readabilityHandler = nil
            self?.stdoutPipe = nil
            self?.stderrPipe = nil
        }
    }

    /// Whether the brainstem is currently running.
    var isRunning: Bool {
        process?.isRunning ?? false
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
