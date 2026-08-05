//  BootLogFile.swift
//  JARVISHUD
//
//  Writes the boot log somewhere that outlives the debugger.
//
//  WHY THIS EXISTS
//  ---------------
//  Three separate questions about this app have gone unanswered for weeks —
//  does the speaker model reach READY or time out, does the voice authority
//  install, does a capability reach the reflex — and every one of them is
//  decided by a line the backend already prints. The lines were unreadable
//  because of where they went:
//
//    * `print()` from a GUI app does not reach a redirected stdout, so
//      launching the binary with `> boot.log` produces an empty file;
//    * the backend child's output is piped into this process and reprinted,
//      inheriting the same fate;
//    * `OS_ACTIVITY_MODE=disable` — set deliberately, to make the Xcode
//      console readable — suppresses the unified-log path as well.
//
//  So the log existed only while a developer was attached to Xcode, watching.
//  **A log that requires someone to be watching is not a log**; it is a live
//  view, and it cannot answer a question about a boot that already happened.
//  That is why "the speaker model was never observed reaching READY" stayed
//  open: not because the event never happened, but because nothing recorded it.
//
//  WHAT THIS DOES NOT CHANGE
//  --------------------------
//  The console filter stays exactly as it is. Selecting a dozen interesting
//  prefixes out of a noisy startup is the right call for a human reading a
//  console in real time. It is the wrong call for the record — the line you
//  need is always the one nobody thought to whitelist. So the console keeps
//  its filter and the FILE gets everything, unfiltered.
//
//  BOUNDED, AND NEVER A REASON TO FAIL
//  ------------------------------------
//  Truncated at each launch: this answers "what happened during the boot I am
//  looking at", and an append-forever file would grow without limit for a
//  question that only concerns the last run. The previous session is kept as
//  `.1` so a crash-and-relaunch does not destroy the evidence of the crash.
//
//  Every operation is best-effort. Logging must never be the reason a HUD
//  fails to start, so a full disk, a missing directory or a permission denial
//  degrades to "no file" and the app carries on.

import Foundation

/// Append-only boot log at `~/Library/Logs/JARVIS/hud-boot.log`. NEVER throws.
final class BootLogFile: @unchecked Sendable {

    static let shared = BootLogFile()

    /// Serialises writes from the two pipe readability handlers, which are
    /// invoked on separate queues. Without this, stdout and stderr interleave
    /// mid-line and the record is harder to read than no record.
    private let queue = DispatchQueue(label: "com.jarvis.hud.bootlog")
    private var handle: FileHandle?

    /// Where the log lives. Public so the app can print the path once at
    /// startup — a log nobody can find is only marginally better than none.
    private(set) var url: URL?

    private init() { open() }

    private func open() {
        let fm = FileManager.default
        guard let logs = fm.urls(for: .libraryDirectory, in: .userDomainMask).first?
            .appendingPathComponent("Logs/JARVIS", isDirectory: true) else { return }
        do {
            try fm.createDirectory(at: logs, withIntermediateDirectories: true)
            let current = logs.appendingPathComponent("hud-boot.log")

            // Keep one generation back. When the HUD dies during boot and is
            // relaunched, the interesting log is the one that just ended.
            if fm.fileExists(atPath: current.path) {
                let previous = logs.appendingPathComponent("hud-boot.1.log")
                try? fm.removeItem(at: previous)
                try? fm.moveItem(at: current, to: previous)
            }

            fm.createFile(atPath: current.path, contents: nil)
            handle = try FileHandle(forWritingTo: current)
            url = current

            let stamp = ISO8601DateFormatter().string(from: Date())
            write("=== JARVIS HUD boot \(stamp) ===")
        } catch {
            handle = nil
            url = nil
        }
    }

    /// Record one line. Safe from any thread. NEVER throws.
    func write(_ line: String) {
        queue.async { [weak self] in
            guard let h = self?.handle,
                  let data = (line + "\n").data(using: .utf8) else { return }
            // `write(contentsOf:)` throws on a full disk; a boot log is never
            // worth taking the app down for.
            try? h.write(contentsOf: data)
        }
    }
}
