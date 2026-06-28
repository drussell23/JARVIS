# A1 Harness — Faithful Fast-Forward Fixture + Continuous GCS Telemetry Sidecar

**Date:** 2026-06-28
**Author:** Derek J. Russell + O+V (Claude Opus 4.8)
**Status:** Foundation built test-first (25 tests green); harness wiring (Cycle 9) pending
**Topic:** Decouple the deterministic `written=True` git-plumbing proof from the 50-min DW gauntlet, and make on-node telemetry teardown-race-proof.

## 1. Motivation

A1 run #14 came back `UNKNOWN` (DW-autarky starvation, not the file-isolation fix) and the warm node
self-deleted before its `debug.log` could be pulled. Two root problems:

1. **The Marathon Trap** — we test a *deterministic git-plumbing* fix (`written=True` durable commit) through a
   *non-deterministic 50-min LLM gauntlet* hostage to continuous DW health.
2. **Telemetry teardown race** — the IAP-SSH *pull* bridge loses everything after the last drained byte when a
   node is hard-killed/preempted.

## 2. Approach (zero-duplication, no production pollution)

**Feature A — Fast-Forward Fixture (`--inject-deterministic-fixture`).** Boot O+V normally, but **swap the
candidate generator via DI** (`FixtureGenerator`, a Decorator over the real `CandidateGenerator`) so generation
returns a deterministic, AST-mutated candidate with **zero provider calls**. The real
VALIDATE → APPLY → `change_engine` → `AutoCommitter` → VERIFY path runs **blind to the swap** — proving the
exact `written=True` git path in <3 min, decoupled from DW. Production `CandidateGenerator` is **never
modified** (Open/Closed).

**Feature B — Continuous Async GCS Telemetry Sidecar.** A node-side async loop pushes the growing `debug.log`
to GCS as **immutable append-only chunks** every ~5s, with a guaranteed final flush, reusing the **native GCS
Vault** in `state_persistence_daemon.py` (ADC, no gsutil).

## 3. The three strict constraints (all satisfied by the foundation)

1. **Cryptographic Network Airgap** — `LLMAirgap` wraps the transport chokepoint and raises
   `FatalAirgapException` on any LLM-provider host, while leaving GCS reachable (the sidecar needs it). A
   passing fixture run therefore *proves* no LLM call occurred — structural, not a promise.
2. **Append-Only Immutable Chunks** — `AppendOnlyChunkStreamer` emits monotonic `…/chunk_00001.log` objects;
   never overwrites; a dead node's log is reconstructable by chunk order. Fail-soft.
3. **Dynamic AST Mutation (no hardcoded patch)** — `build_deterministic_mutation` parses the target's AST,
   injects a harmless seed-derived no-op binding, `compile()`-validates, and returns the mutated source.

## 4. Built foundation (test-first, default-off-safe) — 25 tests

`scripts/a1_deterministic_fixture.py`:
- `build_deterministic_mutation(src, *, seed)` — AST mutation.
- `FatalAirgapException`, `DEFAULT_LLM_HOSTS`, `llm_hosts_from_env`, `is_llm_provider_host`, `LLMAirgap` — airgap.
- `FixtureCandidate`, `fixture_candidate_payload(*, env, read_file)` — no-DW injection core.
- `FixtureGenerator(inner, *, env, read_file)` — DI/Strategy decorator; delegates via `__getattr__`, overrides
  only `generate`, fail-safes to the inner generator when fixture mode is off.

`scripts/a1_gcs_telemetry_sidecar.py`:
- `AppendOnlyChunkStreamer(session_id, sink)` — immutable monotonic chunking, fail-soft.
- `make_gcs_chunk_sink(target_uri, *, client_factory)` — GCS-Vault sink (reuses `_parse_gs_uri`), fail-soft.
- `flush_tick(path, streamer)` + `run_sidecar(path, streamer, *, interval_s, stop_event)` — async tail loop with
  final-flush-on-death.

## 5. Remaining — Cycle 9 (harness wiring)

In `a1_live_fire_chaos_harness.py` + the `--execute-on-node` boot:
- `--inject-deterministic-fixture` flag + env: `JARVIS_A1_FIXTURE_MODE`, `JARVIS_A1_FIXTURE_TARGET`,
  `JARVIS_A1_FIXTURE_SEED`, `JARVIS_A1_GCS_TELEMETRY_TARGET`, `JARVIS_GCS_STREAM_INTERVAL_S` (default 5).
- At fixture boot: install `LLMAirgap` (patch `httpx` send), **swap `orchestrator._generator → FixtureGenerator`**
  at the battle-test construction seam, launch `run_sidecar` as a task.
- Scope honesty (printed in the run output): this proves the file-isolation / durable-commit **git plumbing**,
  not the full autarky A1 autonomy gate.

## 6. Flags (all additive; fixture is OFF unless the flag is passed)

`--inject-deterministic-fixture` · `JARVIS_A1_FIXTURE_MODE` · `JARVIS_A1_FIXTURE_TARGET` ·
`JARVIS_A1_FIXTURE_SEED` · `JARVIS_A1_GCS_TELEMETRY_TARGET` · `JARVIS_GCS_STREAM_INTERVAL_S` ·
`JARVIS_AIRGAP_LLM_HOSTS` (additive host extension).

## 7. Testing

`tests/scripts/test_a1_deterministic_fixture.py` (15) + `tests/scripts/test_a1_gcs_telemetry_sidecar.py` (10).
Cycle 9 adds harness argparse + injection-seam tests.
