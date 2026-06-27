---
title: Ticket #4 Slice 4 — two-flag graduation (2026-04-20)
modules: [tests/governance/test_ticket4_slice4_graduation.py, backend/core/ouroboros/governance/monitor_tool.py, backend/core/ouroboros/governance/background_monitor.py, test_runner.py]
status: merged
source: project_ticket_4_slice4_graduation.md
---

# Ticket #4 Slice 4 — two-flag graduation (2026-04-20)

Closes Ticket #4 — CC-parity for stdout event streaming over the
entire ship: primitive (Slice 1) + Venom tool (Slice 2) + TestRunner
migration (Slice 3) + graduation (Slice 4).

## Flipped defaults

- `JARVIS_TOOL_MONITOR_ENABLED`: `false` → **`true`**
  The Venom `monitor` tool is now enabled by default. Slice 2's
  per-call binary-allowlist + timeout ceiling + argv-only +
  structural arg validation all remain in force — graduation flips
  the opt-in requirement on the model's access, NOT the safeguards.
- `JARVIS_TEST_RUNNER_STREAMING_ENABLED`: `false` → **`true`**
  TestRunner's streaming path (consumes `BackgroundMonitor`
  directly) runs by default. Legacy `_exec_with_timeout` available
  via explicit `"false"` opt-out. Structural TestResult parity
  (`passed`/`total`/`failed`/`failed_tests`) still enforced by
  Slice 3's integration tests.

Both flips preserve the Slice 3-era invariants:
- `JARVIS_TEST_RUNNER_EARLY_EXIT_ON_FAIL` unchanged (default
  `false`) — legacy "run everything" semantics preserved.
- `JARVIS_TEST_RUNNER_PARITY_MODE` unchanged (default `false`) —
  still opt-in because it doubles pytest cost.

## Full-revert matrix (operator contract)

Documented by `test_4h_full_revert_matrix` — four combinations:

| monitor env | streaming env | monitor_state | streaming_state |
|---|---|---|---|
| (unset) | (unset) | **True** | **True** |
| `false` | (unset) | False | **True** |
| (unset) | `false` | **True** | False |
| `false` | `false` | False | False |

Flags are independent — flipping one does NOT affect the other.

## Invariant pins that survive graduation

Slice 4 shipped a dedicated test module
`tests/governance/test_ticket4_slice4_graduation.py` with 17 tests
encoding the contract. Beyond the graduation pins + opt-out pins,
the module enforces:

### Authority invariants
- `test_4i_monitor_still_read_only_post_graduation` — manifest
  capabilities still `{"subprocess"}` (NOT `"write"`), tool NOT in
  `_MUTATION_TOOLS`. Graduation didn't escalate authority.
- `test_4l_monitor_tool_does_not_import_orchestrator_gates` —
  monitor_tool.py still doesn't import iron_gate /
  risk_tier_floor / semantic_guardian. Observability stays
  observability.

### Structural safeguards
- `test_4j_monitor_still_requires_allowlist_post_graduation` —
  binary-allowlist gate STILL fires. Setting a tight allowlist
  (e.g., `"pytest"` only) still denies `/bin/sh`.
- `test_4m_policy_still_validates_cmd_shape_post_graduation` —
  bad_args deny-reason still fires on malformed cmd.
- `test_4n_default_allowlist_still_includes_pytest_family` — the
  default allowlist (pytest/python/node/npm/go/cargo/make) is
  preserved; graduation didn't silently broaden the default.

### Isolation discipline
- `test_4k_test_runner_still_does_not_import_monitor_tool` — the
  Slice 3 isolation boundary HOLDS. TestRunner stays infra.
- `test_4q_ticket_4_surface_still_isolated_post_graduation` —
  greps all three modules (primitive + tool + test_runner) for
  the expected layered-dependency shape:
    - background_monitor.py imports NEITHER consumer
    - monitor_tool.py imports background_monitor ONLY
    - test_runner.py imports background_monitor ONLY (NOT
      monitor_tool)

### Documentation (bit-rot guards)
- `test_4o_monitor_enabled_docstring_documents_graduation` +
  `test_4p_streaming_enabled_docstring_documents_graduation` —
  both env helpers' docstrings carry the graduation language
  ("graduated" + "true" + "false"/"opt"/"legacy"). Future refactors
  that strip the documentation fail loudly.

## Regression spine

17 new tests in the graduation module. Combined with existing
suites, **Ticket #4 total: 111/111 green**:

| Slice | Surface | Tests |
|---|---|---|
| 1 | BackgroundMonitor primitive | 21 |
| 2 | Venom monitor tool | 30 |
| Legacy | TestRunner (pre-streaming) | 23 |
| 3 | TestRunner streaming | 18 |
| 4 | Graduation pins | **17** |
| **Total** | | **111** |

Two existing tests were renamed + reframed during Slice 4 (they
encoded the old defaults):
- `test_policy_denies_when_master_switch_off` →
  `test_policy_denies_when_master_switch_explicitly_off` (now
  asserts `=false` → DENY, not "absent → DENY")
- `test_monitor_enabled_default_is_false` →
  `test_monitor_enabled_default_post_graduation_is_true` + new
  `test_monitor_enabled_explicit_false_opts_out`
- `test_streaming_disabled_by_default` →
  `test_streaming_default_post_graduation_is_true` + new
  `test_streaming_explicit_false_opts_out`

## What Slice 4 intentionally did NOT do

- No SerpentFlow live-dashboard surface. The logger + callback
  contracts are shipped in Slices 2/3; SerpentFlow subscription
  is a separate piece of work.
- No TrinityEventBus wiring. TestRunner still passes
  `event_bus=None` to BackgroundMonitor; threading a bus ref
  through is a future slice.
- No caller-site changes (Orchestrator / LanguageRouter /
  PythonAdapter / SubagentScheduler). All five continue using
  the same TestRunner / monitor APIs unchanged.

## What closing Ticket #4 means operationally

- **Default behavior on a fresh install**: the model CAN call the
  `monitor` Venom tool (read-only, binary-allowlisted); TestRunner
  runs the streaming path. Operators see per-test feedback in
  `[TestRunner] streaming ...` INFO log lines during long pytest
  runs.
- **Kill switch**: set both env vars to `"false"` for full revert
  to pre-Slice-2 behavior. Either one alone reverts only its side.
- **Still deterministic**: TestRunner is still infra — no
  PolicyContext, no model-facing authority surface. Slice 4
  graduation does NOT change that.
- **Manifesto §1 boundary held**: the Venom tool gained default
  availability to the model but NO new authority shape. Manifesto
  §8 observability: streaming stdout events are visible at
  INFO-log + optional programmatic callback; NOT via a gated
  audit-trail-carrying surface.

Ticket #4 closed.
