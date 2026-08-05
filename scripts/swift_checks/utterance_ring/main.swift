// Exercises the REAL UtteranceRecorder.swift, compiled in alongside this.
//
// The property that matters: after more audio than the window has been
// appended, `finish()` must yield the MOST RECENT `maxSeconds` in
// chronological order. A wrapping bug reorders the sentence, which produces
// audio that is present but garbled — and fails verification with exactly the
// symptom being fixed, so it would look like no progress at all.
import AVFoundation
import Foundation

func buffer(_ values: [Float], rate: Double) -> AVAudioPCMBuffer {
    let fmt = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: rate,
                            channels: 1, interleaved: false)!
    let b = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: AVAudioFrameCount(values.count))!
    b.frameLength = AVAudioFrameCount(values.count)
    for (i, v) in values.enumerated() { b.floatChannelData![0][i] = v }
    return b
}

func decodeWavMonoInt16(_ b64: String) -> [Int16] {
    guard let d = Data(base64Encoded: b64), d.count > 44 else { return [] }
    let body = d.subdata(in: 44..<d.count)
    return body.withUnsafeBytes { raw in
        Array(UnsafeBufferPointer(start: raw.baseAddress!.assumingMemoryBound(to: Int16.self),
                                  count: body.count / 2))
    }
}

var failures = 0
func check(_ cond: Bool, _ what: String) {
    print("\(cond ? "PASS" : "FAIL")  \(what)")
    if !cond { failures += 1 }
}

// 16 kHz so no resampling happens and samples survive one-for-one.
let rate = 16000.0
let maxSeconds = 12.0
let cap = Int(rate * maxSeconds)

// ---- 1. Under-filled: order preserved, nothing invented -------------------
do {
    let r = UtteranceRecorder()
    let fmt = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: rate,
                            channels: 1, interleaved: false)!
    r.begin(format: fmt)
    r.append(buffer([0.1, 0.2, 0.3, 0.4], rate: rate))
    let out = decodeWavMonoInt16(r.finish() ?? "")
    check(out.count == 4, "under-filled keeps every frame (got \(out.count))")
    check(out.count == 4 && out[0] < out[1] && out[1] < out[2] && out[2] < out[3],
          "under-filled preserves ascending order")
}

// ---- 2. Wrapped: keeps the LAST window, in order --------------------------
do {
    let r = UtteranceRecorder()
    let fmt = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: rate,
                            channels: 1, interleaved: false)!
    r.begin(format: fmt)
    // 1.5 windows of a strictly increasing ramp, in realistic tap-sized chunks.
    let total = cap + cap / 2
    var next = 0
    let chunk = 4096
    while next < total {
        let n = min(chunk, total - next)
        let vals = (0..<n).map { Float(next + $0) / Float(total) }
        r.append(buffer(vals, rate: rate))
        next += n
    }
    let out = decodeWavMonoInt16(r.finish() ?? "")
    check(out.count == cap, "wrapped yields exactly one window (\(out.count) vs \(cap))")

    var monotonic = true
    for i in 1..<out.count where out[i] < out[i - 1] { monotonic = false; break }
    check(monotonic, "wrapped output is in CHRONOLOGICAL order (no torn seam)")

    // The window must be the RECENT half, not the opening half — this is the
    // whole defect: room tone kept, the command discarded.
    let firstKept = Float(total - cap) / Float(total)
    let expected = Int16(max(-1.0, min(1.0, firstKept)) * 32767.0)
    check(abs(Int(out[0]) - Int(expected)) < 400,
          "window starts at the RECENT end (got \(out[0]), expected ~\(expected))")
    check(out.last! > 32000, "window ends at the newest sample (got \(out.last!))")
}

// ---- 3. Claimed exactly once ---------------------------------------------
do {
    let r = UtteranceRecorder()
    let fmt = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: rate,
                            channels: 1, interleaved: false)!
    r.begin(format: fmt)
    r.append(buffer([0.5, 0.6], rate: rate))
    check(r.finish() != nil, "first finish returns audio")
    check(r.finish() == nil, "second finish returns nil — handed up exactly once")
}

print(failures == 0 ? "\nALL RING CHECKS PASSED" : "\n\(failures) CHECK(S) FAILED")
exit(failures == 0 ? 0 : 1)
