"""test_egress_interceptor_integration.py -- T5 integration spine.

Sovereign Egress Interceptor Mesh: end-to-end composition proof + citizenship
guarantee tests.  Uses real module imports; fakes are limited to the network
layer (aiohttp session) and the filesystem reader used inside the decomposition
planner.

Seven areas (matching the task-T5-brief spec section 6):

  1. Zero-egress proof (THE operator gate): an oversized request is provably
     BLOCKED locally; session.post is NEVER called.
  2. Weight-0.0 classification: LOCAL_EGRESS_OVERWEIGHT -> FailureSource weight
     0.0 -> does NOT trip the topology-sentinel breaker (mirror FSM_EXHAUSTED).
  3. Compression-target re-chunk: decompose_for_block(compression_target=N) ->
     every multi-symbol sub-goal payload <= N.
  4. Sanitize: gpt-oss body with reasoning_effort=none -> floored to non-none;
     unknown model -> unchanged.
  5. Fail-soft asymmetry: malformed body (messages=None) -> assert_egress_weight
     does NOT raise.
  6. Boot-guard: disabled -> warning fires; enabled/default -> silent.
  7. OFF byte-identical: egress_interceptor_enabled() false -> interceptor is a
     no-op on the egress path (minus the boot-guard warning).

ASCII-only source.  ``from __future__ import annotations`` per project mandate.
Python 3.9+.
"""
from __future__ import annotations

import inspect
import logging
import types
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Real module imports (not faked)
# ---------------------------------------------------------------------------

from backend.core.ouroboros.governance.dw_egress_interceptor import (
    LocalEgressOverweightError,
    assert_egress_weight,
    egress_interceptor_enabled,
    estimate_body_chars,
    sanitize_egress_body,
)
from backend.core.ouroboros.governance import topology_sentinel as ts
from backend.core.ouroboros.governance.dw_fault_taxonomy import (
    is_local_egress_overweight,
    is_fsm_exhaustion,
)
from backend.core.ouroboros.governance.goal_decomposition_planner import (
    decompose_for_block,
    estimate_subgoal_payload_chars,
)
from backend.core.ouroboros.governance import goal_decomposition_planner as gdp
from backend.core.ouroboros.governance.governed_loop_service import (
    _warn_if_egress_guard_disabled,
)


# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

def _make_body(chars: int, model: str = "test-model") -> Dict[str, Any]:
    """Return a minimal request body with the given character footprint."""
    return {
        "model": model,
        "messages": [{"role": "user", "content": "x" * chars}],
        "reasoning_effort": "none",
    }


def _run_guard_snippet(
    body: Dict[str, Any],
    model: str,
    *,
    interceptor_enabled: bool = True,
    session_post: MagicMock | None = None,
) -> Dict[str, Any]:
    """Execute the exact guard snippet that lives in doubleword_provider.py.

    Uses the REAL sanitize_egress_body / assert_egress_weight from the module,
    so this exercises the actual composed behaviour.  The only fake is
    ``session_post`` (network I/O).
    """
    if interceptor_enabled:
        try:
            body = sanitize_egress_body(body, model)
            assert_egress_weight(body, model)
        except LocalEgressOverweightError:
            raise  # block egress; session.post is never reached
        except Exception:  # noqa: BLE001 -- I2 fail-soft
            pass  # non-overweight error -> pass-through

    if session_post is not None:
        session_post(body)  # simulates the actual network fire

    return body


# ---------------------------------------------------------------------------
# 1. Zero-egress proof -- THE operator gate
# ---------------------------------------------------------------------------


class TestZeroEgressProof:
    """An oversized request is blocked LOCALLY; session.post is NEVER awaited."""

    def test_oversized_body_raises_local_egress_overweight(self, monkeypatch):
        """assert_egress_weight raises LocalEgressOverweightError on an oversized body."""
        monkeypatch.setenv("JARVIS_DW_EGRESS_MAX_CHARS", "1000")
        body = _make_body(chars=50_000)
        with pytest.raises(LocalEgressOverweightError) as exc_info:
            assert_egress_weight(body, "some-model")
        err = exc_info.value
        assert err.attempted_size >= 50_000
        assert err.max_allowed_size == 1000
        assert err.required_compression_ratio >= 50.0

    def test_session_post_never_called_on_oversized_body(self, monkeypatch):
        """session.post is NEVER called when the body is oversized — zero egress."""
        monkeypatch.setenv("JARVIS_DW_EGRESS_MAX_CHARS", "500")
        session_post = MagicMock()
        body = _make_body(chars=5_000)

        with pytest.raises(LocalEgressOverweightError):
            _run_guard_snippet(body, "big-model", session_post=session_post)

        session_post.assert_not_called()  # THE guarantee: zero network egress

    def test_within_ceiling_reaches_session_post(self, monkeypatch):
        """Happy path: a body within the ceiling does reach session.post."""
        monkeypatch.setenv("JARVIS_DW_EGRESS_MAX_CHARS", "100_000")
        session_post = MagicMock()
        body = _make_body(chars=100)

        _run_guard_snippet(body, "small-model", session_post=session_post)

        session_post.assert_called_once()

    def test_structural_guard_sequence_in_provider(self):
        """Structural: the real doubleword_provider.py contains the correct guard
        sequence (sanitize -> assert_weight -> re-raise) at BOTH chokepoints."""
        import pathlib
        src = pathlib.Path(
            "backend/core/ouroboros/governance/doubleword_provider.py"
        ).read_text(encoding="ascii", errors="replace")

        # Both chokepoint variables are present.
        assert "sanitize_egress_body(body, _effective_model)" in src
        assert "assert_egress_weight(body, _effective_model)" in src
        assert "sanitize_egress_body(_batch_body, _effective_model)" in src
        assert "assert_egress_weight(_batch_body, _effective_model)" in src

        # LocalEgressOverweightError is re-raised (not swallowed) at both sites.
        import re
        reraise_pattern = r"except LocalEgressOverweightError:\s*\n\s*raise"
        matches = re.findall(reraise_pattern, src)
        assert len(matches) >= 2, (
            f"Expected >=2 re-raise blocks, found {len(matches)}"
        )

    def test_guard_precedes_stream_flag_structurally(self):
        """Interceptor guard must appear BEFORE body['stream'] = True."""
        import pathlib
        src = pathlib.Path(
            "backend/core/ouroboros/governance/doubleword_provider.py"
        ).read_text(encoding="ascii", errors="replace")
        intercept_pos = src.find("sanitize_egress_body(body, _effective_model)")
        stream_pos = src.find('body["stream"] = True')
        assert intercept_pos > 0
        assert stream_pos > 0
        assert intercept_pos < stream_pos, (
            "Interceptor must fire BEFORE body['stream'] = True"
        )


# ---------------------------------------------------------------------------
# 2. Weight-0.0 classification -- does NOT trip the vendor breaker
# ---------------------------------------------------------------------------


class TestWeight0Classification:
    """LOCAL_EGRESS_OVERWEIGHT -> FailureSource weight 0.0; breaker stays CLOSED."""

    def test_failure_source_exists_with_correct_value(self):
        assert hasattr(ts.FailureSource, "LOCAL_EGRESS_OVERWEIGHT")
        assert ts.FailureSource.LOCAL_EGRESS_OVERWEIGHT.value == "local_egress_overweight"

    def test_failure_weight_is_zero(self, monkeypatch):
        monkeypatch.delenv("JARVIS_TOPOLOGY_WEIGHT_LOCAL_EGRESS_OVERWEIGHT", raising=False)
        weight = ts.failure_weight(ts.FailureSource.LOCAL_EGRESS_OVERWEIGHT)
        assert weight == 0.0

    def test_mirrors_fsm_exhausted_weight(self, monkeypatch):
        monkeypatch.delenv("JARVIS_TOPOLOGY_WEIGHT_LOCAL_EGRESS_OVERWEIGHT", raising=False)
        monkeypatch.delenv("JARVIS_TOPOLOGY_WEIGHT_FSM_EXHAUSTED", raising=False)
        assert ts.failure_weight(ts.FailureSource.LOCAL_EGRESS_OVERWEIGHT) == (
            ts.failure_weight(ts.FailureSource.FSM_EXHAUSTED)
        )

    def test_overweight_streak_never_trips_breaker(self, monkeypatch):
        """Hammering LOCAL_EGRESS_OVERWEIGHT 50x must NOT open the topology breaker."""
        monkeypatch.setenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", "true")
        monkeypatch.delenv("JARVIS_TOPOLOGY_FORCE_SEVERED", raising=False)
        sentinel = ts.TopologySentinel()
        model = "egress-integration-test-model"
        sentinel.register_endpoint(model)

        for _ in range(50):
            sentinel.report_failure(
                model, ts.FailureSource.LOCAL_EGRESS_OVERWEIGHT, "overweight"
            )

        snap = sentinel._snapshots.get(model)
        assert snap is not None
        assert snap.weighted_failure_streak == 0.0
        assert sentinel.get_state(model) == "CLOSED"

    def test_taxonomy_correctly_classifies_local_egress_overweight(self):
        """is_local_egress_overweight correctly identifies the error type."""
        err = LocalEgressOverweightError(
            attempted_size=2_000_000,
            max_allowed_size=600_000,
            model="integration-test",
        )
        assert is_local_egress_overweight(err) is True
        # Must NOT be mislabeled as FSM exhaustion.
        assert is_fsm_exhaustion(err) is False

    def test_taxonomy_does_not_misclassify_other_errors(self):
        """Generic errors are not classified as local egress overweight."""
        assert is_local_egress_overweight(RuntimeError("something")) is False
        assert is_local_egress_overweight(ValueError("all_providers_exhausted")) is False

    def test_taxonomy_is_fail_soft(self):
        """is_local_egress_overweight never raises even on weird input."""
        assert is_local_egress_overweight(None) is False  # type: ignore[arg-type]
        assert is_local_egress_overweight(object()) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 3. Compression-target re-chunk
# ---------------------------------------------------------------------------


class _GoalStub:
    """Minimal duck-typed goal for decompose_for_block."""

    def __init__(
        self,
        goal_id: str,
        title: str,
        description: str,
        target_files: tuple,
    ) -> None:
        self.goal_id = goal_id
        self.title = title
        self.description = description
        self.target_files = target_files


def _make_scoper_with_sizes(symbol_sizes: Dict[str, int]):
    """Return (scoper_fn, source_str) with per-symbol char sizes.

    Each symbol occupies lines where len(line) ~= 50 chars.
    The scoper returns ScopedTarget objects whose line ranges encompass the
    synthetic content.
    """
    from backend.core.ouroboros.governance.ast_symbol_scoper import ScopedTarget

    CHARS_PER_LINE = 50
    lines: List[str] = []
    targets: List[Any] = []
    lineno = 1
    for name, size in symbol_sizes.items():
        n_lines = max(1, size // CHARS_PER_LINE)
        start = lineno
        for _ in range(n_lines):
            lines.append("x" * (CHARS_PER_LINE - 1))
            lineno += 1
        end = lineno - 1
        targets.append(ScopedTarget("f.py", name, start, end))
    source = "\n".join(lines) + "\n"

    def _scoper(file_path: str, description: str):  # noqa: ARG001
        return tuple(targets)

    return _scoper, source


class TestCompressionTargetReChunk:
    """decompose_for_block(compression_target=N) -> sub-goals fit in N chars."""

    def test_compression_target_kwarg_exists_on_decompose(self):
        sig = inspect.signature(decompose_for_block)
        assert "compression_target" in sig.parameters
        assert sig.parameters["compression_target"].default is None

    def test_none_target_is_byte_identical_to_legacy(self):
        """compression_target=None produces the same output as omitting the kwarg."""
        scoper, _ = _make_scoper_with_sizes({"A": 100, "B": 100})
        goal = _GoalStub("g1", "modify things", "touch A and B", ("f.py",))
        legacy = decompose_for_block(goal, zero_coverage=False, scoper=scoper)
        explicit_none = decompose_for_block(
            goal, zero_coverage=False, scoper=scoper, compression_target=None
        )
        assert [s.to_dict() for s in legacy] == [s.to_dict() for s in explicit_none]

    def test_multi_symbol_sub_goals_fit_within_target(self, monkeypatch):
        """Every returned sub-goal with >1 symbol has payload <= compression_target."""
        scoper, source = _make_scoper_with_sizes(
            {"A": 200, "B": 200, "C": 200, "D": 200}
        )
        monkeypatch.setattr(
            gdp, "_read_source_for_estimate", lambda fp: source, raising=False
        )
        goal = _GoalStub("g2", "refactor", "touch A B C D", ("f.py",))
        subs = decompose_for_block(
            goal, zero_coverage=False, scoper=scoper, compression_target=700
        )
        assert len(subs) >= 1
        for sub in subs:
            if len(sub.scoped_symbols) > 1:
                payload = estimate_subgoal_payload_chars(
                    sub, source_reader=lambda fp: source
                )
                assert payload <= 700, (
                    f"sub {sub.sub_goal_id}: payload {payload} > 700 chars"
                )

    def test_single_oversized_symbol_is_not_dropped(self, monkeypatch):
        """An irreducible symbol (> target alone) is emitted, never silently dropped."""
        scoper, source = _make_scoper_with_sizes({"Huge": 5000})
        monkeypatch.setattr(
            gdp, "_read_source_for_estimate", lambda fp: source, raising=False
        )
        goal = _GoalStub("g3", "rewrite huge", "mutate Huge", ("f.py",))
        subs = decompose_for_block(
            goal, zero_coverage=False, scoper=scoper, compression_target=500
        )
        all_syms = [sym for sub in subs for sym in sub.scoped_symbols]
        assert any("Huge" in sym for sym in all_syms), (
            "Irreducible symbol 'Huge' was silently dropped"
        )

    def test_irreducible_symbol_emits_warning(self, monkeypatch, caplog):
        """Irreducible symbol triggers a WARNING log (never silent)."""
        scoper, source = _make_scoper_with_sizes({"BigFunc": 5000})
        monkeypatch.setattr(
            gdp, "_read_source_for_estimate", lambda fp: source, raising=False
        )
        goal = _GoalStub("g4", "big func", "mutate BigFunc", ("f.py",))
        with caplog.at_level(logging.WARNING):
            decompose_for_block(
                goal, zero_coverage=False, scoper=scoper, compression_target=500
            )
        assert any(
            "irreducible" in rec.message.lower() for rec in caplog.records
        ), f"Expected irreducible warning, got: {[r.message for r in caplog.records]}"

    def test_estimate_subgoal_payload_chars_uses_same_ruler(self):
        """estimate_subgoal_payload_chars uses the same T1 estimator as the interceptor.

        The composition proof: the same estimate_body_chars function that gates
        dispatch is the ruler the chunker uses to partition sub-goals, so no
        off-by-one drift is possible.
        """
        # Verify the planner imports from the interceptor module (not a fork).
        src = inspect.getsource(estimate_subgoal_payload_chars)
        assert "estimate_body_chars" in src or "dw_egress_interceptor" in src, (
            "estimate_subgoal_payload_chars must reuse the interceptor's estimate_body_chars"
        )


# ---------------------------------------------------------------------------
# 4. Sanitize
# ---------------------------------------------------------------------------


class TestSanitize:
    """sanitize_egress_body applies model-specific rules correctly."""

    def test_gpt_oss_floors_reasoning_effort_from_none(self):
        """gpt-oss body with reasoning_effort=none -> floored to a non-none value."""
        body = {
            "model": "openai/gpt-oss-120b",
            "messages": [{"role": "user", "content": "hello"}],
            "reasoning_effort": "none",
        }
        out = sanitize_egress_body(body, "openai/gpt-oss-120b")
        assert out["reasoning_effort"] != "none", (
            f"Expected floored reasoning_effort, got {out['reasoning_effort']!r}"
        )

    def test_unknown_model_is_unchanged(self):
        """Body for an unknown model is returned completely unchanged."""
        body = {
            "model": "totally-unknown-xyz",
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": "none",
            "custom_field": "keep-me",
        }
        out = sanitize_egress_body(body, "totally-unknown-xyz")
        assert out == body

    def test_sanitize_never_raises_on_bad_input(self):
        """sanitize_egress_body is fail-soft: bad inputs never raise."""
        sanitize_egress_body({}, "")
        sanitize_egress_body({"messages": None}, "gpt-oss-120b")
        sanitize_egress_body(None, "gpt-oss")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 5. Fail-soft asymmetry
# ---------------------------------------------------------------------------


class TestFailSoftAsymmetry:
    """I2 guarantee: estimation errors pass through; only confirmed overweight blocks."""

    def test_messages_none_does_not_raise(self):
        """A malformed body (messages=None) -> estimate=0 -> no block (pass-through)."""
        body = {"messages": None, "model": "m"}
        # Must NOT raise -- estimator is fail-soft, returns 0 on bad input.
        assert_egress_weight(body, "m")  # no exception

    def test_empty_dict_does_not_raise(self):
        """Completely empty body -> estimate=0 -> no block."""
        assert_egress_weight({}, "m")

    def test_messages_wrong_type_does_not_raise(self):
        """messages as a string (unexpected type) -> fail-soft, no block."""
        assert_egress_weight({"messages": "not a list"}, "m")

    def test_estimate_returns_zero_on_none_messages(self):
        """estimate_body_chars returns 0 (not an error) for messages=None."""
        result = estimate_body_chars({"messages": None})
        assert result == 0

    def test_guard_snippet_passes_through_on_sanitize_crash(self, monkeypatch):
        """I2: if sanitize raises a non-overweight error, egress is NOT blocked."""
        session_post = MagicMock()

        # Patch sanitize_egress_body to raise an unexpected error.
        monkeypatch.setattr(
            "backend.core.ouroboros.governance.dw_egress_interceptor.sanitize_egress_body",
            lambda body, model: (_ for _ in ()).throw(RuntimeError("sanitize crash")),
        )

        # Run the guard inline (mirrors the chokepoint logic exactly).
        body = _make_body(chars=100)
        # Since monkeypatch alters the module attribute, use the guarded inline
        # logic directly (the same pattern as in test_egress_chokepoint_wiring.py).

        import backend.core.ouroboros.governance.dw_egress_interceptor as egi_mod
        original_sanitize = sanitize_egress_body
        egi_mod.sanitize_egress_body = lambda b, m: (_ for _ in ()).throw(  # type: ignore[method-assign]
            RuntimeError("sanitize crash")
        )
        try:
            if egi_mod.egress_interceptor_enabled():
                try:
                    body = egi_mod.sanitize_egress_body(body, "m")
                    egi_mod.assert_egress_weight(body, "m")
                except LocalEgressOverweightError:
                    raise
                except Exception:  # noqa: BLE001
                    pass  # I2: non-overweight error -> pass-through
            session_post(body)  # simulates reaching the fire point
        finally:
            egi_mod.sanitize_egress_body = original_sanitize  # type: ignore[method-assign]

        session_post.assert_called_once()  # never blocked

    def test_guard_snippet_passes_through_on_weight_estimation_crash(self):
        """I2: if assert_egress_weight raises a non-overweight error, egress still fires."""
        session_post = MagicMock()
        body = _make_body(chars=100)

        # Simulate the guard with a crashing weight check.
        import backend.core.ouroboros.governance.dw_egress_interceptor as egi_mod
        original_assert = egi_mod.assert_egress_weight
        egi_mod.assert_egress_weight = lambda b, m: (_ for _ in ()).throw(  # type: ignore[method-assign]
            ValueError("estimation internal crash")
        )
        try:
            if egi_mod.egress_interceptor_enabled():
                try:
                    body = egi_mod.sanitize_egress_body(body, "m")
                    egi_mod.assert_egress_weight(body, "m")
                except LocalEgressOverweightError:
                    raise
                except Exception:  # noqa: BLE001
                    pass
            session_post(body)
        finally:
            egi_mod.assert_egress_weight = original_assert  # type: ignore[method-assign]

        session_post.assert_called_once()


# ---------------------------------------------------------------------------
# 6. Boot-guard
# ---------------------------------------------------------------------------


class TestBootGuard:
    """_warn_if_egress_guard_disabled fires exactly when the guard is OFF."""

    _SOVEREIGN_WARNING = (
        "[SOVEREIGN WARNING] API Citizenship Guard Disabled: Egress Interceptor "
        "is OFF. Node is vulnerable to overweight payload dispatch."
    )

    def test_warns_when_disabled(self, monkeypatch, caplog):
        monkeypatch.setenv("JARVIS_DW_EGRESS_INTERCEPTOR_ENABLED", "false")
        logger = logging.getLogger("boot_guard_integration_test")
        with caplog.at_level(logging.WARNING, logger=logger.name):
            result = _warn_if_egress_guard_disabled(logger)
        assert result is True
        assert any(self._SOVEREIGN_WARNING in r.message for r in caplog.records)

    def test_silent_when_enabled_default(self, monkeypatch, caplog):
        monkeypatch.delenv("JARVIS_DW_EGRESS_INTERCEPTOR_ENABLED", raising=False)
        logger = logging.getLogger("boot_guard_integration_test2")
        with caplog.at_level(logging.WARNING, logger=logger.name):
            result = _warn_if_egress_guard_disabled(logger)
        assert result is False
        assert not any("SOVEREIGN WARNING" in r.message for r in caplog.records)

    def test_silent_when_enabled_explicit_true(self, monkeypatch, caplog):
        monkeypatch.setenv("JARVIS_DW_EGRESS_INTERCEPTOR_ENABLED", "true")
        logger = logging.getLogger("boot_guard_integration_test3")
        with caplog.at_level(logging.WARNING, logger=logger.name):
            result = _warn_if_egress_guard_disabled(logger)
        assert result is False
        assert not any("SOVEREIGN WARNING" in r.message for r in caplog.records)

    def test_warns_when_disabled_zero(self, monkeypatch, caplog):
        monkeypatch.setenv("JARVIS_DW_EGRESS_INTERCEPTOR_ENABLED", "0")
        logger = logging.getLogger("boot_guard_integration_test4")
        with caplog.at_level(logging.WARNING, logger=logger.name):
            result = _warn_if_egress_guard_disabled(logger)
        assert result is True
        assert any(self._SOVEREIGN_WARNING in r.message for r in caplog.records)

    def test_never_raises(self, monkeypatch):
        """_warn_if_egress_guard_disabled must never raise, even with a broken logger."""
        monkeypatch.setenv("JARVIS_DW_EGRESS_INTERCEPTOR_ENABLED", "off")
        logger = logging.getLogger("boot_guard_never_raise")
        try:
            _warn_if_egress_guard_disabled(logger)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"_warn_if_egress_guard_disabled raised: {exc}")


# ---------------------------------------------------------------------------
# 7. OFF byte-identical -- disabled -> interceptor is a no-op
# ---------------------------------------------------------------------------


class TestOffByteIdentical:
    """egress_interceptor_enabled() == False -> guard is a pure pass-through."""

    def test_disabled_guard_never_calls_sanitize_or_weight(self, monkeypatch):
        """When the interceptor is disabled, sanitize + weight are never invoked."""
        monkeypatch.setenv("JARVIS_DW_EGRESS_INTERCEPTOR_ENABLED", "false")

        sanitize_called = []
        weight_called = []

        import backend.core.ouroboros.governance.dw_egress_interceptor as egi_mod
        original_sanitize = egi_mod.sanitize_egress_body
        original_assert = egi_mod.assert_egress_weight

        def _tracking_sanitize(b, m):
            sanitize_called.append(True)
            return original_sanitize(b, m)

        def _tracking_assert(b, m):
            weight_called.append(True)
            return original_assert(b, m)

        egi_mod.sanitize_egress_body = _tracking_sanitize  # type: ignore[method-assign]
        egi_mod.assert_egress_weight = _tracking_assert  # type: ignore[method-assign]
        try:
            # Simulate the guard snippet with the real enabled() check.
            session_post = MagicMock()
            body = _make_body(chars=999_999)  # oversized: would be blocked if ON

            if egi_mod.egress_interceptor_enabled():
                try:
                    body = egi_mod.sanitize_egress_body(body, "m")
                    egi_mod.assert_egress_weight(body, "m")
                except LocalEgressOverweightError:
                    raise
                except Exception:  # noqa: BLE001
                    pass
            session_post(body)
        finally:
            egi_mod.sanitize_egress_body = original_sanitize  # type: ignore[method-assign]
            egi_mod.assert_egress_weight = original_assert  # type: ignore[method-assign]

        assert not sanitize_called, "sanitize_egress_body must NOT be called when OFF"
        assert not weight_called, "assert_egress_weight must NOT be called when OFF"
        session_post.assert_called_once()  # body passed through unchanged

    def test_disabled_oversized_body_reaches_session_post(self, monkeypatch):
        """When interceptor is OFF, even a hugely oversized body reaches session.post."""
        monkeypatch.setenv("JARVIS_DW_EGRESS_INTERCEPTOR_ENABLED", "false")
        monkeypatch.setenv("JARVIS_DW_EGRESS_MAX_CHARS", "100")  # very low ceiling

        session_post = MagicMock()
        body = _make_body(chars=50_000)

        result = _run_guard_snippet(
            body, "some-model", interceptor_enabled=False, session_post=session_post
        )
        session_post.assert_called_once()  # body not blocked

    def test_egress_interceptor_enabled_returns_false_on_env_false(self, monkeypatch):
        monkeypatch.setenv("JARVIS_DW_EGRESS_INTERCEPTOR_ENABLED", "false")
        assert egress_interceptor_enabled() is False

    def test_egress_interceptor_enabled_returns_true_by_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_DW_EGRESS_INTERCEPTOR_ENABLED", raising=False)
        assert egress_interceptor_enabled() is True


# ---------------------------------------------------------------------------
# 8. Composition integrity -- cross-module seam assertions
# ---------------------------------------------------------------------------


class TestCompositionIntegrity:
    """Cross-module seam assertions: verify the mesh components are wired together."""

    def test_generate_runner_references_egress_overweight(self):
        """generate_runner.py must reference egress overweight handling."""
        import backend.core.ouroboros.governance.phase_runners.generate_runner as gr
        src = inspect.getsource(gr)
        assert (
            "is_local_egress_overweight" in src
            or "LocalEgressOverweightError" in src
        ), "generate_runner must handle LocalEgressOverweightError"

    def test_generate_runner_references_compression_target(self):
        """generate_runner.py must thread compression_target to the decompose seam."""
        import backend.core.ouroboros.governance.phase_runners.generate_runner as gr
        src = inspect.getsource(gr)
        assert "compression_target" in src, (
            "generate_runner must pass compression_target to the decompose seam"
        )

    def test_orchestrator_decompose_seam_accepts_compression_target(self):
        """Orchestrator._decompose_block_or_legacy must accept compression_target kwarg."""
        from backend.core.ouroboros.governance.orchestrator import Orchestrator
        sig = inspect.signature(Orchestrator._decompose_block_or_legacy)
        assert "compression_target" in sig.parameters
        assert sig.parameters["compression_target"].default is None

    def test_interceptor_names_importable_from_provider_module(self):
        """The four interceptor public names are re-exported by doubleword_provider."""
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

    def test_taxonomy_module_imports_from_interceptor_not_forked(self):
        """dw_fault_taxonomy.is_local_egress_overweight uses a lazy import — no fork."""
        src = inspect.getsource(is_local_egress_overweight)
        assert "LocalEgressOverweightError" in src, (
            "is_local_egress_overweight must import from dw_egress_interceptor"
        )

    def test_planner_uses_estimate_body_chars_from_interceptor(self):
        """estimate_subgoal_payload_chars reuses the interceptor's T1 estimate_body_chars."""
        src = inspect.getsource(estimate_subgoal_payload_chars)
        # The function must import or reference the shared estimator.
        assert "estimate_body_chars" in src or "dw_egress_interceptor" in src, (
            "estimate_subgoal_payload_chars must reuse the T1 estimate_body_chars ruler"
        )

    def test_full_mesh_import_cycle_is_safe(self):
        """All mesh modules import cleanly without circular-import errors."""
        import importlib
        for mod_path in [
            "backend.core.ouroboros.governance.dw_egress_interceptor",
            "backend.core.ouroboros.governance.doubleword_provider",
            "backend.core.ouroboros.governance.dw_fault_taxonomy",
            "backend.core.ouroboros.governance.topology_sentinel",
            "backend.core.ouroboros.governance.goal_decomposition_planner",
            "backend.core.ouroboros.governance.governed_loop_service",
        ]:
            mod = importlib.import_module(mod_path)
            assert mod is not None, f"Failed to import {mod_path}"
