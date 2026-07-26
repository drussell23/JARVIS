# Handoff — voice pipeline session, 2026-07-25

## The headline

**The pipeline was never broken. The microphone wasn't receiving the operator.**

Measured with `scripts/hardware_acoustic_profiler.py`:

| | seating distance | close to lid |
|---|---|---|
| rms | 0.0033 | **0.0114** |
| crest | 37.8 dB | **19.5 dB** |
| syllabic modulation | 0.196 | **0.431** |
| `no_speech_prob` | 0.522 | **0.153** |
| Whisper | `"I'm sorry, I'm sorry…"` | **`"Hello, testing, testing."`** |

Speech sits at 15–21 dB crest. At 37.8 dB only consonant bursts survive and sustained
vowels fall into the room floor — **temporal envelope smearing**. No gain stage recovers
that, which is why every amplitude fix failed.

**Five hypotheses died on measurement** before this: HAL contention, gain staging,
int16 quantization, amplitude, Continuity-mic selection.

## Merged to main (`bfde7ae270`)

| PR | What |
|---|---|
| 70094 | Persona matrix + zero-cost phatic fast path (OV→Karen, JARVIS→Daniel, wake-word routing) |
| 70095 | Dual-summon delegation + speech turnstile (role arbitration, 300 ms acoustic tail, lane dispatch) |
| 70096 | Capture forensics flight recorder (raw / processed / model-input + verdict) |
| 70097 | thin-client exit-75 test alignment |
| 70098 | Desktop actuation guard — **fixed the Weather app opening on its own** |
| 70099 | Proportional pre-quantization AGC (replaced tanh saturator) |
| 70100 | Inter-Agent Context Bus (compressed delegation payload, firewall-fenced digest) |
| 70101 | Async-signal-safe shutdown handler (fixed SIGSEGV on SIGTERM) |
| 70102 | Bi-directional AGC (upward normalization, adaptive noise floor) |
| 70103 | Hardware acoustic profiler — **the tool that found the answer** |
| 70104 | Acoustic quality telemetry (SQI + Karen says when she can't hear) |

**Pushed, NOT merged:** `feat/speak-immediate` — priority TTS interrupt, 7 tests.

## Open work, in priority order

### 1. ~~Synapse mount — `wake` dies silently~~ — PHANTOM, closed 2026-07-25 23:20
The forensic step was done first, as instructed, and the premise did not survive it.

`_audio_synapse` is assigned at `harness.py:4502` **unconditionally, before**
`bridge.start()` — so on any daemon whose attach socket answers, it cannot be `None`.

Reproduced live against daemon pid 99385 by sending the exact frame `ov` sends
(`_route_operator_line` → `client.send_audio("wake")` → `{"type":"audio","cmd":"wake"}`):

```
[  1.00s] >>> SENT {"type": "audio", "cmd": "wake"}
[  1.05s] audio_state: {"state": "LISTENING"}
```

and on the hardware plane, 50 ms later:

```
23:20:08 [Bootstrap] audio lease ARMED
23:20:08 [Bootstrap] lease ARM → CONVERSATION mode (pipeline loop running)
```

`LISTENING` is only published *after* `RemoteAudioLease.acquire` is granted, so the
whole chain — synapse → lease → supervisor → mic — was live. **Do not add
`UnmountedSynapseError`.** The likely original observation is the honest
`UNAVAILABLE` published when no audio plane is running, which the TUI renders
quietly enough to read as nothing happening. That is a UX gap, not a mount bug.

### 1b. Capture forensics was issuing a false verdict on every incident — FIXED
Found while confirming the plane was live. `_verdict` opened on the ring's
**lifetime** over-full-scale counter, so the first overdriven frame of a process
latched `input gain, not the chain` onto every rejection written afterwards and the
syllabic-modulation comparison the hint exists for never ran again. All 8 incidents
on disk carried the identical `150 frames above full scale (peak 5.8037)` — one early
transient, recited forever. Overdrive is also not a fault on its own:
`AudioBus._fit_to_range` exists because this device delivers above ±1.0 routinely.

Replaying the 8 stored incidents through the fixed verdict:

```
OLD: device delivered 150 frames above full scale (peak 5.8037) — input gain, not the chain
NEW: speech rhythm survives the chain (raw 0.25 -> processed 0.262) — look downstream of the bus
```

**This is a new lead and it points somewhere nobody has looked.** Syllabic modulation
*survives* the bus (raw ≈ 0.25 → processed ≈ 0.26) and the recogniser still returns
empty. That is not distance and not the chain — it is downstream of the bus: the
buffer assembled for the model, or the model stage itself.

### 1c. The model-input attenuation was not real — the handoff is bit-exact
The `0.0157` vs `0.3992` figure was a window-mismatch artifact (12 s ring containing
one loud transient, vs a 1.72 s utterance tail) and should never have been promoted to
a lead. Settled by full-window normalised cross-correlation of each `model_input.wav`
against its own `processed_bus.wav`:

```
lag_s  corr  bus_seg_pk  mdl_pk   ratio    dB
 7.72  1.000     0.2900  0.2900  1.0000  0.00
 1.00  1.000     0.0195  0.0195  1.0000  0.00
 8.04  1.000     0.5806  0.5806  1.0000  0.00
 2.78  1.000     0.0508  0.0508  1.0000  0.00
```

**Ratio exactly 1.0000 at correlation 1.000 — 0 dB. No attenuation, no dtype fault, no
double normalisation.** Structurally it could not be otherwise: `audio_bus.py:1097`
calls `note_processed(cleaned, ...)` and `audio_bus.py:1103` dispatches *the same
`cleaned` array* to `streaming_stt.on_audio_frame` (registered at
`audio_pipeline_bootstrap.py:178`). There is no transformation between the tap and the
consumer — it is one object.

Two traps worth carrying forward:

- **Alignment must search the whole ring.** Incidents are written 1–8 s after the
  buffer is assembled (measured: 0.94 s–8.04 s), so a short search window finds
  garbage and reports it as divergence. My first pass searched 0.5 s and produced
  correlations of 0.05–0.33, which read convincingly as "the buffer isn't the bus
  audio". It was a search bug.
- **Low correlation produces large fake gains.** Incident `234722` aligns at corr 0.13
  and reports +20.7 dB. Believe no gain figure without its correlation.

### 1d. Handoff integrity is now measured per-incident, not assumed
The module header always described the processed ring as "exactly what the model was
handed" — an assumption the recorder never checked, which is why this cost a hunt.
`CaptureForensics._handoff` now proves or disproves it on every incident, reusing the
rings already held (no new taps): FFT normalised cross-correlation → `lossless` /
`scaled` / `unverifiable`.

`scaled` is the int16→float32 and double-normalisation class the hunt was looking for.
It did not turn out to be present, but it is now named automatically on the first
incident instead of costing a session. The epistemic floor matters as much as the
detector: below `JARVIS_FORENSICS_HANDOFF_CORR_FLOOR` (0.99) the report is
`unverifiable` and says nothing further — an unproven match is not evidence of a fault.
Knobs: `JARVIS_FORENSICS_HANDOFF_CHECK`, `..._CORR_FLOOR`, `..._GAIN_TOL_DB`.

Replayed over the 8 stored incidents: 4 `lossless` (corr 1.0, 0.0 dB), 4
`unverifiable` — including the +20.7 dB phantom, correctly suppressed.

**Where the fault actually is:** the audio reaching Whisper *is* the bus audio, and it
carries speech rhythm. So the remaining candidates are the recogniser itself, or what
`_schedule_transcription` buffered for it. `streaming_stt.py:391-440` already documents
two solved faults of exactly that shape (VAD shrapnel; trailing room tone). That is the
next place to look — not the DSP chain.

Metrics schema 1.0 → 1.1: lifetime totals moved under a `session` key, window-scoped
`over_full_scale_samples` added. Spine: `tests/audio/test_capture_forensics_verdict.py`
(9 tests; the recorder shipped in #70096 with none).

### 2. DW routing audit
Untouched. Confirm `AdaptiveVoiceRouter` prefers DoubleWord and only falls back to local
`UnifiedModelServing` on network timeout.

### 3. Compute escalation — unjustified, not disproven
The large-v3 probe was **inconclusive**: it grabbed an incident holding near-silence
(`peak 0.025`), and `small`/`medium` hit `PermissionError` downloading.
Re-run against a `raw_device.wav` from a capture where speech was actually present,
with models pre-pulled. If a bigger model can't read it either, the answer is device
selection, not GCP.

### 4. AdaptiveInputManager — live CoreAudio rebind (deliberately deferred)
`rank_devices()` and `best_device()` are **built, merged and tested** in
`backend/audio/acoustic_quality.py`. Only the live stream rebind is missing. Deferred
because tearing down a contended input stream wedged processes three times this session.

## Traps — read before touching anything

- **`unified_supervisor.py` needs a TTY.** `python3 unified_supervisor.py` in a real
  terminal. Backgrounding it with `nohup` wedges it at 0 % CPU, state `SN` — it blocks on
  a read that never returns. This cost ~6 restart attempts and 3 outages.
- **It segfaults during `Py_FinalizeEx`** — a C extension frees memory badly at teardown.
  Pre-existing, separate from the handler fix. It cannot exit cleanly; `SIGTERM` may need
  escalation to `-9`.
- **Establish how a daemon was started before stopping it** (`ps -o ppid=`, and check the
  `+` in `STAT`). The LaunchAgent `com.jarvis.supervisor` was **never loaded** — the plist
  exists but `launchctl` has no such label.
- **git needs `dangerouslyDisableSandbox`** in this `.nosync` repo, plus
  `-c branch.autoSetupMerge=false` — a plain `checkout -b` fails on `.git/config` write and
  leaves the tree **half-reverted**.
- **`main` is push-protected** (local hook + remote). PRs only.
- **`lsof` does not reliably report unix-socket listeners.** It reported `NOBODY` on a
  socket that answered a handshake immediately.
- **The profiler refuses to run while the audio plane is live** (contention guard).
  Stop the plane, measure, restart it.
- **`tests/core/` is unhealthy**: one test hangs past 180 s, plus 2 pre-existing collection
  errors (`graduation_tracker` missing, `pyautogui` uninstalled). Unrelated to this work.

## Process state at handoff

- audio plane: **running**, pid 98229, socket bound
- unified_supervisor: **not running** — start it yourself in a terminal
- battle-test daemon: pid 99385 (spawned by `ov`)

## Diagnostics available

```bash
# measure the microphone (stop the audio plane first)
python3 scripts/hardware_acoustic_profiler.py --device 1 --seconds 5
# writes .jarvis/acoustic_profile/latest.json

# every STT rejection writes an incident automatically
ls .jarvis/capture_forensics/
# raw_device.wav / processed_bus.wav / model_input.wav / metrics.json + verdict
```

## Still unproven on hardware

`ov` → `wake` → Karen answering, and the dual summon. All of it is built and tested
in-process; none of it has run against a live spoken turn, because until the distance
finding the mic never delivered intelligible audio. **Test it sitting close to the lid.**
