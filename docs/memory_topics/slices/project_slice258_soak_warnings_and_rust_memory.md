---
title: Project Slice258 Soak Warnings And Rust Memory
modules: [backend/ml_memory_manager.py, backend/rust_extensions]
status: historical
source: project_slice258_soak_warnings_and_rust_memory.md
---

**MERGED to main** PR #69536 / squash `2c38b15814` (2026-06-16), branch `fix/soak-warnings-slice258` (deleted). Six battle-test soak warnings fixed at the root (no log-muting). 82 regression tests green. Builds on [[project_slice257_loop_starvation_complete_session]].

Fixes (file → root cause):
1. `persistent_intelligence_manager._init_cloud_adapter` — called non-existent `CloudDatabaseAdapter.create()` (boot abort every run). Now routes through the canonical `cloud_database_adapter.get_database_adapter()` (singleton + Cloud-SQL ReadinessGate instant-SQLite-fallback + timeout).
2. `git_momentum.compute_recent_momentum_async` — used `asyncio.create_subprocess_exec`, which **forks git on the event-loop thread** (same 67s trap as Slice 257 commit_ratios). Now thread-offloads the bounded sync `compute_recent_momentum` via a dedicated `_get_git_read_executor()` (2-worker pool, `shutdown_git_read_executor()` for teardown). `strategic_direction` gained `_extract_git_themes_async` (off-loop twin) which `load()` now `await`s; sync `_extract_git_themes` kept for back-compat tests. **General lesson: `create_subprocess_exec` is a fork-on-loop trap — always thread-offload `subprocess.run` for git/subprocess from the big organism process.**
3. aegis `daemon.py` aiohttp `DeprecationWarning: Changing state of started application` — `app[_K_PSK_CONSUMED]` was reassigned (bool) at request time on the frozen Application mapping. Fixed: store a one-element box `app[_K_PSK_CONSUMED]={"v":False}` at creation, mutate `["v"]` at runtime (readers at lines ~273/297/320). Aegis tests check behavior via HTTP, not storage shape → safe.
4. `semantic_triage.verify_model` — `/v1/models` 401 because the probe outraces the Aegis credential proxy at boot (200 moments later via AegisPassthrough). Added bounded async retry on transient 401/403/429/5xx (`_TRIAGE_VERIFY_MAX_ATTEMPTS`=4, `_TRIAGE_VERIFY_BACKOFF_S`=1.5, env-tunable); 404 = real config error, no retry.
5. `embedding_service._load_model` — `❌ Insufficient memory` was logged at ERROR for a by-design graceful degradation. Downgraded to accurate WARNING. The denial itself is CORRECT (real pressure; machine was 77% used / ~4GB free of 17GB; `ProactiveResourceGuard` protects a 2GB floor + 800MB est).
- `/v1/models 200` and `DiscordGateway armed` are healthy INFO, not faults.

**RUST MEMORY CRATES — can they help? NO (for this pressure event).** Operator asked. Findings:
- `jarvis_rust_extensions` (Cargo, pyo3 0.20, cdylib) provides `RustMemoryMonitor` (`get_memory_stats`/`is_memory_available`/`suggest_cleanup`/`update_ml_stats`) + `RustModelLoader`. Imported by `backend/ml_memory_manager.py` (`MLMemoryManager`, `self.rust_monitor = RustMemoryMonitor()`).
- **It is NOT built** — `python3 -c "import jarvis_rust_extensions"` → `ModuleNotFoundError`. So `ml_memory_manager` runs its `RUST_AVAILABLE=False` Python fallback; `RustMemoryMonitor` is DORMANT. (Other Rust crates ARE built: `jarvis_rust_core`, `jarvis_performance.so`, chromadb/safetensors/watchfiles — just not this one.)
- Neither `embedding_service`, `proactive_resource_guard`, nor `memory_budget_broker` references `MLMemoryManager`/the Rust monitor — they use psutil.
- Even if built, `RustMemoryMonitor` reads the SAME OS numbers via `sysinfo` that psutil reads — it can't create RAM. It would NOT change the denial.
- **Real future enhancement (separate project, needs Rust toolchain):** build `backend/rust_extensions` (maturin), then wire `MLMemoryManager`/`RustMemoryMonitor.suggest_cleanup()` into the embedding load path so the guard EVICTS other ML models to free room (adaptive) instead of waiting 30s then failing — and use `update_ml_stats` for accurate model accounting vs the hardcoded 800MB estimate. That's the genuine "leverage the Rust" win, but it's build+integration work, not a fix for transient host memory pressure.
