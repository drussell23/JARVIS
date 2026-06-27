---
title: Gap #7 — Presentation Restraint CLOSED (2026-05-04)
modules: [scripts/ouroboros_battle_test.py]
status: historical
source: project_gap_7_presentation_restraint.md
---

# Gap #7 — Presentation Restraint CLOSED (2026-05-04)

5-slice arc closing the operator-flagged "presentation gap": O+V's
boot screen pushed ~30 lines of dashboard content while CC fits the
same ergonomic value into ~5 lines. Rather than copying CC verbatim,
this arc applies CC's *restraint discipline* to O+V's existing
surfaces — content moves behind on-demand REPL verbs, chrome stays
quiet, outcomes stay loud, identity (emojis) stays.

## Slices

* **Slice 1** — `presentation_restraint.py` (~520 LOC, 43 tests):
  Boot redesign substrate. Closed `MinimalWelcomePayload` frozen
  record + `render_minimal_welcome` (CC-style panel — keeps emojis
  in title) + `render_preflight` / `render_organism` (re-render the
  moved content on demand) + in-process `set_captured_layers` /
  `get_captured_layers` for /organism + `suppress_diagnostic_logs`
  (turns off `jarvis.shutdown.diagnostics` propagation to root —
  file handler at DEBUG still preserves full forensics). Wired into
  `serpent_flow.boot_banner` (master-flag short-circuit) +
  `scripts/ouroboros_battle_test.py:_print_preflight` (skip verbose
  checklist when restraint on, **API-key fail-fast still runs** —
  that's a hard error, not chrome). Two new REPL verbs
  ``/preflight`` + ``/organism``.

* **Slice 2** — Color discipline + idle status + load-bearing TTY
  fix (~140 LOC, 26 tests): `chrome_color()` returns ``"dim"`` under
  restraint so green stays reserved for outcomes (✨ evolved, ✓ test
  passed). `format_idle_breadcrumb()` produces compact
  ``IDLE · main · $0.04/$0.50 · EXPLORE`` content. **Critical bug
  fix**: `should_render()` was checking `sys.stdout.isatty()` but
  `prompt_toolkit.patch_stdout(raw=True)` (active during REPL)
  replaces stdout with a non-TTY proxy — the live status line
  silently never surfaced during normal use. Fix: new
  `real_stdout_isatty()` checks `sys.__stdout__` (Python's saved
  unpatched reference), with fallback to patched stdout when
  `__stdout__` is None.

* **Slice 3** — `repl_completion.py` (~430 LOC, 47 tests):
  Auto-discovered slash palette via `inspect.getmembers` +
  `_handle_*` convention. **27 verbs** (22 handlers + 5 built-ins)
  registered automatically — adding a new method auto-registers,
  removing one auto-removes. Single source of truth, no parallel
  registration table. `SlashCommandCompleter` only fires on `/`
  prefix (operator typing prose never sees suggestions). Persistent
  history at `.jarvis/repl_history` via `prompt_toolkit.history.
  FileHistory` (atomic per-command writes) + Ctrl+R reverse-search.

* **Slice 4** — `repl_input_polish.py` (~430 LOC, 54 tests): three
  thin polishes sharing one master flag. `extract_attachments()`
  pulls `@<path>` mentions out of input via heuristic regex
  (whitespace boundary + slash-or-extension predicate filters out
  email addresses + Python decorators). `make_esc_cancel_binding()`
  builds a `KeyBindings` with bare-Esc-when-buffer-empty firing
  `_handle_cancel` for the most-recent active op. Terminal title
  via OSC 0 escape sequences (`\\x1b]0;<text>\\x07` to stderr,
  TERM-aware skipping for `linux` / `dumb`, length-bounded with
  ellipsis, OSC-special chars sanitized). Hooks at op_started /
  op_completed / op_failed.

* **Slice 5** — Graduation. Three master flags
  (`JARVIS_PRESENTATION_RESTRAINT_ENABLED` +
  `JARVIS_REPL_COMPLETION_ENABLED` +
  `JARVIS_REPL_INPUT_POLISH_ENABLED`) flipped default-TRUE.
  Module-owned `register_flags(registry) -> 6` (3 master + 3
  sub-knobs). `register_shipped_invariants() -> 5` AST pins:
    1. **presentation_restraint_default_true** — env-get default
       must remain ``"true"`` (BUG-FIX REGRESSION PIN — silent
       reversion would re-densify boot)
    2. **boot_banner_short_circuits_under_restraint** — 
       `serpent_flow.boot_banner` must call `is_restraint_enabled()`
       and `render_minimal_welcome`
    3. **repl_loop_wires_completion_and_polish** — `_loop` must
       invoke `build_completion_wiring` + `extract_attachments` +
       `make_esc_cancel_binding`
    4. **op_lifecycle_sets_terminal_title** — `op_started` /
       `op_completed` / `op_failed` must each call
       `_maybe_set_terminal_title`
    5. **status_line_uses_real_stdout_isatty** — `should_render()`
       must use `real_stdout_isatty` (not raw `sys.stdout.isatty`)
       — Slice 2 TTY gate fix regression pin

## Numbers

* **198 / 198 green** across 5 slices on first integrated run
* ~1,520 LOC substrate + ~1,800 LOC tests
* 6 FlagSpec seeds; 5 ShippedCodeInvariant pins; 1 memory file

## Architectural properties

* **Information density: pull-on-demand** — preflight / organism /
  diagnostics moved behind verbs. Boot is ~10 lines.
* **Visual hierarchy: green = outcomes only** — chrome (activity
  ribbon, section dividers) goes dim under restraint. Outcomes
  (✨ evolved, ✓ test passed) stay bright_green.
* **Identity preserved** — emojis kept (🐍, 🧭, 🧠, 📡, ⚙️, 🐍, 📝).
  CC restraint applied to *count and density*, not erasure.
* **No hardcoded verb list** — completion palette discovered via
  `inspect.getmembers` from `_handle_*` methods. Adding a new
  method auto-registers; removing auto-removes. Built-ins
  (/help, /status, etc.) layered on top via small explicit list.
* **Heuristic email/decorator safety** — @-mention extractor
  requires whitespace boundary AND (slash OR extension). Email
  addresses and Python decorators stay untouched.
* **TTY gate uses unpatched stdout** — `real_stdout_isatty` checks
  `sys.__stdout__` so the live status line surfaces during the
  REPL even with `patch_stdout(raw=True)` proxying `sys.stdout`.
* **Terminal title TERM-aware** — OSC 0 emission skipped for
  `linux` / `dumb` / non-TTY. Capable terminals get title updates;
  others get no-op.

## Master-flag rollback contract

```
JARVIS_PRESENTATION_RESTRAINT_ENABLED=false  → legacy verbose dashboard
JARVIS_REPL_COMPLETION_ENABLED=false          → no slash palette / tab / history
JARVIS_REPL_INPUT_POLISH_ENABLED=false        → no @-mentions / Esc / title
JARVIS_REPL_HISTORY_ENABLED=false             → no .jarvis/repl_history persistence
JARVIS_TERMINAL_TITLE_ENABLED=false           → opt-out of OSC title only
```

All default-true post-graduation. Setting any to `false` returns
byte-identical legacy behavior for that subsystem.

## Architectural reuse

| Existing asset | Reuse |
|---|---|
| `Rich.Panel` (from `diff_preview` + `ouroboros_tui`) | Welcome panel — same primitive |
| `StatusLineBuilder.render_plain()` (Gap #1+5) | Idle breadcrumb shorts the legacy verbose path |
| `_handle_*` dispatch convention (existing in SerpentREPL) | Single source of truth for completion verbs |
| `prompt_toolkit.completion.Completer` + `history.FileHistory` | Stdlib-equivalent primitives — no custom dropdown / file format |
| OSC 0 escape sequence (terminal-emulator standard) | No third-party terminal libs |
| Gap #1+5's `live_status_line.make_bottom_toolbar_callable` | Now actually fires (TTY gate fix) — operators see Gap #1 status content for the first time |
| `flag_registry_seed._FLAG_PROVIDER_PACKAGES` | `register_flags()` auto-discovered |
| `shipped_code_invariants._INVARIANT_PROVIDER_PACKAGES` | `register_shipped_invariants()` auto-discovered |

## Why-nots (deliberately deferred)

* **Path completion for `@` tokens** — `prompt_toolkit.completion.
  PathCompleter` could complete `@be` → `@backend/...`. Hybrid
  completer composing slash + path is one extra slice. Operator
  flow today: type `@<path>` and remember the path; tab completion
  for prose-mode is the polish.
* **`/help` enrichment with examples per verb** — `VerbRegistry`
  has the descriptions; `/help` could render them as a table.
  Today the operator sees verbs in the slash palette dropdown.
* **Multi-monitor terminal title differentiation** — when operator
  has 3 O+V instances open, all titles say "O+V · GENERATE …".
  Adding session-id discriminator is a small follow-up.
