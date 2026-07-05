"""TDD spine for Domain-1 Staging-0 Task 2 -- lineage stamping.

Covers the durable per-source Lamport counter (``EmitSequence``) and the
publish-ready envelope (``stamp_delta`` + ``DeltaLineage``). The counter is
the ONE fail-CLOSED surface in this module: a monotonic-uniqueness guarantee
that cannot lock must never hand out a possibly-duplicate seq (mirrors the
``PersistentTokenBucket`` billing-guard posture).

Still Body-local, still NO source content on any field (Mandate 1).
"""
from __future__ import annotations

import json
import logging
import threading
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.governance.causal.structural_delta import (
    DeltaLineage,
    EmitSequence,
    StructuralDelta,
    SymbolRecord,
    compute_file_delta,
    stamp_delta,
)

REPO = "jarvis"


# ---------------------------------------------------------------------------
# EmitSequence -- durable monotonic per-repo counter.
# ---------------------------------------------------------------------------

def test_next_strictly_increases_per_repo(tmp_path) -> None:
    path = str(tmp_path / "causal_emit_seq")
    seq = EmitSequence(path=path)
    got = [seq.next(REPO) for _ in range(5)]
    assert got == sorted(got), "seq must be monotonic"
    assert len(set(got)) == len(got), "seq must be strictly increasing (no dupes)"
    assert got[0] < got[1] < got[2] < got[3] < got[4]


def test_new_instance_continues_from_disk(tmp_path) -> None:
    """Persistence, NOT memory: a fresh instance on the same path resumes."""
    path = str(tmp_path / "causal_emit_seq")
    first = EmitSequence(path=path)
    a = first.next(REPO)
    b = first.next(REPO)

    reopened = EmitSequence(path=path)
    c = reopened.next(REPO)
    assert c > b > a, "a new instance must continue from the persisted max, not restart"


def test_two_repos_have_independent_sequences(tmp_path) -> None:
    path = str(tmp_path / "causal_emit_seq")
    seq = EmitSequence(path=path)
    j1 = seq.next("jarvis")
    p1 = seq.next("prime")
    j2 = seq.next("jarvis")
    p2 = seq.next("prime")
    assert j1 == p1, "each repo starts its own sequence"
    assert j2 == p2
    assert j2 > j1 and p2 > p1


def test_env_path_resolution(tmp_path, monkeypatch) -> None:
    """path=None -> JARVIS_CAUSAL_EMIT_SEQ_PATH."""
    target = tmp_path / "env_seq"
    monkeypatch.setenv("JARVIS_CAUSAL_EMIT_SEQ_PATH", str(target))
    seq = EmitSequence()
    seq.next(REPO)
    assert target.exists(), "env-resolved path must be the journal written to"


def test_next_runs_inside_the_section(tmp_path, monkeypatch) -> None:
    """Recording monkeypatch of flock_critical_section: EVERY next() acquires
    the section, and the append lands strictly BETWEEN enter and exit (while
    the real lock is held -- an unlocked read + locked append is a TOCTOU)."""
    from backend.core.ouroboros.governance import cross_process_jsonl as cpj

    path = tmp_path / "causal_emit_seq"

    def _lines() -> int:
        if not path.exists():
            return 0
        return sum(1 for ln in path.read_text().splitlines() if ln.strip())

    real_section = cpj.flock_critical_section
    events: List[Dict[str, Any]] = []

    @contextmanager
    def recording_section(p, **kwargs):
        entry = {"path": str(p), "lines_at_enter": _lines()}
        with real_section(p, **kwargs) as acquired:
            yield acquired
            entry["lines_before_exit"] = _lines()
        events.append(entry)

    monkeypatch.setattr(cpj, "flock_critical_section", recording_section)
    seq = EmitSequence(path=str(path))
    assert seq.next(REPO) >= 1
    assert seq.next(REPO) >= 2
    assert len(events) == 2, "every next() must run under the section"
    for e in events:
        assert str(path) in e["path"]
        assert e["lines_before_exit"] == e["lines_at_enter"] + 1, (
            "the take append must land INSIDE the held section")


def test_fail_closed_when_lock_unavailable(tmp_path, monkeypatch, caplog) -> None:
    """A next() that cannot acquire the lock RAISES and appends nothing -- a
    uniqueness counter that cannot prove monotonicity must never guess."""
    from backend.core.ouroboros.governance import cross_process_jsonl as cpj

    path = tmp_path / "causal_emit_seq"

    @contextmanager
    def never_acquires(p, **kwargs):
        yield False

    monkeypatch.setattr(cpj, "flock_critical_section", never_acquires)
    seq = EmitSequence(path=str(path))
    with caplog.at_level(logging.ERROR):
        with pytest.raises(Exception):
            seq.next(REPO)
    assert not path.exists() or path.read_text().strip() == "", "no lock -> no trace"


def test_fail_closed_when_substrate_import_fails(tmp_path, monkeypatch) -> None:
    """If the locking substrate itself is unavailable, next() RAISES."""
    import builtins

    path = tmp_path / "causal_emit_seq"
    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name.endswith("cross_process_jsonl") or "cross_process_jsonl" in name:
            raise ImportError("simulated substrate outage")
        return real_import(name, *args, **kwargs)

    seq = EmitSequence(path=str(path))
    monkeypatch.setattr(builtins, "__import__", blocked_import)
    with pytest.raises(Exception):
        seq.next(REPO)


def test_concurrent_takes_never_duplicate(tmp_path) -> None:
    """Two EmitSequence INSTANCES racing next() on one journal at a 2-thread
    barrier: every minted seq is distinct (the flock serializes the
    read-max-increment-append so no two takes can read the same stale max)."""
    path = str(tmp_path / "causal_emit_seq")
    s1 = EmitSequence(path=path)
    s2 = EmitSequence(path=path)
    barrier = threading.Barrier(2)
    results: List[int] = []
    lock = threading.Lock()

    def race(seq: EmitSequence) -> None:
        barrier.wait()
        got = seq.next(REPO)
        with lock:
            results.append(got)

    t1 = threading.Thread(target=race, args=(s1,))
    t2 = threading.Thread(target=race, args=(s2,))
    t1.start(); t2.start(); t1.join(timeout=30); t2.join(timeout=30)

    assert len(results) == 2
    assert len(set(results)) == 2, (
        "two racing takes must never mint the same seq: %r" % results)


# ---------------------------------------------------------------------------
# stamp_delta -- the publish-ready envelope, still NO content.
# ---------------------------------------------------------------------------

def _sample_delta() -> StructuralDelta:
    before = "def alpha(x):\n    return x + 1\n"
    after = "def alpha(x):\n    return x + 1\n\ndef beta(y):\n    return y - 1\n"
    return compute_file_delta(REPO, "mod.py", before, after)


def test_stamp_delta_round_trips() -> None:
    delta = _sample_delta()
    lineage = DeltaLineage(
        repo=REPO,
        head_sha="a" * 40,
        parent_sha="b" * 40,
        merge_base="c" * 40,
        emit_seq=7,
    )
    env = stamp_delta(delta, lineage)
    assert env["delta"] == delta.to_dict()
    assert env["lineage"] == asdict(lineage)
    assert env["lineage"]["emit_seq"] == 7
    # JSON-serializable end to end.
    restored = json.loads(json.dumps(env))
    assert restored["lineage"]["head_sha"] == "a" * 40
    assert StructuralDelta.from_dict(restored["delta"]).repo == REPO


def test_stamp_delta_carries_no_content() -> None:
    """The magic token planted in source must be ABSENT from the envelope
    JSON -- the no-content invariant survives lineage stamping."""
    magic = "MAGIC_SECRET_BODY_TOKEN_XYZZY"
    before = "def alpha(x):\n    return x + 1\n"
    after = (
        "def alpha(x):\n"
        "    # %s\n"
        "    return x + 1\n"
        "\n"
        "def beta(y):\n"
        "    return %s\n"
    ) % (magic, magic)
    delta = compute_file_delta(REPO, "mod.py", before, after)
    lineage = DeltaLineage(
        repo=REPO, head_sha="d" * 40, parent_sha="e" * 40,
        merge_base="f" * 40, emit_seq=1,
    )
    blob = json.dumps(stamp_delta(delta, lineage))
    assert magic not in blob, "source content leaked into the publish envelope"


def test_delta_lineage_is_frozen() -> None:
    lineage = DeltaLineage(
        repo=REPO, head_sha="0" * 40, parent_sha="1" * 40,
        merge_base="2" * 40, emit_seq=3,
    )
    with pytest.raises(Exception):
        lineage.emit_seq = 99  # type: ignore[misc]
