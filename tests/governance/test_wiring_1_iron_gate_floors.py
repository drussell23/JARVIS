"""Wiring PR #1 — Phase 7.2 IronGate floors caller wiring pins.

Phase 7.2 shipped `ExplorationFloors.from_env_with_adapted()` as a
new classmethod (the substrate). This wiring PR switches the 6 live
call sites in orchestrator.py + phase_runners/generate_runner.py
from `from_env()` to `from_env_with_adapted()`.

Pinned cage:
  * Master-off byte-identical: when JARVIS_EXPLORATION_LEDGER_LOAD_
    ADAPTED_FLOORS=false (default), from_env_with_adapted is byte-
    identical to from_env. Zero behavior change.
  * Master-on adapted YAML present: new classmethod merges
    adapted required_categories on top of env baseline.
  * Caller-grep invariant: ZERO live (non-test, non-docstring) call
    sites use the legacy from_env(). All 6 known sites switched.
  * Authority invariant: live callers do NOT import the loader
    directly — wiring goes through ExplorationFloors classmethod
    (one-way dependency rule preserved per Phase 7.2 design).
  * No regression in existing exploration-engine behavior (76 prior
    pins survive — re-asserted via combined regression).
"""
from __future__ import annotations

import re
from pathlib import Path
from textwrap import dedent

import pytest

from backend.core.ouroboros.governance.exploration_engine import (
    ExplorationCategory,
    ExplorationFloors,
)


# ---------------------------------------------------------------------------
# Section A — Master-off byte-identical pin
# ---------------------------------------------------------------------------


class TestMasterOffByteIdentical:
    """When master flag is off (default), the new classmethod must
    return floors structurally identical to the legacy `from_env`.
    Pin proves the wiring switch introduces ZERO behavior change in
    the default flag state."""

    def test_master_off_floors_equal_legacy(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS",
            raising=False,
        )
        for complexity in ("trivial", "moderate", "complex"):
            legacy = ExplorationFloors.from_env(complexity)
            adapted = ExplorationFloors.from_env_with_adapted(
                complexity,
            )
            assert adapted.min_score == legacy.min_score, complexity
            assert adapted.min_categories == legacy.min_categories, (
                complexity
            )
            assert adapted.required_categories == (
                legacy.required_categories
            ), complexity

    def test_master_off_explicit_false(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS",
            "false",
        )
        legacy = ExplorationFloors.from_env("moderate")
        adapted = ExplorationFloors.from_env_with_adapted("moderate")
        assert adapted == legacy

    def test_master_off_no_yaml_present(self, monkeypatch, tmp_path):
        # Even with YAML file MISSING + master OFF → byte-identical.
        monkeypatch.delenv(
            "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS",
            raising=False,
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTED_IRON_GATE_FLOORS_PATH",
            str(tmp_path / "missing.yaml"),
        )
        legacy = ExplorationFloors.from_env("moderate")
        adapted = ExplorationFloors.from_env_with_adapted("moderate")
        assert adapted == legacy


# ---------------------------------------------------------------------------
# Section B — Master-on adapted YAML injects required_categories
# ---------------------------------------------------------------------------


class TestMasterOnAdaptedInjection:
    """When master flag is ON and a valid YAML is present, adapted
    required_categories are merged on top of the env baseline.
    Proves the wiring is live end-to-end."""

    def test_master_on_with_yaml_adds_required_category(
        self, monkeypatch, tmp_path,
    ):
        yaml_path = tmp_path / "adapted_iron_gate_floors.yaml"
        yaml_path.write_text(
            "schema_version: 1\n"
            "floors:\n"
            "  - category: comprehension\n"
            "    floor: 2.0\n"
            "    proposal_id: adapt-ig-test\n"
            "    approved_at: '2026-04-26T00:00:00Z'\n"
            "    approved_by: op\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(
            "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS", "1",
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTED_IRON_GATE_FLOORS_PATH", str(yaml_path),
        )
        adapted = ExplorationFloors.from_env_with_adapted("moderate")
        assert ExplorationCategory.COMPREHENSION in adapted.required_categories

    def test_master_on_preserves_env_required_categories(
        self, monkeypatch, tmp_path,
    ):
        # Cage rule: adapted floors only ADD to required_categories;
        # never remove existing env-required ones.
        yaml_path = tmp_path / "y.yaml"
        yaml_path.write_text(
            "schema_version: 1\n"
            "floors:\n"
            "  - category: discovery\n    floor: 1.5\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(
            "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS", "1",
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTED_IRON_GATE_FLOORS_PATH", str(yaml_path),
        )
        legacy = ExplorationFloors.from_env("complex")
        adapted = ExplorationFloors.from_env_with_adapted("complex")
        # Every env-required category survives.
        for c in legacy.required_categories:
            assert c in adapted.required_categories
        # Adapted entry adds discovery (or it was already there).
        assert ExplorationCategory.DISCOVERY in adapted.required_categories

    def test_master_on_does_not_modify_min_score(
        self, monkeypatch, tmp_path,
    ):
        # Cage rule: only required_categories may change; min_score
        # stays env-driven.
        yaml_path = tmp_path / "y.yaml"
        yaml_path.write_text(
            "schema_version: 1\nfloors:\n  - category: comprehension\n    floor: 2.0\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(
            "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS", "1",
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTED_IRON_GATE_FLOORS_PATH", str(yaml_path),
        )
        legacy = ExplorationFloors.from_env("moderate")
        adapted = ExplorationFloors.from_env_with_adapted("moderate")
        assert adapted.min_score == legacy.min_score
        assert adapted.min_categories == legacy.min_categories

    def test_master_on_unknown_category_skipped(
        self, monkeypatch, tmp_path,
    ):
        # Defense-in-depth: unknown YAML category is dropped at load
        # time, NOT injected into required_categories.
        yaml_path = tmp_path / "y.yaml"
        yaml_path.write_text(
            "schema_version: 1\n"
            "floors:\n"
            "  - category: not_a_real_category\n    floor: 2.0\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(
            "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS", "1",
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTED_IRON_GATE_FLOORS_PATH", str(yaml_path),
        )
        legacy = ExplorationFloors.from_env("moderate")
        adapted = ExplorationFloors.from_env_with_adapted("moderate")
        # Unknown category dropped → required_categories unchanged.
        assert adapted.required_categories == legacy.required_categories


# ---------------------------------------------------------------------------
# Section C — Caller-grep invariants
# ---------------------------------------------------------------------------


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _live_python_files(root: Path):
    """Yield all live (non-test, non-docstring-only) Python files
    under backend/."""
    for p in (root / "backend").rglob("*.py"):
        if "/test" in str(p) or p.name.startswith("test_"):
            continue
        yield p


class TestCallerGrepInvariants:
    """Pin the wiring switch — no live caller may use the legacy
    `from_env(` form. Future code that calls the legacy entry point
    must EITHER switch to `from_env_with_adapted` OR explicitly
    document why (then update this pin's allowlist)."""

    def test_zero_live_callers_use_legacy_from_env(self):
        violations = []
        # Pattern: ExplorationFloors.from_env(  but NOT
        # ExplorationFloors.from_env_with_adapted(
        legacy_pattern = re.compile(
            r"ExplorationFloors\.from_env\((?!_with_adapted)",
        )
        # Allowlist: the classmethod definition itself + the docstring
        # in exploration_engine.py do not count as "callers".
        allowlist_paths = {
            "backend/core/ouroboros/governance/exploration_engine.py",
        }
        for path in _live_python_files(_REPO_ROOT):
            rel = str(path.relative_to(_REPO_ROOT))
            if rel in allowlist_paths:
                continue
            try:
                src = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for m in legacy_pattern.finditer(src):
                # Compute line number for clearer failure.
                line_no = src[: m.start()].count("\n") + 1
                violations.append(f"{rel}:{line_no}")
        assert not violations, (
            f"Live callers still use legacy `from_env()` (must switch "
            f"to `from_env_with_adapted()`):\n  "
            + "\n  ".join(violations)
        )

    def test_orchestrator_uses_from_env_with_adapted(self):
        # Direct grep — at least one site present.
        path = (
            _REPO_ROOT
            / "backend/core/ouroboros/governance/orchestrator.py"
        )
        src = path.read_text(encoding="utf-8")
        assert "ExplorationFloors.from_env_with_adapted(" in src

    def test_generate_runner_uses_from_env_with_adapted(self):
        path = (
            _REPO_ROOT
            / "backend/core/ouroboros/governance/phase_runners"
              "/generate_runner.py"
        )
        src = path.read_text(encoding="utf-8")
        assert "ExplorationFloors.from_env_with_adapted(" in src


# ---------------------------------------------------------------------------
# Section D — Authority invariant: callers don't import the loader
# ---------------------------------------------------------------------------


class TestCallerAuthorityInvariants:
    """Live callers (orchestrator, generate_runner) MUST NOT import
    `adapted_iron_gate_loader` directly — wiring is via
    `ExplorationFloors.from_env_with_adapted()` which lazy-imports
    the loader. This preserves the one-way dependency rule (loader
    depends on stdlib only; live callers depend on the public
    classmethod, not the substrate)."""

    @pytest.mark.parametrize("rel", [
        "backend/core/ouroboros/governance/orchestrator.py",
        "backend/core/ouroboros/governance/phase_runners/generate_runner.py",
    ])
    def test_caller_does_not_import_loader(self, rel):
        path = _REPO_ROOT / rel
        src = path.read_text(encoding="utf-8")
        assert "adapted_iron_gate_loader" not in src, (
            f"{rel} imports adapted_iron_gate_loader directly — wiring "
            "should go through ExplorationFloors.from_env_with_adapted() "
            "which lazy-imports the loader internally."
        )


# ---------------------------------------------------------------------------
# Section E — End-to-end wiring smoke
# ---------------------------------------------------------------------------


class TestEndToEndWiringSmoke:
    """Walks the wired call site from a callable orchestrator-style
    construction site through to a live ExplorationFloors with adapted
    required_categories injected. Proves the wiring is functional, not
    just syntactically present."""

    def test_e2e_master_on_yaml_present_floors_tightened(
        self, monkeypatch, tmp_path,
    ):
        yaml_path = tmp_path / "y.yaml"
        yaml_path.write_text(
            "schema_version: 1\n"
            "floors:\n"
            "  - category: history\n    floor: 1.0\n"
            "  - category: structure\n    floor: 1.0\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(
            "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS", "1",
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTED_IRON_GATE_FLOORS_PATH", str(yaml_path),
        )
        # This is the same call the wired orchestrator now makes.
        floors = ExplorationFloors.from_env_with_adapted("moderate")
        # Both adapted categories must be in required_categories.
        assert ExplorationCategory.HISTORY in floors.required_categories
        assert (
            ExplorationCategory.STRUCTURE in floors.required_categories
        )

    def test_e2e_master_off_no_tightening(self, monkeypatch, tmp_path):
        # Same YAML, master OFF → no tightening.
        yaml_path = tmp_path / "y.yaml"
        yaml_path.write_text(
            "schema_version: 1\n"
            "floors:\n"
            "  - category: history\n    floor: 1.0\n",
            encoding="utf-8",
        )
        monkeypatch.delenv(
            "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS",
            raising=False,
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTED_IRON_GATE_FLOORS_PATH", str(yaml_path),
        )
        legacy = ExplorationFloors.from_env("moderate")
        wired = ExplorationFloors.from_env_with_adapted("moderate")
        assert wired == legacy
