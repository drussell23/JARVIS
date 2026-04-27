"""Phase 7.5 — adapted category-weight rebalance boot-time loader pins.

Mirrors 7.1+7.2+7.3+7.4 cage discipline:
  * fail-open paths (master flag off / YAML missing / parse error /
    oversize / non-mapping)
  * per-entry skip (missing new_weights / non-mapping / non-string-keys /
    non-numeric / weight <= 0 / empty)
  * compute_effective_category_weights cage (load-bearing):
      - sum invariant: Σ(new) ≥ Σ(base) — reject if violated
      - per-category floor: ≥ 0.5 × base[k] — reject if violated
      - absolute floor: ≥ MIN_WEIGHT_VALUE — reject if violated
      - schema invariant: output ALWAYS has every base key
      - unknown adapted keys silently dropped
      - latest-occurrence-wins (only the LAST entry is consulted)
      - NEVER raises
  * authority invariants: stdlib + adaptation only; one-way dep
    (does NOT import exploration_engine.py or any orchestrator)
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest import mock

import pytest

from backend.core.ouroboros.governance.adaptation import (
    adapted_category_weight_loader as loader,
)
from backend.core.ouroboros.governance.adaptation.adapted_category_weight_loader import (
    AdaptedRebalanceEntry,
    HALF_OF_BASE,
    MAX_ADAPTED_REBALANCES,
    MAX_WEIGHT_VALUE,
    MAX_YAML_BYTES,
    MIN_WEIGHT_VALUE,
    adapted_category_weights_path,
    compute_effective_category_weights,
    is_loader_enabled,
    load_adapted_rebalances,
)


_BASE = {
    "comprehension": 1.0,
    "discovery": 1.0,
    "call_graph": 1.0,
    "structure": 1.0,
    "history": 1.0,
}


# ---------------------------------------------------------------------------
# Section A — module constants + master flag + dataclass
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_max_adapted_rebalances_is_8(self):
        assert MAX_ADAPTED_REBALANCES == 8

    def test_max_yaml_bytes_is_4MiB(self):
        assert MAX_YAML_BYTES == 4 * 1024 * 1024

    def test_max_weight_value_is_100(self):
        assert MAX_WEIGHT_VALUE == 100.0

    def test_min_weight_value_is_001(self):
        assert MIN_WEIGHT_VALUE == 0.01

    def test_half_of_base_is_05(self):
        assert HALF_OF_BASE == 0.5

    def test_truthy_constant_shape(self):
        assert loader._TRUTHY == ("1", "true", "yes", "on")


class TestMasterFlag:
    def test_default_false(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS",
            raising=False,
        )
        assert is_loader_enabled() is False

    def test_truthy_variants(self, monkeypatch):
        for v in ("1", "true", "TRUE", "Yes", "ON"):
            monkeypatch.setenv(
                "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS",
                v,
            )
            assert is_loader_enabled() is True, v

    def test_falsy_variants(self, monkeypatch):
        for v in ("0", "false", "no", "off", "", " "):
            monkeypatch.setenv(
                "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS",
                v,
            )
            assert is_loader_enabled() is False, v


class TestDataclass:
    def test_frozen(self):
        e = AdaptedRebalanceEntry(
            new_weights={"a": 1.0}, high_value_category="a",
            low_value_category="b", proposal_id="p", approved_at="t",
            approved_by="op",
        )
        with pytest.raises(Exception):  # FrozenInstanceError
            e.proposal_id = "x"  # type: ignore[misc]

    def test_post_init_normalizes_keys_sorted(self):
        # __post_init__ rebuilds new_weights with sorted keys.
        e = AdaptedRebalanceEntry(
            new_weights={"z": 1.0, "a": 2.0, "m": 3.0},
            high_value_category="a", low_value_category="z",
            proposal_id="p", approved_at="t", approved_by="op",
        )
        assert list(e.new_weights.keys()) == ["a", "m", "z"]

    def test_post_init_coerces_float(self):
        e = AdaptedRebalanceEntry(
            new_weights={"a": 1},  # int passed; should become float
            high_value_category="a", low_value_category="a",
            proposal_id="p", approved_at="t", approved_by="op",
        )
        assert isinstance(e.new_weights["a"], float)

    def test_default_path(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_ADAPTED_CATEGORY_WEIGHTS_PATH", raising=False,
        )
        assert adapted_category_weights_path() == (
            Path(".jarvis") / "adapted_category_weights.yaml"
        )

    def test_path_env_override(self, monkeypatch, tmp_path):
        custom = tmp_path / "custom.yaml"
        monkeypatch.setenv(
            "JARVIS_ADAPTED_CATEGORY_WEIGHTS_PATH", str(custom),
        )
        assert adapted_category_weights_path() == custom


# ---------------------------------------------------------------------------
# Section B — master-flag short-circuit
# ---------------------------------------------------------------------------


class TestMasterFlagShortCircuit:
    def test_master_off_returns_empty_even_if_yaml_present(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.delenv(
            "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS",
            raising=False,
        )
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text(
            "schema_version: 1\n"
            "rebalances:\n"
            "  - new_weights: {a: 1.5, b: 0.5}\n"
            "    high_value_category: a\n",
            encoding="utf-8",
        )
        assert load_adapted_rebalances(yaml_path) == []


# ---------------------------------------------------------------------------
# Section C — YAML reader paths
# ---------------------------------------------------------------------------


def _enable(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS", "1",
    )


class TestYAMLReader:
    def test_missing_yaml_returns_empty(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        assert load_adapted_rebalances(tmp_path / "missing.yaml") == []

    def test_oversize_refuses(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "big.yaml"
        yaml_path.write_text("x", encoding="utf-8")
        with mock.patch.object(
            Path, "stat",
            return_value=mock.Mock(st_size=MAX_YAML_BYTES + 1),
        ):
            assert load_adapted_rebalances(yaml_path) == []

    def test_unreadable_returns_empty(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text("rebalances: []\n", encoding="utf-8")
        with mock.patch.object(
            Path, "read_text", side_effect=OSError("permission denied"),
        ):
            assert load_adapted_rebalances(yaml_path) == []

    def test_empty_file_returns_empty(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text("   \n  \n", encoding="utf-8")
        assert load_adapted_rebalances(yaml_path) == []

    def test_no_pyyaml_returns_empty(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text("rebalances: []\n", encoding="utf-8")
        import sys
        sentinel = object()
        original_yaml = sys.modules.pop("yaml", sentinel)
        try:
            import builtins
            real_import = builtins.__import__

            def fake_import(name, *a, **k):
                if name == "yaml":
                    raise ImportError("forced for test")
                return real_import(name, *a, **k)

            with mock.patch.object(builtins, "__import__", side_effect=fake_import):
                assert load_adapted_rebalances(yaml_path) == []
        finally:
            if original_yaml is not sentinel:
                sys.modules["yaml"] = original_yaml  # type: ignore[assignment]

    def test_parse_error_returns_empty(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "bad.yaml"
        yaml_path.write_text(
            "rebalances: [oh no\n  - missing close", encoding="utf-8",
        )
        assert load_adapted_rebalances(yaml_path) == []

    def test_non_mapping_doc_returns_empty(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text("- a\n- list\n", encoding="utf-8")
        assert load_adapted_rebalances(yaml_path) == []

    def test_missing_rebalances_key_returns_empty(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text(
            "schema_version: 1\nother: 7\n", encoding="utf-8",
        )
        assert load_adapted_rebalances(yaml_path) == []

    def test_non_list_rebalances_returns_empty(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text(
            "schema_version: 1\nrebalances: a-string\n",
            encoding="utf-8",
        )
        assert load_adapted_rebalances(yaml_path) == []

    def test_non_mapping_entry_skipped(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text(
            "schema_version: 1\n"
            "rebalances:\n"
            "  - this-is-a-string\n"
            "  - new_weights: {a: 1.5}\n",
            encoding="utf-8",
        )
        out = load_adapted_rebalances(yaml_path)
        assert len(out) == 1
        assert out[0].new_weights == {"a": 1.5}

    def test_max_adapted_rebalances_truncate(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        lines = ["schema_version: 1", "rebalances:"]
        for i in range(MAX_ADAPTED_REBALANCES + 5):
            lines.append(f"  - new_weights: {{a: {1.0 + 0.01 * i}}}")
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        out = load_adapted_rebalances(yaml_path)
        assert len(out) == MAX_ADAPTED_REBALANCES

    def test_happy_path(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text(
            "schema_version: 1\n"
            "rebalances:\n"
            "  - new_weights: {comprehension: 1.20, discovery: 0.90,"
            " call_graph: 1.0, structure: 1.0, history: 1.0}\n"
            "    high_value_category: comprehension\n"
            "    low_value_category: discovery\n"
            "    proposal_id: p-1\n"
            "    approved_at: '2026-04-26T00:00:00Z'\n"
            "    approved_by: op\n",
            encoding="utf-8",
        )
        out = load_adapted_rebalances(yaml_path)
        assert len(out) == 1
        assert out[0].high_value_category == "comprehension"
        assert out[0].new_weights["comprehension"] == 1.20

    def test_clamp_too_large_weight(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text(
            "schema_version: 1\n"
            "rebalances:\n"
            f"  - new_weights: {{a: {MAX_WEIGHT_VALUE + 50}}}\n",
            encoding="utf-8",
        )
        out = load_adapted_rebalances(yaml_path)
        assert out[0].new_weights["a"] == MAX_WEIGHT_VALUE


# ---------------------------------------------------------------------------
# Section D — _parse_entry direct
# ---------------------------------------------------------------------------


class TestParseEntry:
    def test_missing_new_weights_skip(self):
        assert loader._parse_entry({"high_value_category": "a"}, 0) is None

    def test_non_mapping_new_weights_skip(self):
        assert loader._parse_entry({"new_weights": "string"}, 0) is None

    def test_non_string_key_skip(self):
        assert loader._parse_entry(
            {"new_weights": {1: 1.0}}, 0,
        ) is None

    def test_blank_key_skip(self):
        assert loader._parse_entry(
            {"new_weights": {"  ": 1.0}}, 0,
        ) is None

    def test_non_numeric_value_skip(self):
        assert loader._parse_entry(
            {"new_weights": {"a": "high"}}, 0,
        ) is None

    def test_zero_weight_skip(self):
        assert loader._parse_entry(
            {"new_weights": {"a": 0.0}}, 0,
        ) is None

    def test_negative_weight_skip(self):
        assert loader._parse_entry(
            {"new_weights": {"a": -0.1}}, 0,
        ) is None

    def test_clamp_too_large(self):
        e = loader._parse_entry(
            {"new_weights": {"a": MAX_WEIGHT_VALUE + 50}}, 0,
        )
        assert e is not None
        assert e.new_weights["a"] == MAX_WEIGHT_VALUE

    def test_empty_new_weights_skip(self):
        assert loader._parse_entry(
            {"new_weights": {}}, 0,
        ) is None

    def test_lowercases_category_keys(self):
        e = loader._parse_entry(
            {"new_weights": {"COMPREHENSION": 1.0, "Discovery": 1.0}}, 0,
        )
        assert e is not None
        assert "comprehension" in e.new_weights
        assert "discovery" in e.new_weights


# ---------------------------------------------------------------------------
# Section E — compute_effective_category_weights cage
# ---------------------------------------------------------------------------


def _entry(weights, **kwargs):
    return AdaptedRebalanceEntry(
        new_weights=dict(weights),
        high_value_category=kwargs.get("high_value_category", ""),
        low_value_category=kwargs.get("low_value_category", ""),
        proposal_id=kwargs.get("proposal_id", "p"),
        approved_at=kwargs.get("approved_at", ""),
        approved_by=kwargs.get("approved_by", ""),
    )


class TestEffectiveWeights:
    def test_loader_off_returns_base(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS",
            raising=False,
        )
        out = compute_effective_category_weights(_BASE)
        assert out == _BASE
        assert out is not _BASE  # defensive copy

    def test_loader_on_no_entries_returns_base(self, monkeypatch):
        _enable(monkeypatch)
        out = compute_effective_category_weights(_BASE, adapted=[])
        assert out == _BASE

    def test_valid_rebalance_applied(self, monkeypatch):
        _enable(monkeypatch)
        # Total: base = 5.0; new = 1.20 + 0.90 + 1.0 + 1.0 + 1.0 = 5.10
        # Net-tighten: 5.10 ≥ 5.0 ✓
        # Per-cat floor: 0.90 ≥ 0.5 × 1.0 = 0.5 ✓
        out = compute_effective_category_weights(
            _BASE,
            adapted=[_entry({
                "comprehension": 1.20, "discovery": 0.90,
                "call_graph": 1.0, "structure": 1.0, "history": 1.0,
            })],
        )
        assert out["comprehension"] == 1.20
        assert out["discovery"] == 0.90

    def test_sum_invariant_violated_rejected(self, monkeypatch):
        _enable(monkeypatch)
        # base sum = 5.0; new sum = 0.6 + 1.0×4 = 4.6 — REJECT
        out = compute_effective_category_weights(
            _BASE,
            adapted=[_entry({
                "comprehension": 0.60, "discovery": 1.0,
                "call_graph": 1.0, "structure": 1.0, "history": 1.0,
            })],
        )
        assert out == _BASE

    def test_per_category_floor_violated_rejected(self, monkeypatch):
        _enable(monkeypatch)
        # comprehension = 0.30 < 0.5 × 1.0 = 0.5 — REJECT (even
        # though sum would be 5.0 + (1.7 + ... extras) tightening)
        out = compute_effective_category_weights(
            _BASE,
            adapted=[_entry({
                "comprehension": 0.30, "discovery": 1.7,
                "call_graph": 1.0, "structure": 1.0, "history": 1.0,
            })],
        )
        assert out == _BASE

    def test_absolute_floor_violated_rejected(self, monkeypatch):
        _enable(monkeypatch)
        # base for cat="extra" (not in _BASE) — but absolute floor
        # only applies to base-known cats. Test with a base that
        # has a small value where 0.5×base < MIN_WEIGHT_VALUE.
        small_base = {"a": 0.015}  # 0.5×0.015 = 0.0075 < MIN=0.01
        # If candidate has a=0.005, then absolute floor 0.005 <
        # MIN=0.01 → REJECT (even though per-cat floor 0.0075
        # would also reject it).
        out = compute_effective_category_weights(
            small_base,
            adapted=[_entry({"a": 0.005})],
        )
        assert out == small_base

    def test_unknown_adapted_keys_dropped(self, monkeypatch):
        _enable(monkeypatch)
        # adapted has "newcat" not in base — should be dropped.
        out = compute_effective_category_weights(
            _BASE,
            adapted=[_entry({
                "comprehension": 1.20, "discovery": 0.90,
                "call_graph": 1.0, "structure": 1.0, "history": 1.0,
                "newcat": 99.0,  # unknown — dropped
            })],
        )
        assert "newcat" not in out
        assert set(out.keys()) == set(_BASE.keys())

    def test_partial_adapted_uses_base_for_missing(self, monkeypatch):
        _enable(monkeypatch)
        # adapted only specifies 2 of 5 cats; rest default to base.
        # comprehension=1.30, discovery=1.0(base), rest = base
        # sum = 1.30 + 1.0 + 1.0 + 1.0 + 1.0 = 5.30 ≥ 5.0 ✓
        out = compute_effective_category_weights(
            _BASE,
            adapted=[_entry({"comprehension": 1.30})],
        )
        assert out["comprehension"] == 1.30
        assert out["discovery"] == 1.0  # from base

    def test_latest_wins_only_last_entry_consulted(self, monkeypatch):
        _enable(monkeypatch)
        # Two entries: first valid + tightening; second invalid
        # (sum-violation). Should REJECT the latest → fall back to
        # base. Earlier valid entry is NOT consulted.
        out = compute_effective_category_weights(
            _BASE,
            adapted=[
                _entry({  # valid: sum=5.20
                    "comprehension": 1.20, "discovery": 1.0,
                    "call_graph": 1.0, "structure": 1.0,
                    "history": 1.0,
                }),
                _entry({  # invalid: sum=4.0
                    "comprehension": 0.6, "discovery": 0.6,
                    "call_graph": 0.6, "structure": 1.1,
                    "history": 1.1,
                }),
            ],
        )
        assert out == _BASE

    def test_schema_invariant_output_has_all_base_keys(self, monkeypatch):
        _enable(monkeypatch)
        # Even if adapted only mentions 1 cat, output must have
        # all base keys.
        out = compute_effective_category_weights(
            _BASE,
            adapted=[_entry({"comprehension": 1.20})],
        )
        assert set(out.keys()) == set(_BASE.keys())

    def test_loader_exception_falls_back(self, monkeypatch):
        _enable(monkeypatch)
        with mock.patch.object(
            loader, "load_adapted_rebalances",
            side_effect=RuntimeError("boom"),
        ):
            assert compute_effective_category_weights(_BASE) == _BASE

    def test_returns_dict_not_same_object(self, monkeypatch):
        _enable(monkeypatch)
        out = compute_effective_category_weights(_BASE, adapted=[])
        assert out is not _BASE
        # Modifying output should NOT affect base.
        out["comprehension"] = 99.0
        assert _BASE["comprehension"] == 1.0


# ---------------------------------------------------------------------------
# Section F — _net_tighten_check direct
# ---------------------------------------------------------------------------


class TestNetTightenCheck:
    def test_equal_sum_passes(self):
        passed, _ = loader._net_tighten_check(
            {"a": 1.0, "b": 1.0}, {"a": 1.5, "b": 0.5},
        )
        assert passed is True

    def test_lower_sum_rejected(self):
        passed, reason = loader._net_tighten_check(
            {"a": 1.0, "b": 1.0}, {"a": 0.6, "b": 0.6},
        )
        assert passed is False
        assert "sum_invariant" in reason

    def test_per_category_floor_rejected(self):
        passed, reason = loader._net_tighten_check(
            {"a": 1.0, "b": 1.0}, {"a": 0.4, "b": 1.7},
        )
        assert passed is False
        assert "per_category_floor" in reason

    def test_missing_adapted_key_uses_base(self):
        # Per the helper contract, missing keys default to base
        # at the call site. The check itself uses base when not
        # in candidate.
        passed, _ = loader._net_tighten_check(
            {"a": 1.0, "b": 1.0}, {"a": 1.5, "b": 1.0},
        )
        assert passed is True


# ---------------------------------------------------------------------------
# Section G — authority invariants
# ---------------------------------------------------------------------------


_LOADER_PATH = Path(loader.__file__)


class TestAuthorityInvariants:
    def test_no_banned_governance_imports(self):
        source = _LOADER_PATH.read_text()
        tree = ast.parse(source)
        banned_substrings = (
            "exploration_engine",
            "risk_tier_floor",
            "scoped_tool_backend",
            "general_driver",
            "semantic_guardian",
            "orchestrator",
            "tool_executor",
            "phase_runners",
            "gate_runner",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for banned in banned_substrings:
                    assert banned not in node.module, (
                        f"banned import: {node.module}"
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    for banned in banned_substrings:
                        assert banned not in alias.name, (
                            f"banned import: {alias.name}"
                        )

    def test_only_stdlib_and_adaptation(self):
        source = _LOADER_PATH.read_text()
        tree = ast.parse(source)
        stdlib_prefixes = (
            "__future__", "logging", "os", "dataclasses", "pathlib",
            "typing",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("backend."):
                    assert "adaptation" in node.module, (
                        f"non-adaptation backend import: {node.module}"
                    )
                else:
                    assert any(
                        node.module.startswith(p) for p in stdlib_prefixes
                    ) or node.module == "yaml", (
                        f"unexpected import: {node.module}"
                    )

    def test_no_subprocess_or_network_tokens(self):
        source = _LOADER_PATH.read_text()
        for token in (
            "subprocess", "requests", "urllib", "socket",
            "http.client", "asyncio.create_subprocess",
        ):
            assert token not in source, f"banned token: {token}"
