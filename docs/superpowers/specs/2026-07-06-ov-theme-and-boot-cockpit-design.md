# Spec: `ov` — Unified Theme Engine + Wake-Sequence Boot Cockpit

**Date:** 2026-07-06
**Author:** Derek J. Russell (design conducted with Claude)
**Status:** Design — awaiting review before implementation plan
**Scope tag:** UX Polish · Sprint 1 of the "O+V as a product" program

---

## 1. Context

O+V is a proactive autonomous engineering organism. Its CLI carries the
capability, but the *presentation* reads as an internal dev harness, not a
product:

- Launch is `python3 scripts/ouroboros_battle_test.py --cost-cap 0.50 ...` —
  a raw script invocation named "battle test."
- Rendering is visually noisy: six emoji icons in the boot banner, five accent
  colors mixed inside `dim`-bordered panels, hardcoded ASCII rules
  (`"─" * 52`), literal style strings (`"bold cyan"`, `"bright_blue"`)
  scattered across ~7 modules.
- There is no design system — every component picks its own colors.

This spec is **Sprint 1** of a larger product program (later sprints:
first-run onboarding, autonomy/trust controls, daemon/attach lifecycle, Karen
voice, verb-sprawl taming). It deliberately covers only:

1. A unified **theme engine** (`theme.py`) — the styling root cause.
2. The **`ov`** entry point (real packaged binary).
3. The **wake-sequence boot cockpit** — the first impression, hooked to real
   init state.
4. The **naming/namespace** decision.

### Non-goals (explicitly deferred to later sprints)

- First-run onboarding wizard (API keys, budget, repo-trust, autonomy level).
- Autonomy/trust control surface ("what it did while you were away").
- Full `ov daemon` / `ov attach` detach-reattach lifecycle (designed here at
  the interface level; **implementation** of attach/daemon transport is a
  follow-up sprint — this sprint ships `ov` cockpit + `ov run`).
- Karen voice redesign.
- Verb-sprawl / tiered `/help`.

---

## 2. Naming & namespace roles (approved)

The "OUROBOROS + VENOM" identity is retained by giving each name a distinct
role rather than mashing them into one compound title:

| Name | Role | Where it appears |
|------|------|------------------|
| **`ov`** | The execution binary you type | Shell command, packaging entry point |
| **Ouroboros** | Top-level application / the organism | Product wordmark, boot title, docs |
| **Venom** | The agentic execution engine subsystem | "venom primed" in wake seq; internal |

Visual direction (approved): **Restrained Mono** — one accent color applied to
interactive verbs only, no frame, no emoji, no logomark, maximal whitespace.
Claude Code aesthetic.

---

## 3. Architecture — `theme.py` (the styling root cause)

### 3.1 Placement (satisfies DRY mandate #3)

New dependency-free leaf module:

```
backend/core/ouroboros/ui/            # NEW package
  __init__.py
  theme.py                            # tokens, tiers, console factory, primitives
```

`ui/theme.py` imports **only** stdlib + Rich. It must never import from
`governance/` or `battle_test/`, so it can be imported *upward* by:

- the `ov` entry script (`cli/ov.py`),
- Ouroboros governance modules (e.g. `boot_timing.emit_summary`),
- Venom tool rendering (`battle_test/tool_render_view.py`).

This inverts the current dependency smell (presentation logic trapped inside
`battle_test/`) and gives every layer one import target.

### 3.2 Semantic tokens (no component names a color again)

```python
class Token(str, Enum):
    ACCENT    = "accent"     # the one brand color — interactive verbs, prompt glyph, active op-id
    HEADING   = "heading"    # bold default fg — titles (NOT colored)
    BODY      = "body"       # default fg — primary text
    MUTED     = "muted"      # dim — subtitles, context line, separators, hints
    SUCCESS   = "success"    # green — OUTCOMES ONLY (apply/verify OK), reserved
    WARNING   = "warning"    # yellow — soft warnings
    DANGER    = "danger"     # red — errors, rollback
    RULE      = "rule"       # muted hairline for separators
```

**Accent value (the one open taste knob):** default **restrained cyan-teal**
— `truecolor #3AAFA9`, `256 → color(73)`, `standard → "cyan"`. Retunable in a
single token-table entry. *Flag at review if a different hue is wanted
(amber / CC-coral / desaturated green).*

### 3.3 Capability tiers (bulletproof mandate #4)

```python
class ColorTier(IntEnum):
    NONE      = 0   # NO_COLOR, pipe, dumb term → styles stripped
    STANDARD  = 1   # 8/16 color
    C256      = 2   # 256 color
    TRUECOLOR = 3   # 24-bit
```

`detect_tier(console) -> ColorTier` reads `console.color_system`
(`"truecolor" | "256" | "standard" | None`). `supports_unicode() -> bool`
checks `LANG`/`LC_*` for UTF-8 and `console.legacy_windows`.

Tokens and box styles are **tier-indexed**, resolved once at
`build_console()`:

| Token/asset | TRUECOLOR | C256 | STANDARD | NONE |
|-------------|-----------|------|----------|------|
| `accent` | `#3AAFA9` | `color(73)` | `cyan` | *(stripped)* |
| `muted` | `grey50` | `grey50` | `dim` | *(stripped)* |
| box | `ROUNDED` | `ROUNDED` | `ASCII` | `ASCII` |
| separator glyph | `─` | `─` | `─`/`-` (unicode?) | `-` |
| middot `·` | `·` | `·` | unicode? `·`:`-` | `-` |

Structural alignment is Rich's responsibility (it measures), so every fallback
preserves layout. `NO_COLOR` / `FORCE_COLOR` honored natively by Rich.

### 3.4 Primitives (collapse duplicated draw logic — mandate #3)

The three current panel-drawing paths (`presentation_restraint`,
`boot_banner`, ad-hoc consoles) collapse into:

```python
def build_console(*, force_tier: ColorTier | None = None) -> rich.console.Console
    # THE single Console factory. Injects a tier-resolved rich.Theme.
    # force_tier honors JARVIS_UI_THEME_FORCE_TIER for debugging/tests.

def render_rule(console, label: str | None = None) -> None
    # width-measuring separator via console.rule(style="rule"). Kills "─" * N.

def render_panel(console, body, *, token: Token = Token.MUTED, title=None) -> None
    # single panel helper; box_for(tier) picks ROUNDED/ASCII.

def mark(name: str) -> str
    # glyph resolver: "check" -> "✓"/"OK", "dot" -> "·"/"-", degrades by unicode support.

def style_for(token: Token) -> str        # semantic name -> resolved Rich style string
def box_for(tier: ColorTier) -> rich.box.Box
```

### 3.5 Migration of existing consumers (mandate #1 — delete literals at source)

Refactored to import tokens; literal styles removed:

- `battle_test/presentation_restraint.py` (welcome / preflight / organism)
- `battle_test/serpent_flow.py::boot_banner`
- `battle_test/status_line.py`, `battle_test/live_status_line.py`
- `battle_test/diff_preview.py`, `battle_test/diff_display.py`
- `battle_test/tool_render_view.py`
- `battle_test/boot_timing.py::emit_summary` (`"─" * 52`, `"█"` bar → theme)

**Consequence (approved trade):** root-cause deletion means there is **no
"old-look" rollback flag**. Safety comes from the degradation tiers plus a
`JARVIS_UI_THEME_FORCE_TIER` debug override — not from keeping the old code.

---

## 4. Architecture — the `ov` entry point

### 4.1 Packaging (real binary, not an alias — mandate #1)

`pyproject.toml` currently has only `[tool.*]` tables — no `[project]`. This
sprint adds proper packaging metadata:

```toml
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "ouroboros-ov"
version = "0.1.0"
requires-python = ">=3.9"
# dependencies sourced from existing requirements files

[project.scripts]
ov = "backend.core.ouroboros.cli.ov:main"
```

`pip install -e .` → `ov` on PATH. New thin CLI module:

```
backend/core/ouroboros/cli/
  __init__.py
  ov.py            # argparse front-end → dispatches to shared harness bootstrap
```

### 4.2 Subcommands (maps to a proactive organism)

| Command | Behavior | This sprint |
|---------|----------|-------------|
| `ov` | Boot organism + wake sequence → **cockpit** (async-status-primary) | ✅ ship |
| `ov run "<intent>"` | Seed one intent, run to completion, exit (non-interactive) | ✅ ship |
| `ov status` | Print organism health without attaching | ✅ ship (read-only) |
| `ov daemon` | Boot proactive, headless (no cockpit) | ⚠️ alias to existing `--headless` |
| `ov attach` | Attach cockpit to an already-running organism | ⛔ follow-up sprint (stub w/ clear "coming soon") |

### 4.3 Leverage existing harness (mandate #3 — no rewrite)

`scripts/ouroboros_battle_test.py` already owns the boot/argument machinery.
Extract its bootstrap into a reusable callable so **both** the legacy script
and `ov` call one path:

```
scripts/ouroboros_battle_test.py  ─┐
                                    ├─→  harness.BattleTestHarness (unchanged core)
backend/core/ouroboros/cli/ov.py  ─┘
```

`ov.py` is a thin translator: parse subcommand → build the same config object
the script builds → call the shared bootstrap. Legacy script keeps working
(now themed). No engine logic is duplicated or moved.

### 4.4 Cockpit model (mandate #2 — daemon cockpit, not REPL)

The cockpit is **output-primary**: the organism is alive and its async status
is the foreground. Built by *re-emphasizing* existing surfaces, not replacing
the REPL:

- **Foreground:** `live_status_line` (bottom toolbar: phase/route/posture/
  cost/op-id) + `narrative_channel` (the model's voice) + `op_block_buffer`
  (collapsed op history) stream continuously — driven by the organism's
  own event emitter.
- **Steering lane:** the existing `SerpentREPL` input remains, but framed as a
  non-blocking command lane over a running system, not a blocking prompt.
  `patch_stdout(raw=True)` (already used) interleaves async output above input.

The distinction from `claude`: `claude` renders an idle prompt awaiting you;
`ov` renders a **living system you observe and steer**. No new engine — an
inversion of emphasis + the theme.

---

## 5. Architecture — the wake sequence (mandate #1 + #4)

### 5.1 Real-state hook (root cause, not static strings)

`BootTimer` (`boot_timing.py`) already records every boot phase
(`begin`/`end`/`mark`, `PhaseRecord.is_in_flight`). Its docstring already
anticipates a `boot_timed` event. Minimal root-cause extension:

```python
# boot_timing.py — additive, non-breaking
def add_observer(self, cb: Callable[[PhaseRecord], None]) -> None: ...
# begin()/end()/mark() invoke observers best-effort (never raise into boot)
```

The wake sequence is a **consumer** of these real transitions — a phase that
is `in_flight` renders as active; when it `end`s, it renders complete. The
sequence therefore reflects **true system readiness** (sensors registering,
loop arming, Venom priming), never a scripted animation.

### 5.2 Renderer + debounce/backpressure (mandate #4)

```
backend/core/ouroboros/ui/wake_sequence.py   # NEW (imports ui/theme.py only)
  WakeSequenceRenderer
    - subscribes to BootTimer via add_observer
    - coalesces rapid PhaseRecords: dedupe by name, keep latest state
    - renders via Rich Live, refresh capped at ≤16ms (reuse stream_renderer cadence)
    - bounded Live height; completed phases fold into a capped tail buffer
    - on non-TTY / headless: falls back to plain sequential lines (no Live)
```

Backpressure rule: if Venom/sensors spin up faster than the refresh window,
the renderer draws the **latest coalesced state**, not every event — the
terminal is never flooded. This mirrors the existing 16ms batched-update
pattern in `stream_renderer.py`.

### 5.3 Rendered form (Restrained Mono)

```
  ov · ouroboros
  self-evolving engineering organism

  waking   sensors online · loop armed · venom primed
  live
```

- `ov · ouroboros` — `ov` in accent, `ouroboros` in heading weight.
- middot separators via `mark("dot")` (degrade to `-`).
- "waking" line updates in place from real phase state; "live" appears only
  when boot phases complete.
- Non-TTY: same content, printed as plain resolving lines.

---

## 6. Bulletproofing summary (mandate #4)

| Failure mode | Structural protection |
|--------------|----------------------|
| No TrueColor | tier-indexed tokens → 256 → 16 → stripped, alignment preserved |
| No Unicode | `mark()` + `box_for()` degrade glyphs/boxes to ASCII |
| Piped / `NO_COLOR` / dumb term | `NONE` tier: styles stripped, structure via spacing; zero escapes emitted |
| Rapid engine spin-up | coalesce + 16ms refresh cap + bounded Live height |
| Rich import failure | every render wrapped; falls back to plain `print` (existing contract) |
| Boot observer error | `add_observer` callbacks best-effort; never raise into boot hot path |
| Narrow terminal (40-col) | Rich measurement; wake seq + welcome must render clean at 40 and 120 |

---

## 7. Component map (files)

**New:**
- `backend/core/ouroboros/ui/__init__.py`
- `backend/core/ouroboros/ui/theme.py`
- `backend/core/ouroboros/ui/wake_sequence.py`
- `backend/core/ouroboros/cli/__init__.py`
- `backend/core/ouroboros/cli/ov.py`

**Modified:**
- `pyproject.toml` (add `[build-system]`, `[project]`, `[project.scripts]`)
- `backend/core/ouroboros/battle_test/boot_timing.py` (add `add_observer`)
- `backend/core/ouroboros/battle_test/presentation_restraint.py`
- `backend/core/ouroboros/battle_test/serpent_flow.py` (`boot_banner`)
- `backend/core/ouroboros/battle_test/status_line.py`, `live_status_line.py`
- `backend/core/ouroboros/battle_test/diff_preview.py`, `diff_display.py`
- `backend/core/ouroboros/battle_test/tool_render_view.py`
- `scripts/ouroboros_battle_test.py` (extract shared bootstrap callable)

---

## 8. Testing strategy

1. **Tier matrix:** `build_console(force_tier=...)` × {NONE, STANDARD, C256,
   TRUECOLOR} × width {40, 80, 120} × unicode {on, off} → assert no exception,
   token resolves, ASCII box when unicode off, **zero escape leakage** in NONE
   tier (render to string, assert no `\x1b[`).
2. **Guard test (enforces mandate #1 permanently):** grep refactored modules
   for banned literals (`bold cyan`, `bright_`, `"─" *`, raw emoji in banner)
   → assert none remain. Makes the cleanup a regression, not a one-time pass.
3. **BootTimer observer:** register observer, drive `begin`/`end`/`mark`,
   assert callback receives correct `PhaseRecord`s; assert a raising observer
   never propagates into `begin`/`end`.
4. **Wake sequence reflects real state:** feed synthetic phase transitions,
   assert rendered lines match `in_flight`→`done`; assert "live" only after
   completion.
5. **Debounce/backpressure:** fire N≫refresh-window phase events, assert
   render count is bounded (coalesced), latest state shown, no unbounded
   output.
6. **`ov` dispatch:** `ov run "<intent>"` seeds one intent and exits;
   `ov status` is read-only; `ov attach` prints the "coming soon" stub.
7. **Non-TTY fallback:** headless run → wake seq uses plain lines, cockpit
   falls through to existing spinner paths (matches current TTY-gating).

---

## 9. Rollout & flags

- `JARVIS_UI_THEME_FORCE_TIER` — debug override to force a tier (tests +
  troubleshooting). No default (auto-detect).
- No `enable/disable` master for the theme: it *is* the renderer (mandate #1).
- `ov` binary coexists with the legacy `python3 scripts/...` path, which keeps
  working and is now themed.
- Boot-timing already gated by `JARVIS_BOOT_TIMING_ENABLED` (default true); the
  wake observer is a no-op when boot timing is disabled — graceful.

---

## 10. Open questions for review

1. **Accent hue** — restrained cyan-teal `#3AAFA9` default. Keep, or
   amber / CC-coral / desaturated-green?
2. **`ov daemon` / `ov attach`** — confirm attach/daemon transport is a
   follow-up sprint (this sprint stubs `attach`, aliases `daemon` to
   `--headless`). Or pull full detach/reattach into this sprint?
3. **Package name** — `ouroboros-ov` on PyPI-style metadata. Acceptable, or
   prefer another distribution name? (Command stays `ov` regardless.)
