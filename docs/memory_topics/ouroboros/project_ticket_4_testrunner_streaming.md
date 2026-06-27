---
title: Ticket #4 Slice 3 — TestRunner streaming migration (2026-04-20)
modules: [test_runner.py, backend/core/ouroboros/governance/background_monitor.py, backend/core/ouroboros/governance/monitor_tool.py]
status: historical
source: project_ticket_4_testrunner_streaming.md
---

# Ticket #4 Slice 3 — TestRunner streaming migration (2026-04-20)

Closes Slice 3 of the CC-parity observability arc. Slice 1 shipped the
BackgroundMonitor primitive; Slice 2 wired the Venom monitor tool
(deny-by-default, for the model's use). Slice 3 migrates TestRunner —
which is INFRA, not a model-facing tool — to consume the primitive
DIRECTLY for line-granular live feedback during long pytest runs.

## Authorization-bar honored

1. **JARVIS_TEST_RUNNER_STREAMING_ENABLED default false**; dual-path
   comparison via integration tests + optional runtime parity mode.
2. **Per-test-line feedback** into the existing reporting loop:
   grep-stable `[TestRunner] streaming <kind> node=X sequence=N` INFO
   lines + optional `event_callback` ctor kwarg with documented
   payload shape.
3. **Optional early-exit on first failure** behind explicit config
   (`JARVIS_TEST_RUNNER_EARLY_EXIT_ON_FAIL` default false). Default
   preserves legacy "run everything" semantics.
4. **No default flips** for JARVIS_TOOL_MONITOR_ENABLED or the new
   streaming flag — Slice 4 owns graduation.
5. **Diff scoped** to test_runner.py + new streaming test file. No
   new agent tools, no orchestrator edits, no manifest changes.

## Isolation discipline

TestRunner imports `BackgroundMonitor` from `background_monitor.py`
DIRECTLY — NOT `run_monitor_tool` from `monitor_tool.py`. Pinned by
two invariant tests:

- `test_test_runner_does_not_import_monitor_tool` — greps test_runner.py
  for `monitor_tool` / `run_monitor_tool`. Fails loudly if anyone
  accidentally routes TestRunner through the Venom tool surface.
- `test_test_runner_imports_background_monitor_primitive` — positive
  assertion that the primitive import IS present.

Rationale: TestRunner runs at boot / L2 repair / CI without a
PolicyContext. Forcing it through GoverningToolPolicy would be
ceremony without value and would blur §1 (deterministic execution
authority for infra) with §8 (observability where the model is
allowed to see subprocess output).

## Env knobs (all default off)

- `JARVIS_TEST_RUNNER_STREAMING_ENABLED` — master switch
- `JARVIS_TEST_RUNNER_EARLY_EXIT_ON_FAIL` — opt-in early-exit
- `JARVIS_TEST_RUNNER_PARITY_MODE` — operator safety net, runs both
  paths live and WARNs on divergence (doubles pytest cost)

## What changed in test_runner.py

1. Added three env helpers + `_PYTEST_EVENT_RE` regex constant.
2. Added `Callable` import + optional `event_callback` ctor kwarg
   (default None — existing callers unaffected).
3. Split `_run_pytest` into a branching path:
   - Streaming on → `_exec_with_streaming` + `-v` pytest verbosity
   - Streaming off → legacy `_exec_with_timeout` + `-q` (unchanged)
4. Added `_exec_with_streaming` — identical return shape to
   `_exec_with_timeout`; uses `BackgroundMonitor` for line streaming,
   parses pytest events via regex, honors optional early-exit.
5. Added `_compare_paths_loudly` — runtime parity check (only when
   both streaming + parity_mode on). Emits `[TestRunner]
   parity_ok` OR `[TestRunner] parity_divergence fields=...` WARN.

## Structural parity — the headline invariant

`TestResult` fields `{passed, total, failed, failed_tests,
flake_suspected}` are **IDENTICAL across paths** on representative
fixtures. Integration tests run both paths sequentially and
compare. Divergence FAILS LOUDLY with a structured message. Only
`stdout` differs — diagnostic formatting only, NOT a
parity-tested field (legacy uses `-q`, streaming uses `-v`; JSON
report is authoritative for structural fields).

## Event contract (documented in code + pinned by tests)

Per-test INFO log:

    [TestRunner] streaming test_passed node=tests/foo.py::test_bar sequence=42
    [TestRunner] streaming test_failed node=tests/foo.py::test_baz sequence=43
    [TestRunner] streaming early_exit_triggered status=FAILED node=...
    [TestRunner] streaming completed total=5 passed=3 failed=2 early_exit=True returncode=1

Operators grep these in debug.log for live TestRunner progress.
Stable contract — `test_pytest_event_regex_matches_expected_lines`
pins the parser.

Optional programmatic consumer:

    runner = TestRunner(
        repo_root=..., timeout=...,
        event_callback=my_callable,  # called per test event
    )

Payload shape: `{kind, node_id, ts_mono, sequence, raw_line}`.
`kind` ∈ {"test_passed", "test_failed", "test_errored",
"test_skipped"}. Callback exceptions caught + logged at DEBUG —
buggy consumers cannot break the runner.

## Regression spine — 18 tests green

Feature gates (5): streaming default false, early-exit default
false, parity-mode default false, case-insensitive env parsing,
regex catches all four pytest statuses.

Structural parity (2 CRITICAL): all-passing fixture + mixed
pass/fail fixture — legacy and streaming TestResult byte-equal
on structural fields.

Streaming happy path + events (4): valid TestResult, event_callback
fires per test, callback exception caught, callback=None legacy
compat.

Early-exit semantics (2): default runs everything, opt-in stops
on first failure.

Timeout + sandbox (2): timeout enforced, sandbox_dir respected.

Isolation discipline (2 CRITICAL): no import of monitor_tool,
positive import of background_monitor primitive.

Parity mode runtime (1): emits parity_ok / parity_divergence log.

## Combined Ticket #4 status — 92/92 green

- Slice 1 (BackgroundMonitor primitive) — 21 tests
- Slice 2 (Venom monitor tool) — 30 tests
- Slice 3 (TestRunner streaming) — 18 tests
- Legacy TestRunner — 23 tests (unchanged, still green)

## What Slice 4 owns

- Flip `JARVIS_TEST_RUNNER_STREAMING_ENABLED` default false → true
  after 3 clean sessions + live-fire proof (long-running test run
  with partial feedback visible in SerpentFlow).
- Flip `JARVIS_TOOL_MONITOR_ENABLED` default false → true after
  operator battle-test hours at opt-in level.
- SerpentFlow live dashboard surface for event callback — the
  logger + callback contracts ARE already in place; SerpentFlow
  just subscribes.
- Orchestrator-level wiring if anyone wants per-test-line feedback
  in the GENERATE loop rather than just debug.log (optional).

## What stays out of scope for Slice 3

- Default flips (Slice 4).
- TrinityEventBus wiring — TestRunner passes `event_bus=None` to
  BackgroundMonitor. Future slice can thread a bus ref through
  if needed; not required for parity.
- Changes to any caller site (Orchestrator / LanguageRouter /
  PythonAdapter / SubagentScheduler). All 5 callers continue using
  the same TestRunner API with no kwargs changes.
