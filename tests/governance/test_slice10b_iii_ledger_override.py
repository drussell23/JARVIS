"""Slice 10B-iii — Trusted-seed overrides ambiguous_metadata quarantine (state hierarchy fix).

Closes the design flaw surfaced by soak bt-2026-05-26-062945 (FLEET v12).
Pre-Slice-10B-iii ``_seed_trusted_models_from_env`` had a single
"disk wins on conflict" rule, conflating TWO distinct kinds of
disk state:

  1. **Operator decisions** (origin=operator_demoted, demoted_from_bg)
     — env trusted seed MUST NOT override (would silently revert)
  2. **Automatic classifier quarantine** (origin=ambiguous_metadata)
     — AUTO safety pin from `dw_catalog_classifier`; absence of an
     operator decision, NOT one. Env attestation SHOULD override.

# Empirical proof from bt-2026-05-26-062945

  v12 disk had 18 models from prior discovery, all quarantined with
  origin=ambiguous_metadata. Operator set JARVIS_DW_TRUSTED_MODELS=
  "Qwen3.5-397B,Qwen3.5-35B,Qwen3.5-4B,Kimi-K2.6". Pre-Slice-10B-iii:

    seeded 1 trusted model(s) from JARVIS_DW_TRUSTED_MODELS

  Only 1 (the 4B not yet on disk) seeded. The other 3 (already on
  disk as ambiguous_metadata) silently skipped → fleet expansion
  inert → topology bridge couldn't promote them → only 397B (from
  prior soak's legacy `doubleword-397b` alias) actually dispatched.

# Fix mechanism — state hierarchy override

  No disk record         → seed (create promoted=True)
  origin=ambiguous_metadata → OVERRIDE: promote + flip to trusted_seed
  origin=trusted_seed    → idempotent (already promoted)
  origin=operator_demoted → SKIP (operator decision wins)
  origin=demoted_from_bg → SKIP (post-promotion failure wins)
  Other origins → SKIP (defensive)

The override clears stale latency history (the prior auto-quarantine
never ran the model; historical latencies don't apply to the new
trusted-seed promotion path).

# Test surface (2 AST pins + 6 spine)
"""

from __future__ import annotations

import ast
import json
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LEDGER_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "dw_promotion_ledger.py"
)


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 2
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_overridable_origins_frozenset_present() -> None:
    """The ``_OVERRIDABLE_ORIGINS`` frozenset MUST contain
    ``QUARANTINE_AMBIGUOUS_METADATA`` and MUST NOT contain
    ``QUARANTINE_OPERATOR_DEMOTED`` or ``QUARANTINE_DEMOTED_FROM_BG``
    (those are operator decisions and must be preserved)."""
    src = LEDGER_FILE.read_text()
    assert "_OVERRIDABLE_ORIGINS" in src, (
        "Missing _OVERRIDABLE_ORIGINS frozenset — Slice 10B-iii reverted"
    )
    # AST walk to find the assignment and verify membership
    tree = ast.parse(src, filename=str(LEDGER_FILE))
    found_with_ambiguous = False
    operator_decisions_excluded = True
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "_OVERRIDABLE_ORIGINS"
                for t in node.targets
            )
        ):
            members_src = ast.unparse(node.value)
            if "QUARANTINE_AMBIGUOUS_METADATA" in members_src:
                found_with_ambiguous = True
            if (
                "QUARANTINE_OPERATOR_DEMOTED" in members_src
                or "QUARANTINE_DEMOTED_FROM_BG" in members_src
            ):
                operator_decisions_excluded = False
    assert found_with_ambiguous, (
        "_OVERRIDABLE_ORIGINS does not include QUARANTINE_AMBIGUOUS_METADATA "
        "— auto-quarantine will NOT yield to operator attestation"
    )
    assert operator_decisions_excluded, (
        "_OVERRIDABLE_ORIGINS INCLUDES an operator-decision origin — "
        "Slice 10B-iii violates operator-decision-wins discipline"
    )


def test_ast_pin_seed_method_has_override_path() -> None:
    """``_seed_trusted_models_from_env`` MUST have a branch that
    handles the OVERRIDE path (existing record + overridable origin).
    Without it, the seed only handles new-record creation and
    pre-Slice-10B-iii behavior persists."""
    src = LEDGER_FILE.read_text()
    tree = ast.parse(src, filename=str(LEDGER_FILE))
    found_override = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "_seed_trusted_models_from_env"
        ):
            body_src = ast.unparse(node)
            # The body must reference both _OVERRIDABLE_ORIGINS AND
            # set existing.promoted = True (the override mutation)
            if (
                "_OVERRIDABLE_ORIGINS" in body_src
                and "existing.promoted = True" in body_src
            ):
                found_override = True
                break
    assert found_override, (
        "_seed_trusted_models_from_env does not implement the override "
        "path — Slice 10B-iii bridge is inert"
    )
    # Slice 10B-iii attribution
    assert "Slice 10B-iii" in src
    assert "bt-2026-05-26-062945" in src, (
        "Missing soak attribution — future readers can't trace which "
        "FLEET v12 forensic exposed this"
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — 6 (functional)
# ──────────────────────────────────────────────────────────────────────


def _write_disk_record(
    ledger_path: Path,
    model_id: str,
    origin: str,
    *,
    promoted: bool = False,
) -> None:
    from backend.core.ouroboros.governance.dw_promotion_ledger import (
        LEDGER_SCHEMA_VERSION,
    )
    ledger_path.write_text(json.dumps({
        "schema_version": LEDGER_SCHEMA_VERSION,
        "records": [{
            "model_id": model_id,
            "quarantine_origin": origin,
            "success_latencies_ms": [],
            "failure_count": 0,
            "promoted": promoted,
            "promoted_at_unix": None,
            "last_event_unix": 0.0,
        }],
    }))


def test_spine_ambiguous_metadata_overridden_by_env_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The EXACT v12 bug scenario: model exists on disk as
    ambiguous_metadata + env names it as trusted → promote + flip
    origin to trusted_seed."""
    from backend.core.ouroboros.governance.dw_promotion_ledger import (
        PromotionLedger, QUARANTINE_AMBIGUOUS_METADATA,
    )
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "ledger.json"
        _write_disk_record(p, "Qwen/Qwen3.5-397B-A17B-FP8",
                           QUARANTINE_AMBIGUOUS_METADATA)
        monkeypatch.setenv(
            "JARVIS_DW_TRUSTED_MODELS", "Qwen/Qwen3.5-397B-A17B-FP8",
        )
        monkeypatch.setenv(
            "JARVIS_DW_PROMOTION_LEDGER_PATH", str(p),
        )
        ledger = PromotionLedger(path=p, autosave=False)
        ledger.load()
        assert ledger.is_promoted("Qwen/Qwen3.5-397B-A17B-FP8") is True, (
            "Slice 10B-iii failed to override ambiguous_metadata quarantine"
        )


def test_spine_operator_demoted_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator demotion MUST win over env seed. The operator's
    explicit decision is the authoritative record."""
    from backend.core.ouroboros.governance.dw_promotion_ledger import (
        PromotionLedger, QUARANTINE_OPERATOR_DEMOTED,
    )
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "ledger.json"
        _write_disk_record(p, "banned-model", QUARANTINE_OPERATOR_DEMOTED)
        monkeypatch.setenv("JARVIS_DW_TRUSTED_MODELS", "banned-model")
        monkeypatch.setenv(
            "JARVIS_DW_PROMOTION_LEDGER_PATH", str(p),
        )
        ledger = PromotionLedger(path=p, autosave=False)
        ledger.load()
        assert ledger.is_promoted("banned-model") is False, (
            "Slice 10B-iii incorrectly overrode operator_demoted decision — "
            "operator's persistent choice was silently discarded"
        )


def test_spine_demoted_from_bg_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-promotion failure (demoted_from_bg) MUST win over env seed.
    It's empirically-evidenced demotion, not auto-classification."""
    from backend.core.ouroboros.governance.dw_promotion_ledger import (
        PromotionLedger, QUARANTINE_DEMOTED_FROM_BG,
    )
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "ledger.json"
        _write_disk_record(p, "flaky-model", QUARANTINE_DEMOTED_FROM_BG)
        monkeypatch.setenv("JARVIS_DW_TRUSTED_MODELS", "flaky-model")
        monkeypatch.setenv(
            "JARVIS_DW_PROMOTION_LEDGER_PATH", str(p),
        )
        ledger = PromotionLedger(path=p, autosave=False)
        ledger.load()
        assert ledger.is_promoted("flaky-model") is False, (
            "Slice 10B-iii overrode demoted_from_bg — empirical "
            "post-promotion failure record was silently discarded"
        )


def test_spine_existing_trusted_seed_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a model already exists with origin=trusted_seed + promoted=True,
    re-running the seed is idempotent (no error, no state change)."""
    from backend.core.ouroboros.governance.dw_promotion_ledger import (
        PromotionLedger, QUARANTINE_TRUSTED_SEED,
    )
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "ledger.json"
        _write_disk_record(p, "already-trusted", QUARANTINE_TRUSTED_SEED,
                           promoted=True)
        monkeypatch.setenv("JARVIS_DW_TRUSTED_MODELS", "already-trusted")
        monkeypatch.setenv(
            "JARVIS_DW_PROMOTION_LEDGER_PATH", str(p),
        )
        ledger = PromotionLedger(path=p, autosave=False)
        ledger.load()
        assert ledger.is_promoted("already-trusted") is True
        # Idempotent — no exception, no state change


def test_spine_v12_full_fleet_scenario_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The EXACT v12 scenario reproduced end-to-end:
      - 3 models on disk as ambiguous_metadata (Qwen 397B, 35B, Kimi)
      - Env names all 4 fleet models PLUS 1 operator-demoted model
    Post-10B-iii expected: all 4 fleet models promoted; banned skipped."""
    from backend.core.ouroboros.governance.dw_promotion_ledger import (
        PromotionLedger, LEDGER_SCHEMA_VERSION,
        QUARANTINE_AMBIGUOUS_METADATA, QUARANTINE_OPERATOR_DEMOTED,
    )
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "ledger.json"
        # Simulate the actual v12 disk state
        p.write_text(json.dumps({
            "schema_version": LEDGER_SCHEMA_VERSION,
            "records": [
                {"model_id": "Qwen/Qwen3.5-397B-A17B-FP8",
                 "quarantine_origin": QUARANTINE_AMBIGUOUS_METADATA,
                 "success_latencies_ms": [], "failure_count": 0,
                 "promoted": False, "promoted_at_unix": None,
                 "last_event_unix": 0.0},
                {"model_id": "Qwen/Qwen3.5-35B-A3B-FP8",
                 "quarantine_origin": QUARANTINE_AMBIGUOUS_METADATA,
                 "success_latencies_ms": [], "failure_count": 0,
                 "promoted": False, "promoted_at_unix": None,
                 "last_event_unix": 0.0},
                {"model_id": "moonshotai/Kimi-K2.6",
                 "quarantine_origin": QUARANTINE_AMBIGUOUS_METADATA,
                 "success_latencies_ms": [], "failure_count": 0,
                 "promoted": False, "promoted_at_unix": None,
                 "last_event_unix": 0.0},
                {"model_id": "operator-banned",
                 "quarantine_origin": QUARANTINE_OPERATOR_DEMOTED,
                 "success_latencies_ms": [], "failure_count": 0,
                 "promoted": False, "promoted_at_unix": None,
                 "last_event_unix": 0.0},
            ],
        }))
        # Env: all 4 fleet PLUS the banned one (test that banned still loses)
        monkeypatch.setenv(
            "JARVIS_DW_TRUSTED_MODELS",
            "Qwen/Qwen3.5-397B-A17B-FP8,"
            "Qwen/Qwen3.5-35B-A3B-FP8,"
            "Qwen/Qwen3.5-4B,"
            "moonshotai/Kimi-K2.6,"
            "operator-banned"
        )
        monkeypatch.setenv(
            "JARVIS_DW_PROMOTION_LEDGER_PATH", str(p),
        )
        ledger = PromotionLedger(path=p, autosave=False)
        ledger.load()
        promoted = set(ledger.promoted_models())
        # All 4 fleet models should be promoted
        for fleet_model in (
            "Qwen/Qwen3.5-397B-A17B-FP8",
            "Qwen/Qwen3.5-35B-A3B-FP8",
            "Qwen/Qwen3.5-4B",
            "moonshotai/Kimi-K2.6",
        ):
            assert fleet_model in promoted, (
                f"v12 scenario: {fleet_model} NOT promoted — fleet "
                f"expansion broken; promoted set = {promoted}"
            )
        # Operator decision preserved
        assert "operator-banned" not in promoted, (
            "Operator demotion was overridden — discipline broken"
        )


def test_spine_override_clears_stale_latency_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When overriding an ambiguous_metadata quarantine, the prior
    auto-quarantine never ran the model so historical latencies are
    inapplicable. The override path must clear them."""
    from backend.core.ouroboros.governance.dw_promotion_ledger import (
        PromotionLedger, LEDGER_SCHEMA_VERSION,
        QUARANTINE_AMBIGUOUS_METADATA,
    )
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "ledger.json"
        # Plant a quarantined record with stale latencies
        p.write_text(json.dumps({
            "schema_version": LEDGER_SCHEMA_VERSION,
            "records": [{
                "model_id": "test-model",
                "quarantine_origin": QUARANTINE_AMBIGUOUS_METADATA,
                "success_latencies_ms": [500, 600, 700],  # stale
                "failure_count": 99,  # stale
                "promoted": False,
                "promoted_at_unix": None,
                "last_event_unix": 0.0,
            }],
        }))
        monkeypatch.setenv("JARVIS_DW_TRUSTED_MODELS", "test-model")
        monkeypatch.setenv(
            "JARVIS_DW_PROMOTION_LEDGER_PATH", str(p),
        )
        ledger = PromotionLedger(path=p, autosave=False)
        ledger.load()
        # Inspect the in-memory record post-override
        rec = ledger._records["test-model"]
        assert rec.promoted is True
        assert rec.quarantine_origin == "trusted_seed"
        assert rec.success_latencies_ms == [], (
            "Stale latency history NOT cleared on override — "
            "promotion gate may behave incorrectly with phantom data"
        )
        assert rec.failure_count == 0, (
            "Stale failure_count NOT cleared on override"
        )
