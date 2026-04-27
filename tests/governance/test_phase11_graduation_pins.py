"""Slice 11.7 — Phase 11 graduation pin suite.

Pins the graduated state of the Merkle Cartographer + Phase 11.6.{a,b,c,d}
sensor consumers. Five flags flip together (master + 3 merkle consumers
+ 1 file-stat consumer):

  1. JARVIS_MERKLE_CARTOGRAPHER_ENABLED   — master
  2. JARVIS_TODO_USE_MERKLE               — TodoScannerSensor (11.6.a)
  3. JARVIS_DOCSTALE_USE_MERKLE           — DocStalenessSensor (11.6.b)
  4. JARVIS_OPPMINER_USE_MERKLE           — OpportunityMinerSensor (11.6.c)
  5. JARVIS_BACKLOG_SHORT_CIRCUIT_ENABLED — BacklogSensor (11.6.d, file-stat)

Pin sections:
  §1 All five defaults are True (delenv → True)
  §2 Empty-string env reads as default-True (the unset marker)
  §3 Each ``"false"``-class override returns False (hot-revert path)
  §4 Full-revert matrix — flipping each flag off independently flips
     ONLY that one flag (no cross-coupling)
  §5 Master-off legacy invariant — when the cartographer master is off,
     all three merkle consumers fail-safe to legacy (the per-sensor flag
     state doesn't matter; subtree_hash returns "" → fail-safe)
  §6 Backlog stat consumer is independent of the cartographer master —
     it never consults cartographer (architectural divergence pinned in
     11.6.d)
  §7 Module-level public API surface — graduation must not have changed
     the function signatures the orchestrator and tests rely on
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import (
    merkle_cartographer as mc,
)
from backend.core.ouroboros.governance.intake.sensors import (
    backlog_sensor as bls,
)
from backend.core.ouroboros.governance.intake.sensors import (
    doc_staleness_sensor as dss,
)
from backend.core.ouroboros.governance.intake.sensors import (
    opportunity_miner_sensor as oms,
)
from backend.core.ouroboros.governance.intake.sensors import (
    todo_scanner_sensor as tss,
)


# Centralized list of all 5 graduated flags + their reader callable.
_GRADUATED_FLAGS = [
    ("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", mc.is_cartographer_enabled),
    ("JARVIS_TODO_USE_MERKLE", tss.merkle_consult_enabled),
    ("JARVIS_DOCSTALE_USE_MERKLE", dss.merkle_consult_enabled),
    ("JARVIS_OPPMINER_USE_MERKLE", oms.merkle_consult_enabled),
    ("JARVIS_BACKLOG_SHORT_CIRCUIT_ENABLED", bls.short_circuit_enabled),
]


# ===========================================================================
# §1 — All five defaults are True
# ===========================================================================


@pytest.mark.parametrize("env_name,reader", _GRADUATED_FLAGS)
def test_default_is_true_when_env_unset(
    env_name: str, reader, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """delenv → reader returns True. Pinned per-flag so a regression
    on any single flag fails its own test, not the whole batch."""
    monkeypatch.delenv(env_name, raising=False)
    assert reader() is True, (
        f"Slice 11.7 graduation: {env_name} must default True"
    )


# ===========================================================================
# §2 — Empty string is the unset marker
# ===========================================================================


@pytest.mark.parametrize("env_name,reader", _GRADUATED_FLAGS)
def test_empty_string_reads_as_default_true(
    env_name: str, reader, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit ``setenv("", "")`` must behave the same as delenv —
    operators commonly clear flags by exporting them empty in shells."""
    monkeypatch.setenv(env_name, "")
    assert reader() is True


# ===========================================================================
# §3 — Hot-revert: each false-class string returns False
# ===========================================================================


@pytest.mark.parametrize("env_name,reader", _GRADUATED_FLAGS)
@pytest.mark.parametrize("falsy", ["false", "0", "no", "off", "FALSE"])
def test_false_class_string_reverts(
    env_name: str, reader, falsy: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hot-revert path — operator sets the flag to a recognized
    false-class string, reader flips off."""
    monkeypatch.setenv(env_name, falsy)
    assert reader() is False, (
        f"{env_name}={falsy!r} should disable the feature"
    )


# ===========================================================================
# §4 — Full-revert matrix: each flag flips independently
# ===========================================================================


def test_full_revert_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flip each flag off in turn; verify ONLY that one flag flips
    while the other four stay True. Catches accidental cross-coupling
    where one flag's reader peeks at another's state."""
    for env_off, reader_off in _GRADUATED_FLAGS:
        # Reset all to default (delenv) — graduated state is True
        for env_name, _ in _GRADUATED_FLAGS:
            monkeypatch.delenv(env_name, raising=False)
        # Flip exactly one off
        monkeypatch.setenv(env_off, "false")
        # Verify the flipped one is off, the rest still True
        for env_name, reader in _GRADUATED_FLAGS:
            expected = (env_name != env_off)
            actual = reader()
            assert actual is expected, (
                f"After flipping {env_off}=false, {env_name} reader "
                f"returned {actual} (expected {expected})"
            )


# ===========================================================================
# §5 — Master-off legacy invariant
# ===========================================================================


def test_master_off_disables_root_hash(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """When the cartographer master is off, ``current_root_hash`` and
    ``subtree_hash`` return empty string — the fail-safe sentinel that
    forces every per-sensor consumer to fall through to legacy.

    This is the single guarantee that keeps the per-sensor flags
    semantically dependent on the master flag without requiring the
    sensors to read the master flag themselves."""
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "false")
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(tmp_path))
    mc.reset_default_cartographer_for_tests()
    try:
        c = mc.MerkleCartographer(repo_root=tmp_path)
        assert c.current_root_hash() == ""
        assert c.subtree_hash("backend") == ""
    finally:
        mc.reset_default_cartographer_for_tests()


# ===========================================================================
# §6 — Backlog stat consumer is master-flag-independent
# ===========================================================================


def test_backlog_stat_consumer_independent_of_cartographer_master(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BacklogSensor uses file-stat, not cartographer (architectural
    divergence pinned in 11.6.d). Flipping the cartographer master OFF
    must NOT disable the backlog stat consumer — the two are
    independent layers, not stacked."""
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "false")
    monkeypatch.delenv("JARVIS_BACKLOG_SHORT_CIRCUIT_ENABLED", raising=False)
    assert mc.is_cartographer_enabled() is False
    assert bls.short_circuit_enabled() is True


# ===========================================================================
# §7 — Module-level public API surface
# ===========================================================================


def test_module_level_readers_callable_with_no_args() -> None:
    """Each reader is a zero-arg callable returning bool. Pins the
    contract that the orchestrator and observability surfaces rely
    on for read-time flag introspection (no captured-at-init values
    that go stale on hot-revert)."""
    for _, reader in _GRADUATED_FLAGS:
        result = reader()
        assert isinstance(result, bool)


def test_master_default_docstring_references_graduation() -> None:
    """The master-flag docstring must call out the Slice 11.7
    graduation flip — operator-facing documentation is the surface
    that explains why the default changed."""
    assert mc.is_cartographer_enabled.__doc__ is not None
    assert "Slice 11.7" in mc.is_cartographer_enabled.__doc__
    assert "true" in mc.is_cartographer_enabled.__doc__.lower()


@pytest.mark.parametrize("env_name,reader", _GRADUATED_FLAGS[1:])
def test_consumer_default_docstring_references_graduation(
    env_name: str, reader,
) -> None:
    """Each per-sensor consumer reader's docstring must reference the
    graduation. Ensures hot-revert instructions stay co-located with
    the reader so operators discovering the flag via ``help()`` see
    the rollback path."""
    doc = reader.__doc__ or ""
    assert "Slice 11.7" in doc, (
        f"{reader.__qualname__} docstring should reference Slice 11.7 "
        "graduation + hot-revert path"
    )
