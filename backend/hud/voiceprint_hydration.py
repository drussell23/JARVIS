"""Turn a stored voiceprint back into a vector, without guessing.

THE MISMATCH THIS EXISTS FOR
------------------------------
The same speaker profile is stored in two places and the blobs are different
sizes::

    CloudSQL   voiceprint_embedding   1538 bytes
    local      voiceprint_embedding    768 bytes

768 is 192 float32s, which is ECAPA-TDNN's native embedding width. 1538 is
192 float64s plus two bytes of something — almost certainly the same vector at
double precision with a small header. Almost certainly. That word is the
problem.

A cosine similarity does not fail loudly on a misread buffer. Decode 768 bytes
of float32 as float64 and you get 96 finite-looking numbers that are pure
noise; the comparison still runs, still returns a number, and that number
means the operator's Mac says "that didn't sound like you" — a statement about
a person, produced by a type error. The failure is silent, plausible, and
blames the human.

So nothing here is assumed. The blob is INSPECTED: a length that divides
cleanly by a known width for a known dimension is decoded that way, and a
length that does not is REFUSED. Refusing to hydrate is a bad afternoon;
hydrating wrongly is an accusation.

WHY UPCASTING IS SAFE AND DOWNCASTING IS NOT
----------------------------------------------
float32 → float64 is exact: every float32 has an exact float64
representation, so widening cannot move a vector. The reverse rounds, and
rounding a normalised embedding perturbs the cosine at the fourth decimal —
which is inside the margin that separates "you" from "not you" on a
borderline sample. So this widens toward whatever the model wants and never
narrows.

WHY IT NORMALISES NOTHING
---------------------------
Cosine similarity is scale-invariant, and the verification service owns its
own preprocessing. A helper that quietly L2-normalised would be a second
opinion about the maths, and the two would diverge the first time the service
changed. This decodes bytes into the requested dtype and stops.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("JARVIS.VoiceprintHydration")

VOICEPRINT_HYDRATION_SCHEMA_VERSION: str = "voiceprint_hydration.v1"

#: Embedding widths this codebase actually produces, smallest first.
#:
#: 192 is ECAPA-TDNN's output and the one measured in the local profile (768 =
#: 192 x 4). The others are here because a speaker-embedding stack that swaps
#: models is normal and a blob that matches none of these must be refused
#: rather than reinterpreted — the list exists to make "unknown" detectable,
#: not to make every length acceptable.
KNOWN_DIMENSIONS: Tuple[int, ...] = (192, 256, 384, 512)

#: (numpy dtype name, bytes per element).
#:
#: float16 is deliberately ABSENT. Nobody stores half-precision speaker
#: embeddings, and including it made 768 bytes ambiguous between 192xfloat32
#: and 384xfloat16 — an ambiguity invented entirely by offering a reading
#: nothing produces.
_CANDIDATE_DTYPES: Tuple[Tuple[str, int], ...] = (
    ("float32", 4),
    ("float64", 8),
)

#: Header bytes tolerated when the caller has NOT told us the dimension.
#:
#: Zero. This was 16, and a 16-byte tolerance is not a tolerance — it is a
#: licence: 777 bytes of nothing hydrated cleanly as "192 float32 with a
#: 9-byte header", which is exactly the confident reinterpretation this module
#: exists to refuse. Caught by `test_a_length_matching_nothing_is_refused`.
#:
#: With a declared dimension a header is fine, because the arithmetic is then
#: pinned from both ends. Without one, only an exact multiple is a vector.
_MAX_HEADER_BYTES_UNGUIDED = 0

#: Header bytes tolerated when the caller HAS declared the dimension.
#: CloudSQL's 1538 is 1536 + 2, so a small prefix is real.
_MAX_HEADER_BYTES_GUIDED = 16


@dataclass
class Hydrated:
    """A decoded voiceprint, and the evidence for how it was read."""

    vector: Any                    # numpy.ndarray
    dimension: int
    source_dtype: str
    output_dtype: str
    header_bytes: int = 0
    #: What was rejected on the way to this answer, for a log line that
    #: explains itself when the guess is wrong.
    considered: Tuple[str, ...] = ()

    @property
    def shape(self) -> tuple:
        try:
            return tuple(self.vector.shape)
        except Exception:  # noqa: BLE001
            return ()

    def __str__(self) -> str:
        return (f"{self.dimension}-d {self.source_dtype}"
                f"{f' (+{self.header_bytes}B header)' if self.header_bytes else ''}"
                f" -> {self.output_dtype}")


class UnreadableVoiceprint(ValueError):
    """The blob is not a vector of any shape this code knows.

    A distinct type because the CALLER must be able to tell "I could not read
    your voiceprint" from "that was not your voice". Both refuse the unlock;
    only one of them is about the operator.
    """


def default_output_dtype() -> str:
    """The dtype the verification model wants. NEVER raises.

    Torch defaults to float32 and ECAPA is trained there, so that is the
    default — but it is a knob rather than a constant, because the correct
    answer belongs to whatever model is loaded and this module must not be the
    thing that has to change when it is swapped.
    """
    return (os.environ.get("JARVIS_VOICEPRINT_DTYPE", "") or "float32").strip()


def hydrate(blob: Any, *, dtype: str = "",
            expect_dimension: int = 0) -> Hydrated:
    """Decode a stored voiceprint. Raises :class:`UnreadableVoiceprint`.

    The ONLY function here that raises, deliberately: every other failure in
    this codebase degrades, and a silently-degraded voiceprint is a wrong
    answer about a person. A caller that cannot hydrate must refuse to verify,
    and a return value of None would let that be forgotten.
    """
    import numpy as np

    want = (dtype or default_output_dtype()).strip()
    raw = _as_bytes(blob)
    if not raw:
        raise UnreadableVoiceprint("voiceprint blob is empty")

    guided = bool(expect_dimension)
    max_header = (_MAX_HEADER_BYTES_GUIDED if guided
                  else _MAX_HEADER_BYTES_UNGUIDED)
    dims = (expect_dimension,) if guided else KNOWN_DIMENSIONS

    # AMBIGUITY IS REFUSED, NOT BROKEN BY PRECEDENCE.
    #
    # 1538 bytes is 384 float32 + 2 AND 192 float64 + 2. Length cannot tell
    # those apart, and picking one by candidate order would be a coin toss
    # that silently produces a wrong vector half the time. The profile schema
    # already records `embedding_dimension` — so the answer is to ASK, and to
    # refuse until somebody does.
    if not guided:
        exact = [(n, d) for n, w in _CANDIDATE_DTYPES
                 for d in KNOWN_DIMENSIONS if d * w == len(raw)]
        if len(exact) > 1:
            raise UnreadableVoiceprint(
                f"{len(raw)} bytes is ambiguous — it reads as "
                + " or ".join(f"{d}-d {n}" for n, d in exact)
                + "; pass expect_dimension (the profile's "
                  "`embedding_dimension` column) rather than letting this "
                  "guess which one your model was trained on")

    considered = []
    for name, width in _CANDIDATE_DTYPES:
        for dim in dims:
            need = dim * width
            header = len(raw) - need
            if header < 0 or header > max_header:
                considered.append(f"{dim}x{name}={need}B")
                continue
            try:
                # Read from the END. A header sits at the front, so the vector
                # is the trailing `need` bytes — slicing from the front would
                # shift every element by the header width and produce a
                # perfectly finite, entirely wrong vector.
                vec = np.frombuffer(raw[len(raw) - need:], dtype=name)
                if vec.size != dim or not np.isfinite(vec).all():
                    considered.append(f"{dim}x{name}:non-finite")
                    continue
                # `frombuffer` returns a READ-ONLY view onto the original
                # buffer. Handing that to a model that writes in place is a
                # memory-corruption bug that surfaces somewhere else entirely,
                # so `astype` (which copies) is load-bearing, not cosmetic.
                out = vec.astype(want, copy=True)
                h = Hydrated(vector=out, dimension=dim, source_dtype=name,
                             output_dtype=want, header_bytes=max(0, header),
                             considered=tuple(considered))
                logger.info("[VoiceprintHydration] %dB -> %s", len(raw), h)
                return h
            except UnreadableVoiceprint:
                raise
            except Exception as exc:  # noqa: BLE001
                considered.append(f"{dim}x{name}:{type(exc).__name__}")
                continue

    raise UnreadableVoiceprint(
        f"{len(raw)} bytes matches no known embedding shape "
        f"(tried {', '.join(considered) or 'nothing'}); refusing to "
        f"reinterpret — a misread vector produces a confident wrong answer "
        f"about who is speaking")


def _as_bytes(blob: Any) -> bytes:
    """Whatever the driver handed back, as bytes. NEVER raises.

    psycopg2 returns `memoryview` for bytea and sqlite3 returns `bytes`, so
    the same profile arrives as two types depending on which store answered —
    which is exactly the seam this module exists to make invisible.
    """
    try:
        if blob is None:
            return b""
        if isinstance(blob, (bytes, bytearray)):
            return bytes(blob)
        if isinstance(blob, memoryview):
            return blob.tobytes()
        if isinstance(blob, str):
            # Some rows round-trip through text; try hex, then base64.
            import base64
            import binascii
            t = blob.strip()
            try:
                return bytes.fromhex(t[2:] if t.startswith("\\x") else t)
            except (ValueError, binascii.Error):
                pass
            try:
                return base64.b64decode(t, validate=True)
            except (ValueError, binascii.Error):
                return b""
        return bytes(blob)
    except Exception:  # noqa: BLE001
        return b""


def describe(blob: Any) -> Dict[str, Any]:
    """What a blob looks like, without committing to reading it. NEVER raises.

    For diagnostics and for the log line that has to explain a mismatch
    between two stores holding "the same" profile.
    """
    raw = _as_bytes(blob)
    out: Dict[str, Any] = {
        "schema_version": VOICEPRINT_HYDRATION_SCHEMA_VERSION,
        "bytes": len(raw),
        "readable": False,
        "candidates": [],
    }
    for name, width in _CANDIDATE_DTYPES:
        for dim in KNOWN_DIMENSIONS:
            header = len(raw) - dim * width
            if 0 <= header <= _MAX_HEADER_BYTES_GUIDED:
                out["candidates"].append(
                    f"{dim}-d {name}" + (f" +{header}B" if header else ""))
    # `readable` means readable WITHOUT being told the dimension — i.e. an
    # exact multiple. A blob that only resolves once somebody declares the
    # dimension is a candidate, not an answer.
    out["exact"] = [c for c in out["candidates"] if "+" not in c]
    out["readable"] = len(out.get("exact") or []) == 1
    return out


def hydrate_or_none(blob: Any, *, dtype: str = "",
                    expect_dimension: int = 0) -> Optional[Hydrated]:
    """:func:`hydrate`, for callers that genuinely cannot raise. NEVER raises.

    Separate rather than a flag so the raising form stays the default. A
    caller reaching for this is saying "I will handle None", and that sentence
    should be visible at the call site.
    """
    try:
        return hydrate(blob, dtype=dtype, expect_dimension=expect_dimension)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[VoiceprintHydration] refused: %s", exc)
        return None
