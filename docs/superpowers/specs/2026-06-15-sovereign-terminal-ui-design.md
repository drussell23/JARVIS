# Sovereign Terminal UI — Claude-Code-Clean Presentation for O+V

**Date:** 2026-06-15
**Status:** design (pending user review → writing-plans)

## Goal
Make O+V's live terminal presentation read as cleanly as Claude Code: a borderless
`⏺`/`⎿` hierarchy in grayscale, with color reserved for outcomes — while staying robust
against terminal-width overflow and async "dead air." This **tunes the existing
presentation layer** (`presentation_restraint.py` + the SerpentFlow render methods); it
deletes the box-drawing paths rather than adding a parallel renderer, and rides the existing
master flags so OFF is byte-identical rollback.

## Non-goals
- No change to what information is shown (same phases, provider, cost, risk, diff) — only
  *how* it's rendered.
- No change to the TUI split/focus layout FSM, the diff preview, or the observability/SSE
  surfaces.
- No new dependency — Rich is already the rendering substrate.

## Architecture
The render layer is the only thing touched. Three composable units, each independently
testable and OFF-inert:

1. **Chrome vocabulary** (`presentation_restraint.py`): glyph constants (`GLYPH_ACTION="⏺"`,
   `GLYPH_RESULT="⎿"`), a grayscale-first palette accessor, and pure formatting helpers.
   No Rich import here beyond what already exists; returns plain strings / Rich markup.
2. **Op-block renderer** (`serpent_flow.py`): the methods that currently draw
   `┌ │ └ ─` frames + per-phase emojis + colored badges are rewritten to emit the flat
   `⏺ action` / `  ⎿ result` form with 2-space indentation and one blank line between op
   groups.
3. **Fit + pulse** (`presentation_restraint.py` helpers, consumed by `serpent_flow.py`):
   `_fit()` (overflow) and an async `pulse()` context (spinner), both TTY-aware.

### Data flow
Unchanged: orchestrator/intake emit the same phase events → SerpentFlow render methods →
Rich `Console`. Only the render-method bodies change. Every event still maps 1:1 to a
rendered line; we change the glyph/indent/color, not the event model.

---

## Phase 1 — Typographical Restraint
**Changes (the 4 agreed):**
1. **Kill the boxes.** Remove every `_C['border']` `┌ │ └ ─` emission in the op-block /
   plan / generation render methods. Hierarchy is expressed as:
   ```
   ⏺ <action>  <primary target>            <dim right-meta>
     ⎿ <result / sub-detail, dim>
   ```
   Action line at column 0 with `⏺`; result lines indented 2 spaces under it with `⎿`.
2. **Glyph vocabulary.** One vocabulary everywhere: `⏺` for an action/phase-start, `⎿` for
   its result/continuation. Per-phase emojis (`🔬 🧬 ⚙️ 🐍` on lines) are removed.
3. **Grayscale chrome.** Action text = default/white; **all** secondary tokens (provider,
   cost, timing, op-id, posture) = `dim`. Color is reserved: **green only** for
   success/`✓`/applied, **red only** for failure. Risk tier shows as a dim word
   (`notify_apply`) except `approval_required`/`blocked` which may use a single accent.
4. **Vertical rhythm.** Exactly one blank line between distinct operation groups (not
   between an action and its own `⎿` results).

**Emoji decision (default, adjustable at review):** keep the operator's signature emojis in
the **boot panel only**; remove all per-line phase emojis. (If the user prefers fully
emoji-free, flip one constant.)

**Reversibility:** gated by the existing `JARVIS_PRESENTATION_RESTRAINT_ENABLED` master (+ a
sub-flag `JARVIS_OPBLOCK_BORDERLESS_ENABLED`, default TRUE under the master). When the
master is off, the legacy boxed renderer path is retained byte-identically.

---

## Phase 2 — Overflow Integrity Protocol
**Problem:** long file paths / tracebacks wrap, shattering the indentation hierarchy.

**Approach (leverage Rich, do not hand-roll width math):**
- A single helper `fit(text, *, indent, width=None) -> str` that renders through a Rich
  `Text(text, no_wrap=True, overflow="ellipsis")` (or equivalent truncation) so wide chars,
  emoji, and ANSI are width-counted correctly.
- Width source precedence: explicit `width` arg → live `console.width` →
  `os.get_terminal_size().columns` → `80` (headless/pipe fallback).
- Effective budget = `width - indent`; the indent prefix is added back after truncation so
  the `⏺`/`⎿` column never moves.
- Applied uniformly: every rendered op-block line passes through `fit()`. Multi-line
  payloads (tracebacks, diffs) keep their existing dedicated renderers (diff preview is
  out of scope) but their *one-line summaries* in the op-block are `fit()`-bounded.

**Why not raw `os.get_terminal_size()` per line:** it miscounts ANSI/wide chars and
duplicates Rich's measurer. Rich is already the substrate; `get_terminal_size` is only the
no-console fallback. (Matches the repo's "no duplication / leverage existing" mandate.)

---

## Phase 3 — Asynchronous Pulse
**Problem:** during `synthesizing` (network I/O) and `validating` (LiveKernelValidator
subprocess) the active line is frozen — no proof the FSM is alive.

**Approach (leverage Rich spinner, TTY-gated):**
- An async helper `pulse(console, action_line, *, frames="⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏", interval=0.1)`
  used as an async context manager around the awaited work:
  ```python
  async with pulse(console, "⏺ synthesizing · doubleword"):
      result = await provider.generate(...)
  # on exit: spinner cleared, final ⏺ line rewritten in place
  ```
- Renders **only on the active action line**, in place (carriage-return / Rich `Live` or
  `Status`), and clears cleanly on exit (success or exception) leaving the resolved line.
- **TTY-gated** via `real_stdout_isatty()` (the load-bearing check — `sys.__stdout__`, works
  under `patch_stdout(raw=True)`). Headless / sandbox / CI / piped → **no-op** (the awaited
  work still runs; no spinner art leaks into logs). This mirrors the existing rule that the
  stream renderer + diff preview require a real TTY.
- Non-blocking: the spinner is a separate `asyncio` task that ticks while the real coroutine
  is awaited; cancelled + joined on context exit. Never blocks the event loop.

**Reversibility:** gated by `JARVIS_TUI_PULSE_ENABLED` (default TRUE under the restraint
master). Off → static line (today's behavior).

---

## Components / files
| File | Change |
|---|---|
| `battle_test/presentation_restraint.py` | add glyph constants, grayscale palette accessor, `fit()`, async `pulse()`; new sub-flags in `register_flags` |
| `battle_test/serpent_flow.py` | rewrite op-block / plan / generation render methods to borderless `⏺`/`⎿`; route every line through `fit()`; wrap synth/validate awaits in `pulse()` |
| `tests/battle_test/` | new render-snapshot + fit + pulse-gating tests (extend existing presentation tests) |

`stream_renderer.py`, `status_line.py`, `diff_preview.py` are **not** rewritten, but the
status-line/glyph palette is aligned to the grayscale rule for consistency (color audit
only, no structural change).

## Testing
- **Render snapshots:** given a synthetic op lifecycle, assert the rendered text has no
  `┌│└─`, uses `⏺`/`⎿`, indents results 2 spaces, and emits one blank line between groups.
- **Grayscale:** assert secondary tokens carry `dim` and that green/red appear only on
  success/failure lines (markup assertions).
- **Overflow:** a path longer than `width` → output length ≤ width, ends with `…`, `⏺`
  column unchanged; width fallback chain exercised (no console → get_terminal_size → 80).
- **Pulse gating:** `real_stdout_isatty()` True → spinner task starts/stops + line cleared;
  False (headless) → `pulse()` is a no-op, awaited work still completes, zero spinner output.
- **OFF parity:** master flag off → byte-identical to the legacy boxed renderer.

## Risks / invariants
- **TTY detection is load-bearing** — use `real_stdout_isatty()` everywhere, never
  `sys.stdout.isatty()` (fails under `patch_stdout(raw=True)`).
- **Rich import stays in the view layer** — `fit()`/`pulse()` live with the other render
  code; no Rich import leaks into governance/authority modules.
- **No event-model change** — pure presentation; if a render path is missed, worst case is a
  legacy-looking line, never a crash (every helper fail-soft → returns plain text).
