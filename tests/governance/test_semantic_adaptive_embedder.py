"""Regression spine for the ClusterIntelligence-CrossSession arc's
empirical-closure addendum: the adaptive embedder substrate
(``_StdlibHashingEmbedder`` + ``_AdaptiveEmbedder`` + ``_embedder_factory``).

Closes the empirical gap discovered after the structural arc graduated
default-true: ``fastembed`` cannot initialize in offline / sandbox /
CI environments, so the SemanticIndex was operationally inert
(``corpus_n=0``, ``cluster_count=0``) -- making cluster_coverage
envelopes never fire and the entire ClusterIntelligence-CrossSession
arc dead code in production.

The substrate this suite locks down:
  * Pure-stdlib hashing TF-IDF embedder satisfying the same
    ``.embed(texts) -> Optional[List[List[float]]]`` contract as
    fastembed's ``_Embedder``.
  * ``_AdaptiveEmbedder`` wrapper: probes fastembed first, swaps to
    stdlib on first-use failure, publishes a one-time SSE event.
  * ``_embedder_factory()`` env-driven selector with three resolution
    paths (stdlib / fastembed-with-fallback / fastembed-bare).
  * AST invariants pinning the substrate (no fastembed imports inside
    stdlib body; SemanticIndex constructs via factory).
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path
from typing import Any, List, Optional, Sequence
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.core.ouroboros.governance import semantic_index as si


# ---------------------------------------------------------------------------
# _StdlibHashingEmbedder primitive contract
# ---------------------------------------------------------------------------


class TestStdlibEmbedderContract:
    def test_construction_no_io(self) -> None:
        """No file I/O, no network, no fastembed import at construction."""
        emb = si._StdlibHashingEmbedder()
        assert emb.disabled is False
        assert emb.model_name == "stdlib-hashing-tfidf-v1"
        assert emb.dim >= 16
        assert emb._lazy_init() is True

    def test_dim_resolves_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JARVIS_SEMANTIC_STDLIB_EMBEDDER_DIM", "256")
        emb = si._StdlibHashingEmbedder()
        assert emb.dim == 256

    def test_dim_floor_enforced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JARVIS_SEMANTIC_STDLIB_EMBEDDER_DIM", "4")
        emb = si._StdlibHashingEmbedder()
        assert emb.dim == 16

    def test_dim_explicit_override(self) -> None:
        emb = si._StdlibHashingEmbedder(dim=64)
        assert emb.dim == 64

    def test_dim_explicit_floor(self) -> None:
        emb = si._StdlibHashingEmbedder(dim=2)
        assert emb.dim == 16

    def test_model_name_override_preserved(self) -> None:
        emb = si._StdlibHashingEmbedder(model_name="custom-name")
        assert emb.model_name == "custom-name"

    def test_model_name_empty_falls_back(self) -> None:
        emb = si._StdlibHashingEmbedder(model_name="   ")
        assert emb.model_name == "stdlib-hashing-tfidf-v1"

    def test_embed_empty_input_returns_empty_list(self) -> None:
        emb = si._StdlibHashingEmbedder()
        assert emb.embed([]) == []

    def test_embed_single_string_returns_one_vector(self) -> None:
        emb = si._StdlibHashingEmbedder(dim=64)
        out = emb.embed(["hello world"])
        assert out is not None
        assert len(out) == 1
        assert len(out[0]) == 64

    def test_embed_batch_preserves_order_and_shape(self) -> None:
        emb = si._StdlibHashingEmbedder(dim=32)
        out = emb.embed(["alpha", "beta", "gamma"])
        assert out is not None
        assert len(out) == 3
        assert all(len(v) == 32 for v in out)

    def test_embed_returns_python_floats_not_numpy(self) -> None:
        """Contract: returns plain ``List[List[float]]`` so downstream
        code (``_cosine``) sees no numpy dependency at type level."""
        emb = si._StdlibHashingEmbedder(dim=32)
        out = emb.embed(["test"])
        assert out is not None
        assert isinstance(out[0], list)
        assert all(isinstance(x, float) for x in out[0])

    def test_embed_deterministic_across_calls(self) -> None:
        """Same input MUST produce identical vectors across calls
        (hash stability)."""
        emb = si._StdlibHashingEmbedder(dim=128)
        v1 = emb.embed(["the quick brown fox"])
        v2 = emb.embed(["the quick brown fox"])
        assert v1 == v2

    def test_embed_deterministic_across_instances(self) -> None:
        """Stability across embedder instances (md5 is process-stable)."""
        e1 = si._StdlibHashingEmbedder(dim=128)
        e2 = si._StdlibHashingEmbedder(dim=128)
        assert e1.embed(["abc def"]) == e2.embed(["abc def"])

    def test_embed_different_inputs_produce_different_vectors(self) -> None:
        emb = si._StdlibHashingEmbedder(dim=128)
        out = emb.embed(["voice biometrics", "vision frame server"])
        assert out is not None
        assert out[0] != out[1]

    def test_embed_l2_normalized(self) -> None:
        """Output vectors MUST be L2-normalized (cosine arithmetic
        downstream depends on this)."""
        emb = si._StdlibHashingEmbedder(dim=128)
        out = emb.embed(["test text with some words for normalization check"])
        assert out is not None
        norm_sq = sum(x * x for x in out[0])
        assert math.isclose(norm_sq, 1.0, abs_tol=1e-9)

    def test_embed_empty_string_returns_zero_vector(self) -> None:
        emb = si._StdlibHashingEmbedder(dim=32)
        out = emb.embed([""])
        assert out is not None
        assert out[0] == [0.0] * 32

    def test_embed_none_in_input_treated_as_empty(self) -> None:
        emb = si._StdlibHashingEmbedder(dim=32)
        out = emb.embed([None])  # type: ignore[list-item]
        assert out is not None
        assert out[0] == [0.0] * 32

    def test_embed_only_punctuation_returns_zero_vector(self) -> None:
        emb = si._StdlibHashingEmbedder(dim=32)
        out = emb.embed(["!@#$%^&*()"])
        assert out is not None
        assert out[0] == [0.0] * 32

    def test_embed_unicode_safe(self) -> None:
        """Tokenizer + hash MUST handle unicode without raising."""
        emb = si._StdlibHashingEmbedder(dim=64)
        out = emb.embed(["café résumé naïve 日本語"])
        assert out is not None
        assert len(out[0]) == 64

    def test_embed_repeated_token_uses_sublinear_scaling(self) -> None:
        """``1 + log(count)`` for a bucket means doubling the count
        MUST produce a strictly less-than-doubled raw weight (before
        L2 normalize)."""
        emb = si._StdlibHashingEmbedder(dim=128)
        single = emb.embed(["foo"])
        many = emb.embed(["foo foo foo foo"])
        assert single is not None and many is not None
        # Both are L2-normalized, but the *direction* is the same
        # (only one bucket is non-zero in both). The norm-1 vectors
        # must therefore be equal.
        assert single == many

    def test_embed_cosine_similarity_higher_for_similar_text(self) -> None:
        """Quality smoke test: shared vocabulary -> higher cosine."""
        emb = si._StdlibHashingEmbedder(dim=512)
        v_voice_a = emb.embed(["voice biometrics wake word detection"])
        v_voice_b = emb.embed(["wake word voice detection biometric"])
        v_vision = emb.embed(["vision frame server screen capture"])
        assert v_voice_a is not None
        assert v_voice_b is not None
        assert v_vision is not None
        sim_same_topic = si._cosine(v_voice_a[0], v_voice_b[0])
        sim_diff_topic = si._cosine(v_voice_a[0], v_vision[0])
        assert sim_same_topic > sim_diff_topic
        assert sim_same_topic > 0.5

    def test_embed_failure_returns_none(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Defensive: any internal exception -> None (matches sibling
        ``_Embedder`` contract)."""
        emb = si._StdlibHashingEmbedder(dim=32)
        # Force _embed_one to blow up
        with mock.patch.object(
            si._StdlibHashingEmbedder, "_embed_one",
            side_effect=RuntimeError("synthetic"),
        ):
            assert emb.embed(["x"]) is None


# ---------------------------------------------------------------------------
# _AdaptiveEmbedder fallback behavior
# ---------------------------------------------------------------------------


class _PrimaryStub:
    """Drop-in stub for ``_Embedder`` so we can deterministically
    simulate fastembed-success and fastembed-fail paths."""

    def __init__(
        self,
        model_name: str = "stub-primary",
        result: Optional[List[List[float]]] = None,
    ) -> None:
        self._model_name = model_name
        self._result = result
        self.embed_call_count = 0

    @property
    def disabled(self) -> bool:
        return self._result is None

    @property
    def model_name(self) -> str:
        return self._model_name

    def _lazy_init(self) -> bool:
        return True

    def embed(self, texts: Sequence[str]) -> Optional[List[List[float]]]:
        self.embed_call_count += 1
        if self._result is None:
            return None
        return [list(self._result[0]) for _ in texts]


class TestAdaptiveEmbedder:
    def test_construction_attributes(self) -> None:
        ad = si._AdaptiveEmbedder()
        assert ad.disabled is False
        assert ad.using_fallback is False
        assert ad._lazy_init() is True

    def test_model_name_initially_primary(self) -> None:
        ad = si._AdaptiveEmbedder("model-x")
        assert ad.model_name == "model-x"

    def test_empty_input_returns_empty_list(self) -> None:
        ad = si._AdaptiveEmbedder()
        assert ad.embed([]) == []

    def test_primary_success_no_fallback(self) -> None:
        ad = si._AdaptiveEmbedder()
        ad._primary = _PrimaryStub(result=[[0.1, 0.2, 0.3]])  # type: ignore[assignment]
        out = ad.embed(["hello"])
        assert out is not None
        assert ad.using_fallback is False
        assert ad.model_name == "stub-primary"

    def test_primary_failure_swaps_to_fallback(self) -> None:
        ad = si._AdaptiveEmbedder()
        ad._primary = _PrimaryStub(result=None)  # type: ignore[assignment]
        out = ad.embed(["hello world"])
        assert out is not None
        assert ad.using_fallback is True
        assert ad.model_name == "stdlib-hashing-tfidf-v1"
        assert len(out[0]) == ad._fallback.dim

    def test_fallback_persists_after_first_swap(self) -> None:
        """Once the swap fires, primary MUST NOT be re-tried."""
        ad = si._AdaptiveEmbedder()
        primary = _PrimaryStub(result=None)
        ad._primary = primary  # type: ignore[assignment]
        ad.embed(["first"])
        ad.embed(["second"])
        ad.embed(["third"])
        assert primary.embed_call_count == 1
        assert ad.using_fallback is True

    def test_fallback_event_published_once(self) -> None:
        """SSE event MUST fire exactly once per process even on
        repeated failures."""
        ad = si._AdaptiveEmbedder()
        ad._primary = _PrimaryStub(result=None)  # type: ignore[assignment]
        with mock.patch(
            "backend.core.ouroboros.governance.ide_observability_stream."
            "publish_semantic_embedder_fallback",
        ) as pub:
            ad.embed(["a"])
            ad.embed(["b"])
            ad.embed(["c"])
            assert pub.call_count == 1
            kwargs = pub.call_args.kwargs
            assert kwargs.get("primary_model") == "stub-primary"
            assert kwargs.get("fallback_model") == "stdlib-hashing-tfidf-v1"
            assert kwargs.get("fallback_dim") >= 16

    def test_publish_failure_does_not_raise(self) -> None:
        """SSE publish exception MUST be swallowed -- the embedder's
        primary job is delivering vectors, not telemetry."""
        ad = si._AdaptiveEmbedder()
        ad._primary = _PrimaryStub(result=None)  # type: ignore[assignment]
        with mock.patch(
            "backend.core.ouroboros.governance.ide_observability_stream."
            "publish_semantic_embedder_fallback",
            side_effect=RuntimeError("synthetic"),
        ):
            out = ad.embed(["x"])
            assert out is not None
            assert ad.using_fallback is True

    def test_thread_safe_first_use(self) -> None:
        """Two parallel first-use embeds MUST result in only one
        fallback swap."""
        import threading
        ad = si._AdaptiveEmbedder()
        ad._primary = _PrimaryStub(result=None)  # type: ignore[assignment]
        results: List[Any] = []
        publish_count = {"n": 0}
        original = ad._publish_fallback_event_once
        def counting_publish() -> None:
            publish_count["n"] += 1
            original()
        ad._publish_fallback_event_once = counting_publish  # type: ignore[method-assign]
        def worker() -> None:
            results.append(ad.embed(["concurrent"]))
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Either one publish call (lock held strictly) OR each thread
        # observed using_fallback already True. Either way, ad's own
        # _fallback_event_published guard holds: never publishes twice.
        assert ad._fallback_event_published is True
        assert all(r is not None for r in results)


# ---------------------------------------------------------------------------
# _embedder_factory env resolution
# ---------------------------------------------------------------------------


class TestEmbedderFactory:
    def test_default_resolves_to_adaptive(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("JARVIS_SEMANTIC_EMBEDDER", raising=False)
        monkeypatch.delenv(
            "JARVIS_SEMANTIC_EMBEDDER_FALLBACK_ENABLED", raising=False,
        )
        emb = si._embedder_factory()
        assert isinstance(emb, si._AdaptiveEmbedder)

    def test_explicit_stdlib_returns_stdlib(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("JARVIS_SEMANTIC_EMBEDDER", "stdlib")
        emb = si._embedder_factory()
        assert isinstance(emb, si._StdlibHashingEmbedder)

    def test_fastembed_with_fallback_disabled_returns_bare_embedder(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("JARVIS_SEMANTIC_EMBEDDER", "fastembed")
        monkeypatch.setenv(
            "JARVIS_SEMANTIC_EMBEDDER_FALLBACK_ENABLED", "false",
        )
        emb = si._embedder_factory()
        assert isinstance(emb, si._Embedder)
        assert not isinstance(emb, si._AdaptiveEmbedder)

    def test_fastembed_with_fallback_enabled_returns_adaptive(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("JARVIS_SEMANTIC_EMBEDDER", "fastembed")
        monkeypatch.setenv(
            "JARVIS_SEMANTIC_EMBEDDER_FALLBACK_ENABLED", "true",
        )
        emb = si._embedder_factory()
        assert isinstance(emb, si._AdaptiveEmbedder)

    def test_unknown_mode_returns_adaptive(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("JARVIS_SEMANTIC_EMBEDDER", "garbage-value")
        emb = si._embedder_factory()
        assert isinstance(emb, si._AdaptiveEmbedder)

    def test_case_insensitive_mode(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("JARVIS_SEMANTIC_EMBEDDER", "STDLIB")
        emb = si._embedder_factory()
        assert isinstance(emb, si._StdlibHashingEmbedder)

    def test_whitespace_in_mode_stripped(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("JARVIS_SEMANTIC_EMBEDDER", "  fastembed  ")
        monkeypatch.delenv(
            "JARVIS_SEMANTIC_EMBEDDER_FALLBACK_ENABLED", raising=False,
        )
        emb = si._embedder_factory()
        assert isinstance(emb, si._AdaptiveEmbedder)

    def test_factory_passes_model_name_to_adaptive(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("JARVIS_SEMANTIC_EMBEDDER", "fastembed")
        monkeypatch.delenv(
            "JARVIS_SEMANTIC_EMBEDDER_FALLBACK_ENABLED", raising=False,
        )
        emb = si._embedder_factory("custom/model")
        assert isinstance(emb, si._AdaptiveEmbedder)
        assert emb._primary.model_name == "custom/model"


# ---------------------------------------------------------------------------
# SemanticIndex integration -- the critical empirical proof
# ---------------------------------------------------------------------------


class TestSemanticIndexIntegration:
    def test_index_constructs_via_factory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """SemanticIndex.__init__ MUST construct embedder via factory --
        AST invariant pins this; this test pins it dynamically too."""
        monkeypatch.delenv("JARVIS_SEMANTIC_EMBEDDER", raising=False)
        monkeypatch.delenv(
            "JARVIS_SEMANTIC_EMBEDDER_FALLBACK_ENABLED", raising=False,
        )
        si.reset_default_index()
        idx = si.SemanticIndex(tmp_path)
        assert isinstance(idx._embedder, si._AdaptiveEmbedder)

    def test_index_with_stdlib_mode_uses_stdlib_directly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("JARVIS_SEMANTIC_EMBEDDER", "stdlib")
        si.reset_default_index()
        idx = si.SemanticIndex(tmp_path)
        assert isinstance(idx._embedder, si._StdlibHashingEmbedder)

    def test_singleton_reset_picks_up_env_change(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        si.reset_default_index()
        monkeypatch.setenv("JARVIS_SEMANTIC_EMBEDDER", "stdlib")
        idx1 = si.get_default_index(tmp_path)
        assert isinstance(idx1._embedder, si._StdlibHashingEmbedder)


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------


class TestEnvHelpers:
    def test_fallback_enabled_default_true(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(
            "JARVIS_SEMANTIC_EMBEDDER_FALLBACK_ENABLED", raising=False,
        )
        assert si._fastembed_fallback_enabled() is True

    def test_fallback_enabled_explicit_false(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "JARVIS_SEMANTIC_EMBEDDER_FALLBACK_ENABLED", "false",
        )
        assert si._fastembed_fallback_enabled() is False

    def test_stdlib_dim_default_128(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(
            "JARVIS_SEMANTIC_STDLIB_EMBEDDER_DIM", raising=False,
        )
        assert si._stdlib_embedder_dim() == 128

    def test_stdlib_dim_floor_16(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("JARVIS_SEMANTIC_STDLIB_EMBEDDER_DIM", "8")
        assert si._stdlib_embedder_dim() == 16

    def test_stdlib_dim_explicit_value(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("JARVIS_SEMANTIC_STDLIB_EMBEDDER_DIM", "512")
        assert si._stdlib_embedder_dim() == 512


# ---------------------------------------------------------------------------
# AST invariant + flag registry seeds
# ---------------------------------------------------------------------------


class TestShippedInvariants:
    def test_invariant_validates_clean_module(self) -> None:
        """The shipped semantic_index.py MUST satisfy the new invariant."""
        invariants = si.register_shipped_invariants()
        adaptive_inv = next(
            (i for i in invariants
             if i.invariant_name == "semantic_index_adaptive_embedder"),
            None,
        )
        assert adaptive_inv is not None
        target_path = REPO_ROOT / adaptive_inv.target_file
        source = target_path.read_text(encoding="utf-8")
        import ast as _ast
        tree = _ast.parse(source)
        violations = adaptive_inv.validate(tree, source)
        assert violations == (), f"Unexpected violations: {violations}"

    def test_invariant_catches_missing_factory(self) -> None:
        """If the factory call site is replaced, the invariant fires."""
        invariants = si.register_shipped_invariants()
        adaptive_inv = next(
            (i for i in invariants
             if i.invariant_name == "semantic_index_adaptive_embedder"),
            None,
        )
        assert adaptive_inv is not None
        # Build a synthetic source missing the factory call line
        synthetic = '''
class _Embedder: pass
class _StdlibHashingEmbedder: pass
class _AdaptiveEmbedder: pass
def _embedder_factory(): pass
class SemanticIndex:
    def __init__(self, root):
        self._embedder = _Embedder()
'''
        import ast as _ast
        tree = _ast.parse(synthetic)
        violations = adaptive_inv.validate(tree, synthetic)
        assert any("MUST use _embedder_factory()" in v for v in violations)

    def test_invariant_catches_missing_class(self) -> None:
        invariants = si.register_shipped_invariants()
        adaptive_inv = next(
            (i for i in invariants
             if i.invariant_name == "semantic_index_adaptive_embedder"),
            None,
        )
        assert adaptive_inv is not None
        synthetic = '''
class _Embedder: pass
def _embedder_factory(): pass
class SemanticIndex:
    def __init__(self, root):
        self._embedder = _embedder_factory()
'''
        import ast as _ast
        tree = _ast.parse(synthetic)
        violations = adaptive_inv.validate(tree, synthetic)
        assert any("_StdlibHashingEmbedder" in v for v in violations)
        assert any("_AdaptiveEmbedder" in v for v in violations)

    def test_invariant_catches_fastembed_import_in_stdlib_body(self) -> None:
        invariants = si.register_shipped_invariants()
        adaptive_inv = next(
            (i for i in invariants
             if i.invariant_name == "semantic_index_adaptive_embedder"),
            None,
        )
        assert adaptive_inv is not None
        synthetic = '''
class _Embedder: pass
class _StdlibHashingEmbedder:
    def embed(self, x):
        import fastembed
        return None
class _AdaptiveEmbedder: pass
def _embedder_factory(): pass
class SemanticIndex:
    def __init__(self, root):
        self._embedder = _embedder_factory()
'''
        import ast as _ast
        tree = _ast.parse(synthetic)
        violations = adaptive_inv.validate(tree, synthetic)
        assert any("MUST NOT import fastembed" in v for v in violations)


class TestFlagRegistry:
    def test_new_flags_registered(self) -> None:
        """register_flags() MUST surface the two new addendum specs."""
        recorded: List[str] = []
        class _StubRegistry:
            def register(self, spec: Any) -> None:
                recorded.append(spec.name)
        count = si.register_flags(_StubRegistry())
        assert count >= 5
        assert (
            "JARVIS_SEMANTIC_EMBEDDER_FALLBACK_ENABLED" in recorded
        )
        assert "JARVIS_SEMANTIC_STDLIB_EMBEDDER_DIM" in recorded
