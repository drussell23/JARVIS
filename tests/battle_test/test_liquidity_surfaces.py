"""Item #5 — circadian liquidity operator surfaces.

Pins the ``/liquidity`` REPL verb + the status-line liquidity segment,
both pure composition over the ProviderLiquidityLedger the Aegis proxy
hydrates. Restraint contract: the segment renders ONLY when a runway is
dry — the healthy state is invisible — EXCEPT that a dry runway also
surfaces on the idle breadcrumb (the organism may be idle BECAUSE it is
dry).
"""
from __future__ import annotations

import json
import time

import pytest

from backend.core.ouroboros.battle_test.status_line import (
    StatusLineBuilder,
    _format_plain,
)
from backend.core.ouroboros.governance import provider_liquidity_ledger as pl


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "liq.json"
    monkeypatch.setenv("JARVIS_PROVIDER_LIQUIDITY_PATH", str(path))
    pl._reset_for_tests()

    def write(tokens: int, reset_s: float = 300.0, provider="anthropic"):
        path.write_text(json.dumps({
            "schema_version": pl.PROVIDER_LIQUIDITY_SCHEMA_VERSION,
            "providers": {provider: {
                "tokens_remaining": tokens,
                "reset_delta_s": reset_s,
                "recorded_unix": time.time(),
                "last_status": 200,
            }},
        }))
        pl._reset_for_tests()

    yield write
    pl._reset_for_tests()


# ---------------------------------------------------------------------------
# Status-line segment
# ---------------------------------------------------------------------------


def test_segment_renders_when_dry(ledger):
    ledger(tokens=500)
    snap = StatusLineBuilder().snapshot()
    assert snap.liquidity_exhausted is True
    assert snap.liquidity_provider == "anthropic"
    assert "⚠ anthropic dry" in _format_plain(snap, compact=False)


def test_segment_silent_when_healthy(ledger):
    ledger(tokens=9_000_000)
    snap = StatusLineBuilder().snapshot()
    assert snap.liquidity_exhausted is False
    assert "⚠" not in _format_plain(snap, compact=False)


def test_segment_surfaces_on_idle_breadcrumb(ledger):
    ledger(tokens=100)
    snap = StatusLineBuilder().snapshot()
    line = _format_plain(snap, compact=False)
    # Whatever restraint mode renders, the dry token must be present.
    assert "dry" in line


def test_segment_master_off(ledger, monkeypatch):
    ledger(tokens=100)
    monkeypatch.setenv("JARVIS_STATUS_LIQUIDITY_SEGMENT_ENABLED", "0")
    snap = StatusLineBuilder().snapshot()
    assert snap.liquidity_exhausted is False
    assert "⚠" not in _format_plain(snap, compact=False)


def test_segment_reset_minutes_render(ledger):
    ledger(tokens=100, reset_s=600.0)
    snap = StatusLineBuilder().snapshot()
    line = _format_plain(snap, compact=False)
    assert "~" in line and "m" in line


# ---------------------------------------------------------------------------
# /liquidity verb (unbound-method probe — no harness boot)
# ---------------------------------------------------------------------------


class _FakeHarness:
    def __init__(self) -> None:
        self.lines = []

    def _repl_print(self, msg: str) -> None:
        self.lines.append(msg)


def _run_verb() -> list:
    from backend.core.ouroboros.battle_test.harness import BattleTestHarness
    h = _FakeHarness()
    BattleTestHarness._repl_cmd_liquidity(h)
    return h.lines


def test_verb_renders_provider_rows(ledger):
    ledger(tokens=12_000_000)
    lines = _run_verb()
    joined = "\n".join(lines)
    assert "anthropic" in joined
    assert "12,000,000 tokens" in joined
    assert "runway: ok" in joined
    assert "exhausted: no" in joined


def test_verb_marks_dry_runway(ledger):
    ledger(tokens=100)
    lines = _run_verb()
    joined = "\n".join(lines)
    assert "runway: DRY" in joined
    assert "exhausted: yes" in joined


def test_verb_survives_empty_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PROVIDER_LIQUIDITY_PATH", str(tmp_path / "absent.json"),
    )
    pl._reset_for_tests()
    lines = _run_verb()
    assert any("no telemetry recorded yet" in ln for ln in lines)


def test_verb_wired_into_repl_dispatch():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    src = (root / "backend/core/ouroboros/battle_test/harness.py").read_text()
    assert '"/liquidity"' in src
    assert "_repl_cmd_liquidity" in src
