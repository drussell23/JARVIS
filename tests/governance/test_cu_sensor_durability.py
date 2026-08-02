"""The evidence must survive the process that gathered it.

Deferring an emission until a router attaches survives a SLOW boot. It does not
survive the process dying first — and the window in which the router is missing
is exactly the window in which a boot is most likely to fail. So the failures a
user had just experienced were the ones most likely to be lost.

The intake router's WAL cannot close this: it persists envelopes AFTER ingest,
and the whole gap is before it.

EVENT-SOURCED, NOT SNAPSHOTTED
--------------------------------
The journal is append-only, reusing `cross_process_jsonl` — "the single source
of truth for cross-process JSONL append safety", already backing three ledgers.
Rebuilding by replaying occurrences means no read-modify-write on the hot path,
and the cooldown persists for free because an emission is just another entry.

THE TRAP THIS SUITE EXISTS FOR
--------------------------------
Persisting occurrences WITHOUT persisting emissions is worse than persisting
nothing: every restart would re-file every pattern that had already graduated,
so the durability fix would manufacture duplicate governance work.
`test_a_restart_does_not_refile_what_already_graduated` is the one to keep.
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
def _isolated_journal(tmp_path, monkeypatch):
    """Every test gets its own journal and a fresh singleton — otherwise one
    test's durable state is another's starting condition, which is exactly the
    bug class under test."""
    monkeypatch.setenv("JARVIS_CU_JOURNAL_PATH",
                       str(tmp_path / "cu_failure_journal.jsonl"))
    CUExecutionSensor._instance = None
    yield tmp_path / "cu_failure_journal.jsonl"
    CUExecutionSensor._instance = None


def _fail(app: str = "Messages", ts: Optional[float] = None) -> CUExecutionRecord:
    rec = CUExecutionRecord(
        goal="message alice saying hi", success=False, steps_completed=1,
        steps_total=3, elapsed_s=1.0, error="target not found",
        is_messaging=True, contact="alice", app=app,
    )
    if ts is not None:
        rec.timestamp = ts
    return rec


async def _settle(sensor=None):
    """Await the reconcile sweep.

    `asyncio.sleep(0)` used to be enough — reconcile finished inside one
    tick. It now awaits a journal write on a worker thread, so a single
    tick returns before the emission lands. `drain()` is deterministic;
    sleeping longer would only be flaky in a slower place.
    """
    s = sensor or CUExecutionSensor()
    await s.drain()


async def _fail_n(sensor, n: int, **kw):
    for _ in range(n):
        await sensor.record(_fail(**kw))


def _restart() -> CUExecutionSensor:
    """Simulate process death: drop the singleton, construct anew."""
    CUExecutionSensor._instance = None
    return CUExecutionSensor()


class TestSurvivingTheCrash:
    @pytest.mark.asyncio
    async def test_pre_boot_failures_survive_a_restart(self, _isolated_journal):
        """THE gap. Two failures, process dies before governance boots, the
        third failure after restart must still graduate."""
        s1 = CUExecutionSensor()
        await _fail_n(s1, ces._GRADUATION_THRESHOLD - 1)
        assert _isolated_journal.exists(), "nothing was journalled"

        s2 = _restart()
        assert s2.get_stats()["journal_replayed"] == ces._GRADUATION_THRESHOLD - 1
        router = _Router()
        s2._router = router
        await s2.record(_fail())          # the occurrence that completes it
        assert len(router.ingested) == 1, (
            "the pre-crash occurrences did not count toward graduation")

    @pytest.mark.asyncio
    async def test_a_pattern_that_graduated_pre_crash_emits_after_restart(
            self, _isolated_journal):
        """Threshold crossed with no router, then the process dies. The next
        boot must still file it."""
        s1 = CUExecutionSensor()
        await _fail_n(s1, ces._GRADUATION_THRESHOLD)
        assert s1.get_stats()["deferred_emissions"] >= 1

        s2 = _restart()
        router = _Router()
        CUExecutionSensor(router=router)
        await _settle()
        assert len(router.ingested) == 1
        assert s2 is CUExecutionSensor()

    @pytest.mark.asyncio
    async def test_a_restart_does_not_refile_what_already_graduated(
            self, _isolated_journal):
        """THE TRAP. Persisting occurrences without persisting EMISSIONS would
        re-file every graduated pattern on every restart — durability
        manufacturing duplicate governance work."""
        router = _Router()
        s1 = CUExecutionSensor(router=router)
        await _fail_n(s1, ces._GRADUATION_THRESHOLD)
        assert len(router.ingested) == 1

        s2 = _restart()
        router2 = _Router()
        CUExecutionSensor(router=router2)
        await _settle()
        assert router2.ingested == [], (
            "the restart re-filed a pattern that had already graduated")
        assert s2._last_emitted, "the cooldown did not survive the restart"


class TestTheJournalStaysHonest:
    @pytest.mark.asyncio
    async def test_entries_outside_the_window_are_not_replayed(
            self, _isolated_journal):
        old = time.time() - (ces._WINDOW_S + 60)
        s1 = CUExecutionSensor()
        for _ in range(ces._GRADUATION_THRESHOLD):
            await s1.record(_fail(ts=old))
        s2 = _restart()
        assert s2.get_stats()["journal_replayed"] == 0
        assert s2._failure_window == {}

    @pytest.mark.asyncio
    async def test_the_original_timestamp_survives(self, _isolated_journal):
        """A replayed occurrence must age from when the action failed, not from
        when it was read back."""
        ts = time.time() - 3600
        s1 = CUExecutionSensor()
        await s1.record(_fail(ts=ts))
        s2 = _restart()
        sig = _fail().failure_signature
        assert abs(s2._failure_window[sig][0] - ts) < 2.0

    @pytest.mark.asyncio
    async def test_a_future_timestamp_is_clamped(self, _isolated_journal):
        """A clock-skewed entry stamped in the future would never expire out of
        the rolling window."""
        s1 = CUExecutionSensor()
        await s1.record(_fail(ts=time.time() + 86_400 * 7))
        s2 = _restart()
        sig = _fail().failure_signature
        assert s2._failure_window[sig][0] <= time.time() + ces._MAX_FUTURE_SKEW_S + 1

    @pytest.mark.asyncio
    async def test_another_repos_journal_is_ignored(self, _isolated_journal):
        s1 = CUExecutionSensor()
        await _fail_n(s1, 1)
        with _isolated_journal.open("a") as fh:
            fh.write(json.dumps({"v": 1, "k": "f", "t": time.time(),
                                 "sig": "other:thing", "repo": "not-jarvis",
                                 "rec": {}}) + "\n")
        s2 = _restart()
        assert "other:thing" not in s2._failure_window


class TestTornAndHostileFiles:
    @pytest.mark.asyncio
    async def test_a_torn_final_line_does_not_lose_the_file(
            self, _isolated_journal):
        """A process killed mid-append leaves a partial last line. Discarding
        the whole journal for a missing byte would lose the evidence this
        exists to keep — so tolerance is per LINE, not per file."""
        s1 = CUExecutionSensor()
        await _fail_n(s1, 2)
        with _isolated_journal.open("a") as fh:
            fh.write('{"v":1,"k":"f","t":')          # torn
        s2 = _restart()
        assert s2.get_stats()["journal_replayed"] == 2
        assert s2.get_stats()["journal_corrupt_lines"] == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("junk", [
        "not json at all", "[]", "null", '{"k":"f"}', '{"t":"nan","sig":1}',
        '{"v":1,"k":"zzz","t":1,"sig":"x"}',
    ])
    async def test_hostile_lines_are_skipped_not_fatal(
            self, _isolated_journal, junk):
        _isolated_journal.parent.mkdir(parents=True, exist_ok=True)
        _isolated_journal.write_text(junk + "\n")
        s = _restart()                     # must not raise
        assert s.get_stats()["journal_corrupt_lines"] >= 0

    @pytest.mark.asyncio
    async def test_an_unwritable_journal_degrades_to_memory(
            self, tmp_path, monkeypatch):
        """Read-only disk must cost the sensor nothing but durability."""
        monkeypatch.setenv("JARVIS_CU_JOURNAL_PATH", "/proc/nope/journal.jsonl")
        CUExecutionSensor._instance = None
        router = _Router()
        s = CUExecutionSensor(router=router)
        await _fail_n(s, ces._GRADUATION_THRESHOLD)
        assert len(router.ingested) == 1, "a bad journal path broke the sensor"
        assert s.get_stats()["journal_write_failures"] >= 1

    @pytest.mark.asyncio
    async def test_the_master_switch_disables_persistence(
            self, _isolated_journal, monkeypatch):
        monkeypatch.setenv("JARVIS_CU_JOURNAL_ENABLED", "0")
        s = CUExecutionSensor()
        await _fail_n(s, 2)
        assert not _isolated_journal.exists()


class TestPrivacyAndBounds:
    @pytest.mark.asyncio
    async def test_redaction_keeps_the_pattern_and_drops_the_words(
            self, _isolated_journal, monkeypatch):
        """Holding a spoken goal in RAM for 24h and writing it to disk are
        different privacy postures, so the choice is explicit."""
        monkeypatch.setenv("JARVIS_CU_JOURNAL_REDACT", "1")
        s = CUExecutionSensor()
        await s.record(_fail())
        text = _isolated_journal.read_text()
        assert "alice" not in text
        assert "[redacted]" in text
        assert "messaging:messages" in text     # the pattern still survives

    @pytest.mark.asyncio
    async def test_unredacted_is_the_default_and_keeps_the_goal(
            self, _isolated_journal):
        s = CUExecutionSensor()
        await s.record(_fail())
        assert "alice" in _isolated_journal.read_text()

    @pytest.mark.asyncio
    async def test_compaction_bounds_the_file(self, _isolated_journal,
                                              monkeypatch):
        """The journal must not grow without limit across a long-lived install."""
        monkeypatch.setenv("JARVIS_CU_JOURNAL_MAX_LINES", "64")
        old = time.time() - (ces._WINDOW_S + 60)
        s1 = CUExecutionSensor()
        for i in range(80):
            await s1.record(_fail(ts=old))
        before = len(_isolated_journal.read_text().splitlines())
        _restart()                                    # hydrate triggers compact
        after = len(_isolated_journal.read_text().splitlines())
        assert before > 64 and after < before, (
            f"journal not compacted: {before} → {after}")

    @pytest.mark.asyncio
    async def test_stats_expose_durability(self, _isolated_journal):
        s = CUExecutionSensor()
        await _fail_n(s, 2)
        st = _restart().get_stats()
        assert st["journal_enabled"] is True
        assert st["journal_replayed"] == 2
        assert st["journal_write_failures"] == 0


class TestItComposesWithTheBootRace:
    @pytest.mark.asyncio
    async def test_crash_then_slow_boot_still_files_once(self, _isolated_journal):
        """Both failure modes at once: occurrences before a crash, restart with
        no router, then a late attach. Exactly one envelope."""
        s1 = CUExecutionSensor()
        await _fail_n(s1, ces._GRADUATION_THRESHOLD)

        _restart()                                    # crash + reboot
        router = _Router()
        CUExecutionSensor(router=router)              # governance boots late
        await _settle()
        assert len(router.ingested) == 1
        ev = router.ingested[0]
        assert ev.evidence["deferred_by_boot"] is True
