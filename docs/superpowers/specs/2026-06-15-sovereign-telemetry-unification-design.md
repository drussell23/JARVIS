# Sovereign Telemetry Unification — Live-Fire Graduation Matrix

**Date**: 2026-06-15
**Author**: Derek J. Russell (design dialogue + agent execution)
**Status**: IMPLEMENTED 2026-06-15 (uncommitted on `main` working tree) — 27 new tests + 297 affected-suite tests green
**Goal**: `scripts/live_fire_graduation_soak.py` per PRD §P9.1 — reuse `telemetry_harvester.py` for the parse, with a `GraduationContract` per flag. Solve the root problem (duplicated parse + trajectory-blind contracts); no workarounds, no hardcoding.

---

## 1. Problem statement

The Live-Fire Graduation Soak system already exists and is mature:

- `scripts/live_fire_graduation_soak.py` — thin CLI dispatcher (PRD §P9.1).
- `backend/.../graduation/live_fire_soak.py` (1564 LOC) — the substrate harness: forks `ouroboros_battle_test.py`, parses `summary.json` + an **8KB tail** of `debug.log`, classifies the session via `classify_outcome` (clean/runner/infra/migration), refines via per-flag `GraduationContract`, persists an `EvidenceRow`.
- `backend/.../graduation/graduation_contract.py` — P9.2 per-flag `GraduationContract` (summary-only clean predicates).

Two root defects, both surfaced by the goal directive:

1. **Duplicated parse.** The user's brand-new `scripts/telemetry_harvester.py` (Slice 256) extracts a far richer signal — the *self-heal trajectory* (`livefire_fired` → `routed_build` → `retried` → `recovered`), Metric A/B/C, and hardware anomalies (`oom`, `gate_inert`, `livefire_timeout`) — from the **full** `debug.log`. The soak substrate has its own thin `classify_outcome` parse and **never reuses the harvester**. Two parse paths = drift risk.
2. **Trajectory-blind contracts.** `GraduationContract.clean_predicate` only ever receives `summary.json`. Per-flag contracts therefore *cannot* express criteria like "this flag is CLEAN only if the system autonomously recovered AND did not OOM" — exactly the dynamic/adaptive criteria the goal demands.

## 2. Architecture — three units

### Unit A — `telemetry_parse.py` (pure extraction, zero duplication)

New module `backend/core/ouroboros/governance/graduation/telemetry_parse.py`. Stdlib-only, pure, never-raises. Houses the **pure parse** lifted verbatim from the harvester:

- `Metrics` dataclass (Metric A/B/C fields).
- `parse_metrics(log_text, summary, deployer_stdout="") -> Metrics`.
- The grounded regexes (`_RE_LIVEFIRE_FAIL`, `_RE_FAILCLASS_BUILD`, `_RE_RETRY`, `_RE_RECOVERED`, `_RE_OOM`, …).

`scripts/telemetry_harvester.py` is refactored to **import** `Metrics` + `parse_metrics` from this module and keep its own concerns (`certify`, `render_report`, async watcher, CLI) — **byte-identical CLI behavior**. The harvester remains "the user's file"; only the pure parse relocates to a properly importable backend home. The verdict layer (`certify`) stays in the script because graduation classification is a *different concern* from deployment self-heal certification.

### Unit B — Arbiter + dual-signature predicates (`graduation_contract.py`)

The `GraduationContract` becomes the **Sovereign Arbiter** over two independent assessment streams:

- **Legacy stream**: `classify_outcome(summary, tail)` — authoritative for the base tree, migration heuristics, shutdown-noise handling (unchanged; no regression).
- **Telemetry stream**: `parse_metrics(full_log, summary)` → `Metrics`.

**Dual-signature predicate.** `clean_predicate` may be a legacy `(summary) -> bool` OR a new `(summary, metrics) -> bool`. Dispatch by `inspect.signature` positional-arity (≥2 positional params → metrics-aware), falling through safely. `metrics` is duck-typed (`Any`) inside `graduation_contract.py` to keep the module's stdlib-only/AST-pinned posture and avoid an import cycle. Old summary-only predicates keep working unchanged.

**Arbiter priority matrix** (deterministic, pure function `arbitrate_outcome(...)`):

| Priority | Rule | Effect |
|----------|------|--------|
| 1 (highest) | **Anomaly guard**: `metrics.oom` or `metrics.gate_inert` | force NOT-clean (clean→`infra` waiver) — hardware/wiring invariant violated |
| 2 | **Autonomous-recovery override**: legacy ∈ {`infra`,`runner`} AND `livefire_fired` AND `routed_build` AND `retried` AND `recovered` AND NOT `oom` | override → `clean` (the system *caught + healed*; "Intelligent Autonomous Recovery > Legacy Static Errors") |
| 3 | **Metrics-aware predicate downgrade**: outcome is `clean` AND contract predicate (summary, metrics) returns False | `clean` → `runner` |
| 4 (lowest) | **Legacy blocklist override** (existing): RUNNER → INFRA waiver | unchanged |

Each transition appends a structured note (`arbiter_anomaly_oom`, `arbiter_recovery_override`, `contract_metrics_predicate_downgraded`, …) for §7 absolute observability.

### Unit C — substrate wiring + Unified Evidence Row (`live_fire_soak.py`)

- **Full-log capture**: replace the hardcoded `text[-8192:]` with an env-tunable bounded read `JARVIS_LIVE_FIRE_DEBUG_LOG_CAPTURE_BYTES` (default generous, e.g. 1 MiB; bounded for memory safety, no hardcoding). Trajectory markers are sparse; this captures them without unbounded reads. The injectable-runner signature `(exit_code, summary, debug_text)` is **unchanged** — the third element is simply a larger bounded chunk.
- **Gated telemetry ingestion**: new master flag `JARVIS_LIVE_FIRE_TELEMETRY_ARBITER_ENABLED` (default **false** → byte-identical rollback). When on, `run_soak` calls `parse_metrics(debug_text, summary)` and threads `Metrics` into `_maybe_apply_contract` → `arbitrate_outcome`.
- **Unified Evidence Row**: add one additive optional field `telemetry: Optional[Dict]` on `EvidenceRow`, serialized only when present (mirrors the `runner_attributed_kind` pattern). It wraps the legacy classification inside the harvester trajectory context: `{livefire_fired, routed_build, retried, recovered, oom, gate_inert, arbiter_override, arbiter_reason}`.

### Capstone Dogfood Contract

A new built-in `GraduationContract` for **`JARVIS_LIVE_KERNEL_VALIDATOR_ENABLED`** (a real flag — gates the `LiveKernelValidator` live-fire boot check). Predicate `predicate_requires_live_kernel_validation(summary, metrics)` demands a higher evidence bar: CLEAN per default **AND** `metrics.livefire_fired` (live-fire test executed) **AND NOT** `metrics.oom` (zero OOM) **AND** `metrics.recovered` (candidate successfully processed). This closes the loop: the validator's own graduation requires harvester-proven evidence that the validator fired and healed.

## 3. Gating & rollback discipline

- `JARVIS_LIVE_FIRE_TELEMETRY_ARBITER_ENABLED` (default false) — gates *all* new behavior in Unit C. Off ⇒ no `parse_metrics` call, no arbiter, no telemetry field ⇒ byte-identical to today.
- `JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT` (existing, default false) — still gates contract consultation; arbiter only runs when *both* are on (arbiter is a contract-layer refinement).
- New master flags registered in the FlagRegistry seed (no unregistered flags).

## 4. Testing (TDD)

- `tests/governance/test_telemetry_parse_extraction.py` — Unit A: extracted `parse_metrics` is behavior-identical to the harvester's original on fixture logs; harvester still imports + parses identically; round-trips the trajectory + anomaly fields.
- `tests/governance/test_graduation_arbiter.py` — Unit B: each priority-matrix row (anomaly guard, recovery override, metrics-predicate downgrade, blocklist), dual-signature dispatch (1-arg vs 2-arg predicate), never-raises on malformed metrics, determinism.
- `tests/governance/test_live_fire_telemetry_wiring.py` — Unit C: master-off byte-identical (no telemetry field, no parse call); master-on threads metrics → arbiter → EvidenceRow; full-log capture env knob bounds/defaults; capstone contract on `JARVIS_LIVE_KERNEL_VALIDATOR_ENABLED`.
- Existing `graduation_contract` + `live_fire_soak` suites stay green (master-off byte-identical guarantee).

## 5. Non-goals (YAGNI)

- No rewrite of `classify_outcome`'s base tree (augment, not replace).
- No change to the CLI subcommand surface of `live_fire_graduation_soak.py` (the script already satisfies §P9.1; this work deepens the substrate it dispatches to).
- No new producer wiring for Phase 8 ledgers (that's P9.5).
