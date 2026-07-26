# Handoff — voice pipeline, 2026-07-25 → 26

## The headline

**The software is proven good. The captured audio is not intelligible speech.**

That is no longer an inference. Both halves were measured independently:

| | evidence |
|---|---|
| pipeline | Golden Master (real `say` speech) through the **live** `StreamingSTTEngine` → `"Hello, Karen. This is the Golden Master Test of the transcription pipeline."` 100 % match, twice, two phrases |
| bus → model handoff | normalised cross-correlation **1.000**, gain **0.00 dB**, proven live in production on every aligning incident |
| captured audio | the same pipeline returns **nothing**, or `"I'm sorry."` — Whisper's non-speech prior |

The original distance finding stands, now confirmed by four independent instruments:

| | seating distance | close to lid | known-good reference |
|---|---|---|---|
| rms | 0.0033 | 0.0114 | 0.109 |
| crest | 37.8 dB | 19.5 dB | **15.8 dB** |
| syllabic modulation | 0.196 | 0.431 | **0.472** |
| Whisper | `"I'm sorry…"` | `"Hello, testing, testing."` | 100 % |

Speech sits at 15–21 dB crest. Above that, sustained vowels fall into the room floor and
only consonant transients survive — **temporal envelope smearing**. Measured live at
your desk: crest **18.5–37.2 dB**, modulation **0.14–0.24**.

**Crest cannot be fixed by gain.** It is `peak/rms`, a ratio, so a scalar multiply leaves
it mathematically invariant. Proven empirically: raising macOS input volume 40 → 85
lifted rms **6.4× (+16 dB)** and moved crest **not at all**. Every amplitude fix failed
for this reason. (Input gain is still worth having at 85 — it buys real SNR against the
ADC noise floor. It is just not the fix.)

**Eight hypotheses have now died on measurement:** HAL contention, gain staging, int16
quantization, amplitude, Continuity-mic selection, synapse mount, model-input
attenuation, OS-level input boost.

## Merged to main (`78c4680a63`)

| PR | What |
|---|---|
| 70094–70104 | (prior session) persona matrix, dual summon, flight recorder, desktop actuation guard, AGC ×2, profiler, acoustic telemetry |
| 70105 | `speak_immediate` — priority TTS interrupt |
| 70106 | **Window-scoped forensics verdict** + per-incident handoff integrity check |
| 70107 | **Partial cadence onset anchor** + speech-only duration gate |
| 70108 | **Golden Master injector** + the fix that finally gave Karen her voice |
| 70109 | **Adaptive proximity re-binder** (`AdaptiveInputManager`) |
| 70110 | Device identity by name, not CoreAudio index |

## Closed this session

### 1. ~~Synapse mount — `wake` dies silently~~ — PHANTOM
`_audio_synapse` is assigned at `harness.py:4502` **unconditionally, before**
`bridge.start()` — on any daemon whose attach socket answers it cannot be `None`.
Reproduced live by sending the exact frame `ov` sends:

```
[  1.00s] >>> SENT {"type": "audio", "cmd": "wake"}
[  1.05s] audio_state: {"state": "LISTENING"}
23:20:08 [Bootstrap] audio lease ARMED
```

`LISTENING` publishes only *after* `RemoteAudioLease.acquire` is granted, so the whole
chain was live. **Do not add `UnmountedSynapseError`** — it would turn a working path
into a daemon that won't boot. The original observation was most likely the honest
`UNAVAILABLE` published when no audio plane is running. A UX gap, not a mount bug.

### 1b. Forensics verdict was false on every incident — FIXED (#70106)
`_verdict` opened on the ring's **lifetime** over-full-scale counter, so one overdriven
frame early in a process latched `input gain, not the chain` onto every rejection
afterwards and the modulation reasoning never ran again. All 8 incidents on disk carried
the identical `150 frames above full scale (peak 5.8037)`.

```
OLD: device delivered 150 frames above full scale (peak 5.8037) — input gain, not the chain
NEW: speech rhythm survives the chain (raw 0.25 -> processed 0.262) — look downstream of the bus
```

Schema 1.0 → 1.1: lifetime totals moved under a `session` key; window-scoped
`over_full_scale_samples` added. Overdrive is now a *mechanism* reported alongside
measured chain damage, never a standalone cause — `AudioBus._fit_to_range` exists
precisely because this device delivers above ±1.0 routinely.

### 1c. Model-input attenuation was not real
The `0.0157` vs `0.3992` figure was a window-mismatch artifact (12 s ring vs 1.72 s
utterance tail). Full-window cross-correlation: **ratio 1.0000 at correlation 1.000, 0 dB.**
Structurally it could not be otherwise — `audio_bus.py:1097` taps `cleaned` and `:1103`
dispatches *the same array* to `streaming_stt.on_audio_frame` (registered at
`audio_pipeline_bootstrap.py:178`). One object, no transformation between them.

### 1d. Handoff integrity now measured, not assumed (#70106)
`CaptureForensics._handoff` proves or disproves it per incident, reusing the rings
already held: `lossless` / `scaled` / `unverifiable`. `scaled` is the int16→float32 and
double-normalisation class — absent here, but named automatically on the first incident
if it ever appears. Below `JARVIS_FORENSICS_HANDOFF_CORR_FLOOR` (0.99) it reports
`unverifiable` and says nothing more; an unproven match is not evidence of a fault.

Live in production: `correlation 1.0, gain_db 0.0, status lossless` on every aligning
incident.

### 1e. The first partial of every utterance was pre-roll — FIXED (#70107)
`_last_partial_time` was never reset at speech onset, so the interval test was already
true on the **first speech frame**. Every utterance opened by handing Whisper 320 ms of
pre-roll room tone plus 20 ms of speech. Fingerprint in one session:

```
334 x 00:00.340   <- 320ms pre-roll + one 20ms frame
119 x 00:00.860
 47 x 00:01.380
```

Whisper correctly returned nothing; the pipeline then logged *"the model rejected it"*,
wrote a forensic incident (crowding real evidence out of the 8-slot ring), and **held the
transcription lock so the first useful partial was skipped as busy**. Coupled defect: the
minimum-speech gate measured the whole buffer, and 320 ms of pre-roll cleared a 300 ms
floor on its own. Both fixed; first partial 340 ms → 860 ms.

**Already present, do not rebuild** (measured, and now pinned by tests): pre-roll,
post-roll (400 ms of speech yields a 1360 ms final = 320 pre + 400 speech + 640 post),
and micro-pause coalescence (a 200 ms gap yields **one** final of 1760 ms, not two).

### 1f. Karen's voice finally works (#70108)
`acoustic_feedback` called `MacOSVoice().speak()` — a method that has never existed; the
class exposes `say` / `say_and_wait`. **The delivery half still never ran**, after #70105
was written to fix exactly that. The `AttributeError` was raised inside an executor called
from a fire-and-forget task, so it bypassed the guard and surfaced only as
`Task exception was never retrieved`. Every test fake defined `speak`, mirroring the
buggy caller rather than the real class, so the suite stayed green.

Now calls `say_and_wait` (the scheduler holds the floor for the call's duration), a
done-callback logs ticket failures, and a structural test reflects the attribute out of
the source and asserts it exists on the **real** `MacOSVoice`.

**Confirmed live** — the operator heard it:
```
[Acoustic] speaking: The room's washing you out — I can hear something but not what.
[SPEECH START] source=tts_backend
```

### 2. DW routing audit — DONE, already correct
`remote_first()` defaults **true**; `route_for()` returns `remote` unless the router is
disabled, no model is resolved, or the breaker has opened on DW failure. Local is strictly
the emergency fallback, and `audio_plane_host` passes `llm_client=None` so in that host a
local engine is not even present.

Two tests added to `tests/governance/test_adaptive_voice_router.py`: the default route is
DoubleWord **with no env set** (the pre-existing test only proved the *override* worked and
would have passed if the default silently flipped), and local is reached only when DW
cannot serve. Pinned there, not in the audio suite — which microphone is bound and which
LLM answers are unrelated concerns.

### 3. Compute escalation — now unjustified on evidence
Superseded by the Golden Master result. The `base` model transcribes clean speech at
100 %, and the captured audio returns Whisper's **non-speech prior** (`"I'm sorry."`) —
which is what a model emits when handed something that is not language. A larger model
does not make unintelligible audio intelligible. Re-open only if a capture with
crest < 20 dB and modulation > 0.4 still fails.

### 4. AdaptiveInputManager — BUILT, MERGED, LIVE (#70109, #70110)
`rank_devices()`/`best_device()` had tests and zero production callers; they now have a
consumer. `AdaptiveInputManager` arms on sustained crest > 22 dB and probes candidates
**while the operator is speaking** — a microphone scored against an empty room is scored
against nothing, and every mic in a room hears the same voice.

`AudioBus.rebind_input` is the seam, deliberately **not** `stop()` + `start()`: `stop()`
clears `_mic_consumers`, so a restart returns a RUNNING bus with StreamingSTT and the
forensics taps silently detached. It replaces the `FullDuplexDevice` layer only and fails
closed to the incumbent.

**Live results — it works, and it correctly declines:**

```
01:28:59  candidate 0 'Derek J. Russell Microphone' sqi=0.801 continuity
01:28:59  candidate 1 'MacBook Pro Microphone'      sqi=0.732
01:28:59  staying on 1 — nothing cleared the margin
01:29:32  Continuity 0.842 vs MacBook 0.740 — staying
```

The Continuity mic is consistently better (a silent probe scores 0.275, so it is genuinely
hearing speech) but never by the 0.15 margin. Default **OFF**
(`JARVIS_ADAPTIVE_INPUT=1` to arm).

## Open work

### A. Score the incumbent from a fair window (methodological bias)
The incumbent is scored from `report_rejection` telemetry, which **only fires on failed
captures** — so the laptop's worst moments are compared against a fresh 1.5 s window from
the challenger. The bias runs *against* the incumbent, which means the margin's refusal to
swap was conservative-correct, but it also means the 0.07–0.10 gap is not trustworthy.
Fix by scoring the incumbent from the forensics `processed_bus` ring (which holds all
recent audio, not just failures) before considering any margin change.

### B. A closer transducer, physically
This is the actual remaining constraint. The re-binder is standing by and will detect and
bind either of:
- the Continuity mic, **awake and beside you** during a probe (it left the enumeration
  mid-test when the phone slept)
- a headset / AirPods

Target: raw crest < 20 dB with modulation > 0.4. Nothing in software gets there.

### C. Long buffers never endpointing
Observed 12.70 s and 13.14 s buffers with peak pinned at 0.9500. Either the room was
continuously noisy or the VAD is not finding a silence gap long enough to close the
utterance. If the latter, it is separate from the crest problem and would explain empty
long finals. Not yet investigated.

## Traps — read before touching anything

- **A CoreAudio index is not a device identity.** Observed live: the Continuity mic left
  the enumeration when the phone slept and `MacBook Pro Microphone` moved from index 1 to
  index 0. Anything remembered as an integer then points at a different device. Key on
  name, resolve at point of use (#70110).
- **Crest factor is scale-invariant.** No gain change can move it. Do not propose input
  volume as a fix for crest — verified empirically.
- **`sqi` is a composite.** Always log/read `crest`, `modulation` and `rms` beside it; a
  high score can conceal a worse crest than the incumbent.
- **A `PASS` on `"I'm sorry."`/`"Thank you."` is a false pass** — those are Whisper's
  non-speech prior, not transcripts. `stt_golden_master.py` now guards this.
- **Cumulative counters are not measurements.** Two separate bugs this session came from
  reading lifetime state as per-incident evidence — once in the code, once in my own
  analysis of it.
- **Alignment must search the whole ring.** Incidents are written 0.94–8.04 s after the
  buffer is assembled; a short search window finds noise and reports it as divergence.
  And **believe no gain figure without its correlation** — a corr-0.13 match produced a
  convincing +20.7 dB phantom.
- **Fakes must mirror the real contract, not the caller.** A fake defining `speak` kept a
  dead code path green across two releases.
- **Fire-and-forget tasks swallow exceptions.** `create_task` without a done-callback
  turns a hard failure into an interpreter warning on stderr, which in a daemon is nowhere.
- **The audio plane must be restarted to pick up merges.** A six-hour-old process emitted
  every fixed defect while the fixes sat on `main`; verify with `ps -o lstart=` against the
  merge time.
- **`unified_supervisor.py` needs a TTY.** `nohup` wedges it at 0 % CPU, state `SN`.
- **It segfaults during `Py_FinalizeEx`** — pre-existing; `SIGTERM` may need `-9`.
- **git needs `dangerouslyDisableSandbox`** in this `.nosync` repo, plus
  `-c branch.autoSetupMerge=false`.
- **`main` is push-protected.** PRs only.
- **`lsof` does not reliably report unix-socket listeners.**
- **The profiler refuses to run while the audio plane is live.** Stop, measure, restart.
- **`tests/core/` is unhealthy**: one test hangs past 180 s, plus 2 pre-existing collection
  errors. Unrelated to this work.

## Process state at handoff

**Nothing is running.** Audio plane stopped, no battle-test daemon, no supervisor.
Previous background processes were owned by the agent session and died with it.

```bash
# audio plane — add JARVIS_ACOUSTIC_FEEDBACK=false to stop Karen announcing
# that she cannot hear you on every utterance
JARVIS_ADAPTIVE_INPUT=1 python3 backend/audio/audio_plane_host.py
```

macOS input volume was left at **85** (was 40). Keep it — it is +16 dB of real SNR.

## Diagnostics available

```bash
# Does the PIPELINE work? (synthetic speech, known transcript, real engine)
python3 scripts/stt_golden_master.py --golden "Karen can you hear me clearly"

# Does a CAPTURED buffer transcribe in isolation?
python3 scripts/stt_golden_master.py --incident .jarvis/capture_forensics/incident-XXXX
python3 scripts/stt_golden_master.py --replay path/to.wav

# Measure the microphone (stop the audio plane first)
python3 scripts/hardware_acoustic_profiler.py --device 1 --seconds 5

# Every STT rejection writes an incident automatically (ring of 8 — archive
# anything you want to keep, it evicts fast under load)
ls .jarvis/capture_forensics/
```

## Test suites

`tests/audio` + `tests/voice`: **548 passed, 5 skipped**.
New spines: `test_capture_forensics_verdict.py` (17), `test_stt_partial_cadence.py` (7),
`test_adaptive_input.py` (19).

## Still unproven on hardware

A complete conversational turn: `ov` → `wake` → operator speaks → **Karen answers the
question**. Every component is now proven individually — the lease arms, capture runs,
the pipeline transcribes clean speech, DW is primary, and Karen's voice works. What has
never happened is Whisper reading the operator's actual voice, and on current evidence
that requires a closer microphone rather than more code.
