"""Measurement must not teach the thing it measures.

The held-out benchmark and the training handoff's verification score the
served model by running its answers through O+V's OWN parser, so the score
judges exactly what production would act on. That parser also LEARNS: a
malformed diff becomes a LessonMemory lesson, an apply outcome becomes
served-capability evidence, a generation becomes a trajectory row. Inside a
measurement each of those leaks the held-out tasks into the organism -- the
next prompt carries a lesson learned from the test, the next training corpus
carries the test's answers -- and a benchmark that teaches its subject stops
being held out (seen live 2026-10-10: ``[LessonMemory] recorded ... op=heldout-``).

One marker, set for the duration of a measurement (``training_handoff.
evaluation_scope`` sets it, so the benchmark and the handoff's smoke share
it), read by every learning seam. A ContextVar: it follows into tasks and
``asyncio.to_thread`` (both copy the context), and never leaks into work the
measurement did not start. Stdlib only. NEVER raises.
"""
from __future__ import annotations

import contextlib
import logging
from contextvars import ContextVar
from typing import Iterator

logger = logging.getLogger("Ouroboros.EvaluationScope")

__all__ = ["measuring", "measuring_label", "suppressed"]

_MEASURING: "ContextVar[str]" = ContextVar("ov_measuring", default="")


@contextlib.contextmanager
def measuring(label: str) -> Iterator[None]:
    """Everything inside is a measurement: learning seams record nothing."""
    token = _MEASURING.set(str(label or "evaluation"))
    try:
        yield
    finally:
        _MEASURING.reset(token)


def measuring_label() -> str:
    """The measurement in progress, or ``""``."""
    try:
        return _MEASURING.get()
    except Exception:  # noqa: BLE001
        return ""


def suppressed(seam: str) -> bool:
    """For a learning seam: True (and a debug line) when it must not write."""
    label = measuring_label()
    if label:
        logger.debug("[EvaluationScope] %s not recorded: inside measurement %r", seam, label)
        return True
    return False
