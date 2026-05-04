"""Shared scoring primitives tests (Decision C2 from M11 Slice 3).

Pins the cross-arc shape of the four primitives that
:mod:`failure_mode_memory` Slice 3 originated and M11 Slice 3
extracted into :mod:`_scoring_primitives`. Both Upgrade 3 and M11
import from this shared module; future arcs (Upgrade 1, M9) will
too.

Test layout:
  § 1 — recency_weight (literal formula parity pin)
  § 2 — jaccard_similarity
  § 3 — weight_score (with caller-tunable reference)
  § 4 — diversity_dedup (with key_fn)
  § 5 — Authority floor (stdlib-only pin)
  § 6 — Cross-module parity (failure_mode_memory + M11 use same
        primitives via the shared module)
"""
from __future__ import annotations

from pathlib import Path


# ---------------------------------------------------------------------------
# § 1 — recency_weight (literal formula parity)
# ---------------------------------------------------------------------------


class TestRecencyWeight:
    def test_zero_age_is_one(self):
        from backend.core.ouroboros.governance._scoring_primitives import (  # noqa: E501
            recency_weight,
        )
        assert recency_weight(0.0, 14.0) == 1.0

    def test_one_halflife_is_half(self):
        from backend.core.ouroboros.governance._scoring_primitives import (  # noqa: E501
            recency_weight,
        )
        assert abs(recency_weight(14.0 * 86400.0, 14.0) - 0.5) < 1e-9

    def test_two_halflives_is_quarter(self):
        from backend.core.ouroboros.governance._scoring_primitives import (  # noqa: E501
            recency_weight,
        )
        assert abs(recency_weight(28.0 * 86400.0, 14.0) - 0.25) < 1e-9

    def test_negative_age_clamps_to_one(self):
        from backend.core.ouroboros.governance._scoring_primitives import (  # noqa: E501
            recency_weight,
        )
        assert recency_weight(-100.0, 14.0) == 1.0

    def test_zero_halflife_clamps_to_one(self):
        from backend.core.ouroboros.governance._scoring_primitives import (  # noqa: E501
            recency_weight,
        )
        assert recency_weight(86400.0, 0.0) == 1.0

    def test_literal_formula_parity(self):
        """Pinned by direct mathematical comparison — any future
        divergence from ``0.5 ** (age_d / hl_d)`` trips."""
        from backend.core.ouroboros.governance._scoring_primitives import (  # noqa: E501
            recency_weight,
        )
        for age_d, hl_d in (
            (0.0, 14.0), (7.0, 14.0), (14.0, 14.0),
            (28.0, 14.0), (1.0, 1.0), (3.5, 7.0),
        ):
            expected = 0.5 ** (age_d / hl_d)
            actual = recency_weight(age_d * 86400.0, hl_d)
            assert abs(actual - expected) < 1e-9


# ---------------------------------------------------------------------------
# § 2 — jaccard_similarity
# ---------------------------------------------------------------------------


class TestJaccardSimilarity:
    def test_identical_sets(self):
        from backend.core.ouroboros.governance._scoring_primitives import (  # noqa: E501
            jaccard_similarity,
        )
        assert jaccard_similarity(
            ("a.py", "b.py"), ("a.py", "b.py"),
        ) == 1.0

    def test_disjoint_sets(self):
        from backend.core.ouroboros.governance._scoring_primitives import (  # noqa: E501
            jaccard_similarity,
        )
        assert jaccard_similarity(("a.py",), ("b.py",)) == 0.0

    def test_partial_overlap(self):
        from backend.core.ouroboros.governance._scoring_primitives import (  # noqa: E501
            jaccard_similarity,
        )
        result = jaccard_similarity(
            ("a.py", "b.py"), ("b.py", "c.py"),
        )
        assert abs(result - 1.0 / 3.0) < 1e-9

    def test_both_empty_is_one(self):
        from backend.core.ouroboros.governance._scoring_primitives import (  # noqa: E501
            jaccard_similarity,
        )
        assert jaccard_similarity((), ()) == 1.0

    def test_handles_garbage_iterable(self):
        from backend.core.ouroboros.governance._scoring_primitives import (  # noqa: E501
            jaccard_similarity,
        )
        assert jaccard_similarity(42, 17) == 0.0  # type: ignore[arg-type]

    def test_filters_empty_strings(self):
        """Empty-string elements filtered before set construction."""
        from backend.core.ouroboros.governance._scoring_primitives import (  # noqa: E501
            jaccard_similarity,
        )
        a = jaccard_similarity(("a.py", ""), ("a.py",))
        assert a == 1.0


# ---------------------------------------------------------------------------
# § 3 — weight_score (caller-tunable reference)
# ---------------------------------------------------------------------------


class TestWeightScore:
    def test_weight_zero_is_zero(self):
        from backend.core.ouroboros.governance._scoring_primitives import (  # noqa: E501
            weight_score,
        )
        assert weight_score(0) == 0.0

    def test_default_reference_saturates_at_ten(self):
        from backend.core.ouroboros.governance._scoring_primitives import (  # noqa: E501
            weight_score,
        )
        assert weight_score(10) == 1.0

    def test_default_reference_caps_above(self):
        from backend.core.ouroboros.governance._scoring_primitives import (  # noqa: E501
            weight_score,
        )
        assert weight_score(50) == 1.0
        assert weight_score(1000) == 1.0

    def test_custom_reference_changes_curve(self):
        """Caller-tunable: smaller reference saturates faster."""
        from backend.core.ouroboros.governance._scoring_primitives import (  # noqa: E501
            weight_score,
        )
        # weight=5 with reference=5 saturates; with reference=10
        # it shouldn't yet
        assert weight_score(5, reference=5) == 1.0
        assert weight_score(5, reference=10) < 1.0

    def test_zero_reference_returns_zero(self):
        from backend.core.ouroboros.governance._scoring_primitives import (  # noqa: E501
            weight_score,
        )
        assert weight_score(5, reference=0) == 0.0

    def test_log_scale_compresses_outliers(self):
        from backend.core.ouroboros.governance._scoring_primitives import (  # noqa: E501
            weight_score,
        )
        ratio = weight_score(50) / weight_score(2)
        assert ratio < 3.0


# ---------------------------------------------------------------------------
# § 4 — diversity_dedup (caller-supplied key_fn)
# ---------------------------------------------------------------------------


class TestDiversityDedup:
    def test_unique_keys_all_returned(self):
        from backend.core.ouroboros.governance._scoring_primitives import (  # noqa: E501
            diversity_dedup,
        )
        items = [("A", 0.9), ("B", 0.8), ("C", 0.7)]
        result = diversity_dedup(
            items, top_k=3, key_fn=lambda x: x[0],
        )
        assert len(result) == 3
        assert {x[0] for x in result} == {"A", "B", "C"}

    def test_duplicate_keys_deduped_when_pool_diverse(self):
        from backend.core.ouroboros.governance._scoring_primitives import (  # noqa: E501
            diversity_dedup,
        )
        items = [
            ("A", 1.0), ("A", 0.9), ("B", 0.8), ("C", 0.7),
        ]
        result = diversity_dedup(
            items, top_k=3, key_fn=lambda x: x[0],
        )
        keys = [x[0] for x in result]
        assert keys == ["A", "B", "C"]  # 2nd A skipped

    def test_overflow_fills_when_diversity_exhausted(self):
        from backend.core.ouroboros.governance._scoring_primitives import (  # noqa: E501
            diversity_dedup,
        )
        items = [("A", 1.0 - i * 0.1) for i in range(5)]
        result = diversity_dedup(
            items, top_k=3, key_fn=lambda x: x[0],
        )
        assert len(result) == 3

    def test_top_k_zero_returns_empty(self):
        from backend.core.ouroboros.governance._scoring_primitives import (  # noqa: E501
            diversity_dedup,
        )
        assert diversity_dedup([], top_k=0, key_fn=str) == tuple()

    def test_key_fn_failure_treated_as_empty_key(self):
        """Defensive: if key_fn raises on a match, that match is
        treated as having empty-string key (degraded; never blocks
        retrieval)."""
        from backend.core.ouroboros.governance._scoring_primitives import (  # noqa: E501
            diversity_dedup,
        )
        def evil_key(x):
            if x == "boom":
                raise ValueError("hostile")
            return x
        items = ["A", "boom", "B"]
        # Should NOT raise; "boom" gets empty key, "B" has unique key
        result = diversity_dedup(
            items, top_k=3, key_fn=evil_key,
        )
        # Order in primary: A (key=A), boom (key=""), B (key=B)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# § 5 — Authority floor (stdlib-only pin)
# ---------------------------------------------------------------------------


class TestAuthorityFloor:
    def test_imports_stdlib_only(self):
        """The shared scoring primitives module is the LOWEST
        authority floor in the package — it MUST NOT pull in
        any governance code. If any sibling arc starts importing
        from this module + then re-importing back into a
        governance dep, we'd create a cycle. The stdlib-only pin
        breaks that ahead of time."""
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "_scoring_primitives.py"
        )
        source = path.read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                continue
            if stripped.startswith('"'):
                continue
            if stripped.startswith("from "):
                # MUST be stdlib (no backend.* / governance.*)
                assert not stripped.startswith(
                    "from backend.",
                ), (
                    f"_scoring_primitives must be stdlib-only: "
                    f"{stripped!r}"
                )

    def test_module_imports_resolve_in_isolation(self):
        from backend.core.ouroboros.governance import (  # noqa: F401
            _scoring_primitives,
        )


# ---------------------------------------------------------------------------
# § 6 — Cross-module parity (Upgrade 3 + M11 use same primitives)
# ---------------------------------------------------------------------------


class TestCrossModuleParity:
    def test_upgrade_3_recency_uses_shared_primitive(self):
        """Refactor pin: failure_mode_memory._recency_weight
        delegates to _scoring_primitives.recency_weight. Both
        produce identical output for identical input."""
        from backend.core.ouroboros.governance._scoring_primitives import (  # noqa: E501
            recency_weight,
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            _recency_weight as fmm_recency,
        )
        for age_s, hl_d in (
            (0.0, 14.0), (86400.0, 14.0),
            (14.0 * 86400.0, 14.0),
        ):
            assert (
                fmm_recency(age_s, hl_d)
                == recency_weight(age_s, hl_d)
            )

    def test_upgrade_3_jaccard_uses_shared_primitive(self):
        from backend.core.ouroboros.governance._scoring_primitives import (  # noqa: E501
            jaccard_similarity,
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            _jaccard_similarity as fmm_jaccard,
        )
        for a, b in (
            (("a.py",), ("a.py",)),
            (("a.py", "b.py"), ("b.py", "c.py")),
            ((), ()),
        ):
            assert (
                fmm_jaccard(a, b)
                == jaccard_similarity(a, b)
            )

    def test_upgrade_3_weight_score_uses_shared_primitive(self):
        from backend.core.ouroboros.governance._scoring_primitives import (  # noqa: E501
            DEFAULT_WEIGHT_SATURATION_REFERENCE,
            weight_score,
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            _weight_score as fmm_weight_score,
        )
        for w in (0, 1, 2, 5, 10, 50):
            assert (
                fmm_weight_score(w)
                == weight_score(
                    w,
                    reference=DEFAULT_WEIGHT_SATURATION_REFERENCE,
                )
            )
