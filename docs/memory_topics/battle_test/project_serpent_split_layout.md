---
title: Project Serpent Split Layout
modules: [scripts/livefire_serpent_layout.py, backend/core/ouroboros/battle_test/layout_controller.py, backend/core/ouroboros/battle_test/split_layout.py, backend/core/ouroboros/battle_test/layout_repl.py, backend/core/ouroboros/battle_test/serpent_flow_app.py, tests/governance/test_layout_controller.py, tests/governance/test_split_layout.py, tests/governance/test_layout_repl.py, tests/governance/test_serpent_flow_app.py, tests/governance/test_layout_graduation.py, backend/core/ouroboros/battle_test/serpent_flow.py, live_dashboard.py]
status: historical
source: project_serpent_split_layout.md
---

SerpentFlow Opt-in Split Layout — CLOSED 2026-04-21 (5-slice arc).
Closes the "No multi-monitor layout for SerpentFlow" CC-parity gap
while honoring the prior feedback that Derek's TUI preference is
**flowing by default, not pinned dashboards**.

**Product decision recorded (Derek, 2026-04-21):** Option 2 — opt-in
split, default remains flowing SerpentFlow. "Deterministic human
authority over presentation (§1), not model-driven UI."

**What shipped:**
- Slice 1: `layout_controller.py` — state machine (`flow`/`split`/`focus:<region>`),
  env default `JARVIS_SERPENT_LAYOUT_DEFAULT` (default `flow`),
  `--split`/`--flow`/`--layout=<mode>` CLI arg parser, listener hooks,
  singleton. Schema `serpent_layout.v1`. 56 tests.
- Slice 2: `split_layout.py` — Rich Layout wrapper with 3 named regions
  (`stream`/`dashboard`/`diff`). **Lazy Rich import**; **TTY detection**
  → headless/sandbox returns False without raising. Bounded per-region
  buffers (env-tunable, floor=10, default=500). `push(region, text)` /
  `snapshot()` / `start()` / `stop()` API. Schema `serpent_split_layout.v1`.
  24 tests.
- Slice 3: `layout_repl.py` — `/layout` dispatcher with `flow`/`split`/
  `focus <region>`/`help` verbs. Single-verb escape (`/layout flow`) from
  every mode. Malformed region rejected with stable error. 21 tests.
- Slice 4: `serpent_flow_app.py` — thin adapter composing controller +
  renderer. **Zero-change flow default**: emits route to injected
  `stream_writer` (callers pass their existing `Console.print`) —
  split buffers stay empty in flow mode (pinned test). Split/focus
  routes emits to region buffers. `SerpentFlowApp.from_argv()` factory.
  Schema `serpent_flow_app.v1`. 22 tests.
- Slice 5: `test_layout_graduation.py` (29 pins: authority grep, schema
  versions, flow-default, flow-mode behavioral equivalence, TTY fallback,
  REPL verb matrix, escape-from-every-mode, CLI precedence) +
  `scripts/livefire_serpent_layout.py` (10 scenarios, 32 checks).

**Schema versions pinned:** `serpent_layout.v1`, `serpent_split_layout.v1`,
`serpent_flow_app.v1`.

**§1 invariant (grep-enforced):** All 4 new modules import zero of
orchestrator / policy_engine / iron_gate / risk_tier_floor /
semantic_guardian / tool_executor / candidate_generator /
change_engine. Additionally pinned: no imports of `tool_executor` /
`mcp_tool_client` / `tool_registry` — layout is **never** a model-callable
surface. Operator-driven only.

**Why:** CC-parity gap flagged "no split pane". Derek's decision: split
is valuable **as an opt-in**, not a new default. Default posture remains
flowing SerpentFlow (honors `feedback_tui_design.md`). Adds `/layout`
REPL + `--split` CLI for operators who want density when they want it.

**How to apply:** Additive. `serpent_flow.py` (1,900 lines) + `live_dashboard.py`
(1,233 lines) untouched. `SerpentFlowApp` is a new composition adjacent
to existing SerpentFlow — callers migrate opt-in when they want split
support. No env flag graduation needed — default `flow` honored by state
machine; split/focus only engage when operator asks.

**Non-goals (explicit):**
- Model-controlled layout (§1 forbids)
- Multi-monitor assumptions (single-process TUI only)
- Replacing IDE observability (Gap #6 stays the path for deep inspection)

**Landmines resolved:**
- Rich must be lazy-imported — importing `split_layout.py` at Python load
  time never touches Rich; first `start()` call loads it.
- Headless safety — non-TTY `output_stream` (io.StringIO in tests) causes
  `start()` to return False. The push buffer still works — just no render.
- Flow mode must NEVER touch split buffers (pinned by
  `test_flow_mode_does_not_populate_split_buffers` — guards against future
  leaks that would change default UX).
- Single-keystroke escape — `/layout flow` returns from every starting
  mode including `focus:<r>` (parametrized pin).
- CLI precedence: `--split`/`--flow`/`--layout=<m>` > env var > `flow`.
  Invalid CLI value falls through to env; invalid env value falls through
  to `flow` (never wedges the controller).
- Controller lives separately from SplitLayout so tests can construct a
  controller without touching Rich.
- `valid_regions()` order is stable (`stream`, `dashboard`, `diff`) —
  pinned at graduation.

**Files shipped (new):**
- `backend/core/ouroboros/battle_test/layout_controller.py`
- `backend/core/ouroboros/battle_test/split_layout.py`
- `backend/core/ouroboros/battle_test/layout_repl.py`
- `backend/core/ouroboros/battle_test/serpent_flow_app.py`
- `tests/governance/test_layout_controller.py`
- `tests/governance/test_split_layout.py`
- `tests/governance/test_layout_repl.py`
- `tests/governance/test_serpent_flow_app.py`
- `tests/governance/test_layout_graduation.py`
- `scripts/livefire_serpent_layout.py`

**Test tally:** 152 arc tests green; 32 live-fire checks across 10
scenarios; zero modifications to existing SerpentFlow/LiveDashboard.

**Memory update needed:** `feedback_tui_design.md` guidance still holds
("flowing by default") — but the posture is now "flowing default WITH
operator-owned escape hatch to split" rather than "only flowing ever".
