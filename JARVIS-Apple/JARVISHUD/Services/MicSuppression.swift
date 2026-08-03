//  MicSuppression.swift
//  JARVISHUD
//
//  Stop JARVIS hearing itself — without ever being able to go permanently deaf.
//
//  WHAT IS THERE NOW, AND WHY IT IS FRAGILE
//  ------------------------------------------
//  `HUDAppDelegate.speak` currently does:
//
//      isTTSSpeaking = true
//      wakeWord.stop()                    // tears the whole audio graph down
//      ...
//      didFinish -> try? await Task.sleep(for: .seconds(3.0))   // then rebuild
//
//  Three problems, in increasing order of severity:
//
//  1. A three-second deaf window after EVERY utterance, imposed because
//     AVSpeechSynthesizer holds the output device for ~2-3s and rebuilding the
//     input graph too early throws -10877. The delay is a workaround for a
//     teardown that did not need to happen.
//
//  2. Tearing down and rebuilding a working AVAudioEngine on every sentence is
//     the most failure-prone thing in the loop, and the -10877 in the existing
//     comment is that failure already happening.
//
//  3. `didCancel` IS NOT IMPLEMENTED. A cancelled utterance never fires
//     `didFinish`, so `isTTSSpeaking` stays true and the mic is never
//     restarted. PERMANENT DEAFNESS, with no lockfile in sight — the exact
//     orphaned-state failure a `/tmp/jarvis_speaking` file would have caused,
//     already present in memory.
//
//  THE FIX: MUTE THE TAP, DO NOT DESTROY THE GRAPH
//  -------------------------------------------------
//  The engine keeps running; the tap simply drops buffers while JARVIS speaks.
//  Nothing to rebuild, so no -10877, no three-second wait, and the mic is live
//  again on the same runloop turn the utterance ends.
//
//  Crash-safety comes free and structurally: the mute is a field on a live
//  object. If the process dies, the object dies with it. There is no state on
//  disk that can outlive the thing it describes — which is the whole reason
//  not to use a lockfile, stated as a property rather than a preference.

import AVFoundation

extension WakeWordListener {

    /// Drop input while JARVIS is speaking, without stopping the engine.
    ///
    /// Read from the tap closure, which runs on a REAL-TIME AUDIO THREAD.
    /// Deliberately a plain flag and not a lock: blocking a render callback on
    /// a mutex owned by the main thread is a priority inversion that produces
    /// audio glitches and, under load, drop-outs. The worst case here is that
    /// one 4096-frame buffer (~85 ms) slips through on the transition — far
    /// cheaper than stalling the audio thread, and inaudible as an echo.
    func setMuted(_ muted: Bool) {
        micMuted = muted
    }

    var isMicMuted: Bool { micMuted }
}

// MARK: - Wiring notes (apply inside WakeWordListener.swift)
//
//  1. Add the storage. `nonisolated(unsafe)` is the honest annotation: it IS
//     read off-actor from the audio thread, and the race is benign by the
//     argument above.
//
//         nonisolated(unsafe) fileprivate var micMuted = false
//
//  2. Gate the EXISTING tap — one line, no restructuring:
//
//         inputNode.installTap(onBus: 0, bufferSize: 4096, format: format) { buffer, _ in
//             guard !self.micMuted else { return }          // <-- ADD
//             guard buffer.frameLength > 0 else { return }
//             req.append(buffer)
//         }
//
//     Dropping the buffer rather than pausing the node keeps the recognition
//     request alive: `SFSpeechAudioBufferRecognitionRequest` treats a gap as
//     silence, which is exactly what it was.
//
//  3. Unmute in `beginListening()` as well as on didFinish. A restart for any
//     other reason must never inherit a stale mute — the one way this design
//     could reproduce the bug it replaces.

// MARK: - Delegate lifecycle (apply inside HUDAppDelegate)
//
//  REPLACE the teardown-and-sleep with lifecycle-bound muting. The mute now
//  begins when the utterance ACTUALLY starts speaking rather than when we
//  queue it, closing a window where the synthesiser is still warming up and
//  the mic is already deaf.
//
//      // in speak(...): remove `wakeWord.stop()` and keep only
//      isTTSSpeaking = true
//      tts.speak(utterance)
//
//      nonisolated func speechSynthesizer(_ s: AVSpeechSynthesizer,
//                                         didStart utterance: AVSpeechUtterance) {
//          Task { @MainActor [weak self] in self?.wakeWord.setMuted(true) }
//      }
//
//      nonisolated func speechSynthesizer(_ s: AVSpeechSynthesizer,
//                                         didFinish utterance: AVSpeechUtterance) {
//          Task { @MainActor [weak self] in
//              guard let self else { return }
//              self.isTTSSpeaking = false
//              self.wakeWord.setMuted(false)      // no 3s sleep: nothing was torn down
//          }
//      }
//
//      // THE MISSING ONE. Without this a cancelled utterance leaves the mic
//      // muted forever, which is the deafness this file exists to prevent.
//      nonisolated func speechSynthesizer(_ s: AVSpeechSynthesizer,
//                                         didCancel utterance: AVSpeechUtterance) {
//          Task { @MainActor [weak self] in
//              guard let self else { return }
//              self.isTTSSpeaking = false
//              self.wakeWord.setMuted(false)
//          }
//      }
//
//  A belt-and-braces reconciliation, because a delegate callback that never
//  arrives is the one failure this cannot see: on any mic restart, and on
//  connection-status changes, assert the truth from the synthesiser itself —
//
//      wakeWord.setMuted(tts.isSpeaking)
//
//  `AVSpeechSynthesizer.isSpeaking` is the authority. Deriving the mute from
//  it rather than from our own bookkeeping means a dropped callback costs one
//  cycle of staleness instead of permanent silence.
