"""Phase 12 Slice G — ModalityLedger regression spine.

Pins:
  §1 Master flag default-off
  §2 Boot from empty / missing file
  §3 record_metadata_verdict — chat & non-chat
  §4 register_unknown — does NOT downgrade existing decision
  §5 record_probe_result — chat (200) + non-chat (4xx + marker)
  §6 record_dispatch_modality_failure — strongest signal, overwrites
  §7 override_verdict — operator manual demote/promote
  §8 reset_for_catalog_refresh — drops stale, preserves operator overrides
  §9 Persistence round-trip via disk
  §10 Corrupt ledger boots empty (NEVER raises)
  §11 Schema mismatch starts empty
  §12 NEVER-raises contract on garbage inputs
  §13 Verdict accessors (chat_capable_models / non_chat_models / unknown)
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any  # noqa: F401

import pytest

from backend.core.ouroboros.governance.dw_modality_ledger import (
    LEDGER_SCHEMA_VERSION,
    ModalityLedger,
    SOURCE_DISPATCH_4XX,
    SOURCE_METADATA,
    SOURCE_OPERATOR,
    SOURCE_PROBE_2XX,
    SOURCE_PROBE_4XX,
    VERDICT_CHAT_CAPABLE,
    VERDICT_NON_CHAT,
    VERDICT_UNKNOWN,
    modality_verification_enabled,
)


@pytest.fixture
def isolated_path(tmp_path: Path,
                  monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "modality.json"
    monkeypatch.setenv("JARVIS_DW_MODALITY_LEDGER_PATH", str(p))
    return p


@pytest.fixture
def make_ledger(isolated_path: Path):
    def _factory(**kwargs) -> ModalityLedger:
        return ModalityLedger(**kwargs)
    return _factory


# §1
def test_master_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_DW_MODALITY_VERIFICATION_ENABLED", raising=False)
    assert modality_verification_enabled() is False


def test_master_flag_truthy_falsy(monkeypatch: pytest.MonkeyPatch) -> None:
    for v in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv("JARVIS_DW_MODALITY_VERIFICATION_ENABLED", v)
        assert modality_verification_enabled() is True
    for v in ("0", "false", "no", "off", "garbage", ""):
        monkeypatch.setenv("JARVIS_DW_MODALITY_VERIFICATION_ENABLED", v)
        assert modality_verification_enabled() is False


# §2
def test_empty_ledger(make_ledger) -> None:
    led = make_ledger()
    led.load()
    assert led.chat_capable_models() == ()
    assert led.non_chat_models() == ()
    assert led.unknown_models() == ()
    assert led.verdict_for("never/seen") == VERDICT_UNKNOWN


# §3
def test_record_metadata_chat_capable(make_ledger) -> None:
    led = make_ledger()
    led.record_metadata_verdict("vendor/m-7B", is_chat_capable=True)
    assert led.is_chat_capable("vendor/m-7B") is True
    assert led.is_non_chat("vendor/m-7B") is False
    snap = led.snapshot("vendor/m-7B")
    assert snap is not None
    assert snap.source == SOURCE_METADATA


def test_record_metadata_non_chat(make_ledger) -> None:
    led = make_ledger()
    led.record_metadata_verdict("vendor/embedding-8B", is_chat_capable=False)
    assert led.is_non_chat("vendor/embedding-8B") is True


# §4
def test_register_unknown_does_not_downgrade_existing_chat_capable(
    make_ledger,
) -> None:
    led = make_ledger()
    led.record_metadata_verdict("v/m-7B", is_chat_capable=True)
    led.register_unknown("v/m-7B")  # should not downgrade
    assert led.is_chat_capable("v/m-7B") is True


def test_register_unknown_does_not_downgrade_existing_non_chat(
    make_ledger,
) -> None:
    led = make_ledger()
    led.record_metadata_verdict("v/embed-8B", is_chat_capable=False)
    led.register_unknown("v/embed-8B")  # should NOT downgrade
    assert led.is_non_chat("v/embed-8B") is True


def test_register_unknown_for_new_model(make_ledger) -> None:
    led = make_ledger()
    led.register_unknown("brand/new-7B")
    assert led.is_unknown("brand/new-7B") is True


# §5
def test_record_probe_result_chat(make_ledger) -> None:
    led = make_ledger()
    led.record_probe_result(
        "v/m-7B",
        is_chat_capable=True,
        response_body_excerpt='{"choices":[...]}',
    )
    snap = led.snapshot("v/m-7B")
    assert snap is not None
    assert snap.verdict == VERDICT_CHAT_CAPABLE
    assert snap.source == SOURCE_PROBE_2XX


def test_record_probe_result_non_chat(make_ledger) -> None:
    led = make_ledger()
    led.record_probe_result(
        "v/embed-8B",
        is_chat_capable=False,
        response_body_excerpt="model does not support chat",
    )
    snap = led.snapshot("v/embed-8B")
    assert snap is not None
    assert snap.verdict == VERDICT_NON_CHAT
    assert snap.source == SOURCE_PROBE_4XX
    assert "does not support chat" in snap.response_body_excerpt


# §6
def test_record_dispatch_modality_failure_overrides_chat_capable(
    make_ledger,
) -> None:
    """Real dispatch 4xx is the strongest signal — even if metadata
    said CHAT_CAPABLE, observed 4xx demotes to NON_CHAT."""
    led = make_ledger()
    led.record_metadata_verdict("v/m-7B", is_chat_capable=True)
    led.record_dispatch_modality_failure(
        "v/m-7B",
        response_body_excerpt="model does not support chat",
    )
    assert led.is_non_chat("v/m-7B") is True
    snap = led.snapshot("v/m-7B")
    assert snap is not None
    assert snap.source == SOURCE_DISPATCH_4XX


# §7
def test_override_verdict_promote(make_ledger) -> None:
    led = make_ledger()
    led.record_metadata_verdict("v/m-7B", is_chat_capable=False)
    changed = led.override_verdict("v/m-7B", verdict=VERDICT_CHAT_CAPABLE)
    assert changed is True
    assert led.is_chat_capable("v/m-7B") is True
    snap = led.snapshot("v/m-7B")
    assert snap is not None
    assert snap.source == SOURCE_OPERATOR


def test_override_verdict_idempotent_returns_false(make_ledger) -> None:
    """Override that matches existing verdict is a no-op — returns
    False, doesn't bump last_event_unix, doesn't change source."""
    led = make_ledger()
    led.record_metadata_verdict("v/m-7B", is_chat_capable=True)
    changed = led.override_verdict("v/m-7B", verdict=VERDICT_CHAT_CAPABLE)
    assert changed is False
    snap = led.snapshot("v/m-7B")
    assert snap is not None
    # Source stays as metadata (no-op preserves prior provenance)
    assert snap.source == SOURCE_METADATA


def test_override_verdict_invalid_returns_false(make_ledger) -> None:
    led = make_ledger()
    assert led.override_verdict("v/m-7B", verdict="NOT_REAL") is False


# §8
def test_reset_for_catalog_refresh_drops_stale(make_ledger) -> None:
    led = make_ledger()
    led.record_metadata_verdict(
        "v/m-7B", is_chat_capable=True,
        catalog_snapshot_id="snapshot-A",
    )
    led.record_metadata_verdict(
        "v/m-30B", is_chat_capable=True,
        catalog_snapshot_id="snapshot-A",
    )
    # Refresh to new snapshot
    dropped = led.reset_for_catalog_refresh("snapshot-B")
    assert dropped == 2
    assert led.verdict_for("v/m-7B") == VERDICT_UNKNOWN
    assert led.verdict_for("v/m-30B") == VERDICT_UNKNOWN


def test_reset_preserves_operator_overrides(make_ledger) -> None:
    """Operator overrides survive catalog refreshes (cross-snapshot)."""
    led = make_ledger()
    led.record_metadata_verdict(
        "v/m-7B", is_chat_capable=True,
        catalog_snapshot_id="snapshot-A",
    )
    led.override_verdict(
        "v/op-overridden", verdict=VERDICT_NON_CHAT,
        catalog_snapshot_id="snapshot-A",
    )
    led.reset_for_catalog_refresh("snapshot-B")
    # Operator override survives
    assert led.verdict_for("v/op-overridden") == VERDICT_NON_CHAT
    # Metadata-source verdict dropped
    assert led.verdict_for("v/m-7B") == VERDICT_UNKNOWN


def test_reset_preserves_empty_snapshot_id_records(make_ledger) -> None:
    """Verdicts pinned to empty snapshot_id (legacy) survive refreshes."""
    led = make_ledger()
    led.record_metadata_verdict(
        "v/legacy", is_chat_capable=True,
        catalog_snapshot_id="",  # empty
    )
    led.reset_for_catalog_refresh("snapshot-B")
    assert led.is_chat_capable("v/legacy") is True


# §9
def test_persistence_roundtrip(isolated_path: Path, make_ledger) -> None:
    led1 = make_ledger()
    led1.record_metadata_verdict(
        "v/chat-7B", is_chat_capable=True,
    )
    led1.record_probe_result(
        "v/embed-8B",
        is_chat_capable=False,
        response_body_excerpt="embedding only",
    )
    led1.register_unknown("v/unknown-3B")
    # Reload
    led2 = make_ledger()
    led2.load()
    assert led2.is_chat_capable("v/chat-7B") is True
    assert led2.is_non_chat("v/embed-8B") is True
    assert led2.is_unknown("v/unknown-3B") is True


def test_autosave_off(isolated_path: Path) -> None:
    led = ModalityLedger(autosave=False)
    led.record_metadata_verdict("v/m-7B", is_chat_capable=True)
    assert not isolated_path.exists()
    led.save()
    assert isolated_path.exists()


# §10
def test_corrupt_ledger_boots_empty(
    isolated_path: Path, make_ledger,
) -> None:
    isolated_path.parent.mkdir(parents=True, exist_ok=True)
    isolated_path.write_text("not valid json", encoding="utf-8")
    led = make_ledger()
    led.load()  # should not raise
    assert led.all_snapshots() == ()


# §11
def test_schema_mismatch_starts_empty(
    isolated_path: Path, make_ledger,
) -> None:
    isolated_path.parent.mkdir(parents=True, exist_ok=True)
    isolated_path.write_text(json.dumps({
        "schema_version": "dw_modality.99",
        "records": [{"model_id": "should-be-ignored"}],
    }), encoding="utf-8")
    led = make_ledger()
    led.load()
    assert led.all_snapshots() == ()


# §12
def test_garbage_inputs_tolerated(make_ledger) -> None:
    led = make_ledger()
    for bad in (None, "", "  ", "\t"):
        led.record_metadata_verdict(bad, is_chat_capable=True)  # type: ignore[arg-type]
        led.register_unknown(bad)  # type: ignore[arg-type]
        led.record_probe_result(bad, is_chat_capable=True)  # type: ignore[arg-type]
        led.record_dispatch_modality_failure(bad)  # type: ignore[arg-type]
        assert led.verdict_for(bad) == VERDICT_UNKNOWN  # type: ignore[arg-type]


# §13
def test_accessors_partition(make_ledger) -> None:
    led = make_ledger()
    led.record_metadata_verdict("a/chat-7B", is_chat_capable=True)
    led.record_metadata_verdict("b/embed-8B", is_chat_capable=False)
    led.register_unknown("c/unknown-3B")
    led.record_metadata_verdict("d/chat-30B", is_chat_capable=True)
    chat = led.chat_capable_models()
    nonchat = led.non_chat_models()
    unk = led.unknown_models()
    assert chat == ("a/chat-7B", "d/chat-30B")
    assert nonchat == ("b/embed-8B",)
    assert unk == ("c/unknown-3B",)
