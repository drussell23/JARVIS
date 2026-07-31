---
title: Palette alignment — one description column, like Claude Code
modules: [backend/core/ouroboros/battle_test/palette_render.py, backend/core/ouroboros/battle_test/bipartite_layout.py]
status: active
source: session 2026-07-31, operator screenshot
---

# Palette uniform alignment

## Report

The `/` menu's descriptions did not line up: three rows aligned, the fourth
ragged. Operator wants Claude Code's layout.

## It was working as designed

`palette_render.py` already implements CC's layout — full terminal width,
fixed gutter, hanging-indent wrapping — and its docstring says so. The
cockpit mounts it (`bipartite_layout` line ~1908); pt's `CompletionsMenu` is
only the import-failure fallback. **My first read of the mount was wrong and
I corrected it before changing anything.**

The deviation was one default. `_NAME_FIT_QUANTILE = 0.8` sized the name
column to fit 80% of visible names so an outlier could not dictate the
layout — with `/backlog_auto_proposed` (22) on screen, `/anticipate` (11)
would otherwise get fourteen spaces of dead gutter. Real concern.

## Root cause: a redundant second mechanism

`_NAME_COL_MAX_FRACTION` (0.34 of width) + `_ellipsis` ALREADY bound that
case. Verified: a 60-char verb at width 80 renders
`/xxxxxxxxxxxxxxxxxxxxxxxxx…` with every description still aligned.

So the quantile guarded a case the cap already handled, and charged the
alignment for it. Default → **1.0**. The knob
(`JARVIS_PALETTE_NAME_QUANTILE`) stays, because a dense-row preference is
legitimate; it is just not what CC does or what was asked for.

Before → after (width 120):
```
/anticipate      help …          /anticipate              help …
/autobiography   Retrospective…  /autobiography           Retrospective…
/backlog_auto_proposed   Review… /backlog_auto_proposed   Review…
/breadcrumbs     Set/show…       /breadcrumbs             Set/show…
```

## Second defect found: one env var, two defaults

`JARVIS_PALETTE_HEIGHT` was read in `palette_render.palette_rows()` with
default **4** and in `bipartite_layout._palette_height()` with default
**12** — so with the var unset the two renderers disagreed by 3×, and which
height an operator got depended on which one mounted. `bipartite_layout` now
delegates; `palette_render` owns the knob. Shared default 4 → **10** (four
rows showed less than a third of a screen that had room).

## Test note

`test_palette_spacing.py` encoded the OLD contract and was rewritten, not
deleted — every still-true assertion kept (gutter present, wrapping occurs,
cap engages, knob tunable). One of its tests asserted a specific WRAP POINT
("next"/"history" on line two), so widening the column by 8 chars read as
"wrapping stopped working" when only the sentence break had moved. Rewritten
to assert wrapping by SHAPE.

21 tests; 447 green across palette/completion/bipartite. ⚠️ Not confirmed by
the operator in a real terminal.

## Slice 2 — adaptive fluid breakpoints (width starvation)

Strict alignment introduced a geometric failure: on a narrow terminal the
description column collapses and every entry becomes a tower one or two words
wide. The table stops conveying anything exactly when there is least room.

**The snap is derived from the DESCRIPTION column, not the terminal width.**
A fixed "stack below 60" measures the wrong quantity, and two cases prove it:

- width **61** with a 40-char verb → two-column leaves ~15 columns of
  description. That is the tower a breakpoint exists to prevent, and a
  column-count rule sails past it.
- width **59** with 8-char verbs → ample room, and the same rule stacks for
  no reason.

`stacked_mode(width, name_col)` computes
`width - name_col - _GUTTER - 2 < min_desc_col()`, with
`absolute_stack_floor()` (56) kept as a backstop for degenerate terminals and
because "when does it stack?" deserves an answer that does not require
knowing the verb table. Both env-tunable.

**Only the yield sequence changes.** Same `_ellipsis`, same `_wrap`, same
style classes — a second layout engine would be a second place for theme and
truncation to drift, and the two would disagree first on the narrow terminal
nobody tests on.

**`_rendered_height` learned the mode.** A stacked entry costs
`1 + description lines`; a budget that assumed two-column would overrun by
one line per entry on exactly the narrow terminal with least slack.

**Stacked names use the FULL width** — with no description sharing the row,
the 34% fraction cap would be clipping to protect a column that no longer
exists. Still ellipsised against the terminal, so a 200-char verb at width 30
cannot overflow.

Evaluated per call, so a SIGWINCH re-decides on the next frame: the function
is pure and the cockpit calls it every render.

31 tests; parametrised no-overflow across widths 200→12. 478 green across
palette/completion/bipartite.
