"""
One place that turns "an embedding, in whatever form it arrived" into a vector.

Three layers hand embeddings to each other and each invented its own coercion:

  * ``SpeechBrainEngine._as_host_vector``      — torch tensor -> host ndarray
  * ``AdvancedBiometric._as_similarity_vector``— anything -> float32 vector
  * ``VoiceProfileLearningEngine._load_profile``— bytes/str -> ndarray

The third is why the profile would not load on 2026-08-06::

    🧠 [LEARNING-ENGINE] Could not load profile:
        'NoneType' object has no attribute 'shape'

``get_all_speaker_profiles()`` decodes the stored BLOB and hands back
``embedding_array.tolist()`` — a plain ``list[float]``, the correct and already
usable form. The consumer tested for ``bytes`` and for ``str`` and nothing else,
so the one form it actually receives fell through both branches, the vector
stayed ``None``, and the very next line logged ``.shape`` on it. A missing case
in a coercion chain became an exception on a load path.

That is the same shape as the numpy-scalar defect fixed the same week
(``float()`` on a size-1 1-D array) and the same shape as the tensor/device
handling on the engine. Three sites, three partial answers, three different
failure modes. This module is the single answer they all call.

WHAT IS A REPRESENTATION DIFFERENCE, AND WHAT IS NOT
---------------------------------------------------
dtype, device, nesting and container type are *representation* — a float64
torch tensor on MPS, a JSON string, a BLOB and a Python list can all be the
same embedding, and converting between them loses nothing. Those are handled.

**Dimension is not.** A 192-d and a 256-d vector are different claims about the
speaker, and silently reshaping or padding one into the other would fabricate a
comparison. Shape is returned as-is for the caller to accept or reject, which
is why ``coerce_vector`` has no ``expected_dim`` parameter and
``require_vector`` exists separately for callers that do know the dimension.

Never raises into a verdict. Every failure returns ``None``, because an
embedding that cannot be read is missing evidence — never a match, and never a
mismatch either.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import numpy as np

logger = logging.getLogger(__name__)

__all__ = ["coerce_vector", "require_vector", "is_usable"]


def _from_text(value: str) -> Optional[Any]:
    """A JSON array of numbers, as stored by the metrics and profile paths."""
    text = value.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def coerce_vector(embedding: Any, *, dtype: Any = np.float32) -> Optional[np.ndarray]:
    """
    Return a flat ``dtype`` vector for ``embedding``, or ``None``.

    Accepts, in the forms these layers actually exchange:
      * ``None`` / empty                      -> ``None``
      * torch tensor (any device, incl. MPS)  -> detached host array
      * ``bytes`` / ``bytearray`` / memoryview -> ``np.frombuffer`` float32 BLOB
      * ``str``                                -> JSON array
      * list / tuple / ndarray / nested        -> flattened

    ``memoryview`` matters specifically: sqlite3 hands BLOBs back as one under
    some configurations, and a memoryview is neither ``bytes`` nor ``str``, so
    a two-branch isinstance chain misses it exactly the way the list case was
    missed.
    """
    try:
        if embedding is None:
            return None

        # torch tensors — .detach() before .cpu() or a grad-tracking tensor raises
        if hasattr(embedding, "detach") and hasattr(embedding, "cpu"):
            embedding = embedding.detach().cpu().numpy()

        if isinstance(embedding, (bytes, bytearray, memoryview)):
            raw = bytes(embedding)
            if not raw:
                return None
            # Stored BLOBs are float32; a length that is not a whole number of
            # float32s is a different encoding, not an embedding to guess at.
            if len(raw) % 4 != 0:
                logger.warning(
                    "[EmbeddingOps] BLOB of %d bytes is not a whole number of "
                    "float32 values — refusing to reinterpret it", len(raw),
                )
                return None
            embedding = np.frombuffer(raw, dtype=np.float32)

        elif isinstance(embedding, str):
            decoded = _from_text(embedding)
            if decoded is None:
                logger.warning("[EmbeddingOps] string is not a JSON array — cannot coerce")
                return None
            embedding = decoded

        vec = np.asarray(embedding, dtype=dtype)
        if vec.size == 0:
            return None
        return vec.flatten()

    except Exception as exc:  # noqa: BLE001 — a coercion may never raise into a verdict
        logger.error("[EmbeddingOps] coercion failed (%s: %s)", type(exc).__name__, exc)
        return None


def is_usable(vec: Optional[np.ndarray]) -> bool:
    """
    True when ``vec`` can be compared against.

    Non-finite values are rejected. A NaN propagates silently through a cosine
    similarity and yields a NaN score, which compares false against every
    threshold — a corrupt embedding would present as a confident rejection
    rather than as the missing evidence it is.
    """
    if vec is None or vec.size == 0:
        return False
    return bool(np.all(np.isfinite(vec)))


def require_vector(
    embedding: Any,
    *,
    expected_dim: Optional[int] = None,
    label: str = "embedding",
) -> Optional[np.ndarray]:
    """
    ``coerce_vector`` plus finiteness, and dimension when the caller knows it.

    A dimension mismatch is reported and refused rather than reshaped: the two
    vectors describe different feature spaces, and any score computed across
    them would be a fabricated number wearing a real one's units.
    """
    vec = coerce_vector(embedding)
    if vec is None or not is_usable(vec):
        logger.warning("[EmbeddingOps] %s is missing or non-finite — refusing it", label)
        return None
    if expected_dim is not None and vec.shape[0] != expected_dim:
        logger.warning(
            "[EmbeddingOps] %s has dimension %d, expected %d — refusing to "
            "compare across feature spaces", label, vec.shape[0], expected_dim,
        )
        return None
    return vec
