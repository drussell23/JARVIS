---
title: Project Aegis Shields Up Corrected Diagnosis
modules: [tests/governance/test_payload_transport_heuristic.py]
status: historical
source: project_aegis_shields_up_corrected_diagnosis.md
---

The long-held "re-arming aegis ruptures the DW SSE stream → fsm_exhausted, zero state=applied" belief (encoded in `docker-compose.gcp.yml` comments) was **MISATTRIBUTED**. Corrected 2026-06-20 by an isolated soak on GCP node `jarvis-ouroboros-soak-20260619-230622` (aegis ON + RT transport, batch off).

**The RT/SSE-through-aegis path is HEALTHY** and achieves autonomous `state=applied` **with the security proxy actively defending**:
- `[AegisForward] credential injection: /v1/chat/completions key=sha256:90f571d4 scheme=header_bearer`
- `upstream_status=200` (zero 4xx, zero 5xx at aegis→DW boundary)
- zero `Unclosed connection` (#69590 leak fix holds)
- `rupture_risk=False` throughout — NO SSE rupture (don't grep `rupture` loosely — telemetry lines `rupture_risk=False` / "unless it ruptures" are false positives)
- `op-019ee41b-700c`: GENERATE `outcome=ok` → `LEDGER_TERMINAL state=applied`
- boot `/v1/models 401`s = transient "credential proxy warming", self-heal to 200 in ~3 min (fail-soft, NOT a generation failure)
- Structural proof: `env_scrub` pops `DOUBLEWORD_API_KEY` when aegis on → that op could ONLY reach DW through the proxy.

**The REAL weak link** (what regressed the earlier *dual* soak): the **batch-through-aegis-passthrough** path — dynamic transport router sending heavy ops to DW `/v1/batches`. NOT the RT SSE path. So an SSE-pipeline rewrite (zero-buffering / keep-alive armor) would fix a non-problem. Batch-through-aegis is the tracked follow-up (own fix + soak).

**LOAD-BEARING config fact:** flipping `JARVIS_AEGIS_ENABLED=true` + `JARVIS_DW_DYNAMIC_TRANSPORT_ENABLED=false` is NOT enough to pin RT. Both force-batch triggers default ON in `doubleword_provider.py`: `_SLICE36_FORCE_BATCH_ENV` (`JARVIS_DW_FORCE_BATCH_STANDARD_COMPLEX`) default `"1"` (line ~853), and `_force_batch_on_breaker_enabled` (`JARVIS_DW_FORCE_BATCH_ON_CLAUDE_BREAKER`) default `"true"` (line ~386). With `JARVIS_PROVIDER_CLAUDE_DISABLED=true`, defaults force STANDARD/COMPLEX down the batch-through-aegis path. MUST pin both to `0` to reproduce the proven RT lane.

PR **#69603** (branch `fleet/aegis-shields-up`) made the shields-up config durable: aegis=true, dynamic=false, force-batch=0. **SUPERSEDED + CLOSED** — operator rejected the force-batch pins as a "tourniquet" (neuters autonomous scaling to massive refactors).

## Sovereign Aegis Batch-Passthrough Matrix (PR #69604, branch fleet/aegis-batch-passthrough-matrix, 2026-06-20)
Real fix replacing the tourniquet. **batch-through-aegis was NEVER ruptured** — a forced-batch isolation soak proved the full proxied lifecycle (upload→create→poll→retrieve→state=applied) clean with shields up. The only blocker on MASSIVE payloads was SIZE: THREE stacked ceilings 413'd a big batch JSONL:
1. **aiohttp `web.Application` default `client_max_size`=1MB** — the TRUE outermost ceiling, enforced by the server BEFORE handler code, hit first (the non-obvious one; `request._client_max_size` propagates via `Application(client_max_size=)` → `_make_request`, confirmed working — but TestServer/stub upstreams keep the 1MB default, which masked it in tests).
2. Aegis passthrough body cap default 4MB.
3. DW provider upload preflight default 5MB.
The fix (4 files): `request_body.py` `stream_body_capped()` (constant-memory async-gen forward) + `content_length_hint()` (early clean 413); `passthrough.py` streaming forward path default-on (`JARVIS_AEGIS_STREAM_PASSTHROUGH` kill switch → buffered fallback), cap 4→64MB; `daemon.py` `web.Application(client_max_size=cap+1MB)`; `doubleword_provider.py` preflight 5→64MB. INVARIANT: provider cap ≤ aegis cap ≤ aiohttp ceiling. Compose: AEGIS=true + DYNAMIC=true (router ARMED, force-batch pins GONE) + explicit 64MB caps. 274 aegis+slice37 tests green; routing already covered by `tests/governance/test_payload_transport_heuristic.py` (test_large_file_batches ≥400→batch).
**LIVE-VALIDATED on GCP node (heavy soak, dynamic armed):** autonomous op-019ee444 self-routed to batch (`File upload START payload=18730 bytes`), through AegisPassthrough /v1/files→/v1/batches→poll×7→/v1/files/{id}/content all upstream 200, `Batch 2a00461e completed` (await 31s), `LEDGER_TERMINAL state=applied`, 4xx=0/5xx=0/toolarge=0. Live op was 18KB (integration proof); >4MB capacity proven by units. Node deploy: tree is git repo (needs `safe.directory`); `git fetch origin <branch>` populates FETCH_HEAD not origin/<branch> → use `git reset --hard FETCH_HEAD`; code baked in image (build `docker/Dockerfile.soak`, only `.jarvis` bind-mounted) so must rebuild after reset; seed enlarged node-side to 680 lines to trip the ≥400 dynamic-batch line threshold (PR kept clean).
See [[project_containerized_dw_ov_blocker_stack]].
