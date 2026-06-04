"""Slice 84 — DW coder-fleet end-to-end unblock (transport realignment).

Root cause of the v44-v64 "DW down" blocker, found by direct boundary probes
2026-06-03/04: it was NEVER DW being down. Three compounding JARVIS-side defects
starved the strong DW coders the moment Slice 83 ranked them first:

1. **TTFT cap blindness** (`candidate_generator._is_heavy_model`). The adaptive
   primary-timeout widening (2.5× → 75-150s TTFT runway) only matched the
   markers ``("397B", "Kimi")``. DeepSeek-V4-Pro (1000B) and GLM-5.1 (754B) —
   the top-ranked coders — were NOT matched, so they got the bare 30s
   ``_PRIMARY_MAX_TIMEOUT_S`` cap and were killed at elapsed=30.01s before first
   token on a complex SWE-bench prompt+tool-loop. Direct probe: bare-model TTFT
   1.4s; through JARVIS prompt+Venom loop > 30s. Confirmed live: extending the
   markers gave DeepSeek-V4-Pro a 150s cap → it streamed + ran the full tool
   loop + produced a candidate for $0.0031.

2. **reasoning_effort=high stream rupture** (`doubleword_provider`). Direct
   probe: DeepSeek-V4-Pro streaming at effort=none/low/medium = clean; at
   effort=high = ``ClientPayloadError: TransferEncodingError`` (DW serving
   ruptures the chunked stream). ``heavy_code``/``architectural`` ops map to
   "high" → rupture → mislabeled live_transport. Clamp the max effort sent to
   DW so it never sends the unserveable "high".

3. **RT tool records not attached** (`doubleword_provider._generate_realtime`).
   ClaudeProvider returns ``result.with_tool_records(tool_records)``; the DW RT
   path ran the loop (search_code/read_file) but never attached the records, so
   ``GenerationResult.tool_execution_records`` stayed empty → the Iron Gate
   exploration gate saw 0/2 and rejected every DW candidate.

The fix is principled + no-hardcoding: param-aware "heavy" (reuse Slice 82's
catalog param resolver), an ordered effort clamp, and the one-line records
attach. NOT the runbook's "non-blocking Aegis streaming" — `aegis/forwarding.py`
already streams chunk-by-chunk via ``iter_any()``; the 30s was JARVIS's own TTFT
cap, not an Aegis socket limit (verified by raising the Aegis sock_read 12× with
no effect).
"""
from __future__ import annotations

import inspect

from backend.core.ouroboros.governance import candidate_generator as cg
from backend.core.ouroboros.governance import doubleword_provider as dw


# --- Phase 1: param-aware heavy-model detection ---

def test_strong_coders_are_heavy_via_param_count(monkeypatch):
    # No marker env — must qualify purely on parameter count (Slice 82 catalog).
    monkeypatch.delenv("JARVIS_ADAPTIVE_HEAVY_MODEL_MARKERS", raising=False)
    monkeypatch.delenv("JARVIS_HEAVY_MODEL_MIN_PARAMS_B", raising=False)
    assert cg._is_heavy_model("deepseek-ai/DeepSeek-V4-Pro") is True
    assert cg._is_heavy_model("zai-org/GLM-5.1-FP8") is True
    assert cg._is_heavy_model("moonshotai/Kimi-K2.6") is True


def test_small_fast_model_is_not_heavy(monkeypatch):
    # Qwen35B is the cheap fast-path model — must NOT get the heavy runway.
    monkeypatch.delenv("JARVIS_ADAPTIVE_HEAVY_MODEL_MARKERS", raising=False)
    monkeypatch.delenv("JARVIS_HEAVY_MODEL_MIN_PARAMS_B", raising=False)
    assert cg._is_heavy_model("Qwen/Qwen3.5-35B-A3B-FP8") is False


def test_legacy_markers_still_match(monkeypatch):
    # The curated marker fast-path must remain (397B / Kimi).
    monkeypatch.delenv("JARVIS_ADAPTIVE_HEAVY_MODEL_MARKERS", raising=False)
    assert cg._is_heavy_model("Qwen/Qwen3.5-397B-A17B-FP8") is True


def test_heavy_param_threshold_env_tunable(monkeypatch):
    monkeypatch.delenv("JARVIS_ADAPTIVE_HEAVY_MODEL_MARKERS", raising=False)
    # raise threshold above GLM's 754B → GLM no longer heavy via params
    monkeypatch.setenv("JARVIS_HEAVY_MODEL_MIN_PARAMS_B", "800")
    assert cg._is_heavy_model("zai-org/GLM-5.1-FP8") is False
    # DeepSeek-V4-Pro (1000B) still clears 800
    assert cg._is_heavy_model("deepseek-ai/DeepSeek-V4-Pro") is True


def test_unresolvable_model_is_not_heavy_by_param_path(monkeypatch):
    monkeypatch.delenv("JARVIS_ADAPTIVE_HEAVY_MODEL_MARKERS", raising=False)
    # no param, no marker → not heavy (fail-soft)
    assert cg._is_heavy_model("acme/MysteryModel-v1") is False


def test_heavy_model_gets_widened_primary_timeout(monkeypatch):
    # End-to-end: the strong coder must receive MORE than the bare 30s cap.
    monkeypatch.delenv("JARVIS_ADAPTIVE_HEAVY_MODEL_MARKERS", raising=False)
    monkeypatch.delenv("JARVIS_HEAVY_MODEL_MIN_PARAMS_B", raising=False)
    fn = cg.CandidateGenerator._compute_primary_budget
    base = fn(total_s=600.0, model_id="Qwen/Qwen3.5-35B-A3B-FP8")
    heavy = fn(total_s=600.0, model_id="deepseek-ai/DeepSeek-V4-Pro")
    assert heavy > base, "DeepSeek-V4-Pro must get a wider TTFT runway than the 35B"
    assert heavy > cg._PRIMARY_MAX_TIMEOUT_S


# --- Phase 2: reasoning_effort clamp (never send the unserveable "high") ---

def test_high_effort_is_clamped_to_serveable(monkeypatch):
    monkeypatch.delenv("JARVIS_DW_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("JARVIS_DW_MAX_REASONING_EFFORT", raising=False)
    # heavy_code/architectural map to "high" — must be clamped (high ruptures DW)
    assert dw._reasoning_effort_for("heavy_code") != "high"
    assert dw._reasoning_effort_for("architectural") != "high"


def test_clamp_does_not_raise_low_efforts(monkeypatch):
    monkeypatch.delenv("JARVIS_DW_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("JARVIS_DW_MAX_REASONING_EFFORT", raising=False)
    # trivial/simple stay "none"; complex stays "medium" (all serveable)
    assert dw._reasoning_effort_for("trivial") == "none"
    assert dw._reasoning_effort_for("complex") == "medium"


def test_clamp_env_tunable(monkeypatch):
    monkeypatch.delenv("JARVIS_DW_REASONING_EFFORT", raising=False)
    monkeypatch.setenv("JARVIS_DW_MAX_REASONING_EFFORT", "low")
    assert dw._reasoning_effort_for("complex") == "low"   # medium clamped to low
    assert dw._reasoning_effort_for("heavy_code") == "low"


def test_explicit_override_still_wins(monkeypatch):
    # JARVIS_DW_REASONING_EFFORT is the operator kill-switch — wins over clamp.
    monkeypatch.setenv("JARVIS_DW_REASONING_EFFORT", "none")
    assert dw._reasoning_effort_for("heavy_code") == "none"


# --- Phase 3: DW RT path attaches tool records (Iron Gate exploration count) ---

def test_rt_path_attaches_tool_records():
    src = inspect.getsource(dw.DoublewordProvider._generate_realtime)
    assert "with_tool_records(" in src, (
        "RT path must attach tool_records so the Iron Gate exploration gate "
        "counts read_file/search_code calls (mirrors ClaudeProvider)"
    )


def test_generation_result_with_tool_records_roundtrips():
    from backend.core.ouroboros.governance.op_context import GenerationResult

    class _Rec:
        def __init__(self, name):
            self.tool_name = name

    r = GenerationResult(
        candidates=(), provider_name="doubleword", generation_duration_s=0.0,
    )
    recs = (_Rec("search_code"), _Rec("read_file"))
    r2 = r.with_tool_records(recs)
    assert r2.tool_execution_records == recs
    # exploration count would now see 2 read_file/search_code records
    explore = sum(
        1 for rec in r2.tool_execution_records
        if rec.tool_name in {"read_file", "search_code", "get_callers"}
    )
    assert explore == 2
