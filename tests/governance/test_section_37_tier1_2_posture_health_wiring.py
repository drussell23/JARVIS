"""§37 Tier 1 #2 — PostureObserver task-death detection wiring.

Closes the row by wiring the canonical ``posture_health`` substrate
(already shipped: 4-state taxonomy + safe-load wrappers + 52-test
spine + ``invariant_drift_observer.py:438-468`` consumer) into the
remaining 4 surfaces:

  1. **SensorGovernor consumer** — ``_default_posture_fn`` +
     ``_default_signal_bundle_fn`` now compose
     ``posture_health.safe_load_posture_value`` /
     ``safe_load_posture`` so a dead PostureObserver task degrades
     the governor to unweighted (1.0×) caps — equivalent to
     MAINTAIN safe-default — instead of silently applying weights
     against frozen state. Substrate-unavailable rollback path
     preserved.

  2. **Canonical SSE event registration** —
     ``EVENT_TYPE_POSTURE_OBSERVER_DEGRADED`` registered in
     ``ide_observability_stream._VALID_EVENT_TYPES`` so the broker
     accepts publishes from
     ``posture_health._maybe_publish_degraded_event``. Local
     constant in ``posture_health.py`` retained for substrate
     independence; the two definitions MUST agree on string value.

  3. **Operator REPL** — ``/posture health`` subcommand in
     ``posture_repl.py`` renders the classifier verdict (status +
     detail + seconds_since_last_ok + consecutive_failures +
     threshold). When master flag is dormant, returns a notice
     rather than fabricating HEALTHY (operator binding: no
     fake-healthy).

  4. **IDE GET route** — ``GET /observability/posture/health`` in
     ``ide_observability.py`` composes
     ``evaluate_observer_health(observer.task_health_snapshot())``
     and returns the verdict. Loopback-only / rate-limited /
     master-flag-gated per existing IDE-observability contract.

## What the AST pin enforces

  * SensorGovernor's ``_default_posture_fn`` MUST compose
    ``safe_load_posture_value`` (no direct ``store.load_current()``
    call as the primary path).
  * ``_default_signal_bundle_fn`` MUST compose
    ``safe_load_posture``.
  * Canonical SSE event registered in
    ``_VALID_EVENT_TYPES`` frozenset.
  * Local + canonical event-type constants string-equal.
  * REPL handler ``_health`` is reachable from
    ``dispatch_posture_command``.
  * IDE route handler ``_handle_posture_health`` is registered.

Failure on any of these is a load-bearing structural drift, not
cosmetic.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
_GOVERNANCE_ROOT = _REPO_ROOT / "backend/core/ouroboros/governance"


# -----------------------------------------------------------------
# AST pin — SensorGovernor composes the canonical safe-load wrappers
# -----------------------------------------------------------------


def test_sensor_governor_default_posture_fn_composes_safe_load():
    """``_default_posture_fn`` MUST call
    ``safe_load_posture_value`` as the primary path. Direct
    ``store.load_current()`` is allowed ONLY as a substrate-
    unavailable rollback path (inside an ``except ImportError``
    branch)."""
    src = (_GOVERNANCE_ROOT / "sensor_governor.py").read_text(
        encoding="utf-8",
    )
    # Locate the function body.
    fn_match = re.search(
        r"def _default_posture_fn\(\).*?(?=\ndef )",
        src, re.DOTALL,
    )
    assert fn_match, "_default_posture_fn not found"
    body = fn_match.group(0)
    assert "safe_load_posture_value" in body, (
        "_default_posture_fn must compose canonical "
        "posture_health.safe_load_posture_value"
    )
    # Ensure the primary path imports the substrate (not just the
    # rollback path).
    assert "from backend.core.ouroboros.governance.posture_health" in body


def test_sensor_governor_signal_bundle_fn_composes_safe_load():
    src = (_GOVERNANCE_ROOT / "sensor_governor.py").read_text(
        encoding="utf-8",
    )
    fn_match = re.search(
        r"def _default_signal_bundle_fn\(\).*?(?=\ndef )",
        src, re.DOTALL,
    )
    assert fn_match, "_default_signal_bundle_fn not found"
    body = fn_match.group(0)
    assert "safe_load_posture" in body, (
        "_default_signal_bundle_fn must compose canonical "
        "posture_health.safe_load_posture"
    )


# -----------------------------------------------------------------
# AST pin — canonical SSE event registered + matches local constant
# -----------------------------------------------------------------


def test_posture_observer_degraded_in_canonical_valid_event_types():
    """Broker rejects unknown event types — the local constant in
    ``posture_health.py`` is meaningless until the canonical
    frozenset accepts it."""
    from backend.core.ouroboros.governance import (
        ide_observability_stream as _broker_mod,
    )
    assert (
        "posture_observer_degraded"
        in _broker_mod._VALID_EVENT_TYPES
    ), (
        "EVENT_TYPE_POSTURE_OBSERVER_DEGRADED must be registered in "
        "_VALID_EVENT_TYPES — without registration, broker.publish "
        "rejects the event silently"
    )


def test_canonical_and_local_event_constants_string_equal():
    """Two definitions exist (canonical broker frozenset + local
    in ``posture_health.py`` for substrate independence). Pin
    asserts they agree on string value — drift here means SSE
    publishes silently fail."""
    from backend.core.ouroboros.governance import (
        ide_observability_stream as _broker_mod,
    )
    from backend.core.ouroboros.governance import posture_health as _ph_mod
    assert (
        _broker_mod.EVENT_TYPE_POSTURE_OBSERVER_DEGRADED
        == _ph_mod.EVENT_TYPE_POSTURE_OBSERVER_DEGRADED
    )


# -----------------------------------------------------------------
# AST pin — REPL handler registered
# -----------------------------------------------------------------


def test_posture_repl_health_subcommand_registered():
    """`/posture health` subcommand routes to the ``_health``
    helper, which composes the canonical classifier."""
    src = (_GOVERNANCE_ROOT / "posture_repl.py").read_text(
        encoding="utf-8",
    )
    assert 'if head == "health":' in src, (
        "/posture health must be wired in dispatch_posture_command"
    )
    assert "def _health()" in src, (
        "_health() helper must be defined"
    )
    assert "evaluate_observer_health" in src, (
        "_health() must compose canonical evaluate_observer_health"
    )


def test_posture_repl_help_documents_health_subcommand():
    src = (_GOVERNANCE_ROOT / "posture_repl.py").read_text(
        encoding="utf-8",
    )
    assert "/posture health" in src, (
        "_HELP block must document the /posture health subcommand"
    )


# -----------------------------------------------------------------
# AST pin — IDE GET route handler registered
# -----------------------------------------------------------------


def test_ide_observability_posture_health_route_registered():
    src = (_GOVERNANCE_ROOT / "ide_observability.py").read_text(
        encoding="utf-8",
    )
    assert '"/observability/posture/health"' in src, (
        "GET /observability/posture/health route must be registered"
    )
    assert "_handle_posture_health" in src, (
        "_handle_posture_health handler must exist"
    )


def test_ide_handler_method_exists_at_runtime():
    """Belt-and-braces — the AST scan + runtime attribute check
    catch both source drift AND mis-wired registration."""
    from backend.core.ouroboros.governance.ide_observability import (
        IDEObservabilityRouter,
    )
    assert hasattr(IDEObservabilityRouter, "_handle_posture_health")


# -----------------------------------------------------------------
# Functional integration — SensorGovernor's safe wrapper degrades
# correctly when posture_health is enabled + observer is dead
# -----------------------------------------------------------------


@pytest.fixture
def _detection_on(monkeypatch):
    monkeypatch.setenv("JARVIS_POSTURE_HEALTH_DETECTION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "true")
    yield


def test_sensor_governor_returns_none_when_observer_dead(
    _detection_on, monkeypatch,
):
    """When detection is enabled AND observer is None
    (TASK_DEAD), ``_default_posture_fn`` returns None — sensors
    fall back to unweighted (1.0×) caps, equivalent to MAINTAIN
    safe-default. This is the load-bearing closure: stale state
    no longer silently propagates."""
    from backend.core.ouroboros.governance import sensor_governor

    # Force observer to None (simulates dead task).
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.posture_observer."
        "get_default_observer",
        lambda *a, **kw: None,
    )
    # Store also empty — but the safe wrapper short-circuits on
    # observer=None before reading the store, so this doesn't
    # matter for the test.
    fake_store = MagicMock()
    fake_store.load_current.return_value = None
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.posture_observer."
        "get_default_store",
        lambda *a, **kw: fake_store,
    )

    posture = sensor_governor._default_posture_fn()
    assert posture is None, (
        f"observer=None must yield None posture (MAINTAIN "
        f"safe-default), got {posture!r}"
    )


def test_sensor_governor_passes_through_when_detection_off(
    monkeypatch,
):
    """Master flag off → safe wrapper passes through to
    ``store.load_current()`` byte-equivalent to legacy behavior.
    Backward-compat preserved end-to-end."""
    monkeypatch.setenv(
        "JARVIS_POSTURE_HEALTH_DETECTION_ENABLED", "false",
    )
    from backend.core.ouroboros.governance import sensor_governor

    # Build a fake reading with posture.value
    fake_reading = MagicMock()
    fake_reading.posture.value = "EXPLORE"
    fake_reading.evidence = []

    fake_store = MagicMock()
    fake_store.load_current.return_value = fake_reading
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.posture_observer."
        "get_default_store",
        lambda *a, **kw: fake_store,
    )
    # observer doesn't matter when detection is off (pass-through)
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.posture_observer."
        "get_default_observer",
        lambda *a, **kw: None,
    )

    posture = sensor_governor._default_posture_fn()
    assert posture == "EXPLORE"


# -----------------------------------------------------------------
# Functional integration — REPL renders verdict correctly
# -----------------------------------------------------------------


def test_repl_health_subcommand_returns_dormant_when_master_off(
    monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_POSTURE_HEALTH_DETECTION_ENABLED", "false",
    )
    monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "true")
    from backend.core.ouroboros.governance import posture_repl

    posture_repl.set_default_store(MagicMock())  # any non-None
    try:
        result = posture_repl.dispatch_posture_command("/posture health")
    finally:
        posture_repl.reset_default_providers()
    assert result.ok
    assert "dormant" in result.text.lower()
    # Operator binding: no fake-healthy
    assert "HEALTHY" not in result.text


def test_repl_health_subcommand_returns_dead_when_observer_none(
    _detection_on, monkeypatch,
):
    """Detection on + observer=None → status=TASK_DEAD."""
    from backend.core.ouroboros.governance import posture_repl

    posture_repl.set_default_store(MagicMock())  # any non-None
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.posture_observer."
        "get_default_observer",
        lambda *a, **kw: None,
    )
    try:
        result = posture_repl.dispatch_posture_command("/posture health")
    finally:
        posture_repl.reset_default_providers()
    assert result.ok
    assert "TASK_DEAD" in result.text


# -----------------------------------------------------------------
# Provenance pins — v2.84 closure documented in source
# -----------------------------------------------------------------


def test_sensor_governor_documents_v2_84_closure():
    src = (_GOVERNANCE_ROOT / "sensor_governor.py").read_text(
        encoding="utf-8",
    )
    assert "§37 Tier 1 #2" in src, (
        "sensor_governor must cite §37 Tier 1 #2 (v2.84) at the "
        "wiring sites"
    )


def test_ide_observability_documents_v2_84_route():
    src = (_GOVERNANCE_ROOT / "ide_observability.py").read_text(
        encoding="utf-8",
    )
    assert "§37 Tier 1 #2" in src, (
        "ide_observability must cite §37 Tier 1 #2 at the route "
        "registration site"
    )


# -----------------------------------------------------------------
# Substrate completeness sanity — verify we composed canonical
# (didn't introduce parallel logic)
# -----------------------------------------------------------------


def test_no_parallel_classifier_in_sensor_governor():
    """SensorGovernor must compose the canonical classifier — NOT
    re-implement health-check logic. Any function that mentions
    'TASK_DEAD' / 'DEGRADED_HUNG' / 'DEGRADED_FAILING' in
    sensor_governor would be a parallel-logic violation."""
    src = (_GOVERNANCE_ROOT / "sensor_governor.py").read_text(
        encoding="utf-8",
    )
    forbidden = ("TASK_DEAD", "DEGRADED_HUNG", "DEGRADED_FAILING")
    violations = [
        token for token in forbidden if token in src
    ]
    assert violations == [], (
        f"sensor_governor must NOT mention {violations} — "
        f"compose posture_health.evaluate_observer_health instead "
        f"(the classifier owns the policy; the governor is a "
        f"consumer per posture_health.py:558-561 contract)"
    )
