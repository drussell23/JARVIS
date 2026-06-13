"""Slice 236 — capability-aware DW model selection (the "sixth layer" fix).

Layer-6 (the routing root cause behind the Slice-235 validation soak): the
IMMEDIATE→DW reroute (Claude breaker OPEN) escapes the sealed "Claude-only"
IMMEDIATE route but lands on the UNCONDITIONAL baseline default
(``self._model`` = Qwen-397B), which ``resolve_diff_capability_for_model``
correctly classifies ``full_content_only``. So the Slice-235 gate forces
full_content for large files → the 63K-char blob → JSONDecodeError → exhaustion,
even though entitled diff-capable elites (moonshotai/deepseek-ai/zai-org) rank
ABOVE Qwen in ``JARVIS_DW_FAMILY_PREFERENCE`` and were simply never selected.

This slice makes baseline-default selection CAPABILITY-AWARE: when an op warrants
a large-file patch (the SAME size signal the gate reads — no drift) and the
baseline isn't diff-capable, prefer an entitled diff-capable elite from the
preference order. Pure seam (no env/catalog IO in the core), fail-soft, gated.
OFF / small-file / no-elite-reachable / already-capable-baseline are byte-identical.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

from backend.core.ouroboros.governance.providers import (
    _diff_schema_threshold_lines,
    _max_target_line_count,
    capability_aware_default_model,
    capability_aware_selection_enabled,
    op_warrants_diff_capable_model,
    select_diff_capable_model,
    should_force_full_content,
)

_QWEN = "Qwen/Qwen3.5-397B-A17B-FP8"
_KIMI = "moonshotai/Kimi-K2.6"
_DEEPSEEK = "deepseek-ai/DeepSeek-V4-Pro"
_GLM = "zai-org/GLM-5.1-FP8"
_CAPABLE_FAMILIES = frozenset({"moonshotai", "deepseek-ai", "zai-org"})
_PREF = {"moonshotai": 1.0, "deepseek-ai": 0.9, "zai-org": 0.7, "qwen": 0.5}


class TestOpWarrantsDiffCapableModel:
    def test_large_file_warrants_diff_capable_model(self, tmp_path, monkeypatch):
        monkeypatch.delenv("JARVIS_DIFF_SCHEMA_THRESHOLD_LINES", raising=False)
        thr = _diff_schema_threshold_lines()
        (tmp_path / "big.py").write_text("\n".join(str(i) for i in range(thr + 500)))
        assert op_warrants_diff_capable_model(
            target_files=["big.py"], repo_root=tmp_path,
        ) is True

    def test_small_file_does_not_warrant(self, tmp_path):
        (tmp_path / "small.py").write_text("a\nb\nc\n")
        assert op_warrants_diff_capable_model(
            target_files=["small.py"], repo_root=tmp_path,
        ) is False

    def test_at_threshold_does_not_warrant(self, tmp_path, monkeypatch):
        # strictly greater-than — exactly at the threshold stays full_content,
        # so the op does NOT warrant a diff-capable model (mirrors the gate).
        monkeypatch.setenv("JARVIS_DIFF_SCHEMA_THRESHOLD_LINES", "100")
        (tmp_path / "exact.py").write_text("\n".join(str(i) for i in range(100)))
        assert op_warrants_diff_capable_model(
            target_files=["exact.py"], repo_root=tmp_path,
        ) is False

    def test_missing_files_do_not_warrant(self, tmp_path):
        assert op_warrants_diff_capable_model(
            target_files=["ghost.py"], repo_root=tmp_path,
        ) is False

    def test_fail_soft_false_on_bad_input(self):
        assert op_warrants_diff_capable_model(
            target_files=["x.py"], repo_root=None,
        ) is False

    def test_size_signal_matches_gate_no_drift(self, tmp_path, monkeypatch):
        """The selector's 'needs a diff-capable model' MUST equal the gate's
        'would emit a diff IF the model were capable' — same threshold, same
        line-count primitive — so the two can never drift apart."""
        monkeypatch.delenv("JARVIS_DIFF_SCHEMA_THRESHOLD_LINES", raising=False)
        thr = _diff_schema_threshold_lines()
        (tmp_path / "big.py").write_text("\n".join(str(i) for i in range(thr + 500)))
        (tmp_path / "small.py").write_text("\n".join(str(i) for i in range(10)))
        for rel in ("big.py", "small.py"):
            n = _max_target_line_count([rel], tmp_path)
            warrants = op_warrants_diff_capable_model(
                target_files=[rel], repo_root=tmp_path,
            )
            gate_would_use_diff = should_force_full_content(
                schema_capability="full_content_and_diff",
                target_line_count=n,
                threshold_lines=thr,
            ) is False
            assert warrants == gate_would_use_diff


class TestSelectDiffCapableModel:
    def test_picks_highest_preference_diff_capable_family(self):
        chosen = select_diff_capable_model(
            available_models=[_QWEN, _DEEPSEEK, _KIMI, _GLM],
            family_preference=_PREF,
            diff_capable_families=_CAPABLE_FAMILIES,
        )
        assert chosen == _KIMI  # moonshotai:1.0 ranks highest

    def test_skips_non_diff_capable_even_if_higher_preferred(self):
        # Qwen has a preference weight but is NOT diff-capable → never chosen.
        chosen = select_diff_capable_model(
            available_models=[_QWEN, _GLM],
            family_preference={"qwen": 5.0, "zai-org": 0.1},
            diff_capable_families=_CAPABLE_FAMILIES,
        )
        assert chosen == _GLM

    def test_no_diff_capable_available_returns_none(self):
        chosen = select_diff_capable_model(
            available_models=[_QWEN, "Qwen/Qwen3.5-35B-A3B-FP8"],
            family_preference=_PREF,
            diff_capable_families=_CAPABLE_FAMILIES,
        )
        assert chosen is None

    def test_deterministic_tie_break_by_model_id(self):
        # Two models in the SAME (top) family with no per-model preference →
        # stable ascending model-id tie-break.
        chosen = select_diff_capable_model(
            available_models=["moonshotai/Kimi-Z", "moonshotai/Kimi-A"],
            family_preference={"moonshotai": 1.0},
            diff_capable_families=_CAPABLE_FAMILIES,
        )
        assert chosen == "moonshotai/Kimi-A"

    def test_empty_and_garbage_inputs_return_none(self):
        assert select_diff_capable_model(
            available_models=[], family_preference={}, diff_capable_families=_CAPABLE_FAMILIES,
        ) is None
        assert select_diff_capable_model(
            available_models=["", None, 123], family_preference=_PREF,
            diff_capable_families=_CAPABLE_FAMILIES,
        ) is None

    def test_no_hardcoded_model_name_in_source(self):
        # The selector must NOT pin a specific model id — selection is purely
        # data-driven from the injected catalog + preference + capable families.
        src = inspect.getsource(select_diff_capable_model)
        assert "Kimi" not in src and "moonshotai" not in src
        assert "DeepSeek" not in src and "deepseek" not in src


class TestCapabilityAwareSelectionFlag:
    def test_default_is_enabled(self, monkeypatch):
        monkeypatch.delenv("JARVIS_DW_CAPABILITY_AWARE_SELECTION_ENABLED", raising=False)
        assert capability_aware_selection_enabled() is True

    def test_explicit_off(self, monkeypatch):
        monkeypatch.setenv("JARVIS_DW_CAPABILITY_AWARE_SELECTION_ENABLED", "false")
        assert capability_aware_selection_enabled() is False


class TestCapabilityAwareDefaultModel:
    """The pure orchestration seam: baseline default → capability-aware choice."""

    def _big(self, tmp_path):
        (tmp_path / "big.py").write_text("\n".join(str(i) for i in range(2000)))
        return ["big.py"]

    def test_large_file_non_capable_baseline_picks_elite(self, tmp_path):
        out = capability_aware_default_model(
            base_model=_QWEN,
            target_files=self._big(tmp_path), repo_root=tmp_path,
            available_models=[_QWEN, _KIMI, _DEEPSEEK],
            family_preference=_PREF, diff_capable_families=_CAPABLE_FAMILIES,
            enabled=True,
        )
        assert out == _KIMI

    def test_small_file_keeps_baseline(self, tmp_path):
        # preference order unaffected for small ops — baseline default stands.
        (tmp_path / "small.py").write_text("a\nb\n")
        out = capability_aware_default_model(
            base_model=_QWEN,
            target_files=["small.py"], repo_root=tmp_path,
            available_models=[_QWEN, _KIMI],
            family_preference=_PREF, diff_capable_families=_CAPABLE_FAMILIES,
            enabled=True,
        )
        assert out == _QWEN

    def test_no_elite_reachable_degrades_to_baseline(self, tmp_path):
        # graceful full_content degradation: keep a parser-handleable baseline.
        out = capability_aware_default_model(
            base_model=_QWEN,
            target_files=self._big(tmp_path), repo_root=tmp_path,
            available_models=[_QWEN, "Qwen/Qwen3.5-35B-A3B-FP8"],
            family_preference=_PREF, diff_capable_families=_CAPABLE_FAMILIES,
            enabled=True,
        )
        assert out == _QWEN

    def test_disabled_is_byte_identical(self, tmp_path):
        out = capability_aware_default_model(
            base_model=_QWEN,
            target_files=self._big(tmp_path), repo_root=tmp_path,
            available_models=[_QWEN, _KIMI],
            family_preference=_PREF, diff_capable_families=_CAPABLE_FAMILIES,
            enabled=False,
        )
        assert out == _QWEN

    def test_baseline_already_capable_unchanged(self, tmp_path):
        # If the baseline default is itself diff-capable, no needless switch.
        out = capability_aware_default_model(
            base_model=_KIMI,
            target_files=self._big(tmp_path), repo_root=tmp_path,
            available_models=[_KIMI, _DEEPSEEK],
            family_preference=_PREF, diff_capable_families=_CAPABLE_FAMILIES,
            enabled=True,
        )
        assert out == _KIMI

    def test_fail_soft_returns_baseline(self):
        # bad repo_root → never raise, return baseline.
        out = capability_aware_default_model(
            base_model=_QWEN,
            target_files=["x.py"], repo_root=None,
            available_models=[_QWEN, _KIMI],
            family_preference=_PREF, diff_capable_families=_CAPABLE_FAMILIES,
            enabled=True,
        )
        assert out == _QWEN


class TestProviderWiring:
    """The DoubleWordProvider._resolve_effective_model baseline fallback must
    route through the capability-aware seam (tested via a lightweight stub +
    injected catalog — the heavy provider need not be constructed)."""

    def _call_method(self, *, repo_root, ctx, base):
        from backend.core.ouroboros.governance import doubleword_provider as dwp
        stub = SimpleNamespace(_repo_root=repo_root, _model=base)
        return dwp.DoublewordProvider._capability_aware_default(stub, ctx, base)

    def _inject_catalog(self, monkeypatch, model_ids, pref):
        from backend.core.ouroboros.governance import dw_catalog_client as cat
        from backend.core.ouroboros.governance import dw_catalog_classifier as cls
        monkeypatch.setattr(
            cat, "load_cached_snapshot",
            lambda *a, **k: SimpleNamespace(model_ids=lambda: tuple(model_ids)),
        )
        monkeypatch.setattr(cls, "_family_preference", lambda: dict(pref))

    def test_large_op_routes_baseline_to_entitled_elite(self, tmp_path, monkeypatch):
        monkeypatch.delenv("JARVIS_DW_CAPABILITY_AWARE_SELECTION_ENABLED", raising=False)
        monkeypatch.delenv("JARVIS_DW_DIFF_CAPABLE_FAMILIES", raising=False)
        self._inject_catalog(monkeypatch, [_QWEN, _KIMI, _DEEPSEEK], _PREF)
        (tmp_path / "big.py").write_text("\n".join(str(i) for i in range(2000)))
        ctx = SimpleNamespace(target_files=["big.py"], provider_route="immediate")
        out = self._call_method(repo_root=tmp_path, ctx=ctx, base=_QWEN)
        assert out == _KIMI

    def test_small_op_keeps_baseline(self, tmp_path, monkeypatch):
        monkeypatch.delenv("JARVIS_DW_CAPABILITY_AWARE_SELECTION_ENABLED", raising=False)
        self._inject_catalog(monkeypatch, [_QWEN, _KIMI], _PREF)
        (tmp_path / "small.py").write_text("a\nb\n")
        ctx = SimpleNamespace(target_files=["small.py"], provider_route="immediate")
        out = self._call_method(repo_root=tmp_path, ctx=ctx, base=_QWEN)
        assert out == _QWEN

    def test_disabled_keeps_baseline(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_DW_CAPABILITY_AWARE_SELECTION_ENABLED", "false")
        self._inject_catalog(monkeypatch, [_QWEN, _KIMI], _PREF)
        (tmp_path / "big.py").write_text("\n".join(str(i) for i in range(2000)))
        ctx = SimpleNamespace(target_files=["big.py"], provider_route="immediate")
        out = self._call_method(repo_root=tmp_path, ctx=ctx, base=_QWEN)
        assert out == _QWEN

    def test_no_entitled_elite_keeps_baseline(self, tmp_path, monkeypatch):
        monkeypatch.delenv("JARVIS_DW_CAPABILITY_AWARE_SELECTION_ENABLED", raising=False)
        monkeypatch.delenv("JARVIS_DW_DIFF_CAPABLE_FAMILIES", raising=False)
        self._inject_catalog(monkeypatch, [_QWEN, "Qwen/Qwen3.5-35B-A3B-FP8"], _PREF)
        (tmp_path / "big.py").write_text("\n".join(str(i) for i in range(2000)))
        ctx = SimpleNamespace(target_files=["big.py"], provider_route="immediate")
        out = self._call_method(repo_root=tmp_path, ctx=ctx, base=_QWEN)
        assert out == _QWEN

    def test_resolve_effective_model_wires_capability_aware_default(self):
        # The baseline fallback (self._model) must route through the seam — and
        # the sentinel/topology overrides must NOT (they are deliberate choices).
        from backend.core.ouroboros.governance import doubleword_provider as dwp
        src = inspect.getsource(dwp.DoublewordProvider._resolve_effective_model)
        assert "_capability_aware_default" in src
