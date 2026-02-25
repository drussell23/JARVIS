# tests/integration/test_lease_contention.py
"""Test that two journal instances correctly contend for a single lease."""

import sqlite3
import tempfile
import time
import pytest
from pathlib import Path


@pytest.fixture
def short_tmp_path(request):
    """Short temp directory for consistent test paths.

    SQLite can fail with overly long paths, so we use /tmp directly.
    """
    import shutil

    short_base = tempfile.mkdtemp(prefix="jt_", dir="/tmp")
    p = Path(short_base)
    request.addfinalizer(lambda: shutil.rmtree(str(p), ignore_errors=True))
    return p


class TestLeaseContention:
    """Verify single-writer lease semantics between two OrchestrationJournal instances."""

    async def test_two_journals_only_one_wins(self, short_tmp_path):
        """When two journals compete for the same lease, only the first acquires it.

        The second journal's acquire_lease call should time out and return False
        because the first journal still holds a valid (non-expired) lease.
        """
        from backend.core.orchestration_journal import OrchestrationJournal
        import backend.core.orchestration_journal as mod

        db_path = short_tmp_path / "orchestration.db"

        j1 = OrchestrationJournal()
        await j1.initialize(db_path)
        j2 = OrchestrationJournal()
        await j2.initialize(db_path)

        # Shorten the acquire timeout so the test doesn't wait 20s
        old_timeout = mod.LEASE_ACQUIRE_TIMEOUT_S
        mod.LEASE_ACQUIRE_TIMEOUT_S = 2.0
        try:
            ok1 = await j1.acquire_lease("holder_1")
            ok2 = await j2.acquire_lease("holder_2")
            assert ok1 is True, "First journal should acquire the lease"
            assert ok2 is False, "Second journal should fail to acquire while first holds it"
        finally:
            mod.LEASE_ACQUIRE_TIMEOUT_S = old_timeout

        await j1.close()
        await j2.close()

    async def test_crashed_leader_replaced(self, short_tmp_path):
        """A crashed leader (closed without release) can be replaced after TTL expiry.

        Simulates a crash by closing the journal without releasing the lease,
        then backdates the lease renewal timestamp to simulate TTL expiry.
        A second journal should then successfully take over with an incremented epoch.
        """
        from backend.core.orchestration_journal import OrchestrationJournal
        import backend.core.orchestration_journal as mod

        db_path = short_tmp_path / "orchestration.db"

        # Leader 1 acquires then "crashes" (close without release)
        j1 = OrchestrationJournal()
        await j1.initialize(db_path)
        ok1 = await j1.acquire_lease("leader_1")
        assert ok1 is True, "Leader 1 should acquire the lease"
        epoch1 = j1.epoch
        assert epoch1 >= 1, "Epoch should be at least 1 after first acquisition"
        await j1.close()  # "crash" -- lease not released, just connection closed

        # Backdate lease to simulate TTL expiry
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE lease SET last_renewed=? WHERE id=1",
            (time.time() - mod.LEASE_TTL_S - 5.0,),
        )
        conn.commit()
        conn.close()

        # Leader 2 takes over after TTL expiry
        j2 = OrchestrationJournal()
        await j2.initialize(db_path)
        ok2 = await j2.acquire_lease("leader_2")
        assert ok2 is True, "Leader 2 should acquire the lease after TTL expiry"
        assert j2.epoch == epoch1 + 1, (
            f"Epoch should increment on takeover: expected {epoch1 + 1}, got {j2.epoch}"
        )

        # Leader 2 can write with its fenced epoch
        seq = j2.fenced_write(
            "recovery", "control_plane", payload={"reason": "leader_replaced"}
        )
        assert seq >= 1, "fenced_write should return a valid sequence number"

        await j2.close()
