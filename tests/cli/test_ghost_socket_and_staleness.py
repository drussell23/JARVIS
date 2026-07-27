"""Two ways an operator was left in the dark, both observed on 2026-07-27.

**A dead daemon's socket wedged `ov` permanently.** `probe_socket` classifies
a socket that exists and does not answer as "booting" — which is exactly what
a `kill` leaves behind. The client waited for a corpse, and the stale-socket
reaper lives inside the harness the client was refusing to start: the cleanup
was trapped inside the thing that could not boot. Only a human deleting a file
broke the cycle, which no operator would know to do.

**A daemon ran for 36 hours** carrying none of that day's merged fixes, and
nothing on screen said so. Two rounds of debugging went into a process that
structurally could not contain the fix.

Both are the same failure in different clothes: state that determines whether
anything works, with no way to observe it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from backend.core.ouroboros.battle_test.daemon_provenance import (
    read_provenance,
    staleness_line,
    write_provenance,
)

_REPO = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# 1. a socket nobody owns is a ghost, not a boot
# --------------------------------------------------------------------------

def test_booting_is_corroborated_against_a_live_process() -> None:
    """THE wedge. A `booting` claim with no live owner must fall through to
    the reap-and-ignite path instead of being waited on."""
    src = (_REPO / "backend/core/ouroboros/cli/thin_client.py").read_text()
    branch = src.split('if state == "booting":')[1][:1400]
    assert "_live_incumbent() is None" in branch, (
        "a booting claim is believed without checking whether anyone owns it"
    )
    assert 'state = "stale"' in branch, (
        "an unowned socket does not fall through to the ghost path"
    )


def test_the_operator_is_told_why_it_ignited() -> None:
    """Observability over silent reroute: a client that quietly reclassifies
    a socket teaches nobody anything."""
    src = (_REPO / "backend/core/ouroboros/cli/thin_client.py").read_text()
    assert "treating as a ghost and igniting" in src


def test_a_live_incumbent_is_still_waited_for() -> None:
    """The inverse, and it is load-bearing: cleaning a live-but-slow
    organism's socket makes it permanently unattachable — the 2026-07-23
    class this codebase already paid for once."""
    src = (_REPO / "backend/core/ouroboros/cli/thin_client.py").read_text()
    branch = src.split('if state == "booting":')[1][:1600]
    assert "organism already waking" in branch
    assert branch.index("_live_incumbent() is None") < \
        branch.index("organism already waking"), (
        "the liveness check must GATE the wait, not follow it"
    )


def test_one_authority_answers_who_is_home() -> None:
    """DRY: the reaper, the preflight and this check read the same lock, so
    they cannot disagree about whether a daemon exists."""
    src = (_REPO / "backend/core/ouroboros/cli/thin_client.py").read_text()
    assert "live_incumbent_pid" in src


# --------------------------------------------------------------------------
# 2. the daemon says what it is made of
# --------------------------------------------------------------------------

def test_a_stamp_records_commit_and_boot_time(tmp_path: Path) -> None:
    target = write_provenance(tmp_path / "p.json")
    assert target is not None and target.exists()
    stamp = read_provenance(target)
    assert stamp["pid"] > 0
    assert stamp["booted_at"] > 0
    assert isinstance(stamp["commit"], str)


def test_a_current_daemon_says_nothing(tmp_path: Path) -> None:
    """A banner that always shows is chrome, and gets read past exactly when
    it finally matters."""
    assert staleness_line(write_provenance(tmp_path / "p.json")) == ""


def test_an_old_daemon_on_an_old_commit_is_flagged(tmp_path: Path) -> None:
    """The 36-hour case."""
    stamp = tmp_path / "p.json"
    stamp.write_text(json.dumps({
        "pid": 1, "booted_at": time.time() - 129_600,
        "commit": "0" * 40, "branch": "main",
    }))
    line = staleness_line(stamp)
    assert "1d ago" in line
    assert "restart" in line, "the operator is told the state but not the fix"


@pytest.mark.parametrize("age,expected", [
    (90, "1m"), (7200, "2h"), (200_000, "2d"),
])
def test_age_reads_naturally(age: float, expected: str, tmp_path: Path) -> None:
    stamp = tmp_path / "p.json"
    stamp.write_text(json.dumps({
        "pid": 1, "booted_at": time.time() - age, "commit": "0" * 40,
    }))
    assert expected in staleness_line(stamp)


def test_a_missing_stamp_is_silent(tmp_path: Path) -> None:
    """An older daemon predates the stamp. Absence is not staleness, and
    guessing would cry wolf on every upgrade."""
    assert staleness_line(tmp_path / "absent.json") == ""


@pytest.mark.parametrize("body", [
    "", "not json", "[]", '{"booted_at": "yesterday"}', '{"commit": null}',
])
def test_a_corrupt_stamp_never_raises(body: str, tmp_path: Path) -> None:
    stamp = tmp_path / "p.json"
    stamp.write_text(body)
    assert isinstance(staleness_line(stamp), str)


def test_provenance_is_bounded_and_cannot_hang_a_boot() -> None:
    """It shells out to git. A banner that can stall an attach is worse than
    no banner."""
    import inspect

    from backend.core.ouroboros.battle_test import daemon_provenance

    src = inspect.getsource(daemon_provenance._git)
    assert "timeout=" in src


def test_it_is_fast_enough_to_sit_on_the_attach_path(tmp_path: Path) -> None:
    stamp = write_provenance(tmp_path / "p.json")
    started = time.perf_counter()
    staleness_line(stamp)
    assert time.perf_counter() - started < 2.0


# --------------------------------------------------------------------------
# 3. both ends are wired
# --------------------------------------------------------------------------

def test_the_daemon_stamps_at_boot() -> None:
    src = (_REPO / "backend/core/ouroboros/battle_test/harness.py").read_text()
    assert "write_provenance()" in src


def test_the_client_reports_before_it_trusts_anything() -> None:
    """It must print BEFORE the UI mounts — a stale daemon answers every verb
    with old behaviour while looking perfectly healthy."""
    src = (_REPO / "backend/core/ouroboros/cli/ov.py").read_text()
    assert "staleness_line()" in src
    assert src.index("staleness_line()") < src.index("PromptSession(")
