/// ParentWatch — the HUD leaves when whoever launched it leaves.
///
/// WHY THIS EXISTS, GIVEN THE ORPHAN BUG WAS ALREADY FIXED
/// -------------------------------------------------------
/// `BrainstemLauncher` declares `JARVIS_PARENT_PID` so the Python brainstem
/// watches the HUD and exits with it (`brainstem/parent_watch.py`). That fix
/// rests on an assumption stated in its own comment: *"Xcode's Stop button
/// SIGKILLs this process."*
///
/// Sometimes it does not. Xcode also detaches:
///
///     Xcode has killed the LLDB RPC server (pid: N) to allow the debugger
///     to detach from your process.
///
/// Measured on 2026-08-06, twenty-one seconds apart:
///
///     80876  ppid 1      (launchd)      ← orphan, previous run, still alive
///     81007  ppid 81299  (debugserver)  ← the run Xcode was attached to
///
/// When the HUD is *detached* rather than killed it is reparented to launchd and
/// keeps running: holding its ports, its microphone claim, its UDS socket, and
/// competing with the next instance for all three.
///
/// And it takes the existing protection down with it. The brainstem watches the
/// HUD; the HUD is still alive; so the brainstem correctly stays. Every link in
/// the chain watches the link above it, and the topmost link watched nothing —
/// so orphaning the top orphans everything below it, protection intact and
/// useless. This closes that by giving the chain a root.
///
/// WHY NOT SIGNAL HANDLERS, atexit, OR `deinit`
/// --------------------------------------------
/// None of them run on SIGKILL, and SIGKILL is one of the two ways this session
/// ends. A process cannot clean up after its own uncatchable death; something
/// must observe it from outside. Here the roles invert — we are the observer,
/// and the thing we observe is our own launcher.
///
/// WHY IT IS INERT FOR A NORMALLY-LAUNCHED APP
/// ------------------------------------------
/// A double-clicked app is started by launchd and has ppid 1 for its whole life.
/// Exiting when "the parent dies" would be nonsense there — launchd outlives
/// everything. So the watch arms only when the parent is something else, which
/// is precisely the debugger/terminal case where our lifetime really is bounded
/// by theirs. Same discipline as `JARVIS_PARENT_PID`: refuse to supervise on an
/// undeclared parent, so a developer is never killed by their own shell.

import Foundation

enum ParentWatch {

    /// pid 1. Any process reparented here has already lost its original parent.
    private static let launchdPID: pid_t = 1

    private static var source: DispatchSourceProcess?

    /// Set from the Xcode scheme to disable the watch without editing code.
    /// Present so the behaviour can be turned off during a debugging session
    /// that deliberately outlives its launcher — never as a default.
    private static let disableKey = "JARVIS_HUD_PARENT_WATCH_DISABLED"

    /// Begin watching the launching process. Idempotent; safe to call once at
    /// startup and harmless if called again.
    ///
    /// - Parameter onOrphaned: Invoked on the main queue when the parent exits.
    ///   Given so the caller owns *how* to leave — this type decides only *when*.
    ///   A watch that called `exit()` itself would skip shutdown work the app
    ///   legitimately needs to do, which is how "clean up orphans" becomes
    ///   "corrupt state on every stop".
    static func arm(onOrphaned: @escaping () -> Void) {
        guard source == nil else { return }

        if ProcessInfo.processInfo.environment[disableKey] == "1" {
            print("[ParentWatch] \(disableKey)=1 — not watching; this process can outlive its launcher")
            return
        }

        let parent = getppid()

        guard parent != launchdPID else {
            // Normal launch. There is no parent whose death should end us.
            print("[ParentWatch] parent is launchd — not arming (normal app launch)")
            return
        }

        // Read the parent's identity for the log BEFORE arming. Once it exits
        // the name is unrecoverable, and "parent 81299 exited" is a much worse
        // diagnostic than naming debugserver.
        let parentName = processName(of: parent) ?? "pid \(parent)"

        let src = DispatchSource.makeProcessSource(
            identifier: parent,
            eventMask: .exit,
            queue: .main
        )
        src.setEventHandler {
            print("[ParentWatch] launcher (\(parentName)) exited — leaving rather than orphaning")
            onOrphaned()
        }
        src.resume()
        source = src

        print("[ParentWatch] watching launcher \(parentName); this process will not outlive it")

        // A DispatchSourceProcess established on an ALREADY-dead pid never
        // fires. The window is small but real: the parent can die between
        // getppid() and resume(). Re-check afterwards, because the failure mode
        // of missing it is exactly the orphan this exists to prevent.
        if getppid() != parent {
            print("[ParentWatch] launcher exited while arming — leaving now")
            onOrphaned()
        }
    }

    /// Best-effort name for a pid, for logging only.
    private static func processName(of pid: pid_t) -> String? {
        var buffer = [CChar](repeating: 0, count: 4096)
        // proc_pidpath is in libproc; declared here rather than importing the
        // whole header to keep this file self-contained.
        let size = proc_pidpath_shim(pid, &buffer, UInt32(buffer.count))
        guard size > 0 else { return nil }
        let path = String(cString: buffer)
        return (path as NSString).lastPathComponent
    }
}

// `proc_pidpath` lives in libproc.dylib and is not surfaced by Foundation.
@_silgen_name("proc_pidpath")
private func proc_pidpath_shim(_ pid: pid_t, _ buffer: UnsafeMutablePointer<CChar>, _ size: UInt32) -> Int32
