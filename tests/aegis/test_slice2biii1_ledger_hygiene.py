"""Slice 2B-iii.1 — Aegis battle-test ledger hygiene.

Closes the gap surfaced by the re-detonation soak bt-2026-05-24-225714:

  fallback_err_msg=lease_denied:_reason=cost_ceiling_exceeded
    _detail='session_cap_exceeded'

The Aegis daemon's budget state machine REPLAYED a stale
``.jarvis/aegis/spend.jsonl`` WAL from earlier soaks (the daemon's
``stale_lock_detected`` warning at boot showed the lock file was
2226s old). The lease was denied at the very first request because
the daemon thought it was already over its session cap.

Operator binding: "Do not simply loosen the default session_cap_usd.
The ceiling should remain strict. Implement ledger hygiene." So the
fix is to give EVERY battle-test session a clean financial slate
WITHOUT touching the cap configuration.

# Fix

* New module ``backend/core/ouroboros/aegis/ledger_hygiene.py``:
  pure function ``rotate_aegis_wal_for_battle_test(*, session_tag,
  max_backups=10)`` that:
  - Rotates ``wal_path()`` → ``{wal_path}.bak-{session_tag}``
    (preserves prior session ledgers — auditable, not deleted)
  - Removes the ``{wal_path}.lock`` companion (process-coordination
    artifact, no data)
  - Prunes oldest ``.bak-*`` files past ``max_backups`` cap
    (prevents unbounded disk growth)
  - Idempotent: missing files are no-op (not an error)
  - NEVER raises into the caller — logs + returns a structured
    HygieneResult so the harness can decide whether to abort
* Wired into ``scripts/ouroboros_battle_test.py`` BEFORE
  ``aegis_preflight()`` runs, gated by
  ``JARVIS_AEGIS_BATTLE_TEST_HYGIENE_ENABLED`` (default-TRUE) so
  operators can opt out (e.g., for soak-continuity debugging).
* Production Aegis use (non-battle-test) NEVER touches the WAL —
  the hygiene helper is ONLY invoked from the battle-test harness.

# Why rotate, not delete?

Rotation preserves forensic continuity: if a prior soak surfaced
something interesting in its WAL, the operator can still read
``.jarvis/aegis/spend.jsonl.bak-bt-2026-05-24-222008`` after the
next soak starts. Deletion would destroy that audit trail.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

import pytest

from backend.core.ouroboros.aegis import ledger_hygiene


# ──────────────────────────────────────────────────────────────────────
# Spine #1 — rotation moves WAL to .bak-{tag}, leaves clean slate
# ──────────────────────────────────────────────────────────────────────

def test_rotate_aegis_wal_for_battle_test_renames_to_bak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The original WAL file is moved to ``<wal>.bak-<tag>`` and
    the path returned by ``wal_path()`` is left absent (so the
    daemon boots with an empty slate)."""
    wal = tmp_path / "spend.jsonl"
    wal.write_text('{"entry": "stale-from-prior-soak"}\n')
    monkeypatch.setenv("JARVIS_AEGIS_WAL_PATH", str(wal))

    result = ledger_hygiene.rotate_aegis_wal_for_battle_test(
        session_tag="bt-2026-05-24-test",
    )

    assert result.ok is True
    assert wal.exists() is False, "WAL not removed from canonical path"
    backup = wal.with_name("spend.jsonl.bak-bt-2026-05-24-test")
    assert backup.exists() is True, f"backup file missing at {backup}"
    assert backup.read_text() == '{"entry": "stale-from-prior-soak"}\n'


# ──────────────────────────────────────────────────────────────────────
# Spine #2 — lock file is removed
# ──────────────────────────────────────────────────────────────────────

def test_rotate_aegis_wal_for_battle_test_removes_lock_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``.lock`` companion file (process-coordination artifact)
    is removed — it has no audit value, and a stale lock causes the
    daemon to log ``[CrossProcessJSONL] stale_lock_detected`` at boot."""
    wal = tmp_path / "spend.jsonl"
    wal.write_text("")
    lock = wal.with_suffix(".jsonl.lock")
    lock.write_text("")
    monkeypatch.setenv("JARVIS_AEGIS_WAL_PATH", str(wal))

    result = ledger_hygiene.rotate_aegis_wal_for_battle_test(
        session_tag="bt-test",
    )

    assert result.ok is True
    assert lock.exists() is False, f"lock file not removed: {lock}"


# ──────────────────────────────────────────────────────────────────────
# Spine #3 — missing files are no-op (idempotent)
# ──────────────────────────────────────────────────────────────────────

def test_rotate_aegis_wal_for_battle_test_idempotent_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First boot of a fresh repo has no WAL + no lock. The helper
    must succeed cleanly (no exception, ok=True) so the harness can
    invoke it unconditionally."""
    wal = tmp_path / "no-such-spend.jsonl"
    monkeypatch.setenv("JARVIS_AEGIS_WAL_PATH", str(wal))
    assert not wal.exists()

    result = ledger_hygiene.rotate_aegis_wal_for_battle_test(
        session_tag="fresh-boot",
    )

    assert result.ok is True
    # No rotation occurred (nothing to rotate); no backup file created.
    assert not list(wal.parent.glob("*.bak-*"))


# ──────────────────────────────────────────────────────────────────────
# Spine #4 — backup pruning cap (oldest .bak-* files dropped)
# ──────────────────────────────────────────────────────────────────────

def test_rotate_aegis_wal_for_battle_test_prunes_old_backups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After N+1 rotations with max_backups=N, only the N newest
    backups survive (oldest is dropped). Prevents unbounded disk
    growth across many soak sessions."""
    wal = tmp_path / "spend.jsonl"
    monkeypatch.setenv("JARVIS_AEGIS_WAL_PATH", str(wal))

    # Pre-seed 3 existing backups + a fresh WAL
    import time
    for i, name in enumerate(("sess-A", "sess-B", "sess-C")):
        bak = wal.with_name(f"spend.jsonl.bak-{name}")
        bak.write_text(f"{name}\n")
        # Stagger mtimes so prune-by-oldest is deterministic
        ts = time.time() - (10 - i)
        import os
        os.utime(bak, (ts, ts))
    wal.write_text("current\n")

    # Rotate with cap=2 → after rotation we have current→bak-sess-D
    # PLUS sess-A/B/C from before. cap=2 means keep 2 newest, drop 2.
    result = ledger_hygiene.rotate_aegis_wal_for_battle_test(
        session_tag="sess-D", max_backups=2,
    )

    assert result.ok is True
    surviving = sorted(p.name for p in wal.parent.glob("spend.jsonl.bak-*"))
    assert len(surviving) == 2, (
        f"expected 2 surviving backups, got {len(surviving)}: {surviving}"
    )
    # The 2 newest by mtime should be sess-C (T-7) + sess-D (just now)
    assert "spend.jsonl.bak-sess-D" in surviving, (
        f"newest backup (the just-rotated one) missing: {surviving}"
    )
    assert "spend.jsonl.bak-sess-C" in surviving, (
        f"second-newest backup (sess-C, T-7) missing: {surviving}"
    )
    # Oldest two (sess-A, sess-B) should be gone
    assert "spend.jsonl.bak-sess-A" not in surviving
    assert "spend.jsonl.bak-sess-B" not in surviving


# ──────────────────────────────────────────────────────────────────────
# Spine #5 — never raises into the caller
# ──────────────────────────────────────────────────────────────────────

def test_rotate_aegis_wal_for_battle_test_never_raises_on_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure modes (permission denied, FS errors) are logged but
    return ok=False — they don't crash the harness boot."""
    wal = tmp_path / "spend.jsonl"
    wal.write_text("")
    monkeypatch.setenv("JARVIS_AEGIS_WAL_PATH", str(wal))

    # Patch Path.replace to simulate a permission error
    def boom(*args, **kwargs):
        raise PermissionError("simulated FS denial")

    monkeypatch.setattr(Path, "replace", boom)

    # Helper must not raise — operator wants the harness to keep
    # booting even if hygiene fails (operator can investigate via
    # the structured result + logs).
    result = ledger_hygiene.rotate_aegis_wal_for_battle_test(
        session_tag="boom-test",
    )

    assert result.ok is False
    assert "permission" in result.detail.lower() or "denial" in result.detail.lower()


# ──────────────────────────────────────────────────────────────────────
# Wiring check — harness invokes hygiene helper BEFORE preflight
# ──────────────────────────────────────────────────────────────────────

def test_harness_calls_ledger_hygiene_before_aegis_preflight() -> None:
    """``scripts/ouroboros_battle_test.py`` must invoke
    ``rotate_aegis_wal_for_battle_test`` BEFORE the
    ``aegis_preflight`` call. Source-order matters: a clean WAL
    must be in place before the daemon boots + reads it."""
    repo_root = Path(__file__).resolve().parents[2]
    harness_src = (repo_root / "scripts" / "ouroboros_battle_test.py").read_text()

    hygiene_idx = harness_src.find("rotate_aegis_wal_for_battle_test")
    preflight_idx = harness_src.find("aegis_preflight()")
    assert hygiene_idx > 0, (
        "scripts/ouroboros_battle_test.py does not invoke "
        "rotate_aegis_wal_for_battle_test"
    )
    assert preflight_idx > 0, (
        "aegis_preflight() invocation not found in harness"
    )
    assert hygiene_idx < preflight_idx, (
        f"hygiene helper invoked AFTER aegis_preflight() — "
        f"rotation must happen BEFORE the daemon boots so it "
        f"reads a clean WAL. "
        f"hygiene at char {hygiene_idx}, preflight at char {preflight_idx}"
    )


# ──────────────────────────────────────────────────────────────────────
# Wiring check — hygiene gated by env knob (operator can opt out)
# ──────────────────────────────────────────────────────────────────────

def test_hygiene_can_be_disabled_via_env_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``JARVIS_AEGIS_BATTLE_TEST_HYGIENE_ENABLED=false`` skips
    rotation — for operators who want WAL continuity across runs
    (e.g., debugging a multi-session budget regression)."""
    wal = tmp_path / "spend.jsonl"
    wal.write_text("PRESERVE-ME")
    monkeypatch.setenv("JARVIS_AEGIS_WAL_PATH", str(wal))
    monkeypatch.setenv("JARVIS_AEGIS_BATTLE_TEST_HYGIENE_ENABLED", "false")

    result = ledger_hygiene.rotate_aegis_wal_for_battle_test(
        session_tag="should-not-rotate",
    )

    assert result.ok is True
    assert result.skipped is True
    assert wal.read_text() == "PRESERVE-ME", "WAL was rotated despite hygiene disabled"
