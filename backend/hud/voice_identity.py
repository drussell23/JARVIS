"""Is the person speaking the owner of this Mac?

THE AUTHORITY THAT WORKS THROUGH A LOCKED SCREEN
--------------------------------------------------
`unlock_screen` is APPROVAL_REQUIRED and Touch ID cannot answer for it: there
is no surface to draw a dialog on while the screen is locked, which is why the
verdict came back DENIED 99ms after the request and the log said "operator
declined" for a prompt nobody saw. A microphone works through a locked screen.
So the authority is the voice — and this is what turns a captured utterance
into a yes or a no.

FOUR ANSWERS, NOT TWO
-----------------------
"Deny" is not one state. An operator whose Mac will not unlock needs to know
WHICH of these happened, because the fix is different every time:

    NOT_ENROLLED    there is no voiceprint on this machine to compare against
    NOT_READY       the model has not finished loading
    NO_AUDIO        nothing captured the sentence
    REJECTED        it was compared, and it was not you

Collapsing them into "denied" is the same defect as the consent verdict that
reported "operator declined" for a prompt that was never drawn. Every one of
these fails CLOSED; they simply say why.

READINESS IS NOT ON THE COMMAND PATH
--------------------------------------
Measured: `SpeakerVerificationService.initialize()` did not complete in 150
seconds — speechbrain and torch loading a speaker-embedding model. Putting
that between "unlock my screen" and the screen unlocking is not a slow
feature, it is a broken one; the utterance it would verify expires first.

So warming is a background task started at boot, readiness is a state anyone
can ask about, and a verification attempted before the model is ready returns
NOT_READY immediately rather than blocking. The organism is allowed to be
still waking up, and is required to say so.

WHY THIS WRAPS RATHER THAN REIMPLEMENTS
-----------------------------------------
`SpeakerVerificationService` already owns embedding, adaptive thresholds,
enrolled profiles and continuous learning. This adds no model, no threshold
and no storage of its own — it is the seam between that service and the
consent boundary, plus the honest state machine the service does not have.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger("JARVIS.VoiceIdentity")

VOICE_IDENTITY_SCHEMA_VERSION: str = "voice_identity.v1"


def voice_authority_enabled() -> bool:
    """Master gate. Default TRUE. NEVER raises.

    Off does not mean "unlock without checking" — it means this authority is
    unavailable, and a capability that needs it is refused. There is
    deliberately no setting that turns a verified unlock into an unverified
    one.
    """
    return (os.environ.get("JARVIS_VOICE_AUTHORITY_ENABLED", "true")
            or "").strip().lower() not in ("0", "false", "no", "off")


def min_confidence() -> float:
    """Score an utterance must reach to be accepted. NEVER raises.

    A FLOOR, not the decision. `SpeakerVerificationService` computes its own
    adaptive threshold from enrollment quality and history, and this never
    lowers it — a verification the service already rejected stays rejected.
    This only refuses to accept a `verified: true` that arrived with a
    confidence the operator would not consider convincing for unlocking a
    computer.
    """
    try:
        raw = (os.environ.get("JARVIS_VOICE_MIN_CONFIDENCE", "") or "").strip()
        return max(0.0, min(1.0, float(raw))) if raw else 0.75
    except (TypeError, ValueError):
        return 0.75


def _enrollment_timeout_s() -> float:
    """How long the profile lookup may take. NEVER raises."""
    try:
        raw = (os.environ.get("JARVIS_ENROLLMENT_TIMEOUT_S", "") or "").strip()
        return max(5.0, min(180.0, float(raw))) if raw else 60.0
    except (TypeError, ValueError):
        return 60.0


def warm_timeout_s() -> float:
    """How long the background warm-up may take before it is called failed."""
    try:
        raw = (os.environ.get("JARVIS_VOICE_WARM_TIMEOUT_S", "") or "").strip()
        return max(10.0, min(900.0, float(raw))) if raw else 300.0
    except (TypeError, ValueError):
        return 300.0


class Readiness(str, enum.Enum):
    """Whether this authority can answer a question right now."""

    COLD = "cold"          # nothing has started it
    WARMING = "warming"    # loading; ask again shortly
    READY = "ready"
    FAILED = "failed"      # it tried and could not
    DISABLED = "disabled"


class Verdict(str, enum.Enum):
    """What the voice said about who is speaking."""

    VERIFIED = "verified"
    REJECTED = "rejected"
    NOT_ENROLLED = "not_enrolled"
    NOT_READY = "not_ready"
    NO_AUDIO = "no_audio"
    UNAVAILABLE = "unavailable"

    @property
    def approves(self) -> bool:
        """Only ONE value approves. Everything else fails closed."""
        return self is Verdict.VERIFIED


@dataclass
class Identification:
    """The result of asking who spoke."""

    verdict: str
    confidence: float = 0.0
    speaker: str = ""
    #: A sentence for a person. The operator hears this when unlock refuses,
    #: so it says what to DO, not what went wrong internally.
    spoken_reason: str = ""
    detail: str = ""
    elapsed_ms: float = 0.0
    #: sha256[:8] of the sample this judged, so a log line can be tied to an
    #: utterance without the log containing any of it.
    sample: str = ""
    schema_version: str = VOICE_IDENTITY_SCHEMA_VERSION

    @property
    def approves(self) -> bool:
        try:
            return Verdict(self.verdict).approves
        except Exception:  # noqa: BLE001 — an unreadable verdict is not consent
            return False


_SPOKEN: Dict[str, str] = {
    Verdict.NOT_ENROLLED.value:
        "I don't have a voiceprint for you on this Mac yet, so I can't "
        "confirm it's you. Enroll your voice and I'll be able to.",
    Verdict.NOT_READY.value:
        "I'm still loading my voice recognition — give me a moment and ask "
        "again.",
    Verdict.NO_AUDIO.value:
        "I didn't capture your voice clearly enough to check it was you.",
    Verdict.REJECTED.value:
        "That didn't sound like you, so I've left it locked.",
    Verdict.UNAVAILABLE.value:
        "I can't check who's speaking right now, so I've left it locked.",
}


class VoiceIdentity:
    """Warms the verifier, and answers who is speaking. NEVER raises."""

    def __init__(self, service: Any = None) -> None:
        self._service = service
        self._readiness = (Readiness.READY if service is not None
                           else Readiness.COLD)
        self._warm_task: Optional[asyncio.Task] = None
        self._warm_started = 0.0
        self._detail = ""
        self._counts: Dict[str, int] = {}
        #: Last answer from the profile store. None = nobody has asked yet,
        #: "" = asked and there is genuinely no profile. Never conflated.
        self._enrolled_cache: Optional[str] = None
        self._enrolled_at = 0.0
        self._refresh_task: Optional[asyncio.Task] = None

    # -- readiness -------------------------------------------------------

    @property
    def readiness(self) -> Readiness:
        if not voice_authority_enabled():
            return Readiness.DISABLED
        return self._readiness

    async def warm(self) -> None:
        """Resolve enrollment at boot. Does NOT load the model. NEVER raises.

        WHY THE MODEL IS NOT WARMED HERE ANY MORE
        -------------------------------------------
        It was, and it was the boot starvation. Measured from an OS thread
        that cannot itself be starved:

            worst loop round-trip lag : 13.82s
            VERDICT                   : WARM-UP STARVES THE LOOP

        which matches the 14.33s stall in the live boot log almost exactly.
        `SpeakerVerificationService.initialize()` is `async def`, but its body
        loads torch and speechbrain without ever yielding — so awaiting it
        stops the loop for fourteen seconds, during which JARVIS cannot hear,
        answer, or dispatch anything. I built the sentinel to catch that class
        of defect and then introduced one with it.

        The obvious fix does not work. `initialize()` creates
        `asyncio.Lock` and `asyncio.Queue` on whatever loop is running, so
        pushing it into a worker thread's loop would bind them there and break
        the moment `verify_speaker` is called from the main loop — precisely
        the "bound to a different event loop" failure just removed from
        `ipc_server`. The stall cannot be moved off the loop; it can only be
        moved off the BOOT PATH.

        So enrollment — one cheap database read, and the only part needed to
        answer "who would I be checking for" — resolves here. The model loads
        lazily on the first verification that actually needs it, via
        `start_warming` inside `identify`, which already reports NOT_READY and
        invites a retry. The cost is one "give me a moment" on the first
        unlock of a session. The alternative was fourteen seconds of deafness
        during every boot, for a capability that may never be used.
        """
        try:
            await self.refresh_enrollment()
        except Exception:  # noqa: BLE001
            pass

    def start_warming(self) -> None:
        """Begin loading the model in the background. Idempotent. NEVER raises.

        Fire-and-forget on purpose. Boot must not wait for a speaker model,
        and nothing that calls this cares when it finishes — they ask
        :attr:`readiness` at the moment they need an answer.
        """
        try:
            if not voice_authority_enabled():
                return
            if self._warm_task is not None and not self._warm_task.done():
                return
            if self._readiness is Readiness.READY:
                return
            self._readiness = Readiness.WARMING
            self._warm_started = time.monotonic()
            self._warm_task = asyncio.get_event_loop().create_task(self._warm())
            logger.info("[VoiceIdentity] warming the speaker model in the "
                        "background — verification will report NOT_READY "
                        "until it lands")
        except Exception:  # noqa: BLE001
            logger.debug("[VoiceIdentity] warm start degraded", exc_info=True)

    async def _warm(self) -> None:
        t0 = time.monotonic()
        try:
            from backend.voice.speaker_verification_service import (
                SpeakerVerificationService,
            )
            svc = SpeakerVerificationService()
            await asyncio.wait_for(svc.initialize(), timeout=warm_timeout_s())
            self._service = svc
            self._readiness = Readiness.READY
            logger.info("[VoiceIdentity] speaker model READY after %.1fs",
                        time.monotonic() - t0)
        except asyncio.TimeoutError:
            self._readiness = Readiness.FAILED
            self._detail = f"initialize() exceeded {warm_timeout_s():.0f}s"
            logger.error("[VoiceIdentity] warm-up TIMED OUT after %.0fs — "
                         "voice authority unavailable", warm_timeout_s())
        except Exception as exc:  # noqa: BLE001
            self._readiness = Readiness.FAILED
            self._detail = f"{type(exc).__name__}: {exc}"
            logger.error("[VoiceIdentity] warm-up failed: %s", self._detail)

    # -- enrollment ------------------------------------------------------

    def enrolled_speaker(self) -> Optional[str]:
        """Who there is a voiceprint for. NEVER raises.

        Three answers, and the third is the one that matters:

            "Derek"   a profile exists
            ""        the service is loaded and has NO profile
            None      UNKNOWN — nothing can say yet

        WHY THIS ASKS THE SERVICE AND NOT A DIRECTORY
        -----------------------------------------------
        The first version of this read `~/.jarvis/voice/embeddings/`, found it
        empty, and answered "nobody is enrolled". That was wrong in the most
        familiar way: `SpeakerVerificationService._load_speaker_profiles`
        loads from `intelligence.learning_database`, so the profiles live in a
        database and an empty local directory says nothing whatsoever about
        whether a voiceprint exists. `voice_enrollment.json` recording
        `samples: 59, source: CloudSQL_voiceprints` is consistent with exactly
        that.

        An empty directory answering "not enrolled" is the same defect as
        `is_screen_locked()` returning False when it cannot see, and the same
        defect as the consent verdict reporting "operator declined" for a
        prompt nobody drew: a probe that could not look, reporting absence.
        Here it would have told the operator "I don't have a voiceprint for
        you" while a perfectly good one sat in a database nobody had asked.

        So the SERVICE is the authority, and until it is loaded the honest
        answer is None. The local directory survives only as a fast-path hint
        — a name there is evidence, an absence there is not.
        """
        try:
            svc = self._service
            if svc is not None:
                for attr in ("speaker_profiles", "profiles", "_profiles"):
                    profiles = getattr(svc, attr, None)
                    # NON-EMPTY only. An empty dict is NOT a statement that
                    # nobody is enrolled — it is the state a service is in
                    # before `_load_speaker_profiles` has succeeded, and that
                    # load fails routinely: measured 2026-08-03 08:47,
                    #
                    #   EcapaFacade error ... falling back to local engine
                    #   CloudSQL init timed out after 10.0s - SQLite-only mode
                    #
                    # left `speaker_profiles == {}` on a live service, and this
                    # branch reported "I don't have a voiceprint for you on
                    # this Mac yet" while "Derek J. Russell / 272 samples" sat
                    # in the local SQLite one query away. Same defect as
                    # `is_screen_locked` answering False when it could not see:
                    # an empty container from a FAILED load, read as absence.
                    #
                    # A populated dict is authoritative. An empty one means
                    # "ask something that actually knows".
                    if isinstance(profiles, dict) and profiles:
                        names = [str(k) for k in profiles if k]
                        if names:
                            return sorted(names)[0]
            # Whatever the cheap database lookup last found. See
            # :meth:`refresh_enrollment` — enrollment is a ROW, not a model.
            return self._enrolled_cache
        except Exception:  # noqa: BLE001
            return None

    def _schedule_enrollment_refresh(self) -> None:
        """Resolve enrollment soon, without waiting for it. NEVER raises."""
        try:
            if self._refresh_task is not None and not self._refresh_task.done():
                return
            self._refresh_task = asyncio.get_event_loop().create_task(
                self.refresh_enrollment())
        except Exception:  # noqa: BLE001 — no loop, or shutting down
            pass

    async def refresh_enrollment(self) -> Optional[str]:
        """Ask the profile store who is enrolled. NEVER raises.

        ENROLLMENT IS A DATABASE QUESTION, NOT A MODEL QUESTION
        --------------------------------------------------------
        The speaker model takes over 150 seconds to load. Whether a voiceprint
        EXISTS does not need it at all — `learning_db.get_all_speaker_profiles`
        is a single query, and the row is already there: measured on this
        machine, `speaker_profiles` holds one profile for "Derek J. Russell"
        with 272 samples, an enrollment quality of 0.95 and a 1538-byte
        embedding.

        Separating the two questions is what lets the organism say something
        useful while it wakes up. "I know it's you I'd be checking for, my
        voice model is still loading" is an answer an operator can act on;
        "unknown" is not, and "you are not enrolled" would have been false.

        Cached, because the answer changes only when somebody enrolls, and
        this is consulted on a path where an operator is waiting.
        """
        try:
            if not voice_authority_enabled():
                return None
            now = time.monotonic()
            if (self._enrolled_cache is not None
                    and (now - self._enrolled_at) < 300.0):
                return self._enrolled_cache
            # PATIENT, because nothing is waiting on this.
            #
            # Measured 2026-08-03 01:40:34 — this timed out at 15s and left
            # enrollment UNKNOWN for the whole session, while `LearningDB`
            # itself logged "Initialization timed out (15s) — retrying with
            # fast_mode (SQLite-first)" and fell back to a local database that
            # holds no profiles. Two 15s ceilings racing the same cold
            # CloudSQL connect, both losing.
            #
            # A cold connect measured 5.2s idle and rather more during boot,
            # when it competes with everything else waking up. This runs in
            # the background and has no operator waiting on it — the ONLY cost
            # of waiting longer is that enrollment stays UNKNOWN a little
            # longer, and UNKNOWN is already handled honestly. The cost of
            # giving up early is a whole session that cannot verify anybody.
            timeout = _enrollment_timeout_s()
            from intelligence.learning_database import get_learning_database
            db = await asyncio.wait_for(get_learning_database(),
                                        timeout=timeout)
            profiles = await asyncio.wait_for(
                db.get_all_speaker_profiles(), timeout=timeout)
            names = []
            for p in (profiles or []):
                n = (p.get("speaker_name") if isinstance(p, dict)
                     else getattr(p, "speaker_name", ""))
                if n:
                    names.append(str(n))
            # Prefer the primary user when the store holds several — a machine
            # with two profiles still has one owner.
            primary = [p for p in (profiles or [])
                       if isinstance(p, dict) and p.get("is_primary_user")]
            if primary and primary[0].get("speaker_name"):
                self._enrolled_cache = str(primary[0]["speaker_name"])
            else:
                self._enrolled_cache = sorted(names)[0] if names else ""
            self._enrolled_at = now
            logger.info("[VoiceIdentity] enrollment: %s",
                        self._enrolled_cache or "nobody enrolled")
            return self._enrolled_cache
        except asyncio.TimeoutError:
            logger.warning("[VoiceIdentity] enrollment lookup timed out — "
                           "leaving it UNKNOWN rather than guessing")
            return None
        except Exception as exc:  # noqa: BLE001
            logger.info("[VoiceIdentity] enrollment lookup unavailable (%s) — "
                        "UNKNOWN, not 'nobody'", type(exc).__name__)
            return None

    # -- the question ----------------------------------------------------

    async def identify(self, audio: Optional[bytes], *,
                       sample: str = "") -> Identification:
        """Who is speaking? NEVER raises. NEVER blocks on warm-up.

        Order matters: the cheap, certain refusals come first, so a machine
        with no voiceprint says so instantly instead of loading a model to
        compare an utterance against nothing.
        """
        t0 = time.monotonic()

        def _out(v: Verdict, *, conf: float = 0.0, speaker: str = "",
                 detail: str = "") -> Identification:
            self._counts[v.value] = self._counts.get(v.value, 0) + 1
            out = Identification(
                verdict=v.value, confidence=round(conf, 4), speaker=speaker,
                spoken_reason=_SPOKEN.get(v.value, ""), detail=detail,
                elapsed_ms=round((time.monotonic() - t0) * 1000.0, 1),
                sample=sample)
            logger.info("[VoiceIdentity] %s conf=%.2f sample=%s (%.0fms) %s",
                        v.value, out.confidence, sample or "-",
                        out.elapsed_ms, detail or "")
            return out

        try:
            if not voice_authority_enabled():
                return _out(Verdict.UNAVAILABLE, detail="authority disabled")
            if not audio:
                return _out(Verdict.NO_AUDIO, detail="no utterance held")

            # A LOCAL PRINT SHORT-CIRCUITS; ITS ABSENCE DOES NOT.
            #
            # Readiness is checked BEFORE concluding "not enrolled", because
            # until the service has loaded, whether a voiceprint exists is
            # UNKNOWN — the profiles live in the learning database, not on
            # disk. Answering NOT_ENROLLED from an unloaded service would tell
            # the operator their voice was never enrolled when the truth is
            # that nothing had looked yet, and they would go and re-record 59
            # samples they already have.
            # CACHED ONLY. Never a live lookup here.
            #
            # This briefly called `refresh_enrollment()`, which measured 5.2s
            # against CloudSQL on a cold connection — five seconds sitting
            # between "unlock my screen" and the screen unlocking, on the exact
            # path this class exists to keep clear of slow work. The same
            # mistake as putting the speaker model here, just three orders of
            # magnitude cheaper and therefore easier to miss.
            #
            # The lookup belongs to `warm()` at boot and to the 300s cache. If
            # nothing has resolved it yet the answer is UNKNOWN, which reports
            # NOT_READY and kicks a refresh for next time — never a blocking
            # wait, and never a guess.
            owner = self.enrolled_speaker()
            if owner is None:
                self._schedule_enrollment_refresh()
            state = self.readiness
            if state is not Readiness.READY:
                if state in (Readiness.COLD, Readiness.WARMING):
                    # Warming is not an error, and it must not become a wait:
                    # the utterance expires long before the model lands.
                    self.start_warming()
                    return _out(Verdict.NOT_READY,
                                detail=f"model {state.value}; enrollment "
                                       f"{'unknown' if owner is None else owner or 'none'}")
                return _out(Verdict.UNAVAILABLE,
                            detail=self._detail or f"model {state.value}")

            # UNKNOWN IS NOT "NOBODY". Checked as `is None`, never `not owner`.
            #
            # Measured 2026-08-03 01:41:27, with 272 samples sitting in
            # CloudSQL: the enrollment lookup timed out, returned None, and
            # `if not owner` treated that identically to "" — so JARVIS told
            # its owner "I don't have a voiceprint for you on this Mac yet"
            # and suggested he enroll a voice he enrolled last October.
            #
            # Three states were designed precisely so this could not happen,
            # and then two of them were collapsed by one falsy check. `None`
            # and `""` are different claims and the operator acts on them
            # differently: one is "wait", the other is "enroll".
            if owner is None:
                self._schedule_enrollment_refresh()
                return _out(Verdict.NOT_READY,
                            detail="enrollment UNKNOWN — the profile store "
                                   "could not be reached; NOT a statement "
                                   "that nobody is enrolled")
            if owner == "":
                return _out(Verdict.NOT_ENROLLED,
                            detail="profile store reachable and holds no "
                                   "voiceprint")

            result = await self._service.verify_speaker(audio, owner)
            verified = bool((result or {}).get("verified"))
            conf = float((result or {}).get("confidence") or 0.0)
            who = str((result or {}).get("speaker_name") or owner)

            if not verified:
                return _out(Verdict.REJECTED, conf=conf, speaker=who,
                            detail="service returned verified=false")
            floor = min_confidence()
            if conf < floor:
                # The service said yes; the confidence says otherwise. Refuse
                # — this floor can only ever make the answer stricter.
                return _out(Verdict.REJECTED, conf=conf, speaker=who,
                            detail=f"confidence {conf:.2f} below floor {floor:.2f}")
            return _out(Verdict.VERIFIED, conf=conf, speaker=who)
        except Exception as exc:  # noqa: BLE001 — a fault is never consent
            return _out(Verdict.UNAVAILABLE,
                        detail=f"{type(exc).__name__}: {exc}")

    def stats(self) -> Dict[str, Any]:
        return {
            "schema_version": VOICE_IDENTITY_SCHEMA_VERSION,
            "enabled": voice_authority_enabled(),
            "readiness": self.readiness.value,
            "enrolled_speaker": self.enrolled_speaker() or None,
            "min_confidence": min_confidence(),
            "verdicts": dict(self._counts),
            "detail": self._detail,
        }


_IDENTITY: Optional[VoiceIdentity] = None


def get_voice_identity() -> VoiceIdentity:
    """Process-wide voice authority. NEVER raises."""
    global _IDENTITY
    if _IDENTITY is None:
        _IDENTITY = VoiceIdentity()
    return _IDENTITY


def reset_voice_identity() -> None:
    """Testing seam. NEVER raises."""
    global _IDENTITY
    _IDENTITY = None
