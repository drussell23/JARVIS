"""Slice 216 — Concurrency Kernel: closed-client recycle + multi-step enable.

Live evidence (first hours of the dual-engine soak, 2026-06-10): the Claude
rescue path retried with backoff on `APIConnectionError -> RuntimeError:
"Cannot send a request, as the client has been closed"` — a retry that can
NEVER succeed (the shared client object is dead; only a recycle helps). The
existing hard-pool recycle matches exception CLASS NAMES (RuntimeError is too
generic to add), so this adds a MESSAGE-KEYED trigger: any member of the
cause chain whose message says the client is closed -> recycle NOW.

Also pins: multi-step orchestration master ON in the soak compose (the
`no_plan` root cause — 4th cut wire of the autonomy chain).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance.providers import (
    _is_closed_client_error,
)

_ROOT = Path(__file__).resolve().parents[2]


class _Err(Exception):
    pass


def test_closed_client_chain_detected():
    chain = [
        _Err("Connection error."),
        RuntimeError("Cannot send a request, as the client has been closed."),
    ]
    assert _is_closed_client_error(chain) is True


def test_closed_client_variant_wording():
    assert _is_closed_client_error([RuntimeError("client is closed")]) is True


def test_benign_chain_not_detected():
    chain = [_Err("Connection error."), TimeoutError("read timed out")]
    assert _is_closed_client_error(chain) is False


def test_empty_chain_safe():
    assert _is_closed_client_error([]) is False


def test_retry_path_recycles_on_closed_client():
    """Source pin: the retry handler must recycle (not just back off) when
    the closed-client signature appears in the chain."""
    src = (_ROOT / "backend" / "core" / "ouroboros" / "governance"
           / "providers.py").read_text(encoding="utf-8")
    assert "_is_closed_client_error" in src
    assert "closed_client" in src  # the recycle reason key


def test_soak_compose_enables_multi_step():
    """The no_plan root cause: JARVIS_MULTI_STEP_ORCHESTRATION_ENABLED was
    default-FALSE and absent from the soak — the 4th cut wire."""
    compose = (_ROOT / "docker-compose.dw-cortex-soak.yml").read_text(
        encoding="utf-8")
    assert 'JARVIS_MULTI_STEP_ORCHESTRATION_ENABLED: "1"' in compose
