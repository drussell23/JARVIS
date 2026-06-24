"""Tests for the Phase 3 Biometric-Semantic Binding -- local ASR + WER
phrase-match.

WER is a pure stdlib function. ``asr_phrase_match`` is exercised with an
INJECTED fake ``transcribe_fn`` -- NO real Whisper / audio ever runs here
(the module must import cleanly without pulling whisper / torch). Every
error path is fail-CLOSED: a transcription error / empty transcript /
timeout -> (False, ...).
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.command_node import (
    semantic_phrase_match as spm,
)


# --- WER correctness ------------------------------------------------------


def test_wer_exact_match_is_zero():
    assert spm.word_error_rate(
        "the sovereign organism authorizes this mutation",
        "the sovereign organism authorizes this mutation",
    ) == 0.0


def test_wer_one_word_off_in_ten_is_about_point_one():
    ref = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
    # Substitute one word out of ten.
    hyp = "alpha bravo charlie delta echo foxtrot golf hotel india ZULU"
    wer = spm.word_error_rate(hyp, ref)
    assert abs(wer - 0.1) < 1e-9


def test_wer_total_mismatch_is_at_least_one():
    ref = "the immutable orange protocol still holds"
    hyp = "completely different unrelated spoken words here now"
    assert spm.word_error_rate(hyp, ref) >= 1.0


def test_wer_punctuation_and_case_insensitive():
    ref = "Voice gate open, blast radius acknowledged"
    hyp = "voice gate open blast radius acknowledged"
    assert spm.word_error_rate(hyp, ref) == 0.0
    # Heavy punctuation + casing must still match perfectly.
    hyp2 = "VOICE GATE OPEN... BLAST-RADIUS, ACKNOWLEDGED!!!"
    assert spm.word_error_rate(hyp2, ref) == 0.0


def test_wer_whitespace_collapse():
    ref = "live voice fresh nonce"
    hyp = "  live   voice\tfresh\n nonce  "
    assert spm.word_error_rate(hyp, ref) == 0.0


def test_wer_minor_stutter_is_small_not_lockout():
    # A repeated word (stutter) is one insertion in eight ref words.
    ref = "operator presence confirmed for cross repo elevation now"
    hyp = "operator operator presence confirmed for cross repo elevation now"
    wer = spm.word_error_rate(hyp, ref)
    assert 0.0 < wer < 0.2  # small, not a lockout


def test_wer_empty_reference_empty_hyp_is_zero():
    assert spm.word_error_rate("", "") == 0.0


def test_wer_empty_reference_nonempty_hyp_is_one():
    # Never divide by zero; never report a perfect match for unexpected
    # speech against an empty reference.
    assert spm.word_error_rate("unexpected words", "") == 1.0


def test_wer_empty_hypothesis_against_reference_is_one():
    assert spm.word_error_rate("", "alpha bravo charlie") == 1.0


# --- asr_phrase_match: PASS / FAIL on threshold ---------------------------


def _fake_transcribe(text: str):
    async def _fn(audio, sample_rate):  # noqa: ARG001
        return text
    return _fn


def test_asr_phrase_match_pass_on_exact():
    passed, info = asyncio.run(spm.asr_phrase_match(
        audio=b"\x00\x01", sample_rate=16000,
        expected_phrase="the immutable orange protocol still holds",
        transcribe_fn=_fake_transcribe(
            "the immutable orange protocol still holds"),
    ))
    assert passed is True
    assert info["wer"] == 0.0
    assert info["transcript"] == "the immutable orange protocol still holds"


def test_asr_phrase_match_pass_at_threshold_boundary():
    # 1 word off in 10 -> wer 0.1 == threshold 0.10 -> PASS (<=).
    ref = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
    hyp = "alpha bravo charlie delta echo foxtrot golf hotel india ZULU"
    passed, info = asyncio.run(spm.asr_phrase_match(
        audio=b"x", sample_rate=16000, expected_phrase=ref,
        transcribe_fn=_fake_transcribe(hyp), wer_threshold=0.10,
    ))
    assert passed is True
    assert abs(info["wer"] - 0.1) < 1e-9


def test_asr_phrase_match_fail_above_threshold():
    ref = "alpha bravo charlie delta echo"
    hyp = "alpha bravo WRONG WRONG WRONG"  # 3/5 = 0.6 wer
    passed, info = asyncio.run(spm.asr_phrase_match(
        audio=b"x", sample_rate=16000, expected_phrase=ref,
        transcribe_fn=_fake_transcribe(hyp), wer_threshold=0.10,
    ))
    assert passed is False
    assert info["wer"] > 0.10


def test_asr_phrase_match_env_threshold(monkeypatch):
    monkeypatch.setenv("JARVIS_COMMAND_NODE_WER_THRESHOLD", "0.5")
    ref = "alpha bravo charlie delta"
    hyp = "alpha bravo WRONG delta"  # 1/4 = 0.25 wer
    passed, info = asyncio.run(spm.asr_phrase_match(
        audio=b"x", sample_rate=16000, expected_phrase=ref,
        transcribe_fn=_fake_transcribe(hyp),
    ))
    assert passed is True  # 0.25 <= env 0.5
    assert info["threshold"] == 0.5


# --- asr_phrase_match: fail-CLOSED ----------------------------------------


def test_asr_phrase_match_transcribe_exception_fails_closed():
    async def _boom(audio, sample_rate):  # noqa: ARG001
        raise RuntimeError("whisper exploded")

    passed, info = asyncio.run(spm.asr_phrase_match(
        audio=b"x", sample_rate=16000, expected_phrase="anything at all",
        transcribe_fn=_boom,
    ))
    assert passed is False
    assert info["wer"] == 1.0
    assert info["transcript"] == ""


def test_asr_phrase_match_empty_transcript_fails_closed():
    passed, info = asyncio.run(spm.asr_phrase_match(
        audio=b"x", sample_rate=16000, expected_phrase="some phrase here",
        transcribe_fn=_fake_transcribe("   "),  # whitespace-only
    ))
    assert passed is False
    assert info["transcript"] == ""


def test_asr_phrase_match_timeout_fails_closed(monkeypatch):
    monkeypatch.setenv("JARVIS_COMMAND_NODE_TRANSCRIBE_TIMEOUT_S", "0.05")

    async def _slow(audio, sample_rate):  # noqa: ARG001
        await asyncio.sleep(5.0)
        return "too late"

    passed, info = asyncio.run(spm.asr_phrase_match(
        audio=b"x", sample_rate=16000, expected_phrase="some phrase",
        transcribe_fn=_slow,
    ))
    assert passed is False


def test_asr_phrase_match_sync_transcribe_fn_supported():
    def _sync(audio, sample_rate):  # noqa: ARG001 -- not a coroutine
        return "live voice fresh nonce"

    passed, _ = asyncio.run(spm.asr_phrase_match(
        audio=b"x", sample_rate=16000,
        expected_phrase="live voice fresh nonce",
        transcribe_fn=_sync,
    ))
    assert passed is True


def test_module_imports_without_whisper_or_torch():
    """The module must import cleanly without pulling heavy ML deps."""
    import sys
    import importlib

    mod = importlib.import_module(
        "backend.core.ouroboros.governance.command_node.semantic_phrase_match"
    )
    assert hasattr(mod, "asr_phrase_match")
    assert hasattr(mod, "word_error_rate")
    # Importing this module must not have imported whisper / torch.
    assert "whisper" not in sys.modules
    assert "torch" not in sys.modules
