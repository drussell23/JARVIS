"""The audio of the sentence just spoken — held briefly, for one question.

WHY A HOLDER AND NOT AN ARGUMENT
----------------------------------
The obvious design threads the bytes through `route(command, screenshot,
audio)` down to whatever needs them. It does not survive contact with the
consent flow: a gated capability SUSPENDS, the turn ends, and the verdict
arrives out of band through `CapabilityRouter.resume` — a completely different
call stack with no relationship to the one that had the audio. Threading it
would mean either parking bytes inside the suspension record (audio living as
long as a 900-second consent TTL) or plumbing it through every intermediate
signature that has no business seeing it.

So the audio is deposited here and claimed by name, with a lifetime measured
in seconds rather than in call frames.

WHAT THIS REFUSES TO DO
-------------------------
* **Never writes to disk.** A file outlives the question it was captured to
  answer. The one thing worse than a Mac that will not unlock is a Mac with a
  folder full of recordings of its owner.
* **Never logs the bytes**, or any prefix of them. Sizes and hashes only.
* **Claim is destructive.** `claim()` removes what it returns, so one
  utterance answers one question. A verifier cannot be handed the same sample
  twice, which is the shape of a replay.
* **Expires on a clock, not on use.** If nothing ever claims it, it still goes
  away. The failure mode of a hold-until-claimed design is audio that lives
  forever because the consumer crashed.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("JARVIS.UtteranceAudio")

UTTERANCE_AUDIO_SCHEMA_VERSION: str = "utterance_audio.v1"

#: The only format the Swift recorder emits. Named so a future second format
#: must be added deliberately rather than arriving as an unvalidated string.
FORMAT_WAV16K = "wav16k_b64"


def max_bytes() -> int:
    """Largest utterance accepted, decoded. NEVER raises.

    12 seconds of 16 kHz mono PCM16 is ~384 KB, so this is generous by design
    and still bounded — the field arrives over a socket and a size limit is the
    cheapest defence against a payload that is not what it claims to be.
    """
    try:
        raw = (os.environ.get("JARVIS_UTTERANCE_MAX_BYTES", "") or "").strip()
        return max(1024, min(8 * 1024 * 1024, int(raw))) if raw else 2 * 1024 * 1024
    except (TypeError, ValueError):
        return 2 * 1024 * 1024


def ttl_s() -> float:
    """How long a held utterance stays claimable. NEVER raises.

    Long enough to cover speaker verification on a warm model plus the IPC
    round-trip; far too short for the audio to still be around when the next
    person sits down at the machine.
    """
    try:
        raw = (os.environ.get("JARVIS_UTTERANCE_TTL_S", "") or "").strip()
        return max(1.0, min(120.0, float(raw))) if raw else 30.0
    except (TypeError, ValueError):
        return 30.0


@dataclass
class Utterance:
    """One captured sentence. The bytes, and nothing that identifies them."""

    audio: bytes
    fmt: str
    captured_at: float
    #: First 8 hex chars of the sha256. For correlating a verification result
    #: with the sample it came from in a log, without the log containing any
    #: part of the sample.
    digest: str = ""

    @property
    def age_s(self) -> float:
        return max(0.0, time.time() - self.captured_at)

    @property
    def expired(self) -> bool:
        return self.age_s > ttl_s()

    def __repr__(self) -> str:  # never let bytes reach a log by accident
        return (f"<Utterance {len(self.audio)}B {self.fmt} "
                f"sha={self.digest} age={self.age_s:.1f}s>")


class UtteranceHolder:
    """Holds at most ONE utterance. NEVER raises.

    One rather than a queue: the question being asked is always "who just
    spoke", and a backlog of older samples is a set of answers to questions
    nobody is asking any more.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: Optional[Utterance] = None
        self._deposits = 0
        self._claims = 0
        self._rejected = 0

    def deposit(self, audio_b64: str, fmt: str = FORMAT_WAV16K) -> bool:
        """Take custody of a base64 utterance. NEVER raises.

        Returns whether it was accepted. Rejection is silent to the operator
        and loud in the log: a malformed sample means verification will be
        impossible in a moment, and the reason wants to be findable then.
        """
        try:
            if not audio_b64:
                return False
            if fmt != FORMAT_WAV16K:
                self._rejected += 1
                logger.warning("[Utterance] rejected unknown format %r", fmt)
                return False
            # Validate BEFORE holding. Storing bytes that turn out not to be
            # decodable means the failure surfaces at verification time, which
            # is the worst possible moment to discover it.
            try:
                raw = base64.b64decode(audio_b64, validate=True)
            except (binascii.Error, ValueError):
                self._rejected += 1
                logger.warning("[Utterance] rejected undecodable base64 "
                               "(%d chars)", len(audio_b64))
                return False
            if not raw or len(raw) > max_bytes():
                self._rejected += 1
                logger.warning("[Utterance] rejected %d bytes (limit %d)",
                               len(raw), max_bytes())
                return False
            if not raw.startswith(b"RIFF"):
                self._rejected += 1
                logger.warning("[Utterance] rejected: not a RIFF/WAVE payload")
                return False
            u = Utterance(audio=raw, fmt=fmt, captured_at=time.time(),
                          digest=hashlib.sha256(raw).hexdigest()[:8])
            with self._lock:
                self._held = u
                self._deposits += 1
            logger.info("[Utterance] held %s", u)
            return True
        except Exception:  # noqa: BLE001 — audio never breaks a command
            logger.debug("[Utterance] deposit degraded", exc_info=True)
            return False

    def claim(self) -> Optional[Utterance]:
        """Take the held utterance, removing it. NEVER raises.

        Destructive by design — one sentence answers one question. Returns
        None when nothing is held or what was held has aged out, and those are
        the same answer to a caller: there is no evidence to verify.
        """
        try:
            with self._lock:
                u, self._held = self._held, None
            if u is None:
                return None
            if u.expired:
                logger.info("[Utterance] discarded %s — older than %.0fs",
                            u, ttl_s())
                return None
            self._claims += 1
            return u
        except Exception:  # noqa: BLE001
            return None

    def peek_age_s(self) -> Optional[float]:
        """Age of what is held, without claiming it. NEVER raises.

        For answering "could this be verified right now?" without consuming
        the evidence that would answer it.
        """
        try:
            with self._lock:
                u = self._held
            return None if u is None or u.expired else u.age_s
        except Exception:  # noqa: BLE001
            return None

    def drop(self) -> None:
        """Forget whatever is held. NEVER raises."""
        try:
            with self._lock:
                self._held = None
        except Exception:  # noqa: BLE001
            pass

    def stats(self) -> dict:
        with self._lock:
            held = self._held
        return {
            "schema_version": UTTERANCE_AUDIO_SCHEMA_VERSION,
            "holding": held is not None and not held.expired,
            "deposits": self._deposits,
            "claims": self._claims,
            "rejected": self._rejected,
            "ttl_s": ttl_s(),
            "max_bytes": max_bytes(),
        }


_HOLDER: Optional[UtteranceHolder] = None
_HOLDER_LOCK = threading.Lock()


def get_utterance_holder() -> UtteranceHolder:
    """Process-wide holder. NEVER raises."""
    global _HOLDER
    with _HOLDER_LOCK:
        if _HOLDER is None:
            _HOLDER = UtteranceHolder()
        return _HOLDER


def reset_utterance_holder() -> None:
    """Testing seam. NEVER raises."""
    global _HOLDER
    with _HOLDER_LOCK:
        _HOLDER = None
