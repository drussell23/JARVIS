"""Phase 12 Slice F — Substrate Error Unmasking regression spine.

Pins the ground-truth-error contract:
  * DoublewordInfraError carries status_code + response_body + model_id
  * is_modality_error() / is_terminal_auth_error() / is_transient()
    classify FROM STRUCTURED FIELDS, never regex on model_id
  * candidate_generator._generate_background re-raises the structured
    exception instead of stringifying through RuntimeError(_dw_error)
  * Sentinel dispatch classifier reads status_code attribute first,
    falling back to str(exc) regex only when status_code is absent

Pins:
  §1 DoublewordInfraError carries the new structured fields
  §2 Bounded buffers — response_body capped at 1024, model_id capped at 128
  §3 is_modality_error: 4xx + body marker required (NOT regex on id)
  §4 is_modality_error: 4xx alone with no marker → False (conservative)
  §5 is_modality_error: status_code outside 400/404/422 → False
  §6 is_terminal_auth_error: 401 / 403 → True
  §7 is_transient: 429/5xx + non-HTTP → True; modality 4xx → False
  §8 _generate_background re-raises structured exception (preserves
     status_code through the dispatcher chain)
  §9 _generate_background non-structured exceptions still raise
     RuntimeError (legacy path preserved)
  §10 Sentinel dispatch classifier prefers structured status_code over
      str(exc) regex match (HTTP 503 in body but status_code=400 →
      LIVE_TRANSPORT, not LIVE_HTTP_5XX)
  §11 Modality marker bodies — observed DW + OpenAI-compat strings
  §12 No regex on model_id anywhere — Slice G enforces this; Slice F
      pins that the unmasking layer doesn't introduce it
"""
from __future__ import annotations

import inspect
from typing import Any  # noqa: F401

import pytest

from backend.core.ouroboros.governance import candidate_generator as cg
from backend.core.ouroboros.governance.doubleword_provider import (
    DoublewordInfraError,
)


# ---------------------------------------------------------------------------
# §1 — Structured fields present on DoublewordInfraError
# ---------------------------------------------------------------------------


def test_infra_error_carries_status_response_body_model_id() -> None:
    err = DoublewordInfraError(
        "Chat completions failed: 400 task mismatch",
        status_code=400,
        response_body='{"error": "model does not support chat"}',
        model_id="Qwen/Qwen3-Embedding-8B",
    )
    assert err.status_code == 400
    assert "does not support chat" in err.response_body
    assert err.model_id == "Qwen/Qwen3-Embedding-8B"


def test_infra_error_legacy_two_arg_construction_still_works() -> None:
    """Pre-Slice-F callers passing only (reason, status_code) must
    continue to work. New fields default to empty string."""
    err = DoublewordInfraError("Chat completions failed", status_code=503)
    assert err.status_code == 503
    assert err.response_body == ""
    assert err.model_id == ""


# ---------------------------------------------------------------------------
# §2 — Bounded buffers
# ---------------------------------------------------------------------------


def test_response_body_bounded_to_1024_chars() -> None:
    body = "x" * 5000
    err = DoublewordInfraError("oops", status_code=500, response_body=body)
    assert len(err.response_body) == 1024


def test_model_id_bounded_to_128_chars() -> None:
    err = DoublewordInfraError(
        "oops", status_code=500, model_id="z" * 500,
    )
    assert len(err.model_id) == 128


def test_none_inputs_coerce_to_empty_string() -> None:
    err = DoublewordInfraError(
        "oops",
        status_code=500,
        response_body=None,    # type: ignore[arg-type]
        model_id=None,         # type: ignore[arg-type]
    )
    assert err.response_body == ""
    assert err.model_id == ""


# ---------------------------------------------------------------------------
# §3 — is_modality_error: requires 4xx AND body marker (no regex on id)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body_marker", [
    "model does not support chat",
    "Not a chat model — this is an OCR specialist",
    "endpoint not supported for this model",
    "Embedding only — please use /v1/embeddings",
    "MODEL_NOT_CHAT",
    "Task mismatch: request was for chat",
    "Wrong endpoint",
    "Unsupported endpoint",
    "model is not available for chat",
])
def test_modality_error_4xx_with_marker(body_marker: str) -> None:
    """All observed DW + OpenAI-compat modality markers."""
    err = DoublewordInfraError(
        f"Chat completions failed: 400 {body_marker}",
        status_code=400,
        response_body=body_marker,
        model_id="lightonai/LightOnOCR-2-1B-bbox-soup",
    )
    assert err.is_modality_error() is True


@pytest.mark.parametrize("status", [400, 404, 422])
def test_modality_error_accepts_400_404_422(status: int) -> None:
    err = DoublewordInfraError(
        "model does not support chat",
        status_code=status,
        response_body="model does not support chat",
    )
    assert err.is_modality_error() is True


# ---------------------------------------------------------------------------
# §4 — Conservative: 4xx without marker → NOT modality
# ---------------------------------------------------------------------------


def test_modality_error_400_without_marker_is_not_terminal() -> None:
    """A 400 about bad max_tokens must NOT be classified as modality
    — that would permanently kill an otherwise-healthy model."""
    err = DoublewordInfraError(
        "Chat completions failed: 400 max_tokens out of range",
        status_code=400,
        response_body="max_tokens must be <= 8192",
        model_id="Qwen/Qwen3.5-9B",
    )
    assert err.is_modality_error() is False


def test_modality_error_400_empty_body_is_not_terminal() -> None:
    err = DoublewordInfraError(
        "Bad request",
        status_code=400,
        response_body="",
    )
    assert err.is_modality_error() is False


# ---------------------------------------------------------------------------
# §5 — Non-modality status codes never modality
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [200, 401, 403, 429, 500, 502, 503, 504])
def test_modality_error_non_4xx_modality_codes_are_false(status: int) -> None:
    """Even with a body that says 'does not support chat', if the
    status is 503, it's not a modality verdict — server is overloaded."""
    err = DoublewordInfraError(
        "oops",
        status_code=status,
        response_body="model does not support chat",
    )
    assert err.is_modality_error() is False


# ---------------------------------------------------------------------------
# §6 — is_terminal_auth_error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_terminal_auth_error_for_401_403(status: int) -> None:
    err = DoublewordInfraError("auth failed", status_code=status)
    assert err.is_terminal_auth_error() is True


@pytest.mark.parametrize("status", [400, 404, 422, 429, 500, 503, 0])
def test_terminal_auth_error_false_for_others(status: int) -> None:
    err = DoublewordInfraError("oops", status_code=status)
    assert err.is_terminal_auth_error() is False


# ---------------------------------------------------------------------------
# §7 — is_transient
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_for_5xx_and_429(status: int) -> None:
    err = DoublewordInfraError("oops", status_code=status)
    assert err.is_transient() is True


def test_transient_for_non_http_zero() -> None:
    """status_code=0 = DNS/TLS/network — conservative: assume transient."""
    err = DoublewordInfraError("connection refused", status_code=0)
    assert err.is_transient() is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_transient_false_for_modality_and_auth_4xx(status: int) -> None:
    err = DoublewordInfraError("oops", status_code=status)
    assert err.is_transient() is False


# ---------------------------------------------------------------------------
# §8 — _generate_background re-raises structured exception
# ---------------------------------------------------------------------------


def test_source_generate_background_preserves_structured_error() -> None:
    """Source-level pin: ``_generate_background`` MUST re-raise the
    structured DoublewordInfraError (or any exception with status_code)
    instead of stringifying it through RuntimeError(_dw_error).

    This is the substrate of Slice H's terminal-vs-transient breaker
    — without the structured object surviving, the breaker can't read
    status_code."""
    src = inspect.getsource(cg.CandidateGenerator._generate_background)
    # The new path must capture the original exception
    assert "_structured_error" in src
    # And re-raise it before the legacy RuntimeError fallback
    structured_idx = src.index("raise _structured_error")
    legacy_idx = src.index("raise RuntimeError(_dw_error)")
    assert structured_idx < legacy_idx, (
        "structured exception must re-raise BEFORE the legacy "
        "RuntimeError fallback so DoublewordInfraError reaches the "
        "dispatcher with status_code intact"
    )


def test_source_generate_background_captures_status_code() -> None:
    """The _dw_error string must surface status_code when available
    so legacy log-line consumers see ground truth too."""
    src = inspect.getsource(cg.CandidateGenerator._generate_background)
    assert "status_code" in src
    assert "response_body" in src


# ---------------------------------------------------------------------------
# §9 — Legacy path preserved for non-structured exceptions
# ---------------------------------------------------------------------------


def test_source_generate_background_legacy_runtime_error_path() -> None:
    """Non-structured exceptions (timeouts, empty results) still raise
    RuntimeError as before — Slice F doesn't change the shape of the
    legacy path, only adds the structured-preserve branch."""
    src = inspect.getsource(cg.CandidateGenerator._generate_background)
    # Both raise paths exist
    assert "raise _structured_error" in src
    assert "raise RuntimeError(_dw_error)" in src


# ---------------------------------------------------------------------------
# §10 — Sentinel classifier prefers structured status_code
# ---------------------------------------------------------------------------


def test_source_sentinel_dispatch_uses_status_code_first() -> None:
    """Source-level pin: the sentinel dispatch classifier MUST check
    ``getattr(exc, 'status_code', None)`` BEFORE falling back to
    str(exc) regex. Pin the ordering."""
    src = inspect.getsource(cg.CandidateGenerator._dispatch_via_sentinel)
    status_idx = src.index('"status_code"')
    # The legacy regex check ("429" in err_str) is the fallback path —
    # it must appear AFTER the structured check
    legacy_idx = src.rindex('"429" in err_str')
    assert status_idx < legacy_idx, (
        "sentinel dispatch must check structured status_code BEFORE "
        "falling back to str(exc) regex match"
    )


def test_source_sentinel_dispatch_logs_unmasked_status() -> None:
    """The unmasked status_code must appear in the WARNING log line
    so operators see ground truth in debug.log without further
    introspection. Pin source-level."""
    src = inspect.getsource(cg.CandidateGenerator._dispatch_via_sentinel)
    assert "http_%d" in src or "http_{" in src, (
        "sentinel WARNING line must include unmasked status code"
    )
    assert "body=" in src, (
        "sentinel WARNING line must include response body excerpt"
    )


# ---------------------------------------------------------------------------
# §11 — Sentinel classifier passes structured fields to report_failure
# ---------------------------------------------------------------------------


def test_source_sentinel_dispatch_forwards_structured_to_report() -> None:
    """report_failure() is called with status_code + response_body
    + is_terminal kwargs. Slice H breaker reads those to decide
    TERMINAL_OPEN vs OPEN. Slice F pre-wires this so Slice H is a
    drop-in addition on the sentinel side."""
    src = inspect.getsource(cg.CandidateGenerator._dispatch_via_sentinel)
    assert "status_code=_status_code" in src
    assert "response_body=_response_body" in src
    assert "is_terminal=" in src


def test_source_sentinel_dispatch_falls_back_on_typeerror() -> None:
    """Pre-Slice-H sentinels don't accept the new kwargs — the dispatch
    must fall back to the legacy 3-arg call on TypeError so we don't
    break any in-flight rollout."""
    src = inspect.getsource(cg.CandidateGenerator._dispatch_via_sentinel)
    assert "except TypeError" in src
    # Legacy fallback present
    fallback_window = src[src.index("except TypeError"):]
    assert "report_failure" in fallback_window[:500]


# ---------------------------------------------------------------------------
# §12 — No regex on model_id in unmasking layer
# ---------------------------------------------------------------------------


def test_no_regex_on_model_id_in_unmasking_layer() -> None:
    """Operator-mandated: capability/modality decisions MUST NOT be
    derived from regex pattern-matching on model_id (e.g. checking
    'Embedding' or 'OCR' in id). Slice F's classification reads
    status_code + response_body markers ONLY. Pin this source-level
    so a future PR can't silently re-introduce the Zero-Order shortcut."""
    src = inspect.getsource(DoublewordInfraError.is_modality_error)
    # The function must not import re or do pattern-match on model_id
    assert "self.model_id" not in src, (
        "is_modality_error must NOT read self.model_id — that would be "
        "regex-on-id, the Zero-Order shortcut the operator rejected"
    )
    assert "import re" not in src
    # It MUST read self.response_body (the server-side ground truth)
    assert "self.response_body" in src
    # And status_code (the structured field)
    assert "self.status_code" in src
