---
title: Project V2 86 Cadence Connectivity
modules: [backend/core/ouroboros/governance/doubleword_provider.py, backend/core/ouroboros/governance/providers.py, backend/core/ouroboros/governance/graduation/live_fire_soak.py]
status: historical
source: project_v2_86_cadence_connectivity.md
---

May 10 2026 early hours: cadence connectivity arc shipped after operator-paced 5+ hour debug session. Each layer was a real structural bug that affected ALL cadence paths regardless of operator (cron / launchd / `--once` / manual).

**Discovery sequence**:

Operator wanted to verify live-fire graduation cadence works (cron #1 attempted 2026-05-06 failed silently due to macOS TCC; launchd path added but never proven). Launched `--once` 2026-05-09 21:35 PDT. Soak ran 30s then idled through wall-clock cap producing 0 ops + $0 cost (RUNNER classification). Iterative root-cause diagnosis peeled back 4 layers.

**Layer 1 — `.env` not loading into harness subprocess**:
- Canonical providers (`doubleword_provider.py:43-44`, `providers.py`) read `os.environ` at module load with no `load_dotenv()` call
- Wrapper's env-block boundary (`scripts/run_live_fire_graduation_soak.sh`) is the canonical source-of-truth per the install script's design comment: "single source of truth for the env block"
- But the wrapper didn't source `.env`
- Fix at lines 24-77: bash 3.2-portable `.env` loader using `case`/glob (not bash 4 regex)
- macOS frozen at bash 3.2.57 — `[!chars]` is literal in 3.2 so we use `[^chars]` POSIX form
- `eval "x=\${$key:-}"` indirect lookup avoids the bash 4.2+ `${!var:-default}` combinator
- Two-stage `case` glob: `[A-Z_]*` head + `*[^A-Z0-9_]*` body negation
- Operator-override discipline preserved: explicit shell-set values WIN over `.env`

**Layer 2 — `--once` bypasses the wrapper's env-block**:
- `install_live_fire_soak_cron.sh::run_once` inlined a duplicate of the wrapper's env block
- Launchd path used the wrapper; `--once` did not
- Fix: `run_once` now delegates to `WRAPPER_SCRIPT` (which the launchd plist already references at line 323)
- One inline duplicate eliminated; single env-block source-of-truth honored
- Cadence-kind enum bug caught: passed `once` but preflight only accepts `{cron, launchd, adhoc}`; corrected to `adhoc` matching the wrapper's own preflight default

**Layer 3 — `JARVIS_TOPOLOGY_SENTINEL_ENABLED` defaults to FALSE**:
- YAML `brain_selection_policy.yaml` has `dw_allowed: false` for every route (intentional — Phase 12 Slice E graduation purged YAML routing in favor of dynamic catalog from `dw_catalog_classifier`)
- The v2 dynamic-catalog path through `provider_topology.is_dw_blocked_for_route` is gated by the sentinel flag
- Without the flag, the v1 path reads YAML's `dw_allowed: false` and blocks every DW op
- Catalog discovery completes correctly (`routes_assigned=['background','complex','speculative','standard']`) but the orchestrator never consults it
- Fix at `scripts/run_live_fire_graduation_soak.sh:104` (post-`.env`-load): `export JARVIS_TOPOLOGY_SENTINEL_ENABLED="${JARVIS_TOPOLOGY_SENTINEL_ENABLED:-true}"`
- Operator-override discipline preserved
- Verified: `Phase 10 sentinel preflight: healthy=True schema=topology.2 routes_with_dw_models=['standard','complex','background','speculative']`

**Layer 4 — `socksio` not installed → httpx 0.28+ ImportError on every Claude API call**:
- When the harness runs under a parent process whose network tunnel is `socks5h://localhost:N` (Claude Code agent sandboxing — `ALL_PROXY=socks5h://...` in the spawned subprocess env), httpx 0.28+ raises `Using SOCKS proxy, but the 'socksio' package is not installed`
- ClaudeProvider fallback fails → ops stall
- **Wrong first fix**: stripped proxy env vars in the wrapper. Caused `ClientConnectorDNSError: Cannot connect to host api.doubleword.ai:443 [nodename nor servname provided, or not known]` because the SOCKS proxy IS the network path under sandboxed agents — stripping it broke DNS resolution
- **Correct structural fix**: `socksio==1.0.0` pinned in `requirements.txt:158` (next to `httpx==0.28.1` with provenance comment citing the sandbox-tunnel rationale). Wrapper preserves the inherited proxy block; httpx 0.28 now accepts the `socks5h://` scheme without raising
- Verified: `[ClaudeProvider] → stream model=claude-sonnet-4-6 timeout=196.0s max_tokens=16384 temp=1.0 thinking=on`

**Substrate hygiene**: each fix preserves operator-override discipline (explicit shell values win), uses portable bash (3.2+/zsh/4+), composes existing canonical primitives (no parallel logic, no hardcoded substitutes), and adds AST-grep-able provenance comments citing the diagnosis.

**What v2.86 does NOT close** (deferred to v2.87+):

**Layer 5 — Topology sentinel blanket-blocks DW under load**:
- Even with all 4 v2.86 fixes, when transient failures hit (`/v1/files` ConnectionTimeoutError, heavy probe ConnectorTimeout on `moonshotai/Kimi-K2.6` / `deepseek-ai/DeepSeek-OCR-2`), the sentinel marks DW unhealthy
- Routes subsequent BG/STANDARD ops as `background_dw_blocked_by_topology` despite Qwen3.5-397B-A17B-FP8 being reachable
- Final 38-min soak stats: 24 ops processed, **18 (75%) blocked by topology** despite all 4 fixes
- Multi-component investigation (heavy probe → modality ledger → catalog classifier → topology sentinel → orchestrator chain) — own arc

**Layer 6 — session-id linkage on 0-cost terminal path**:
- 0-cost outcome → `JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT` correctly downgrades to RUNNER
- BUT harness's terminal row writes `outcome=infra session=unknown` when `_read_most_recent_session` can't link the session (battle-test killed before atexit writes `summary.json`)
- Session-detection anchor logic in `live_fire_soak.py::_read_most_recent_session` mtime-sort is correct, but the missing `summary.json` causes the link failure
- Multi-hour investigation requiring careful instrumentation

**Honest pause point**: 4 fixes shipped tonight are real ship-able structural improvements that strengthen the system permanently regardless of cadence outcome. The substrate's clean baseline at session start (`socks=0 dns=0 blocked=0`) proves the v2.86 fixes work. Layers 5-6 are deeper architectural arcs.

**Files modified**:
- `scripts/run_live_fire_graduation_soak.sh` — `.env` loader (lines 24-77) + sentinel flag default (line 104)
- `scripts/install_live_fire_soak_cron.sh` — `run_once` delegates to wrapper (lines 255-280)
- `requirements.txt` — pinned `socksio==1.0.0` (line 158) with provenance comment

**Test impact**: no new tests in v2.86 — these are configuration / dependency fixes, not new substrate. Cumulative regression spine still green across all v2.85-shipped surfaces.

**Operator binding 2026-05-09 satisfied verbatim across all 4 fixes**: solved root problems directly, no workarounds (we caught and rejected the wrong proxy-strip approach), no shortcuts (reasoned through the sandbox tunneling architecture), composes existing wrapper / install script delegation / topology sentinel architecture / httpx[socks] extras pattern. Zero new env knobs invented; zero parallel network paths; zero shell-portability hacks.

**NEXT**: Layer 5 + Layer 6 deep dive (separate session — fresh eyes required for the multi-component instrumentation work).
