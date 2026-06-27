---
title: Project Inline Prompt Gate Closure
modules: []
status: historical
source: project_inline_prompt_gate_closure.md
---

May 2, 2026: InlinePromptGate 5-slice arc closed same-day. Closes the operator-intervention granularity gap between O+V's autonomous loop and CC's human-supervised UX. With this shipped, O+V is structurally A across all 9 dimensions vs CC.

**Five slices shipped:**

1. **Slice 1 — Primitive bridge** (`inline_prompt_gate.py`, commit `2d63f545d5`): Pure-stdlib phase-boundary primitive. 5-value `PhaseInlineVerdict` closed taxonomy (ALLOW / DENY / PAUSE_OP / EXPIRED / DISABLED). Frozen `PhaseInlinePromptRequest` + `PhaseInlinePromptVerdict` dataclasses. Total `compute_phase_inline_verdict` mapping function. Deterministic `derive_prompt_id`. Truncation helpers + env-knob clamping. Phase C `MonotonicTighteningVerdict.PASSED` stamping outcome-aware (DENY/PAUSE_OP → "passed"; ALLOW/EXPIRED/DISABLED → empty). Controller STATE_* constants redefined verbatim with byte-parity test. 83 tests.

2. **Slice 2 — Async producer/bridge** (`inline_prompt_gate_runner.py`, commit `afe8e6937b`): `request_phase_inline_prompt()` async + `bridge_to_controller_request()` adapter + 4 sentinel constants (`PHASE_BOUNDARY_TOOL_SENTINEL="phase_boundary"`, `PHASE_BOUNDARY_RULE_ID="phase_boundary_inline_prompt"`, `PHASE_BOUNDARY_CALL_ID_PREFIX="pb-"`, `DEFAULT_REVIEWER="phase_boundary_producer"`). Bridges Slice 1 request → existing `InlinePromptController.request()` Future. Defensive degradation matrix: capacity/state-error/secondary-timeout → DISABLED; async cancellation propagates per asyncio convention. **ZERO new SSE wiring** — proved by integration test that existing `attach_controller_to_broker` listener fires `inline_prompt_*` events for phase-boundary prompts identically to per-tool-call prompts. 34 tests.

3. **Slice 3 — HTTP POST response surface** (`inline_prompt_gate_http.py`, commit `0a92dc3cc0`): `InlinePromptGateHTTPRouter` with 3 routes (GET list / GET detail / POST respond). Defense-in-depth flag split (`JARVIS_INLINE_PROMPT_GATE_HTTP_ENABLED` distinct from producer flag — operator can enable producer without exposing HTTP write authority). Per-IP sliding-window rate limit, body-size cap, CORS allowlist, closed verdict vocabulary `ACCEPTED_VERDICTS={allow, allow_always, deny, pause}`. Phase-boundary sentinel filter: per-tool-call prompts return `404 not_phase_boundary` even with knowledge of prompt_id (4 tests). Idempotent `409 already_terminal` path with current snapshot. **Separate from `ide_observability.py`** to preserve its AST-pinned read-only invariant — write authority lives in its own module. 39 tests.

4. **Slice 4 — Listener-based renderer** (`inline_prompt_gate_renderer.py`, commit `b0526a8c82`): Closes the operator-visibility gap. Existing `ConsoleInlineRenderer` is bound to `InlinePermissionMiddleware` (per-tool-call only); phase-boundary prompts had NO render path. Listener subscribes via `controller.on_transition()` (same mechanism as SSE bridge), filters by sentinel + rule_id fallback, renders distinct `[Phase Boundary]` header (vs `[InlinePrompt]` for tool-call). Pure formatters NEVER raise. attach() returns no-op unsub if controller resolution fails (renderer is operator-UX, not authority — must never block boot). 39 tests.

5. **Slice 5 — Graduation** (commit `37e1719458`): `JARVIS_INLINE_PROMPT_GATE_ENABLED` flipped default false→true. HTTP master flag stays default-false per Move 6 cost-ramp pattern. **8 IPG flags** + **9 IPG AST-pin invariants** registered via dynamic discovery (Priority #6 module-owned-registration contract). 152 total flags / 61 total invariants post-Slice-5. All 9 IPG invariants validate clean against main. 23 graduation tests covering master flag flip semantics, dynamic discovery, end-to-end composition (allow/deny/pause/timeout through full producer → controller → renderer stack), Phase C tightening stamp, master-off disables full stack, and reuse contract proving phase-boundary + per-tool-call coexist on singleton without renderer cross-talk.

**Architectural reuse spine — no duplication:**
- `InlinePromptController` singleton serves both phase-boundary and per-tool-call prompts; sentinel filter keeps scopes disjoint by construction
- `inline_permission_observability.attach_controller_to_broker` fires SSE events for phase-boundary prompts identically (zero new SSE wiring)
- `inline_permission_repl.dispatch_inline_command` `/allow /deny /pause /always` verbs work for phase-boundary prompts via shared singleton (zero new REPL verbs)
- Module-owned `register_flags(registry)` + `register_shipped_invariants()` discovered automatically — no edits to `flag_registry_seed.py` or `meta/shipped_code_invariants.py` required

**Sweep results:** 472/472 combined sweep across full IPG stack (Slices 1-5) + inline_permission stack (observability + prompt + memory + graduation) + Priority #1 Slice 5 graduation pin (canonical "all 61 invariants validate clean against main" test).

**Where O+V stands post Priority #4 + #5 + #6 + InlinePromptGate Slice 5:** A across the board structurally. Operator-intervention granularity gap CLOSED. The 9-dimensional comparison vs CC: structurally ahead in cross-session learning + counterfactual replay + temporal coherence audits + speculative branching + long-horizon drift + Antivenom v2 + dynamic registration + observability; peer in operator UX granularity (this slice). CC's only remaining advantage: the network effect of being a hosted product with Anthropic's distribution.

**Deferred to Slice 5b (one-line follow-up):** SerpentFlow boot wire-up: `self._unsub_inline_prompt_renderer = attach_phase_boundary_renderer(self._console.print)` in `SerpentFlow.__init__`. Renderer activates on attach; boot wiring lives separate from the renderer's primitive.

**Why:** The "biggest UX hole" identified post Priority #4/#5/#6 closure was operator-intervention granularity — the autonomous loop was observable and abortable but not steerable mid-op. CC's UX moat isn't observability; it's the operator's ability to redirect a decision before it commits. InlinePromptGate closes that gap by reusing the existing `InlinePromptController` substrate (already had Future-backed registry, 4 operator actions, timeout, listeners, capacity, history, singleton — but bound to per-tool-call prompts via the middleware). The arc was 70% existing-substrate-glue, 30% new code.

**How to apply:** Phase-boundary prompts now activate at NOTIFY_APPLY GATE→APPLY transitions when `JARVIS_INLINE_PROMPT_GATE_ENABLED=true` (default). Operators answer via existing REPL verbs (`/allow`, `/deny`, `/pause`) or — once `JARVIS_INLINE_PROMPT_GATE_HTTP_ENABLED=true` — via `POST /observability/inline_prompt/{prompt_id}/respond`. SerpentFlow renders prompts with `[Phase Boundary]` header. Verdict propagates back through `PhaseInlinePromptVerdict` with Phase C tightening stamp on operator-inserted friction (DENY / PAUSE_OP).

**Commits:** `2d63f545d5` (Slice 1) → `afe8e6937b` (Slice 2) → `0a92dc3cc0` (Slice 3) → `b0526a8c82` (Slice 4) → `37e1719458` (Slice 5).
