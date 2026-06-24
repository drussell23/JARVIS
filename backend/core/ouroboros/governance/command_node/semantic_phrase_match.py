"""Biometric-Semantic Binding -- local ASR + WER phrase-match (Phase 3).

This CLOSES the H1 security finding. Phase 2 verified the *speaker* (the
ECAPA-TDNN voice biometric) but NOT *what was said* -- a replayed /
stitched recording of the operator's voice that produced a high ECAPA
score could authorize even if it did not utter the live challenge phrase.
Phase 3 binds the spoken content to the audio: the operator must utter
the randomized live challenge phrase, transcribed by the LOCAL Whisper
ASR and matched (>= 90% by default) against the expected phrase.

AIR-GAP / PRIVACY
=================
The ASR is the EXISTING local Whisper model
(``backend.voice_unlock.ml_engine_registry`` -> ``get_engine("whisper")``,
cached under ``MLConfig.CACHE_DIR/whisper``). NO third-party cloud API is
ever called -- the audio never leaves the host. The heavy whisper/torch
deps are LAZY-imported inside the adapter so this module imports cleanly
in a bare test env (a ``transcribe_fn`` is injectable for tests so no real
audio / Whisper runs there).

The transcript is NEVER persisted. Only ``sha256(transcript)`` reaches the
audit ledger (the caller computes / records it). WER is a pure-stdlib
function -- no heavy dependency.

FAIL-CLOSED ABSOLUTE
====================
Any transcription error, empty transcript, timeout, or exception ->
``(False, ...)``. There is no code path that returns ``True`` on an error.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

logger = logging.getLogger("CommandNode.SemanticPhraseMatch")

# --- env knobs (no hardcoding) --------------------------------------------

# Default WER threshold: 0.10 == >= 90% match. A minor stutter / static is a
# small WER (a few percent), not a lockout; a wrong / replayed phrase is a
# large WER and is rejected.
_DEFAULT_WER_THRESHOLD = 0.10
# Bounded transcription -- never let an ASR hang the write-path.
_DEFAULT_TRANSCRIBE_TIMEOUT_S = 20.0


def _wer_threshold() -> float:
    """``JARVIS_COMMAND_NODE_WER_THRESHOLD`` (default 0.10). Clamped to
    ``[0.0, 1.0]``. Any parse error -> the safe default."""
    try:
        val = float(os.environ.get(
            "JARVIS_COMMAND_NODE_WER_THRESHOLD",
            str(_DEFAULT_WER_THRESHOLD),
        ))
    except (TypeError, ValueError):
        return _DEFAULT_WER_THRESHOLD
    if val < 0.0:
        return 0.0
    if val > 1.0:
        return 1.0
    return val


def _transcribe_timeout_s() -> float:
    """``JARVIS_COMMAND_NODE_TRANSCRIBE_TIMEOUT_S`` (default 20s)."""
    try:
        val = float(os.environ.get(
            "JARVIS_COMMAND_NODE_TRANSCRIBE_TIMEOUT_S",
            str(_DEFAULT_TRANSCRIBE_TIMEOUT_S),
        ))
    except (TypeError, ValueError):
        return _DEFAULT_TRANSCRIBE_TIMEOUT_S
    return val if val > 0.0 else _DEFAULT_TRANSCRIBE_TIMEOUT_S


# --- normalization + WER (pure stdlib) ------------------------------------

# Strip everything that is not a word char or whitespace. Punctuation,
# casing, and runs of whitespace must NOT affect the match.
_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)


def _normalize_tokens(text: str) -> Tuple[str, ...]:
    """Lowercase, strip punctuation, collapse whitespace, tokenize to
    words. Deterministic and total (never raises)."""
    if not isinstance(text, str):
        return ()
    lowered = text.lower()
    no_punct = _PUNCT_RE.sub(" ", lowered)
    return tuple(no_punct.split())


def _levenshtein(a: Tuple[str, ...], b: Tuple[str, ...]) -> int:
    """Token-level Levenshtein edit distance via a small pure-Python DP
    (two-row, O(len(a)*len(b)) time, O(min) space). No heavy dep."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    # Ensure the inner (column) dimension is the shorter for less memory.
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            cur[j] = min(
                prev[j] + 1,        # deletion
                cur[j - 1] + 1,     # insertion
                prev[j - 1] + cost,  # substitution / match
            )
        prev = cur
    return prev[-1]


def word_error_rate(hypothesis: str, reference: str) -> float:
    """Token-level WER = Levenshtein(hyp_tokens, ref_tokens) / len(ref_tokens).

    Pure stdlib. Case-insensitive, punctuation-insensitive,
    whitespace-collapsing. ``0.0`` == perfect; a total mismatch is ``>= 1.0``.

    Edge cases (fail-CLOSED -- a degenerate input yields a HIGH error, never
    a spurious 0.0):
      * empty reference + empty hypothesis -> 0.0 (both nothing == match)
      * empty reference + non-empty hypothesis -> 1.0 (all insertions; never
        divide by zero, never report a perfect match for unexpected speech)
    """
    hyp = _normalize_tokens(hypothesis)
    ref = _normalize_tokens(reference)
    if not ref:
        # No reference words: only a (normalized) empty hypothesis matches.
        return 0.0 if not hyp else 1.0
    dist = _levenshtein(hyp, ref)
    return dist / len(ref)


# --- the local Whisper transcription adapter (lazy) -----------------------


async def _default_transcribe_fn(audio: bytes, sample_rate: int) -> str:
    """Thin adapter over the EXISTING local Whisper engine.

    Lazy-imports the heavy ML deps INSIDE the call so this module imports
    cleanly in a bare test env. Resolves ``get_engine("whisper")`` from the
    ML engine registry (the cached ``whisper.load_model`` wrapped by
    ``WhisperWrapper``) and runs ``.transcribe(...)``.

    Whisper expects a float32 mono PCM numpy array normalized to ``[-1, 1]``;
    we decode the raw 16-bit little-endian PCM bytes accordingly. The model's
    ``.transcribe(...)`` returns a dict with a ``"text"`` key.

    Runs the blocking transcription off the event loop. Any import / runtime
    error propagates to ``asr_phrase_match`` which fails CLOSED.
    """
    import numpy as np  # local: heavy-ish, keep import lazy

    # Decode 16-bit signed little-endian PCM -> float32 [-1, 1].
    pcm = np.frombuffer(audio or b"", dtype=np.int16).astype(np.float32)
    if pcm.size:
        pcm = pcm / 32768.0

    # Resolve the EXISTING local Whisper engine (cached model). NO cloud.
    from backend.voice_unlock.ml_engine_registry import (  # noqa: E501
        get_ml_registry_sync,
    )
    registry = get_ml_registry_sync(auto_create=True)
    if registry is None:
        raise RuntimeError("ML engine registry unavailable")
    model = registry.get_engine("whisper")  # raises if not loaded

    def _run_sync() -> str:
        result = model.transcribe(pcm, language="en", fp16=False)
        if isinstance(result, dict):
            return str(result.get("text", "") or "")
        # Some wrappers return the text directly.
        return str(result or "")

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run_sync)


# --- the public phrase-match gate -----------------------------------------


async def asr_phrase_match(
    *,
    audio: bytes,
    sample_rate: int,
    expected_phrase: str,
    transcribe_fn: Optional[
        Callable[[bytes, int], Awaitable[str]]
    ] = None,
    wer_threshold: Optional[float] = None,
) -> Tuple[bool, Dict[str, Any]]:
    """Transcribe ``audio`` via the local Whisper ASR and PASS iff the
    transcript matches ``expected_phrase`` within ``wer_threshold``.

    Returns ``(passed, {transcript, wer, threshold})``.

    FAIL-CLOSED ABSOLUTE: a transcription error, empty / whitespace-only
    transcript, timeout, or ANY exception -> ``(False, {...})``. The
    transcription is bounded by a timeout
    (``JARVIS_COMMAND_NODE_TRANSCRIBE_TIMEOUT_S``).

    The ``transcribe_fn`` is injectable (tests pass a fake so no real
    Whisper / audio runs); the default lazily binds the EXISTING local
    Whisper engine.
    """
    threshold = wer_threshold if wer_threshold is not None else _wer_threshold()
    fn = transcribe_fn or _default_transcribe_fn

    info: Dict[str, Any] = {
        "transcript": "",
        "wer": 1.0,
        "threshold": threshold,
    }

    try:
        transcript = await asyncio.wait_for(
            _maybe_await_str(fn(audio, sample_rate)),
            timeout=_transcribe_timeout_s(),
        )
    except asyncio.TimeoutError:
        logger.error(
            "[SemanticPhraseMatch] transcription TIMED OUT -- fail-CLOSED",
        )
        return False, info
    except Exception:  # noqa: BLE001 -- FAIL-CLOSED on any ASR error
        logger.error(
            "[SemanticPhraseMatch] transcription raised -- fail-CLOSED",
            exc_info=True,
        )
        return False, info

    if not isinstance(transcript, str):
        transcript = "" if transcript is None else str(transcript)

    # Empty / whitespace-only transcript -> fail-CLOSED (never match).
    if not transcript.strip():
        info["transcript"] = ""
        info["wer"] = 1.0
        return False, info

    wer = word_error_rate(transcript, expected_phrase)
    info["transcript"] = transcript
    info["wer"] = wer
    passed = wer <= threshold
    return passed, info


async def _maybe_await_str(value: Any) -> Any:
    """Await ``value`` if it's awaitable; else return it. Lets a
    ``transcribe_fn`` be sync OR async."""
    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
        return await value
    return value


__all__ = [
    "asr_phrase_match",
    "word_error_rate",
]
