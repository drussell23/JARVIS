"""
Regression spine for three defects in ``advanced_biometric_verification``.

1. An UNMEASURED SNR was read as 30 dB and then INFLATED the acoustic and
   physics weights -- the two channels noise degrades first.
2. ``fusion_weights.get(name, <literal>)`` guarded a lookup that the very next
   line performed with ``fusion_weights[name]``, so an absent weight raised
   KeyError inside a verification instead of falling back.
3. A dead-silent capture -- the shape a TCC-denied microphone returns -- reached
   the stages, where a zero-magnitude embedding produces a 0/0 cosine and a NaN
   that compares False against every threshold.

These exercise the real objects. No stage is stubbed and no seam is injected:
the point is what the shipped code does with the inputs a locked machine
actually produces.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.voice.advanced_biometric_verification import (  # noqa: E402
    AdvancedBiometricVerifier,
)
from backend.voice.biological_bounds import is_measured  # noqa: E402


@pytest.fixture()
def verifier() -> AdvancedBiometricVerifier:
    return AdvancedBiometricVerifier()


class _Model:
    """Minimal stand-in carrying only what _compute_fusion_weights reads."""

    def __init__(self, weights):
        self.fusion_weights = weights


# ---------------------------------------------------------------------------
# 1. Unmeasured SNR
# ---------------------------------------------------------------------------

BALANCED = {"embedding": 0.4, "acoustic": 0.3, "physics": 0.1, "spoofing": 0.2}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "context",
    [
        None,
        {},
        {"snr_db": None},
        {"snr_db": float("nan")},
        {"snr_db": "loud"},
        {"quality": 0.9},  # a bare quality scalar is not an SNR
    ],
    ids=["none", "empty", "null", "nan", "string", "quality-not-snr"],
)
async def test_unmeasured_snr_makes_no_adjustment(verifier, context):
    """
    An SNR nobody measured must not move a single weight.

    It used to move two, in the wrong direction: the default of 30 dB cleared
    the ``> 25`` branch, so the acoustic and physics weights were multiplied up
    for a recording whose quality was entirely unknown.
    """
    got = await verifier._compute_fusion_weights(_Model(dict(BALANCED)), context)

    total = sum(BALANCED.values())
    for name, raw in BALANCED.items():
        assert got[name] == pytest.approx(raw / total), name


@pytest.mark.asyncio
async def test_a_noisy_capture_leans_on_the_embedding(verifier):
    """
    The adjustment still happens when the SNR IS measured -- and in the
    direction that survives noise. A guard that neutered the feature would pass
    the test above while making the whole function pointless.
    """
    clean = await verifier._compute_fusion_weights(_Model(dict(BALANCED)), {"snr_db": 45.0})
    noisy = await verifier._compute_fusion_weights(_Model(dict(BALANCED)), {"snr_db": 2.0})

    assert noisy["embedding"] > clean["embedding"]
    assert noisy["acoustic"] < clean["acoustic"]
    assert sum(noisy.values()) == pytest.approx(1.0)
    assert sum(clean.values()) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_unmeasured_snr_is_not_treated_as_clean(verifier):
    """
    The specific inversion. Unmeasured must not land on the clean side of the
    curve, which is where a 30 dB default put it.
    """
    unmeasured = await verifier._compute_fusion_weights(_Model(dict(BALANCED)), {})
    clean = await verifier._compute_fusion_weights(_Model(dict(BALANCED)), {"snr_db": 45.0})

    assert unmeasured["acoustic"] != pytest.approx(clean["acoustic"]) or \
        unmeasured["embedding"] == pytest.approx(clean["embedding"])
    # The load-bearing claim: unmeasured never boosts the noise-fragile channels
    # above their unadjusted share.
    raw_total = sum(BALANCED.values())
    assert unmeasured["acoustic"] == pytest.approx(BALANCED["acoustic"] / raw_total)
    assert unmeasured["physics"] == pytest.approx(BALANCED["physics"] / raw_total)


# ---------------------------------------------------------------------------
# 2. Weights that are not measurements
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), None, "0.3", -0.2])
async def test_unmeasured_weights_are_dropped_not_substituted(verifier, bad):
    """
    A weight that is not a measurement is dropped and the rest renormalised.
    Substituting one would let a stage nobody weighted cast a vote.
    """
    weights = dict(BALANCED)
    weights["acoustic"] = bad

    got = await verifier._compute_fusion_weights(_Model(weights), {"snr_db": 30.0})

    assert "acoustic" not in got
    assert sum(got.values()) == pytest.approx(1.0)
    # And the survivors keep their RATIO to one another -- renormalising is not
    # the same as redistributing the dropped weight by hand.
    assert got["embedding"] / got["physics"] == pytest.approx(
        BALANCED["embedding"] / BALANCED["physics"]
    )


@pytest.mark.asyncio
async def test_one_nan_weight_does_not_poison_the_others(verifier):
    """
    The old renormaliser summed every value and divided by the total, so a
    single NaN weight made EVERY weight NaN.
    """
    weights = dict(BALANCED)
    weights["physics"] = float("nan")

    got = await verifier._compute_fusion_weights(_Model(weights), {"snr_db": 20.0})

    assert got, "everything was dropped"
    for name, value in got.items():
        assert is_measured(value, 0.0, 1.0), f"{name}={value!r}"


@pytest.mark.asyncio
@pytest.mark.parametrize("weights", [{}, None, {"a": float("nan")}, {"a": 0.0}])
async def test_no_surviving_weight_abstains_rather_than_divides(verifier, weights):
    """Returns an empty mapping. It must never raise ZeroDivisionError."""
    got = await verifier._compute_fusion_weights(_Model(weights), {"snr_db": 30.0})
    assert got == {}


# ---------------------------------------------------------------------------
# 3. Dead capture (TCC)
# ---------------------------------------------------------------------------

class _Features:
    def __init__(self, embedding):
        self.embedding = embedding


@pytest.mark.parametrize(
    "embedding, expect_dead",
    [
        (np.zeros(192, dtype=np.float32), True),          # TCC denial: silence
        (np.full(192, np.nan, dtype=np.float32), True),
        (np.full(192, np.inf, dtype=np.float32), True),
        (np.array([], dtype=np.float32), True),
        (None, True),
        (np.full(192, 1e-30, dtype=np.float32), True),    # denormal, norm ~ 0
        (np.ones(192, dtype=np.float32), False),
    ],
    ids=["zeros", "nan", "inf", "empty", "none", "denormal", "live"],
)
def test_dead_capture_detection(verifier, embedding, expect_dead):
    reason = verifier._dead_capture_reason(_Features(embedding))
    assert (reason is not None) is expect_dead, reason


def test_the_zero_vector_really_does_produce_nan_cosine():
    """
    Why magnitude is checked and not just finiteness.

    If this ever stops being true the gate is over-strict and should be
    revisited -- but while it holds, a zero embedding reaching the stages means
    a NaN reaching the thresholds, and a NaN compares False against all of them.
    """
    zeros = np.zeros(192, dtype=np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):
        cosine = float(zeros @ zeros) / (np.linalg.norm(zeros) * np.linalg.norm(zeros))
    assert math.isnan(cosine)


def test_dead_capture_gate_precedes_the_stages():
    """
    The gate is only worth anything if it runs before the comparisons. Asserted
    structurally: the refusal must appear ahead of the asyncio.gather that fans
    the stages out.
    """
    import inspect

    source = inspect.getsource(AdvancedBiometricVerifier.verify_speaker)
    assert "_dead_capture_reason" in source
    assert source.index("_dead_capture_reason") < source.index("asyncio.gather")
