"""Slice C graduation regression spine for MissionInferrer.

Pins:
  * Master flag ``JARVIS_GOAL_INFERENCE_ENABLED`` defaults to True
    post-2026-05-03 graduation; ``register_flags`` exposes default=True.
  * SSE event ``goal_inference_built`` fires on cache miss (real
    rebuild) and NOT on cache hit (avoids observability storm).
  * Publisher carries the lightweight projection (built_at, build_ms,
    hypotheses_count, top theme + confidence, sources_contributing,
    build_reason).
  * Engine handles publisher-import failure / publish failure
    silently (defensive fail-soft mirror of every other publisher).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.core.ouroboros.governance import goal_inference as gi


# ---------------------------------------------------------------------------
# Master flag flip
# ---------------------------------------------------------------------------


class TestMasterFlagFlip:
    def test_default_true_post_graduation(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(
            "JARVIS_GOAL_INFERENCE_ENABLED", raising=False,
        )
        assert gi.inference_enabled() is True

    def test_explicit_false_overrides(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("JARVIS_GOAL_INFERENCE_ENABLED", "false")
        assert gi.inference_enabled() is False

    def test_register_flags_master_default_true(self) -> None:
        recorded = {}
        class _R:
            def register(self, spec):
                recorded[spec.name] = spec.default
        gi.register_flags(_R())
        assert recorded["JARVIS_GOAL_INFERENCE_ENABLED"] is True


# ---------------------------------------------------------------------------
# SSE publish behavior
# ---------------------------------------------------------------------------


class TestSSEPublish:
    def test_publish_fires_on_cache_miss(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("JARVIS_GOAL_INFERENCE_ENABLED", "true")
        gi.reset_default_engine()
        engine = gi.GoalInferenceEngine(repo_root=tmp_path)
        with mock.patch(
            "backend.core.ouroboros.governance.ide_observability_stream."
            "publish_goal_inference_built",
        ) as pub:
            engine.build(force=True)
            assert pub.called
            kwargs = pub.call_args.kwargs
            assert "built_at" in kwargs
            assert "build_ms" in kwargs
            assert "total_samples" in kwargs
            assert "hypotheses_count" in kwargs
            assert "build_reason" in kwargs
            assert kwargs["build_reason"] in (
                "first_build", "refresh_elapsed",
            )

    def test_publish_skipped_on_cache_hit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("JARVIS_GOAL_INFERENCE_ENABLED", "true")
        gi.reset_default_engine()
        engine = gi.GoalInferenceEngine(repo_root=tmp_path)
        engine.build(force=True)  # First build populates cache
        with mock.patch(
            "backend.core.ouroboros.governance.ide_observability_stream."
            "publish_goal_inference_built",
        ) as pub:
            # Subsequent build() within refresh window returns cached;
            # no rebuild => no SSE publish.
            engine.build()
            assert not pub.called

    def test_publish_failure_does_not_break_build(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Defensive: publisher exception MUST be swallowed --
        the engine's primary job is producing inferences, not
        telemetry delivery (mirror semantic_index pattern)."""
        monkeypatch.setenv("JARVIS_GOAL_INFERENCE_ENABLED", "true")
        gi.reset_default_engine()
        engine = gi.GoalInferenceEngine(repo_root=tmp_path)
        with mock.patch(
            "backend.core.ouroboros.governance.ide_observability_stream."
            "publish_goal_inference_built",
            side_effect=RuntimeError("synthetic publisher failure"),
        ):
            result = engine.build(force=True)
            assert result is not None
            assert result.build_reason in (
                "first_build", "refresh_elapsed",
            )

    def test_publish_carries_top_theme_when_inferences_present(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("JARVIS_GOAL_INFERENCE_ENABLED", "true")
        gi.reset_default_engine()
        engine = gi.GoalInferenceEngine(repo_root=tmp_path)
        # Inject a synthetic InferredGoal in cache, then force rebuild.
        # Easier: just call build() against the live repo and inspect.
        captured = {}
        def _capture(**kwargs):
            captured.update(kwargs)
            return "evt-id-stub"
        with mock.patch(
            "backend.core.ouroboros.governance.ide_observability_stream."
            "publish_goal_inference_built",
            side_effect=_capture,
        ):
            engine.build(force=True)
        # Live repo has commits, so we expect non-zero hypotheses
        # at least most runs. The contract we pin is that the
        # publish kwargs always carry the projection fields.
        assert "top_theme" in captured
        assert "top_confidence" in captured
        assert "sources_contributing" in captured
        assert isinstance(captured["hypotheses_count"], int)


# ---------------------------------------------------------------------------
# Publisher contract (in ide_observability_stream)
# ---------------------------------------------------------------------------


class TestPublisherContract:
    def test_publisher_returns_none_when_stream_disabled(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from backend.core.ouroboros.governance import (
            ide_observability_stream as ios,
        )
        with mock.patch.object(ios, "stream_enabled", return_value=False):
            r = ios.publish_goal_inference_built(
                built_at=1.0, build_ms=1, total_samples=0,
                hypotheses_count=0,
            )
            assert r is None

    def test_publisher_returns_none_on_publish_exception(self) -> None:
        from backend.core.ouroboros.governance import (
            ide_observability_stream as ios,
        )
        class _BrokenBroker:
            def publish(self, *a, **kw):
                raise RuntimeError("synthetic")
        with mock.patch.object(
            ios, "stream_enabled", return_value=True,
        ), mock.patch.object(
            ios, "get_default_broker", return_value=_BrokenBroker(),
        ):
            r = ios.publish_goal_inference_built(
                built_at=1.0, build_ms=1, total_samples=0,
                hypotheses_count=0,
            )
            assert r is None
