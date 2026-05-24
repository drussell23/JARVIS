"""Aegis battle-test ledger hygiene (Slice 2B-iii.1).

Closes the ``cost_ceiling_exceeded:session_cap_exceeded`` failure
mode surfaced by re-detonation soak bt-2026-05-24-225714: the Aegis
daemon's ``ImmutableBudgetStateMachine.replay_for_recovery()``
correctly replayed the prior soak's ``.jarvis/aegis/spend.jsonl``
WAL, so the *new* session booted already over its session cap and
denied the very first lease request with HTTP 401.

Operator binding (verbatim from Slice 2B-iii.1 directive):
  "Do not simply loosen the default session_cap_usd. The ceiling
  should remain strict. Implement ledger hygiene. When a new battle
  test session starts via the ouroboros_battle_test.py harness,
  explicitly clear or rotate the .jarvis/aegis/spend.jsonl WAL and
  any associated lock files. Each test session should begin with a
  clean financial slate."

# Architectural placement

This module lives under ``aegis/`` (it knows where the WAL lives) but
is invoked ONLY from ``scripts/ouroboros_battle_test.py``. Production
Aegis use (non-battle-test) NEVER auto-rotates the WAL — that would
destroy the very budget-continuity audit trail Slice 1 was built to
provide. The battle-test harness is the single seam.

# What it does

  * **Rotate** (not delete): ``wal_path()`` → ``<wal>.bak-<session_tag>``
    via ``os.replace`` (atomic on POSIX). Preserves the prior session's
    spend ledger for forensic analysis.
  * **Remove** ``<wal>.lock`` — pure process-coordination artifact
    (cross_process_jsonl flock companion), no audit value, stale
    locks confuse the daemon's stale_lock_detected guard.
  * **Prune** oldest ``<wal>.bak-*`` files past ``max_backups`` cap
    (default 10) — prevents unbounded disk growth across many soaks.
  * **Idempotent**: missing WAL / missing lock are no-ops (not errors)
    — the helper can be invoked unconditionally at boot.
  * **NEVER raises**: returns a structured ``HygieneResult`` even on
    permission errors or FS denials so the harness keeps booting.

# Env gating

``JARVIS_AEGIS_BATTLE_TEST_HYGIENE_ENABLED`` (default **TRUE** when
the module is invoked) — operators can set ``=false`` to skip
rotation for soak-continuity debugging.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from backend.core.ouroboros.aegis.flags import wal_path

logger = logging.getLogger(__name__)


# Env flag — battle-test-only opt-out. Operator default is rotation ON.
ENV_BATTLE_TEST_HYGIENE_ENABLED: str = "JARVIS_AEGIS_BATTLE_TEST_HYGIENE_ENABLED"

# Default cap on retained .bak-* files. Bounded retention prevents
# unbounded disk growth from frequent soak iterations.
DEFAULT_MAX_BACKUPS: int = 10


@dataclass(frozen=True)
class HygieneResult:
    """Outcome of one ``rotate_aegis_wal_for_battle_test()`` invocation.

    Fields are all primitives so the harness can serialize them into
    its boot diagnostics without round-tripping through a custom
    encoder.
    """
    ok: bool
    skipped: bool = False
    rotated_path: Optional[str] = None
    lock_removed: bool = False
    pruned_count: int = 0
    detail: str = ""


def _hygiene_enabled() -> bool:
    """Read the env flag at call-time (not module-import time) so
    monkeypatch-based tests + per-soak operator overrides work."""
    raw = os.environ.get(ENV_BATTLE_TEST_HYGIENE_ENABLED, "true").strip().lower()
    return raw in ("true", "1", "yes", "on")


def _prune_old_backups(wal: Path, max_backups: int) -> int:
    """Drop oldest ``<wal-name>.bak-*`` files past the cap. Returns
    count pruned. ``max_backups <= 0`` keeps everything (no prune).
    Sort by mtime ascending; drop the head."""
    if max_backups <= 0:
        return 0
    pattern = f"{wal.name}.bak-*"
    backups = list(wal.parent.glob(pattern))
    if len(backups) <= max_backups:
        return 0
    # Sort oldest-first by mtime
    backups.sort(key=lambda p: p.stat().st_mtime)
    to_drop = backups[: len(backups) - max_backups]
    pruned = 0
    for p in to_drop:
        try:
            p.unlink()
            pruned += 1
        except OSError as err:
            logger.warning(
                "[LedgerHygiene] prune failed for %s: %r — leaving in place",
                p, err,
            )
    return pruned


def rotate_aegis_wal_for_battle_test(
    *,
    session_tag: str,
    max_backups: int = DEFAULT_MAX_BACKUPS,
) -> HygieneResult:
    """Rotate the Aegis WAL + remove its lock for a clean battle-test
    financial slate.

    Args:
        session_tag: Short identifier for the rotated backup filename
            (e.g., ``"bt-2026-05-24-225714"``). Becomes the ``.bak-``
            suffix.
        max_backups: Cap on retained ``.bak-*`` files. Default 10.

    Returns:
        :class:`HygieneResult`. ``ok=True`` for both successful
        rotation AND skip-when-disabled; ``skipped=True`` flags the
        latter. ``ok=False`` is reserved for genuine failures
        (permission errors, FS denials) that the operator should
        investigate; the harness still keeps booting.

    Never raises. All exceptions are caught, logged, and folded into
    ``HygieneResult(ok=False, detail=...)``.
    """
    if not _hygiene_enabled():
        logger.info(
            "[LedgerHygiene] %s=false — rotation skipped (operator "
            "opted out for soak-continuity debugging)",
            ENV_BATTLE_TEST_HYGIENE_ENABLED,
        )
        return HygieneResult(
            ok=True, skipped=True,
            detail=f"{ENV_BATTLE_TEST_HYGIENE_ENABLED}=false",
        )

    wal = wal_path()
    lock = wal.with_suffix(wal.suffix + ".lock")
    try:
        rotated_path: Optional[str] = None
        if wal.exists():
            backup = wal.with_name(f"{wal.name}.bak-{session_tag}")
            # os.replace is atomic on POSIX; overwrites any prior
            # backup with the same tag (re-runs of the same session-id
            # — rare, but tolerant).
            wal.replace(backup)
            rotated_path = str(backup)
            logger.info(
                "[LedgerHygiene] rotated WAL %s → %s (clean financial "
                "slate for new battle-test session)",
                wal, backup,
            )

        lock_removed = False
        if lock.exists():
            try:
                lock.unlink()
                lock_removed = True
                logger.info(
                    "[LedgerHygiene] removed stale lock %s "
                    "(process-coordination artifact, no audit value)",
                    lock,
                )
            except OSError as err:
                logger.warning(
                    "[LedgerHygiene] lock removal failed for %s: %r "
                    "— daemon's stale_lock_detected guard will handle",
                    lock, err,
                )

        # Ensure the parent dir exists for the next session's WAL writes.
        try:
            wal.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # daemon will surface a clearer error if writes fail

        pruned = _prune_old_backups(wal, max_backups)
        if pruned:
            logger.info(
                "[LedgerHygiene] pruned %d old .bak-* file(s) "
                "(max_backups=%d retained)", pruned, max_backups,
            )

        return HygieneResult(
            ok=True, skipped=False,
            rotated_path=rotated_path, lock_removed=lock_removed,
            pruned_count=pruned,
            detail=f"session_tag={session_tag}",
        )
    except Exception as err:  # noqa: BLE001 — fail-closed surface
        logger.warning(
            "[LedgerHygiene] rotation failed: %r — harness keeps "
            "booting; operator should investigate stale WAL state",
            err,
        )
        return HygieneResult(
            ok=False, skipped=False,
            detail=f"{type(err).__name__}: {err!s}",
        )


__all__ = [
    "HygieneResult",
    "rotate_aegis_wal_for_battle_test",
    "ENV_BATTLE_TEST_HYGIENE_ENABLED",
    "DEFAULT_MAX_BACKUPS",
]
