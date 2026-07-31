"""Regression spine for the memory corpus authority + admission ledger.

Each test pins a behaviour that was WRONG before this arc, not merely one
that exists. The two that matter most:

* ``test_untracked_ghost_is_not_a_topic`` — the router loaded 764 files where
  git tracked 383, hash-deduplicated the byte-identical copies, and left 301
  DIVERGED snapshots standing as first-class topics. This builds that exact
  shape (a tracked topic plus an ignored copy with different content) and
  proves only the tracked one survives. A test that merely counted files
  would have passed the whole time the bug was live.

* ``test_unknown_drift_is_never_penalised`` — the epistemic invariant. An
  unmeasured staleness verdict must weigh exactly as much as it did before
  staleness existed; penalising on absence of evidence silently reranks
  every topic older than the scan window.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import memory_corpus as mc
from backend.core.ouroboros.governance.memory_admission import (
    AdmissionDecision,
    AdmissionReason,
    AdmissionRecord,
    AdmissionRow,
    MemoryConsumer,
    latest_record,
    render_admission_lines,
    reset_default_registry,
)


@pytest.fixture(autouse=True)
def _clean():
    mc.reset_caches_for_tests()
    reset_default_registry()
    yield
    mc.reset_caches_for_tests()
    reset_default_registry()


def _git(repo: Path, *args: str, at: str = "") -> None:
    """Run git, optionally pinning the commit timestamp.

    ``%ct`` is second-granular, so two commits made in the same second are
    indistinguishable to the drift comparison. Real history rarely does that;
    a test making two commits back-to-back always does. Pinning the date
    makes the ordering the test claims to exercise actually exist.
    """
    env = None
    if at:
        import os
        env = {**os.environ, "GIT_AUTHOR_DATE": at, "GIT_COMMITTER_DATE": at}
    subprocess.run(["git", *args], cwd=str(repo), check=True,
                   capture_output=True, env=env)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A real git repo with a tracked topic and an ignored conflict copy."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.t")
    _git(tmp_path, "config", "user.name", "t")

    topics = tmp_path / "docs" / "memory_topics" / "ouroboros"
    topics.mkdir(parents=True)
    (tmp_path / "orchestrator.py").write_text("x = 1\n")
    (topics / "project_a.md").write_text(
        "---\nmodules: [orchestrator.py]\n---\n\n# A\n\ncurrent body\n")
    (tmp_path / ".gitignore").write_text("*\\ 2\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed", at="2026-01-01T00:00:00+00:00")

    # The ghost: same name, same frontmatter, DIVERGED body — exactly what an
    # iCloud conflict copy is, and exactly what hash-dedup cannot catch.
    ghost = tmp_path / "docs" / "memory_topics" / "ouroboros 2"
    ghost.mkdir()
    (ghost / "project_a.md").write_text(
        "---\nmodules: [orchestrator.py]\n---\n\n# A\n\nSTALE body\n")
    return tmp_path


# ---------------------------------------------------------------------------
# Corpus authority
# ---------------------------------------------------------------------------


def test_untracked_ghost_is_not_a_topic(repo: Path) -> None:
    listing = mc.corpus_listing_sync(repo)
    assert listing.provenance is mc.CorpusProvenance.GIT_TRACKED
    assert listing.size == 1
    assert listing.excluded == 1
    assert all("ouroboros 2" not in str(p) for p in listing.paths)


def test_router_loads_only_the_declared_corpus(repo: Path, monkeypatch) -> None:
    """The end-to-end claim: the ghost never reaches a fragment list."""
    monkeypatch.setenv("JARVIS_MEMORY_ROUTING_ENABLED", "1")
    from backend.core.ouroboros.governance.module_routing import (
        _load_topic_fragments_worker,
    )
    fragments, listing = _load_topic_fragments_worker(
        str(repo / "docs" / "memory_topics"), str(repo))
    assert len(fragments) == 1
    assert "STALE body" not in fragments[0].summary
    assert listing.excluded == 1


def test_walk_fallback_is_stamped_not_silent(tmp_path: Path) -> None:
    """No git → a listing that SAYS it is unverified, never a confident one."""
    topics = tmp_path / "docs" / "memory_topics" / "d"
    topics.mkdir(parents=True)
    (topics / "t.md").write_text("# T\n")
    listing = mc.corpus_listing_sync(tmp_path)
    assert listing.provenance is mc.CorpusProvenance.WALK_FALLBACK
    assert listing.degraded is True
    assert listing.size == 1


def test_git_empty_answer_does_not_erase_a_visible_corpus(
    repo: Path, monkeypatch,
) -> None:
    """Git saying "none" while files exist must not read as an empty mind."""
    monkeypatch.setattr(mc, "_run_git", lambda *a, **k: "")
    listing = mc.corpus_listing_sync(repo)
    assert listing.provenance is mc.CorpusProvenance.WALK_FALLBACK
    assert listing.size >= 1


def test_authority_flag_off_restores_the_tree_walk(repo: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_MEMORY_CORPUS_AUTHORITY", "0")
    listing = mc.corpus_listing_sync(repo)
    assert listing.provenance is mc.CorpusProvenance.WALK_FALLBACK
    assert listing.size == 2  # the ghost is back, as the rollback promises


def test_absent_topics_dir_is_absent_not_empty(tmp_path: Path) -> None:
    listing = mc.corpus_listing_sync(tmp_path)
    assert listing.provenance is mc.CorpusProvenance.ABSENT


# ---------------------------------------------------------------------------
# Referential staleness
# ---------------------------------------------------------------------------


def test_drift_detects_a_subject_that_moved_after_the_topic(repo: Path) -> None:
    readings = mc.drift_readings_sync(
        repo, [("docs/memory_topics/ouroboros/project_a.md", ["orchestrator.py"])])
    reading = readings["docs/memory_topics/ouroboros/project_a.md"]
    assert reading.drift is mc.Drift.FRESH  # same commit

    (repo / "orchestrator.py").write_text("x = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "move the subject",
         at="2026-02-01T00:00:00+00:00")
    mc.reset_caches_for_tests()

    readings = mc.drift_readings_sync(
        repo, [("docs/memory_topics/ouroboros/project_a.md", ["orchestrator.py"])])
    reading = readings["docs/memory_topics/ouroboros/project_a.md"]
    assert reading.drift is mc.Drift.DRIFTED
    assert reading.newest_subject == "orchestrator.py"
    assert reading.rank_multiplier == pytest.approx(mc.staleness_penalty())


def test_same_second_tie_reads_fresh_not_drifted(repo: Path) -> None:
    """Ambiguity does not penalise — the same rule UNKNOWN follows.

    ``%ct`` is second-granular, so a topic committed alongside its subject
    (the common case: one commit touching both) and a topic committed one
    second apart are indistinguishable. Resolving a tie to DRIFTED would
    demote every topic written in the same commit as the code it documents,
    which is exactly the topic most likely to be correct.
    """
    topic = "docs/memory_topics/ouroboros/project_a.md"
    tie = {topic: 1_700_000_000, "orchestrator.py": 1_700_000_000}
    assert mc.drift_for(repo, topic, ["orchestrator.py"], tie).drift \
        is mc.Drift.FRESH


def test_orphaned_when_every_declared_module_is_gone(repo: Path) -> None:
    readings = mc.drift_readings_sync(
        repo, [("docs/memory_topics/ouroboros/project_a.md", ["deleted_thing.py"])])
    assert readings["docs/memory_topics/ouroboros/project_a.md"].drift \
        is mc.Drift.ORPHANED


def test_unbound_when_a_topic_declares_no_modules(repo: Path) -> None:
    readings = mc.drift_readings_sync(
        repo, [("docs/memory_topics/ouroboros/project_a.md", [])])
    assert readings["docs/memory_topics/ouroboros/project_a.md"].drift \
        is mc.Drift.UNBOUND


def test_unknown_drift_is_never_penalised(repo: Path) -> None:
    """The epistemic invariant: absence of evidence does not rerank."""
    unknown = mc.drift_for(repo, "x.md", ["orchestrator.py"], None)
    assert unknown.drift is mc.Drift.UNKNOWN
    assert unknown.rank_multiplier == 1.0

    from backend.core.ouroboros.governance.module_routing import _drift_multiplier
    for verdict in ("unknown", "fresh", "unbound", "orphaned"):
        assert _drift_multiplier(verdict) == 1.0
    assert _drift_multiplier("drifted") == pytest.approx(mc.staleness_penalty())


def test_bounded_scan_still_decides_a_one_sided_comparison(repo: Path) -> None:
    """A topic outside the window vs a subject inside it is DECIDABLY drifted.

    This is the property that makes the bounded history walk affordable
    without making it useless.
    """
    touch = {"orchestrator.py": 9_000_000}  # topic absent from the map
    reading = mc.drift_for(
        repo, "docs/memory_topics/ouroboros/project_a.md",
        ["orchestrator.py"], touch)
    assert reading.drift is mc.Drift.DRIFTED

    touch = {"docs/memory_topics/ouroboros/project_a.md": 9_000_000}
    reading = mc.drift_for(
        repo, "docs/memory_topics/ouroboros/project_a.md",
        ["orchestrator.py"], touch)
    assert reading.drift is mc.Drift.FRESH

    assert mc.drift_for(repo, "a.md", ["orchestrator.py"], {}).drift \
        is mc.Drift.UNKNOWN


def test_ambiguous_bare_module_name_is_not_guessed(repo: Path) -> None:
    """Two files share a basename → unresolved, never a confident wrong pick."""
    for sub in ("pkg_a", "pkg_b"):
        d = repo / sub
        d.mkdir()
        (d / "dup.py").write_text("y = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "two files, one name")
    mc.reset_caches_for_tests()
    assert mc._resolve_declared(repo, "dup.py") is None


def test_touch_map_is_memoised_on_head(repo: Path) -> None:
    calls = {"n": 0}
    real = mc._last_touch_map

    def counting(root):
        calls["n"] += 1
        return real(root)

    mc._last_touch_map = counting  # type: ignore[assignment]
    try:
        mc._touch_map_cached(repo)
        mc._touch_map_cached(repo)
        assert calls["n"] == 1
        mc.reset_caches_for_tests()
        mc._touch_map_cached(repo)  # served from the persisted file
        assert calls["n"] == 1
    finally:
        mc._last_touch_map = real  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Admission ledger
# ---------------------------------------------------------------------------


def _row(uri: str, admitted: bool, score: float, chars: int = 100) -> AdmissionRow:
    return AdmissionRow(
        source_id=uri, uri=uri, content_hash="h",
        decision=(AdmissionDecision.ADMITTED if admitted
                  else AdmissionDecision.WITHHELD),
        reason=(AdmissionReason.SEMANTIC if admitted
                else AdmissionReason.RANK_BELOW_CUTOFF),
        score=score, chars=chars,
    )


def test_row_cap_drops_withheld_never_admitted(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_MEMORY_ADMISSION_MAX_ROWS", "4")
    rows = [_row(f"a{i}.md", True, 1.0) for i in range(6)] + \
           [_row(f"w{i}.md", False, 0.1 * i) for i in range(50)]
    rec = AdmissionRecord.of(
        op_id="o", consumer=MemoryConsumer.MAIN, rows=rows,
        corpus_size=56, corpus_provenance="git_tracked", corpus_excluded=0,
        char_budget=1000,
    )
    assert rec.admitted_count == 6
    assert len([r for r in rec.rows if r.admitted]) == 6
    assert rec.rows_withheld_from_record == 50


def test_record_reports_the_corpus_it_was_offered() -> None:
    rec = AdmissionRecord.of(
        op_id="o", consumer=MemoryConsumer.REVIEW, rows=[_row("a.md", True, 1.0)],
        corpus_size=383, corpus_provenance="git_tracked", corpus_excluded=381,
        char_budget=2000,
    )
    payload = rec.as_payload()
    assert payload["corpus"] == {"size": 383, "provenance": "git_tracked",
                                 "excluded": 381}
    assert payload["consumer"] == "review"


def test_consumer_coercion_never_borrows_main_identity() -> None:
    assert MemoryConsumer.coerce("explore") is MemoryConsumer.EXPLORE
    assert MemoryConsumer.coerce("nonsense") is MemoryConsumer.UNKNOWN
    assert MemoryConsumer.coerce(None) is MemoryConsumer.UNKNOWN


def test_empty_pass_still_files_a_record(repo: Path, monkeypatch) -> None:
    """"Nothing loaded" needs a WHY, and only a record can carry it."""
    monkeypatch.setenv("JARVIS_MEMORY_ROUTING_ENABLED", "1")
    from backend.core.ouroboros.governance.module_routing import ModuleContextRouter

    router = ModuleContextRouter(
        repo, topics_dir=repo / "docs" / "memory_topics" / "nope")
    ctx = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        router.route([], "q", op_id="empty-op"))
    assert ctx.section == ""
    rec = latest_record()
    assert rec is not None and rec.op_id == "empty-op"
    assert rec.corpus_provenance == "absent"


def test_render_distinguishes_no_pass_from_nothing_loaded() -> None:
    blank = "\n".join(render_admission_lines())
    assert "no routing pass recorded" in blank

    rec = AdmissionRecord.of(
        op_id="o", consumer=MemoryConsumer.MAIN,
        rows=[_row("w.md", False, 0.1)],
        corpus_size=10, corpus_provenance="git_tracked", corpus_excluded=0,
        char_budget=100,
    )
    loaded = "\n".join(render_admission_lines(rec))
    assert "nothing loaded" in loaded
    assert "no routing pass recorded" not in loaded


def test_render_flags_a_degraded_corpus() -> None:
    rec = AdmissionRecord.of(
        op_id="o", consumer=MemoryConsumer.MAIN, rows=[_row("a.md", True, 1.0)],
        corpus_size=684, corpus_provenance="walk_fallback", corpus_excluded=0,
        char_budget=2000,
    )
    assert "⚠" in "\n".join(render_admission_lines(rec))


def test_disabled_ledger_says_disabled_not_empty(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_MEMORY_ADMISSION_ENABLED", "0")
    assert "disabled" in "\n".join(render_admission_lines())


def test_latest_record_tracks_recency_not_insertion_order() -> None:
    from backend.core.ouroboros.governance.memory_admission import record_admission

    for op in ("old-op", "new-op"):
        record_admission(AdmissionRecord.of(
            op_id=op, consumer=MemoryConsumer.MAIN,
            rows=[_row("a.md", True, 1.0)], corpus_size=1,
            corpus_provenance="git_tracked", corpus_excluded=0,
            char_budget=100,
        ))
    # Re-route the OLDER op: the newest record wins, not the first-seen op.
    record_admission(AdmissionRecord.of(
        op_id="old-op", consumer=MemoryConsumer.MAIN,
        rows=[_row("b.md", True, 1.0)], corpus_size=1,
        corpus_provenance="git_tracked", corpus_excluded=0, char_budget=100,
    ))
    rec = latest_record()
    assert rec is not None and rec.op_id == "old-op"


# ---------------------------------------------------------------------------
# Surface
# ---------------------------------------------------------------------------


def test_memory_context_verb_is_not_read_as_a_search_term() -> None:
    from backend.core.ouroboros.battle_test.memory_surface import (
        compose_memory_lines,
    )
    out = "\n".join(compose_memory_lines("context"))
    assert "memory · context" in out
    assert "topics matching" not in out
