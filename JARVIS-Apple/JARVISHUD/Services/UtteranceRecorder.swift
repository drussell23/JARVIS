//  UtteranceRecorder.swift
//  JARVISHUD
//
//  Keeps the audio of the sentence you just said, so something other than the
//  speech recogniser can look at it.
//
//  WHY THIS EXISTS
//  ---------------
//  `unlock_screen` needs an authority that can answer through a locked screen.
//  Touch ID cannot — there is no surface to draw a dialog on, which is why the
//  verdict came back DENIED 99ms after the request and the log said "operator
//  declined" for a prompt nobody saw. A microphone works through a locked
//  screen, so speaker verification is the authority that fits. But the backend
//  has never had a single sample of audio: the tap feeds
//  `SFSpeechAudioBufferRecognitionRequest` and nothing else, so Python only
//  ever receives the TEXT of what was said.
//
//  This is that missing wire. It is also what enrollment needs — you cannot
//  build a voiceprint from a transcript.
//
//  REAL-TIME DISCIPLINE
//  --------------------
//  `append` is called from the audio render callback. That thread must not
//  allocate, must not lock, and must not block, or the price is dropped audio
//  — the `HALC_ProxyIOContext::IOWorkLoop: skipping cycle due to overload`
//  lines already in the log. So:
//
//    * the buffer is allocated ONCE, when the engine format is known;
//    * `append` does a bounds check and a `memcpy`, nothing else;
//    * it stops writing when full rather than shifting, because sliding a
//      2MB window on a render callback is how you turn a voice assistant into
//      a stutter.
//
//  Everything expensive — resampling, WAV framing, base64 — happens in
//  `finish()`, which is called from a normal queue after the sentence ends.
//
//  WHAT IT DELIBERATELY DOES NOT DO
//  ---------------------------------
//  It never writes audio to disk. A file would outlive the question it was
//  captured to answer, and the one thing worse than a Mac that will not unlock
//  is a Mac with a folder full of recordings of its owner. The bytes live in
//  one preallocated buffer, are overwritten by the next sentence, and are
//  handed up exactly once.
//
//  It also records nothing while `micMuted` is set, because the caller appends
//  behind the same gate that keeps JARVIS's own voice out of the recogniser.
//  A verifier trained on the assistant's synthesiser is its own kind of funny.

import Foundation
import AVFoundation

final class UtteranceRecorder: @unchecked Sendable {

    /// Longest sentence we keep. A spoken command that runs past this is
    /// truncated rather than dropped — the opening seconds are the part a
    /// verifier wants, and a partial sample beats no sample.
    private let maxSeconds: Double = 12.0

    /// What the speaker-verification model expects. 16 kHz mono is the
    /// standard input rate for x-vector/ECAPA style embeddings; sending 48 kHz
    /// would make the backend resample, and resampling in the process that did
    /// not capture it is how sample-rate bugs become somebody else's.
    private let targetRate: Double = 16_000

    private var storage: UnsafeMutablePointer<Float>?
    private var capacity: Int = 0
    /// Where the next frame goes. Wraps; never shifts.
    private var head: Int = 0
    /// Frames written since `begin`, monotonic. Distinguishes a buffer that is
    /// partly filled from one that has wrapped, without a second flag.
    private var written: Int = 0
    private var sourceFormat: AVAudioFormat?

    deinit { storage?.deallocate() }

    /// Prepare for a new sentence at `format`. Allocates only when the shape
    /// changes — a headphone swap changes the hardware rate, and reallocating
    /// on every restart would churn a megabyte several times a minute.
    func begin(format: AVAudioFormat) {
        let needed = Int(format.sampleRate * maxSeconds)
        if storage == nil || capacity != needed {
            storage?.deallocate()
            storage = UnsafeMutablePointer<Float>.allocate(capacity: max(1, needed))
            capacity = max(1, needed)
        }
        sourceFormat = format
        head = 0
        written = 0
    }

    /// Append one tap buffer. CALLED ON THE AUDIO RENDER THREAD.
    ///
    /// KEEPS THE MOST RECENT `maxSeconds`, NOT THE FIRST.
    ///
    /// This used to stop writing once full, which sounds conservative and is
    /// the opposite of what a verifier needs. `begin()` runs when the audio
    /// ENGINE starts, so the buffer filled with whatever the room was doing
    /// while nobody was talking, and by the time an actual command arrived
    /// there was no space left for it. Measured live 2026-08-05, saying
    /// "unlock my screen":
    ///
    ///     [JARVIS Voice] utterance captured (345KB base64)
    ///     ⚠️ AUDIO DEBUG: Audio appears to be silent!
    ///     ⚠️ No speaker match found. Best confidence: 0.00%
    ///     'unlock_screen' NOT authorised (That didn't sound like you)
    ///
    /// Three hundred kilobytes of room tone, faithfully captured, and the
    /// sentence the operator actually said discarded for lack of room. The
    /// verifier did its job correctly on the evidence it was given.
    ///
    /// The original objection to a moving window was that "sliding a 2MB
    /// window on a render callback" stutters — and that is true of SHIFTING,
    /// which memmoves the whole buffer on every callback. A ring does not
    /// shift. It writes at an index and wraps, so this is still two memcpys
    /// at worst, no allocation, no lock, and O(frames) rather than O(capacity).
    ///
    /// `written` is monotonic and only ever grows, so `finish()` can tell a
    /// partially-filled buffer from a wrapped one without a second flag.
    func append(_ buffer: AVAudioPCMBuffer) {
        guard let dst = storage,
              let src = buffer.floatChannelData?[0] else { return }
        let frames = Int(buffer.frameLength)
        guard frames > 0, capacity > 0 else { return }

        // A single buffer longer than the whole window: keep only its tail,
        // which is the part nearest the end of the sentence.
        let take = min(frames, capacity)
        let offset = frames - take

        let first = min(take, capacity - head)
        dst.advanced(by: head).update(from: src.advanced(by: offset), count: first)
        if take > first {
            dst.update(from: src.advanced(by: offset + first), count: take - first)
        }
        head = (head + take) % capacity
        written += take
    }

    var hasAudio: Bool { written > 0 }

    /// The captured sentence as base64 WAV at `targetRate`, or nil.
    ///
    /// Call OFF the audio thread. Downsamples with a box filter across the
    /// decimation window rather than picking every Nth sample: plain
    /// decimation folds everything above 8 kHz back into the band a speaker
    /// embedding is computed from, and the artefacts it produces are exactly
    /// the kind a verifier reads as "different person".
    func finish() -> String? {
        guard let src = storage, written > 0, let format = sourceFormat else { return nil }
        let sourceRate = format.sampleRate
        guard sourceRate > 0 else { return nil }

        // Unwrap the ring into chronological order before anything else looks
        // at it. `written < capacity` means it never wrapped, so the frames sit
        // at 0..<written already; otherwise the oldest frame is under `head`.
        //
        // Done here rather than in `append` on purpose: this runs once per
        // sentence on a normal queue, whereas append runs on the render thread
        // hundreds of times a second. The expensive half belongs where it can
        // afford to be.
        let count = min(written, capacity)
        var linear = [Float](repeating: 0, count: count)
        linear.withUnsafeMutableBufferPointer { dst in
            guard let base = dst.baseAddress else { return }
            if written < capacity {
                base.update(from: src, count: count)
            } else {
                let tail = capacity - head          // oldest slice, at the top
                base.update(from: src.advanced(by: head), count: tail)
                base.advanced(by: tail).update(from: src, count: head)
            }
        }
        let encoded = linear.withUnsafeBufferPointer { buf -> String? in
            guard let src = buf.baseAddress else { return nil }
            return Self.encode(src, count: count, sourceRate: sourceRate,
                               targetRate: targetRate)
        }
        // Hand it up exactly once. Reset AFTER the copy, not before — the
        // anti-replay guarantee is that the same sentence cannot be claimed
        // twice, not that it can be lost between the copy and the reset.
        head = 0
        written = 0
        return encoded
    }

    /// Resample to `targetRate` and frame as base64 WAV. Pure; no state.
    ///
    /// Split out of `finish()` so the ring-unwrapping and the signal work are
    /// separable — the unwrap is where a subtle ordering bug would hide, and it
    /// is much easier to reason about when it is not tangled with resampling.
    private static func encode(_ src: UnsafePointer<Float>, count: Int,
                               sourceRate: Double, targetRate: Double) -> String? {

        let ratio = sourceRate / targetRate
        guard ratio >= 1.0 else { return nil }          // never upsample
        let outCount = Int(Double(count) / ratio)
        guard outCount > 0 else { return nil }

        var pcm = [Int16](repeating: 0, count: outCount)
        for i in 0..<outCount {
            let start = Int(Double(i) * ratio)
            let end = min(count, Int(Double(i + 1) * ratio))
            guard start < end else { continue }
            var sum: Float = 0
            for j in start..<end { sum += src[j] }
            let mean = sum / Float(end - start)
            // Clamp BEFORE scaling. A float sample can legitimately exceed
            // ±1.0 after mixing, and letting that wrap produces a click that
            // reads as a consonant.
            let clamped = max(-1.0, min(1.0, mean))
            pcm[i] = Int16(clamped * 32767.0)
        }
        return Self.wav(pcm, sampleRate: Int(targetRate)).base64EncodedString()
    }

    /// Discard whatever was captured. Used when a sentence turns out not to be
    /// a command — there is no reason to keep audio nobody asked a question of.
    func discard() {
        head = 0
        written = 0
    }

    /// Minimal 16-bit mono RIFF/WAVE. Written by hand rather than via
    /// `AVAudioFile` because that writes to a URL, and the whole point is that
    /// this audio never touches a filesystem.
    private static func wav(_ samples: [Int16], sampleRate: Int) -> Data {
        let dataBytes = samples.count * 2
        var d = Data(capacity: 44 + dataBytes)
        func u32(_ v: UInt32) { withUnsafeBytes(of: v.littleEndian) { d.append(contentsOf: $0) } }
        func u16(_ v: UInt16) { withUnsafeBytes(of: v.littleEndian) { d.append(contentsOf: $0) } }

        d.append(contentsOf: Array("RIFF".utf8))
        u32(UInt32(36 + dataBytes))
        d.append(contentsOf: Array("WAVE".utf8))
        d.append(contentsOf: Array("fmt ".utf8))
        u32(16)                                   // PCM header size
        u16(1)                                    // format = PCM
        u16(1)                                    // channels = mono
        u32(UInt32(sampleRate))
        u32(UInt32(sampleRate * 2))               // byte rate
        u16(2)                                    // block align
        u16(16)                                   // bits per sample
        d.append(contentsOf: Array("data".utf8))
        u32(UInt32(dataBytes))
        samples.withUnsafeBufferPointer { buf in
            buf.baseAddress.map { d.append(UnsafeBufferPointer(start: $0, count: buf.count)) }
        }
        return d
    }
}
