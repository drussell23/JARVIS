---
title: Project Slice259 Adaptive Embedding Tiering
modules: [backend/core/embedding_service.py, tests/core/test_slice259_adaptive_embedding_tiering.py]
status: historical
source: project_slice259_adaptive_embedding_tiering.md
---

**MERGED to main** PR #69537 / squash `b46ec23fd6` (2026-06-16). Builds on [[project_slice258_soak_warnings_and_rust_memory]] (the EmbeddingService "Insufficient memory" warning). PRD §3.9 (v3.28→3.29). `backend/core/embedding_service.py`; 11 engine tests `tests/core/test_slice259_adaptive_embedding_tiering.py` + 14 existing embedding/broker/fallback green.

**The physics (settled with operator):** memory denial is fixed by shrinking the model's FOOTPRINT, NOT by the memory monitor and NOT by ARM64 assembly. The repo's `backend/core/arm64_simd_asm.s` (`arm64_dot_product`/`l2_norm`/`matvec`/`softmax`) is a COMPUTE/NEON lever — speeds vector math, never shrinks RAM. `RustMemoryMonitor` only measures + isn't built. The lever already in-repo: **fastembed** (ONNX Runtime + CoreML/ARM64, `BAAI/bge-small-en-v1.5`, ~200MB, 384-dim — dimension-compatible with `all-MiniLM-L6-v2`), via the Slice 153 `_FastembedSTAdapter`. ONNX RT ships ARM64/CoreML kernels → LITE gets asm-grade speed free.

**Engine** (`EmbeddingTier` IntEnum HIGH=2/LITE=1/NONE=0):
- Phase 1: `fastembed>=0.3.0` added to primary `requirements.txt` (was only in `requirements-semantic.txt`). Installed on M1 (rides pinned `onnxruntime==1.19.2`; arm64 `py_rust_stemmers`). `fastembed 0.8.0` installed; adapter encodes 384-dim in ~9s cold.
- Phase 2 (demotion): `_load_model` → `_try_load_pytorch_tier` (broker OR guard gate at `pytorch_estimate_mb`); on denial/torch-absence → `_try_load_fastembed_tier` (gate `_lite_headroom_available` = `fastembed_estimate_mb`+`lite_floor_gb`). LIVE-VALIDATED: HIGH-denied → real fastembed loaded 1.2s → 384-dim encodes.
- Phase 3 (promotion / blindspot armor): `_promotion_loop` bg poller (`promotion_poll_s` 60s) while LITE → `maybe_promote_tier()`: needs `_pytorch_headroom_available()` (= `pytorch_estimate_mb`+`promotion_headroom_gb`) for `promotion_stable_checks` consecutive ticks (HYSTERESIS, default 2) AND gate re-grant (short 2s timeout) → loads PyTorch, ATOMIC swap under `_model_lock` (in-flight encode never sees half-loaded), frees LITE. Climbs back to full fidelity; never permanently degraded. Loop cancelled in `_async_cleanup`/`_sync_cleanup`.
- Seams for tests: `_load_sentence_transformer()` (mock to avoid torch), `_make_fastembed_model(factory=)`, `_check_memory_budget(component=,estimated_mb=,timeout=)` parameterized, `_available_gb()` (guard.get_memory_info → psutil fallback).
- Env knobs (NO hardcoding): `JARVIS_EMBEDDING_ADAPTIVE_TIERING`/`_ADAPTIVE_PROMOTION`, `EMBEDDING_{PYTORCH,FASTEMBED}_ESTIMATE_MB`, `EMBEDDING_PROMOTION_{POLL_S,STABLE_CHECKS,HEADROOM_GB}`, `EMBEDDING_LITE_FLOOR_GB`. Observability: `active_tier`/`tier_name`/`tier_status()`/`get_stats()["active_tier"]`/`tier_transitions`.

**Test-construct an EmbeddingService (singleton):** `es.EmbeddingService._instance=None; svc=es.EmbeddingService(config=cfg)`; force legacy path by `monkeypatch.setattr(memory_budget_broker,'get_memory_budget_broker',lambda:None)`. Demotion tests should set `promotion_enabled=False` to avoid a dangling `_promotion_loop` task under `asyncio.run`.

**Deferred (real future Rust win):** `rust_performance/.../ml/quantized_inference.rs` ("INT8 ... 4× memory reduction") = torch-free native embedding — needs crate built (maturin) + a BERT/transformer runtime on top of the int8 matmul primitives. Not worth it while fastembed/ONNX delivers the footprint win.
