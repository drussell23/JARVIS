"""Speech becomes text in the prompt, without ever eating what you typed.

Karen already heard whole utterances and answered them; what she could not do
was put the words somewhere the operator could correct them first. The
protocol was already right — `audio_state_ipc` has emitted
`{"type":"transcript","chunk":…,"final":…,"utterance_id":…}` since it
shipped, and nothing consumed it.

The hard part is not the transport. The prompt buffer is SHARED: the operator
may be typing into it while chunks arrive, and both write to one string.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test.transcript_composer import (
    TranscriptComposer, parse_transcript_frame,
)


def _f(text: str, *, final: bool = False, uid: str = "u-1", seq: int = 0,
       role: str = "user") -> dict:
    return {"type": "transcript", "role": role, "text": text,
            "final": final, "utterance_id": uid, "seq": seq}


class _Buf:
    """A buffer the composer edits, mirroring what the client does."""

    def __init__(self, text: str = "", cursor: int = -1) -> None:
        self.text = text
        self.cursor = len(text) if cursor < 0 else cursor

    def apply(self, result) -> None:
        if not result.edits:
            return
        self.text = self.text[:result.start] + result.text + self.text[result.end:]
        self.cursor = result.start + len(result.text)


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------

def test_partials_refine_in_place_instead_of_stacking() -> None:
    """One evolving sentence, not every revision the recogniser passed
    through."""
    c, buf = TranscriptComposer(), _Buf()
    for text in ("fix the", "fix the flaky", "fix the flaky test"):
        buf.apply(c.on_chunk(_f(text), buf.text, buf.cursor))
    assert buf.text == "fix the flaky test"


def test_the_final_releases_the_span() -> None:
    c, buf = TranscriptComposer(), _Buf()
    buf.apply(c.on_chunk(_f("fix the"), buf.text, buf.cursor))
    result = c.on_chunk(_f("fix the test", final=True), buf.text, buf.cursor)
    buf.apply(result)
    assert buf.text == "fix the test"
    assert result.released is True
    assert result.provisional is False
    assert c.active is False


def test_a_partial_is_marked_provisional() -> None:
    """So the surface can dim it — the operator should see what is still
    being revised."""
    c = TranscriptComposer()
    assert c.on_chunk(_f("fix"), "", 0).provisional is True


def test_a_second_utterance_appends_at_the_cursor() -> None:
    """A second hold continues where the operator is looking rather than
    overwriting the first."""
    c, buf = TranscriptComposer(), _Buf()
    buf.apply(c.on_chunk(_f("first thing", final=True), buf.text, buf.cursor))
    buf.text += " "
    buf.cursor = len(buf.text)
    buf.apply(c.on_chunk(_f("second thing", uid="u-2"), buf.text, buf.cursor))
    assert buf.text == "first thing second thing"


# --------------------------------------------------------------------------
# the shared buffer — the whole reason this is hard
# --------------------------------------------------------------------------

def test_typing_AFTER_the_span_is_never_overwritten() -> None:
    """The naive "replace the last N characters" deletes exactly this."""
    c, buf = TranscriptComposer(), _Buf()
    buf.apply(c.on_chunk(_f("fix the"), buf.text, buf.cursor))
    buf.text += " NOW"                      # operator types after
    buf.apply(c.on_chunk(_f("fix the flaky"), buf.text, buf.cursor))
    assert buf.text == "fix the flaky NOW", "typed text was eaten"


def test_typing_BEFORE_the_span_shifts_it_rather_than_clobbering() -> None:
    """The naive "replace from a stored offset" writes over their words the
    moment they add a character earlier in the line."""
    c, buf = TranscriptComposer(), _Buf()
    buf.apply(c.on_chunk(_f("the flaky"), buf.text, buf.cursor))
    buf.text = "please " + buf.text         # operator types before
    buf.apply(c.on_chunk(_f("the flaky test"), buf.text, buf.cursor))
    assert buf.text == "please the flaky test"


def test_editing_INSIDE_the_span_makes_it_theirs() -> None:
    """The composer does not guess. If its words are gone, it commits what
    is there and stops touching it — a dictation feature that occasionally
    eats a typed sentence is one nobody trusts twice."""
    c, buf = TranscriptComposer(), _Buf()
    buf.apply(c.on_chunk(_f("fix the flaky"), buf.text, buf.cursor))
    buf.text = "fix the STABLE"             # operator rewrites mid-span
    result = c.on_chunk(_f("fix the flaky test"), buf.text, buf.cursor)
    buf.apply(result)
    assert buf.text == "fix the STABLE", "the operator's edit was overwritten"
    assert result.released is True
    assert c.released_by_edit == 1


def test_an_ambiguous_relocation_is_refused() -> None:
    """Two copies of the same words mean we cannot tell which is ours, and
    guessing would rewrite a sentence the operator typed themselves."""
    c, buf = TranscriptComposer(), _Buf()
    buf.apply(c.on_chunk(_f("test"), buf.text, buf.cursor))
    # MOVED *and* duplicated. An intact span at its own offset is not
    # ambiguous — position confirms it — so the text has to shift as well.
    buf.text = "run test and test"
    result = c.on_chunk(_f("test again"), buf.text, buf.cursor)
    assert result.released is True
    assert result.edits is False


def test_a_cleared_buffer_does_not_resurrect_the_span() -> None:
    c, buf = TranscriptComposer(), _Buf()
    buf.apply(c.on_chunk(_f("fix the"), buf.text, buf.cursor))
    buf.text, buf.cursor = "", 0            # submitted or cleared
    result = c.on_chunk(_f("fix the test"), buf.text, buf.cursor)
    assert result.released is True
    assert result.edits is False


# --------------------------------------------------------------------------
# a socket promises nothing
# --------------------------------------------------------------------------

def test_an_out_of_order_partial_is_dropped() -> None:
    """A partial applied after a later one rewinds the sentence in front of
    the operator."""
    c, buf = TranscriptComposer(), _Buf()
    buf.apply(c.on_chunk(_f("fix the flaky", seq=2), buf.text, buf.cursor))
    result = c.on_chunk(_f("fix the", seq=1), buf.text, buf.cursor)
    buf.apply(result)
    assert buf.text == "fix the flaky"
    assert c.dropped_stale == 1


def test_a_duplicate_chunk_is_dropped() -> None:
    c, buf = TranscriptComposer(), _Buf()
    buf.apply(c.on_chunk(_f("fix", seq=1), buf.text, buf.cursor))
    assert c.on_chunk(_f("fix", seq=1), buf.text, buf.cursor).edits is False


def test_karens_own_speech_is_never_composed() -> None:
    """She is talking, not dictating. Her words have no business in the
    operator's prompt."""
    c = TranscriptComposer()
    assert c.on_chunk(_f("I'll fix that", role="karen"), "", 0).edits is False
    assert parse_transcript_frame(_f("hi", role="karen")) is None


def test_an_unidentifiable_frame_is_refused() -> None:
    """Without an id it cannot be sequenced, so applying it risks
    interleaving two utterances into one sentence."""
    assert parse_transcript_frame({"type": "transcript", "text": "x"}) is None


@pytest.mark.parametrize("junk", [None, {}, "string", 42, {"role": "user"}])
def test_junk_never_reaches_the_buffer(junk) -> None:
    assert TranscriptComposer().on_chunk(junk, "hello", 5).edits is False


def test_a_runaway_recogniser_is_bounded() -> None:
    c = TranscriptComposer(max_chars=50)
    result = c.on_chunk(_f("x" * 5000), "", 0)
    assert len(result.text) == 50


# --------------------------------------------------------------------------
# cancelling
# --------------------------------------------------------------------------

def test_cancel_removes_only_what_was_dictated() -> None:
    c, buf = TranscriptComposer(), _Buf("typed ")
    buf.cursor = len(buf.text)
    buf.apply(c.on_chunk(_f("spoken words"), buf.text, buf.cursor))
    assert buf.text == "typed spoken words"
    buf.apply(c.cancel(buf.text))
    assert buf.text == "typed ", "cancel ate the operator's own text"


def test_cancel_removes_the_span_even_when_typing_followed_it() -> None:
    """Appending after the dictation does not make the dictated words the
    operator's — they are still exactly what was spoken, so cancel takes
    back precisely those and leaves the typing alone."""
    c, buf = TranscriptComposer(), _Buf()
    buf.apply(c.on_chunk(_f("spoken"), buf.text, buf.cursor))
    buf.text = "spoken and then typed"
    buf.apply(c.cancel(buf.text))
    assert buf.text == " and then typed"


def test_cancel_keeps_text_the_operator_edited() -> None:
    """A cancel that deletes an edited sentence is worse than one that
    leaves a few stray words behind."""
    c, buf = TranscriptComposer(), _Buf()
    buf.apply(c.on_chunk(_f("spoken"), buf.text, buf.cursor))
    buf.text = "SPOKEN differently"          # edited INSIDE the span
    result = c.cancel(buf.text)
    buf.apply(result)
    assert buf.text == "SPOKEN differently"
    assert result.edits is False


def test_the_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_LIVE_TRANSCRIPT_ENABLED", "0")
    assert TranscriptComposer().on_chunk(_f("fix"), "", 0).edits is False


# --------------------------------------------------------------------------
# the producer side
# --------------------------------------------------------------------------

def test_the_synapse_accumulates_deltas_into_a_sentence() -> None:
    """The wire carries deltas; a cockpit replaces a SPAN and so needs the
    sentence so far. Accumulating in one place beats every consumer doing
    it — and the wire has no ordering field, so `seq` is added here too."""
    from backend.core.ouroboros.battle_test.audio_synapse import (
        RemoteAudioLease,
    )

    sent: list = []
    lease = RemoteAudioLease(
        lambda _s: None,
        lambda uid, text, **kw: sent.append((text, kw["final"], kw["seq"])),
    )
    for chunk, final in (("fix the ", False), ("flaky ", False),
                         ("test", True)):
        lease._on_message({
            "type": "transcript", "role": "user", "chunk": chunk,
            "final": final, "utterance_id": "u-1",
        })

    assert [t for t, _f, _s in sent] == [
        "fix the ", "fix the flaky ", "fix the flaky test",
    ]
    assert [s for _t, _f, s in sent] == [1, 2, 3]
    assert sent[-1][1] is True


def test_the_synapse_is_inert_without_a_sink() -> None:
    """Absent the sink, voice behaves exactly as it did before."""
    from backend.core.ouroboros.battle_test.audio_synapse import (
        RemoteAudioLease,
    )

    lease = RemoteAudioLease(lambda _s: None)
    lease._on_message({"type": "transcript", "role": "user", "chunk": "x",
                       "final": True, "utterance_id": "u-1"})


def test_an_unfinalised_utterance_cannot_grow_forever() -> None:
    """A dropped final or a crashed recogniser must not leak."""
    from backend.core.ouroboros.battle_test.audio_synapse import (
        RemoteAudioLease,
    )

    lease = RemoteAudioLease(lambda _s: None, lambda *_a, **_k: None)
    for i in range(50):
        lease._on_message({"type": "transcript", "role": "user", "chunk": "x",
                           "final": False, "utterance_id": f"u-{i}"})
    assert len(lease._utterances) <= 8
