"""A billing state is not an availability state.

MEASURED (bt-2026-08-18-021438): DoubleWord answered every deep probe with
``HTTPError 402: Payment Required``. The classifier had no entry for 402, so
it fell through to ``-> outage degrade``; the drop streak reached **7,619**
and ``failover_lifecycle`` fired 125 awaken triggers, 23 of them
``HARD OUTAGE escalation ... FORCED AWAKEN``. Nothing was provisioned only
because ``JARVIS_FAILOVER_VM_ORCHESTRATION_HOLD`` happened to be set -- the
sole thing standing between a payment error and a GCE bill was an env var
that nothing in the classification path consults.

The law these tests pin already existed for 401/403 and is stated in
``provider_heartbeat`` itself: an auth error "is NOT a DW outage ... freezes
the loop instead of degrading (which would conflate a config bug with an
outage -> false awaken)". 402 belongs to that family. So does 429, with a
different remedy again: the plane is healthy and asking for less traffic.

Each test uses its OWN ledger. The shared one is durable and currently holds
7,699 real failures, which silently confounds any streak assertion made
against it -- the first version of this verification read ``degrading=True``
for 429 and looked like a bug in the fix rather than history in the file.
"""
from __future__ import annotations

import asyncio
import io
import os
import tempfile
import urllib.error
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import provider_heartbeat as ph


def _http(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("u", code, "m", {}, io.BytesIO(b""))


def _beat(code: int):
    """One beat against a fresh ledger. Returns (heartbeat, dlq_payloads)."""
    tmp = tempfile.mkdtemp()
    # Path, not str: `SurfaceHealthLedger.load()` calls `p.exists()` directly,
    # so a str silently fails the record inside the ledger's own fail-soft
    # handler and every streak assertion quietly reads 0. The annotation says
    # `Optional[Path]`; it is not enforced, and the first draft of this file
    # read a clean 0 for a real 5xx outage because of it.
    ledger = ph.SurfaceHealthLedger(path=Path(tmp) / "ledger.json")
    hb = ph.DWHeartbeat(ledger=ledger)
    captured = []
    hb._dlq_emit_fn = lambda payload: captured.append(payload)
    hb._inference_dispatch_fn = lambda: (_ for _ in ()).throw(_http(code))
    hb._probe_fn = hb._deep_inference_probe
    asyncio.run(hb.beat())
    return hb, captured


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_HEARTBEAT_ENABLED", "true")


class TestClassification:
    @pytest.mark.parametrize("code,expected", [
        (401, "auth"), (403, "auth"),
        (402, "funding"),
        (429, "throttled"),
        (500, "outage"), (503, "outage"), (502, "outage"),
    ])
    def test_status_code_maps_to_its_remedy(self, code, expected):
        assert ph._classify_probe_exception(_http(code)) == expected

    def test_unknown_error_is_still_conservatively_an_outage(self):
        assert ph._classify_probe_exception(RuntimeError("?")) == "outage"

    def test_env_extends_a_class_and_can_never_empty_one(self, monkeypatch):
        """Config widens the net; it must not be able to cut a hole in it.

        A classifier an env var could narrow to nothing is a classifier a
        typo turns back into "everything is an outage" -- which is the state
        this whole module exists to leave."""
        monkeypatch.setenv("JARVIS_PROBE_FUNDING_HTTP_CODES", "1402")
        assert ph._classify_probe_exception(_http(1402)) == "funding"
        assert ph._classify_probe_exception(_http(402)) == "funding"


class TestFundingBreaker:
    def test_402_freezes_and_closes_the_awaken_path(self):
        hb, dlq = _beat(402)
        assert hb._frozen is True
        # The two inputs `_hard_outage_confirmed()` reads. Both zero BY
        # CONSTRUCTION while frozen, which is what makes the awaken
        # structurally unreachable rather than merely unlikely.
        assert hb.consecutive_failures() == 0
        assert hb.is_degrading() is False
        assert dlq and dlq[0]["event"] == "provider_funding_required"
        assert dlq[0]["remedy"] == "billing"

    def test_402_records_no_degrade_verdict_at_all(self):
        hb, _ = _beat(402)
        assert hb.consecutive_failures() == 0

    def test_frozen_heartbeat_stops_its_own_retry_loop(self):
        """`run()`'s loop condition is the breaker; the freeze is the trip."""
        hb, _ = _beat(402)
        beats = []
        hb._probe_fn = lambda: beats.append(1)
        asyncio.run(hb.run())
        assert beats == [], "a frozen heartbeat must not beat again"


class TestThrottle:
    def test_429_neither_freezes_nor_degrades(self):
        hb, dlq = _beat(429)
        assert hb._frozen is False, "a rate limit clears itself; freezing is wrong"
        assert hb.consecutive_failures() == 0, "a 429 must not walk toward an awaken"
        assert not dlq

    def test_429_is_counted_so_it_is_never_invisible(self):
        hb, _ = _beat(429)
        assert hb._transient_skips >= 1


class TestNoRegressionForRealOutages:
    def test_5xx_still_degrades(self):
        """The failover must still work. A fix that disarmed it would be worse
        than the bug: 402 was over-triggering, and silence would be under-."""
        hb, _ = _beat(503)
        assert hb._frozen is False
        assert hb.consecutive_failures() == 1

    def test_401_keeps_its_existing_law(self):
        hb, dlq = _beat(401)
        assert hb._frozen is True
        assert dlq and dlq[0]["event"] == "aegis_configuration_error"


class TestOperatorIsTold:
    def test_the_funding_event_type_is_registered_or_it_vanishes(self):
        """An unregistered event_type is dropped SILENTLY by the broker.

        `_VALID_EVENT_TYPES` is exactly where a missing entry disappears
        without an error, so membership is pinned rather than assumed."""
        from backend.core.ouroboros.governance import ide_observability_stream as s
        assert s.EVENT_TYPE_PROVIDER_FUNDING_REQUIRED in s._VALID_EVENT_TYPES

    def test_publisher_never_raises_on_a_bad_payload(self):
        from backend.core.ouroboros.governance import ide_observability_stream as s
        assert s.publish_provider_funding_required(None) is None  # type: ignore[arg-type]
