"""Tests for VerdictAuthority -- single source of truth for component/phase status."""
import time
from datetime import datetime, timezone

import pytest

from backend.core.root_authority_types import (
    SubsystemState,
    RequiredTier,
    VerdictReasonCode,
    RecoveryAction,
    ResourceVerdict,
    PhaseVerdict,
)


def _make_verdict(
    origin="test",
    epoch=1,
    seq=1,
    state=SubsystemState.READY,
    boot_allowed=True,
    serviceable=True,
    reason_code=VerdictReasonCode.HEALTHY,
    evidence=None,
    **kw,
):
    return ResourceVerdict(
        origin=origin,
        correlation_id="corr-test",
        epoch=epoch,
        monotonic_ns=time.monotonic_ns(),
        wall_utc=datetime.now(timezone.utc).isoformat(),
        sequence=seq,
        state=state,
        boot_allowed=boot_allowed,
        serviceable=serviceable,
        required_tier=RequiredTier.REQUIRED,
        reason_code=reason_code,
        reason_detail="ok",
        retryable=False,
        evidence=evidence or {},
        **kw,
    )


class TestVerdictAuthoritySubmit:
    @pytest.fixture
    def authority(self):
        from backend.core.verdict_authority import VerdictAuthority

        return VerdictAuthority()

    @pytest.mark.asyncio
    async def test_submit_and_read(self, authority):
        v = _make_verdict(origin="docker")
        assert await authority.submit_verdict("docker", v) is True
        assert authority.get_component_status("docker") is v

    @pytest.mark.asyncio
    async def test_missing_component_returns_none(self, authority):
        assert authority.get_component_status("nonexistent") is None

    @pytest.mark.asyncio
    async def test_rejects_stale_epoch(self, authority):
        authority.begin_epoch()  # epoch=1
        authority.begin_epoch()  # epoch=2
        v_old = _make_verdict(epoch=1)
        assert await authority.submit_verdict("docker", v_old) is False

    @pytest.mark.asyncio
    async def test_rejects_out_of_order_monotonic(self, authority):
        authority.begin_epoch()
        v1 = _make_verdict(epoch=1, seq=1)
        await authority.submit_verdict("docker", v1)
        v2 = ResourceVerdict(
            origin="docker",
            correlation_id="corr",
            epoch=1,
            monotonic_ns=v1.monotonic_ns - 1000,
            wall_utc=datetime.now(timezone.utc).isoformat(),
            sequence=0,
            state=SubsystemState.DEGRADED,
            boot_allowed=True,
            serviceable=True,
            required_tier=RequiredTier.REQUIRED,
            reason_code=VerdictReasonCode.UNKNOWN,
            reason_detail="late",
            retryable=False,
        )
        assert await authority.submit_verdict("docker", v2) is False
        assert authority.get_component_status("docker") is v1

    @pytest.mark.asyncio
    async def test_rejects_heal_without_evidence(self, authority):
        authority.begin_epoch()
        v_degraded = _make_verdict(
            epoch=1,
            state=SubsystemState.DEGRADED,
            serviceable=True,
            reason_code=VerdictReasonCode.NOT_INSTALLED,
        )
        await authority.submit_verdict("docker", v_degraded)
        v_ready = _make_verdict(epoch=1, seq=2)
        assert await authority.submit_verdict("docker", v_ready) is False

    @pytest.mark.asyncio
    async def test_allows_heal_with_evidence(self, authority):
        authority.begin_epoch()
        v_degraded = _make_verdict(
            epoch=1,
            state=SubsystemState.DEGRADED,
            serviceable=True,
            reason_code=VerdictReasonCode.NOT_INSTALLED,
        )
        await authority.submit_verdict("docker", v_degraded)
        v_ready = _make_verdict(
            epoch=1,
            seq=2,
            evidence={"recovery_proof": "docker_started_pid_12345"},
        )
        assert await authority.submit_verdict("docker", v_ready) is True

    @pytest.mark.asyncio
    async def test_allows_degradation_without_evidence(self, authority):
        authority.begin_epoch()
        v_ready = _make_verdict(epoch=1)
        await authority.submit_verdict("docker", v_ready)
        v_degraded = _make_verdict(
            epoch=1,
            seq=2,
            state=SubsystemState.DEGRADED,
            serviceable=True,
            reason_code=VerdictReasonCode.CIRCUIT_BREAKER_OPEN,
        )
        assert await authority.submit_verdict("docker", v_degraded) is True


class TestVerdictAuthorityEpoch:
    @pytest.fixture
    def authority(self):
        from backend.core.verdict_authority import VerdictAuthority

        return VerdictAuthority()

    def test_begin_epoch_increments(self, authority):
        assert authority.current_epoch == 0
        e1 = authority.begin_epoch()
        assert e1 == 1
        assert authority.current_epoch == 1
        e2 = authority.begin_epoch()
        assert e2 == 2
        assert authority.current_epoch == 2

    @pytest.mark.asyncio
    async def test_current_epoch_verdict_accepted(self, authority):
        authority.begin_epoch()  # epoch=1
        v = _make_verdict(epoch=1)
        assert await authority.submit_verdict("svc", v) is True

    @pytest.mark.asyncio
    async def test_future_epoch_verdict_accepted(self, authority):
        """A verdict from a future epoch is not stale -- accept it."""
        authority.begin_epoch()  # epoch=1
        v = _make_verdict(epoch=2)
        assert await authority.submit_verdict("svc", v) is True


class TestVerdictAuthorityPhase:
    @pytest.fixture
    def authority(self):
        from backend.core.verdict_authority import VerdictAuthority

        return VerdictAuthority()

    @pytest.mark.asyncio
    async def test_submit_and_read_phase(self, authority):
        pv = PhaseVerdict(
            phase_name="resources",
            state=SubsystemState.READY,
            boot_allowed=True,
            serviceable=True,
            manager_verdicts={},
            reason_codes=(),
            warnings=(),
            epoch=1,
            monotonic_ns=time.monotonic_ns(),
            wall_utc=datetime.now(timezone.utc).isoformat(),
            correlation_id="c1",
        )
        assert await authority.submit_phase_verdict(pv) is True
        assert authority.get_phase_status("resources") is pv

    @pytest.mark.asyncio
    async def test_rejects_stale_epoch_phase(self, authority):
        authority.begin_epoch()  # epoch=1
        authority.begin_epoch()  # epoch=2
        pv = PhaseVerdict(
            phase_name="resources",
            state=SubsystemState.READY,
            boot_allowed=True,
            serviceable=True,
            manager_verdicts={},
            reason_codes=(),
            warnings=(),
            epoch=1,
            monotonic_ns=time.monotonic_ns(),
            wall_utc=datetime.now(timezone.utc).isoformat(),
            correlation_id="c1",
        )
        assert await authority.submit_phase_verdict(pv) is False

    @pytest.mark.asyncio
    async def test_missing_phase_returns_none(self, authority):
        assert authority.get_phase_status("nonexistent") is None


class TestVerdictAuthoritySnapshot:
    @pytest.fixture
    def authority(self):
        from backend.core.verdict_authority import VerdictAuthority

        return VerdictAuthority()

    @pytest.mark.asyncio
    async def test_snapshot_is_copy(self, authority):
        authority.begin_epoch()
        v = _make_verdict(epoch=1)
        await authority.submit_verdict("docker", v)
        snap = authority.get_all_verdicts_snapshot()
        assert "docker" in snap
        assert snap["docker"] is v
        # Mutation of snapshot must not affect authority
        snap["injected"] = v
        assert authority.get_component_status("injected") is None

    @pytest.mark.asyncio
    async def test_snapshot_empty_initially(self, authority):
        snap = authority.get_all_verdicts_snapshot()
        assert snap == {}

    @pytest.mark.asyncio
    async def test_snapshot_multiple_components(self, authority):
        authority.begin_epoch()
        v1 = _make_verdict(epoch=1, origin="docker")
        v2 = _make_verdict(epoch=1, origin="redis")
        await authority.submit_verdict("docker", v1)
        await authority.submit_verdict("redis", v2)
        snap = authority.get_all_verdicts_snapshot()
        assert len(snap) == 2
        assert snap["docker"] is v1
        assert snap["redis"] is v2


class TestVerdictAuthorityPhaseDisplay:
    @pytest.fixture
    def authority(self):
        from backend.core.verdict_authority import VerdictAuthority

        return VerdictAuthority()

    @pytest.mark.asyncio
    async def test_no_phase_returns_pending(self, authority):
        assert authority.get_phase_display("resources") == {"status": "pending"}

    @pytest.mark.asyncio
    async def test_phase_display_from_verdict(self, authority):
        pv = PhaseVerdict(
            phase_name="resources",
            state=SubsystemState.DEGRADED,
            boot_allowed=True,
            serviceable=True,
            manager_verdicts={},
            reason_codes=(VerdictReasonCode.NOT_INSTALLED,),
            warnings=(),
            epoch=1,
            monotonic_ns=time.monotonic_ns(),
            wall_utc=datetime.now(timezone.utc).isoformat(),
            correlation_id="c1",
        )
        await authority.submit_phase_verdict(pv)
        display = authority.get_phase_display("resources")
        assert display["status"] == "degraded"
        assert display["detail"] == "not_installed"

    @pytest.mark.asyncio
    async def test_phase_display_no_reason_codes(self, authority):
        """Phase with no reason codes should produce empty detail."""
        pv = PhaseVerdict(
            phase_name="backend",
            state=SubsystemState.READY,
            boot_allowed=True,
            serviceable=True,
            manager_verdicts={},
            reason_codes=(),
            warnings=(),
            epoch=1,
            monotonic_ns=time.monotonic_ns(),
            wall_utc=datetime.now(timezone.utc).isoformat(),
            correlation_id="c1",
        )
        await authority.submit_phase_verdict(pv)
        display = authority.get_phase_display("backend")
        assert display["status"] == "ready"
        assert display["detail"] == ""


class TestVerdictAuthorityEdgeCases:
    @pytest.fixture
    def authority(self):
        from backend.core.verdict_authority import VerdictAuthority

        return VerdictAuthority()

    @pytest.mark.asyncio
    async def test_same_monotonic_ns_accepted(self, authority):
        """Equal monotonic_ns (not strictly less) should be accepted."""
        authority.begin_epoch()
        ns = time.monotonic_ns()
        v1 = ResourceVerdict(
            origin="svc",
            correlation_id="c1",
            epoch=1,
            monotonic_ns=ns,
            wall_utc=datetime.now(timezone.utc).isoformat(),
            sequence=1,
            state=SubsystemState.READY,
            boot_allowed=True,
            serviceable=True,
            required_tier=RequiredTier.REQUIRED,
            reason_code=VerdictReasonCode.HEALTHY,
            reason_detail="ok",
            retryable=False,
        )
        v2 = ResourceVerdict(
            origin="svc",
            correlation_id="c2",
            epoch=1,
            monotonic_ns=ns,
            wall_utc=datetime.now(timezone.utc).isoformat(),
            sequence=2,
            state=SubsystemState.DEGRADED,
            boot_allowed=True,
            serviceable=True,
            required_tier=RequiredTier.REQUIRED,
            reason_code=VerdictReasonCode.CIRCUIT_BREAKER_OPEN,
            reason_detail="cb open",
            retryable=False,
        )
        await authority.submit_verdict("svc", v1)
        # Same monotonic_ns: existing.monotonic_ns > verdict.monotonic_ns is False
        # so it should NOT be rejected by the out-of-order check
        result = await authority.submit_verdict("svc", v2)
        assert result is True

    @pytest.mark.asyncio
    async def test_overwrite_with_same_severity(self, authority):
        """Replacing a verdict with the same severity (no heal) needs no evidence."""
        authority.begin_epoch()
        v1 = _make_verdict(
            epoch=1,
            state=SubsystemState.DEGRADED,
            serviceable=True,
            reason_code=VerdictReasonCode.NOT_INSTALLED,
        )
        await authority.submit_verdict("svc", v1)
        v2 = _make_verdict(
            epoch=1,
            seq=2,
            state=SubsystemState.DEGRADED,
            serviceable=True,
            reason_code=VerdictReasonCode.CIRCUIT_BREAKER_OPEN,
        )
        assert await authority.submit_verdict("svc", v2) is True

    @pytest.mark.asyncio
    async def test_epoch_zero_verdict_accepted_at_epoch_zero(self, authority):
        """Before any begin_epoch call, epoch=0 verdicts should be accepted."""
        v = _make_verdict(epoch=0)
        assert await authority.submit_verdict("svc", v) is True
