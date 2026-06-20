# Sovereign C2 Telemetry Bridge — distributed observability

High-fidelity, real-time insight into the remote Linux engine from the M1 dev
interface — **not** SSH log-tailing. The M1 subscribes to the engine's **native
SSE event stream** (the same `StreamEventBroker` the IDE extensions consume) and
renders FleetEvaluator EWMA, OperationAdvisor blocks, and `state=applied`
victories live.

## Why this design (reuse-first + secure)
The engine already publishes everything over `GET /observability/stream` (a
push-based async SSE broker, started by `GovernedLoopService` as the
`EventChannelServer` on `127.0.0.1:8099`). A parallel Redis Pub/Sub would
duplicate it. The stream is **loopback-only by a grep-pinned security
invariant** — so the secure transport is an **SSH local-forward**, which keeps
the stream loopback-only and adds zero internet-facing surface. SSH carries the
encrypted bytes; the payload is the live native event stream.

```
 Linux engine (Publisher)                         M1 (Subscriber)
 ┌───────────────────────────┐    SSH -L tunnel   ┌──────────────────────────┐
 │ GLS → EventChannelServer  │  (encrypted)       │ jarvis_c2_subscriber.py  │
 │ 127.0.0.1:8099            │◄───────────────────│ → live dashboard         │
 │ /observability/stream     │                    │ (zero backend imports;   │
 │ fleet_calibrated,         │                    │  cannot starve the loop) │
 │ operation_terminal, …     │                    └──────────────────────────┘
 └───────────────────────────┘
```

## Deploy the engine with the C2 overlay
`network_mode: host` makes the container's loopback the host's loopback, so the
loopback-only stream is reachable by an SSH forward to the host:

```bash
docker compose -f docker-compose.prod.yml -f docker-compose.c2.yml up -d --build
```

## Connect from the M1 (one command)
```bash
./scripts/jarvis_c2_connect.sh user@linux-host
```
This opens `ssh -N -L 8099:localhost:8099 user@linux-host` and launches the
subscriber against `http://localhost:8099`. Live output:

```
[C2] connected — live engine telemetry:
  14:03:11 📊 calib[repair_sentinel] DeepSeek-V4-Pro ast=1.0 vtps=18.7
           └ applied=3 advisor_blocked=1 failed=0 grad=0 | EWMA[DeepSeek-V4-Pro=19]
  14:03:48 ✅ state=applied op=op-019ee...
           └ applied=4 advisor_blocked=1 failed=0 grad=0 | EWMA[DeepSeek-V4-Pro=19]
  14:04:02 🛡  advisor BLOCKED op=op-019ef...
```

Manual variant (two terminals) if you prefer:
```bash
ssh -N -L 8099:localhost:8099 user@linux-host        # terminal 1
python3 scripts/jarvis_c2_subscriber.py              # terminal 2 (M1)
```

## Security posture
- Stream stays **loopback-only** on the engine (invariant untouched).
- Transport is **SSH** (encrypted, key-auth) — no new listening port on the public interface.
- Subscriber is **read-only + standalone** (imports only stdlib + aiohttp; no
  orchestrator/policy modules) — it physically cannot mutate the engine or drag
  it onto the Mac.

## Validation status
- ✅ Subscriber pure projection logic unit-tested (7 tests).
- ✅ Reuses the existing SSE schema (`fleet_calibrated`, `operation_terminal`,
  governor/breaker events) — all already published by the engine.
- ⚠️ End-to-end tunnel + live render validated on the first Linux deploy (no
  remote host available locally).
