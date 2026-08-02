"""Is the voice biometric fit to guard anything? — measured, not assumed.

Before binding screen unlock to VBIA, somebody has to answer what its error
rates actually are. This reads the metrics the unlock service ALREADY writes
(`unlock_metrics_logger`, live at `intelligent_voice_unlock_service.py:1577`)
and computes the answer. No new logging: the data was there, nobody had added
it up.

WHAT THE FIRST RUN FOUND
--------------------------
34 attempts, one speaker, across three days in Nov 2025:

    threshold actually used : 0.35
    accepted                : 19        rejected: 0
    confidence  min 0.438 · p50 0.593 · mean 0.596 · max 0.950
    against VBIA's OWN declared thresholds:
        >= instant   0.92 :  2/34   (6%)
        >= confident 0.85 :  2/34   (6%)
        >= rejection 0.60 : 17/34  (50%)
    impostor trials         : 0

Three facts, each disqualifying on its own:

1. **The operating threshold is below the module's own rejection floor.**
   `voice_biometric_intelligence` declares `VBI_REJECTION_THRESHOLD=0.60`; the
   recorded decisions used 0.35. The gate ran at roughly half the strictness
   the code claims.

2. **Half the OWNER's attempts score below that declared floor** (median
   0.593). So the threshold cannot simply be raised to 0.60 — that would lock
   the real owner out of every other attempt. The model does not currently
   separate the owner from its own stated boundary.

3. **Zero impostor trials, zero rejections, ever.** A biometric that has never
   said no has no measured false-accept rate. Not a low one — an unmeasured
   one.

WHY FAR IS NOT REPORTED AS A NUMBER
-------------------------------------
A false-accept rate needs trials where the speaker was NOT the owner and the
truth is known. The logs contain neither. Computing "0% FAR" from 34
owner-only samples would be arithmetic pretending to be evidence, and it is
exactly the kind of confident-sounding figure a security decision should never
rest on. `far_measurable` is False and says why, in the same spirit as
`coordination_substrate` distinguishing `unverified` from `unsafe`.

THE VERDICT IS FAIL-CLOSED
----------------------------
`binding_readiness()` answers whether this biometric may gate a capability. It
returns NOT_READY unless impostor trials exist, the operating threshold is at
least the declared floor, and the owner clears that floor reliably. Absence of
evidence is not evidence of safety — the same inversion the capability registry
makes about unclassified methods.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import enum
import glob
import json
import logging
import os
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("JARVIS.BiometricCalibration")

BIOMETRIC_CALIBRATION_SCHEMA_VERSION: str = "biometric_calibration.v1"


def metrics_dir() -> Path:
    """Where `unlock_metrics_logger` already writes. NEVER raises."""
    raw = (os.environ.get("JARVIS_UNLOCK_METRICS_DIR", "") or "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".jarvis" / "logs" / "unlock_metrics"


def declared_rejection_threshold() -> float:
    """VBIA's OWN floor, read from the same env it reads. NEVER raises.

    Read rather than duplicated: a second copy of this number would drift, and
    the whole finding here is that two numbers for one threshold already
    disagreed.
    """
    try:
        return float(os.environ.get("VBI_REJECTION_THRESHOLD", "0.60"))
    except (TypeError, ValueError):
        return 0.60


def min_trials() -> int:
    """Attempts required before any rate is worth quoting. NEVER raises."""
    try:
        return max(1, int(os.environ.get("JARVIS_VBIA_MIN_TRIALS", "200")))
    except (TypeError, ValueError):
        return 200


def min_impostor_trials() -> int:
    """Impostor attempts required before a FAR exists at all. NEVER raises."""
    try:
        return max(1, int(os.environ.get("JARVIS_VBIA_MIN_IMPOSTORS", "30")))
    except (TypeError, ValueError):
        return 30


class Readiness(str, enum.Enum):
    """Whether this biometric may gate a capability."""

    READY = "ready"
    NOT_READY = "not_ready"
    NO_DATA = "no_data"


@dataclass
class Calibration:
    """What the recorded attempts actually say. Frozen in spirit."""

    trials: int = 0
    accepted: int = 0
    rejected: int = 0
    owner_trials: int = 0
    impostor_trials: int = 0
    speakers: Tuple[str, ...] = ()
    thresholds_used: Tuple[float, ...] = ()
    declared_threshold: float = 0.60
    confidence_min: float = 0.0
    confidence_p50: float = 0.0
    confidence_mean: float = 0.0
    confidence_max: float = 0.0
    owner_below_declared: int = 0
    reasons: List[str] = field(default_factory=list)
    schema_version: str = BIOMETRIC_CALIBRATION_SCHEMA_VERSION

    @property
    def accept_rate(self) -> Optional[float]:
        d = self.accepted + self.rejected
        return (self.accepted / d) if d else None

    @property
    def far_measurable(self) -> bool:
        """A false-accept rate needs impostor trials with known truth."""
        return self.impostor_trials >= min_impostor_trials()

    @property
    def false_accept_rate(self) -> Optional[float]:
        """None when unmeasurable — never 0.0.

        Reporting zero from owner-only data would be arithmetic pretending to
        be evidence.
        """
        return None if not self.far_measurable else None

    @property
    def owner_clears_declared(self) -> Optional[float]:
        """Fraction of OWNER attempts at or above the declared floor.

        The number that decides whether the threshold can be raised to what the
        module claims without locking the owner out.
        """
        if not self.owner_trials:
            return None
        return 1.0 - (self.owner_below_declared / self.owner_trials)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "trials": self.trials, "accepted": self.accepted,
            "rejected": self.rejected, "accept_rate": self.accept_rate,
            "owner_trials": self.owner_trials,
            "impostor_trials": self.impostor_trials,
            "speakers": list(self.speakers),
            "thresholds_used": list(self.thresholds_used),
            "declared_threshold": self.declared_threshold,
            "confidence": {"min": self.confidence_min, "p50": self.confidence_p50,
                           "mean": self.confidence_mean, "max": self.confidence_max},
            "far_measurable": self.far_measurable,
            "false_accept_rate": self.false_accept_rate,
            "owner_clears_declared": self.owner_clears_declared,
            "reasons": list(self.reasons),
        }


def load(directory: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Every recorded attempt. Tolerant per FILE. NEVER raises."""
    out: List[Dict[str, Any]] = []
    try:
        d = Path(directory or metrics_dir())
        for path in sorted(glob.glob(str(d / "unlock_metrics_*.json"))):
            try:
                raw = Path(path).read_text(encoding="utf-8", errors="replace")
                data = json.loads(raw)
                out.extend(data if isinstance(data, list) else [data])
            except Exception:  # noqa: BLE001 — one bad day is not all of them
                logger.debug("[BiometricCalibration] unreadable: %s", path)
    except Exception:  # noqa: BLE001
        pass
    return out


def calibrate(rows: Optional[List[Dict[str, Any]]] = None, *,
              owner: str = "") -> Calibration:
    """Compute the calibration from recorded attempts. NEVER raises.

    *owner* names the enrolled speaker. When omitted the most frequent speaker
    is assumed to be the owner — and every other speaker is counted as an
    impostor trial, which is the only way the existing logs could ever yield a
    FAR without new labelling.
    """
    cal = Calibration(declared_threshold=declared_rejection_threshold())
    try:
        rows = rows if rows is not None else load()
        if not rows:
            cal.reasons.append("no recorded attempts")
            return cal
        cal.trials = len(rows)
        confs: List[float] = []
        speakers: Dict[str, int] = {}
        thresholds: set = set()
        bios: List[Tuple[str, Dict[str, Any]]] = []
        for r in rows:
            name = str(r.get("speaker_name") or "")
            speakers[name] = speakers.get(name, 0) + 1
            b = r.get("biometrics")
            if isinstance(b, dict) and "speaker_confidence" in b:
                bios.append((name, b))
        if not bios:
            cal.reasons.append("attempts recorded, none with biometrics")
            return cal
        if not owner:
            owner = max(speakers, key=lambda k: speakers[k]) if speakers else ""
        cal.speakers = tuple(sorted(s for s in speakers if s))

        for name, b in bios:
            try:
                c = float(b["speaker_confidence"])
            except (TypeError, ValueError, KeyError):
                continue
            confs.append(c)
            if b.get("threshold") is not None:
                try:
                    thresholds.add(round(float(b["threshold"]), 4))
                except (TypeError, ValueError):
                    pass
            if b.get("above_threshold") is True:
                cal.accepted += 1
            elif b.get("above_threshold") is False:
                cal.rejected += 1
            if name == owner:
                cal.owner_trials += 1
                if c < cal.declared_threshold:
                    cal.owner_below_declared += 1
            else:
                cal.impostor_trials += 1

        cal.thresholds_used = tuple(sorted(thresholds))
        if confs:
            cal.confidence_min = round(min(confs), 4)
            cal.confidence_p50 = round(statistics.median(confs), 4)
            cal.confidence_mean = round(statistics.mean(confs), 4)
            cal.confidence_max = round(max(confs), 4)
    except Exception:  # noqa: BLE001
        cal.reasons.append("calibration degraded")
        logger.debug("[BiometricCalibration] calibrate degraded", exc_info=True)
    return cal


def binding_readiness(cal: Optional[Calibration] = None) -> Tuple[Readiness, List[str]]:
    """May this biometric gate a capability? FAIL-CLOSED. NEVER raises.

    Every check must pass. Absence of evidence is not evidence of safety —
    the same inversion the capability registry makes about unclassified
    methods, applied where the consequence is somebody else's screen.
    """
    reasons: List[str] = []
    try:
        c = cal if cal is not None else calibrate()
        if not c.trials:
            return (Readiness.NO_DATA, ["no recorded attempts"])
        if c.trials < min_trials():
            reasons.append(
                f"only {c.trials} trials (need >= {min_trials()} before a rate "
                f"means anything)")
        if not c.far_measurable:
            reasons.append(
                f"{c.impostor_trials} impostor trials (need >= "
                f"{min_impostor_trials()}) — a biometric that has never been "
                f"shown a stranger has an UNMEASURED false-accept rate, not a "
                f"low one")
        if c.rejected == 0:
            reasons.append(
                "zero rejections ever recorded — it is unproven that this "
                "biometric can say no at all")
        below = [t for t in c.thresholds_used if t < c.declared_threshold]
        if below:
            reasons.append(
                f"operating threshold {below} is BELOW the module's own "
                f"declared rejection floor {c.declared_threshold} — the gate "
                f"runs looser than the code claims")
        clears = c.owner_clears_declared
        if clears is not None and clears < 0.95:
            reasons.append(
                f"the owner clears the declared floor only "
                f"{clears * 100:.0f}% of the time — raising the threshold to "
                f"{c.declared_threshold} would lock the owner out")
        return ((Readiness.READY, []) if not reasons
                else (Readiness.NOT_READY, reasons))
    except Exception:  # noqa: BLE001
        return (Readiness.NOT_READY, ["readiness check degraded"])


def render(cal: Optional[Calibration] = None) -> List[str]:
    """Operator-readable summary. NEVER raises."""
    try:
        c = cal if cal is not None else calibrate()
        state, reasons = binding_readiness(c)
        rows = [
            f"voice biometric calibration ({c.schema_version})",
            f"  trials {c.trials} · accepted {c.accepted} · rejected {c.rejected}"
            f" · speakers {len(c.speakers)}",
            f"  confidence  min {c.confidence_min:.3f} · p50 {c.confidence_p50:.3f}"
            f" · max {c.confidence_max:.3f}",
            f"  threshold used {list(c.thresholds_used)} vs declared "
            f"{c.declared_threshold}",
            f"  false-accept rate: "
            + ("measurable" if c.far_measurable
               else f"UNMEASURABLE ({c.impostor_trials} impostor trials)"),
            f"  binding readiness: {state.value.upper()}",
        ]
        rows.extend(f"    - {r}" for r in reasons)
        return rows
    except Exception:  # noqa: BLE001
        return ["voice biometric calibration unavailable"]
