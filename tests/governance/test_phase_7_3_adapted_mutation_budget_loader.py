"""Phase 7.3 — adapted per-Order mutation budget boot-time loader pins.

Mirrors 7.1 + 7.2 cage discipline:
  * fail-open paths (master flag off / YAML missing / parse error /
    oversize / non-mapping)
  * per-entry skip (unknown order / non-integer budget / negative)
  * latest-occurrence-wins per order
  * Order-2 floor MIN_ORDER2_BUDGET enforced
  * cap at MAX_ADAPTED_BUDGETS
  * clamp at MAX_BUDGET_VALUE
  * compute_effective_max_mutations cage: NEVER raises budget;
    always returns min(env_default, adapted)
  * authority invariants: stdlib + adaptation.ledger only; one-way
    dep (does NOT import scoped_tool_backend.py)
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest import mock

import pytest

from backend.core.ouroboros.governance.adaptation import (
    adapted_mutation_budget_loader as loader,
)
from backend.core.ouroboros.governance.adaptation.adapted_mutation_budget_loader import (
    AdaptedBudgetEntry,
    MAX_ADAPTED_BUDGETS,
    MAX_BUDGET_VALUE,
    MAX_YAML_BYTES,
    MIN_ORDER2_BUDGET,
    adapted_budgets_path,
    compute_effective_max_mutations,
    is_loader_enabled,
    load_adapted_budgets,
)


# ---------------------------------------------------------------------------
# Section A — module constants + master flag + dataclass
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_max_adapted_budgets_is_8(self):
        assert MAX_ADAPTED_BUDGETS == 8

    def test_max_yaml_bytes_is_4MiB(self):
        assert MAX_YAML_BYTES == 4 * 1024 * 1024

    def test_max_budget_value_is_64(self):
        assert MAX_BUDGET_VALUE == 64

    def test_min_order2_budget_is_1(self):
        assert MIN_ORDER2_BUDGET == 1

    def test_known_orders_frozenset_1_and_2(self):
        # Internal allowlist; pin shape so future Order-3 addition is
        # an intentional opt-in change.
        assert loader._KNOWN_ORDERS == frozenset({1, 2})

    def test_truthy_constant_shape(self):
        assert loader._TRUTHY == ("1", "true", "yes", "on")


class TestMasterFlag:
    def test_default_false(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS",
            raising=False,
        )
        assert is_loader_enabled() is False

    def test_truthy_variants(self, monkeypatch):
        for v in ("1", "true", "TRUE", "Yes", "ON"):
            monkeypatch.setenv(
                "JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS", v,
            )
            assert is_loader_enabled() is True, v

    def test_falsy_variants(self, monkeypatch):
        for v in ("0", "false", "no", "off", "", " "):
            monkeypatch.setenv(
                "JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS", v,
            )
            assert is_loader_enabled() is False, v


class TestDataclass:
    def test_frozen(self):
        entry = AdaptedBudgetEntry(
            order=2, budget=1,
            proposal_id="p1", approved_at="t1", approved_by="op",
        )
        with pytest.raises(Exception):  # FrozenInstanceError
            entry.budget = 5  # type: ignore[misc]

    def test_default_path(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_ADAPTED_MUTATION_BUDGETS_PATH", raising=False,
        )
        p = adapted_budgets_path()
        assert p == Path(".jarvis") / "adapted_mutation_budgets.yaml"

    def test_path_env_override(self, monkeypatch, tmp_path):
        custom = tmp_path / "custom.yaml"
        monkeypatch.setenv(
            "JARVIS_ADAPTED_MUTATION_BUDGETS_PATH", str(custom),
        )
        assert adapted_budgets_path() == custom


# ---------------------------------------------------------------------------
# Section B — master-flag short-circuit
# ---------------------------------------------------------------------------


class TestMasterFlagShortCircuit:
    def test_master_off_returns_empty_even_if_yaml_present(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.delenv(
            "JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS",
            raising=False,
        )
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text(
            "schema_version: 1\nbudgets:\n  - order: 2\n    budget: 1\n",
            encoding="utf-8",
        )
        # Should not even read the file when master off.
        assert load_adapted_budgets(yaml_path) == {}


# ---------------------------------------------------------------------------
# Section C — YAML reader paths
# ---------------------------------------------------------------------------


def _enable(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS", "1",
    )


class TestYAMLReader:
    def test_missing_yaml_returns_empty(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        assert load_adapted_budgets(tmp_path / "missing.yaml") == {}

    def test_oversize_refuses(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "big.yaml"
        yaml_path.write_text("x", encoding="utf-8")
        with mock.patch.object(
            Path, "stat",
            return_value=mock.Mock(st_size=MAX_YAML_BYTES + 1),
        ):
            assert load_adapted_budgets(yaml_path) == {}

    def test_unreadable_returns_empty(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text("budgets: []\n", encoding="utf-8")
        with mock.patch.object(
            Path, "read_text", side_effect=OSError("permission denied"),
        ):
            assert load_adapted_budgets(yaml_path) == {}

    def test_empty_file_returns_empty(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text("   \n  \n", encoding="utf-8")
        assert load_adapted_budgets(yaml_path) == {}

    def test_no_pyyaml_returns_empty(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text("budgets: []\n", encoding="utf-8")
        # Force ImportError on `import yaml` inside loader.
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
                assert load_adapted_budgets(yaml_path) == {}
        finally:
            if original_yaml is not sentinel:
                sys.modules["yaml"] = original_yaml  # type: ignore[assignment]

    def test_parse_error_returns_empty(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "bad.yaml"
        yaml_path.write_text("budgets: [oh no\n  - missing close", encoding="utf-8")
        assert load_adapted_budgets(yaml_path) == {}

    def test_non_mapping_doc_returns_empty(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text("- just\n- a\n- list\n", encoding="utf-8")
        assert load_adapted_budgets(yaml_path) == {}

    def test_missing_budgets_key_returns_empty(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text("schema_version: 1\nother: 7\n", encoding="utf-8")
        assert load_adapted_budgets(yaml_path) == {}

    def test_non_list_budgets_returns_empty(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text(
            "schema_version: 1\nbudgets: a-string\n", encoding="utf-8",
        )
        assert load_adapted_budgets(yaml_path) == {}

    def test_non_mapping_entry_skipped(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text(
            "schema_version: 1\n"
            "budgets:\n"
            "  - this-is-a-string\n"
            "  - order: 2\n"
            "    budget: 1\n",
            encoding="utf-8",
        )
        assert load_adapted_budgets(yaml_path) == {2: 1}

    def test_max_adapted_budgets_truncate(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        # Build a YAML with > MAX_ADAPTED_BUDGETS entries (only Orders
        # 1+2 are valid; the rest are unknown and skipped, but cap
        # check fires regardless).
        # Use unknown orders so no over-write happens; use Order=1
        # repeatedly to also prove latest-wins.
        lines = ["schema_version: 1", "budgets:"]
        for i in range(MAX_ADAPTED_BUDGETS + 5):
            lines.append(f"  - order: 1\n    budget: {i + 1}")
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        out = load_adapted_budgets(yaml_path)
        # Cap on number processed; latest-wins on Order=1.
        assert out[1] == MAX_ADAPTED_BUDGETS  # 8th entry's budget

    def test_latest_occurrence_per_order_wins(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text(
            "schema_version: 1\n"
            "budgets:\n"
            "  - order: 2\n    budget: 3\n"
            "  - order: 2\n    budget: 1\n",
            encoding="utf-8",
        )
        assert load_adapted_budgets(yaml_path) == {2: 1}

    def test_clamp_too_large_budget(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text(
            "schema_version: 1\n"
            "budgets:\n"
            f"  - order: 1\n    budget: {MAX_BUDGET_VALUE + 100}\n",
            encoding="utf-8",
        )
        assert load_adapted_budgets(yaml_path) == {1: MAX_BUDGET_VALUE}

    def test_happy_path_both_orders(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text(
            "schema_version: 1\n"
            "budgets:\n"
            "  - order: 1\n    budget: 5\n    proposal_id: p-1\n"
            "    approved_at: '2026-04-26T00:00:00Z'\n"
            "    approved_by: op\n"
            "  - order: 2\n    budget: 1\n    proposal_id: p-2\n",
            encoding="utf-8",
        )
        assert load_adapted_budgets(yaml_path) == {1: 5, 2: 1}


# ---------------------------------------------------------------------------
# Section D — _parse_entry direct
# ---------------------------------------------------------------------------


class TestParseEntry:
    def test_missing_order_skip(self):
        assert loader._parse_entry({"budget": 1}, 0) is None

    def test_non_integer_order_skip(self):
        assert loader._parse_entry({"order": "two", "budget": 1}, 0) is None

    def test_unknown_order_skip(self):
        assert loader._parse_entry({"order": 3, "budget": 1}, 0) is None

    def test_non_integer_budget_skip(self):
        assert loader._parse_entry(
            {"order": 1, "budget": "five"}, 0,
        ) is None

    def test_negative_budget_skip(self):
        assert loader._parse_entry({"order": 1, "budget": -1}, 0) is None

    def test_clamp_too_large(self):
        e = loader._parse_entry(
            {"order": 1, "budget": MAX_BUDGET_VALUE + 50}, 0,
        )
        assert e is not None
        assert e.budget == MAX_BUDGET_VALUE

    def test_order2_below_floor_raised(self):
        # Order-2 budget=0 should be raised to MIN_ORDER2_BUDGET=1.
        e = loader._parse_entry({"order": 2, "budget": 0}, 0)
        assert e is not None
        assert e.budget == MIN_ORDER2_BUDGET

    def test_order1_zero_budget_allowed(self):
        # Order-1 has no minimum floor — read-only is a valid Order-1
        # configuration.
        e = loader._parse_entry({"order": 1, "budget": 0}, 0)
        assert e is not None
        assert e.budget == 0

    def test_float_budget_truncated_to_int(self):
        e = loader._parse_entry({"order": 1, "budget": 3.9}, 0)
        assert e is not None
        assert e.budget == 3  # int() truncates

    def test_provenance_preserved(self):
        e = loader._parse_entry(
            {
                "order": 2, "budget": 1,
                "proposal_id": "x", "approved_at": "t",
                "approved_by": "op",
            }, 0,
        )
        assert e == AdaptedBudgetEntry(
            order=2, budget=1,
            proposal_id="x", approved_at="t", approved_by="op",
        )


# ---------------------------------------------------------------------------
# Section E — compute_effective_max_mutations cage
# ---------------------------------------------------------------------------


class TestEffectiveBudget:
    def test_loader_off_returns_env_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS",
            raising=False,
        )
        assert compute_effective_max_mutations(2, 5) == 5

    def test_loader_on_no_entry_returns_env_default(self, monkeypatch):
        _enable(monkeypatch)
        # Pre-load empty mapping so we don't depend on cwd YAML.
        assert compute_effective_max_mutations(
            2, 5, adapted={},
        ) == 5

    def test_loader_on_entry_lower_returns_lower(self, monkeypatch):
        _enable(monkeypatch)
        assert compute_effective_max_mutations(
            2, 5, adapted={2: 1},
        ) == 1

    def test_loader_on_entry_higher_clamped_to_env(self, monkeypatch):
        # Defense-in-depth: even if YAML somehow has budget > env,
        # min() ensures we never raise.
        _enable(monkeypatch)
        assert compute_effective_max_mutations(
            2, 3, adapted={2: 99},
        ) == 3

    def test_negative_env_default_normalized_to_zero(self, monkeypatch):
        _enable(monkeypatch)
        assert compute_effective_max_mutations(
            1, -5, adapted={1: 0},
        ) == 0

    def test_other_order_unaffected(self, monkeypatch):
        # Adapted entry for order=2 must not affect order=1 caller.
        _enable(monkeypatch)
        assert compute_effective_max_mutations(
            1, 10, adapted={2: 1},
        ) == 10

    def test_adapted_loader_exception_falls_back(self, monkeypatch):
        _enable(monkeypatch)
        with mock.patch.object(
            loader, "load_adapted_budgets",
            side_effect=RuntimeError("boom"),
        ):
            # adapted=None → loader call raises → fallback to env_default
            assert compute_effective_max_mutations(2, 5) == 5

    def test_string_order_coerced(self, monkeypatch):
        # Defensive: int() on caller-supplied order.
        _enable(monkeypatch)
        assert compute_effective_max_mutations(
            "2", 5, adapted={2: 1},  # type: ignore[arg-type]
        ) == 1


# ---------------------------------------------------------------------------
# Section F — authority invariants
# ---------------------------------------------------------------------------


_LOADER_PATH = Path(loader.__file__)


class TestAuthorityInvariants:
    def test_no_banned_governance_imports(self):
        """Loader must NOT import scoped_tool_backend.py or any other
        backend-mutating module — one-way dependency rule (callers
        import this; never reverse).
        """
        source = _LOADER_PATH.read_text()
        tree = ast.parse(source)
        banned_substrings = (
            "scoped_tool_backend",
            "general_driver",
            "subagent_contracts",
            "agentic_general_subagent",
            "exploration_engine",
            "semantic_guardian",
            "orchestrator",
            "tool_executor",
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

    def test_only_stdlib_and_adaptation_ledger(self):
        """Top-level imports must be stdlib or adaptation.* only."""
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
        """No subprocess, requests, urllib spawning, or network I/O."""
        source = _LOADER_PATH.read_text()
        for token in (
            "subprocess", "requests", "urllib", "socket",
            "http.client", "asyncio.create_subprocess",
        ):
            assert token not in source, f"banned token: {token}"
