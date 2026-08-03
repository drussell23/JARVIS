"""Regression spine for the soak breaker's self-amplifying spend ledger.

WHAT HAPPENED
---------------
`SoakCircuitBreaker._durable_append` wrote the total it had just computed into
`SpendEntry.actual_cost_usd`, on the same WAL that `reconcile_on_boot` sums
across every row. So the reader was an input to itself, and a self-referential
sum does not drift — it doubles. Measured on the real ledger, seventeen
consecutive boots:

    3.63 → 7.27 → 14.53 → 29.07 → … → 119055.97 → 238111.95

A true spend of $3.63 was presented to a $2 cap as $238,111.95, every LLM
dispatch in the process was refused against it, and the attended HUD answered
a spoken command by reading the refusal string aloud.

The test that matters is `test_a_trip_does_not_raise_the_baseline_it_measured`:
it fails on the old code at the FIRST doubling, not the seventeenth.
"""
from __future__ import annotations

import json
import time

import pytest

from backend.core.ouroboros.aegis.spend_wal import (
    SpendEntry, SpendEntryKind, append_entry_sync,
)
from backend.core.ouroboros.governance.soak_circuit_breaker import (
    SoakBreakerConfig, SoakCircuitBreaker, _WAL_SELF_ROUTE, role_is_attended,
)


@pytest.fixture()
def wal(tmp_path, monkeypatch):
    """An isolated WAL. NEVER the repo's real ledger."""
    p = tmp_path / "spend.jsonl"
    monkeypatch.setenv("JARVIS_AEGIS_WAL_PATH", str(p))
    return p


def _spend(wal, usd, *, route="standard", op="op"):
    append_entry_sync(wal, SpendEntry(
        kind=SpendEntryKind.ADMIT, ts=time.time(), op_id=op,
        route=route, actual_cost_usd=usd))


# ── The amplification loop ──────────────────────────────────────────────────

def test_a_trip_does_not_raise_the_baseline_it_measured(wal):
    """The whole defect, in one assertion.

    Observe a total, write the observation down, observe again. The second
    number must equal the first. On the old code it was exactly double.
    """
    _spend(wal, 3.6333)
    b = SoakCircuitBreaker()

    first = b._replay_committed_spend()
    assert first == pytest.approx(3.6333)

    b._durable_append("soak_trip", {"cost_used_usd": first,
                                    "event": "soak_circuit_tripped"})
    second = b._replay_committed_spend()

    assert second == pytest.approx(first), (
        f"the breaker's own trip row was summed as spend: "
        f"{first} became {second}")


def test_seventeen_boots_do_not_move_the_number(wal):
    """The observed shape: 2**17 growth. Bounded now by construction."""
    _spend(wal, 3.6333)
    b = SoakCircuitBreaker()
    for _ in range(17):
        total = b._replay_committed_spend()
        b._durable_append("soak_trip", {"cost_used_usd": total})
    assert b._replay_committed_spend() == pytest.approx(3.6333)


def test_the_summary_row_carries_no_spend_but_keeps_the_audit(wal):
    """Both halves of the fix, separately.

    The total must still be reconstructable — the row exists for post-hoc
    audit — it just must not live in the field that means "money this row
    spent".
    """
    SoakCircuitBreaker()._durable_append(
        "soak_trip", {"cost_used_usd": 41.5, "event": "soak_circuit_tripped"})
    row = json.loads(wal.read_text().strip().splitlines()[-1])
    assert row["actual_cost_usd"] is None
    assert row["route"] == _WAL_SELF_ROUTE
    assert "41.5" in row["detail"]          # audit intact


def test_the_reader_refuses_self_rows_even_if_a_writer_regresses(wal):
    """Belt and braces. If some future edit puts a total back in the spend
    field, the replay must still not sum it — one guard on a self-referential
    loop is one edit away from being none."""
    append_entry_sync(wal, SpendEntry(
        kind=SpendEntryKind.RECONCILE, ts=time.time(),
        op_id=f"{_WAL_SELF_ROUTE}:soak_trip", route=_WAL_SELF_ROUTE,
        actual_cost_usd=999999.0, detail="a regressed writer"))
    _spend(wal, 1.25)
    assert SoakCircuitBreaker()._replay_committed_spend() == pytest.approx(1.25)


def test_a_genuine_reconcile_row_is_still_counted(wal):
    """The exclusion is by ROUTE, not by kind. RECONCILE rows from real lease
    settlement are spend and must keep counting — 54 of the 71 on the real
    ledger were exactly that."""
    append_entry_sync(wal, SpendEntry(
        kind=SpendEntryKind.RECONCILE, ts=time.time(), op_id="lease-42",
        route="standard", actual_cost_usd=2.5))
    assert SoakCircuitBreaker()._replay_committed_spend() == pytest.approx(2.5)


# ── The horizon ─────────────────────────────────────────────────────────────

def test_spend_from_a_previous_episode_is_not_this_episode_s(wal, monkeypatch):
    monkeypatch.setenv("JARVIS_SOAK_BASELINE_HORIZON_S", "3600")
    now = time.time()
    append_entry_sync(wal, SpendEntry(
        kind=SpendEntryKind.ADMIT, ts=now - 86400 * 14, op_id="ancient",
        route="standard", actual_cost_usd=50.0))
    _spend(wal, 0.75)
    assert SoakCircuitBreaker()._replay_committed_spend(now) == pytest.approx(0.75)


def test_the_horizon_can_be_switched_off(wal, monkeypatch):
    monkeypatch.setenv("JARVIS_SOAK_BASELINE_HORIZON_S", "0")
    now = time.time()
    append_entry_sync(wal, SpendEntry(
        kind=SpendEntryKind.ADMIT, ts=now - 86400 * 14, op_id="ancient",
        route="standard", actual_cost_usd=50.0))
    assert SoakCircuitBreaker()._replay_committed_spend(now) == pytest.approx(50.0)


def test_the_horizon_is_derived_from_declared_episode_length(monkeypatch):
    monkeypatch.delenv("JARVIS_SOAK_BASELINE_HORIZON_S", raising=False)
    monkeypatch.setenv("OUROBOROS_BATTLE_MAX_WALL_SECONDS", "9000")
    assert SoakCircuitBreaker.baseline_horizon_s() == 9000.0
    monkeypatch.delenv("OUROBOROS_BATTLE_MAX_WALL_SECONDS")
    monkeypatch.setenv("JARVIS_SOAK_MAX_GCE_RUNTIME_S", "60")
    assert SoakCircuitBreaker.baseline_horizon_s() == 3600.0   # floored


def test_an_in_flight_lease_still_counts_fail_closed(wal):
    """Unchanged behaviour, pinned: a lease we cannot prove settled is a lease
    we must assume spent."""
    append_entry_sync(wal, SpendEntry(
        kind=SpendEntryKind.ADMIT, ts=time.time(), op_id="inflight",
        route="standard", reserve_cost_usd=0.30))
    assert SoakCircuitBreaker()._replay_committed_spend() == pytest.approx(0.30)


# ── Role scoping ────────────────────────────────────────────────────────────

def test_silence_means_unattended_and_fully_armed(monkeypatch):
    """The direction where being wrong costs real money."""
    monkeypatch.delenv("JARVIS_PROCESS_ROLE", raising=False)
    monkeypatch.setenv("JARVIS_SOAK_CIRCUIT_BREAKER_ENABLED", "true")
    assert not role_is_attended()
    assert SoakBreakerConfig.from_env().enabled


def test_a_soak_that_declares_itself_stays_armed(monkeypatch):
    monkeypatch.setenv("JARVIS_PROCESS_ROLE", "soak")
    monkeypatch.setenv("JARVIS_SOAK_CIRCUIT_BREAKER_ENABLED", "true")
    assert SoakBreakerConfig.from_env().enabled


def test_the_attended_hud_is_not_governed_by_a_soak_cap(monkeypatch):
    """`.env` arms this for every process in the repo. The HUD has an operator
    at the keyboard, which is the exact condition the breaker's own purpose
    statement excludes."""
    monkeypatch.setenv("JARVIS_PROCESS_ROLE", "hud")
    monkeypatch.setenv("JARVIS_SOAK_CIRCUIT_BREAKER_ENABLED", "true")
    assert role_is_attended()
    assert not SoakBreakerConfig.from_env().enabled


def test_an_unknown_role_is_treated_as_unattended(monkeypatch):
    monkeypatch.setenv("JARVIS_PROCESS_ROLE", "something-nobody-defined")
    monkeypatch.setenv("JARVIS_SOAK_CIRCUIT_BREAKER_ENABLED", "true")
    assert SoakBreakerConfig.from_env().enabled
