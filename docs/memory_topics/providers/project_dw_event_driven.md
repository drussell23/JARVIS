---
title: Project Dw Event Driven
modules: [backend/core/ouroboros/governance/doubleword_provider.py]
status: historical
source: project_dw_event_driven.md
---

DoubleWord provider switched from fixed 5s polling to a 3-tier event-driven architecture (Apr 8 2026):

- **Tier 0: Real-time SSE** (default ON) — `/v1/chat/completions` with `stream=true`. Zero polling. Token-by-token streaming. Venom tool loop. Falls back to batch on 429/503.
- **Tier 1: Webhook-driven batch** — `BatchFutureRegistry` + `/webhook/doubleword` endpoint on EventChannelServer. Standard Webhooks HMAC-SHA256. Requires `DOUBLEWORD_WEBHOOK_SECRET` env var.
- **Tier 2: Adaptive backoff poll** — Exponential backoff (2s base, 1.5x, 30s cap, ±25% jitter). Network-aware. One-line logs instead of tracebacks.

**Why:** Manifesto §3 mandates zero polling. Battle testing showed batch (16-22s) and real-time (20-40s) have comparable latency, but real-time eliminates the polling loop entirely and enables streaming.

**Wiring status (Apr 8):** All 3 tiers fully wired in GLS. BatchFutureRegistry instantiated and passed to DoublewordProvider. EventChannelServer started with batch_registry. Graceful shutdown in GLS.stop().

**DW timeout mitigations (Apr 8):** Three optimizations to reduce DW response time and prevent killing active streams:
1. **Complexity-aware max_tokens** — trivial=4096, moderate=8192, complex=16384 (was always 16384). `_DW_COMPLEXITY_MAX_TOKENS` dict in `doubleword_provider.py`.
2. **Stream activity tracking** — `_last_chunk_at` monotonic timestamp updated on each SSE chunk.
3. **Stream-aware timeout extension** — `asyncio.shield()` in candidate_generator RT path. If DW received chunk within 10s of budget expiry, grants up to 30s extension while preserving Tier 1 reserve (25s, env: `OUROBOROS_TIER1_MIN_RESERVE_S`).
4. **Minimum-viable fallback guard** — `_call_fallback` skips API call if remaining < 10s (`OUROBOROS_MIN_VIABLE_FALLBACK_S`), raising immediately with diagnostic message instead of a doomed timeout.

**How to apply:** `DOUBLEWORD_REALTIME_ENABLED` defaults to `true` (opt-out via `=false`). Webhook requires cloud relay or tunnel for DW to reach local EventChannelServer. Set `DOUBLEWORD_WEBHOOK_SECRET` to enable Tier 1.
