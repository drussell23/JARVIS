# Spec: The `ov` Awakening — Product Boot Experience + Karen Boot Briefing

**Date:** 2026-07-07
**Author:** Derek J. Russell (design conducted with Claude)
**Status:** Design — approved in session; awaiting written-spec review before implementation plan
**Scope tag:** UX Polish · Sprint 2 of the "O+V as a product" program
**Predecessor:** `2026-07-06-ov-theme-and-boot-cockpit-design.md` (Sprint 1 — theme engine, `ov` binary, wake-sequence renderer)

---

## 1. Context & problem statement

Sprint 1 shipped the Restrained Mono theme engine, the packaged `ov` binary,
and the `WakeSequenceRenderer` — but the renderer was **never wired into the
boot** (zero importers; wired-but-inert), and bare `ov` still delegates
straight into the battle-test soak harness, whose boot output is a debugging
surface: Zombie Reaper banner, single-flight report, full Preflight Checklist,
and an INFO-level log flood — much of it raw ANSI that predates the theme.

**Category mismatch:** a soak-debug harness is the product's front door.
`claude` opens with a quiet branded canvas; `ov` opens with a wall of
diagnostics.

This sprint makes `ov` boot as an **awakening**: the organism's ouroboros
crest draws itself alive, Karen speaks a live-state briefing, real boot
readiness renders honestly beneath, and the whole ceremony cools into the
restrained teal working surface. The soak workflow keeps its verbose output
untouched.

### Approved design decisions (from session iteration)

| Decision | Outcome |
|---|---|
| Brand vs. restraint | **Threshold ritual**: full-chroma crest at boot only → cools to Restrained Mono. Evolves Sprint 1 §2's "no logomark" — consciously superseded for the boot moment only. |
| The mark | **Crest v5**: procedurally generated serpent — solid fill, hard-edge quadrant rendering (no anti-aliasing/blur), scale-free gradient, tail tapering into the open mouth, wedge head with ignited eye, thick flat-topped block V. |
| Static assets | **None.** The crest is generated at runtime from geometry (pure math, <50ms, cached). One geometry source, tier-resolved. |
| Karen at boot | **Dynamic briefing** composed from live system vectors via the shipped Sprint-2 speech pipeline, with a local live-state fallback breaker. |
| Working surface | **Option A — frameless flow.** After cool-down: one cooled header line, then the existing themed REPL surfaces. The serpent lives only in the awakening. |
| Clean-boot architecture | **Option 1 — presentation-layer split.** `ov` conducts presentation; the harness keeps the one 6-layer boot ordering; banner emission sites gate at the source. |

### Non-goals

- `ov attach` / daemon transport (still a follow-up sprint).
- First-run onboarding wizard.
- Any new authority surface. The awakening is presentation + voice only.
- Changing soak/CI/graduation-run output (SOAK mode is byte-identical).
- Serpent-framed `/split` cockpit (Option C was declined — may revisit later).
- iTerm2/kitty inline-image logo blit (optional garnish, explicitly cut).

---

## 2. Global constraints (every task inherits these)

1. **CLI/terminal only.** Text cells + ANSI truecolor inside the existing
   prompt_toolkit REPL and `patch_stdout(raw=True)` model. No GUI, no web.
2. **Theme-tier degradable.** All rendering routes through `ui/theme.py`'s
   ladder (TRUECOLOR → C256 → STANDARD → NONE). Zero escape leakage at NONE.
   TTY detection uses `real_stdout_isatty` (`sys.__stdout__`) — never
   `sys.stdout.isatty()` (fails under `patch_stdout`).
3. **REPL-safe.** The awakening must not block the event loop, corrupt
   scrollback, or delay fatal-error visibility. NEVER-raises contract on all
   render paths.
4. **Root-cause only (Mandate 1).** Harness banner suppression happens by
   conditional logic **at each emission source**. No global stdout redirect,
   no stream wrapper, no regex filter in `ov.py`. The ERROR/CRITICAL bypass
   is **structural**: fatal telemetry does not route through the gate at all,
   so no gate state can suppress it.
5. **Reactive geometry (Mandate 2).** `ui/crest.py` scales proportionally
   from `console.size` measured at execution time, clamped to sane bounds.
   No hardcoded padding, no fixed canvas dimensions.
6. **Thin conductor (Mandate 3).** `ui/awakening.py` orchestrates existing
   components — it embeds `WakeSequenceRenderer` + `WakeModel` and subscribes
   via `BootTimer.add_observer`. It must not invent a redundant phase-tracking
   loop. `cli/ov.py` stays a facade: it sets presentation mode and delegates
   to the one shared bootstrap; zero boot-ordering logic.
7. **Bulletproof (Mandate 4).** The test strategy MUST include a SIGWINCH
   mid-animation resize proof (§9.2) and the ERROR-bypass proof (§9.3).
8. **Env-var driven config**, `from __future__ import annotations`,
   Python 3.9+ (`asyncio.wait_for`, not `asyncio.timeout`) — repo standards.
9. **Karen reuse (Sprint-2 DRY).** Boot briefing reuses `LedgerView`-shaped
   payload filtering (`strip_code`), persona prompt, `DWSpeechProvider`,
   `KarenSpeechSynthesizer`, and the `VoiceDuplexArbiter` — no parallel
   speech path.

---

## 3. Architecture — components

### 3.1 New: `backend/core/ouroboros/ui/crest.py`

Procedural crest generator. Imports: stdlib + `ui.theme` only (leaf module,
same dependency rule as `theme.py`).

**Geometry (ported from the approved v5 generator):**
- Annulus (coil) with mouth gap at the top; body thickness tapers over the
  final ~52° of arc and the tail tip **intrudes ~15° into the gap**, thinning
  between the jaws.
- Wedge head (capsule, narrowing to snout) at the gap's clockwise edge,
  open-mouth notch cut from the snout half; eye disc offset inside the head.
- Thick V: two stroke capsules meeting at a point, **flat machined top**
  (samples above the top line rejected).
- Hard-edge rendering: each cell = 2×2 subpixels; each subpixel supersampled
  (3×3) and **thresholded at 0.5 coverage** — no anti-aliasing, no blending
  (the approved fix for v4's blur). Quadrant glyph lookup (16 patterns).
  Isolated single-quadrant crumb cells (no orthogonal neighbor) are removed.
- Pixel aspect correction (~1.08 vertical) so the coil renders circular.

**Reactivity (Mandate 2 — normative):** the generator takes the measured
console width/height at call time and derives ALL radii/thickness/V metrics
proportionally from a single scale factor, clamped to
`[CREST_MIN_COLS, min(measured, CREST_MAX_COLS)]` (defaults 46/72, env
overridable). Below `CREST_MIN_COLS` or insufficient height the crest reports
`unavailable` and the awakening degrades (§6). Zero absolute dimensions.

**Output:** `CrestFrame` — per-cell `(x, y, glyph, style_or_rgb, kind,
trace_delay_s)` where `kind ∈ {coil, head, eye, v}`; gradient color and
trace delay both derived from arc angle (tail=0 → head=1). Tier resolution:

| Tier | Color treatment |
|---|---|
| TRUECOLOR | Per-cell RGB gradient (green → gold → purple circulation) |
| C256 | Same RGB, Rich color downgrade |
| STANDARD | Single `accent` style for coil/head, bold for V — geometry + trace identical |
| NONE / no-unicode | Crest unavailable — caller degrades to plain wake lines |

Result cached per `(cols-bucket, rows-bucket, tier)`; regeneration on
resize is cheap (<50ms budget, pure math).

### 3.2 New: `backend/core/ouroboros/ui/awakening.py`

The awakening conductor — a **thin orchestrator** (Mandate 3):

- Owns one Rich `Live` region (16ms max refresh, mirroring
  `stream_renderer.py` cadence) rendering: crest (animated by revealing cells
  whose `trace_delay ≤ elapsed`) above the **embedded**
  `WakeSequenceRenderer` output (real `BootTimer` phases via `add_observer`
  — the Sprint-1 module finally wired, not reimplemented).
- **Eye-ignition callback:** when the trace completes (elapsed ≥ max delay),
  invokes the injected `on_ignition` callable exactly once — production wires
  this to the Karen briefing submission (§4). Injection keeps `ui/` free of
  governance imports (dependency rule preserved).
- **Breathing:** post-trace, a slow brightness modulation on coil styles
  (style-table swap per pulse tick, bounded) until boot phases complete.
- **"live" honesty:** the final state renders only when `WakeModel.is_live`
  — animation completion NEVER fakes readiness (Sprint-1 §5 principle).
- **Cool-down:** ~400ms staged recolor of crest cells toward `accent`/muted →
  Live region replaced by the single cooled header line
  (`ov · ouroboros   live` + one muted context line) → returns control; the
  REPL proceeds exactly as today.
- **Skip:** `Esc` or `Enter` ONLY (raw, non-blocking read while Live is
  active) jumps straight to cool-down completion. Any other key is passed
  through untouched — "any keypress" would swallow the first character of a
  command typed during the wake phase and corrupt the REPL's stdin buffer
  (approved §10 resolution 2). Headless/non-TTY/NONE-tier/no-unicode/narrow
  terminal → no Live at all: plain sequential wake lines (existing fallback
  path).
- **Resize (SIGWINCH):** on console size change mid-animation, the conductor
  regenerates the crest for the new measurement (cache miss → recompute),
  remaps elapsed trace time onto the new frame, and continues — no crash, no
  fractured cells (proven by §9.2).
- NEVER-raises: every callback and render wrapped; failure degrades to plain
  wake lines and logs at DEBUG.

### 3.3 New: `backend/core/ouroboros/governance/comms/karen_boot_briefing.py`

`BootBriefingComposer` — gathers live vectors, composes speech, one breaker:

- **Vectors (all read-only, each optional, gathered concurrently at boot
  start):** LastSessionSummary tokens (`apply=…/N verify=P/T commit=…`),
  queued-op count + top intent from intake, posture + confidence from the
  DirectionInferrer triplet files, pending-approval count. A vector that
  fails to read is simply absent — no exceptions escape the gather.
- **Primary path (DRY, Sprint-2 pipeline):** vectors → briefing payload →
  `strip_code`/`first_line` filters → persona prompt (Karen: Australian,
  dry, high-SNR) → `DWSpeechProvider` → `KarenSpeechSynthesizer` →
  `VoiceDuplexArbiter.submit` at `PROACTIVE_INFO`.
- **Circuit breaker (Mandate 4 of the briefing decision):**
  `asyncio.wait_for(…, timeout=JARVIS_KAREN_BOOT_BRIEF_TIMEOUT_S)` (default
  4.0s). On timeout / provider error / empty result → **local deterministic
  composition over the SAME gathered vectors** — state-driven prose, not a
  canned greeting ("Awake. Two ops queued overnight; one awaiting your
  sign-off."). Truly-empty state (fresh install) degrades to the still-
  factual minimal line ("Awake. First session on this repo."). No generic
  boilerplate strings disconnected from state (approved mandate).
- **Voice-off** (`JARVIS_KAREN_VOICE_ENABLED` false): the composed line still
  renders as the `💭` narrative line under the cooled header — silent but
  observable (philosophy §7).
- Entire briefing is a fire-and-forget task (reference retained); it can
  never block, delay, or crash the boot. Karen speaking does not gate the
  prompt; barge-in works from her first word (shipped duplex stack).

### 3.4 Modified: `backend/core/ouroboros/cli/ov.py`

Facade only. `cockpit` action: resolve presentation mode → set
`JARVIS_OV_PRESENTATION=cockpit` (process-local env) → delegate to
`scripts.ouroboros_battle_test.main(argv)` exactly as today. `run`/`daemon`
continue to imply SOAK. No boot logic, no output manipulation (Mandate 1),
no duplicated initialization (Mandate 3).

### 3.5 Modified: harness presentation gate (root cause)

`battle_test/harness.py` + `scripts/ouroboros_battle_test.py`:

- `PresentationMode` enum {`COCKPIT`, `SOAK`} resolved once from
  `JARVIS_OV_PRESENTATION` (default `SOAK` — legacy script and `ov run`
  unchanged by construction).
- **Each banner emission site branches at the source** (Mandate 1): zombie
  reaper banner, single-flight report, `_print_preflight`, boot-timing
  emit_summary chrome. COCKPIT: the content is withheld from stdout and
  remains available via the existing `/preflight` + `/organism` verbs
  (already designed to hold detail). SOAK: byte-identical current output
  (golden-tested, §9.4).
- Logging: COCKPIT sets root level WARNING (INFO flood gone).
  **Structural ERROR/CRITICAL bypass:** fatal paths — the No-API-keys error,
  single-flight conflict report, and any `logger.error/critical` — do not
  pass through the gate's conditionals at all; they print/log unconditionally
  in both modes. The gate can only ever withhold nominal-detail output.
- The awakening hook: in COCKPIT mode the harness invokes
  `ui.awakening.run_awakening(...)` (injected `on_ignition` → briefing) at
  the point where SOAK prints its banners. One boot ordering, two skins.

### 3.6 Working surface (after cool-down)

Frameless flow (approved Option A): cooled header once, then today's themed
surfaces — `live_status_line` bottom toolbar, narrative channel, op blocks,
`ov ›` prompt. No persistent brand chrome. No changes required beyond the
cooled-header print.

---

## 4. The awakening timeline (normative sequence)

```
t0      ov (cockpit) → PresentationMode=COCKPIT → shared bootstrap starts
        briefing vector-gather task starts (concurrent, read-only)
t0..    crest traces tail→body→head (~1.4s nominal) in the Live region;
        real BootTimer phases render beneath via embedded WakeSequenceRenderer
ignite  trace complete → eye pulse begins → on_ignition fires ONCE →
        briefing submitted (DW primary / 4s breaker → local live-state line)
hold    crest breathes until WakeModel.is_live (real readiness gates "live")
cool    ~400ms recolor sweep → Live replaced by cooled header → REPL prompt
after   Karen may still be speaking (independent audio plane; barge-in live)
```

Skip: keypress at any point → immediate cool-down completion (briefing still
delivered). Headless / `ov run` / `ov daemon` / non-TTY / NONE tier /
no-unicode / narrow terminal: no Live, plain wake lines; SOAK output where
SOAK mode applies.

---

## 5. Flags & configuration

| Flag | Default | Meaning |
|---|---|---|
| `JARVIS_OV_PRESENTATION` | `soak` | `cockpit` set by `ov` cockpit action; everything else soaks |
| `JARVIS_OV_AWAKENING_ENABLED` | `true` | Master for the crest animation (cockpit only); `false` → cooled header directly |
| `JARVIS_KAREN_BOOT_BRIEF_TIMEOUT_S` | `4.0` | DW breaker deadline for the boot briefing |
| `JARVIS_KAREN_VOICE_ENABLED` | existing | Voice-off → briefing renders as text line only |
| `JARVIS_OV_CREST_MIN_COLS` / `_MAX_COLS` | `46` / `72` | Reactive-geometry clamp bounds |
| `JARVIS_UI_THEME_FORCE_TIER` | existing | Debug/test tier override (Sprint 1) |

No rollback flag for the presentation gate itself: SOAK mode **is** the
legacy renderer (mode default preserves it), mirroring Sprint 1's
no-old-look-flag decision.

---

## 6. Degradation matrix

| Condition | Behavior |
|---|---|
| TRUECOLOR + unicode + TTY + width ≥ min | Full crest: per-cell gradient, trace, eye pulse, breathing, cool-down |
| C256 | Same geometry/animation, palette downgraded |
| STANDARD (16) | Same geometry/trace, single accent color, bold V |
| NONE / `NO_COLOR` / pipe | No crest; plain wake lines; zero escapes |
| No unicode | Crest unavailable (quadrant glyphs required); plain wake lines |
| Non-TTY / headless / `ov run` | No Live; plain lines; SOAK banners per mode |
| Width < min or height insufficient | Crest skipped; cooled header only |
| SIGWINCH mid-animation | Regenerate at new measurement; remap elapsed trace; continue (§9.2) |
| DW down at boot | 4s breaker → local live-state briefing line |
| Awakening render failure | NEVER-raises → plain wake lines, DEBUG log |
| Fatal init failure | ERROR/CRITICAL bypass — full visibility in both modes |

---

## 7. What this sprint deliberately reuses (DRY ledger)

| Existing asset | Role here |
|---|---|
| `ui/theme.py` | Tier detection, styles, console factory — untouched, consumed |
| `ui/wake_sequence.py` | Embedded verbatim as the phase view (finally wired) |
| `battle_test/boot_timing.py::add_observer` | Real-phase feed (Sprint 1 addition) |
| `stream_renderer.py` 16ms cadence pattern | Live refresh discipline |
| Sprint-2 Karen stack (`karen_synth/*`, duplex arbiter) | Entire speech path |
| `LastSessionSummary`, intake queue, posture triplet | Briefing vectors |
| `/preflight`, `/organism` verbs | Home for withheld banner detail |
| Harness bootstrap + `battle_main(argv)` | The one boot ordering (facade delegation) |

---

## 8. Component/file map

**New:**
- `backend/core/ouroboros/ui/crest.py`
- `backend/core/ouroboros/ui/awakening.py`
- `backend/core/ouroboros/governance/comms/karen_boot_briefing.py`

**Modified:**
- `backend/core/ouroboros/cli/ov.py` (presentation-mode env + delegation only)
- `backend/core/ouroboros/battle_test/harness.py` (PresentationMode + gated emission sites + awakening hook)
- `scripts/ouroboros_battle_test.py` (gate at its banner sites: reaper, preflight, logging level)

**Tests:**
- `tests/ui/test_crest.py`, `tests/ui/test_awakening.py`
- `tests/voice/test_karen_boot_briefing.py`
- `tests/battle_test/test_presentation_gate.py`
- `tests/ui/test_theme_guard.py` (extended)

---

## 9. Testing strategy (normative)

1. **Crest invariants** — for widths {46, 60, 72, 100→clamped} × tiers
   {TRUECOLOR, C256, STANDARD} (forced): solid interior (every coil cell's
   ring-interior neighbors filled — no holes), zero isolated crumb cells,
   tail-tip cells present inside the gap arc, eye cells present, V cells
   present with flat top row; NONE tier / no-unicode → generator reports
   unavailable; rendered NONE output contains no `\x1b[`. Geometry derived
   values must differ across widths (proves reactivity — Mandate 2).
2. **SIGWINCH resize proof (Mandate 4)** — drive the conductor with a fake
   clock mid-trace, deliver a console-size change (direct resize injection
   plus, on POSIX, a real `os.kill(os.getpid(), signal.SIGWINCH)` against a
   pty-backed console), assert: no exception, crest regenerated at the new
   measurement, elapsed-trace remap keeps monotonic reveal (no cell flashes
   back to hidden), final cool-down header renders intact at the new width.
3. **ERROR bypass proof (Mandate 1)** — in COCKPIT mode with the gate active:
   inject `logger.error` + `logger.critical` records and the No-API-keys
   fatal path; assert all reach the terminal stream; assert reaper/preflight
   banners do NOT; assert `/preflight` verb still serves the withheld detail.
4. **SOAK golden regression** — SOAK-mode boot banner output byte-identical
   to pre-change capture (reaper text, preflight checklist, log level).
5. **Conductor** — synthetic BootTimer feed → phases render beneath crest;
   `on_ignition` fires exactly once; keypress skip → immediate cooled state;
   "live" withheld until `WakeModel.is_live`; render-failure injection →
   plain-lines fallback, no raise; headless → no Live.
6. **Briefing** — synthetic vectors → composed text contains the live values;
   forced DW timeout/error → fallback line contains the same live values
   (proves state-driven fallback, not boilerplate); gather-failure per vector
   → vector absent, no exception; voice-off → text-only render; whole task
   time-boxed (never blocks the boot task).
7. **Guard test extension** — grep-enforce: no banner `print(` outside the
   gate in the touched harness regions; no raw ANSI constants reintroduced.
8. **Live-mic acceptance gate (local, interactive — the finish line):**
   1. `ov` on real hardware: crest animates, cools, prompt arrives.
   2. Karen speaks a briefing that references real state (queued ops /
      last session).
   3. **Barge-in:** speak over her mid-briefing → she stops within the
      arbiter's preemption latency.
   4. **Spoken build command** → VoiceCommandSensor → a real op enters the
      governed loop (visible in the op blocks).
   5. **AEC:** Karen's own speech does not retrigger VAD/barge-in (no
      self-echo loop).
   6. `ov run` in a second terminal → full SOAK banners (regression eyeball).

---

## 10. Review resolutions (approved 2026-07-07)

1. **Cool-down destination line — RESOLVED: keep the context line.** The
   cooled header is `ov · ouroboros   live` plus one muted context line
   (`awakened HH:MM · N ops queued`), its values sourced from the same
   briefing vector gather (LedgerView-shaped telemetry — DRY, high-SNR).
   The header is never empty.
2. **Skip key scope — RESOLVED: `Esc`/`Enter` only.** "Any keypress" would
   swallow the first character of a command typed during the wake phase and
   corrupt the REPL's stdin buffer (Bulletproof violation). All other keys
   pass through untouched.
3. **Crest max width — RESOLVED: hard clamp at 72 columns.** Generation
   stays dynamic below the clamp; unbounded scalar growth on ultra-wide
   viewports would disperse visual density and break Restrained Mono.
