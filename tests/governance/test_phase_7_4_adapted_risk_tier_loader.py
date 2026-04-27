"""Phase 7.4 — adapted risk-tier ladder boot-time loader pins.

Mirrors 7.1 + 7.2 + 7.3 cage discipline:
  * fail-open paths (master flag off / YAML missing / parse error /
    oversize / non-mapping)
  * per-entry skip (missing tier_name / invalid charset / missing
    insert_after / oversize)
  * latest-occurrence-wins per tier_name
  * cap at MAX_ADAPTED_TIERS
  * compute_extended_ladder cage (load-bearing):
      - base_ladder elements ALWAYS present in same relative order
      - adapted tier_name colliding with base → SKIP
      - adapted insert_after not in base → SKIP
      - NEVER raises
  * authority invariants: stdlib + adaptation.ledger only; one-way
    dep (does NOT import risk_tier_floor.py or any orchestrator
    module)
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest import mock

import pytest

from backend.core.ouroboros.governance.adaptation import (
    adapted_risk_tier_loader as loader,
)
from backend.core.ouroboros.governance.adaptation.adapted_risk_tier_loader import (
    AdaptedTierEntry,
    MAX_ADAPTED_TIERS,
    MAX_TIER_NAME_CHARS,
    MAX_YAML_BYTES,
    adapted_risk_tiers_path,
    compute_extended_ladder,
    is_loader_enabled,
    load_adapted_tiers,
)


_BASE = ("SAFE_AUTO", "NOTIFY_APPLY", "APPROVAL_REQUIRED", "BLOCKED")


# ---------------------------------------------------------------------------
# Section A — module constants + master flag + dataclass
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_max_adapted_tiers_is_16(self):
        assert MAX_ADAPTED_TIERS == 16

    def test_max_yaml_bytes_is_4MiB(self):
        assert MAX_YAML_BYTES == 4 * 1024 * 1024

    def test_max_tier_name_chars_is_64(self):
        assert MAX_TIER_NAME_CHARS == 64

    def test_valid_tier_name_charset(self):
        # Slice 4b miner output: uppercase + digits + underscore
        for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_":
            assert c in loader._VALID_TIER_NAME_CHARS
        # Lowercase / dash / space are NOT valid
        for c in "abc -.":
            assert c not in loader._VALID_TIER_NAME_CHARS

    def test_truthy_constant_shape(self):
        assert loader._TRUTHY == ("1", "true", "yes", "on")


class TestMasterFlag:
    def test_default_false(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS", raising=False,
        )
        assert is_loader_enabled() is False

    def test_truthy_variants(self, monkeypatch):
        for v in ("1", "true", "TRUE", "Yes", "ON"):
            monkeypatch.setenv(
                "JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS", v,
            )
            assert is_loader_enabled() is True, v

    def test_falsy_variants(self, monkeypatch):
        for v in ("0", "false", "no", "off", "", " "):
            monkeypatch.setenv(
                "JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS", v,
            )
            assert is_loader_enabled() is False, v


class TestDataclass:
    def test_frozen(self):
        entry = AdaptedTierEntry(
            tier_name="X", insert_after="SAFE_AUTO",
            failure_class="fc", proposal_id="p", approved_at="t",
            approved_by="op",
        )
        with pytest.raises(Exception):  # FrozenInstanceError
            entry.tier_name = "Y"  # type: ignore[misc]

    def test_default_path(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_ADAPTED_RISK_TIERS_PATH", raising=False,
        )
        assert adapted_risk_tiers_path() == (
            Path(".jarvis") / "adapted_risk_tiers.yaml"
        )

    def test_path_env_override(self, monkeypatch, tmp_path):
        custom = tmp_path / "custom.yaml"
        monkeypatch.setenv(
            "JARVIS_ADAPTED_RISK_TIERS_PATH", str(custom),
        )
        assert adapted_risk_tiers_path() == custom


# ---------------------------------------------------------------------------
# Section B — master-flag short-circuit
# ---------------------------------------------------------------------------


class TestMasterFlagShortCircuit:
    def test_master_off_returns_empty_even_if_yaml_present(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.delenv(
            "JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS", raising=False,
        )
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text(
            "schema_version: 1\n"
            "tiers:\n"
            "  - tier_name: NEW_TIER\n"
            "    insert_after: SAFE_AUTO\n",
            encoding="utf-8",
        )
        assert load_adapted_tiers(yaml_path) == []


# ---------------------------------------------------------------------------
# Section C — YAML reader paths
# ---------------------------------------------------------------------------


def _enable(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS", "1",
    )


class TestYAMLReader:
    def test_missing_yaml_returns_empty(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        assert load_adapted_tiers(tmp_path / "missing.yaml") == []

    def test_oversize_refuses(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "big.yaml"
        yaml_path.write_text("x", encoding="utf-8")
        with mock.patch.object(
            Path, "stat",
            return_value=mock.Mock(st_size=MAX_YAML_BYTES + 1),
        ):
            assert load_adapted_tiers(yaml_path) == []

    def test_unreadable_returns_empty(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text("tiers: []\n", encoding="utf-8")
        with mock.patch.object(
            Path, "read_text", side_effect=OSError("permission denied"),
        ):
            assert load_adapted_tiers(yaml_path) == []

    def test_empty_file_returns_empty(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text("   \n  \n", encoding="utf-8")
        assert load_adapted_tiers(yaml_path) == []

    def test_no_pyyaml_returns_empty(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text("tiers: []\n", encoding="utf-8")
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
                assert load_adapted_tiers(yaml_path) == []
        finally:
            if original_yaml is not sentinel:
                sys.modules["yaml"] = original_yaml  # type: ignore[assignment]

    def test_parse_error_returns_empty(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "bad.yaml"
        yaml_path.write_text("tiers: [oh no\n  - missing close", encoding="utf-8")
        assert load_adapted_tiers(yaml_path) == []

    def test_non_mapping_doc_returns_empty(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text("- just\n- a\n- list\n", encoding="utf-8")
        assert load_adapted_tiers(yaml_path) == []

    def test_missing_tiers_key_returns_empty(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text(
            "schema_version: 1\nother: 7\n", encoding="utf-8",
        )
        assert load_adapted_tiers(yaml_path) == []

    def test_non_list_tiers_returns_empty(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text(
            "schema_version: 1\ntiers: a-string\n", encoding="utf-8",
        )
        assert load_adapted_tiers(yaml_path) == []

    def test_non_mapping_entry_skipped(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text(
            "schema_version: 1\n"
            "tiers:\n"
            "  - this-is-a-string\n"
            "  - tier_name: NEW_TIER\n"
            "    insert_after: SAFE_AUTO\n",
            encoding="utf-8",
        )
        out = load_adapted_tiers(yaml_path)
        assert len(out) == 1
        assert out[0].tier_name == "NEW_TIER"

    def test_max_adapted_tiers_truncate(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        # Build a YAML with > MAX_ADAPTED_TIERS distinct entries.
        lines = ["schema_version: 1", "tiers:"]
        for i in range(MAX_ADAPTED_TIERS + 5):
            lines.append(
                f"  - tier_name: NEW_TIER_{i:02d}\n"
                f"    insert_after: SAFE_AUTO"
            )
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        out = load_adapted_tiers(yaml_path)
        assert len(out) == MAX_ADAPTED_TIERS

    def test_latest_occurrence_per_tier_name_wins(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch)
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text(
            "schema_version: 1\n"
            "tiers:\n"
            "  - tier_name: NEW_TIER\n    insert_after: SAFE_AUTO\n"
            "  - tier_name: NEW_TIER\n    insert_after: BLOCKED\n",
            encoding="utf-8",
        )
        out = load_adapted_tiers(yaml_path)
        assert len(out) == 1
        # Latest wins: insert_after should be BLOCKED, not SAFE_AUTO.
        assert out[0].insert_after == "BLOCKED"

    def test_happy_path_two_entries(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        yaml_path = tmp_path / "a.yaml"
        yaml_path.write_text(
            "schema_version: 1\n"
            "tiers:\n"
            "  - tier_name: NOTIFY_APPLY_HARDENED_NETWORK\n"
            "    insert_after: NOTIFY_APPLY\n"
            "    failure_class: network_egress\n"
            "    proposal_id: p-1\n"
            "    approved_at: '2026-04-26T00:00:00Z'\n"
            "    approved_by: op\n"
            "  - tier_name: APPROVAL_REQUIRED_HARDENED_PERM\n"
            "    insert_after: APPROVAL_REQUIRED\n"
            "    failure_class: permission_loosen\n"
            "    proposal_id: p-2\n",
            encoding="utf-8",
        )
        out = load_adapted_tiers(yaml_path)
        assert len(out) == 2
        assert out[0].tier_name == "NOTIFY_APPLY_HARDENED_NETWORK"
        assert out[1].failure_class == "permission_loosen"


# ---------------------------------------------------------------------------
# Section D — _parse_entry direct
# ---------------------------------------------------------------------------


class TestParseEntry:
    def test_missing_tier_name_skip(self):
        assert loader._parse_entry({"insert_after": "SAFE_AUTO"}, 0) is None

    def test_blank_tier_name_skip(self):
        assert loader._parse_entry(
            {"tier_name": "  ", "insert_after": "SAFE_AUTO"}, 0,
        ) is None

    def test_lowercase_tier_name_skip(self):
        assert loader._parse_entry(
            {"tier_name": "new_tier", "insert_after": "SAFE_AUTO"}, 0,
        ) is None

    def test_tier_name_with_dash_skip(self):
        assert loader._parse_entry(
            {"tier_name": "NEW-TIER", "insert_after": "SAFE_AUTO"}, 0,
        ) is None

    def test_tier_name_with_path_traversal_skip(self):
        assert loader._parse_entry(
            {"tier_name": "../../etc/passwd",
             "insert_after": "SAFE_AUTO"}, 0,
        ) is None

    def test_tier_name_too_long_skip(self):
        long_name = "X" * (MAX_TIER_NAME_CHARS + 1)
        assert loader._parse_entry(
            {"tier_name": long_name, "insert_after": "SAFE_AUTO"}, 0,
        ) is None

    def test_tier_name_at_max_allowed(self):
        name = "X" * MAX_TIER_NAME_CHARS
        e = loader._parse_entry(
            {"tier_name": name, "insert_after": "SAFE_AUTO"}, 0,
        )
        assert e is not None
        assert e.tier_name == name

    def test_missing_insert_after_skip(self):
        assert loader._parse_entry({"tier_name": "NEW_TIER"}, 0) is None

    def test_invalid_insert_after_skip(self):
        assert loader._parse_entry(
            {"tier_name": "NEW_TIER", "insert_after": "lower"}, 0,
        ) is None

    def test_provenance_preserved(self):
        e = loader._parse_entry(
            {
                "tier_name": "NEW_TIER",
                "insert_after": "SAFE_AUTO",
                "failure_class": "fc",
                "proposal_id": "p",
                "approved_at": "t",
                "approved_by": "op",
            }, 0,
        )
        assert e == AdaptedTierEntry(
            tier_name="NEW_TIER", insert_after="SAFE_AUTO",
            failure_class="fc", proposal_id="p",
            approved_at="t", approved_by="op",
        )


# ---------------------------------------------------------------------------
# Section E — compute_extended_ladder cage
# ---------------------------------------------------------------------------


def _entry(name, after, **kwargs):
    return AdaptedTierEntry(
        tier_name=name, insert_after=after,
        failure_class=kwargs.get("failure_class", ""),
        proposal_id=kwargs.get("proposal_id", ""),
        approved_at=kwargs.get("approved_at", ""),
        approved_by=kwargs.get("approved_by", ""),
    )


class TestExtendedLadder:
    def test_loader_off_returns_base(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS", raising=False,
        )
        assert compute_extended_ladder(_BASE) == _BASE

    def test_loader_on_no_entries_returns_base(self, monkeypatch):
        _enable(monkeypatch)
        assert compute_extended_ladder(_BASE, adapted=[]) == _BASE

    def test_insertion_after_safe_auto(self, monkeypatch):
        _enable(monkeypatch)
        out = compute_extended_ladder(
            _BASE, adapted=[_entry("NEW_X", "SAFE_AUTO")],
        )
        assert out == (
            "SAFE_AUTO", "NEW_X", "NOTIFY_APPLY",
            "APPROVAL_REQUIRED", "BLOCKED",
        )

    def test_insertion_after_blocked(self, monkeypatch):
        _enable(monkeypatch)
        out = compute_extended_ladder(
            _BASE, adapted=[_entry("CRITICAL", "BLOCKED")],
        )
        assert out == (
            "SAFE_AUTO", "NOTIFY_APPLY",
            "APPROVAL_REQUIRED", "BLOCKED", "CRITICAL",
        )

    def test_multiple_insertions_after_same_slot_ordered(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        out = compute_extended_ladder(
            _BASE,
            adapted=[
                _entry("ALPHA", "SAFE_AUTO"),
                _entry("BETA", "SAFE_AUTO"),
            ],
        )
        # Both inserted after SAFE_AUTO, in YAML order.
        assert out == (
            "SAFE_AUTO", "ALPHA", "BETA", "NOTIFY_APPLY",
            "APPROVAL_REQUIRED", "BLOCKED",
        )

    def test_collision_with_base_skipped(self, monkeypatch):
        # Defense-in-depth: even if YAML somehow has tier_name
        # matching a base ladder entry, it must NOT override.
        _enable(monkeypatch)
        out = compute_extended_ladder(
            _BASE,
            adapted=[_entry("BLOCKED", "SAFE_AUTO")],
        )
        assert out == _BASE

    def test_insert_after_unknown_skipped(self, monkeypatch):
        _enable(monkeypatch)
        out = compute_extended_ladder(
            _BASE,
            adapted=[_entry("NEW_X", "DOES_NOT_EXIST")],
        )
        assert out == _BASE

    def test_base_ladder_relative_order_preserved(self, monkeypatch):
        # Defense-in-depth: insertions anywhere must NEVER reorder
        # the base ladder. Iterate over output, extract base elements
        # in order, assert they match base_ladder.
        _enable(monkeypatch)
        out = compute_extended_ladder(
            _BASE,
            adapted=[
                _entry("X1", "SAFE_AUTO"),
                _entry("X2", "BLOCKED"),
                _entry("X3", "NOTIFY_APPLY"),
            ],
        )
        base_extracted = tuple(t for t in out if t in _BASE)
        assert base_extracted == _BASE

    def test_loader_exception_falls_back(self, monkeypatch):
        _enable(monkeypatch)
        with mock.patch.object(
            loader, "load_adapted_tiers",
            side_effect=RuntimeError("boom"),
        ):
            assert compute_extended_ladder(_BASE) == _BASE

    def test_returns_tuple_not_list(self, monkeypatch):
        _enable(monkeypatch)
        out = compute_extended_ladder(_BASE, adapted=[])
        assert isinstance(out, tuple)
        out2 = compute_extended_ladder(
            _BASE, adapted=[_entry("NEW", "SAFE_AUTO")],
        )
        assert isinstance(out2, tuple)

    def test_empty_base_ladder_handled(self, monkeypatch):
        _enable(monkeypatch)
        # No base ladder → all adapted entries skip (insert_after
        # invalid) → returns empty tuple.
        assert compute_extended_ladder(
            (), adapted=[_entry("NEW", "SAFE_AUTO")],
        ) == ()


# ---------------------------------------------------------------------------
# Section F — authority invariants
# ---------------------------------------------------------------------------


_LOADER_PATH = Path(loader.__file__)


class TestAuthorityInvariants:
    def test_no_banned_governance_imports(self):
        """Loader must NOT import risk_tier_floor.py or any other
        canonical-ladder-mutating module — one-way dependency rule
        (callers import this; never reverse).
        """
        source = _LOADER_PATH.read_text()
        tree = ast.parse(source)
        banned_substrings = (
            "risk_tier_floor",
            "scoped_tool_backend",
            "general_driver",
            "exploration_engine",
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
        source = _LOADER_PATH.read_text()
        for token in (
            "subprocess", "requests", "urllib", "socket",
            "http.client", "asyncio.create_subprocess",
        ):
            assert token not in source, f"banned token: {token}"
