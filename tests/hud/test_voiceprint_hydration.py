"""Decoding a voiceprint, without guessing what it is.

The same profile is stored twice at different sizes — 768 bytes locally, 1538
in CloudSQL. A cosine similarity does not fail loudly on a misread buffer: it
returns a plausible number, and that number becomes "that didn't sound like
you". A statement about a person, produced by a type error.
"""
from __future__ import annotations

import numpy as np
import pytest

from backend.hud.voiceprint_hydration import (
    KNOWN_DIMENSIONS, UnreadableVoiceprint, describe, hydrate,
    hydrate_or_none,
)


def _blob(dim: int = 192, dtype: str = "float32", header: int = 0) -> bytes:
    v = np.linspace(-1.0, 1.0, dim).astype(dtype)
    return (b"\x00" * header) + v.tobytes()


# ── The mandate ─────────────────────────────────────────────────────────────

def test_a_768_byte_float32_blob_hydrates_to_the_expected_shape_and_dtype():
    """THE ASSERTION ASKED FOR.

    768 bytes is 192 float32s — ECAPA-TDNN's native width, and exactly what
    the local SQLite profile holds.
    """
    raw = _blob(192, "float32")
    assert len(raw) == 768

    h = hydrate(raw, dtype="float32")

    assert h.dimension == 192
    assert h.source_dtype == "float32"
    assert h.shape == (192,)
    assert h.vector.dtype == np.float32
    assert np.isfinite(h.vector).all()


def test_the_values_survive_the_round_trip_exactly():
    """Shape and dtype are not enough — a vector can be the right shape and
    still be nonsense."""
    v = np.linspace(-1.0, 1.0, 192).astype("float32")
    h = hydrate(v.tobytes(), dtype="float32")
    assert np.array_equal(h.vector, v)


def test_the_result_is_writable_and_owns_its_memory():
    """`np.frombuffer` returns a READ-ONLY view onto the source buffer.
    Handing that to a model that writes in place is a corruption bug that
    surfaces somewhere else entirely, so the copy is load-bearing."""
    raw = _blob()
    h = hydrate(raw)
    assert h.vector.flags.writeable
    assert h.vector.base is None or h.vector.flags.owndata
    h.vector[0] = 42.0                      # must not raise, must not corrupt
    assert hydrate(raw).vector[0] != 42.0


def test_upcasting_to_float64_is_exact():
    """float32 -> float64 is lossless; every float32 has an exact float64
    representation, so widening cannot move a vector."""
    v = np.linspace(-1.0, 1.0, 192).astype("float32")
    h = hydrate(v.tobytes(), dtype="float64")
    assert h.vector.dtype == np.float64
    assert np.array_equal(h.vector, v.astype("float64"))


# ── Refusing, rather than reinterpreting ────────────────────────────────────

def test_a_length_matching_nothing_is_refused_not_guessed():
    """The whole point. A misread buffer produces a confident wrong answer
    about who is speaking, so an unrecognisable length must raise."""
    with pytest.raises(UnreadableVoiceprint) as e:
        hydrate(b"\x01" * 777)
    assert "refusing to reinterpret" in str(e.value)


def test_an_empty_blob_is_refused():
    with pytest.raises(UnreadableVoiceprint):
        hydrate(b"")
    with pytest.raises(UnreadableVoiceprint):
        hydrate(None)


def test_a_blob_of_non_finite_values_is_refused():
    """NaNs in an embedding make every cosine NaN, which compares False
    against every threshold — a silent, total verification failure."""
    v = np.full(192, np.nan, dtype="float32")
    with pytest.raises(UnreadableVoiceprint):
        hydrate(v.tobytes())


def test_the_narrower_reading_wins_when_a_length_is_ambiguous():
    """768 bytes is 192 float32 AND 96 float64. A writer producing 768 bytes
    for a 192-d model was writing float32, so that is the reading."""
    h = hydrate(_blob(192, "float32"))
    assert (h.dimension, h.source_dtype) == (192, "float32")


def test_a_small_header_is_tolerated_when_the_dimension_is_declared():
    """CloudSQL's 1538 is 1536 + 2 — a real header, not corruption. It is
    only readable once the dimension is declared, because 1538 also reads as
    384 float32 + 2."""
    h = hydrate(_blob(192, "float64", header=2), dtype="float64",
                expect_dimension=192)
    assert h.dimension == 192 and h.source_dtype == "float64"
    assert h.header_bytes == 2


def test_the_vector_is_read_from_the_END_when_a_header_is_present():
    """A header sits at the FRONT. Slicing from the front would shift every
    element by the header width and produce a finite, entirely wrong vector."""
    v = np.linspace(-1.0, 1.0, 192).astype("float64")
    h = hydrate(b"\xde\xad" + v.tobytes(), dtype="float64",
                expect_dimension=192)
    assert np.array_equal(h.vector, v)


def test_a_large_remainder_is_not_called_a_header():
    with pytest.raises(UnreadableVoiceprint):
        hydrate(_blob(192, "float32", header=64), expect_dimension=192)


def test_an_ambiguous_length_is_refused_rather_than_settled_by_precedence():
    """1538 bytes reads as 384 float32 + 2 AND 192 float64 + 2. Picking one by
    candidate order is a coin toss that produces a wrong vector half the time;
    the profile schema records `embedding_dimension` precisely so nobody has
    to guess."""
    # 1536 bytes exactly: 384 float32 OR 192 float64. Refused by name.
    with pytest.raises(UnreadableVoiceprint) as e:
        hydrate(_blob(192, "float64"))
    assert "ambiguous" in str(e.value)

    # 1538 (CloudSQL's actual size) is not an exact multiple of anything, so
    # unguided it is simply unreadable — a different refusal, same discipline.
    raw = _blob(192, "float64", header=2)
    with pytest.raises(UnreadableVoiceprint):
        hydrate(raw)
    assert hydrate(raw, dtype="float64", expect_dimension=192).dimension == 192


# ── Whatever the driver hands back ──────────────────────────────────────────

def test_a_memoryview_from_psycopg2_reads_the_same_as_sqlite_bytes():
    """psycopg2 returns memoryview for bytea, sqlite3 returns bytes — the same
    profile arrives as two types depending on which store answered."""
    raw = _blob()
    assert np.array_equal(hydrate(raw).vector,
                          hydrate(memoryview(raw)).vector)
    assert np.array_equal(hydrate(raw).vector,
                          hydrate(bytearray(raw)).vector)


def test_a_hex_encoded_blob_is_understood():
    raw = _blob()
    assert np.array_equal(hydrate("\\x" + raw.hex()).vector, hydrate(raw).vector)


# ── Diagnostics ─────────────────────────────────────────────────────────────

def test_describe_reports_shape_without_committing_to_a_reading():
    d = describe(_blob(192, "float32"))
    assert d["bytes"] == 768 and d["readable"]
    assert any("192-d float32" in c for c in d["candidates"])


def test_describe_says_so_when_nothing_fits():
    assert describe(b"\x00" * 777)["readable"] is False


def test_the_non_raising_form_returns_none_rather_than_a_wrong_vector():
    assert hydrate_or_none(b"\x00" * 777) is None
    assert hydrate_or_none(_blob()) is not None


@pytest.mark.parametrize("dim", KNOWN_DIMENSIONS)
def test_every_declared_dimension_round_trips(dim):
    """Declared, because some widths collide: 384 float32 and 192 float64 are
    both 1536 bytes. The dimension comes from the profile row, not a guess."""
    h = hydrate(_blob(dim, "float32"), expect_dimension=dim)
    assert h.dimension == dim and h.shape == (dim,)


# ── The real profile on this machine ────────────────────────────────────────

def test_the_actual_local_voiceprint_hydrates():
    """Not a fixture. The 768-byte blob measured in
    ~/.jarvis/learning/jarvis_learning.db for "Derek J. Russell"."""
    import pathlib
    import sqlite3

    p = pathlib.Path.home() / ".jarvis/learning/jarvis_learning.db"
    if not p.exists():
        pytest.skip("no local learning database on this machine")
    con = sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "select voiceprint_embedding from speaker_profiles limit 1"
        ).fetchone()
    except sqlite3.Error:
        pytest.skip("no speaker_profiles table")
    finally:
        con.close()
    if row is None or row["voiceprint_embedding"] is None:
        pytest.skip("no stored voiceprint")

    h = hydrate(row["voiceprint_embedding"])
    assert h.dimension in KNOWN_DIMENSIONS
    assert np.isfinite(h.vector).all()
    assert h.vector.flags.writeable
