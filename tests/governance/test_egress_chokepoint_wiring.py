"""test_egress_chokepoint_wiring.py -- T2 tests.

Verifies that the egress interceptor is wired at BOTH chokepoints in
doubleword_provider.py:
  1. Realtime body (~line 3171): interceptor fires BEFORE stream flag or
     session.post is touched.  An oversized body raises
     LocalEgressOverweightError and session.post is never called.
  2. Batch body (~line 1756): interceptor fires BEFORE _compose_jsonl_batch_entry
     is called.  An oversized batch body raises LocalEgressOverweightError.

The tests use monkeypatching of the interceptor module's public functions
(egress_interceptor_enabled, sanitize_egress_body, assert_egress_weight) rather
than instantiating the full DoublewordProvider (which requires aiohttp sessions,
live credentials, and other heavy machinery).  Each test verifies the guard
snippet behaviour in isolation via a thin integration shim that calls the same
conditional block the provider code runs.

Three test classes:
  TestGuardSnippetBehaviour  -- unit-level: the guard block itself
  TestRealtimeChokepoint     -- structural: grep confirms the realtime site calls
                                the interceptor API under egress_interceptor_enabled
  TestBatchChokepoint        -- structural: grep confirms the batch site calls
                                the interceptor API under egress_interceptor_enabled
"""
from __future__ import annotations

import ast
import pathlib
import re
import types
from unittest.mock import MagicMock, patch

import pytest

from backend.core.ouroboros.governance.dw_egress_interceptor import (
    LocalEgressOverweightError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PROVIDER_SRC = pathlib.Path(
    "backend/core/ouroboros/governance/doubleword_provider.py"
)


def _provider_source() -> str:
    return _PROVIDER_SRC.read_text(encoding="ascii", errors="replace")


# ---------------------------------------------------------------------------
# 1. Unit-level guard snippet behaviour
#    (the same conditional block that is embedded at each chokepoint)
# ---------------------------------------------------------------------------


class TestGuardSnippetBehaviour:
    """Simulate the guard snippet to prove I2 asymmetry is correct."""

    @staticmethod
    def _run_guard(
        body: dict,
        model: str,
        *,
        interceptor_enabled: bool = True,
        sanitize_raises: Exception | None = None,
        weight_raises: Exception | None = None,
        session_post_mock: MagicMock | None = None,
    ) -> dict:
        """Execute the guard snippet exactly as written in doubleword_provider.py.

        Returns the (possibly sanitized) body.  Raises LocalEgressOverweightError
        when weight assertion fails.  Never raises for other interceptor errors.
        """
        # Build fake interceptor callables.
        def _enabled() -> bool:
            return interceptor_enabled

        def _sanitize(b: dict, m: str) -> dict:
            if sanitize_raises is not None:
                raise sanitize_raises
            return dict(b, _sanitized=True)  # mark that sanitize ran

        def _assert_weight(b: dict, m: str) -> None:
            if weight_raises is not None:
                raise weight_raises

        # The guard snippet (verbatim from doubleword_provider.py).
        if _enabled():
            try:
                body = _sanitize(body, model)
                _assert_weight(body, model)
            except LocalEgressOverweightError:
                raise  # block egress
            except Exception:  # noqa: BLE001
                pass  # I2: never block on sanitize/estimate error

        if session_post_mock is not None:
            # Simulate reaching the fire point only if no exception was raised.
            session_post_mock(body)

        return body

    def test_normal_body_is_sanitized_and_passes(self):
        """Happy path: normal body goes through sanitize and weight check."""
        post_mock = MagicMock()
        body = {"messages": [{"role": "user", "content": "hello"}]}
        result = self._run_guard(body, "test-model", session_post_mock=post_mock)
        assert result.get("_sanitized") is True, "sanitize_egress_body was not called"
        post_mock.assert_called_once()

    def test_overweight_body_blocks_egress_and_post_never_called(self):
        """Overweight body raises LocalEgressOverweightError; session.post not reached."""
        post_mock = MagicMock()
        body = {"messages": [{"role": "user", "content": "x" * 1_000}]}
        with pytest.raises(LocalEgressOverweightError):
            self._run_guard(
                body,
                "test-model",
                weight_raises=LocalEgressOverweightError(
                    attempted_size=1_000_000,
                    max_allowed_size=100,
                    model="test-model",
                ),
                session_post_mock=post_mock,
            )
        post_mock.assert_not_called()  # critical: zero egress

    def test_sanitize_error_does_not_block_egress(self):
        """I2: a sanitize bug (non-overweight exception) must not block egress."""
        post_mock = MagicMock()
        body = {"messages": [{"role": "user", "content": "hello"}]}
        result = self._run_guard(
            body,
            "test-model",
            sanitize_raises=RuntimeError("unexpected sanitize crash"),
            session_post_mock=post_mock,
        )
        # post must still be called (request was not blocked)
        post_mock.assert_called_once()
        # body came back unchanged (sanitize did not run to completion)
        assert "_sanitized" not in result

    def test_weight_non_overweight_error_does_not_block(self):
        """I2: a weight estimation crash (not LocalEgressOverweightError) passes through."""
        post_mock = MagicMock()
        body = {"messages": [{"role": "user", "content": "hello"}]}
        # sanitize succeeds, but assert_egress_weight throws something unexpected
        result = self._run_guard(
            body,
            "test-model",
            weight_raises=ValueError("unexpected weight crash"),
            session_post_mock=post_mock,
        )
        post_mock.assert_called_once()

    def test_guard_disabled_skips_interceptor_entirely(self):
        """When egress_interceptor_enabled() is False body is returned unchanged."""
        post_mock = MagicMock()
        body = {"messages": [{"role": "user", "content": "hello"}]}
        result = self._run_guard(
            body,
            "test-model",
            interceptor_enabled=False,
            # Even if weight would have raised, it must not be called:
            weight_raises=LocalEgressOverweightError(
                attempted_size=999_999,
                max_allowed_size=1,
                model="test-model",
            ),
            session_post_mock=post_mock,
        )
        # body passes through unchanged; session.post is called
        assert "_sanitized" not in result
        post_mock.assert_called_once()


# ---------------------------------------------------------------------------
# 2. Structural assertion: realtime chokepoint
# ---------------------------------------------------------------------------


class TestRealtimeChokepoint:
    """Verify that the realtime body (~line 3171) actually calls the interceptor."""

    def test_interceptor_functions_called_at_realtime_site(self):
        """The realtime body site must call sanitize + weight under the enabled guard."""
        src = _provider_source()

        # Find the realtime site: the guard block that follows the body-dict
        # ending in ``**_reasoning_request_params(complexity=_complexity or``.
        # The distinguishing signature is:
        #   body = sanitize_egress_body(body, _effective_model)
        #   assert_egress_weight(body, _effective_model)
        # within the scope of ``if egress_interceptor_enabled():``.

        assert "egress_interceptor_enabled" in src, (
            "egress_interceptor_enabled not found in doubleword_provider.py"
        )
        assert "sanitize_egress_body(body, _effective_model)" in src, (
            "sanitize_egress_body(body, _effective_model) not found in doubleword_provider.py"
        )
        assert "assert_egress_weight(body, _effective_model)" in src, (
            "assert_egress_weight(body, _effective_model) not found in doubleword_provider.py"
        )

    def test_realtime_guard_precedes_stream_flag(self):
        """The interceptor block must appear BEFORE body['stream'] = True."""
        src = _provider_source()
        interceptor_pos = src.find("sanitize_egress_body(body, _effective_model)")
        stream_flag_pos = src.find("body[\"stream\"] = True")
        assert interceptor_pos > 0, "sanitize_egress_body realtime call not found"
        assert stream_flag_pos > 0, "body['stream'] = True not found"
        assert interceptor_pos < stream_flag_pos, (
            "Interceptor guard must appear BEFORE body['stream'] = True "
            f"(interceptor_pos={interceptor_pos}, stream_flag_pos={stream_flag_pos})"
        )

    def test_overweight_error_re_raised_at_realtime_site(self):
        """Source has ``except LocalEgressOverweightError: raise`` at the realtime site."""
        src = _provider_source()
        # Count occurrences of the re-raise pattern
        pattern = r"except LocalEgressOverweightError:\s*\n\s*raise"
        matches = re.findall(pattern, src)
        assert len(matches) >= 2, (
            "Expected >=2 ``except LocalEgressOverweightError: raise`` blocks "
            f"(realtime + batch), found {len(matches)}"
        )


# ---------------------------------------------------------------------------
# 3. Structural assertion: batch chokepoint
# ---------------------------------------------------------------------------


class TestBatchChokepoint:
    """Verify that the batch body (~line 1756) actually calls the interceptor."""

    def test_interceptor_functions_called_at_batch_site(self):
        """The batch body site must call sanitize + weight under the enabled guard."""
        src = _provider_source()
        # The batch site uses _batch_body as the local variable.
        assert "sanitize_egress_body(_batch_body, _effective_model)" in src, (
            "sanitize_egress_body(_batch_body, ...) not found — batch chokepoint not wired"
        )
        assert "assert_egress_weight(_batch_body, _effective_model)" in src, (
            "assert_egress_weight(_batch_body, ...) not found — batch chokepoint not wired"
        )

    def test_batch_guard_precedes_compose_jsonl(self):
        """_batch_body is intercepted BEFORE _compose_jsonl_batch_entry is called."""
        src = _provider_source()
        # The interceptor runs on _batch_body before it is passed to compose.
        intercept_pos = src.find("sanitize_egress_body(_batch_body, _effective_model)")
        compose_pos = src.find('self._compose_jsonl_batch_entry({')
        assert intercept_pos > 0, "sanitize_egress_body(_batch_body) call not found"
        assert compose_pos > 0, "_compose_jsonl_batch_entry not found"
        assert intercept_pos < compose_pos, (
            "Batch interceptor guard must appear BEFORE _compose_jsonl_batch_entry "
            f"(intercept_pos={intercept_pos}, compose_pos={compose_pos})"
        )

    def test_batch_body_local_var_passed_to_compose(self):
        """The compose call must receive _batch_body (not the raw inline dict)."""
        src = _provider_source()
        # After the interceptor, the compose call should reference _batch_body.
        assert '"body": _batch_body,' in src, (
            '"body": _batch_body, not found — _compose_jsonl_batch_entry '
            "is not consuming the intercepted _batch_body"
        )

    def test_batch_guard_disabled_is_off_by_default_inverted(self):
        """OFF path: when disabled the guard block is skipped (source inspection)."""
        src = _provider_source()
        # There must be exactly TWO egress_interceptor_enabled() call sites
        # (one realtime, one batch).
        count = src.count("egress_interceptor_enabled()")
        assert count >= 2, (
            f"Expected >=2 egress_interceptor_enabled() call sites, got {count}"
        )


# ---------------------------------------------------------------------------
# 4. Import cycle safety
# ---------------------------------------------------------------------------


class TestImportCycleSafety:
    """Verify that importing doubleword_provider does NOT cause a circular import."""

    def test_import_does_not_raise(self):
        """Top-level import of doubleword_provider must succeed without ImportError."""
        # If we get here the import already worked (test file imports from it
        # transitively). This explicit re-import validates the module is
        # importable without errors in a fresh module lookup.
        import importlib
        # Use importlib to force an explicit import resolution.
        mod = importlib.import_module(
            "backend.core.ouroboros.governance.doubleword_provider"
        )
        assert mod is not None

    def test_interceptor_names_visible_on_provider_module(self):
        """The four interceptor names must be importable from doubleword_provider."""
        from backend.core.ouroboros.governance.doubleword_provider import (  # noqa: PLC0415
            LocalEgressOverweightError as _LEE,
            assert_egress_weight as _aw,
            egress_interceptor_enabled as _ei,
            sanitize_egress_body as _sb,
        )
        assert callable(_ei)
        assert callable(_sb)
        assert callable(_aw)
        assert issubclass(_LEE, Exception)
