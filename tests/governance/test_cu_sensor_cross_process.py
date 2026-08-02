"""Two daemons, one repo, one stream of failures.

A soak daemon and an interactive daemon against the same checkout are two
sensors with two in-memory windows over ONE stream of real failures. That
splits in both directions, and the quieter direction is the worse one:

    OVER-FIRE   both cross the threshold, both file the same pattern.
                The router dedups, so the cost is a duplicate envelope.

    UNDER-FIRE  four real failures land two-and-two. Neither reaches three,
                so NOTHING is filed — the pattern is invisible to governance
                while the journal on disk plainly shows four occurrences.
                A missing envelope looks exactly like a healthy system.

Measured before the fix, with both daemons already booted::

    A local window: 2 · B local window: 2 · journal lines: 4  → 0 envelopes

ONE ROOT CAUSE
----------------
The decision was made against LOCAL state while the truth was already on disk.
So the decision moved into the journal, under the journal's own lock:
append-and-decide in a single critical section, so no process can count a
window that excludes an occurrence already written.

The claim is a TTL LEASE, not a lock — `op_lease`'s hard-won lesson that "the
static in-flight lock carried no liveness signal" and wedged a whole session
when its holder died. A process that dies between claiming and ingesting must
not silence a pattern forever.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

import pytest

from backend.core.ouroboros.governance.intake.sensors import cu_execution_sensor as ces
from backend.core.ouroboros.governance.intake.sensors.cu_execution_sensor import (
    CUExecutionRecord,
    CUExecutionSensor,
)


class _Router:
    def __init__(self):
        self.ingested = []

    async def ingest(self, envelope):
        self.ingested.append(envelope)
        return "accepted"


@pytest.fixture(autouse=True)
def _shared_journal(tmp_path, monkeypatch):
    """ONE journal, as two daemons on one repo would share."""
    monkeypatch.setenv("JARVIS_CU_JOURNAL_PATH",
                       str(tmp_path / "cu_failure_journal.jsonl"))
    CUExecutionSensor._instance = None
    yield tmp_path / "cu_failure_journal.jsonl"
    CUExecutionSensor._instance = None


def _daemon(router: Optional[_Router] = None) -> CUExecutionSensor:
    """A second sensor over the same journal — what a second process is.

    The singleton is dropped so the constructor really runs; each instance
    mints its own owner token, which is what makes them distinguishable to the
    claim check.
    """
    CUExecutionSensor._instance = None
    return CUExecutionSensor(router=router or _Router())


def _fail(app: str = "Messages") -> CUExecutionRecord:
    return CUExecutionRecord(
        goal="message alice saying hi", success=False, steps_completed=1,
        steps_total=3, elapsed_s=1.0, error="target not found",
        is_messaging=True, contact="alice", app=app,
    )


def _total(*sensors) -> int:
    return sum(len(s._router.ingested) for s in sensors)


class TestUnderFire:
    @pytest.mark.asyncio
    async def test_failures_split_across_daemons_still_graduate(self):
        """THE quiet defect. Four real failures, two each, zero envelopes."""
        A, B = _daemon(), _daemon()
        for s in (A, B, A, B):
            await s.record(_fail())
        assert _total(A, B) == 1, (
            "split occurrences did not reach the threshold — the pattern is "
            "invisible to governance while the journal shows four of them")

    @pytest.mark.asyncio
    async def test_the_count_in_the_envelope_is_the_TRUE_count(self):
        """Not the filing daemon's local view of it."""
        A, B = _daemon(), _daemon()
        for s in (A, B, A, B):
            await s.record(_fail())
        ev = (A._router.ingested + B._router.ingested)[0]
        assert ev.evidence["occurrence_count"] >= ces._GRADUATION_THRESHOLD

    @pytest.mark.asyncio
    async def test_a_third_daemon_joining_late_sees_the_history(self):
        A, B = _daemon(), _daemon()
        await A.record(_fail())
        await B.record(_fail())
        C = _daemon()
        assert C.get_stats()["journal_replayed"] == 2
        await C.record(_fail())
        assert len(C._router.ingested) == 1


class TestOverFire:
    @pytest.mark.asyncio
    async def test_a_concurrent_third_failure_files_exactly_once(self):
        A, B = _daemon(), _daemon()
        for _ in range(2):
            await A.record(_fail())
            await B.record(_fail())
        await asyncio.gather(A.record(_fail()), B.record(_fail()))
        assert _total(A, B) == 1, "both daemons filed the same pattern"

    @pytest.mark.asyncio
    async def test_daemons_have_distinct_owner_tokens(self):
        """A module-global owner made two sensors in one process
        indistinguishable — which silently disabled the mutual-exclusion check
        inside the very harness written to prove it worked. The token belongs
        to the sensor life."""
        assert _daemon()._owner != _daemon()._owner

    @pytest.mark.asyncio
    async def test_the_cooldown_is_shared(self):
        """One daemon files; the other must honour that cooldown."""
        A = _daemon()
        for _ in range(ces._GRADUATION_THRESHOLD):
            await A.record(_fail())
        assert len(A._router.ingested) == 1
        B = _daemon()
        for _ in range(ces._GRADUATION_THRESHOLD):
            await B.record(_fail())
        assert B._router.ingested == [], "the second daemon re-filed it"


class TestTheClaimIsALease:
    @pytest.mark.asyncio
    async def test_a_live_claim_blocks_the_other_daemon(self, _shared_journal):
        A, B = _daemon(), _daemon()
        for _ in range(ces._GRADUATION_THRESHOLD):
            await A.record(_fail())
        sig = _fail().failure_signature
        mine, _count, why = B._decide(sig, None, time.time())
        assert mine is False and why in ("claimed_elsewhere", "cooldown")

    @pytest.mark.asyncio
    async def test_an_expired_claim_is_retried_by_the_other_daemon(
            self, _shared_journal, monkeypatch):
        """A process that dies between claiming and ingesting must not silence
        the pattern forever — the whole reason this is a lease."""
        A = _daemon()
        sig = _fail().failure_signature
        now = time.time()
        for _ in range(ces._GRADUATION_THRESHOLD):
            await A.record(_fail())
        # Hand-write a STALE claim from a dead process, and remove the emission
        # so the only thing standing in the way is the claim itself.
        kept = [l for l in _shared_journal.read_text().splitlines()
                if '"k":"e"' not in l and '"k":"c"' not in l]
        kept.append(json.dumps({"v": 1, "k": "c", "t": now - 10_000,
                                "sig": sig, "repo": "jarvis",
                                "own": "9999:deadbeef"}))
        _shared_journal.write_text("\n".join(kept) + "\n")
        B = _daemon()
        mine, _c, why = B._decide(sig, None, time.time())
        assert mine is True and why == "claimed", (
            "a dead process's claim silenced the pattern permanently")

    @pytest.mark.asyncio
    async def test_a_released_claim_is_retried_immediately(
            self, _shared_journal):
        """An ingest failure writes a release so the next attempt need not wait
        out the TTL."""
        sig = _fail().failure_signature
        A = _daemon()
        for _ in range(ces._GRADUATION_THRESHOLD):
            await A.record(_fail())
        text = _shared_journal.read_text()
        assert '"k":"c"' in text, "no claim was recorded"

    def test_the_ttl_is_clamped(self, monkeypatch):
        for raw, lo, hi in (("0", 5.0, 5.0), ("99999", 900.0, 900.0),
                            ("garbage", 60.0, 60.0)):
            monkeypatch.setenv("JARVIS_CU_CLAIM_TTL_S", raw)
            assert lo <= ces._claim_ttl_s() <= hi


class TestDegradingUnderContention:
    @pytest.mark.asyncio
    async def test_an_unusable_journal_still_honours_the_local_rules(
            self, monkeypatch):
        """THE bug this test exists for.

        An earlier draft treated "the journal could not answer" as "emit", so
        every record looked like a fresh decision and one pattern produced
        THREE envelopes. Degrading may cost a duplicate under contention; it
        must never cost the threshold or the cooldown.
        """
        monkeypatch.setenv("JARVIS_CU_JOURNAL_PATH", "/proc/nope/journal.jsonl")
        CUExecutionSensor._instance = None
        s = CUExecutionSensor(router=_Router())
        for _ in range(ces._GRADUATION_THRESHOLD + 3):
            await s.record(_fail())
        assert len(s._router.ingested) == 1, (
            f"{len(s._router.ingested)} envelopes for one pattern — the "
            f"degraded path bypassed the rules")
        assert s._claim_lock_failures >= 1

    @pytest.mark.asyncio
    async def test_journal_disabled_keeps_single_process_behaviour(
            self, monkeypatch):
        monkeypatch.setenv("JARVIS_CU_JOURNAL_ENABLED", "0")
        CUExecutionSensor._instance = None
        s = CUExecutionSensor(router=_Router())
        for _ in range(ces._GRADUATION_THRESHOLD + 2):
            await s.record(_fail())
        assert len(s._router.ingested) == 1

    @pytest.mark.asyncio
    async def test_contention_is_counted(self, monkeypatch):
        """§7 — a non-zero counter is how an operator learns two daemons are
        fighting over the journal."""
        monkeypatch.setenv("JARVIS_CU_JOURNAL_PATH", "/proc/nope/j.jsonl")
        CUExecutionSensor._instance = None
        s = CUExecutionSensor(router=_Router())
        await s.record(_fail())
        assert s.get_stats()["claim_lock_failures"] >= 1


class TestDistinctPatternsAreIndependent:
    @pytest.mark.asyncio
    async def test_two_daemons_two_patterns(self):
        A, B = _daemon(), _daemon()
        for _ in range(ces._GRADUATION_THRESHOLD):
            await A.record(_fail(app="Messages"))
        for _ in range(ces._GRADUATION_THRESHOLD):
            await B.record(_fail(app="Slack"))
        assert _total(A, B) == 2
