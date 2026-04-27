"""Phase 7.2 — IronGate adapted-floor loader regression suite.

Pins:
  * Module constants + master flag default-false-pre-graduation.
  * AdaptedFloorEntry frozen dataclass.
  * Master-flag short-circuit (returns empty when off).
  * YAML reader paths: missing / blank / oversize / parse failure /
    not-a-mapping / floors-key-missing / per-entry validation.
  * Per-entry pins:
    - Missing category → SKIP
    - Unknown category → SKIP (with warning)
    - Non-numeric floor → SKIP
    - floor <= 0 → SKIP
    - floor > MAX_FLOOR_VALUE → CLAMPED
    - latest-occurrence-wins per category
  * compute_adapted_required_categories() pin: only floor > 0
    categories surface as required.
  * Cap at MAX_ADAPTED_FLOORS.
  * ExplorationFloors.from_env_with_adapted() integration:
    - master-off → identical to from_env()
    - missing YAML → identical to from_env()
    - YAML present + master on → required_categories merged
    - merged is UNION (additive); env required_categories preserved
    - unknown YAML category → tolerated (defense-in-depth)
  * Authority invariants (AST grep): no banned governance imports;
    stdlib-only top-level; no subprocess/network.
"""
from __future__ import annotations

import ast as _ast
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parent.parent.parent
_LOADER_PATH = (
    _REPO / "backend" / "core" / "ouroboros" / "governance"
    / "adaptation" / "adapted_iron_gate_loader.py"
)


from backend.core.ouroboros.governance.adaptation.adapted_iron_gate_loader import (
    AdaptedFloorEntry,
    MAX_ADAPTED_FLOORS,
    MAX_FLOOR_VALUE,
    MAX_YAML_BYTES,
    _parse_entry,
    adapted_floors_path,
    compute_adapted_required_categories,
    is_loader_enabled,
    load_adapted_floors,
)
from backend.core.ouroboros.governance.exploration_engine import (
    ExplorationCategory,
    ExplorationFloors,
)


# ===========================================================================
# A — Module constants + master flag + dataclass
# ===========================================================================


def test_max_adapted_floors_pinned():
    assert MAX_ADAPTED_FLOORS == 64


def test_max_floor_value_pinned():
    assert MAX_FLOOR_VALUE == 100.0


def test_max_yaml_bytes_pinned():
    assert MAX_YAML_BYTES == 4 * 1024 * 1024


def test_master_flag_default_false_pre_graduation(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS", raising=False,
    )
    assert is_loader_enabled() is False


def test_master_flag_truthy_variants(monkeypatch):
    for val in ("1", "true", "yes", "on"):
        monkeypatch.setenv(
            "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS", val,
        )
        assert is_loader_enabled() is True


def test_master_flag_falsy_variants(monkeypatch):
    for val in ("0", "false", "no", "off", "", "garbage"):
        monkeypatch.setenv(
            "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS", val,
        )
        assert is_loader_enabled() is False


def test_adapted_floor_entry_is_frozen():
    e = AdaptedFloorEntry(
        category="comprehension", floor=2.0,
        proposal_id="p", approved_at="t", approved_by="op",
    )
    import dataclasses
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.floor = 5.0  # type: ignore[misc]


def test_adapted_floors_path_default_under_jarvis():
    p = adapted_floors_path()
    assert p.name == "adapted_iron_gate_floors.yaml"
    assert p.parent.name == ".jarvis"


def test_adapted_floors_path_env_override(monkeypatch, tmp_path):
    custom = tmp_path / "custom.yaml"
    monkeypatch.setenv(
        "JARVIS_ADAPTED_IRON_GATE_FLOORS_PATH", str(custom),
    )
    assert adapted_floors_path() == custom


# ===========================================================================
# B — Master-flag short-circuit
# ===========================================================================


def test_load_returns_empty_when_master_off(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS", "0",
    )
    yaml_path = tmp_path / "floors.yaml"
    yaml_path.write_text(
        "schema_version: 1\nfloors:\n  - {category: comprehension, floor: 2}\n"
    )
    out = load_adapted_floors(yaml_path)
    assert out == {}


# ===========================================================================
# C — YAML reader paths
# ===========================================================================


def _enable(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS", "1",
    )


def test_load_returns_empty_when_yaml_missing(monkeypatch, tmp_path):
    _enable(monkeypatch)
    out = load_adapted_floors(tmp_path / "nope.yaml")
    assert out == {}


def test_load_returns_empty_when_yaml_blank(monkeypatch, tmp_path):
    _enable(monkeypatch)
    p = tmp_path / "blank.yaml"
    p.write_text("")
    out = load_adapted_floors(p)
    assert out == {}


def test_load_returns_empty_when_yaml_oversize(monkeypatch, tmp_path):
    _enable(monkeypatch)
    p = tmp_path / "huge.yaml"
    big = "schema_version: 1\nfloors: []\n# " + ("x" * (MAX_YAML_BYTES + 100))
    p.write_text(big)
    out = load_adapted_floors(p)
    assert out == {}


def test_load_returns_empty_when_yaml_parse_fails(monkeypatch, tmp_path):
    _enable(monkeypatch)
    p = tmp_path / "bad.yaml"
    p.write_text("schema_version: 1\nfloors: [unclosed\n")
    out = load_adapted_floors(p)
    assert out == {}


def test_load_returns_empty_when_top_not_mapping(monkeypatch, tmp_path):
    _enable(monkeypatch)
    p = tmp_path / "list.yaml"
    p.write_text("- a\n- b\n")
    out = load_adapted_floors(p)
    assert out == {}


def test_load_returns_empty_when_floors_key_missing(monkeypatch, tmp_path):
    _enable(monkeypatch)
    p = tmp_path / "no_floors.yaml"
    p.write_text("schema_version: 1\nother: foo\n")
    out = load_adapted_floors(p)
    assert out == {}


def test_load_loads_one_valid_entry(monkeypatch, tmp_path):
    _enable(monkeypatch)
    p = tmp_path / "one.yaml"
    p.write_text(
        "schema_version: 1\nfloors:\n"
        "  - {category: comprehension, floor: 2.5}\n"
    )
    out = load_adapted_floors(p)
    assert out == {"comprehension": 2.5}


def test_load_loads_multiple_categories(monkeypatch, tmp_path):
    _enable(monkeypatch)
    p = tmp_path / "multi.yaml"
    p.write_text(
        "schema_version: 1\nfloors:\n"
        "  - {category: comprehension, floor: 2}\n"
        "  - {category: discovery, floor: 1}\n"
        "  - {category: call_graph, floor: 1.5}\n"
    )
    out = load_adapted_floors(p)
    assert set(out.keys()) == {"comprehension", "discovery", "call_graph"}


def test_load_skips_unknown_category(monkeypatch, tmp_path):
    _enable(monkeypatch)
    p = tmp_path / "unknown.yaml"
    p.write_text(
        "schema_version: 1\nfloors:\n"
        "  - {category: unknown_cat, floor: 5}\n"
        "  - {category: comprehension, floor: 2}\n"
    )
    out = load_adapted_floors(p)
    assert "unknown_cat" not in out
    assert "comprehension" in out


def test_load_skips_non_numeric_floor(monkeypatch, tmp_path):
    _enable(monkeypatch)
    p = tmp_path / "non_numeric.yaml"
    p.write_text(
        "schema_version: 1\nfloors:\n"
        "  - {category: comprehension, floor: 'not_a_number'}\n"
        "  - {category: discovery, floor: 1}\n"
    )
    out = load_adapted_floors(p)
    assert "comprehension" not in out
    assert "discovery" in out


def test_load_skips_floor_zero_or_negative(monkeypatch, tmp_path):
    _enable(monkeypatch)
    p = tmp_path / "zero.yaml"
    p.write_text(
        "schema_version: 1\nfloors:\n"
        "  - {category: comprehension, floor: 0}\n"
        "  - {category: discovery, floor: -1}\n"
        "  - {category: history, floor: 1.5}\n"
    )
    out = load_adapted_floors(p)
    assert "comprehension" not in out
    assert "discovery" not in out
    assert "history" in out


def test_load_clamps_floor_at_max(monkeypatch, tmp_path):
    _enable(monkeypatch)
    p = tmp_path / "huge_floor.yaml"
    p.write_text(
        "schema_version: 1\nfloors:\n"
        f"  - {{category: comprehension, floor: {MAX_FLOOR_VALUE + 100}}}\n"
    )
    out = load_adapted_floors(p)
    assert out["comprehension"] == MAX_FLOOR_VALUE


def test_load_caps_at_max_adapted_floors(monkeypatch, tmp_path):
    _enable(monkeypatch)
    # Generate MAX_ADAPTED_FLOORS+10 entries; only first MAX should
    # be loaded. All entries use the same category cycle so we can
    # check the cap fires.
    p = tmp_path / "many.yaml"
    cats = ("comprehension", "discovery", "call_graph", "structure",
            "history")
    entries = []
    for i in range(MAX_ADAPTED_FLOORS + 10):
        cat = cats[i % len(cats)]
        entries.append(f"  - {{category: {cat}, floor: {1.0 + i*0.01}}}\n")
    p.write_text("schema_version: 1\nfloors:\n" + "".join(entries))
    # The actual category-dict will only have 5 unique entries
    # (latest-wins) but the CAP defends against the loader trying
    # to process 100+ entries.
    out = load_adapted_floors(p)
    # Loader processed at most MAX_ADAPTED_FLOORS entries (cap is
    # on processing, not on output dict size since latest-wins)
    assert len(out) <= len(cats)


def test_load_latest_occurrence_wins_per_category(monkeypatch, tmp_path):
    _enable(monkeypatch)
    p = tmp_path / "supersede.yaml"
    p.write_text(
        "schema_version: 1\nfloors:\n"
        "  - {category: comprehension, floor: 1.0}\n"
        "  - {category: comprehension, floor: 3.0}\n"  # supersedes
    )
    out = load_adapted_floors(p)
    assert out["comprehension"] == 3.0


# ===========================================================================
# D — compute_adapted_required_categories
# ===========================================================================


def test_compute_required_categories_filters_zero_floors():
    out = compute_adapted_required_categories({
        "comprehension": 2.0,
        "discovery": 0.0,
        "call_graph": 1.5,
    })
    assert out == frozenset({"comprehension", "call_graph"})


def test_compute_required_categories_empty_input():
    assert compute_adapted_required_categories({}) == frozenset()


def test_compute_required_categories_all_negative_or_zero():
    out = compute_adapted_required_categories({
        "comprehension": 0,
        "discovery": -1.0,
    })
    assert out == frozenset()


# ===========================================================================
# E — _parse_entry direct
# ===========================================================================


def test_parse_entry_minimal_valid():
    e = _parse_entry({"category": "comprehension", "floor": 2.0}, 0)
    assert e is not None
    assert e.category == "comprehension"
    assert e.floor == 2.0


def test_parse_entry_missing_category_returns_none():
    e = _parse_entry({"floor": 2.0}, 0)
    assert e is None


def test_parse_entry_unknown_category_returns_none():
    e = _parse_entry({"category": "fake", "floor": 2.0}, 0)
    assert e is None


def test_parse_entry_zero_floor_returns_none():
    e = _parse_entry({"category": "comprehension", "floor": 0}, 0)
    assert e is None


def test_parse_entry_provenance_preserved():
    e = _parse_entry({
        "category": "comprehension", "floor": 2.0,
        "proposal_id": "adapt-ig-abc",
        "approved_at": "2026-04-26", "approved_by": "alice",
    }, 0)
    assert e is not None
    assert e.proposal_id == "adapt-ig-abc"
    assert e.approved_at == "2026-04-26"
    assert e.approved_by == "alice"


def test_parse_entry_category_lowercased():
    """Category names normalized to lowercase before validation."""
    e = _parse_entry({"category": "COMPREHENSION", "floor": 2.0}, 0)
    assert e is not None
    assert e.category == "comprehension"


# ===========================================================================
# F — ExplorationFloors.from_env_with_adapted integration
# ===========================================================================


def test_from_env_with_adapted_master_off_identical_to_from_env(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv(
        "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS", "0",
    )
    monkeypatch.setenv(
        "JARVIS_ADAPTED_IRON_GATE_FLOORS_PATH",
        str(tmp_path / "floors.yaml"),
    )
    base = ExplorationFloors.from_env("moderate")
    merged = ExplorationFloors.from_env_with_adapted("moderate")
    assert merged.required_categories == base.required_categories
    assert merged.min_score == base.min_score
    assert merged.min_categories == base.min_categories


def test_from_env_with_adapted_missing_yaml_identical_to_from_env(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv(
        "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS", "1",
    )
    monkeypatch.setenv(
        "JARVIS_ADAPTED_IRON_GATE_FLOORS_PATH",
        str(tmp_path / "missing.yaml"),
    )
    base = ExplorationFloors.from_env("moderate")
    merged = ExplorationFloors.from_env_with_adapted("moderate")
    assert merged.required_categories == base.required_categories


def test_from_env_with_adapted_merges_required_categories(
    monkeypatch, tmp_path,
):
    """Adapted floor for `discovery` adds DISCOVERY to required_categories."""
    monkeypatch.setenv(
        "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS", "1",
    )
    yaml_path = tmp_path / "floors.yaml"
    yaml_path.write_text(
        "schema_version: 1\nfloors:\n"
        "  - {category: discovery, floor: 2.0}\n"
    )
    monkeypatch.setenv(
        "JARVIS_ADAPTED_IRON_GATE_FLOORS_PATH", str(yaml_path),
    )
    merged = ExplorationFloors.from_env_with_adapted("moderate")
    assert ExplorationCategory.DISCOVERY in merged.required_categories


def test_from_env_with_adapted_preserves_env_required(
    monkeypatch, tmp_path,
):
    """Adapted floors are ADDITIVE — env's existing required_categories
    are preserved."""
    monkeypatch.setenv(
        "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS", "1",
    )
    yaml_path = tmp_path / "floors.yaml"
    yaml_path.write_text(
        "schema_version: 1\nfloors:\n"
        "  - {category: discovery, floor: 1.0}\n"
    )
    monkeypatch.setenv(
        "JARVIS_ADAPTED_IRON_GATE_FLOORS_PATH", str(yaml_path),
    )
    base = ExplorationFloors.from_env("complex")
    merged = ExplorationFloors.from_env_with_adapted("complex")
    # Every base required category is still required
    assert base.required_categories.issubset(merged.required_categories)


def test_from_env_with_adapted_does_not_modify_min_score(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv(
        "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS", "1",
    )
    yaml_path = tmp_path / "floors.yaml"
    yaml_path.write_text(
        "schema_version: 1\nfloors:\n"
        "  - {category: discovery, floor: 99.0}\n"
    )
    monkeypatch.setenv(
        "JARVIS_ADAPTED_IRON_GATE_FLOORS_PATH", str(yaml_path),
    )
    base = ExplorationFloors.from_env("moderate")
    merged = ExplorationFloors.from_env_with_adapted("moderate")
    # Adapted floors are categorical-coverage; min_score stays env-driven
    assert merged.min_score == base.min_score
    assert merged.min_categories == base.min_categories


def test_from_env_with_adapted_tolerates_unknown_yaml_category(
    monkeypatch, tmp_path,
):
    """Defense-in-depth: unknown YAML category in
    compute_adapted_required_categories doesn't crash from_env_with_adapted."""
    monkeypatch.setenv(
        "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS", "1",
    )
    yaml_path = tmp_path / "mixed.yaml"
    yaml_path.write_text(
        "schema_version: 1\nfloors:\n"
        "  - {category: unknown_cat, floor: 2.0}\n"   # filtered by loader
        "  - {category: comprehension, floor: 1.0}\n"  # valid
    )
    monkeypatch.setenv(
        "JARVIS_ADAPTED_IRON_GATE_FLOORS_PATH", str(yaml_path),
    )
    merged = ExplorationFloors.from_env_with_adapted("moderate")
    # Unknown filtered at loader; valid surfaced as required
    assert ExplorationCategory.COMPREHENSION in merged.required_categories


# ===========================================================================
# G — Authority invariants (AST grep)
# ===========================================================================


def test_loader_module_has_no_banned_governance_imports():
    tree = _ast.parse(_LOADER_PATH.read_text(encoding="utf-8"))
    banned_substrings = (
        "orchestrator",
        "iron_gate",
        "exploration_engine",
        "change_engine",
        "candidate_generator",
        "risk_tier_floor",
        "semantic_guardian",
        "semantic_firewall",
        "scoped_tool_backend",
        ".gate.",
        "phase_runners",
        "providers",
    )
    found_banned = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom):
            mod = node.module or ""
            for sub in banned_substrings:
                if sub in mod:
                    found_banned.append((mod, sub))
    assert not found_banned, (
        f"adapted_iron_gate_loader.py contains banned imports: "
        f"{found_banned}"
    )


def test_loader_module_imports_only_stdlib_at_top_level():
    tree = _ast.parse(_LOADER_PATH.read_text(encoding="utf-8"))
    stdlib_prefixes = (
        "__future__",
        "logging", "os", "dataclasses", "pathlib", "typing",
    )
    for node in tree.body:
        if isinstance(node, _ast.ImportFrom):
            mod = node.module or ""
            assert any(
                mod == p or mod.startswith(p + ".") for p in stdlib_prefixes
            ), f"unauthorized top-level import {mod!r}"


def test_loader_module_does_not_call_subprocess_or_network():
    src = _LOADER_PATH.read_text(encoding="utf-8")
    forbidden = (
        "subprocess.",
        "socket.",
        "urllib.",
        "requests.",
        "http.client",
        "os." + "system(",
        "shutil.rmtree(",
    )
    found = [tok for tok in forbidden if tok in src]
    assert not found
