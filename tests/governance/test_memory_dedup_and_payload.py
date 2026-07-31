"""Identity is the payload; the wire has a ceiling.

Two invariants, both about a loader trusting the wrong thing.

The router walked `rglob("*.md")` and injected whatever the filesystem held,
so an iCloud conflict copy was a SECOND topic — paying full tokens in a
GENERATE prompt to repeat what the first one already said. Filtering the NAME
would have fixed one cause; hashing the PAYLOAD immunises against every cause
at once, because none of them can change what the document is.

The surface sends counts and a capped hit list, never the corpus. These pin
that it stays that way.
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# 1 — content-addressed deduplication
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_identical_payloads_under_different_names_load_once(
        tmp_path: Path) -> None:
    """The mandate, stated exactly: same content, unrelated names, one topic.

    The two filenames share no substring a pattern could key on — no
    ` 2` suffix, no `copy`, nothing. That is the point: this must hold for a
    duplicate that no naming convention could have caught, which is what
    separates a content hash from the filter it replaces.
    """
    from backend.core.ouroboros.governance.module_routing import (
        _load_topic_fragments_worker,
    )

    body = (
        "---\nmodules: [backend/core/ouroboros/governance/orchestrator.py]\n---\n"
        "# Blast Radius Provenance\n\nUnknown is never zero.\n"
    )
    topics = tmp_path / "docs" / "memory_topics" / "ouroboros"
    topics.mkdir(parents=True)
    (topics / "project_blast_provenance.md").write_text(body, encoding="utf-8")
    (topics / "zzz_unrelated_filename.md").write_text(body, encoding="utf-8")

    frags, _listing = _load_topic_fragments_worker(
        str(tmp_path / "docs" / "memory_topics"), str(tmp_path))

    assert len(frags) == 1, (
        f"corpus incremented twice for one document: "
        f"{[f.uri for f in frags]}"
    )


@pytest.mark.asyncio
async def test_transport_artefacts_do_not_defeat_the_hash(
        tmp_path: Path) -> None:
    """CRLF and trailing whitespace are not authorship.

    A sync client or a Windows checkout rewrites line endings without
    touching a word. Hashing raw bytes would call those different documents
    and defeat the deduplication entirely — the normalisation is what makes
    the hash an identity rather than a checksum.
    """
    from backend.core.ouroboros.governance.module_routing import (
        _load_topic_fragments_worker,
    )

    body = "# Posture\n\nEXPLORE, CONSOLIDATE, HARDEN, MAINTAIN.\n"
    topics = tmp_path / "t"
    topics.mkdir()
    (topics / "a.md").write_text(body, encoding="utf-8")
    (topics / "b.md").write_text(
        body.replace("\n", "\r\n") + "   \n", encoding="utf-8")

    frags, _listing = _load_topic_fragments_worker(str(topics), str(tmp_path))
    assert len(frags) == 1, "CRLF/trailing-space copy loaded as a second topic"


@pytest.mark.asyncio
async def test_genuinely_different_topics_both_survive(tmp_path: Path) -> None:
    """The guard against a hash so aggressive it eats real memory.

    A deduplicator that merged distinct documents would silently delete
    knowledge — a worse failure than the duplication it fixes, and a silent
    one. Two topics differing by a single sentence must both load.
    """
    from backend.core.ouroboros.governance.module_routing import (
        _load_topic_fragments_worker,
    )

    topics = tmp_path / "t"
    topics.mkdir()
    (topics / "a.md").write_text("# A\n\nOne fact.\n", encoding="utf-8")
    (topics / "b.md").write_text("# A\n\nOne fact. And another.\n",
                                 encoding="utf-8")

    frags, _listing = _load_topic_fragments_worker(str(topics), str(tmp_path))
    assert len(frags) == 2, "distinct documents were merged — memory was lost"


# ---------------------------------------------------------------------------
# 2 — the payload never floods the socket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_memory_payload_respects_the_byte_ceiling() -> None:
    """Every form of the verb stays under the cap, against the REAL corpus.

    Run against this repo's own 383 topics rather than a fixture, because the
    question is not whether the clamp works on synthetic input — it is
    whether the verb's actual composition stays bounded as the corpus grows.
    """
    from backend.core.ouroboros.battle_test.memory_surface import (
        _MAX_PAYLOAD_BYTES, compose_memory_lines,
    )

    for arg in ("", "project", "advisor", "e", "a"):
        rows = compose_memory_lines(arg)
        size = sum(len(r.encode("utf-8")) + 1 for r in rows)
        assert size <= _MAX_PAYLOAD_BYTES, (
            f"/memory topics {arg!r} put {size} bytes on the wire "
            f"(cap {_MAX_PAYLOAD_BYTES})"
        )


@pytest.mark.asyncio
async def test_truncation_announces_itself() -> None:
    """A clamped payload must say so.

    A silent tail-drop reads as "that is all there is", which is the one
    thing a memory surface must never imply. Asserted at a tiny cap so the
    marker is forced regardless of corpus size.
    """
    from backend.core.ouroboros.battle_test.memory_surface import _clamp_payload

    rows = [f"  row {i} " + "x" * 40 for i in range(50)]
    out = _clamp_payload(rows, limit=256)

    assert len(out) < len(rows), "nothing was truncated at a 256-byte cap"
    assert "truncated" in out[-1], f"truncation was silent: {out[-1]!r}"
    assert sum(len(r.encode()) + 1 for r in out[:-1]) <= 256


@pytest.mark.asyncio
async def test_the_corpus_is_what_the_repo_tracks() -> None:
    """Untracked files are not memory.

    The root fix. iCloud conflict copies live in the working tree and are
    gitignored; asking git rather than the filesystem excludes them — and
    every other class of stray — without a pattern list to maintain.
    """
    from backend.core.ouroboros.battle_test.memory_surface import topic_counts

    counts = topic_counts()
    assert counts, "no topics found at all"
    for domain in counts:
        assert not domain.endswith((" 2", " 3")), (
            f"an untracked sync copy entered the corpus: {domain!r}"
        )
