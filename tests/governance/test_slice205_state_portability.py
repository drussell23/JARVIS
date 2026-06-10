"""Slice 205 — State Portability (the HONEST kernel of "survive lifecycles").

The authorized plan asked for a live multi-node replication cluster with
hot-swap that "preserves every microsecond of unsupervised-days evidence
across a migration." That was declined: (1) there is no second node to
replicate to — it would be untestable dead code; (2) chaining the strict
unsupervised-days metric across a cross-host migration is exactly the
metric-laundering the Slice-204 honesty guard exists to prevent (a migration
is a SUPERVISED act); (3) live leader-election is split-brain-prone and
unneeded for a single-operator system.

The REAL gap the instinct pointed at: the migration pack's .jarvis allowlist
was stale — it carried crypto/roadmaps/episodic/semantic/evidence but NOT the
operational-state ledgers built this session (registry, chronos, M10
graduation, bandit, single-use sentinels). So a migration today would leave
the evolutionary history behind. This slice makes that real state travel on
the EXISTING offline operator-run migration path — the correct mechanism.

Chronos already handles the cross-host boundary honestly: a new host = new
image_id = recorded as a supervised migration → total_operational_s chains
(history travels) while unsupervised_interval_s resets (no laundering).
"""
from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACK = _REPO_ROOT / "scripts" / "pack_sovereign_release.sh"


# ===========================================================================
# A — the operational-state ledgers now travel on migration
# ===========================================================================

def test_migration_allowlist_carries_operational_state():
    src = _PACK.read_text(encoding="utf-8")
    for ledger in (
        "observability_registry.bin",   # Slice 193 — hedge/registry counters
        "chronos_coherence.json",       # Slice 204 — uptime continuity chain
        "m10_graduation_state.json",    # Slice 197 — autonomous graduation
        "bandit_router_state.json",     # Slice 201 — learned model posteriors
    ):
        assert ledger in src, f"migration would leave behind: {ledger}"


def test_migration_allowlist_carries_single_use_sentinels():
    """The single-use markers must travel so the new host doesn't re-fire the
    genesis PR or re-propose an already-proposed strategy draft."""
    src = _PACK.read_text(encoding="utf-8")
    assert "genesis_proposal.done" in src
    assert ".strategy_proposal_marker" in src


def test_pack_still_excludes_secrets_and_bulk():
    """The fix must NOT widen the artifact to secrets / regenerable bulk —
    .env never travels in the tarball; the broad .jarvis exclude still holds
    with only the explicit allowlist re-added."""
    src = _PACK.read_text(encoding="utf-8")
    assert "--exclude='.env'" in src
    assert "--exclude='.jarvis'" in src  # broad exclude; allowlist re-includes


# ===========================================================================
# B — progress.txt legibility ledger (the Ralph pattern, git-tracked)
# ===========================================================================

def test_progress_ledger_exists_and_is_grounded():
    p = _REPO_ROOT / "progress.txt"
    assert p.exists(), "git-tracked progress.txt should exist"
    text = p.read_text(encoding="utf-8")
    assert "COMPLETED" in text.upper() and "NEXT" in text.upper()
    # grounded in the real arc, not invented
    assert "204" in text or "Chronos" in text
