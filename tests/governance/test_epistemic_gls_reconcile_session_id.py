"""Sovereign Epistemic Context Matrix — FIX 2 regression guard.

GovernedLoopService.stop()'s LR2 reconcile block previously derived the
session id from ``self._session_dir`` — but the service never assigns
``_session_dir`` (only the harness does), so ``_sid`` was always "" and the
``if _sid:`` guard never fired → reconcile never ran. Worse, the 6c WRITER
keys the QuarantineLedger by ``get_active_session_id()`` while this READER
keyed by ``_session_dir.name`` — a latent reader/writer mismatch.

The fix resolves the session id the SAME way the writer does
(``strategic_direction.get_active_session_id()``) with a ``pid-<getpid()>``
fail-soft fallback — mirroring ``orchestrator._resolve_session_id``. The
``if _sid:`` guard is now always true → reconcile actually runs, and reader
and writer agree on the key.
"""
from __future__ import annotations

import inspect

from backend.core.ouroboros.governance import governed_loop_service as glsmod


def test_stop_reconcile_uses_get_active_session_id():
    src = inspect.getsource(glsmod.GovernedLoopService.stop)
    # The canonical session source, not the (always-empty) _session_dir.name.
    assert "get_active_session_id" in src
    # pid fallback mirrors orchestrator._resolve_session_id.
    assert "pid-" in src or "getpid" in src


def test_stop_reconcile_no_longer_keys_on_session_dir_name():
    src = inspect.getsource(glsmod.GovernedLoopService.stop)
    # The old reader pattern (_Path(str(_sess_dir)).name) is gone from the
    # session-id resolution — the writer never used it.
    assert "_session_dir\").name" not in src
    assert "_sess_dir)).name" not in src


def test_get_active_session_id_is_importable():
    from backend.core.ouroboros.governance.strategic_direction import (
        get_active_session_id,
    )
    # Returns None or a str; never raises.
    v = get_active_session_id()
    assert v is None or isinstance(v, str)


def test_reconcile_block_still_targets_epistemic_quarantine_ledger():
    src = inspect.getsource(glsmod.GovernedLoopService.stop)
    assert "epistemic_quarantine.jsonl" in src
    assert "QuarantineLedger" in src
    assert "incremental_update" in src
