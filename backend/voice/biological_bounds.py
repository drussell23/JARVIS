"""
One place that decides whether a number is a measurement of a human voice.

WHY THIS EXISTS
---------------
On 2026-08-06 the operator's own voice was rejected by his own profile::

    ⚠️  Spoofing indicators detected:
        [('unnatural_formants', 0.5), ('inconsistent_rate', 0.2)]
    ⚠️  Warnings: High uncertainty (53.9%), Voice physics constraints violated
    [CapabilityRouter] 'unlock_screen' NOT authorised (That didn't sound like you)

Neither indicator was a measurement. ``speaker_profiles`` on that machine held::

    speaking_rate_wpm = 420.0     # no human sustains 420 wpm; speech is 110-180
    formant_f1_hz     = 42.9      # F1 is ~500 Hz
    formant_f2_hz     = 80.03     # F2 is ~1500 Hz

PR #70426 taught the *anti-spoofing* stage to abstain on inputs like these. That
is a downstream mitigation: it stops the system reasoning from the garbage, it
does not stop the garbage being written. Two extractors were producing it:

  * ``enroll_voice._analyze_formants`` took peaks of the whole-utterance FFT **in
    frequency order** and called the first two F1/F2. At 16 kHz over a
    multi-second sample the bin spacing is sub-Hz and ``distance=20`` bins is
    ~4 Hz, so "the first two peaks" are DC-adjacent rumble — 42.9 Hz and
    80.0 Hz, precisely the values found on disk. On failure it returned a
    hardcoded ``(500.0, 1500.0)``.
  * ``advanced_feature_extraction.extract_features`` substituted textbook
    constants for every stage that raised — ``formants = [500, 1500, 2500,
    3500]``, ``pitch = 150/20/50``, ``hnr = 15.0``. A failure, wearing the
    units of a measurement.

Both are the arc's recurring defect: **a fault presented as a verdict.** The
answer is the same at every layer — say "I could not measure this", in a form
no downstream consumer can mistake for a value.

MEASURABILITY IS NOT PLAUSIBILITY
---------------------------------
These two bands are different and conflating them would disable the biometric:

  * A **measurability** band (this module) is the range a quantity occupies
    across *all* humans, generously drawn. Outside it, no human produced the
    number: the extractor failed, the unit is not the one declared, or the field
    was never filled in. Such a value carries no information and must be dropped.
  * A **plausibility** band (``PhysicsConstraints``' scoring thresholds) is the
    range a *typical* voice occupies. Inside the measurability band but outside
    this one is a real measurement of an unusual voice — evidence, to be scored,
    possibly penalised. Never dropped.

So ``pitch_mean_hz`` is measurable over 50-500 Hz while the scorer's typical band
is 85-255 Hz. Narrowing the first to the second would silently discard genuine
high-pitched speakers instead of scoring them.

THE TWO ABSENCE VALUES, AND WHY THERE ARE TWO
---------------------------------------------
  * ``None`` at the dict/database layer. SQL NULL is the store's own word for
    "no value", and every reader here already treats NULL as absent.
  * ``UNMEASURED`` (NaN) in ``float``-typed dataclass fields, where ``None``
    would be a type violation rippling through a dozen call sites. NaN is the
    IEEE encoding of exactly this fact, it is rejected by ``is_measured``, and
    it is loud: it propagates through arithmetic instead of averaging in as a
    plausible zero the way ``0.0`` does.

``0.0`` is never used for absence. That substitution is the original bug: an
unset 0.0 formant gives a ratio of 0.0, trips ``< 0.1``, and lands a confident
0.5 spoofing penalty on a voice nobody looked at.

Never raises. A validator that throws on a security path converts missing
evidence into an outage, and the caller learns nothing either way.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "UNMEASURED",
    "Bound",
    "BOUNDS",
    "bound_for",
    "canonical_name",
    "is_measured",
    "is_absent",
    "BiologicalBoundsValidator",
    "MeasuredAggregator",
    "SanitizeResult",
    "Violation",
    "default_validator",
]


#: The in-memory "this was not measured" float. NaN, not 0.0 — see module docs.
UNMEASURED = float("nan")


@dataclass(frozen=True)
class Bound:
    """
    The range a quantity occupies in real human speech, and why.

    ``low``/``high`` are inclusive. ``why`` is carried so a drop can explain
    itself in a log line without the reader going to find this table.
    """

    name: str
    low: float
    high: float
    unit: str
    why: str

    @property
    def min_env(self) -> str:
        return f"JARVIS_VOICE_PHYSICS_MIN_{self.name.upper()}"

    @property
    def max_env(self) -> str:
        return f"JARVIS_VOICE_PHYSICS_MAX_{self.name.upper()}"

    def contains(self, value: float) -> bool:
        return self.low <= value <= self.high

    def describe(self) -> str:
        return f"{self.low:g}-{self.high:g} {self.unit}"


def _b(name: str, low: float, high: float, unit: str, why: str) -> Tuple[str, Bound]:
    return name, Bound(name=name, low=low, high=high, unit=unit, why=why)


#: Canonical quantity -> measurability band.
#:
#: Only quantities with a *physiological* range appear here. A quantity with no
#: principled bound (energy, spectral flux, an embedding norm) is deliberately
#: absent: the validator then checks only that the value is a finite number and
#: passes it through, rather than inventing a limit to enforce. Adding a bound is
#: how a new quantity opts in; nothing needs to be edited elsewhere.
BOUNDS: Dict[str, Bound] = dict([
    _b("pitch_mean_hz", 50.0, 500.0, "Hz",
       "human f0 spans ~50 Hz (deep male creak) to ~500 Hz (child); "
       "outside this is not a pitch track"),
    # The 0.5 Hz floor is measurability, not typicality: a tracker that reports
    # zero spread across a whole utterance did not track anything. Genuinely
    # monotone delivery still lands above it and is scored, not dropped — that
    # is what the ``low_pitch_variation`` synthesis indicator is for.
    #
    # KNOWN TRADE-OFF, chosen deliberately. The pitch extractor now emits NaN
    # when no frame yielded a period, so it CAN distinguish "unset" from "a
    # measured spread of exactly 0.0" — which means this floor could drop to 0.0
    # and let a perfectly flat synthetic voice fire ``low_pitch_variation``
    # instead of abstaining. It stays at 0.5 because profiles written by the old
    # extractors are still on disk holding 0.0 as an unset default, and lowering
    # the floor would turn each of those into a 0.4 spoofing penalty against the
    # enrolled speaker — the precise defect this arc exists to remove. A false
    # negative on a degenerate input (a literally 0.000 Hz spread, which real
    # replay and conversion attacks do not produce, and which the embedding
    # comparison scores near zero anyway) is the cheaper error. Revisit once no
    # pre-#70427 profile remains.
    _b("pitch_std_hz", 0.5, 200.0, "Hz",
       "within-utterance f0 spread; >200 Hz is a tracker jumping octaves"),
    _b("pitch_range_hz", 0.0, 1000.0, "Hz",
       "max-min f0 over an utterance"),
    _b("formant_f1_hz", 150.0, 1200.0, "Hz",
       "F1 is set by vocal tract opening: ~250 Hz (/i/) to ~1000 Hz (/a/). "
       "42.9 Hz is DC rumble, not a formant"),
    _b("formant_f2_hz", 500.0, 3500.0, "Hz",
       "F2 tracks tongue advancement: ~600 Hz (/u/) to ~2800 Hz (/i/)"),
    _b("formant_f3_hz", 1200.0, 4500.0, "Hz",
       "F3 is largely speaker-anatomy; below F2 is a mis-ordered root set"),
    _b("formant_f4_hz", 2000.0, 5500.0, "Hz",
       "F4 approaches the Nyquist limit of a 16 kHz capture"),
    _b("speaking_rate_wpm", 40.0, 300.0, "wpm",
       "conversational speech is 110-180 wpm; trained auctioneers reach ~250. "
       "420 wpm is a word count divided by the wrong duration"),
    _b("pause_ratio", 0.0, 1.0, "ratio",
       "a fraction of frames; outside [0,1] is not a ratio"),
    _b("spectral_centroid_hz", 20.0, 8000.0, "Hz",
       "bounded above by Nyquist at the 16 kHz capture rate"),
    _b("spectral_rolloff_hz", 20.0, 8000.0, "Hz",
       "bounded above by Nyquist at the 16 kHz capture rate"),
    _b("jitter", 0.0, 0.5, "fraction",
       "cycle-to-cycle f0 perturbation; >50% is not a periodic signal"),
    _b("shimmer", 0.0, 0.5, "fraction",
       "cycle-to-cycle amplitude perturbation"),

    # ── The ``_percent`` columns are a SEPARATE quantity, not an alias ────
    #
    # The in-memory fields are fractions (``PhysicsConstraints.max_jitter =
    # 0.02`` means 2%) but the database columns are named ``jitter_percent`` /
    # ``shimmer_percent``, and TWO live writers disagree about which unit they
    # hold:
    #
    #   quick_voice_enhancement.py:1412   'jitter_percent': mean(jitters) * 100
    #   speaker_verification_service:2927 "jitter_percent": features.jitter
    #
    # The first scales to percent, the second stores the raw fraction. This
    # machine's row holds 1.0 and 5.0 — exactly 100x the old fabricated
    # defaults of 0.01 and 0.05, so this profile came from the percent writer.
    #
    # Treating the column as an alias of the fraction quantity, as a first
    # version of this file did, gave it the 0-0.5 band and flagged those stored
    # values as biologically impossible. They are not: 1% jitter and 5% shimmer
    # are textbook healthy-voice numbers. The boot sanitiser would have deleted
    # correctly-scaled real data on the strength of a unit guess — the exact
    # class of error this module exists to prevent, pointed the other way.
    #
    # So the columns get their own band, drawn to be impossible under EITHER
    # reading: 50 is beyond any real voice as a percentage AND far beyond one as
    # a fraction. Nothing legitimate is rejected under either unit, and nothing
    # impossible is admitted under either. The bound refuses to guess.
    #
    # FOLLOW-UP, deliberately not done here: unify the writers on one unit and
    # migrate the column. That is a data migration touching four call sites and
    # a stored schema, and doing it unverified at the end of this change would
    # risk rescaling real profiles by 100x — precisely the kind of silent
    # numeric damage that produced the 42.9 Hz formants.
    _b("jitter_percent", 0.0, 50.0, "fraction-or-percent",
       "unit-ambiguous column: bounded to what is impossible under either "
       "reading, pending writer unification"),
    _b("shimmer_percent", 0.0, 50.0, "fraction-or-percent",
       "unit-ambiguous column: bounded to what is impossible under either "
       "reading, pending writer unification"),
    _b("harmonic_to_noise_ratio_db", -10.0, 45.0, "dB",
       "voiced speech is ~7-25 dB; >45 dB has no vocal-fold noise floor "
       "and is synthesis"),
])


#: Names the rest of the system uses for the same quantity.
#:
#: The DB columns, the ``VoiceBiometricFeatures`` attributes and the enrollment
#: dataclass drifted apart long before this module existed. Mapping them is
#: strictly cheaper — and far more auditable — than renaming three schemas.
ALIASES: Dict[str, str] = {
    # VoiceBiometricFeatures attribute names
    "pitch_mean": "pitch_mean_hz",
    "pitch_std": "pitch_std_hz",
    "pitch_range": "pitch_range_hz",
    "formant_f1": "formant_f1_hz",
    "formant_f2": "formant_f2_hz",
    "formant_f3": "formant_f3_hz",
    "formant_f4": "formant_f4_hz",
    "spectral_centroid": "spectral_centroid_hz",
    "spectral_rolloff": "spectral_rolloff_hz",
    "speaking_rate": "speaking_rate_wpm",
    "harmonic_to_noise_ratio": "harmonic_to_noise_ratio_db",
    "hnr": "harmonic_to_noise_ratio_db",
    # Database column names that differ from the canonical key.
    #
    # ``jitter_percent`` and ``shimmer_percent`` are deliberately ABSENT: they
    # are registered as quantities in their own right above, because their unit
    # is not the same as the in-memory field's and aliasing them applied the
    # fraction band to percent data. See the note in BOUNDS.
    "speech_rate_wpm": "speaking_rate_wpm",
    "average_pitch_hz": "pitch_mean_hz",
    # Enrollment dataclass
    "rate": "speaking_rate_wpm",
}


def canonical_name(name: str) -> str:
    """The registry key for whatever this layer happens to call the quantity."""
    key = str(name).strip().lower()
    if key in BOUNDS:
        return key
    return ALIASES.get(key, key)


def bound_for(name: str) -> Optional[Bound]:
    """The band for ``name``, or ``None`` when the quantity has no physiological range."""
    return BOUNDS.get(canonical_name(name))


def is_absent(value: Any) -> bool:
    """
    True when ``value`` is one of the two absence forms, or unreadable as a number.

    This is the single ``None``/NaN predicate shared by the extractor, the
    anti-spoofing stage and the plausibility scorer, so the three cannot drift
    into disagreeing about what "missing" means.
    """
    if value is None:
        return True
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return True
    return not math.isfinite(float(value))


def is_measured(value: Any, low: Optional[float] = None, high: Optional[float] = None) -> bool:
    """
    True when ``value`` is a real measurement, inside its band when one is given.

    ``None``, NaN, infinity, non-numeric and out-of-band all read as NOT
    measured. Zero is excluded by every band whose quantity cannot be zero,
    which is deliberate: an unset float field defaults to 0.0, and 0 Hz is not
    a formant.

    The type check is strict on purpose. ``float("500")`` succeeds, so a
    permissive version accepts a feature field holding a *string* — but a
    numeric field that arrived as text was not produced by the measurement path,
    and quietly parsing it is the same leniency that let unset zeros become
    spoofing findings. ``bool`` is excluded explicitly because it is a subclass
    of ``int`` and ``float(True)`` is 1.0 — a silent 1 Hz.
    """
    if is_absent(value):
        return False
    numeric = float(value)
    if low is not None and numeric < low:
        return False
    if high is not None and numeric > high:
        return False
    return True


def _env_float(key: str, default: float, source: Mapping[str, str]) -> float:
    """
    A bound read from the environment, defaulting on anything unreadable.

    A malformed override must not reach a security decision: keeping the default
    is correct, and the warning names the knob so a typo does not persist as
    "the bound is not working".
    """
    raw = str(source.get(key, "")).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("[Bounds] %s=%r is not a number — keeping %g", key, raw, default)
        return default
    if not math.isfinite(value):
        logger.warning("[Bounds] %s=%r is not finite — keeping %g", key, raw, default)
        return default
    return value


@dataclass(frozen=True)
class Violation:
    """A quantity that failed its band, and by how much."""

    name: str
    value: Any
    bound: Optional[Bound]
    reason: str

    def describe(self) -> str:
        where = f" (band {self.bound.describe()})" if self.bound else ""
        return f"{self.name}={self.value!r}{where}: {self.reason}"


@dataclass
class SanitizeResult:
    """What survived a sanitisation pass, and what did not."""

    clean: Dict[str, Any] = field(default_factory=dict)
    dropped: List[Violation] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.dropped

    def describe(self) -> str:
        if not self.dropped:
            return "all measured"
        return "; ".join(v.describe() for v in self.dropped)

    def names(self) -> List[str]:
        return [v.name for v in self.dropped]


class BiologicalBoundsValidator:
    """
    The gate every DSP feature crosses before it is stored or scored.

    Built from the environment so a different microphone, codec or extractor
    version can be accommodated without a code change: every band is overridable
    as ``JARVIS_VOICE_PHYSICS_MIN_<NAME>`` / ``..._MAX_<NAME>``, the same
    namespace ``PhysicsConstraints`` already uses, so each bound has exactly one
    knob rather than two that can disagree.

    Instances are cheap and immutable in practice; ``default_validator()``
    returns a process-wide one for callers with no reason to hold their own.
    """

    def __init__(self, bounds: Optional[Mapping[str, Bound]] = None) -> None:
        self._bounds: Dict[str, Bound] = dict(bounds if bounds is not None else BOUNDS)

    # ── construction ────────────────────────────────────────────────────
    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "BiologicalBoundsValidator":
        source = os.environ if env is None else env
        resolved: Dict[str, Bound] = {}
        for name, bound in BOUNDS.items():
            low = _env_float(bound.min_env, bound.low, source)
            high = _env_float(bound.max_env, bound.high, source)
            if low > high:
                # An inverted band rejects everything, which would present as
                # "the extractor is broken" for every field at once. Refuse the
                # override rather than enforce a bound that cannot be satisfied.
                logger.warning(
                    "[Bounds] %s: overridden band %g-%g is inverted — keeping %s",
                    name, low, high, bound.describe(),
                )
                resolved[name] = bound
                continue
            resolved[name] = Bound(name=name, low=low, high=high,
                                   unit=bound.unit, why=bound.why)
        return cls(resolved)

    # ── introspection ───────────────────────────────────────────────────
    def bound(self, name: str) -> Optional[Bound]:
        return self._bounds.get(canonical_name(name))

    def bounds(self) -> Mapping[str, Bound]:
        return dict(self._bounds)

    # ── the three answers ───────────────────────────────────────────────
    def check(self, name: str, value: Any) -> Optional[Violation]:
        """
        ``None`` when ``value`` is a usable measurement of ``name``; the
        violation otherwise. This is the one place the decision is made — every
        other method here is a shape adapter over it.
        """
        if value is None:
            return Violation(name, value, self.bound(name), "absent")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return Violation(name, value, self.bound(name),
                             f"{type(value).__name__} is not a measurement")
        numeric = float(value)
        if not math.isfinite(numeric):
            return Violation(name, value, self.bound(name), "not finite")

        bound = self.bound(name)
        if bound is None:
            return None  # finite number, no physiological range to enforce
        if not bound.contains(numeric):
            return Violation(name, numeric, bound,
                             f"outside {bound.describe()} — {bound.why}")
        return None

    def validate(self, name: str, value: Any) -> Optional[float]:
        """The value as a float when measured, else ``None`` — for dicts and SQL."""
        return None if self.check(name, value) else float(value)

    def coerce(self, name: str, value: Any) -> float:
        """
        The value as a float when measured, else ``UNMEASURED`` — for ``float``
        dataclass fields, where ``None`` would be a type violation.
        """
        return UNMEASURED if self.check(name, value) else float(value)

    def measured(self, name: str, value: Any) -> bool:
        """True when ``value`` is a usable measurement of ``name``."""
        return self.check(name, value) is None

    # ── bulk shapes ─────────────────────────────────────────────────────
    def _is_measurement_field(self, name: str, value: Any) -> bool:
        """
        True when ``name``/``value`` is something this validator has any
        business judging.

        A profile row carries a speaker name, a BLOB, timestamps and counters
        alongside the acoustics. Without this, ``sanitize`` on a whole row nulls
        the speaker's NAME — "str is not a measurement" is true and completely
        beside the point, and it was caught by a test rather than by reading.

        A registered quantity is always judged. Anything else is judged only if
        it is already numeric, where the finiteness check is meaningful and the
        type check cannot fire.
        """
        if self.bound(name) is not None:
            return True
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    def sanitize(
        self,
        mapping: Mapping[str, Any],
        *,
        label: str = "features",
        only: Optional[Iterable[str]] = None,
    ) -> SanitizeResult:
        """
        A copy of ``mapping`` with every unmeasurable entry set to ``None``.

        The key is kept rather than removed: a caller building a SQL row needs
        the column to exist so it can be written NULL, and a caller comparing
        dicts needs the absence to be visible rather than inferred.

        Dropping is logged at WARNING, never silently — a value that failed its
        band means an extractor is broken, and that is operational news.
        """
        selected = set(only) if only is not None else None
        result = SanitizeResult()
        for key, value in mapping.items():
            if selected is not None and key not in selected:
                result.clean[key] = value
                continue
            if not self._is_measurement_field(key, value):
                result.clean[key] = value
                continue
            violation = self.check(key, value)
            if violation is None:
                result.clean[key] = value
            else:
                result.clean[key] = None
                result.dropped.append(violation)
        if result.dropped:
            logger.warning(
                "[Bounds] %s: %d field(s) dropped as unmeasurable — %s",
                label, len(result.dropped), result.describe(),
            )
        return result

    def audit(self, mapping: Mapping[str, Any]) -> List[Violation]:
        """
        Every entry of ``mapping`` that is present but biologically impossible.

        Absent entries are NOT violations: a profile that never recorded a
        formant is incomplete, not corrupt, and the two warrant different
        handling on disk. Only a value that is *there* and *impossible* means
        something wrote a fabrication.
        """
        violations: List[Violation] = []
        for key, value in mapping.items():
            if value is None:
                continue
            if self.bound(key) is None and not isinstance(value, (int, float)):
                continue  # unbounded non-numeric column (name, blob, timestamp)
            violation = self.check(key, value)
            if violation is not None:
                violations.append(violation)
        return violations


class MeasuredAggregator:
    """
    Reductions over per-sample features that ignore what was never measured.

    Enrolment averages N samples into the one profile every later verification
    is judged against, so the reduction is as load-bearing as the extraction.
    Two failure modes a bare ``np.mean`` has here:

      * one NaN makes the whole column NaN, discarding the other nineteen
        samples — which would turn the extractors' new honesty about failure
        into a total enrolment failure;
      * an out-of-band value is averaged in at full weight, dragging the
        enrolled figure toward a number no vocal tract produces.

    Every method returns ``UNMEASURED`` when nothing measurable survives, which
    the write gate turns into SQL NULL. It lives here rather than in either
    caller because ``quick_voice_enhancement`` and ``enroll_voice`` both need
    it, and because this module imports nothing heavier than the standard
    library — the enrolment scripts pull in the whole SpeechBrain stack, so a
    helper defined there cannot be exercised without it.
    """

    def __init__(self, validator: Optional[BiologicalBoundsValidator] = None) -> None:
        self._validator = validator or default_validator()

    def measured_values(self, quantity: str, values: Iterable[Any]) -> List[float]:
        return [float(v) for v in values if self._validator.measured(quantity, v)]

    def mean(self, quantity: str, values: Iterable[Any]) -> float:
        materialized = list(values)
        kept = self.measured_values(quantity, materialized)
        if not kept:
            logger.warning(
                "[Bounds] %s: no sample of %d was measurable — reporting "
                "unmeasured rather than a value nothing measured",
                quantity, len(materialized),
            )
            return UNMEASURED
        if len(kept) < len(materialized):
            logger.info(
                "[Bounds] %s: averaged %d of %d sample(s); %d unmeasurable",
                quantity, len(kept), len(materialized), len(materialized) - len(kept),
            )
        return sum(kept) / len(kept)

    def std(self, quantity: str, values: Iterable[Any]) -> float:
        """
        Population standard deviation, or ``UNMEASURED``.

        Fewer than two measured samples gives ``UNMEASURED`` rather than 0.0: a
        single sample produces a zero spread, which reads as "this speaker is
        perfectly consistent" when it means "there was nothing to compare".
        """
        kept = self.measured_values(quantity, values)
        if len(kept) < 2:
            return UNMEASURED
        mean = sum(kept) / len(kept)
        return (sum((v - mean) ** 2 for v in kept) / len(kept)) ** 0.5

    def min(self, quantity: str, values: Iterable[Any]) -> float:
        kept = self.measured_values(quantity, values)
        return min(kept) if kept else UNMEASURED

    def max(self, quantity: str, values: Iterable[Any]) -> float:
        kept = self.measured_values(quantity, values)
        return max(kept) if kept else UNMEASURED

    def dynamic_range_db(self, energies: Iterable[Any]) -> float:
        """
        Peak-to-floor energy in dB, or ``UNMEASURED``.

        The form this replaces was ``20*log10(max/(min + 1e-10) + 1e-10)``,
        where the epsilon turned a zero floor — silence in at least one sample —
        into a ~200 dB range presented as a measurement of the speaker's
        dynamics. A non-positive floor means there is no floor to measure.
        """
        kept = [float(e) for e in energies
                if isinstance(e, (int, float)) and not isinstance(e, bool)
                and math.isfinite(e) and e > 0]
        if len(kept) < 2:
            return UNMEASURED
        return 20.0 * math.log10(max(kept) / min(kept))


_DEFAULT: Optional[BiologicalBoundsValidator] = None


def default_validator() -> BiologicalBoundsValidator:
    """
    The process-wide validator, built from the environment on first use.

    Lazy rather than module-level so a test can set ``JARVIS_VOICE_PHYSICS_*``
    before the first call, and so importing this module never reads the
    environment as a side effect.
    """
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = BiologicalBoundsValidator.from_env()
    return _DEFAULT


def reset_default_validator() -> None:
    """Drop the cached process-wide validator (tests that change the env)."""
    global _DEFAULT
    _DEFAULT = None
