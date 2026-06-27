---
title: Gap #1+3+5 — Live Status Line + Collapsible Op Blocks (2026-05-04)
modules: []
status: historical
source: project_gap_135_live_status_collapse.md
---

# Gap #1+3+5 — Live Status Line + Collapsible Op Blocks (2026-05-04)

5-slice arc closing the last three operator-UX gaps from the original
SerpentFlow / LiveDashboard audit. **Critical re-diagnosis** mid-arc:
Gap #5 ("REPL is blocking") was wrong — `patch_stdout(raw=True)` already
interleaves concurrent output above the prompt. What operators
actually missed was the **persistent status line** (Gap #1). Both
gaps collapsed into one slice.

## Slices

* **Slice 1** — `live_status_line.py` (~270 LOC): wraps the existing
  ``StatusLineBuilder.render_plain()`` via
  ``make_bottom_toolbar_callable`` and plumbs it into
  ``PromptSession(bottom_toolbar=...)`` at ``serpent_flow.py:4096``.
  Zero new rendering surface — reuses the fully-built builder
  registered by ``harness.py:1389``. Master flag
  ``JARVIS_LIVE_STATUS_LINE_ENABLED``. Closes Gap #1 + the
  operator-visible aspect of Gap #5 (live phase/cost/route during
  typing). 26 tests.

* **Slice 2** — `op_block_buffer.py` (~530 LOC): per-op buffered
  rendering substrate. Frozen ``OpBlock`` dataclass + closed 3-value
  ``OpBlockState`` (BUFFERING/COMMITTED/EXPANDED). Thread-safe FIFO
  ring with monotonic ``o-N`` refs (NEVER reused — mirrors the
  ``BoundedBodyStore`` and ``DiffArchive`` safety contracts).
  Mutating API ``start_op`` / ``append`` / ``commit`` /
  ``mark_expanded`` / ``discard_active``. Active-index pruning on
  eviction (so concurrent appends never silently misroute).
  Bound: ``JARVIS_OP_BLOCK_BUFFER_SIZE`` (default 50). 42 tests.

* **Slice 3** — Wire buffer into `_op_line` + lifecycle hooks +
  unified ``/expand`` REPL verb (~280 LOC across serpent_flow.py).
  Master-flag-gated parallel buffer record (existing console.print
  paths preserved). Lifecycle hooks: ``op_started`` →
  ``buffer.start_op``, ``_op_line`` → ``buffer.append``,
  ``op_completed`` / ``op_failed`` → ``buffer.commit`` with
  collapsed summary. **Unified ``/expand <ref>`` REPL verb**
  dispatches by prefix:
    * ``t-N`` → :class:`BoundedBodyStore` (Gap #2)
    * ``d-N`` → :class:`DiffArchive` (Gap #4)
    * ``o-N`` → :class:`OpBlockBuffer` (this arc)
    * ``<op-id>`` (no prefix) → most-recent ``o-N`` for that op
  9 tests + AST regression pins for the wiring.

* **Slice 4** — Integration tests + edge cases (12 tests):
  no-TTY fallback (byte-identical legacy behavior), concurrent
  ops via real threading, eviction-with-active-index correctness
  (concurrent appends after eviction must safely fail rather
  than misroute), ``discard_active`` cleanup for cancelled ops,
  master-flag interlocks (both off → full legacy passthrough).

* **Slice 5** — Graduation. Master flags
  ``JARVIS_LIVE_STATUS_LINE_ENABLED`` + ``JARVIS_OP_COLLAPSE_ENABLED``
  flipped default-TRUE. Module-owned ``register_flags(registry) -> 3``
  + ``register_shipped_invariants() -> 4 ShippedCodeInvariant``
  pinning:
    1. **status_line_callable_wired_into_prompt_async** (BUG-FIX
       REGRESSION PIN — without it, Gap #1 silently regresses)
    2. **op_block_state_taxonomy_frozen** (closed 3-value enum)
    3. **serpent_flow_op_lifecycle_buffer_hooks** (op_started /
       _op_line / op_completed / op_failed all call their
       _maybe_buffer_* helpers)
    4. **handle_expand_dispatches_three_prefixes** (t-/d-/o- routing)
  19 graduation tests including end-to-end production-seed-boot
  resolution + AST-pin synthetic-positive coverage.

## Numbers

* **108 / 108 green** across 5 slices on first integrated run
* **0 regressions** in adjacent code
* ~1,250 LOC substrate + ~1,400 LOC tests
* 3 FlagSpec seeds; 4 ShippedCodeInvariant pins; 1 memory file

## Architectural properties

* **Zero new rendering surface** — reuses the ``StatusLineBuilder``
  fully implemented at status_line.py and registered by harness.py.
  Slice 1 is consumer-only.
* **Non-disruptive parallel buffering** — the existing
  ``console.print`` flow is unchanged; the buffer hook is a
  side-effect that the master flag gates. Operators who flip the
  flag off get byte-identical legacy behavior.
* **No silent ref reuse** — ``OpBlockBuffer``'s monotonic counter
  never resets, even after eviction or clear (mirrors Gap #2 +
  Gap #4 contracts).
* **Active-index pruning** — when a still-BUFFERING block is
  evicted, the active-index entry is also pruned so concurrent
  appends after eviction safely fail rather than misroute.
* **Unified /expand verb** — single REPL command dispatches across
  three artifact substrates by ref prefix. No three separate
  verbs / no per-prefix flag.

## Why Gap #5 was a phantom (re-diagnosis log)

The original audit asserted "REPL is blocking, no live background
updates — while operator types, no new op completions, cost ticks,
or phase transitions are visible." Reading the actual REPL loop
(``serpent_flow.py:4163``) showed:

  * ``with patch_stdout(raw=True):`` wraps the prompt_async call
  * Concurrent ``Console.print`` calls from ANY task are redirected
    to interleave above the prompt
  * Operators DO see ops complete + phase transitions during typing

What operators actually noticed was the **missing status line**
(phase / cost / route stayed invisible). That's Gap #1. So Gap #5
collapses into Gap #1 — the live-status-line wiring closes both.

A genuinely-async-blocking REPL would require migration from
``PromptSession.prompt_async`` to a full ``Application`` + ``Layout``.
That's a separate, much larger refactor that this arc deliberately
does NOT undertake — the operator UX problem is solved without it.

## Reused architectural assets

| Existing asset | Reuse |
|---|---|
| `StatusLineBuilder` + `StatusSnapshot` | Whole rendering machinery — Slice 1 just plumbs it into bottom_toolbar |
| `harness.py:1389` builder registration | No changes needed — already wires CostTracker/IdleWatchdog/GovernedLoopService into the builder |
| `_bottom_toolbar` callable | Wrapped via `make_bottom_toolbar_callable` (preserves swarm digest, adds status segment) |
| `BoundedBodyStore` (Gap #2 Slice 3) | Same monotonic-ref ring design language |
| `DiffArchive` (Gap #4 Slice 1) | Same terminal-frozen state semantics |
| `_FLAG_PROVIDER_PACKAGES` discovery | Module-owned `register_flags()` auto-discovered |
| `_INVARIANT_PROVIDER_PACKAGES` discovery | Module-owned `register_shipped_invariants()` auto-discovered |
| `patch_stdout(raw=True)` | Already in use — Gap #5 was a phantom because of this |

## Why-nots (deliberately deferred)

* **Full prompt_toolkit Application+Layout migration** — would be a
  much larger refactor; the operator UX problem is solved without it
  via the live status line.
* **Visual collapse glyph in op headers** — current design just
  buffers + commits with summary line; rendering a clickable ▶/▼
  glyph requires Rich Live integration that would conflict with
  the deliberate UI-Slice-3 architectural choice.
* **Per-op `/expand` notification when buffer commits** — would add
  log noise; operators discover via ``/expand`` (no args).
