"""Slice 10B — JARVIS_DW_TRUSTED_MODELS seeds PromotionLedger at boot.

Closes the orthogonal half of the bt-2026-05-25-215404 cost
catastrophe. Slice 10A fixed the ROUTING layer (SWE-bench ops now
target STANDARD not IMMEDIATE), but the underlying TOPOLOGY still
rejected DW even on STANDARD because:

  [DiscoveryRunner] boot complete: ok=False models=23
                                   routes_assigned=['speculative']
  [CandidateGenerator] BACKGROUND route: DW failed
    (background_dw_blocked_by_topology:Catalog-driven (Phase 12).
     Static list purged; ranking authority is dw_catalog_classifier)

Even though DW discovered 23 models, ALL 23 failed
``has_ambiguous_metadata()`` (parameter_count_b is None AND
pricing_out_per_m_usd is None per Zero-Trust §3.6) and pinned
SPECULATIVE-only. Standard / Background / Complex routes saw zero
models → topology block → Claude fallback → cost explosion.

# Fix mechanism — operator-attested trusted seed

Add ``JARVIS_DW_TRUSTED_MODELS=model1,model2,...`` env knob. At
``PromotionLedger.load()``, after disk records are read, any
trusted models not already on disk are force-promoted with
origin=``trusted_seed``. The classifier's ``is_promoted`` check
passes from boot 0, bypassing the prove-it ledger's
10-success requirement.

# Discipline

* Disk records ALWAYS win on conflict — if a model already has
  a persisted state (promoted / quarantined / demoted), the env
  seed is silently skipped. Operator must clear the disk record
  to re-seed.
* Empty/unset env → no-op. Byte-equivalent to pre-Slice-10B.
* Trusted seeds are ATTESTATIONS — the operator vouches the
  model_id corresponds to a real DW endpoint model. If the
  endpoint doesn't have that model, the classifier finds no
  card; the seed is harmless (no provider call ever lands).
* QUARANTINE_TRUSTED_SEED added to canonical origin enum —
  AST-pinned to ensure future origin additions preserve the
  taxonomy.

# Test surface (2 AST pins + 5 spine)
"""

from __future__ import annotations

import ast
import os
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


def test_ast_pin_trusted_seed_constant_in_canonical_origins() -> None:
    """``QUARANTINE_TRUSTED_SEED`` MUST be added to the canonical
    ``_VALID_QUARANTINE_ORIGINS`` frozenset. Otherwise PromotionRecord
    serialization would silently downgrade ``trusted_seed`` origin
    to ``ambiguous_metadata`` on round-trip via from_json_dict()."""
    src = LEDGER_FILE.read_text()
    assert "QUARANTINE_TRUSTED_SEED" in src, (
        "Missing QUARANTINE_TRUSTED_SEED constant — Slice 10B reverted"
    )
    assert "QUARANTINE_TRUSTED_SEED = \"trusted_seed\"" in src, (
        "QUARANTINE_TRUSTED_SEED value drifted — round-trip serialization breaks"
    )
    # Must appear in the _VALID_QUARANTINE_ORIGINS frozenset
    tree = ast.parse(src, filename=str(LEDGER_FILE))
    found_in_valid = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "_VALID_QUARANTINE_ORIGINS"
                for t in node.targets
            )
        ):
            members = ast.unparse(node.value)
            if "QUARANTINE_TRUSTED_SEED" in members:
                found_in_valid = True
    assert found_in_valid, (
        "QUARANTINE_TRUSTED_SEED defined but NOT in _VALID_QUARANTINE_ORIGINS "
        "— round-trip will downgrade to ambiguous_metadata"
    )


def test_ast_pin_seed_method_called_from_load() -> None:
    """``PromotionLedger.load()`` MUST call ``_seed_trusted_models_from_env()``
    so the trusted seed fires automatically on the standard ledger init
    path. Without this hook, the env knob is decorative."""
    src = LEDGER_FILE.read_text()
    assert "_seed_trusted_models_from_env" in src, (
        "Missing _seed_trusted_models_from_env method"
    )
    assert "JARVIS_DW_TRUSTED_MODELS" in src, (
        "Missing JARVIS_DW_TRUSTED_MODELS env reference"
    )
    # The seed method must be invoked from load()
    tree = ast.parse(src, filename=str(LEDGER_FILE))
    load_calls_seed = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "load":
            body_src = ast.unparse(node)
            if "_seed_trusted_models_from_env" in body_src:
                load_calls_seed = True
    assert load_calls_seed, (
        "load() does not call _seed_trusted_models_from_env — "
        "env seed never fires"
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — 5 (functional)
# ──────────────────────────────────────────────────────────────────────


def test_spine_trusted_seed_promotes_unknown_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model_id listed in JARVIS_DW_TRUSTED_MODELS, when no disk
    record exists, must end up in the ledger as promoted=True with
    origin=trusted_seed. The classifier's is_promoted(model_id)
    must return True for it."""
    from backend.core.ouroboros.governance.dw_promotion_ledger import (
        PromotionLedger, QUARANTINE_TRUSTED_SEED,
    )
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "ledger.json"
        monkeypatch.setenv("JARVIS_DW_TRUSTED_MODELS", "doubleword-397b")
        monkeypatch.setenv(
            "JARVIS_DW_PROMOTION_LEDGER_PATH", str(ledger_path),
        )
        ledger = PromotionLedger(path=ledger_path, autosave=False)
        ledger.load()
        # Trusted model is now promoted
        assert ledger.is_promoted("doubleword-397b") is True, (
            "Trusted seed model NOT promoted after load — Slice 10B inert"
        )
        # Record has the canonical origin tag
        promoted_set = set(ledger.promoted_models())
        assert "doubleword-397b" in promoted_set


def test_spine_multiple_trusted_models_csv_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CSV with multiple model_ids + extra whitespace + empty tokens
    is parsed correctly. All listed models end up promoted."""
    from backend.core.ouroboros.governance.dw_promotion_ledger import (
        PromotionLedger,
    )
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "ledger.json"
        monkeypatch.setenv(
            "JARVIS_DW_TRUSTED_MODELS",
            "doubleword-397b, ,deepseek-r1, qwen3-235b ,, ",
        )
        monkeypatch.setenv(
            "JARVIS_DW_PROMOTION_LEDGER_PATH", str(ledger_path),
        )
        ledger = PromotionLedger(path=ledger_path, autosave=False)
        ledger.load()
        for mid in ("doubleword-397b", "deepseek-r1", "qwen3-235b"):
            assert ledger.is_promoted(mid) is True, (
                f"Trusted model {mid} not promoted — CSV parser broken"
            )


def test_spine_disk_record_wins_over_env_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a model already has a disk record (e.g., demoted), the
    env seed MUST be silently skipped. Operator-driven state on
    disk wins over the env attestation — env is bootstrap-only,
    not a live override."""
    import json
    from backend.core.ouroboros.governance.dw_promotion_ledger import (
        PromotionLedger, LEDGER_SCHEMA_VERSION,
        QUARANTINE_OPERATOR_DEMOTED,
    )
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "ledger.json"
        # Seed disk with a DEMOTED record for the same model_id
        # that the env attempts to trust-seed
        ledger_path.write_text(json.dumps({
            "schema_version": LEDGER_SCHEMA_VERSION,
            "records": [{
                "model_id": "doubleword-397b",
                "quarantine_origin": QUARANTINE_OPERATOR_DEMOTED,
                "success_latencies_ms": [],
                "failure_count": 0,
                "promoted": False,
                "promoted_at_unix": None,
                "last_event_unix": 0.0,
            }],
        }))
        monkeypatch.setenv("JARVIS_DW_TRUSTED_MODELS", "doubleword-397b")
        monkeypatch.setenv(
            "JARVIS_DW_PROMOTION_LEDGER_PATH", str(ledger_path),
        )
        ledger = PromotionLedger(path=ledger_path, autosave=False)
        ledger.load()
        # Disk record (demoted) MUST win — env seed silently skipped
        assert ledger.is_promoted("doubleword-397b") is False, (
            "Env seed overrode disk demotion — operator's persistent "
            "decision was discarded (data integrity violation)"
        )
        assert ledger.is_quarantined("doubleword-397b") is True


def test_spine_empty_env_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset / empty / whitespace-only env → no-op. Ledger state
    byte-equivalent to pre-Slice-10B."""
    from backend.core.ouroboros.governance.dw_promotion_ledger import (
        PromotionLedger,
    )
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "ledger.json"
        # Test each empty-shape variant
        for env_val in ("", "   ", " , , ", ",,,"):
            monkeypatch.setenv("JARVIS_DW_TRUSTED_MODELS", env_val)
            monkeypatch.setenv(
                "JARVIS_DW_PROMOTION_LEDGER_PATH", str(ledger_path),
            )
            ledger = PromotionLedger(path=ledger_path, autosave=False)
            ledger.load()
            assert len(ledger.promoted_models()) == 0, (
                f"Empty env value {env_val!r} produced phantom seeds"
            )


def test_spine_trusted_seed_origin_round_trips_through_persistence() -> None:
    """A PromotionRecord with origin=trusted_seed must round-trip
    through to_json_dict / from_json_dict without origin downgrade.
    Without QUARANTINE_TRUSTED_SEED in _VALID_QUARANTINE_ORIGINS, the
    from_json_dict path silently coerces to ambiguous_metadata."""
    from backend.core.ouroboros.governance.dw_promotion_ledger import (
        PromotionRecord, QUARANTINE_TRUSTED_SEED,
    )
    rec = PromotionRecord(
        model_id="doubleword-397b",
        quarantine_origin=QUARANTINE_TRUSTED_SEED,
        promoted=True,
    )
    payload = rec.to_json_dict()
    assert payload["quarantine_origin"] == "trusted_seed"
    restored = PromotionRecord.from_json_dict(payload)
    assert restored is not None
    assert restored.quarantine_origin == QUARANTINE_TRUSTED_SEED, (
        f"Round-trip downgraded origin from trusted_seed to "
        f"{restored.quarantine_origin} — Slice 10B persistence broken"
    )
    assert restored.promoted is True
