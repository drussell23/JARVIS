"""Integration test: degraded resource manager -> phase verdict -> display."""
import asyncio
import time
from datetime import datetime, timezone

import pytest

from backend.core.root_authority_types import (
    SubsystemState, RequiredTier, VerdictReasonCode,
    ResourceVerdict, aggregate_verdicts,
)
from backend.core.verdict_authority import VerdictAuthority


def _verdict(origin, state, tier, boot_allowed=True, serviceable=True,
             reason_code=VerdictReasonCode.HEALTHY, **kw):
    return ResourceVerdict(
        origin=origin, correlation_id="corr-int", epoch=1,
        monotonic_ns=time.monotonic_ns(),
        wall_utc=datetime.now(timezone.utc).isoformat(),
        sequence=1, state=state, boot_allowed=boot_allowed,
        serviceable=serviceable, required_tier=tier,
        reason_code=reason_code, reason_detail=f"{origin} test",
        retryable=False, **kw,
    )


class TestDegradedResourceFlow:
    """End-to-end: cloud_first boot with degraded resources shows degraded, not green."""

    def test_cloud_mode_resources_show_degraded(self):
        async def _run():
            authority = VerdictAuthority()
            authority.begin_epoch()

            verdicts = {
                "ports": _verdict("ports", SubsystemState.READY, RequiredTier.REQUIRED),
                "docker": _verdict("docker", SubsystemState.DEGRADED, RequiredTier.ENHANCEMENT,
                                    serviceable=False,
                                    reason_code=VerdictReasonCode.NOT_INSTALLED),
                "gcp": _verdict("gcp", SubsystemState.DEGRADED, RequiredTier.ENHANCEMENT,
                                 serviceable=True,
                                 reason_code=VerdictReasonCode.MEMORY_ADMISSION_CLOUD_FIRST),
            }

            for name, v in verdicts.items():
                await authority.submit_verdict(name, v)

            phase = aggregate_verdicts("resources", verdicts, epoch=1, correlation_id="c1")
            await authority.submit_phase_verdict(phase)

            # Phase should be READY (only required=ports is ready)
            assert phase.state == SubsystemState.READY
            assert phase.boot_allowed is True
            # But warnings should include docker and gcp
            assert len(phase.warnings) == 2

            # Display should say "ready", not "complete"
            display = authority.get_phase_display("resources")
            assert display["status"] == "ready"
            assert display["status"] != "complete"

        asyncio.run(_run())

    def test_required_port_crash_blocks_boot(self):
        async def _run():
            authority = VerdictAuthority()
            authority.begin_epoch()

            verdicts = {
                "ports": _verdict("ports", SubsystemState.CRASHED, RequiredTier.REQUIRED,
                                   boot_allowed=False, serviceable=False,
                                   reason_code=VerdictReasonCode.PORT_CONFLICT),
            }

            for name, v in verdicts.items():
                await authority.submit_verdict(name, v)

            phase = aggregate_verdicts("resources", verdicts, epoch=1, correlation_id="c1")
            await authority.submit_phase_verdict(phase)

            assert phase.boot_allowed is False
            assert phase.state == SubsystemState.CRASHED
            display = authority.get_phase_display("resources")
            assert display["status"] == "crashed"
            assert display["detail"] == "port_conflict"

        asyncio.run(_run())

    def test_stale_verdict_rejected_by_authority(self):
        async def _run():
            authority = VerdictAuthority()
            authority.begin_epoch()  # epoch=1
            authority.begin_epoch()  # epoch=2

            stale_v = ResourceVerdict(
                origin="ports", correlation_id="c", epoch=1,
                monotonic_ns=time.monotonic_ns(),
                wall_utc=datetime.now(timezone.utc).isoformat(),
                sequence=1, state=SubsystemState.READY,
                boot_allowed=True, serviceable=True,
                required_tier=RequiredTier.REQUIRED,
                reason_code=VerdictReasonCode.HEALTHY,
                reason_detail="stale", retryable=False,
            )
            assert await authority.submit_verdict("ports", stale_v) is False

        asyncio.run(_run())

    def test_heal_requires_evidence(self):
        async def _run():
            authority = VerdictAuthority()
            authority.begin_epoch()

            degraded = _verdict("docker", SubsystemState.DEGRADED, RequiredTier.ENHANCEMENT,
                                 serviceable=False,
                                 reason_code=VerdictReasonCode.NOT_INSTALLED)
            await authority.submit_verdict("docker", degraded)

            # Attempt heal without evidence
            heal_no_proof = ResourceVerdict(
                origin="docker", correlation_id="c", epoch=1,
                monotonic_ns=time.monotonic_ns(),
                wall_utc=datetime.now(timezone.utc).isoformat(),
                sequence=2, state=SubsystemState.READY,
                boot_allowed=True, serviceable=True,
                required_tier=RequiredTier.ENHANCEMENT,
                reason_code=VerdictReasonCode.HEALTHY,
                reason_detail="magically fixed", retryable=False,
            )
            assert await authority.submit_verdict("docker", heal_no_proof) is False

            # Attempt heal with evidence
            heal_with_proof = ResourceVerdict(
                origin="docker", correlation_id="c", epoch=1,
                monotonic_ns=time.monotonic_ns(),
                wall_utc=datetime.now(timezone.utc).isoformat(),
                sequence=3, state=SubsystemState.READY,
                boot_allowed=True, serviceable=True,
                required_tier=RequiredTier.ENHANCEMENT,
                reason_code=VerdictReasonCode.HEALTHY,
                reason_detail="docker started",
                retryable=False,
                evidence={"recovery_proof": "docker_started_pid_99"},
            )
            assert await authority.submit_verdict("docker", heal_with_proof) is True

        asyncio.run(_run())
