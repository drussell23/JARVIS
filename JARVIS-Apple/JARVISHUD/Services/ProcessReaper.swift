/// ProcessReaper — ask a process to leave, then confirm that it did.
///
/// WHY THIS EXISTS
/// ---------------
/// `terminateOlderInstancesIfNeeded()` called `NSRunningApplication.forceTerminate()`
/// and moved on. That method returns `Bool` and the result was discarded, nothing
/// waited, and nothing verified — so when it failed it failed *silently*.
///
/// Measured 2026-08-06: a new HUD started at 12:04:24 while pid 80876 from the
/// previous run was still alive, and was still alive minutes later. The code
/// written to prevent exactly that had already run and reported nothing, because
/// there was nothing in it capable of reporting.
///
/// A request is not an outcome. Every reap here is verified against the kernel
/// (`kill(pid, 0)`), and a reap that could not be completed says so.
///
/// GRACE BEFORE FORCE, AND WHY IT IS NOT POLITENESS
/// ------------------------------------------------
/// The old code opened with force. `forceTerminate` is uncatchable, so the HUD
/// being killed never runs its shutdown — and its Python brainstem child is
/// therefore orphaned at the moment we kill its parent. Reaping one orphan by
/// creating another is not a reap.
///
/// SIGTERM first gives the target the chance to take its own children with it.
/// This is the same discipline `BrainstemLauncher.killStaleProcesses()` already
/// documents: *"an orphan that has just been told to leave should get the chance
/// to close its sockets and flush... SIGKILL only for what ignores that."*
/// That reaper still has its own copy of the escalation loop; adopting this type
/// there means making a synchronous startup path async, which is a larger change
/// than this fix should carry. Named here so it is a known follow-up rather than
/// an unremarked divergence.
///
/// ASYNC, NOT `Thread.sleep`
/// -------------------------
/// Waiting is done with `Task.sleep`, so the grace period does not block the
/// thread that is waiting. The caller is app startup on the main actor; blocking
/// it for the full grace window would freeze the UI of the instance that is
/// trying to come up.

import Foundation

@MainActor
enum ProcessReaper {

    /// What happened to one process.
    enum Outcome {
        /// Gone after being asked politely.
        case exitedGracefully
        /// Ignored the request; required force.
        case forced
        /// Still alive after everything. The caller must decide what that means.
        case survived
        /// Already gone before we started.
        case alreadyGone
    }

    /// Total grace before escalating to force, in seconds.
    /// Env-overridable so a slow machine can be given more without a rebuild.
    private static var graceSeconds: Double {
        if let raw = ProcessInfo.processInfo.environment["JARVIS_REAP_GRACE_S"],
           let value = Double(raw), value > 0, value <= 30 {
            return value
        }
        return 2.0
    }

    /// How often to re-ask the kernel whether the process is gone.
    private static var pollSeconds: Double {
        if let raw = ProcessInfo.processInfo.environment["JARVIS_REAP_POLL_S"],
           let value = Double(raw), value > 0, value <= 1 {
            return value
        }
        return 0.1
    }

    /// Is this pid still alive, according to the kernel?
    ///
    /// `kill(pid, 0)` sends no signal; it only performs the existence and
    /// permission check. This is the authority — an `NSRunningApplication` handle
    /// can go stale, and a `Bool` returned by a termination request describes
    /// only whether the request was accepted.
    static func isAlive(_ pid: pid_t) -> Bool {
        guard pid > 1 else { return false }
        return kill(pid, 0) == 0
    }

    /// One process to reap, and how to ask it.
    ///
    /// `requestExit`/`force` are closures rather than a fixed signal pair so an
    /// `NSRunningApplication` caller can route through AppKit's quit path, which
    /// lets the target run its shutdown and take its own children with it.
    /// A signal-only reaper could not express that.
    struct Target {
        let pid: pid_t
        let requestExit: () -> Void
        let force: () -> Void

        /// Signal-based target, for a bare pid with no app handle.
        static func signals(_ pid: pid_t) -> Target {
            Target(pid: pid,
                   requestExit: { kill(pid, SIGTERM) },
                   force: { kill(pid, SIGKILL) })
        }
    }

    /// Reap a set of processes in PHASES: ask all, wait once, force the
    /// survivors, wait once, report.
    ///
    /// Phased rather than one concurrent task per process, for two reasons.
    ///
    /// The practical one: N stale instances would otherwise cost N grace periods
    /// in series if reaped in a loop, and startup is waiting on all of them. One
    /// shared wait covers every target at once.
    ///
    /// The structural one: a task group would have to carry `NSRunningApplication`
    /// — which is not `Sendable` — across an isolation boundary. Swift 6's
    /// region-based isolation checker rejects that outright, and the honest
    /// response is to stop crossing the boundary rather than to annotate the
    /// crossing away. Everything here stays on the main actor, so there is no
    /// region to cross and no unsafe opt-out to justify.
    ///
    /// - Returns: Outcome per target, in the order given, verified against the
    ///   kernel rather than inferred from whether a request was accepted.
    @discardableResult
    static func reapAll(_ targets: [Target], label: String) async -> [Outcome] {
        guard !targets.isEmpty else { return [] }

        var outcomes = [Outcome](repeating: .alreadyGone, count: targets.count)
        var pending: [Int] = []

        // Phase 1 — ask everyone at once.
        for (index, target) in targets.enumerated() where isAlive(target.pid) {
            target.requestExit()
            pending.append(index)
        }
        guard !pending.isEmpty else { return outcomes }

        // Phase 2 — one shared grace window. Polling, so a process that leaves
        // immediately does not cost the worst case.
        pending = await waitForExit(targets, pending: pending, label: label, outcomes: &outcomes, as: .exitedGracefully)
        guard !pending.isEmpty else { return outcomes }

        // Phase 3 — insist, only on what ignored the request.
        for index in pending {
            BootLogFile.shared.note("[Reaper] \(label) pid \(targets[index].pid) ignored the request after \(graceSeconds)s — forcing")
            targets[index].force()
        }

        // Phase 4 — force is not instantaneous either, and claiming success
        // without looking is the defect this type exists to remove.
        pending = await waitForExit(targets, pending: pending, label: label, outcomes: &outcomes, as: .forced)

        // Deliberately loud. A process that survives force is wedged in the
        // kernel (uninterruptible I/O, or a debugger holding it), and the next
        // instance is about to contend with it for ports, the microphone and the
        // UDS socket. Silence here is what produced the 12:04 duplicate.
        for index in pending {
            outcomes[index] = .survived
            BootLogFile.shared.note("[Reaper] ⚠️ \(label) pid \(targets[index].pid) SURVIVED force — it will contend with this instance")
        }
        return outcomes
    }

    /// Poll until every pending target is gone or the grace window expires.
    /// Returns the indices still alive.
    private static func waitForExit(
        _ targets: [Target],
        pending: [Int],
        label: String,
        outcomes: inout [Outcome],
        as outcome: Outcome
    ) async -> [Int] {
        var remaining = pending
        let deadline = Date().addingTimeInterval(graceSeconds)

        while !remaining.isEmpty && Date() < deadline {
            try? await Task.sleep(nanoseconds: UInt64(pollSeconds * 1_000_000_000))
            remaining = remaining.filter { index in
                if isAlive(targets[index].pid) { return true }
                outcomes[index] = outcome
                // Success is recorded, not assumed.
                //
                // The first version logged only escalation and survival, so a
                // reap that WORKED left no trace -- and "no trace" is exactly
                // what the old forceTerminate() left when it FAILED. A record
                // that only exists on failure cannot distinguish the two, which
                // is the whole defect this type was written to remove.
                BootLogFile.shared.note("[Reaper] \(label) pid \(targets[index].pid) \(outcome)")
                return false
            }
        }
        return remaining
    }
}
