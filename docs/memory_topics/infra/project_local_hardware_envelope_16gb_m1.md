---
title: Project Local Hardware Envelope 16Gb M1
modules: [backend/core/ouroboros/governance/background_agent_pool.py, backend/core/ouroboros/governance/candidate_generator.py]
status: historical
source: project_local_hardware_envelope_16gb_m1.md
---

PRD §47 added 2026-05-26 (PR #59073 squash-merged fb7a11a136). Answers operator's question "for my local 16GB M1 Mac, how many agents can O+V deploy or spawn?" with empirical numbers from bt-2026-05-26-184355 PURE-DW v15 soak.

**6 concurrency surfaces** (each separate dimension):
1. BackgroundAgentPool workers — default 3 (`JARVIS_BG_POOL_SIZE`, `background_agent_pool.py:284`)
2. SubagentScheduler graphs — default 2 (`JARVIS_SUBAGENT_MAX_GRAPHS`, `autonomy/subagent_scheduler.py:305`)
3. MAX_PARALLEL_SCOPES per op — constant 3 (`candidate_generator.py:3928`)
4. AST helper ProcessPool — default 1 (`JARVIS_AST_HELPER_POOL_MAX_WORKERS`)
5. 18 intake sensors — file-watch pollers (cheap)
6. Aegis daemon — 1 separate process when `JARVIS_AEGIS_ENABLED=true` (~200 MB)

**Hard envelope on 16 GB M1**: ProcessMemoryWatchdog auto-derives cap = `psutil.virtual_memory().total × 0.75` = **12,288 MB**; warn = 10,445 MB. Empirically `cap=12288MB warn=10445MB` from bt-2026-05-26-184355/debug.log.

**Peak RSS observed in v15**: 1,927 MB (~12-16% of cap). Practical headroom after macOS slop: 6-7 GB.

**Net answer**: ~34 asyncio tasks+subprocesses at max fan-out, ~8-12 doing real work simultaneously. Binding constraint is **provider cost (~$0.005-0.03/op), NOT local RAM/CPU/process count**. Hardware overprovisioned for default workload.

**Safe scaling path**: `JARVIS_BG_POOL_SIZE=5` + `JARVIS_AST_HELPER_POOL_MAX_WORKERS=2` (additive ~700 MB) before touching `JARVIS_SUBAGENT_MAX_GRAPHS` (compound ~2,600 MB).

**Cap auto-scales** via `JARVIS_PROCESS_MEMORY_CAP_FRACTION=0.75 × total RAM` — same recommendations stay safe relative to host's actual RAM (32GB/64GB/96GB+ tiers in §47.5 table).

Related: [[project_predictive_provider_resilience]] (provider-side cost discipline), [[project_aegis_zero_trust_arc_closed]] (Aegis daemon as 6th surface), [[feedback_no_preresult_euphoria]] (§47.6 honest framing — 4-5 BG tier inferred-safe, not directly soaked).
