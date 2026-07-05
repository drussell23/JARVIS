# Domain 1 — Staging 1: The Transport Hop

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A Body-side `StructuralDeltaSensor` publishes bounded structural deltas as `causal.delta.<repo>` over the proven Stage-2/3/4 bridge; a Brain-side `CausalDeltaSubscriber` receives them, dedups by `TrinityEvent.fingerprint`, and orders them causally — all riding existing transport, with Stage-3 WAL durability inherited on a severed link.

**Architecture:** The sensor computes a delta (Staging-0 `compute_file_delta` + `stamp_delta`) and `publish_raw(topic=f"causal.delta.{repo}", data=envelope, source=RepoType, correlation_id=head_sha, causation_id=parent_sha)` onto the Body's local `TrinityEventBus`. The Body `TrinityBusBridge` (allowlist gains `causal.delta.*`) mirrors it to the broker; the Stage-3 `DurableOutbound` journals it at publish (local-origin passes `_journal_local_origin_only`), so a mid-transit partition queues it on disk and replays on mTLS restore — **no new durability code, no retry loop**. On the Brain, the inbound `TrinityBusBridge` republishes onto the Brain's `TrinityEventBus`; `CausalDeltaSubscriber` subscribes `causal.delta.*`, reads `RepoType` **reflectively from the payload** (never the topic string), and records deltas in causal order. Nothing net-new in transport/dedup/routing.

**Tech Stack:** Staging-0 `structural_delta` engine; `TrinityEventBus.publish_raw`/`subscribe`; the Stage-2/3/4 `TrinityBusBridge`/`DistributedEventBus`/`DurableOutbound`; `RepoType`.

## Global Constraints (Staging-1 mandates, binding)

- **MANDATE 1 (root-cause durability):** a severed bridge mid-transit queues the payload on disk via the EXISTING Stage-3 `DurableOutbound` FS-WAL (journal-at-publish → replay on reconnect). NO retry loops, NO `sleep`, NO heuristic back-off anywhere in the sensor or subscriber.
- **MANDATE 2 (reflective RepoType, no string-match routing):** the subscriber determines the source repo by reading the `RepoType` enum from the payload/`TrinityEvent.source` — NEVER by parsing the topic string or `if repo == "jarvis"` case-switching. Subscribe the wildcard `causal.delta.*`; branch on the reflected enum only for tagging, never for routing logic.
- **MANDATE 3 (DRY, zero new transport):** `publish_raw` + add `causal.delta.*` to the `TrinityBusBridge` outbound allowlist + `TrinityEvent.fingerprint` 60s-window dedup ONLY. No new socket, dedup algorithm, or wire-routing handler.
- **MANDATE 4 (adversarial loopback):** the two-process loopback test fires concurrent interleaved deltas from all three repos (jarvis/prime/reactor) simultaneously; proves the Brain subscriber drops exact fingerprint-duplicates while processing distinct deltas in causal order, non-blocking.
- Standing: dark behind `JARVIS_DISTRIBUTED_BUS_ENABLED` (existing master); `from __future__ import annotations`; py3.9 (`asyncio.wait_for`, never `asyncio.timeout`); async-first, no blocking on the loop; ASCII; fail-soft (a malformed inbound delta is logged-and-dropped, never crashes the bus); single-writer (the Body only publishes; the graph write is Staging 2 — the subscriber here only RECORDS receipt, no graph). TDD; named-files commits.

## Key facts (scouted, verified)

- Body bridge allowlist: `scripts/run_body_mode.py:425` `outbound_topics=["intake.remote_signal.*", "console.*"]` → add `"causal.delta.*"`.
- `_journal_local_origin_only` (`run_body_mode.py:86`): passes local-origin trinity events → causal deltas (origin `mac-body`) are journaled by `DurableOutbound` automatically (inherited durability).
- Brain attach point: `organism_bus_host.py:171` `trinity_bus = await get_trinity_event_bus()`; construct the subscriber and `await sub.start()` right where `RemoteIntakeBridge` is wired (`:184-189`), lazy-imported inside the guard (master-off byte-identical).
- `TrinityEvent.fingerprint` (`trinity_event_bus.py:291`) = `sha256(topic:source.value:sorted(payload))[:16]`; dedup window `TRINITY_DEDUP_WINDOW` default 60s (`trinity_event_bus.py:971-982`). Reuse — no new dedup.
- `publish_raw(topic, data, priority=NORMAL, target=BROADCAST, persist=True, correlation_id=None, causation_id=None) -> str` (`trinity_event_bus.py:1006`); `RepoType.{JARVIS,PRIME,REACTOR,BROADCAST}` values `jarvis/prime/reactor/broadcast` (`trinity_event_bus.py:181`).
- Staging-0: `compute_file_delta(repo, file_path, before, after) -> StructuralDelta`; `stamp_delta(delta, DeltaLineage) -> Dict`; `EmitSequence.next(repo) -> int` (all in `governance/causal/structural_delta.py`).

---

### Task 1: `StructuralDeltaSensor` (Body publisher) + allowlist

**Files:**
- Create: `backend/core/ouroboros/governance/causal/structural_delta_sensor.py`
- Modify: `scripts/run_body_mode.py` (allowlist `causal.delta.*`)
- Test: `tests/governance/causal/test_structural_delta_sensor.py`

**Interfaces (Produces):**
```python
CAUSAL_DELTA_TOPIC_PREFIX = "causal.delta."

class StructuralDeltaSensor:
    def __init__(self, trinity_bus, *, repo: str,
                 emit_seq: Optional[EmitSequence] = None,
                 sha_reader: Optional[Callable[[str], "GitLineage"]] = None) -> None: ...
    async def emit_file_change(self, file_path: str, before_source: str,
                               after_source: str, *, lineage: "GitLineage") -> Optional[str]:
        """Compute delta (Staging-0) -> stamp lineage (repo from RepoType, head/parent/merge_base
        from `lineage`, emit_seq from the durable counter) -> publish_raw(
        topic=CAUSAL_DELTA_TOPIC_PREFIX + repo, data=envelope,
        source=RepoType(repo), correlation_id=head_sha, causation_id=parent_sha).
        Returns the event_id, or None if the delta is empty AND not file_level_churn
        (nothing structural changed -> nothing to publish). Fail-soft: never raises."""

@dataclass(frozen=True)
class GitLineage:   # the git-read SHAs, injectable so unit tests need no real repo
    head_sha: str
    parent_sha: str
    merge_base: str
```
- `repo` is validated to a `RepoType` at construction (reflective — `RepoType(repo)` raises on unknown, caught → the sensor refuses to construct for a non-trinity repo; NO string allowlist). The topic embeds `repo` for human-readable routing but the SOURCE enum is the authoritative identity.
- Durability note (Mandate 1): the sensor only `publish_raw`s; the Stage-3 `DurableOutbound` on the same broker journals it. A test asserts a causal-delta event passes `_journal_local_origin_only` (so it IS journaled) — the durability is inherited, not re-coded.

- [ ] **Step 1: Write failing tests** — injected fake `trinity_bus` recording `publish_raw` calls: (a) a real before/after pair → one `publish_raw` with `topic=="causal.delta.jarvis"`, `source==RepoType.JARVIS`, `correlation_id==head_sha`, `causation_id==parent_sha`, and `data` == the `stamp_delta` envelope (no content — magic-token absent); (b) empty structural change (identical source) → no publish, returns None; (c) `file_level_churn` (overflow) → publishes; (d) unknown repo → sensor construction refuses (ValueError caught → documented); (e) `_journal_local_origin_only` returns True for a synthesized causal-delta trinity event (durability inheritance pin).
- [ ] **Step 2: RED.** **Step 3: Implement.** **Step 4: GREEN** + `causal.delta.*` added to the Body allowlist (verified by a one-line assertion test on `run_body_mode`'s outbound list, or a grep-test). **Step 5: Commit** — `feat(domain1): StructuralDeltaSensor publishes causal.delta.<repo> + bridge allowlist (Staging 1 Task 1)`.

---

### Task 2: `CausalDeltaSubscriber` (Brain receiver) — reflective, dedup, causal order

**Files:**
- Create: `backend/core/ouroboros/governance/causal/causal_delta_subscriber.py`
- Modify: `backend/core/ouroboros/governance/transport/organism_bus_host.py` (wire it beside `RemoteIntakeBridge`)
- Test: `tests/governance/causal/test_causal_delta_subscriber.py`

**Interfaces (Produces):**
```python
class CausalDeltaSubscriber:
    def __init__(self, trinity_bus, *, on_delta: Optional[Callable[[dict], None]] = None) -> None: ...
    async def start(self) -> None:   # subscribe "causal.delta.*"
    async def stop(self) -> None:
    def observed(self) -> List[Tuple[str, int, str]]:  # (repo, emit_seq, head_sha) in causal order
    def observed_count(self) -> int
```
- Handler: parse the envelope; read the source repo REFLECTIVELY — `RepoType(event.source)` (or `RepoType(envelope["lineage"]["repo"])`), never the topic string; validate the lineage block; append `(repo, emit_seq, head_sha)` to a per-repo causal-ordered log (ordered by `emit_seq` within a repo — the Lamport guarantee from Staging 0; cross-repo interleave is recorded with a global receive index too). `TrinityEvent.fingerprint` dedup at the bus handles exact duplicates BEFORE the handler fires (reuse — the subscriber adds NO dedup); an idempotency guard on `(repo, emit_seq)` is the belt-and-suspenders for a replay outside the 60s window. Malformed envelope → log-and-drop (fail-soft). NON-BLOCKING: any non-trivial work (e.g. the `on_delta` callback) must not block the bus loop — the handler is a fast append; heavy work (Staging 2 graph fold) is deferred/offloaded there, not here.
- Wire in `organism_bus_host`: lazy import inside the existing start-guard, `await sub.start()` after the intake bridge; `await sub.stop()` in stop. Master-off byte-identical.

- [ ] **Step 1: Write failing tests** — real `TrinityEventBus` (TRINITY_MULTICAST_ENABLED=false): (a) publish 3 causal deltas (jarvis/prime/reactor) → `observed()` has all 3, repo read reflectively (test a delta whose TOPIC says one repo but SOURCE enum says another → the SOURCE wins — proves no topic-string routing); (b) publish the SAME delta twice within 60s → observed once (fingerprint dedup); (c) out-of-order emit_seq within one repo → `observed()` for that repo is in emit_seq order; (d) malformed envelope (missing lineage) → dropped, no raise; (e) organism_bus_host constructs the subscriber when gen/bus armed (recording fake), byte-identical off.
- [ ] **Step 2: RED.** **Step 3: Implement.** **Step 4: GREEN.** **Step 5: Commit** — `feat(domain1): CausalDeltaSubscriber -- reflective RepoType, fingerprint dedup, causal order (Staging 1 Task 2)`.

---

### Task 3: The adversarial two-process loopback (Mandate 4)

**Files:**
- Test: `tests/governance/causal/test_causal_transport_loopback.py`

Real localhost WS pair (reuse `test_bridge_reflection_and_cursor.py` `_Pair`/`_cfg`/`_free_port` + `organism_bus_host` patterns): a Body-side `TrinityEventBus`+bridge+`StructuralDeltaSensor`(×3 repos) and a Brain-side `TrinityEventBus`+host+`CausalDeltaSubscriber`, linked over a real mTLS-disabled WS.

- [ ] **Step 1: The adversarial test:**
  - Three `StructuralDeltaSensor`s (repo=jarvis/prime/reactor) publish `N` deltas each **concurrently** (`asyncio.gather`) with **interleaved** emit — rapid-fire, simultaneous.
  - Inject a fraction as **exact duplicates** (same topic+source+payload within 60s).
  - **Assert (mathematically):** the Brain subscriber's `observed_count()` == `3·N` distinct (duplicates dropped — `set` equality on `(repo, emit_seq, head_sha)`); every duplicate's fingerprint collided and was dropped (assert `bus.events_deduplicated` advanced by exactly the injected-dup count); each repo's observed sub-sequence is in `emit_seq` order (causal chronological within source); the Brain loop never blocked (a concurrent heartbeat/counter task kept ticking through the storm — assert its tick count is within expected band, i.e. the ingest didn't starve it).
  - Condition-polled to a bounded deadline; run 3× for flake-immunity.
- [ ] **Step 2: Run** `python3 -m pytest tests/governance/causal/test_causal_transport_loopback.py -v` (unsandboxed — real sockets). **Step 3: Commit** — `test(domain1): adversarial 3-repo concurrent transport loopback -- dedup + causal order + non-blocking (Staging 1 Task 3)`.

---

## Self-Review
1. **Spec coverage (Staging 1 = spec Staging 1 "the transport hop"):** publish `causal.delta.*` (T1), Brain subscriber echo (T2), two-process loopback + dup-fingerprint dedup (T3), RepoType round-trips (T1/T2). Durability inherited from Stage-3 (T1 pin). Graph fold is Staging 2 — correctly out.
2. **Placeholder scan:** exact topics/signatures/envs; the `_Pair` reuse is a named scaffold, not a TBD.
3. **Type consistency:** `stamp_delta` envelope (Staging-0) is the `data` payload verbatim; `RepoType` is the reflected identity in both T1 (publish source) and T2 (read source); `(repo, emit_seq, head_sha)` tuple identical in T2/T3.
