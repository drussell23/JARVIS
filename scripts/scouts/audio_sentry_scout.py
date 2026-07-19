"""Phase 3 Audio Sentry Scout — EMPIRICAL M1 benchmarks (2026-07-19).

Three candidate engines, real hardware, real mic, real numbers. Legs
that cannot run (permission / key) report BLOCKED — never estimated.
"""
from __future__ import annotations
import json, os, sys, time
import numpy as np
import psutil
import sounddevice as sd

RATE = 16000
CHUNK = 480               # 30ms
PROC = psutil.Process()

def cpu_mem_window(seconds, tick_cb):
    PROC.cpu_percent(None)
    m0 = PROC.memory_info().rss
    t0 = time.monotonic()
    peaks = m0
    while time.monotonic() - t0 < seconds:
        tick_cb()
        peaks = max(peaks, PROC.memory_info().rss)
    return PROC.cpu_percent(None), (peaks - m0) / 1e6

def leg_energy_gate(seconds=45):
    """Ears-Ajar: rolling RMS + voice-band (85-3000Hz) envelope on 30ms
    chunks from the REAL microphone. Gate opens only when both cross
    thresholds — the heavy engines stay cold."""
    out = {"leg": "energy_gate"}
    opens = 0
    chunks = 0
    proc_ns = []
    freqs = np.fft.rfftfreq(CHUNK, 1 / RATE)
    band = (freqs >= 85) & (freqs <= 3000)
    noise_floor = [1e-4]
    try:
        with sd.InputStream(samplerate=RATE, channels=1, blocksize=CHUNK, dtype="float32") as stream:
            PROC.cpu_percent(None)
            m0 = PROC.memory_info().rss
            t0 = time.monotonic()
            while time.monotonic() - t0 < seconds:
                data, _ = stream.read(CHUNK)
                a = time.perf_counter_ns()
                x = data[:, 0]
                rms = float(np.sqrt(np.mean(x * x)))
                spec = np.abs(np.fft.rfft(x))
                voice_ratio = float(spec[band].sum() / (spec.sum() + 1e-9))
                noise_floor[0] = 0.995 * noise_floor[0] + 0.005 * rms
                gate = rms > max(3 * noise_floor[0], 0.01) and voice_ratio > 0.55
                proc_ns.append(time.perf_counter_ns() - a)
                chunks += 1
                if gate:
                    opens += 1
            out["cpu_pct"] = PROC.cpu_percent(None)
            out["mem_delta_mb"] = (PROC.memory_info().rss - m0) / 1e6
        out["chunks"] = chunks
        out["gate_opens"] = opens
        out["fp_rate_pct"] = round(100.0 * opens / max(1, chunks), 3)
        out["dsp_us_per_chunk"] = round(float(np.mean(proc_ns)) / 1000.0, 1)
        out["dsp_theoretical_cpu_pct"] = round(
            100.0 * (np.mean(proc_ns) / 1e9) / (CHUNK / RATE), 3)
        out["status"] = "OK"
    except Exception as e:
        out["status"] = f"BLOCKED({type(e).__name__}: {e})"
    return out

def leg_sfspeech(seconds=75):
    """Continuous on-device SFSpeechRecognizer session: measure CPU +
    lifespan until macOS tears the session down (rate-limit boundary)."""
    out = {"leg": "sfspeech_continuous"}
    try:
        import Speech
        from AVFoundation import AVAudioEngine
        auth = {"v": None}
        Speech.SFSpeechRecognizer.requestAuthorization_(lambda s: auth.__setitem__("v", s))
        deadline = time.monotonic() + 6
        while auth["v"] is None and time.monotonic() < deadline:
            time.sleep(0.1)
        # 3 == authorized
        if auth["v"] != 3:
            out["status"] = f"BLOCKED(speech-auth={auth['v']} — TCC prompt needs interactive session)"
            return out
        rec = Speech.SFSpeechRecognizer.alloc().init()
        if rec is None or not rec.isAvailable():
            out["status"] = "BLOCKED(recognizer unavailable)"
            return out
        req = Speech.SFSpeechAudioBufferRecognitionRequest.alloc().init()
        try:
            req.setRequiresOnDeviceRecognition_(True)
        except Exception:
            pass
        engine = AVAudioEngine.alloc().init()
        node = engine.inputNode()
        fmt = node.outputFormatForBus_(0)
        state = {"ended": None, "results": 0, "t0": time.monotonic()}
        def _cb(result, error):
            if result is not None:
                state["results"] += 1
            if error is not None and state["ended"] is None:
                state["ended"] = time.monotonic() - state["t0"]
        task = rec.recognitionTaskWithRequest_resultHandler_(req, _cb)
        node.installTapOnBus_bufferSize_format_block_(
            0, 1024, fmt, lambda buf, when: req.appendAudioPCMBuffer_(buf))
        engine.prepare()
        ok, err = engine.startAndReturnError_(None)
        if not ok:
            out["status"] = f"BLOCKED(engine start: {err})"
            return out
        PROC.cpu_percent(None)
        m0 = PROC.memory_info().rss
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds and state["ended"] is None:
            time.sleep(0.25)
        out["cpu_pct"] = PROC.cpu_percent(None)
        out["mem_delta_mb"] = (PROC.memory_info().rss - m0) / 1e6
        out["session_lifespan_s"] = round(state["ended"] if state["ended"] else time.monotonic() - t0, 1)
        out["session_died"] = state["ended"] is not None
        out["partial_results"] = state["results"]
        engine.stop(); task.cancel()
        out["status"] = "OK"
    except Exception as e:
        out["status"] = f"BLOCKED({type(e).__name__}: {e})"
    return out

def leg_porcupine(seconds=45):
    """Porcupine hotword on the real mic (needs PICOVOICE_ACCESS_KEY)."""
    out = {"leg": "porcupine"}
    key = os.environ.get("PICOVOICE_ACCESS_KEY", "").strip()
    if not key:
        out["status"] = "BLOCKED(no PICOVOICE_ACCESS_KEY — commercial key required)"
        return out
    try:
        import pvporcupine
        pp = pvporcupine.create(access_key=key, keywords=["jarvis", "porcupine"])
        triggers = 0
        frames = 0
        with sd.InputStream(samplerate=pp.sample_rate, channels=1,
                            blocksize=pp.frame_length, dtype="int16") as stream:
            PROC.cpu_percent(None)
            m0 = PROC.memory_info().rss
            t0 = time.monotonic()
            while time.monotonic() - t0 < seconds:
                data, _ = stream.read(pp.frame_length)
                if pp.process(data[:, 0]) >= 0:
                    triggers += 1
                frames += 1
            out["cpu_pct"] = PROC.cpu_percent(None)
            out["mem_delta_mb"] = (PROC.memory_info().rss - m0) / 1e6
        pp.delete()
        out["frames"] = frames
        out["false_triggers"] = triggers
        out["fp_rate_pct"] = round(100.0 * triggers / max(1, frames), 3)
        out["status"] = "OK"
    except Exception as e:
        out["status"] = f"BLOCKED({type(e).__name__}: {e})"
    return out

if __name__ == "__main__":
    results = {
        "host": "M1 MacBook Pro (live hardware)",
        "energy_gate": leg_energy_gate(),
        "sfspeech": leg_sfspeech(),
        "porcupine": leg_porcupine(),
    }
    print(json.dumps(results, indent=2))
