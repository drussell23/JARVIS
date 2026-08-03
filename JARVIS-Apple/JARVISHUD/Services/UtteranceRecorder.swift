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
    private var count: Int = 0
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
        count = 0
    }

    /// Append one tap buffer. CALLED ON THE AUDIO RENDER THREAD.
    ///
    /// No allocation, no locking, no logging. `count` is written here and read
    /// in `finish()` after the engine has been torn down, so the two never
    /// overlap; a torn read would cost a few frames at the tail of a sample,
    /// which is not worth a lock on this thread.
    func append(_ buffer: AVAudioPCMBuffer) {
        guard let dst = storage,
              let src = buffer.floatChannelData?[0] else { return }
        let frames = Int(buffer.frameLength)
        guard frames > 0, count < capacity else { return }
        let room = min(frames, capacity - count)
        dst.advanced(by: count).update(from: src, count: room)
        count += room
    }

    var hasAudio: Bool { count > 0 }

    /// The captured sentence as base64 WAV at `targetRate`, or nil.
    ///
    /// Call OFF the audio thread. Downsamples with a box filter across the
    /// decimation window rather than picking every Nth sample: plain
    /// decimation folds everything above 8 kHz back into the band a speaker
    /// embedding is computed from, and the artefacts it produces are exactly
    /// the kind a verifier reads as "different person".
    func finish() -> String? {
        guard let src = storage, count > 0, let format = sourceFormat else { return nil }
        let sourceRate = format.sampleRate
        guard sourceRate > 0 else { return nil }

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
        count = 0                                        // hand it up exactly once
        return Self.wav(pcm, sampleRate: Int(targetRate)).base64EncodedString()
    }

    /// Discard whatever was captured. Used when a sentence turns out not to be
    /// a command — there is no reason to keep audio nobody asked a question of.
    func discard() { count = 0 }

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
