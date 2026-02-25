# tests/integration/test_crash_recovery.py
"""Test that journal state survives crash and can be rebuilt via replay."""

import sqlite3
import tempfile
import time
import pytest
from pathlib import Path


@pytest.fixture
def short_tmp_path(request):
    """Short temp directory for consistent test paths."""
    short_base = tempfile.mkdtemp(prefix="jt_", dir="/tmp")
    p = Path(short_base)
    import shutil
    request.addfinalizer(lambda: shutil.rmtree(str(p), ignore_errors=True))
    return p


class TestCrashRecovery:
    async def test_journal_survives_close_and_reopen(self, short_tmp_path):
        """Write entries, close journal, reopen and verify entries are recoverable."""
        from backend.core.orchestration_journal import OrchestrationJournal
        import backend.core.orchestration_journal as mod
        db_path = short_tmp_path / "orchestration.db"

        # Session 1: Write some entries
        j1 = OrchestrationJournal()
        await j1.initialize(db_path)
        await j1.acquire_lease("session_1")
        epoch1 = j1.epoch

        j1.fenced_write("state_transition", "comp_a", payload={"to": "STARTING"})
        j1.fenced_write("state_transition", "comp_a", payload={"to": "READY"})
        j1.fenced_write("state_transition", "comp_b", payload={"to": "STARTING"})

        await j1.close()  # Simulate crash -- lease not released

        # Backdate lease to simulate TTL expiry
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE lease SET last_renewed=? WHERE id=1",
            (time.time() - mod.LEASE_TTL_S - 5.0,),
        )
        conn.commit()
        conn.close()

        # Session 2: Reopen and rebuild state via replay
        j2 = OrchestrationJournal()
        await j2.initialize(db_path)
        ok = await j2.acquire_lease("session_2")
        assert ok is True
        assert j2.epoch == epoch1 + 1

        # Replay all entries from the beginning
        entries = await j2.replay_from(0)
        assert len(entries) >= 3

        # Filter by target
        comp_a_entries = await j2.replay_from(0, target_filter=["comp_a"])
        assert len(comp_a_entries) >= 2
        actions = [e["action"] for e in comp_a_entries]
        assert all(a == "state_transition" for a in actions)

        # Filter by action
        transitions = await j2.replay_from(0, action_filter=["state_transition"])
        targets = [e["target"] for e in transitions]
        assert "comp_a" in targets
        assert "comp_b" in targets

        await j2.close()

    async def test_component_state_persists_across_restart(self, short_tmp_path):
        """Component state table survives close and reopen."""
        from backend.core.orchestration_journal import OrchestrationJournal
        import backend.core.orchestration_journal as mod
        db_path = short_tmp_path / "orchestration.db"

        # Session 1: Set component state
        j1 = OrchestrationJournal()
        await j1.initialize(db_path)
        await j1.acquire_lease("session_1")

        seq = j1.fenced_write("state_transition", "comp_a", payload={"to": "READY"})
        j1.update_component_state("comp_a", "READY", seq)

        await j1.close()

        # Backdate lease
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE lease SET last_renewed=? WHERE id=1",
            (time.time() - mod.LEASE_TTL_S - 5.0,),
        )
        conn.commit()
        conn.close()

        # Session 2: Verify state persisted
        j2 = OrchestrationJournal()
        await j2.initialize(db_path)
        await j2.acquire_lease("session_2")

        state = j2.get_component_state("comp_a")
        assert state is not None
        assert state["status"] == "READY"
        assert state["last_seq"] == seq

        # Can also get all states (returns dict keyed by component name)
        all_states = j2.get_all_component_states()
        assert len(all_states) >= 1
        assert "comp_a" in all_states
        assert all_states["comp_a"]["status"] == "READY"

        await j2.close()

    async def test_idempotency_key_prevents_duplicate_after_recovery(self, short_tmp_path):
        """Idempotency keys survive restart and prevent duplicate writes."""
        from backend.core.orchestration_journal import OrchestrationJournal
        import backend.core.orchestration_journal as mod
        db_path = short_tmp_path / "orchestration.db"

        # Session 1: Write with idempotency key
        j1 = OrchestrationJournal()
        await j1.initialize(db_path)
        await j1.acquire_lease("session_1")

        idem_key = "start:comp_a:unique123"
        seq1 = j1.fenced_write(
            "state_transition", "comp_a",
            payload={"to": "STARTING"},
            idempotency_key=idem_key,
        )
        assert seq1 >= 1

        await j1.close()

        # Backdate lease
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE lease SET last_renewed=? WHERE id=1",
            (time.time() - mod.LEASE_TTL_S - 5.0,),
        )
        conn.commit()
        conn.close()

        # Session 2: Try to write with same idempotency key
        j2 = OrchestrationJournal()
        await j2.initialize(db_path)
        await j2.acquire_lease("session_2")

        # Same idempotency key should return the original seq (deduplicated)
        seq2 = j2.fenced_write(
            "state_transition", "comp_a",
            payload={"to": "STARTING"},
            idempotency_key=idem_key,
        )
        assert seq2 == seq1  # Should be the original sequence number

        # Verify only one entry with this target+payload exists (not counting lease entries)
        entries = await j2.replay_from(0, target_filter=["comp_a"])
        # The idempotency_key column is not returned in replay results,
        # so we verify deduplication by checking total count of comp_a entries
        state_transitions = [e for e in entries if e["action"] == "state_transition"]
        assert len(state_transitions) == 1

        await j2.close()
