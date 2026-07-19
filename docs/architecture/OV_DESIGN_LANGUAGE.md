# O+V Design Language — the Operator Surface Contract

**Status**: operator-authorized 2026-07-18. Structurally enforced by
`battle_test/presentation_router.py` (the middleware every CLI-facing
line pipes through) and `tests/battle_test/test_presentation_ast_parity.py`
(the AST sentinel that makes bypass a test failure, not a code review
hope). This document is the *why*; the router is the *law*.

## 1. Identity

O+V's surface is **Restrained Mono with a pulse**: near-monochrome calm
(single teal chrome, `chrome_color()` reserving green for outcomes),
borderless flowing output, and a *rationed* set of semantic glyphs that
give the organism a voice CC doesn't have. Ceremony at the door (the
awakening crest is the ONE sanctioned moment of brand color), restraint
at the desk.

## 2. The Glyph Ration (closed set — six, plus typography)

Every operator-facing glyph is one of SIX semantic marks, each with a
meaning, an ASCII degradation (via `theme.mark()`), and a rule for when
it may appear. Anything else is scrubbed by the router.

| Glyph | Name | ASCII | Meaning — and the ONLY time it appears |
|---|---|---|---|
| `⏺` | `action` | `*` | An actor did/does something: op steps, tool calls, verb outputs' lead line |
| `⎿` | `detail` | `-` | Continuation/detail under an action line; recast telemetry lands here |
| `💭` | `voice` | `K:` | The organism speaking in its own voice (Karen, narrative intent) |
| `🗣` | `human` | `you:` | The operator's words echoed back (transcripts, preambles) |
| `⚠` | `warn` | `!` | Degradation the operator should notice (budget, dry runway, drift) |
| `🎙` | `audio` | `mic` | Live audio-plane state (listening, voice channel active) |

**Typographic set** (not rationed — structure, not semantics): `·`
separator, `✓`/`✗` outcomes, `›` prompt, `─` rule. These come from the
existing `theme._GLYPHS` table and degrade with it.

Legacy glyphs are ALIASED, not tolerated: `⛲`→`⚠`, and decorative
emoji (🔍 📊 🛤️ 💡 🧠 ⚡ 🔥 🎯 …) are stripped on the UI plane. Boot
banners and log files are NOT the UI plane; they keep their grep
anchors.

## 3. Microcopy Voice

1. **Never show a field name.** `runway=DRY` is telemetry; the operator
   reads `runway: DRY` (recast) or, better, prose (`anthropic dry,
   resets in ~5m`). The router recasts `key=value` dumps automatically;
   surfaces SHOULD do better than the recast by hand.
2. **Never show a raw enum or a truncated hash without meaning.**
   `chat-02b3e8ce49ad` earns its place only as an `/expand`-able ref.
3. **Sentence-case, no exclamation marks, no filler.** The organism is
   calm. "generation abandoned, back to prompt" — not "Cancelled!!".
4. **Outcomes are color, not adjectives.** Green text IS "success";
   the word "successfully" is banned.

## 4. Color Roles (composes `chrome_color()` + `theme.Token`)

| Role | Rule |
|---|---|
| Chrome (labels, structure) | single teal; NEVER green |
| Outcomes | green — reserved by `chrome_color()` discipline |
| Failures | red, dim red for detail |
| Warnings | yellow, only with `⚠` |
| Brand chroma | the awakening crest ONLY |

Tier degradation is owned by `ui/theme.py` (truecolor→256→16→none);
surfaces never branch on `TERM` themselves — they ask the theme.

## 5. Density & Spacing

- One blank line maximum between blocks (router collapses runs).
- A verb's output: one `action` lead line, `detail` lines beneath,
  nothing else. If it needs more than ~8 lines, it needs an
  `/expand` ref instead.
- No boxes, no panels, no borders on flowing output (Sovereign
  borderless render is the standard; Panels only for the boot summary
  and diff previews).

## 6. Enforcement (the part that survives without discipline)

- **`PresentationRouter`** (`battle_test/presentation_router.py`): the
  gateway. `route_line()` scrubs unregistered glyphs (alias → strip),
  recasts `key=value` telemetry into detail-voice, normalizes spacing,
  and prefixes the semantic glyph — composing `theme.mark()` so ASCII
  terminals get the same geometry. Master
  `JARVIS_PRESENTATION_ROUTER_ENABLED` (default on; off = byte-identical
  legacy passthrough).
- **Chokepoint wiring**: the harness's `_repl_print` — the funnel every
  REPL verb, chat turn, and IPC render already flows through — pipes
  through the router. New surfaces inherit the law by using the funnel.
- **AST Sentinel** (`test_presentation_ast_parity.py`): walks the
  declared UI-plane modules and fails on (a) raw `print()` calls,
  (b) string literals carrying unregistered emoji, (c) a `_repl_print`
  that no longer routes. The module list grows additively — sweeping a
  surface means adding it to the sentinel so it can never rot back.

## 7. Sweep Ledger

| Surface | State |
|---|---|
| `_repl_print` funnel (all harness verbs, chat sink, IPC renders) | routed 2026-07-18 |
| status-line liquidity token | canonical `⚠` 2026-07-18 |
| `presentation_router.py` / `chat_text_bridge.py` / `audio_state_ipc.py` | sentinel-pinned |
| SerpentFlow op blocks / diff renders | already speak the language (origin of it) |
| boot preflight, `/help`, narrative renderer, legacy verb bodies | NOT yet swept — add to sentinel as each is swept |
