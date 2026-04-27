"""Wiring PR #3 — Phase 7.4 risk-tier ladder caller wiring pins.

Phase 7.4 shipped `compute_extended_ladder(base_ladder, adapted=None)`
as the substrate (PR #23118). This wiring PR threads that helper into
`risk_tier_floor.py` via a new `get_active_tier_order()` function and
switches all 6 internal `_ORDER` consumers to use it.

Pinned cage:
  * Master-off byte-identical: when JARVIS_RISK_TIER_FLOOR_LOAD_
    ADAPTED_TIERS=false (default), get_active_tier_order() returns
    dict equal to canonical _ORDER. Zero behavior change.
  * Master-on extends ladder: adapted YAML inserts new tier(s)
    between base tiers; get_active_tier_order() returns extended dict.
  * Case normalization: adapted YAML uses uppercase (matches Slice
    4b miner's `_synthesize_tier_name` charset [A-Z0-9_]+); wiring
    lowercases at the boundary so _ORDER consumers find the new
    tiers under the canonical lowercase convention.
  * Defense-in-depth: loader raise → falls back to canonical _ORDER
    baseline (NEVER raises into caller).
  * Caller-grep: ZERO live consumers of `_ORDER[X]` / `X in _ORDER`
    in risk_tier_floor.py outside `get_active_tier_order()` itself.
  * Behavioral: env-floor / vision-floor / recommended-floor /
    apply-floor-to-name all recognize the extended ladder.
  * Authority: get_active_tier_order is THE single wiring boundary;
    callers don't import the loader directly.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

from backend.core.ouroboros.governance.risk_tier_floor import (
    _ORDER,
    apply_floor_to_name,
    get_active_tier_order,
    recommended_floor,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]
_FLOOR_PATH = (
    _REPO_ROOT
    / "backend/core/ouroboros/governance/risk_tier_floor.py"
)


# ---------------------------------------------------------------------------
# Section A — get_active_tier_order: master-off byte-identical
# ---------------------------------------------------------------------------


class TestMasterOffByteIdentical:
    def test_master_off_returns_dict_equal_to_canonical_order(
        self, monkeypatch,
    ):
        monkeypatch.delenv(
            "JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS",
            raising=False,
        )
        active = get_active_tier_order()
        assert active == _ORDER
        # Same content, but a NEW dict (mutation-safe for callers).
        assert active is not _ORDER

    def test_master_off_explicit_false(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS", "false",
        )
        assert get_active_tier_order() == _ORDER

    def test_master_off_no_yaml_present(self, monkeypatch, tmp_path):
        monkeypatch.delenv(
            "JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS",
            raising=False,
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTED_RISK_TIERS_PATH",
            str(tmp_path / "missing.yaml"),
        )
        assert get_active_tier_order() == _ORDER

    def test_caller_mutation_does_not_affect_canonical_order(
        self, monkeypatch,
    ):
        monkeypatch.delenv(
            "JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS",
            raising=False,
        )
        active = get_active_tier_order()
        active["new_thing"] = 99
        # Re-fetch — must not see the mutation.
        active2 = get_active_tier_order()
        assert "new_thing" not in active2


# ---------------------------------------------------------------------------
# Section B — Master-on extends ladder
# ---------------------------------------------------------------------------


class TestMasterOnExtension:
    def test_master_on_inserts_adapted_tier(
        self, monkeypatch, tmp_path,
    ):
        # Insert a new tier between NOTIFY_APPLY and APPROVAL_REQUIRED.
        yaml_path = tmp_path / "y.yaml"
        yaml_path.write_text(
            "schema_version: 1\n"
            "tiers:\n"
            "  - tier_name: NOTIFY_APPLY_HARDENED_NETWORK\n"
            "    insert_after: NOTIFY_APPLY\n"
            "    failure_class: network_egress\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(
            "JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS", "1",
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTED_RISK_TIERS_PATH", str(yaml_path),
        )
        active = get_active_tier_order()
        # New tier present, lowercased.
        assert "notify_apply_hardened_network" in active
        # All canonical tiers preserved.
        for c in ("safe_auto", "notify_apply",
                  "approval_required", "blocked"):
            assert c in active
        # Rank ordering: new tier slots immediately after notify_apply.
        assert (
            active["notify_apply"]
            < active["notify_apply_hardened_network"]
            < active["approval_required"]
        )

    def test_master_on_canonical_relative_order_preserved(
        self, monkeypatch, tmp_path,
    ):
        # Defense-in-depth: even with multiple adapted insertions,
        # canonical tier ordering must be preserved.
        yaml_path = tmp_path / "y.yaml"
        yaml_path.write_text(
            "schema_version: 1\n"
            "tiers:\n"
            "  - tier_name: NOTIFY_APPLY_X\n    insert_after: NOTIFY_APPLY\n"
            "  - tier_name: BLOCKED_X\n    insert_after: BLOCKED\n"
            "  - tier_name: SAFE_AUTO_X\n    insert_after: SAFE_AUTO\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(
            "JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS", "1",
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTED_RISK_TIERS_PATH", str(yaml_path),
        )
        active = get_active_tier_order()
        # Canonical strict ordering: safe_auto < notify_apply <
        # approval_required < blocked.
        assert (
            active["safe_auto"]
            < active["notify_apply"]
            < active["approval_required"]
            < active["blocked"]
        )

    def test_master_on_unknown_insert_after_skipped(
        self, monkeypatch, tmp_path,
    ):
        # Defense-in-depth: insert_after not in base ladder → SKIPPED.
        yaml_path = tmp_path / "y.yaml"
        yaml_path.write_text(
            "schema_version: 1\n"
            "tiers:\n"
            "  - tier_name: WONT_LAND\n    insert_after: DOES_NOT_EXIST\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(
            "JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS", "1",
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTED_RISK_TIERS_PATH", str(yaml_path),
        )
        active = get_active_tier_order()
        assert "wont_land" not in active
        assert active == _ORDER  # base unchanged

    def test_master_on_adapted_tier_lowercased_for_lookup(
        self, monkeypatch, tmp_path,
    ):
        # The wiring layer must lowercase adapted tier names so
        # downstream consumers (which use lowercase per _norm_tier
        # convention) can find them.
        yaml_path = tmp_path / "y.yaml"
        yaml_path.write_text(
            "schema_version: 1\n"
            "tiers:\n"
            "  - tier_name: APPROVAL_REQUIRED_HARDENED_PERM\n"
            "    insert_after: APPROVAL_REQUIRED\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(
            "JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS", "1",
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTED_RISK_TIERS_PATH", str(yaml_path),
        )
        active = get_active_tier_order()
        # Lookup MUST work via lowercase (the canonical convention).
        assert "approval_required_hardened_perm" in active
        # Uppercase form NOT present (we lowercase at the boundary).
        assert "APPROVAL_REQUIRED_HARDENED_PERM" not in active


# ---------------------------------------------------------------------------
# Section C — Defense-in-depth (loader raises)
# ---------------------------------------------------------------------------


class TestDefenseInDepth:
    def test_loader_raise_falls_back_to_canonical(self, monkeypatch):
        from backend.core.ouroboros.governance.adaptation import (
            adapted_risk_tier_loader as loader,
        )
        monkeypatch.setenv(
            "JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS", "1",
        )
        with mock.patch.object(
            loader, "compute_extended_ladder",
            side_effect=RuntimeError("boom"),
        ):
            # NEVER raises into caller; falls back to canonical.
            assert get_active_tier_order() == _ORDER


# ---------------------------------------------------------------------------
# Section D — Caller-source invariants
# ---------------------------------------------------------------------------


class TestCallerSourceInvariants:
    def test_zero_remaining_internal_order_lookups(self):
        """All internal `_ORDER[X]` / `X in _ORDER` consumers in
        risk_tier_floor.py must be switched to `get_active_tier_order()`.
        Only the canonical definition + docstring references may remain."""
        src = _FLOOR_PATH.read_text(encoding="utf-8")
        # Allowed: assignment line, docstring text, the wiring helper
        # body itself.
        # Forbidden in any function body: `_ORDER[<name>]` or
        # `<name> in _ORDER` outside get_active_tier_order().
        # We approximate by checking the helper body bounds.
        # Simpler: count consumer-style uses outside the helper.
        helper_start = src.find("def get_active_tier_order(")
        helper_end = src.find("\ndef _norm_tier(", helper_start)
        outside = src[:helper_start] + src[helper_end:]
        # Forbidden patterns (allowing the canonical assignment _ORDER = {...}
        # by excluding "_ORDER = {").
        forbidden_lookup = re.compile(r"_ORDER\[")
        forbidden_membership = re.compile(r"\bin\s+_ORDER\b")
        lookup_hits = forbidden_lookup.findall(outside)
        membership_hits = forbidden_membership.findall(outside)
        assert not lookup_hits, (
            f"Live `_ORDER[...]` consumers remain outside the wiring "
            f"helper — must switch to get_active_tier_order(): "
            f"{len(lookup_hits)} hits"
        )
        assert not membership_hits, (
            f"Live `... in _ORDER` consumers remain outside the wiring "
            f"helper — must switch to get_active_tier_order(): "
            f"{len(membership_hits)} hits"
        )

    def test_wiring_helper_imports_compute_extended_ladder(self):
        src = _FLOOR_PATH.read_text(encoding="utf-8")
        # The helper must lazy-import the substrate function.
        assert "compute_extended_ladder" in src
        assert (
            "from backend.core.ouroboros.governance.adaptation"
            ".adapted_risk_tier_loader import" in src
        )

    def test_wiring_helper_returns_new_dict(self):
        # Pin the mutation-safety contract via source check.
        src = _FLOOR_PATH.read_text(encoding="utf-8")
        # Either `dict(_ORDER)` fallback OR comprehension-construction
        # of the extended dict.
        assert (
            "dict(_ORDER)" in src
            or "{name.lower(): rank" in src
        )


# ---------------------------------------------------------------------------
# Section E — Behavioral: downstream consumers see extended ladder
# ---------------------------------------------------------------------------


def _seed_yaml_with_new_tier(monkeypatch, tmp_path, tier_upper, after_upper):
    yaml_path = tmp_path / "y.yaml"
    yaml_path.write_text(
        "schema_version: 1\n"
        "tiers:\n"
        f"  - tier_name: {tier_upper}\n"
        f"    insert_after: {after_upper}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS", "1",
    )
    monkeypatch.setenv(
        "JARVIS_ADAPTED_RISK_TIERS_PATH", str(yaml_path),
    )
    return tier_upper.lower()


class TestBehavioralWiring:
    def test_env_min_risk_tier_recognizes_adapted_tier(
        self, monkeypatch, tmp_path,
    ):
        new_tier = _seed_yaml_with_new_tier(
            monkeypatch, tmp_path,
            "NOTIFY_APPLY_HARDENED_X", "NOTIFY_APPLY",
        )
        # Set JARVIS_MIN_RISK_TIER to the NEW adapted tier — must
        # be recognized (was previously rejected as unrecognised).
        monkeypatch.setenv("JARVIS_MIN_RISK_TIER", new_tier)
        # recommended_floor should pick up the explicit floor.
        floor = recommended_floor()
        assert floor == new_tier

    def test_apply_floor_to_name_passes_through_unknown(
        self, monkeypatch, tmp_path,
    ):
        # Pre-existing behavior: unknown tier names pass through.
        # Verify that an unknown tier is STILL unknown after wiring
        # (extended ladder doesn't introduce false-positives).
        monkeypatch.delenv(
            "JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS",
            raising=False,
        )
        out, applied = apply_floor_to_name("totally_unknown_tier")
        assert out == "totally_unknown_tier"
        assert applied is None

    def test_apply_floor_to_name_accepts_adapted_tier_name(
        self, monkeypatch, tmp_path,
    ):
        new_tier = _seed_yaml_with_new_tier(
            monkeypatch, tmp_path,
            "BLOCKED_HARDENED_PERM", "BLOCKED",
        )
        # Without floor configured, no upgrade should fire.
        out, applied = apply_floor_to_name(new_tier)
        assert out == new_tier
        assert applied is None

    def test_recommended_floor_uses_extended_ranking(
        self, monkeypatch, tmp_path,
    ):
        # Set explicit floor to NEW adapted tier; engage paranoia
        # mode (notify_apply). The strictest-wins rule should pick
        # the adapted tier (it's positioned strictly above
        # notify_apply per its insert_after).
        new_tier = _seed_yaml_with_new_tier(
            monkeypatch, tmp_path,
            "NOTIFY_APPLY_HARDENED_NET", "NOTIFY_APPLY",
        )
        monkeypatch.setenv("JARVIS_MIN_RISK_TIER", new_tier)
        monkeypatch.setenv("JARVIS_PARANOIA_MODE", "1")
        floor = recommended_floor()
        # Paranoia is notify_apply; explicit env is the new
        # adapted tier (rank > notify_apply). Strictest wins.
        assert floor == new_tier

    def test_recommended_floor_master_off_byte_identical(
        self, monkeypatch,
    ):
        # No adapted YAML loaded; behavior must match pre-wiring exactly.
        monkeypatch.delenv(
            "JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS",
            raising=False,
        )
        monkeypatch.setenv("JARVIS_MIN_RISK_TIER", "approval_required")
        monkeypatch.setenv("JARVIS_PARANOIA_MODE", "0")
        floor = recommended_floor()
        assert floor == "approval_required"

    def test_apply_floor_master_off_byte_identical(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS",
            raising=False,
        )
        monkeypatch.setenv("JARVIS_MIN_RISK_TIER", "approval_required")
        out, applied = apply_floor_to_name("safe_auto")
        assert out == "approval_required"
        assert applied == "approval_required"


# ---------------------------------------------------------------------------
# Section F — Authority invariants
# ---------------------------------------------------------------------------


class TestAuthorityInvariants:
    def test_no_external_imports_of_underscore_order(self):
        # `_ORDER` is module-private; no live caller should import it
        # directly. This pin guards against accidental reach-around.
        violations = []
        backend_dir = _REPO_ROOT / "backend"
        for path in backend_dir.rglob("*.py"):
            rel = str(path.relative_to(_REPO_ROOT))
            if "/test" in rel or path.name.startswith("test_"):
                continue
            if rel == "backend/core/ouroboros/governance/risk_tier_floor.py":
                continue
            try:
                src = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if "from backend.core.ouroboros.governance.risk_tier_floor import" in src:
                # Only legitimate import is `apply_floor_to_name` (and
                # other public APIs). _ORDER must not appear.
                if "_ORDER" in src:
                    violations.append(rel)
        assert not violations, (
            f"External callers import private _ORDER directly:\n  "
            + "\n  ".join(violations)
        )
