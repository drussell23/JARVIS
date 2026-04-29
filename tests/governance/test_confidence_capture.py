"""Priority 1 Slice 1 — Logprob capture primitive regression spine.

Pins the structural contract for the confidence-capture primitive
(PRD §26.5.1; scope: memory/project_priority_1_confidence_aware_execution_plan.md).

§-numbered coverage map:

  §1   Master flag JARVIS_CONFIDENCE_CAPTURE_ENABLED — default false (Slice 1)
  §2   Knobs: max_tokens + top_k FlagRegistry-typed with defensive bounds
  §3   ConfidenceToken frozen dataclass + margin computation
  §4   ConfidenceTrace frozen + provenance fields
  §5   ConfidenceCapturer bounded ring buffer + thread-safety
  §6   Capturer master-off short-circuits to no-op
  §7   Capturer never raises on malformed input (defensive normalization)
  §8   ConfidenceSummary projection arithmetic
  §9   ConfidenceSummary.to_dict() JSON shape
  §10  extract_openai_compat_logprobs_from_chunk OpenAI shape integration
  §11  extract_openai_compat_logprobs_from_chunk DW delta-nested shape
  §12  Authority invariants (no forbidden imports, pure stdlib)
  §13  No control-flow influence — pure capture (provider response unchanged)
"""
from __future__ import annotations

import ast
import inspect
import math
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.verification import confidence_capture
from backend.core.ouroboros.governance.verification.confidence_capture import (
    CONFIDENCE_CAPTURE_SCHEMA_VERSION,
    ConfidenceCapturer,
    ConfidenceSummary,
    ConfidenceToken,
    ConfidenceTrace,
    compute_summary,
    confidence_capture_enabled,
    confidence_capture_max_tokens,
    confidence_capture_top_k,
    extract_openai_compat_logprobs_from_chunk,
)


# ===========================================================================
# §1 — Master flag default true (Slice 5 graduation, was false in Slice 1)
# ===========================================================================


def test_master_flag_default_true_post_graduation(monkeypatch) -> None:
    """Slice 5 graduated default — was false in Slice 1, flipped
    to true in Slice 5. Hot-revert: explicit false-class env value."""
    monkeypatch.delenv("JARVIS_CONFIDENCE_CAPTURE_ENABLED", raising=False)
    assert confidence_capture_enabled() is True


@pytest.mark.parametrize("val", ["", " ", "  ", "\t"])
def test_master_flag_empty_default_true_post_graduation(
    monkeypatch, val,
) -> None:
    monkeypatch.setenv("JARVIS_CONFIDENCE_CAPTURE_ENABLED", val)
    assert confidence_capture_enabled() is True


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE"])
def test_master_flag_explicit_true_enables(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_CONFIDENCE_CAPTURE_ENABLED", val)
    assert confidence_capture_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "garbage"])
def test_master_flag_falsy_disables(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_CONFIDENCE_CAPTURE_ENABLED", val)
    assert confidence_capture_enabled() is False


# ===========================================================================
# §2 — Knobs
# ===========================================================================


def test_max_tokens_default(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_CONFIDENCE_CAPTURE_MAX_TOKENS", raising=False)
    assert confidence_capture_max_tokens() == 4096


def test_max_tokens_floored_at_one(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_CONFIDENCE_CAPTURE_MAX_TOKENS", "0")
    assert confidence_capture_max_tokens() == 1
    monkeypatch.setenv("JARVIS_CONFIDENCE_CAPTURE_MAX_TOKENS", "-100")
    assert confidence_capture_max_tokens() == 1


def test_max_tokens_garbage_falls_back_to_default(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_CONFIDENCE_CAPTURE_MAX_TOKENS", "garbage")
    assert confidence_capture_max_tokens() == 4096


def test_top_k_default(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_CONFIDENCE_CAPTURE_TOP_K", raising=False)
    assert confidence_capture_top_k() == 5


def test_top_k_clamped_to_provider_cap(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_CONFIDENCE_CAPTURE_TOP_K", "100")
    assert confidence_capture_top_k() == 20  # provider hard cap
    monkeypatch.setenv("JARVIS_CONFIDENCE_CAPTURE_TOP_K", "0")
    assert confidence_capture_top_k() == 1  # floor
    monkeypatch.setenv("JARVIS_CONFIDENCE_CAPTURE_TOP_K", "-5")
    assert confidence_capture_top_k() == 1


# ===========================================================================
# §3 — ConfidenceToken
# ===========================================================================


def test_confidence_token_frozen() -> None:
    tok = ConfidenceToken(token="x", logprob=-0.1)
    with pytest.raises((AttributeError, Exception)):
        tok.token = "y"  # type: ignore[misc]


def test_confidence_token_hashable() -> None:
    tok = ConfidenceToken(token="x", logprob=-0.1, top_logprobs=(("x", -0.1),))
    hash(tok)  # must not raise


def test_token_margin_with_two_alternatives() -> None:
    tok = ConfidenceToken(
        token="the",
        logprob=-0.05,
        top_logprobs=(("the", -0.05), ("a", -3.0)),
    )
    margin = tok.margin_top1_top2()
    assert margin is not None
    assert abs(margin - 2.95) < 1e-6


def test_token_margin_returns_none_with_fewer_than_two() -> None:
    assert ConfidenceToken(token="x", logprob=-0.5).margin_top1_top2() is None
    assert ConfidenceToken(
        token="x", logprob=-0.5,
        top_logprobs=(("x", -0.5),),
    ).margin_top1_top2() is None


def test_token_margin_handles_inf_gracefully() -> None:
    tok = ConfidenceToken(
        token="x",
        logprob=-0.1,
        top_logprobs=(("x", float("-inf")), ("y", -3.0)),
    )
    assert tok.margin_top1_top2() is None  # non-finite → None


# ===========================================================================
# §4 — ConfidenceTrace
# ===========================================================================


def test_confidence_trace_frozen() -> None:
    trace = ConfidenceTrace(provider="dw", model_id="x")
    with pytest.raises((AttributeError, Exception)):
        trace.provider = "claude"  # type: ignore[misc]


def test_confidence_trace_default_empty() -> None:
    trace = ConfidenceTrace()
    assert trace.tokens == ()
    assert trace.provider == ""
    assert trace.model_id == ""
    assert trace.capture_truncated is False
    assert trace.schema_version == CONFIDENCE_CAPTURE_SCHEMA_VERSION


# ===========================================================================
# §5 — ConfidenceCapturer
# ===========================================================================


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("JARVIS_CONFIDENCE_CAPTURE_ENABLED", "true")
    yield


def test_capturer_appends_when_enabled(enabled) -> None:
    cap = ConfidenceCapturer(provider="dw", model_id="m")
    assert cap.append(token="x", logprob=-0.1) is True
    assert len(cap) == 1


def test_capturer_provider_metadata_preserved(enabled) -> None:
    cap = ConfidenceCapturer(provider="doubleword", model_id="qwen-397b")
    assert cap.provider == "doubleword"
    assert cap.model_id == "qwen-397b"
    cap.append(token="x", logprob=-0.1)
    trace = cap.freeze()
    assert trace.provider == "doubleword"
    assert trace.model_id == "qwen-397b"


def test_capturer_freeze_immutable(enabled) -> None:
    cap = ConfidenceCapturer()
    cap.append(token="a", logprob=-0.1)
    cap.append(token="b", logprob=-0.2)
    trace1 = cap.freeze()
    cap.append(token="c", logprob=-0.3)
    trace2 = cap.freeze()
    # First trace not affected by subsequent appends
    assert len(trace1.tokens) == 2
    assert len(trace2.tokens) == 3


def test_capturer_bounded_ring_buffer(enabled, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_CONFIDENCE_CAPTURE_MAX_TOKENS", "3")
    cap = ConfidenceCapturer()
    for i in range(10):
        cap.append(token=str(i), logprob=-0.1)
    assert len(cap) == 3
    assert cap.capture_truncated is True
    trace = cap.freeze()
    assert trace.capture_truncated is True


def test_capturer_explicit_max_overrides_env(enabled, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_CONFIDENCE_CAPTURE_MAX_TOKENS", "100")
    cap = ConfidenceCapturer(max_tokens=2)
    cap.append(token="a", logprob=-0.1)
    cap.append(token="b", logprob=-0.2)
    cap.append(token="c", logprob=-0.3)  # dropped
    assert len(cap) == 2
    assert cap.capture_truncated is True


def test_capturer_reset_clears_state(enabled) -> None:
    cap = ConfidenceCapturer()
    cap.append(token="x", logprob=-0.1)
    assert len(cap) == 1
    cap.reset()
    assert len(cap) == 0
    assert cap.capture_truncated is False


def test_capturer_thread_safe_under_concurrent_appends(enabled) -> None:
    """RLock contract — concurrent appends preserve count."""
    import threading
    cap = ConfidenceCapturer(max_tokens=10000)
    n_threads = 4
    appends_per = 100

    def worker():
        for i in range(appends_per):
            cap.append(token=str(i), logprob=-0.1 * i)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(cap) == n_threads * appends_per


# ===========================================================================
# §6 — Master-off short-circuits
# ===========================================================================


def test_capturer_master_off_append_returns_false(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_CONFIDENCE_CAPTURE_ENABLED", "false")
    cap = ConfidenceCapturer()
    assert cap.append(token="x", logprob=-0.1) is False
    assert len(cap) == 0


def test_capturer_master_off_freeze_returns_empty(monkeypatch) -> None:
    """Even if state somehow accumulated, master-off freeze returns empty."""
    monkeypatch.setenv("JARVIS_CONFIDENCE_CAPTURE_ENABLED", "true")
    cap = ConfidenceCapturer(provider="dw", model_id="m")
    cap.append(token="x", logprob=-0.1)
    monkeypatch.setenv("JARVIS_CONFIDENCE_CAPTURE_ENABLED", "false")
    trace = cap.freeze()
    assert trace.tokens == ()
    # Provenance still preserved
    assert trace.provider == "dw"
    assert trace.model_id == "m"


# ===========================================================================
# §7 — Defensive normalization (NEVER raises)
# ===========================================================================


def test_capturer_handles_none_token(enabled) -> None:
    cap = ConfidenceCapturer()
    cap.append(token=None, logprob=-0.1)
    trace = cap.freeze()
    assert trace.tokens[0].token == ""


def test_capturer_handles_non_numeric_logprob(enabled) -> None:
    cap = ConfidenceCapturer()
    cap.append(token="x", logprob="not a number")
    trace = cap.freeze()
    assert trace.tokens[0].logprob == float("-inf")


def test_capturer_handles_nan_logprob(enabled) -> None:
    cap = ConfidenceCapturer()
    cap.append(token="x", logprob=float("nan"))
    trace = cap.freeze()
    # NaN → -inf normalization
    assert trace.tokens[0].logprob == float("-inf")


def test_capturer_handles_malformed_top_logprobs(enabled) -> None:
    cap = ConfidenceCapturer()
    # Mix of dicts, tuples, and garbage entries
    cap.append(
        token="x", logprob=-0.1,
        top_logprobs=[
            {"token": "x", "logprob": -0.1},
            ("y", -0.2),
            "garbage",  # silently skipped
            None,
            42,
        ],
    )
    trace = cap.freeze()
    # Only the well-formed dict and tuple entries land
    assert len(trace.tokens[0].top_logprobs) == 2


def test_capturer_handles_non_iterable_top_logprobs(enabled) -> None:
    cap = ConfidenceCapturer()
    cap.append(token="x", logprob=-0.1, top_logprobs=42)  # type: ignore
    trace = cap.freeze()
    assert trace.tokens[0].top_logprobs == ()


# ===========================================================================
# §8 — ConfidenceSummary projection arithmetic
# ===========================================================================


def test_compute_summary_empty_trace() -> None:
    summary = compute_summary(ConfidenceTrace())
    assert summary.token_count == 0
    assert summary.mean_top1_logprob is None
    assert summary.mean_top1_top2_margin is None


def test_compute_summary_none_input() -> None:
    summary = compute_summary(None)
    assert isinstance(summary, ConfidenceSummary)
    assert summary.token_count == 0


def test_compute_summary_arithmetic() -> None:
    trace = ConfidenceTrace(
        tokens=(
            ConfidenceToken(
                token="a", logprob=-0.1,
                top_logprobs=(("a", -0.1), ("b", -2.0)),
            ),
            ConfidenceToken(
                token="b", logprob=-0.5,
                top_logprobs=(("b", -0.5), ("c", -1.0)),
            ),
        ),
        provider="dw", model_id="m",
    )
    summary = compute_summary(trace)
    assert summary.token_count == 2
    assert summary.has_alternatives_count == 2
    # mean top1: (-0.1 + -0.5) / 2 = -0.3
    assert abs(summary.mean_top1_logprob - (-0.3)) < 1e-6
    # margins: 1.9, 0.5 → mean=1.2, min=0.5, max=1.9
    assert abs(summary.mean_top1_top2_margin - 1.2) < 1e-6
    assert abs(summary.min_top1_top2_margin - 0.5) < 1e-6
    assert abs(summary.max_top1_top2_margin - 1.9) < 1e-6


def test_compute_summary_no_alternatives() -> None:
    """Tokens with no top-K alternatives — top1 stats present, margin None."""
    trace = ConfidenceTrace(
        tokens=(
            ConfidenceToken(token="a", logprob=-0.1),
            ConfidenceToken(token="b", logprob=-0.2),
        ),
    )
    summary = compute_summary(trace)
    assert summary.token_count == 2
    assert summary.has_alternatives_count == 0
    assert summary.mean_top1_logprob is not None
    assert summary.mean_top1_top2_margin is None
    assert summary.min_top1_top2_margin is None
    assert summary.max_top1_top2_margin is None


def test_compute_summary_preserves_truncation_flag() -> None:
    trace = ConfidenceTrace(capture_truncated=True, provider="dw")
    summary = compute_summary(trace)
    assert summary.capture_truncated is True
    assert summary.provider == "dw"


# ===========================================================================
# §9 — ConfidenceSummary.to_dict()
# ===========================================================================


def test_summary_to_dict_full_shape() -> None:
    summary = ConfidenceSummary(
        token_count=10,
        has_alternatives_count=8,
        mean_top1_logprob=-0.3,
        mean_top1_top2_margin=1.5,
        min_top1_top2_margin=0.5,
        max_top1_top2_margin=2.5,
        capture_truncated=True,
        provider="doubleword",
        model_id="qwen",
    )
    d = summary.to_dict()
    assert d["schema_version"] == CONFIDENCE_CAPTURE_SCHEMA_VERSION
    assert d["token_count"] == 10
    assert d["mean_top1_logprob"] == -0.3
    assert d["capture_truncated"] is True
    assert d["provider"] == "doubleword"


def test_summary_to_dict_handles_none_metrics() -> None:
    summary = ConfidenceSummary()
    d = summary.to_dict()
    assert d["token_count"] == 0
    assert d["mean_top1_logprob"] is None
    assert d["mean_top1_top2_margin"] is None


# ===========================================================================
# §10-§11 — extract_openai_compat_logprobs_from_chunk
# ===========================================================================


def test_extract_openai_choice_level_shape() -> None:
    """Standard OpenAI streaming shape — logprobs on the choice."""
    chunk = {
        "choices": [{
            "logprobs": {
                "content": [{
                    "token": "the",
                    "logprob": -0.05,
                    "top_logprobs": [
                        {"token": "the", "logprob": -0.05},
                        {"token": "a", "logprob": -3.0},
                    ],
                }],
            },
        }],
    }
    out = extract_openai_compat_logprobs_from_chunk(chunk)
    assert len(out) == 1
    tok, lp, top = out[0]
    assert tok == "the"
    assert lp == -0.05


def test_extract_openai_delta_nested_shape() -> None:
    """DW shape — logprobs nested under delta."""
    chunk = {
        "choices": [{
            "delta": {
                "content": " the",
                "logprobs": {
                    "content": [{
                        "token": " the",
                        "logprob": -0.1,
                        "top_logprobs": [
                            {"token": " the", "logprob": -0.1},
                        ],
                    }],
                },
            },
        }],
    }
    out = extract_openai_compat_logprobs_from_chunk(chunk)
    assert len(out) == 1


def test_extract_returns_empty_on_missing_choices() -> None:
    assert extract_openai_compat_logprobs_from_chunk({}) == ()
    assert extract_openai_compat_logprobs_from_chunk({"choices": []}) == ()


def test_extract_returns_empty_on_no_logprobs() -> None:
    chunk = {"choices": [{"delta": {"content": "x"}}]}
    assert extract_openai_compat_logprobs_from_chunk(chunk) == ()


def test_extract_returns_empty_on_malformed_input() -> None:
    """NEVER raises on garbage."""
    assert extract_openai_compat_logprobs_from_chunk(None) == ()
    assert extract_openai_compat_logprobs_from_chunk("not a dict") == ()
    assert extract_openai_compat_logprobs_from_chunk(42) == ()
    assert extract_openai_compat_logprobs_from_chunk(
        {"choices": "not a list"},
    ) == ()


def test_extract_handles_non_iterable_content() -> None:
    chunk = {
        "choices": [{
            "logprobs": {"content": "not a list"},
        }],
    }
    assert extract_openai_compat_logprobs_from_chunk(chunk) == ()


# ===========================================================================
# §12 — Authority invariants (AST-pinned)
# ===========================================================================


_FORBIDDEN_IMPORTS = (
    "backend.core.ouroboros.governance.orchestrator",
    "backend.core.ouroboros.governance.phase_runners",
    "backend.core.ouroboros.governance.candidate_generator",
    "backend.core.ouroboros.governance.iron_gate",
    "backend.core.ouroboros.governance.change_engine",
    "backend.core.ouroboros.governance.policy",
    "backend.core.ouroboros.governance.semantic_guardian",
    "backend.core.ouroboros.governance.semantic_firewall",
    "backend.core.ouroboros.governance.providers",
    "backend.core.ouroboros.governance.doubleword_provider",
)


def test_authority_no_forbidden_imports() -> None:
    src = Path(inspect.getfile(confidence_capture)).read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for forbidden in _FORBIDDEN_IMPORTS:
                    assert forbidden not in alias.name, (
                        f"forbidden import: {alias.name}"
                    )
        elif isinstance(node, ast.ImportFrom) and node.module:
            for forbidden in _FORBIDDEN_IMPORTS:
                assert forbidden not in node.module, (
                    f"forbidden import: {node.module}"
                )


def test_authority_pure_stdlib_only() -> None:
    """Slice 1 primitive imports only stdlib + typing."""
    src = Path(inspect.getfile(confidence_capture)).read_text()
    tree = ast.parse(src)
    allowed_roots = {
        "logging", "math", "os", "threading", "time",
        "dataclasses", "typing", "__future__",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root in allowed_roots, (
                    f"non-stdlib import: {alias.name}"
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            assert root in allowed_roots, (
                f"non-stdlib import: {node.module}"
            )


# ===========================================================================
# §13 — No control-flow influence (pure capture)
# ===========================================================================


def test_capture_does_not_mutate_input_chunk() -> None:
    """Extractor MUST NOT modify the chunk dict (read-only on stream)."""
    chunk = {
        "choices": [{
            "delta": {"content": "x"},
            "logprobs": {
                "content": [{
                    "token": "x",
                    "logprob": -0.1,
                    "top_logprobs": [{"token": "x", "logprob": -0.1}],
                }],
            },
        }],
    }
    chunk_copy = {**chunk, "choices": [{**chunk["choices"][0]}]}
    extract_openai_compat_logprobs_from_chunk(chunk)
    # Top-level structure unchanged
    assert "choices" in chunk
    assert chunk["choices"][0]["delta"]["content"] == "x"


def test_append_does_not_mutate_input_top_logprobs(enabled) -> None:
    cap = ConfidenceCapturer()
    original = [
        {"token": "x", "logprob": -0.1},
        {"token": "y", "logprob": -0.2},
    ]
    cap.append(token="x", logprob=-0.1, top_logprobs=original)
    # Original list unchanged
    assert len(original) == 2
    assert original[0]["token"] == "x"
