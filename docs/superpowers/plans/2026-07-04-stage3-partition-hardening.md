# Stage 3 — Partition Hardening & Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Mac↔Brain link survives real failure: a dropped socket degrades to durable fsync'd disk queuing, reconnect re-races discovery and replays exactly (monotonic Last-Event-ID, ack-trimmed), and the deliberate-partition suite proves zero torn ledgers / zero duplicated terminal states.

**Architecture:** Arm the DORMANT ack frame the Stage-0 codec explicitly reserved for this stage (`bus_bridge_server.py:148` — "FRAME_ACK is plumbed for Stage 3 WAL trim; no-op here"): the server acks its contiguous ingest cursor; the client consumes acks to trim a durable outbound WAL (REUSING the intake `WAL` class verbatim — flock/fsync append-only JSONL with tombstones + compaction). The WAL journals every bridgeable event at publish time (upstream of connection state — root-cause, not retry theater), seeds reconnect replay beyond the broker's bounded 512-event ring, and survives Body process death. Reconnect re-races discovery via a URL-resolver seam (the spec's line-69 requirement, currently violated by the static URL). Capacity is a dynamic fraction of live `shutil.disk_usage` free bytes — zero hardcoded byte caps.

**Tech Stack:** Existing Stage-0/1/2 transport (`bus_frame` codec, `BusBridgeClient/Server`, `DistributedEventBus`, `TrinityBusBridge`), intake `WAL` class (`intake/wal.py`), `fsm_checkpoint.py` HMAC suspend/resume (proven, reused unchanged), pytest.

## Global Constraints (operator mandates, binding on every task)

- **MANDATE 1 — Root-Cause Only:** socket drop ⇒ immediate graceful degrade to durable disk-backed queuing (fsync'd). NO in-memory retry loops, NO arbitrary timeout polling, NO connection slamming.
- **MANDATE 2 — Architectural Purity:** replay cursor = continuous monotonic `Last-Event-ID` tracking ONLY. ZERO hardcoded buffer limits — queue capacity scales dynamically with disk availability + env configuration.
- **MANDATE 3 — DRY:** reuse `StreamEventBroker` partition-recovery surface + `bus_frame` codec + intake `WAL` class + server-side dedup + `fsm_checkpoint` machinery. NO duplicate dedup logic on the Brain (single-writer FSM ledger + idempotent qualified-id ingest already exist).
- **MANDATE 4 — Bulletproof:** the deliberate-partition suite is the load-bearing deliverable: kill the WS mid-operation, prove Body→WAL queuing, Brain HMAC-checkpoint suspension (service-death leg), and mathematically exact post-reconnect replay (zero gap, zero dup, zero torn terminal states).
- Plus the standing constraints: `from __future__ import annotations`; py3.9 (`asyncio.wait_for`, never `asyncio.timeout`); async-first; ships dark behind `JARVIS_DISTRIBUTED_BUS_ENABLED` (default false), byte-identical off; fail-soft (never crash the bus loops); ASCII; commit per task, named files only.

## Key facts for implementers (scouted, verified)

- `bus_frame.ack_frame(source_id: str, last_event_id: str) -> BusFrame` EXISTS (`bus_frame.py:101`); `FRAME_ACK="ack"`; the ack rides the `last_event_id` field. Today NOBODY sends or consumes it.
- `BusBridgeClient._pump_inbound` drops every non-EVENT frame (`bus_bridge_client.py:248`) — ack consumption hooks in there.
- Broker event ids are zero-padded hex sequence numbers minted per-broker — **monotonic and order-comparable for a single source broker** (the cursor-purity foundation).
- `WAL`/`WALEntry` (`intake/wal.py:28-37`): `WAL(path, max_age_days=7)`, `append(WALEntry)`, `update_status(lease_id, "acked"|"dead_letter")`, `pending_entries() -> List[WALEntry]`, `compact()`. Durability via `cross_process_jsonl.flock_append_line` (fsync fallback). Reuse AS-IS — `lease_id` carries the event_id.
- `StreamEventBroker` history ring: `deque(maxlen=512 default)` drop-oldest (`JARVIS_IDE_STREAM_HISTORY_MAXLEN`) — pure-broker replay cannot recover past it. That bound is the WAL's reason to exist.
- `fsm_checkpoint.py`: `capture_inflight(reason=...)` (suspend, shutdown-triggered), `list_pending()` (HMAC-verified only, fail-closed), `hydrate_pending_checkpoints(ingest_fn)` + `mark_resumed` (exactly-once resume). REUSED UNCHANGED — the partition suite's service-death leg exercises it; we add zero new checkpoint code (Mandate 3).
- Disk probe precedent: `shutil.disk_usage` one-liner pattern (`predictive_engine.py:260`).

---

### Task 1: Arm the ack lane — server emits, client consumes

**Files:**
- Modify: `backend/core/ouroboros/governance/transport/bus_bridge_server.py` (`_handle_ws` FRAME_EVENT branch + ack cadence state)
- Modify: `backend/core/ouroboros/governance/transport/bus_bridge_client.py` (`_pump_inbound` FRAME_ACK branch + cursor + hook)
- Test: `tests/governance/transport/test_ack_lane.py`

**Interfaces:**
- Consumes: `bus_frame.ack_frame(source_id, last_event_id)` (`bus_frame.py:101`); `FRAME_ACK`; the real localhost `_Pair` scaffolding from `tests/governance/transport/test_bridge_reflection_and_cursor.py`.
- Produces: server: after ingesting event frames, sends `ack_frame(cfg.source_id, <last ingested event_id>)` on a cadence — every `JARVIS_BUS_ACK_EVERY_N` events (default 16) OR `JARVIS_BUS_ACK_INTERVAL_S` seconds (default 5.0), whichever first; both env-resolved floats/ints, no literals in logic. Client: `BusBridgeClient.last_acked_id -> Optional[str]` property (monotonic: only advances, never regresses — compare zero-padded hex strings) + constructor kwarg `on_ack: Optional[Callable[[str], None]] = None` invoked (fail-soft, exceptions swallowed+logged) on each cursor advance.
- Semantics: the ack carries the event_id of the last EVENT frame the server ingested on THIS connection (per-connection state, reset on new WS). Client ignores regressive/duplicate acks (monotonic guard). Server sends the ack as a normal `ws.send_bytes(frame.encode())` from the `_handle_ws` receive loop (no new task).

- [ ] **Step 1: Write the failing tests** — real localhost pair: (a) publish N=20 client-side trinity-prefixed events → within the ack interval the client's `last_acked_id` equals the 20th event's id and `on_ack` fired ≥1 time with monotonically increasing ids; (b) monotonic guard: hand-deliver a regressive `ack_frame` via a fake ws message into `_pump_inbound`'s parse path (unit-level: call the new `_apply_ack(frame)` directly) → cursor unchanged; (c) legacy shape: a client with no `on_ack` and a server talking to an OLD client (no ack consumption = frames ignored per current L248 behavior) both keep working — assert the Stage-2 suites still pass unmodified.
- [ ] **Step 2: RED** — `python3 -m pytest tests/governance/transport/test_ack_lane.py -x -q` → AttributeError/assertions.
- [ ] **Step 3: Implement** — server `_handle_ws`: in the `FRAME_EVENT` branch, after `self._ingest(frame)`, track `conn_last_eid = frame.event["event_id"]` + counters; when cadence trips, `await ws.send_bytes(bf.ack_frame(self._cfg.source_id, conn_last_eid).encode())` (guarded try/except, replace the L148 no-op comment with the live description). Client: in `_pump_inbound`, route `frame.kind == bf.FRAME_ACK` to `self._apply_ack(frame)` (new method: monotonic-advance `self._last_acked_id`, invoke `self._on_ack` fail-soft) BEFORE the existing non-event `continue`.
- [ ] **Step 4: GREEN** — new file + `tests/governance/transport/ -q` all green (Stage-0/1/2 unregressed).
- [ ] **Step 5: Commit** — `git add` the 3 named files; `feat(stage3): arm the dormant ack lane -- server ingest-cursor acks, client monotonic consumption (Task 1)`.

---

### Task 2: `DurableOutbound` — the Body WAL (journal at publish, trim on ack)

**Files:**
- Create: `backend/core/ouroboros/governance/transport/durable_outbound.py`
- Test: `tests/governance/transport/test_durable_outbound.py`

**Interfaces:**
- Consumes: `WAL`, `WALEntry` from `backend.core.ouroboros.governance.intake.wal` (reused verbatim — Mandate 3); `StreamEventBroker.subscribe()` (`.queue`-based); `shutil.disk_usage`.
- Produces:
  ```python
  class DurableOutbound:
      def __init__(self, broker, *, wal_path: Optional[str] = None,
                   op_prefix: str = "trinity:") -> None: ...
      async def start(self) -> None   # durable subscriber armed
      async def stop(self) -> None
      def on_ack(self, acked_event_id: str) -> None      # Task-1 hook target
      def pending(self) -> List[Dict[str, Any]]           # ordered by event_id (monotonic hex)
      def pending_count(self) -> int
      @property
      def degraded_capacity(self) -> bool
  ```
  Env: `JARVIS_BODY_WAL_PATH` (default `<repo>/.jarvis/body_outbound_wal.jsonl`), `JARVIS_BODY_WAL_DISK_FRACTION` (default `0.05` — the WAL may grow to at most this fraction of CURRENT `shutil.disk_usage(dir).free`, re-probed on every append batch; NO byte-cap constants — Mandate 2), `JARVIS_BODY_WAL_MAX_AGE_DAYS` (default 7, passed to `WAL`).
- Semantics (Mandate 1 — upstream of connection state): a broker subscriber journals every event whose `op_id.startswith(op_prefix)` as `WALEntry(lease_id=<event_id>, envelope_dict=<StreamEvent.to_dict()>, status="pending")` — at PUBLISH time, connected or not, so a partition (or a Body crash) can never lose a signal that `publish()` accepted. `on_ack(eid)`: every pending entry with `lease_id <= eid` (zero-padded hex compare) → `update_status(lease_id, "acked")`; every `JARVIS_BODY_WAL_COMPACT_EVERY_N` acks (default 256) → `wal.compact()`. Capacity: before append, if WAL file size > fraction×free → `compact()` first; if STILL over → set `degraded_capacity=True`, drop-oldest-pending via `update_status(oldest, "dead_letter")` + ONE loud `logger.warning` per episode (mirroring the broker's `stream_lag` single-event pattern — Mandate 3); never raises, never blocks the loop (size probe + WAL I/O already flock/fsync-per-line; probes use `os.stat` on the WAL file, cheap).

- [ ] **Step 1: Write the failing tests** — (a) journal-at-publish: real broker + started `DurableOutbound`, publish 5 trinity events + 2 non-prefix events → `pending_count()==5`, ordered by event_id; (b) crash-survival (THE WAL point): tear down the instance without acks, construct a NEW `DurableOutbound` on the same `wal_path` → `pending()` returns the same 5 (fsync'd truth, not memory); (c) ack-trim: `on_ack(<3rd id>)` → `pending_count()==2` and a fresh instance from disk agrees (tombstones durable); (d) dynamic capacity: monkeypatch `shutil.disk_usage` to return tiny `free` → append flips `degraded_capacity` True, drops OLDEST pending with exactly one warning, newest survives; restore disk → next append clears the flag; (e) zero hardcoded caps: grep-style assertion that the module contains no integer byte-size literals in the capacity path (read the module source in the test and assert `JARVIS_BODY_WAL_DISK_FRACTION` is the only capacity knob).
- [ ] **Step 2: RED.** `python3 -m pytest tests/governance/transport/test_durable_outbound.py -x -q` → ModuleNotFoundError.
- [ ] **Step 3: Implement** per the Produces block (~120 lines; the WAL class does all durability heavy-lifting).
- [ ] **Step 4: GREEN** + full transport suite.
- [ ] **Step 5: Commit** — `feat(stage3): DurableOutbound -- fsync WAL journal at publish, ack-driven trim, disk-fraction capacity (Task 2)`.

---

### Task 3: WAL-seeded reconnect replay + re-raced discovery (the client integration)

**Files:**
- Modify: `backend/core/ouroboros/governance/transport/bus_bridge_client.py` (url resolver seam + WAL-seeded replay)
- Modify: `backend/core/ouroboros/governance/transport/distributed_event_bus.py` (thread `DurableOutbound` + `url_resolver` through `start_client`)
- Test: `tests/governance/transport/test_wal_replay_and_rediscovery.py`

**Interfaces:**
- Consumes: Task-1 `on_ack` hook + Task-2 `DurableOutbound` (`pending()`, `on_ack`); `bus_frame.event_frame` needs a `StreamEvent` — rebuild via `StreamEvent(**entry.envelope_dict)`-compatible construction (envelope_dict is `StreamEvent.to_dict()`; strip `schema_version` if the constructor rejects it — check `to_dict()` keys against the dataclass).
- Produces: `BusBridgeClient.__init__` gains `url_resolver: Optional[Callable[[], Awaitable[Optional[str]]]] = None` and `durable: Optional[Any] = None` kwargs. `run()`'s reconnect loop: each attempt resolves `url = await self._url_resolver() or self._url` (discovery re-race per attempt — spec line 69; static url stays the fallback + the legacy default). `_connect_once`: after the hello frame and BEFORE starting the live pump, send every `durable.pending()` entry (ordered) as event frames — the server's qualified-id dedup absorbs any overlap with the broker-cursor replay (Mandate 3: no client-side dedup added). Wire `client._on_ack = durable.on_ack` when both present (DistributedEventBus composes this). `DistributedEventBus.__init__` gains optional `durable_outbound`/`url_resolver` passthroughs (default None = Stage-2-identical).
- Ordering note for the implementer: WAL-seeded frames go out FIRST (they are the oldest truth), then the live pump's broker-cursor replay (`initial_last_sent_id`) — duplicates across the two streams are the SERVER's job (existing `_mark_seen`), exactness is asserted end-to-end in Task 4.

- [ ] **Step 1: Write the failing tests** — real pair: (a) rediscovery: client constructed with a `url_resolver` that returns a NEW (working) URL after the first (dead) one; kill+restart server on a different port; assert reconnect lands on the resolver's URL (spec line 69 pin); (b) WAL-seeded replay: publish 6 events while NO server exists (broker history artificially tiny: `JARVIS_IDE_STREAM_HISTORY_MAXLEN=2` env — proving broker replay alone CANNOT recover) → start server → connect → server broker holds ALL 6 exactly once (WAL carried what the ring evicted); (c) legacy: no `durable`, no `url_resolver` → byte-identical Stage-2 behavior (existing suites green).
- [ ] **Step 2: RED.**
- [ ] **Step 3: Implement** (keep the diff additive; the resolver seam replaces `self._resolve_url()`'s static return only when the kwarg is provided).
- [ ] **Step 4: GREEN** + `tests/governance/transport/ -q` (Stage-0/1/2 + Tasks 1-2) all green.
- [ ] **Step 5: Commit** — `feat(stage3): WAL-seeded reconnect replay + per-attempt discovery re-race (Task 3)`.

---

### Task 4: Body-mode integration — degrade surfacing + WAL wiring

**Files:**
- Modify: `scripts/run_body_mode.py` (compose `DurableOutbound` + `url_resolver=discover_brain_endpoint`; census gains queue depth; "Brain offline" degrade line)
- Test: `tests/infra/test_run_body_mode.py` (extend)

**Interfaces:**
- Consumes: Tasks 1-3 seams; the driver's existing injectable-seam pattern (`durable_factory` kwarg added, mirroring the others).
- Produces: census line becomes `[BodyMode] lag_events=%d worst_ms=%.1f connected=%s queued=%d` ; on a connected→disconnected transition the driver logs `[BodyMode] Brain offline -- %d signals queued (durable)` exactly once per episode (spec line 68's operator surface); SUMMARY gains `queued_at_exit=%d`. Discovery failure at START no longer exits 2 when the WAL is armed — it degrades: signals journal durably, reconnect keeps re-racing (root-cause degrade, Mandate 1); `--require-brain` CLI flag restores the old exit-2 contract for the acceptance runs that want it.
- [ ] **Step 1: Write the failing tests** — injected seams: (a) census includes `queued=` from a fake durable's `pending_count`; (b) offline-at-start with WAL armed → NO exit 2, signals journaled (fake durable records appends), "Brain offline" logged once; (c) `--require-brain` restores exit 2.
- [ ] **Step 2: RED.** **Step 3: Implement.** **Step 4: GREEN** (5 existing + new all pass).
- [ ] **Step 5: Commit** — `feat(stage3): body mode degrades durably -- WAL wiring, queue census, Brain-offline surfacing (Task 4)`.

---

### Task 5: THE DELIBERATE-PARTITION SUITE (load-bearing — Mandate 4)

**Files:**
- Create: `tests/governance/transport/test_deliberate_partition.py`
- Test: (self)

**Interfaces:** Consumes everything above + `fsm_checkpoint.capture_inflight/list_pending/hydrate_pending_checkpoints/mark_resumed` (REUSED UNCHANGED — Mandate 3) + the real localhost pair scaffolding.

Deterministic legs (all in-proc, real WS, real WAL, real brokers — no sleeps-as-logic, condition-polled with bounded budgets):
- [ ] **Leg A — kill mid-stream, exact replay:** stream 30 events; at event ~10 HARD-KILL the server (`runner.cleanup()` — socket death, not graceful close); continue publishing 10 more during the partition (assert: client `connected=False`, WAL `pending_count>=10`, fsync'd); restart the server (fresh broker = fresh dedup state, same path); reconnect via resolver; assert the destination ends with ALL 30 exactly once — **zero gap, zero dup, mathematically asserted on the full ordered id set** (set equality + count equality + per-id occurrence == 1).
- [ ] **Leg B — Body process death mid-partition:** during a partition with N pending, destroy every client-side object (simulate crash: new broker, new client, new `DurableOutbound` from the SAME wal_path); reconnect; assert the N pending cross exactly once. (Proves durability is disk-truth, not object-lifetime.)
- [ ] **Leg C — Brain service death, HMAC suspend/resume:** in-proc: seed a fake in-flight op context, call `capture_inflight(reason="partition_test", base_dir=tmp)`; assert `list_pending(base_dir=tmp)` returns it HMAC-VERIFIED; TAMPER one byte of the checkpoint file on disk → assert `list_pending` now REJECTS it (fail-closed — the "zero torn ledger" property at the checkpoint layer); untampered twin: `hydrate_pending_checkpoints(ingest_fn)` re-injects EXACTLY once (`mark_resumed` second call False).
- [ ] **Leg D — no duplicated terminal state:** re-deliver the SAME WAL pending twice across two reconnects (simulate double-replay: manually re-send `pending()` after the acks were processed but before trim persisted — the worst-case race); assert the server-side count for those ids is STILL exactly 1 (qualified-id dedup is the single dedup authority — Mandate 3).
- [ ] Run: `python3 -m pytest tests/governance/transport/test_deliberate_partition.py -v` → 4 legs pass, repeated 3x for flake-immunity (`-p no:randomly --count 3` if pytest-repeat present, else 3 manual runs).
- [ ] **Commit** — `test(stage3): the deliberate-partition suite -- kill/crash/suspend/double-replay legs, exactness asserted (Task 5)`.

---

### Task 6: Live-fire acceptance (operator/agent-run, after merge)

No new code — the run itself, mirroring Stage-2's flow:
- [ ] Provision persistent Brain (Stage-2 env + `JARVIS_BRAIN_SHIP_PROVIDER_KEYS=true` now that #69843 exists + idle window 3600).
- [ ] Body mode with `--inject-test-signal 3 --duration-s 600`; at T+120s **close the firewall** (the deterministic live partition), inject 3 more signals (must journal: census `queued=3`, "Brain offline" line); at T+300s reopen the firewall; assert reconnect + census `queued=0` + Brain-side log shows exactly 6 distinct dedup_keys ingested, zero duplicates, all 6 provenance `chain_ok=True`.
- [ ] Brain-restart leg: `systemctl restart jarvis-brain.service` mid-op via SSH; assert `capture_inflight` fired (journal shows checkpoint write) and post-boot `hydrate_pending_checkpoints` resumed (log `fsm_resume`).
- [ ] Teardown to $0, gcloud-verified. Ledger the verdict.

---

## Beyond Stage 3 — the O+V advancement menu (for sign-off, NOT in this plan's tasks)

Each item is root-cause-grade, leverages existing machinery, zero hardcoding. Recommend picking 2-3 as "Stage 4" after the partition suite is green:

1. **Sovereign Brain Resurrection** ⭐ (highest leverage): when the Body's re-raced discovery finds NO Brain for `JARVIS_BRAIN_RESURRECT_AFTER_S` (env), the Body invokes the EXISTING `FailoverLifecycleController` awaken path against the `jarvis-brain-golden` family — the organism re-provisions its own Brain from the golden image, $0-orphan rules intact. The Body stops being a client and becomes a keeper. (Reuses: failover lifecycle, golden image, discovery, WAL — the queued signals replay into the resurrected Brain automatically via Stage 3.)
2. **Priority-aware WAL replay**: replay pending in urgency order (IMMEDIATE envelopes first) using the existing `EventPriority`/envelope-urgency fields instead of FIFO — after a long partition, the Brain sees the critical backlog first (Manifesto §5 intelligence-driven routing, applied to recovery).
3. **Latency-physics adaptive transport**: feed the Amnesia-Cure latency persistence (cross-run physics, already landed) into heartbeat/backoff/ack-cadence tuning — the link learns its own physics instead of env constants.
4. **Partition telemetry → posture**: link-state transitions publish DirectionInferrer ambient signals so flaky-network periods bias posture toward HARDEN (13th ambient signal; consumed by the existing PostureObserver).
5. **Actuation-lane durability (Brain→Mac)**: mirror `DurableOutbound` on the Brain side for Ghost Hands intents / NOTIFY_APPLY previews, so a Mac-offline window loses no actuation intent.
6. **Mesh-grade origin guard**: upgrade `TrinityBusBridge`'s loop guard to full multi-hop semantics (the Task-1 review caveat) — unlocks multi-Brain topologies (regional Brains, one Body).
7. **`/brain` console verb**: SerpentFlow surface for link state, WAL depth, last-ack age, resurrect status — Manifesto §7 absolute observability for the distributed organism.

## Self-Review

1. **Spec coverage (Stage-3, spec lines 62-74 + 94 + 96-101):** partition→WAL queuing (T2/T4), graceful console degrade "Brain offline — N queued" (T4), no busy-poll + discovery re-race per attempt (T3), Last-Event-ID monotonic replay + Brain dedup idempotence (T1/T3/T5-D), FSM-ledger authority + HMAC checkpoint resume (T5-C, machinery reused unchanged), heartbeat degraded-mode (already existed in Stage 0 — `degrade_after_missed_hb`; asserted alive in T5-A via `connected=False`), single-writer (unchanged; Mac still publish-only), partition-simulation test (T5), live soak (T6). Cutover posture: after T6, body-mode + brain-node IS the operating shape; full harness-level cutover ergonomics deferred to the menu (#7) deliberately.
2. **Placeholder scan:** clean — every step names exact files/envs/assertions; T2/T3 carry exact signatures; no TBDs.
3. **Type consistency:** `on_ack(acked_event_id: str)` (T2) matches the T1 hook `Callable[[str], None]`; `pending() -> List[Dict]` ordering contract used identically in T3/T5; `url_resolver: Callable[[], Awaitable[Optional[str]]]` matches `discover_brain_endpoint`'s shape (T4).
