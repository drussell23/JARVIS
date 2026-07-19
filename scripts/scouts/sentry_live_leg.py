"""Interactive accuracy + 30-min soak legs (2026-07-19).
Uses the PRODUCTION EarsAjarGate + on-device SFSpeechRecognizer.
argv[1]: 'interactive' (600s, log wake-word hits) | 'soak' (1800s)."""
from __future__ import annotations
import json, sys, time
import numpy as np, psutil, sounddevice as sd
from backend.core.ouroboros.governance.comms.duplex.ears_ajar import EarsAjarGate

MODE = sys.argv[1] if len(sys.argv) > 1 else "interactive"
SECONDS = 1800 if MODE == "soak" else 600
RATE, CHUNK = 16000, 480
PROC = psutil.Process()

import Speech
from AVFoundation import AVAudioEngine

def recognize_window(seconds=4.0):
    """One windowed on-device recognition burst; returns transcripts."""
    hits = []
    try:
        rec = Speech.SFSpeechRecognizer.alloc().init()
        if rec is None or not rec.isAvailable():
            return hits
        req = Speech.SFSpeechAudioBufferRecognitionRequest.alloc().init()
        try: req.setRequiresOnDeviceRecognition_(True)
        except Exception: pass
        eng = AVAudioEngine.alloc().init()
        node = eng.inputNode()
        fmt = node.outputFormatForBus_(0)
        def _cb(result, error):
            if result is not None:
                hits.append(str(result.bestTranscription().formattedString()))
        task = rec.recognitionTaskWithRequest_resultHandler_(req, _cb)
        node.installTapOnBus_bufferSize_format_block_(
            0, 1024, fmt, lambda b, w: req.appendAudioPCMBuffer_(b))
        eng.prepare(); eng.startAndReturnError_(None)
        time.sleep(seconds)
        eng.stop(); node.removeTapOnBus_(0)
        req.endAudio(); task.cancel()
    except Exception as e:
        hits.append(f"<err {type(e).__name__}>")
    return hits

def main():
    gate = EarsAjarGate(rate=RATE, chunk=CHUNK)
    events = []
    floor_log = []
    PROC.cpu_percent(None)
    t0 = time.monotonic()
    with sd.InputStream(samplerate=RATE, channels=1, blocksize=CHUNK, dtype="float32") as stream:
        while time.monotonic() - t0 < SECONDS:
            data, _ = stream.read(CHUNK)
            payload = gate.feed(data[:, 0])
            now = round(time.monotonic() - t0, 1)
            if int(now) % 60 == 0 and (not floor_log or floor_log[-1]["t"] != int(now)):
                floor_log.append({"t": int(now), "floor": round(gate.noise_floor, 6),
                                  "cpu": PROC.cpu_percent(None)})
            if payload is not None and MODE == "interactive":
                # gate fired → windowed recognizer (stream released first)
                stream.stop()
                text = recognize_window(4.0)
                stream.start()
                gate.close_window()
                low = " ".join(text).lower()
                events.append({"t": now, "heard": text[-1] if text else "",
                               "jarvis": "jarvis" in low, "karen": "karen" in low})
            elif payload is not None:
                gate.close_window()
    print(json.dumps({
        "mode": MODE, "seconds": SECONDS,
        "gate_stats": {k: (round(v, 6) if isinstance(v, float) else v)
                        for k, v in gate.stats.items()},
        "floor_drift": floor_log[:35],
        "events": events[-40:],
        "wake_hits": sum(1 for e in events if e.get("jarvis") or e.get("karen")),
    }, indent=1))

main()
