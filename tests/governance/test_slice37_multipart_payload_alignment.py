"""Slice 37 — Multipart Payload Alignment + Cleanup Discipline.

Tests:
  * Phase 1: pre-flight size guard in ``_upload_file`` rejects
    oversized payloads with HTTP 413 semantic before round-trip
  * Phase 1: payload diagnostic log line captures size + custom_id +
    model_id
  * Phase 2: explicit ``_aegis_lease`` cleanup on all failure paths
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DW_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "doubleword_provider.py"
)


def test_ast_pin_slice37_payload_diagnostic_present() -> None:
    """``_upload_file`` MUST log payload size + custom_id + model
    BEFORE the POST. Without this, HTTP 500s can't be correlated
    with payload shape from log greps."""
    src = DW_FILE.read_text()
    assert "Slice 37" in src
    assert "_payload_bytes" in src
    assert "JARVIS_DW_UPLOAD_MAX_BYTES" in src
    assert "File upload START:" in src or "File upload START " in src


def test_ast_pin_slice37_size_guard_returns_none() -> None:
    """Pre-flight oversized payload MUST set ``_last_error_status``
    to 413 (Payload Too Large semantic) and return None — fail-fast
    before the HTTP round-trip."""
    src = DW_FILE.read_text()
    tree = ast.parse(src, filename=str(DW_FILE))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_upload_file"
        ):
            body = ast.unparse(node)
            assert "_last_error_status = 413" in body
            assert "PRE-FLIGHT REJECTED" in body
            return
    pytest.fail("_upload_file not located")


def test_ast_pin_slice37_finally_lease_cleanup() -> None:
    """``_upload_file`` MUST have a try/finally where the finally
    block attempts ``_aegis_release_call_lease(_aegis_lease)`` for
    explicit per-call cleanup on every exit path."""
    src = DW_FILE.read_text()
    tree = ast.parse(src, filename=str(DW_FILE))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_upload_file"
        ):
            body = ast.unparse(node)
            assert "finally:" in body, (
                "_upload_file missing try/finally cleanup discipline"
            )
            assert "release_call_lease" in body
            return
    pytest.fail("_upload_file not located")


def test_ast_pin_slice37_error_body_capture_widened() -> None:
    """HTTP error log MUST capture 2000 chars of response body (was
    500 chars pre-Slice-37). Wider window so operators can see the
    full DW error message including any structured error code."""
    src = DW_FILE.read_text()
    # body[:2000] appears in the new error log line; body[:500] in
    # _upload_file should be REPLACED (other call sites may keep 500)
    tree = ast.parse(src, filename=str(DW_FILE))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_upload_file"
        ):
            body = ast.unparse(node)
            assert "body[:2000]" in body, (
                "_upload_file error log must capture 2000 chars of "
                "response body (Slice 37 diagnostic widening)"
            )
            return
    pytest.fail("_upload_file not located")


def test_spine_env_knob_overrides_default_max_bytes() -> None:
    """``JARVIS_DW_UPLOAD_MAX_BYTES`` env overrides the default upload guard.
    Operators can tighten or loosen it. Sovereign Aegis Batch-Passthrough
    Matrix (2026-06-20) raised the default 5 MiB -> 64 MiB so massive
    multi-file refactor JSONL clears the preflight, aligned with the Aegis
    passthrough cap (JARVIS_AEGIS_MAX_REQUEST_BODY_BYTES)."""
    src = DW_FILE.read_text()
    # The env knob name + default value documented
    assert "JARVIS_DW_UPLOAD_MAX_BYTES" in src
    assert "64 * 1024 * 1024" in src  # 64 MiB default (aligned with Aegis cap)


def test_spine_payload_size_logged_with_model_and_custom_id() -> None:
    """The START log line MUST include all four fields:
    payload bytes, custom_id, model, op_id. These are the
    correlation keys for any HTTP error postmortem."""
    src = DW_FILE.read_text()
    tree = ast.parse(src, filename=str(DW_FILE))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_upload_file"
        ):
            body = ast.unparse(node)
            assert "payload=%d bytes" in body
            assert "custom_id=%s" in body
            assert "model=%s" in body
            assert "op=%s" in body
            return
    pytest.fail("_upload_file not located")


# ──────────────────────────────────────────────────────────────────────
# Phase 2 — Cleanup discipline across all 4 batch lifecycle methods.
# ──────────────────────────────────────────────────────────────────────


_PHASE2_METHODS = (
    "_upload_file",
    "_create_batch",
    "_adaptive_poll_batch",
    "_retrieve_result",
)


def _find_async_method(tree: ast.AST, name: str) -> ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    pytest.fail(f"{name} not located in doubleword_provider.py")


def test_ast_pin_slice37_phase2_all_methods_have_finally() -> None:
    """All 4 batch lifecycle methods MUST contain a ``finally:``
    block. Without it, lease + rate-limiter accounting can drift on
    early-throw paths."""
    tree = ast.parse(DW_FILE.read_text(), filename=str(DW_FILE))
    for method in _PHASE2_METHODS:
        node = _find_async_method(tree, method)
        body = ast.unparse(node)
        assert "finally:" in body, (
            f"{method} missing try/finally cleanup discipline"
        )


def test_ast_pin_slice37_phase2_lease_release_attempt() -> None:
    """Every batch lifecycle method MUST attempt to import +
    invoke ``release_call_lease`` from the Aegis bridge in its
    finally block. The import is suppressed (forward-looking)
    until the bridge publishes the helper."""
    tree = ast.parse(DW_FILE.read_text(), filename=str(DW_FILE))
    for method in _PHASE2_METHODS:
        node = _find_async_method(tree, method)
        body = ast.unparse(node)
        assert "release_call_lease" in body, (
            f"{method} missing forward-looking lease release"
        )
        assert "ImportError" in body and "AttributeError" in body, (
            f"{method} must suppress ImportError/AttributeError "
            "on the forward-looking release"
        )


def test_ast_pin_slice37_phase2_rate_limiter_guard_present() -> None:
    """Methods that use the rate-limiter MUST guard
    ``_rate_limiter_recorded`` so the finally block doesn't
    double-record on success or drop the metric on early-throw."""
    tree = ast.parse(DW_FILE.read_text(), filename=str(DW_FILE))
    for method in ("_create_batch", "_adaptive_poll_batch", "_retrieve_result"):
        node = _find_async_method(tree, method)
        body = ast.unparse(node)
        assert "_rate_limiter_recorded" in body, (
            f"{method} missing _rate_limiter_recorded sentinel"
        )


def test_ast_pin_slice37_phase2_lease_initialized_before_try() -> None:
    """Every batch lifecycle method MUST initialize
    ``_aegis_lease = None`` and THEN enter a try block where the
    finally clause can safely reference the name. Some methods have
    earlier disposable try blocks for env parsing — the contract
    here is that there is AT LEAST ONE ``try:`` that follows
    ``_aegis_lease = None`` lexically (i.e., the cleanup-bearing
    try block exists below the init)."""
    tree = ast.parse(DW_FILE.read_text(), filename=str(DW_FILE))
    for method in _PHASE2_METHODS:
        node = _find_async_method(tree, method)
        body = ast.unparse(node)
        idx_init = body.find("_aegis_lease = None")
        assert idx_init != -1, (
            f"{method} missing explicit _aegis_lease=None init"
        )
        # There must be a "try:" AFTER the init line — that's the
        # cleanup-bearing one. Earlier try blocks (env-parse, etc.)
        # are fine; what matters is that the lease-init has a try
        # below it whose finally references the lease.
        idx_try_after = body.find("try:", idx_init)
        assert idx_try_after != -1, (
            f"{method}: no try: block found AFTER _aegis_lease=None "
            f"(init@{idx_init}); the cleanup-bearing try must follow "
            "the init line"
        )
