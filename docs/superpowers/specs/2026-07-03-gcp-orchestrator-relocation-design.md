# GCP Orchestrator Relocation — Distributed Body/Brain Design

**Date:** 2026-07-03
**Status:** DRAFT — awaiting user approval (brainstorming gate; no implementation until approved)
**Author:** O+V campaign (Claude, for Derek J. Russell)
**Supersedes:** the discarded local process-isolation design (rejected: N isolated Python runtimes would exhaust the M1's 16GB unified memory → swap-thrash, trading a CPU bottleneck for an I/O one).

## Problem

The Ouroboros governance brain — orchestrator event loop, FSM pipeline, 16 sensors, posture/semantic/cost observers, cognitive components — runs in one process on a 16GB M1 Mac. Two hard ceilings, proven across the A1 campaign:

1. **GIL contention** — five point-fixed starvation tiers plus a sixth un-instrumented 81s stall; sync/CPU work on any subsystem stalls the control-plane loop. Non-convergent.
2. **Memory** — the M1's 16GB unified memory cannot host the brain's full working set alongside the macOS Body; local process isolation would multiply the runtime footprint into swap.

Both dissolve if the brain runs on a right-sized cloud CPU VM and the Mac serves purely as the **macOS-native Body** (screen/voice/keyboard/Ghost Hands) + **operator console**, decoupled across an async network boundary.

## Goal

A **pure distributed client-server architecture**: the governance Brain runs on a GCP CPU VM with its own compute/memory; the Mac Body captures/actuates locally and streams events to the Brain over an async, self-healing transport. Network partitions degrade gracefully and reconcile on reconnect **without corrupting the FSM ledger**. No local emulation of the cloud; no hardcoded endpoints; no brittle polling.

## Non-Goals (YAGNI)

- Not moving the macOS Body — screen capture, voice I/O, keyboard, Ghost Hands need Mac APIs and stay local by definition.
- Not moving J-Prime inference (separate L4 GPU node; the Brain VM is CPU-only). The Brain *calls* DW/J-Prime as it does today.
- Not the hedge-governor batch-vs-RT routing fix (queued as the immediately-following slice per user).
- Not a new messaging protocol — we extend `TrinityEventBus` across the wire, not replace it.

## Architecture

```
┌─ Mac — Body + Operator Console (local) ──────┐            ┌─ GCP CPU VM — Governance Brain (persistent) ─┐
│  Ghost Hands (screen/voice/keyboard actuate) │            │  Ouroboros orchestrator + FSM pipeline        │
│  Body-DEPENDENT sensors:                     │  async WS  │  Body-INDEPENDENT sensors (TestFailure,       │
│    VisionSensor (needs screen frames),       │◄══════════►│    GitHubIssue, Backlog, DocStaleness, …)     │
│    VoiceCommand (needs mic)                  │  (Trinity  │  posture / semantic / cost observers          │
│  SerpentFlow operator REPL/TUI               │   EventBus │  cognitive components                         │
│  Local TrinityEventBus + durable WAL queue   │   bridge)  │  GCP TrinityEventBus + EventChannelServer     │
│  WS client + Reachability discovery          │            │  mutation-target repo clone (/opt/trinity/…)  │
└───────────────────────────────────────────────┘           └───────────────────────────────────────────────┘
```

### The split (what runs where)
- **Mac (Body + console):** Ghost Hands actuation; the two Body-dependent sensors (`VisionSensor` reads the Ferrari frame server; `VoiceCommand` reads the mic); the SerpentFlow operator REPL/TUI; a local `TrinityEventBus`; a durable local event WAL; the WS client.
- **GCP Brain VM:** the orchestrator + full FSM pipeline; the 14 Body-independent sensors; posture/semantic/cost observers; cognitive components; the GCP `TrinityEventBus`; `EventChannelServer` (already hosts webhooks + `/observability/*`) now also hosting the WS server; the **mutation-target repo clone** at `/opt/trinity/jarvis` (the `IsomorphicEnv` path pattern, now the primary runtime rather than a soak sandbox).

### Mutation target (resolves "where does O+V write")
O+V mutates code on the **Brain VM's own repo clone** (`/opt/trinity/jarvis`), commits with the O+V signature (`AutoCommitter`), and pushes via the sovereign cross-repo mutator (Immutable-Orange rules preserved). The Mac's local repo is the operator's working copy, which pulls Brain-authored commits. This reuses the already-proven `IsomorphicEnv` node runtime — the migration promotes it from soak-sandbox to primary.

## Distributed TrinityEventBus (DRY — extend, don't replace)

The core move: `TrinityEventBus` publish/subscribe stays the *only* messaging API. We add a **network transport adapter** that bridges the Mac-local bus and the GCP bus so a `publish()` on either side propagates to subscribers on both — callers are unchanged; only the transport spans a socket.

- **Substrate to reuse:** `StreamEventBroker` (`ide_observability_stream.py`) already provides the exact partition-recovery surface — bounded history ring, **drop-oldest backpressure with a single `stream_lag` event**, **`Last-Event-ID` replay on reconnect**, **heartbeat cadence**, all env-tunable. The bridge is a thin bidirectional WS layer over this broker rather than net-new plumbing. `EventChannelServer` already runs the aiohttp server that will host the WS endpoint alongside the webhooks it already serves.
- **Direction of flow:** Mac→Brain carries Body sensor signals (Vision/Voice `SignalEnvelope`s — already serializable) + operator console commands (`/cancel`, `/posture`, `/attach`, approvals). Brain→Mac carries actuation intents (Ghost Hands: click/type/narrate), NOTIFY_APPLY diff previews, streaming generation tokens for the console, and posture/status telemetry for the TUI.

## Transport & Discovery (async, no hardcoded IP)

- **Async WS** (aiohttp — already a dependency). No blocking calls on either event loop; the Mac's UI and the Brain's loop both stay responsive under latency.
- **Service discovery — no hardcoded IP:** the Mac discovers the Brain VM's endpoint the same way the failover path already discovers J-Prime — `gcp_compute_rest.get_node_endpoints()` (zone-aware natIP/internal-IP lookup) + the **Reachability Racer** (`failover_lifecycle`) that probes candidate endpoints concurrently and binds the first healthy 200. A stable Brain-VM name/label is resolved to its current IP dynamically at connect time and on every reconnect.
- **Reconnect:** exp-backoff + jitter (the VS Code/Sublime extensions' proven client pattern), with `Last-Event-ID` replay so no event is missed across a blip.

## Bulletproof — Network Partition Semantics

Distributed faults are the design's hardest requirement. Structural coverage:

1. **Brain unreachable (partition or VM down):**
   - The Mac Body **keeps capturing** (screen/voice) and **queues signals to a durable local WAL** (reuse the intake WAL persistence pattern — append-only, fsync'd).
   - The console degrades gracefully: it surfaces "Brain offline — N signals queued" and continues local-only functions (screen capture, history). No new autonomous ops start (correct — the FSM lives on the Brain).
   - No busy-poll: reconnect is exp-backoff; discovery re-races on each attempt.
2. **Reconnect + state-sync WITHOUT FSM-ledger corruption:**
   - Every event carries a **monotonic, per-source event ID** (already the `Last-Event-ID` contract). On reconnect the Mac replays its WAL from the last Brain-acked ID; the Brain **deduplicates by event ID** (idempotent ingest — the intake router already dedups).
   - The FSM ledger is **authoritative on the Brain** and already crash-safe: strict Two-Phase Commit + the cryptographic phantom-write gate + HMAC-signed checkpoints. Replayed events re-enter intake as *signals*, not as ledger writes — they cannot corrupt terminal states. An op suspended when the partition hit resumes from its signed checkpoint (the suspend/resume machinery proven live this campaign).
3. **Split-brain avoidance:** the Brain is the single writer of the FSM ledger and the single mutator of the repo clone. The Mac never writes the ledger; it only emits signals and renders console/actuation. There is exactly one authority — no reconciliation conflict is possible by construction.
4. **Heartbeat both directions:** WS ping/pong; a missed heartbeat window flips each side to degraded mode deterministically (no ambiguous half-open sockets).

## Leverage-Existing Map (DRY)

| Need | Reuse |
|---|---|
| WS server host | `EventChannelServer` (already aiohttp, serves webhooks + `/observability/*`) |
| Bounded broker + replay + heartbeat + backpressure | `StreamEventBroker` / `IDEStreamRouter` (`ide_observability_stream.py`) |
| Reconnect client (backoff+jitter, Last-Event-ID) | VS Code/Sublime/JetBrains extension clients (`extensions/*`) |
| Endpoint discovery (no hardcoded IP) | `gcp_compute_rest.get_node_endpoints` + `failover_lifecycle` Reachability Racer |
| Brain-VM provisioning | `gcp_compute_rest` / golden-image bake (CPU node variant of the J-Prime bake) |
| Brain-side repo runtime | `IsomorphicEnv` (`/opt/trinity/jarvis`), promoted from sandbox to primary |
| Durable local queue | intake WAL persistence pattern |
| Messaging API | `TrinityEventBus` publish/subscribe (unchanged; transport adapter added) |

## Staging (each stage independently shippable + testable)

0. **Transport substrate.** Bidirectional WS bridge over `StreamEventBroker` + a `TrinityEventBus` network-transport adapter (publish here → subscribers there). TDD: local two-process loopback proving publish/subscribe crosses the socket, `Last-Event-ID` replay, heartbeat, drop-oldest backpressure. No relocation yet.
1. **Brain-VM provisioning + repo runtime.** CPU golden-image node hosting the orchestrator + `/opt/trinity/jarvis`; discovery wired (Racer). Orchestrator boots headless on the VM, reachable via WS. Mac still runs everything locally — VM is a warm standby.
2. **Move Body-independent sensors + observers + FSM to the Brain VM.** The 14 non-Body sensors, posture/semantic/cost, cognitive components, and the FSM pipeline run on the VM. The Mac runs only Vision/Voice sensors + console + Ghost Hands, streaming over WS. Live soak: `ControlPlaneStarvation` on the **Mac** → ~0 (the brain's compute is gone); Brain-VM loop censused separately.
3. **Partition hardening + cutover.** Local WAL, degrade/reconnect/replay, heartbeat-driven degraded mode, split-brain-free single-writer proof. The deliberate-partition test (kill the WS mid-op, assert: Body queues, op suspends to signed checkpoint, reconnect replays, FSM ledger uncorrupted) is the proof the distributed guarantee holds.

## Testing Strategy

- **Partition simulation** (load-bearing): sever the WS mid-op → assert Mac WAL queues signals, the in-flight op suspends to a signed checkpoint, reconnect replays from last-acked ID, the FSM ledger shows no torn/duplicated terminal state.
- Transport: publish/subscribe crosses the process/socket boundary; `Last-Event-ID` replay is exact; drop-oldest emits a single lag event; heartbeat-miss flips degraded deterministically.
- Discovery: no hardcoded IP anywhere (grep-enforced invariant, like the existing authority-invariant greps); a VM IP change is picked up on reconnect.
- Idempotency: replayed events dedup (no double-enqueue); single-writer (Mac never writes the ledger — AST/grep-enforced).
- Live soak per stage with `ControlPlaneStarvation` census on both hosts.

## Risks / Open Questions (for user review)

1. **Latency budget.** Body→Brain→actuation round-trips now cross a network. For autonomous code ops (the A1 path) this is fine (seconds-scale). For real-time Ghost Hands actuation driven by Vision, added RTT may matter — is real-time visual actuation in scope, or is the Brain's role autonomous code-dev (latency-tolerant)? **Recommend:** scope the WS to autonomous-dev signals first; keep any hard-real-time Body loop local.
2. **Cost.** A persistent CPU Brain VM runs 24/7 (unlike the ephemeral failover node). Right-size (e.g. `e2-standard-4`/`e2-highmem-4`) + an idle-suspend policy? Or on-demand-per-session?
3. **The repo-clone sync model.** Brain mutates `/opt/trinity/jarvis` and pushes; the Mac pulls. Confirm the operator is comfortable that autonomous commits originate from the VM's clone (the sovereign cross-repo mutator's Immutable-Orange rules still gate merges).
4. **Secrets/auth on the VM** (DW keys, GitHub, gcloud ADC) — the golden-image + metadata pattern already handles this for J-Prime; confirm reuse.
5. **Security of the WS boundary** — loopback-only won't work across hosts; needs auth (token/mTLS) + the ephemeral-/32-firewall pattern already used for the J-Prime mesh.

## Decision needed from user

Approve this distributed Body/Brain design (Approach B), or adjust the open questions above (esp. #1 latency scope, #2 cost/lifecycle, #5 WS auth) before I invoke `writing-plans` for Stage 0 (the transport substrate — shippable with no relocation).
