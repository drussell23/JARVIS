//  MicSuppression.swift
//  JARVISHUD
//
//  Stop JARVIS hearing itself — without ever being able to go permanently deaf.
//
//  FOUR THINGS SPEAK, AND THEY EACH INVENTED THEIR OWN SIGNAL
//  -----------------------------------------------------------
//  1. `HUDAppDelegate.tts`            — tore the audio graph down, slept 3s
//  2. `AppState.VoiceManager`         — suppressed NOTHING at all
//  3. Python `_hud_tts`               — wrote `/tmp/jarvis_speaking`
//  4. Backend `realtime_voice_*`      — told UnifiedSpeechStateManager, which
//                                       had no way to reach this process
//
//  Three mechanisms, one of which was absent, for one question. This file is
//  the single arbiter they all report to.
//
//  WHY REFCOUNTED CLAIMS AND NOT A BOOLEAN
//  -----------------------------------------
//  A boolean loses. The delegate finishes utterance A and sets `speaking =
//  false` while utterance B — queued on the other synthesiser — is still
//  playing, and the mic opens into JARVIS's own voice. Each source instead
//  holds a NAMED CLAIM; the mic is muted while any claim is live and opens when
//  the last one ends. Exactly the reason a file descriptor is not closed by
//  whichever holder happens to exit first.
//
//  WHY EVERY CLAIM HAS A DEADLINE
//  --------------------------------
//  This is the property that matters most, and the one every previous attempt
//  lacked.
//
//  `didCancel` WAS NEVER IMPLEMENTED. A cancelled utterance never fires
//  `didFinish`, so `isTTSSpeaking` stayed true and the mic never came back:
//  permanent deafness, silent, with no file to blame. The lockfile had the same
//  shape from the other direction — SIGKILL Python and `finally:` never runs,
//  so `/tmp/jarvis_speaking` outlives the process that meant it.
//
//  Implementing `didCancel` fixes the case we thought of. A deadline fixes the
//  cases we did not: a delegate callback that never arrives, a socket that dies
//  mid-utterance, a synthesiser wedged in the audio server. A claim states when
//  it stops being true, and the gate believes the clock over every one of its
//  informants. Deafness stops being a bug to avoid and becomes a state the
//  design cannot represent.
//
//  MUTE THE TAP, DO NOT DESTROY THE GRAPH
//  ----------------------------------------
//  The old path called `wakeWord.stop()` — tearing down a working
//  AVAudioEngine on every sentence — and then slept three seconds before
//  rebuilding, because AVSpeechSynthesizer holds the output device for ~2-3s
//  and rebuilding early throws -10877. The delay was a workaround for a
//  teardown that did not need to happen.
//
//  Here the engine keeps running and the tap simply drops buffers. Nothing is
//  rebuilt, so there is no -10877 and no three-second deaf window, and the mic
//  is live again on the same runloop turn the utterance ends.
//
//  Crash-safety comes free and structurally: claims live on an object inside
//  this process. If the process dies they die with it. Nothing on disk can
//  outlive the thing it describes — the property the lockfile could never have,
//  stated as a consequence rather than a promise.

import Foundation
import AVFoundation

/// Who is holding the microphone shut. Named rather than counted so a log can
/// say WHICH source is keeping the mic closed — "muted" with no owner is the
/// state nobody can debug.
enum SpeechClaimant: String, CaseIterable {
    case hudSynthesizer      = "hud_tts"        // HUDAppDelegate.tts
    case voiceManager        = "voice_manager"  // AppState.VoiceManager
    case backend             = "backend"        // Python, over the IPC socket
    case manual              = "manual"         // operator / test override
}

/// One source's claim on the microphone, and when it stops being true.
private struct SpeechClaim {
    let claimant: SpeechClaimant
    /// Monotonic. `Date()` would let an NTP step or a daylight-saving change
    /// extend a mute by an hour, and the operator would simply be unheard.
    let deadline: ContinuousClock.Instant
    /// When this claim was taken. Distinct from `deadline`, which moves every
    /// time the claim is extended — age must be measured from the ORIGINAL
    /// grab, or an extending claim looks perpetually new and never qualifies
    /// as stale.
    let opened: ContinuousClock.Instant
    let reason: String

    func isLive(_ now: ContinuousClock.Instant) -> Bool { now < deadline }
}

/// The single arbiter of "is JARVIS speaking, and therefore is our mic shut".
///
/// MainActor-isolated: every mutation arrives from a delegate hop or the IPC
/// reader, and serialising them here means the refcount cannot be corrupted by
/// two synthesisers finishing at once. The AUDIO THREAD never touches this —
/// it reads a single `Bool` that this class publishes (see
/// `WakeWordListener.micMuted`), because blocking a render callback on a lock
/// owned by the main thread is a priority inversion that produces drop-outs.
@MainActor
final class SpeechGate {

    static let shared = SpeechGate()

    /// Set when the mute state changes. `WakeWordListener` installs this.
    var onMuteChange: ((Bool) -> Void)?

    private var claims: [SpeechClaimant: SpeechClaim] = [:]
    /// Per-claimant "is it actually speaking right now?" probes. The design
    /// doc always specified this belt-and-braces; the method existed with ZERO
    /// callers — so a dropped `didFinish` cost the whole estimated deadline
    /// (observed: ~4.2s of dead mic) instead of one sweep.
    private var reconcilers: [SpeechClaimant: () -> Bool] = [:]

    /// Let the gate ask the synthesiser itself, every sweep.
    func registerReconciler(_ who: SpeechClaimant, isSpeaking: @escaping () -> Bool) {
        reconcilers[who] = isSpeaking
    }
    private var sweepTask: Task<Void, Never>?
    private var lastPublished = false

    /// How long a claim may last when its source did not say. A source that
    /// cannot estimate its own duration gets a bounded default rather than an
    /// open one — there is no such thing as "until further notice" here.
    private let defaultClaimSeconds: Double = 15.0

    /// Absolute ceiling on any claim, however it was requested. Mirrors
    /// `SpeechStateConfig.MAX_SPEAKING_DURATION_MS` on the Python side: the
    /// backend already decided no single utterance runs past 60s, and a mute
    /// derived from one must not outlive it.
    private let maxClaimSeconds: Double = 60.0

    /// How often expired claims are swept. The sweep is what turns a deadline
    /// from a fact into an effect; without it a claim would expire and the mic
    /// would stay shut until the next unrelated event happened to re-evaluate.
    private let sweepInterval: Duration = .milliseconds(250)

    private init() {}

    // MARK: - Claims

    /// Hold the mic shut for `seconds`, or until `release` — whichever is first.
    ///
    /// Re-claiming for the same claimant EXTENDS rather than stacks. Two
    /// `didStart` callbacks from one synthesiser are one speaker, and counting
    /// them would need two releases to open a mic that one cancel should open.
    func claim(_ who: SpeechClaimant,
               seconds: Double? = nil,
               reason: String = "") {
        let bounded = min(max(seconds ?? defaultClaimSeconds, 0.1), maxClaimSeconds)
        // Preserve the ORIGINAL grab time across extensions: `claim()` is
        // called again on didStart and on every reconcile, and resetting
        // `opened` each time would mean a stuck claim never ages.
        let firstSeen = claims[who]?.opened ?? ContinuousClock.now
        claims[who] = SpeechClaim(
            claimant: who,
            deadline: ContinuousClock.now.advanced(by: .seconds(bounded)),
            opened: firstSeen,
            reason: reason)
        print("[SpeechGate] claim \(who.rawValue) for \(String(format: "%.1f", bounded))s \(reason)")
        startSweeping()
        publish()
    }

    /// Hold the mic shut until an ABSOLUTE wall-clock instant.
    ///
    /// What the backend sends, because only it knows how long its own utterance
    /// plus echo cooldown will run. Converted to monotonic ON ARRIVAL: the two
    /// processes do not share a clock, so a wall-clock deadline is trusted for
    /// its DURATION from now, never for its absolute value. A backend whose
    /// clock is five minutes fast must not mute this mic for five minutes.
    func claim(_ who: SpeechClaimant,
               untilEpochMs deadlineMs: Double,
               nowEpochMs: Double,
               reason: String = "") {
        let remaining = (deadlineMs - nowEpochMs) / 1000.0
        guard remaining > 0 else {
            release(who, reason: "deadline already passed on arrival")
            return
        }
        claim(who, seconds: remaining, reason: reason)
    }

    /// Drop one source's claim. Idempotent — releasing what was never claimed
    /// is a no-op, so a `didCancel` that arrives after `didFinish` is harmless.
    func release(_ who: SpeechClaimant, reason: String = "") {
        guard claims.removeValue(forKey: who) != nil else { return }
        print("[SpeechGate] release \(who.rawValue) \(reason)")
        publish()
    }

    /// Drop every claim. For teardown and for the operator's escape hatch.
    func releaseAll(reason: String = "") {
        guard !claims.isEmpty else { return }
        claims.removeAll()
        print("[SpeechGate] release ALL \(reason)")
        publish()
    }

    // MARK: - Reconciliation

    /// Assert a claim from the synthesiser itself rather than from our
    /// bookkeeping.
    ///
    /// `AVSpeechSynthesizer.isSpeaking` is the authority for a local
    /// synthesiser, and a dropped delegate callback is the one failure this
    /// class cannot otherwise see. Deriving the claim from the synthesiser
    /// costs one sweep of staleness instead of a permanently shut mic. Called
    /// on every sweep and on every mic restart.
    func reconcile(_ who: SpeechClaimant, isSpeaking: Bool) {
        if isSpeaking {
            // Extend ONLY when the claim is about to lapse — re-claiming every
            // 250ms sweep would spam the log and reset deadlines pointlessly.
            let needsExtend: Bool = {
                guard let c = claims[who] else { return true }
                return ContinuousClock.now.advanced(by: .seconds(1.0)) >= c.deadline
            }()
            if needsExtend {
                claim(who, seconds: 2.0, reason: "(reconciled from synthesizer)")
            }
        } else if claims[who] != nil {
            // The synthesiser says idle but a claim is live: the callback was
            // dropped. Release NOW — one sweep of staleness, not the deadline.
            release(who, reason: "(reconciled: synthesizer idle)")
        }
    }

    // MARK: - State

    var isMuted: Bool {
        let now = ContinuousClock.now
        return claims.values.contains { $0.isLive(now) }
    }

    /// Who is currently holding it shut. For the status menu and for logs.
    var holders: [String] {
        let now = ContinuousClock.now
        return claims.values.filter { $0.isLive(now) }
            .map(\.claimant.rawValue).sorted()
    }

    // MARK: - Sweep

    private func startSweeping() {
        guard sweepTask == nil else { return }
        sweepTask = Task { [weak self] in
            while !Task.isCancelled {
                let interval = await MainActor.run { self?.sweepInterval ?? .milliseconds(250) }
                try? await Task.sleep(for: interval)
                guard let self else { return }
                let stop = await MainActor.run { () -> Bool in
                    self.expire()
                    self.reconcileStaleClaimsOnly()
                    self.publish()
                    if self.claims.isEmpty {
                        self.sweepTask = nil
                        return true
                    }
                    return false
                }
                if stop { return }
            }
        }
    }

    /// Consult the synthesisers ONLY where a dropped callback is plausible.
    ///
    /// HANG RISK, self-inflicted. The first version probed every registered
    /// reconciler on every 250ms sweep. `AVSpeechSynthesizer.isSpeaking` is not
    /// a stored property — reading it performs a SYNCHRONOUS THREAD HOP
    /// (`-[_NSThreadPerformInfo wait]` in the trace), so a user-interactive
    /// sweep blocked on a default-QoS thread four times a second, forever, and
    /// Xcode correctly flagged the priority inversion.
    ///
    /// The probe is an ANOMALY DETECTOR: it exists to catch a `didFinish` that
    /// never arrived. So it should run only when an anomaly is actually
    /// possible, and the conditions are precise:
    ///
    ///   * NO live claim -> nothing could have been dropped. Skip entirely.
    ///     This is the common case, and it now costs zero blocking calls.
    ///   * A claim YOUNGER than the grace period -> a normal utterance still
    ///     in flight. Its callback has not had time to be late yet.
    ///
    /// What remains is exactly the suspicious case: a claim old enough that a
    /// well-behaved synthesiser should already have released it. The cost of
    /// the blocking read is then paid once per sweep per genuinely-stuck claim
    /// rather than continuously, and the recovery window is unchanged.
    private func reconcileStaleClaimsOnly() {
        guard !reconcilers.isEmpty, !claims.isEmpty else { return }
        let now = ContinuousClock.now
        for (who, probe) in reconcilers {
            guard let claim = claims[who], claim.isLive(now) else { continue }
            // Old enough that `didFinish` is overdue, not merely pending.
            guard now >= claim.opened.advanced(by: .seconds(1.5)) else { continue }
            reconcile(who, isSpeaking: probe())
        }
    }

    /// Drop claims whose deadline has passed. The whole safety net: this runs
    /// on a clock and consults nothing else, so a wedged synthesiser or a dead
    /// socket cannot keep the microphone shut by failing to answer.
    private func expire() {
        let now = ContinuousClock.now
        for (who, claim) in claims where !claim.isLive(now) {
            claims.removeValue(forKey: who)
            print("[SpeechGate] EXPIRED \(who.rawValue) — its owner never released it")
        }
    }

    private func publish() {
        let muted = isMuted
        guard muted != lastPublished else { return }
        lastPublished = muted
        print("[SpeechGate] mic \(muted ? "MUTED" : "LIVE")\(muted ? " by \(holders.joined(separator: "+"))" : "")")
        onMuteChange?(muted)
    }
}

// MARK: - WakeWordListener integration

extension WakeWordListener {

    /// Bind this listener to the gate. Called once at start-up.
    ///
    /// The gate pushes; the listener does not poll. A poll would put a
    /// filesystem or lock access on the audio path, which is what the old
    /// `FileManager.fileExists(atPath: "/tmp/jarvis_speaking")` did on EVERY
    /// recognition callback — a synchronous `stat` several times a second, and
    /// at the wrong layer besides: it dropped RESULTS while still feeding
    /// JARVIS's own voice into the recogniser, so the transcript accumulated
    /// what JARVIS said and acted on it the moment the flag cleared.
    @MainActor
    func bindToSpeechGate() {
        SpeechGate.shared.onMuteChange = { [weak self] muted in
            self?.setMuted(muted)
        }
        // Assert the CURRENT truth immediately, not just future changes. A
        // listener that only subscribed would start unmuted and stay that way
        // until the next transition — opening the mic in the middle of an
        // utterance already in progress.
        setMuted(SpeechGate.shared.isMuted)
    }
}
