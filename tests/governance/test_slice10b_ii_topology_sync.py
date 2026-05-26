"""Slice 10B-ii — Dynamic topology sync: provider_topology consults PromotionLedger for trusted-seed bypass.

Closes the two-registry disconnect surfaced by soak bt-2026-05-26-000630
(v11 DW-PRIMARY soak). Slice 10B (PR #58165) seeded
``JARVIS_DW_TRUSTED_MODELS`` into PromotionLedger, but the topology
check (``dw_allowed_for_route`` / ``dw_models_for_route``) never
consulted PromotionLedger — it only read brain_selection_policy.yaml
where ``dw_allowed: false`` + ``dw_models: []`` for STANDARD/COMPLEX/
BG/SPEC remained the Phase-12 frozen contract awaiting catalog
discovery. Result: trusted seeds were structurally invisible to the
topology gate; every STANDARD op cascaded to Claude.

# Empirical proof from bt-2026-05-26-000630

  17:00  [PromotionLedger] Slice 10B: seeded 1 trusted model(s) ✓
  17:09  Route: standard (swe_bench_pro_envelope:not_human_blocking) ✓
  17:09  [CandidateGenerator] Topology block: route=standard
         reason=Static list purged; ranking authority is dw_catalog_classifier
         — routing direct to Claude  ← THE BUG
  17:09  → Claude (BadRequestError: credit balance too low)
  17:57  LoopDeadman tombstone — engine dead, $0.00 burn

# Fix mechanism

Add ``_trusted_seed_dw_models_for_route(route)`` helper that:
  1. Returns empty tuple for IMMEDIATE (Manifesto §5 exclusion)
  2. Loads PromotionLedger.promoted_models()
  3. For each promoted model_id, builds a synthetic ModelCard with
     parameter_count_b parsed from the model_id via the existing
     parse_parameter_count helper (e.g., "doubleword-397b" → 397.0)
  4. Filters via dw_catalog_classifier.gate_for_route (same per-route
     param/price thresholds as the normal discovery classifier)
  5. Returns the admitted model_ids

Two integration points in ProviderTopology:
  - dw_allowed_for_route: returns True when bypass returns non-empty
  - dw_models_for_route: returns bypass list when YAML+catalog empty

YAML stays the SAFETY CONTRACT (immutable cost-policy); PromotionLedger
becomes the RUNTIME AUTHORITY when operator attests specific models.

# Test surface (3 AST pins + 6 spine)
"""

from __future__ import annotations

import ast
import os
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOPOLOGY_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "provider_topology.py"
)


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 3
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_trusted_seed_helper_present() -> None:
    """``_trusted_seed_dw_models_for_route`` MUST exist as a
    module-level function. Slice 10B-ii's bridge depends on it."""
    src = TOPOLOGY_FILE.read_text()
    assert "def _trusted_seed_dw_models_for_route(" in src, (
        "Slice 10B-ii helper function missing — bypass dead"
    )
    # Slice 10B-ii attribution + bt soak link
    assert "Slice 10B-ii" in src
    assert "bt-2026-05-26-000630" in src, (
        "Missing soak attribution — future readers can't trace which "
        "forensic surfaced the two-registry disconnect"
    )
    # IMMEDIATE exclusion (Manifesto §5)
    assert "_TRUSTED_SEED_BYPASS_FORBIDDEN_ROUTES" in src
    assert '"immediate"' in src, (
        "IMMEDIATE not in forbidden-routes frozenset — bypass leaks "
        "and violates Manifesto §5 (Claude-direct for human-reflex)"
    )


def test_ast_pin_dw_allowed_for_route_consults_bypass() -> None:
    """``dw_allowed_for_route`` MUST call
    ``_trusted_seed_dw_models_for_route`` when the YAML returns
    ``dw_allowed=False``. Without this hook, the candidate_generator's
    v1 topology-block path never sees the bypass."""
    src = TOPOLOGY_FILE.read_text()
    tree = ast.parse(src, filename=str(TOPOLOGY_FILE))
    found_call = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "dw_allowed_for_route"
        ):
            body_src = ast.unparse(node)
            if "_trusted_seed_dw_models_for_route" in body_src:
                found_call = True
                break
    assert found_call, (
        "dw_allowed_for_route does not call "
        "_trusted_seed_dw_models_for_route — Slice 10B-ii bridge "
        "inert on the v1 topology-check path"
    )


def test_ast_pin_dw_models_for_route_consults_bypass() -> None:
    """``dw_models_for_route`` MUST consult
    ``_trusted_seed_dw_models_for_route`` as a fallback after
    catalog + YAML both return empty. Without this hook, the v2
    sentinel-mode topology check (and candidate_generator's
    sentinel-on path) miss the bypass."""
    src = TOPOLOGY_FILE.read_text()
    tree = ast.parse(src, filename=str(TOPOLOGY_FILE))
    found_call = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "dw_models_for_route"
        ):
            body_src = ast.unparse(node)
            if "_trusted_seed_dw_models_for_route" in body_src:
                found_call = True
                break
    assert found_call, (
        "dw_models_for_route does not call "
        "_trusted_seed_dw_models_for_route — bridge inert on v2 path"
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — 6 (functional)
# ──────────────────────────────────────────────────────────────────────


def test_spine_trusted_seed_admits_non_immediate_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With JARVIS_DW_TRUSTED_MODELS=doubleword-397b set, the helper
    must admit the model into STANDARD, COMPLEX, BACKGROUND, and
    SPECULATIVE routes (per-route gates pass)."""
    from backend.core.ouroboros.governance.provider_topology import (
        _trusted_seed_dw_models_for_route,
    )
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("JARVIS_DW_TRUSTED_MODELS", "doubleword-397b")
        monkeypatch.setenv(
            "JARVIS_DW_PROMOTION_LEDGER_PATH",
            str(Path(tmp) / "ledger.json"),
        )
        for route in ("standard", "complex", "background", "speculative"):
            seeds = _trusted_seed_dw_models_for_route(route)
            assert "doubleword-397b" in seeds, (
                f"Trusted seed NOT admitted into {route} route — "
                f"Slice 10B-ii bridge broken on {route}; got {seeds}"
            )


def test_spine_immediate_route_excluded_from_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manifesto §5: IMMEDIATE is Claude-direct by design. Even with
    trusted seeds present, IMMEDIATE must return empty tuple. The
    operator's cost preference does NOT override §5 human-reflex
    routing."""
    from backend.core.ouroboros.governance.provider_topology import (
        _trusted_seed_dw_models_for_route,
    )
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("JARVIS_DW_TRUSTED_MODELS", "doubleword-397b")
        monkeypatch.setenv(
            "JARVIS_DW_PROMOTION_LEDGER_PATH",
            str(Path(tmp) / "ledger.json"),
        )
        seeds = _trusted_seed_dw_models_for_route("immediate")
        assert seeds == (), (
            f"IMMEDIATE bypass LEAKED — violates Manifesto §5 "
            f"(Claude-direct, speed supersedes cost); got {seeds}"
        )


def test_spine_no_trusted_seeds_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no trusted seeds are configured, helper returns empty
    tuple for all routes (byte-equivalent to pre-Slice-10B-ii)."""
    from backend.core.ouroboros.governance.provider_topology import (
        _trusted_seed_dw_models_for_route,
    )
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.delenv("JARVIS_DW_TRUSTED_MODELS", raising=False)
        monkeypatch.setenv(
            "JARVIS_DW_PROMOTION_LEDGER_PATH",
            str(Path(tmp) / "ledger.json"),
        )
        for route in ("standard", "complex", "background", "speculative"):
            seeds = _trusted_seed_dw_models_for_route(route)
            assert seeds == (), (
                f"Empty trusted-seed env produced phantom seeds on {route}: {seeds}"
            )


def test_spine_model_id_with_no_parseable_size_filtered_by_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trusted model_id without a parseable parameter size
    (e.g., 'mystery-model') must FAIL the STANDARD/COMPLEX gates
    (min_params_b=14B/30B) because parameter_count_b parses to None,
    AND the gate rejects when min > 0 AND card.parameter_count_b is None.
    The model SHOULD pass BACKGROUND/SPECULATIVE which don't gate on params."""
    from backend.core.ouroboros.governance.provider_topology import (
        _trusted_seed_dw_models_for_route,
    )
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("JARVIS_DW_TRUSTED_MODELS", "mystery-model")
        monkeypatch.setenv(
            "JARVIS_DW_PROMOTION_LEDGER_PATH",
            str(Path(tmp) / "ledger.json"),
        )
        assert _trusted_seed_dw_models_for_route("standard") == ()
        assert _trusted_seed_dw_models_for_route("complex") == ()
        # BACKGROUND + SPECULATIVE have no min_params_b → admit
        assert "mystery-model" in _trusted_seed_dw_models_for_route("background")
        assert "mystery-model" in _trusted_seed_dw_models_for_route("speculative")


def test_spine_dw_allowed_for_route_bypass_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: when YAML returns dw_allowed=False for STANDARD
    (the production state) AND a trusted seed exists, the public
    dw_allowed_for_route method must return True. This is the
    contract the candidate_generator's topology-block check reads
    on the v1 path."""
    from backend.core.ouroboros.governance.provider_topology import (
        get_topology,
    )
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("JARVIS_DW_TRUSTED_MODELS", "doubleword-397b")
        monkeypatch.setenv(
            "JARVIS_DW_PROMOTION_LEDGER_PATH",
            str(Path(tmp) / "ledger.json"),
        )
        topology = get_topology()
        # If topology is fully disabled, the test is moot (always returns
        # True). Skip in that case.
        if not topology.enabled:
            pytest.skip("Topology disabled in this environment")
        allowed_standard = topology.dw_allowed_for_route("standard")
        allowed_immediate = topology.dw_allowed_for_route("immediate")
        assert allowed_standard is True, (
            "Slice 10B-ii: STANDARD route should be DW-allowed with "
            "trusted seed present (bridge failed)"
        )
        # IMMEDIATE: production yaml has dw_allowed=False AND bypass
        # excludes IMMEDIATE per §5 → stays False
        assert allowed_immediate is False, (
            "IMMEDIATE bypass LEAKED end-to-end — §5 violated"
        )


def test_spine_dw_models_for_route_bypass_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: dw_models_for_route must return the trusted seed
    list when YAML+catalog are both empty for STANDARD. This is the
    contract sentinel-mode topology checks read on the v2 path."""
    from backend.core.ouroboros.governance.provider_topology import (
        get_topology,
    )
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("JARVIS_DW_TRUSTED_MODELS", "doubleword-397b")
        monkeypatch.setenv(
            "JARVIS_DW_PROMOTION_LEDGER_PATH",
            str(Path(tmp) / "ledger.json"),
        )
        topology = get_topology()
        if not topology.enabled:
            pytest.skip("Topology disabled in this environment")
        models_standard = topology.dw_models_for_route("standard")
        models_immediate = topology.dw_models_for_route("immediate")
        assert "doubleword-397b" in models_standard, (
            f"Slice 10B-ii: dw_models_for_route('standard') missing "
            f"trusted seed; got {models_standard}"
        )
        # IMMEDIATE excluded — even if YAML had a model, the bypass
        # for IMMEDIATE returns empty; here YAML is also empty for IMMEDIATE
        # so the assertion is that doubleword-397b is NOT in the result.
        assert "doubleword-397b" not in models_immediate, (
            "IMMEDIATE bypass LEAKED into dw_models_for_route"
        )
