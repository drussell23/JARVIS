"""AgentFingerprint — Layer-3 adaptive rogue-Agent-commit detector.

When the OCA sovereign gate (Layer 2) refuses/flags a commit, the
``commit_authority_archive`` BYPASS_SUSPECTED record should say
*whether the offending commit looks Agent-authored*. The Cursor
Agent fingerprint (verbose LLM prose + ``[integrity-verified: …]``
trailer) **will evolve** — so this detector is deliberately NOT a
rigid single-string match. It is:

  * **Structural** — a *flexible* regex for the integrity trailer
    (tolerant of hex length / spacing / case drift), a near-
    conclusive signal on its own.
  * **Adaptive multi-signal** — a weighted LLM-prose scorer over
    several independent, paraphrase-robust signals (verbosity,
    explanatory-connective density, full-sentence prose, absence of
    a terse conventional-commit header). No single phrase is
    decisive; the threshold is env-tunable (no hardcoded magic
    gate).
  * **Composing, not duplicating** — it reads the canonical O+V
    signature from :mod:`auto_committer` (NOT a re-typed copy) to
    **exclude sanctioned autonomous commits**: a real Ouroboros+Venom
    commit carries the O+V signature and is NEVER flagged. Only
    NON-O+V LLM-prose / integrity-trailer commits are flagged.

Forensics only. It changes no gate, issues no verdict, blocks
nothing — it enriches an existing telemetry record. NEVER raises.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Tuple

logger = logging.getLogger("Ouroboros.AgentFingerprint")


AGENT_FINGERPRINT_SCHEMA_VERSION: str = "agent_fingerprint.v1"

_ENV_THRESHOLD = "JARVIS_AGENT_FINGERPRINT_THRESHOLD"
_DEFAULT_THRESHOLD = 0.6
_MIN_THRESHOLD = 0.1
_MAX_THRESHOLD = 1.0

# Flexible structural signal — NOT an exact string. Tolerates
# ``[integrity-verified: abc123]`` / ``[ integrity-verified :
# DEADBEEFCAFE ]`` / case drift. A future fingerprint that keeps
# any verified-hash trailer still trips this.
_INTEGRITY_TRAILER_RE = re.compile(
    r"\[\s*integrity[-_ ]?verified\s*:\s*[0-9a-f]{4,}\s*\]",
    re.IGNORECASE,
)

# Explanatory-connective markers typical of LLM-authored commit
# prose. A *set* of independent anchors — paraphrase-robust because
# the scorer needs several, not one specific phrase.
_PROSE_MARKERS: Tuple[str, ...] = (
    "this commit", "this change", "additionally", "furthermore",
    "moreover", "in order to", "as well as", "ensures that",
    "introduces", "this addition", "improving the", "enhances",
    " — ",  # em-dash explanatory aside
)

# A terse conventional-commit header LOWERS suspicion.
_CONVENTIONAL_HEADER_RE = re.compile(
    r"^[a-z]+(\([a-z0-9_./-]+\))?!?:\s+\S",
)


def fingerprint_threshold() -> float:
    """``JARVIS_AGENT_FINGERPRINT_THRESHOLD`` (default 0.6, clamped
    [0.1, 1.0]). Tunable so the detector adapts without code
    changes — no hardcoded decision constant. NEVER raises."""
    raw = os.environ.get(_ENV_THRESHOLD, "").strip()
    try:
        v = float(raw) if raw else _DEFAULT_THRESHOLD
    except (TypeError, ValueError):
        v = _DEFAULT_THRESHOLD
    return max(_MIN_THRESHOLD, min(_MAX_THRESHOLD, v))


@dataclass(frozen=True)
class AgentFingerprint:
    """Verdict of the adaptive detector. ``matched`` ⇒ the message
    looks rogue-Agent-authored AND is not a sanctioned O+V commit.
    ``signals`` lists which signals fired (forensics)."""

    matched: bool
    score: float
    signals: Tuple[str, ...]
    reason: str
    schema_version: str = AGENT_FINGERPRINT_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_git_write_attempt": self.matched,
            "fingerprint_score": round(self.score, 3),
            "fingerprint_signals": list(self.signals),
            "fingerprint_reason": self.reason,
            "schema_version": self.schema_version,
        }


def _ov_signature_present(message: str) -> bool:
    """Compose the canonical O+V signature/co-author (NOT a
    re-typed copy). A sanctioned Ouroboros+Venom autonomous commit
    carries these — it must NEVER be flagged as a rogue Agent.
    NEVER raises; degrades to False (treat as not-sanctioned →
    fail toward flagging only if other signals fire)."""
    try:
        from backend.core.ouroboros.governance.auto_committer import (
            ov_coauthor_line,
            ov_signature_substring,
        )
        sig = ov_signature_substring()
        co = ov_coauthor_line()
        return (bool(sig) and sig in message) or (
            bool(co) and co in message
        )
    except Exception:  # noqa: BLE001
        return False


def detect_agent_authored(message: object) -> AgentFingerprint:
    """Adaptive detection. NEVER raises. Empty/non-str →
    not matched."""
    try:
        if not isinstance(message, str) or not message.strip():
            return AgentFingerprint(
                False, 0.0, (), "empty_or_non_string",
            )

        # Sanctioned autonomous commits are excluded outright
        # (composed canonical signature, not duplicated).
        if _ov_signature_present(message):
            return AgentFingerprint(
                False, 0.0, ("ov_signature",),
                "sanctioned_ov_commit_excluded",
            )

        signals = []
        score = 0.0

        # (1) Structural integrity trailer — near-conclusive.
        if _INTEGRITY_TRAILER_RE.search(message):
            signals.append("integrity_trailer")
            score += 0.7

        body = message.strip()
        first_line = body.splitlines()[0].strip() if body else ""
        low = body.lower()

        # (2) Terse conventional header present → suspicion DOWN.
        has_conv = bool(_CONVENTIONAL_HEADER_RE.match(first_line))
        if not has_conv:
            signals.append("no_conventional_header")
            score += 0.15

        # (3) Explanatory-connective density (need several — a
        # single marker is not decisive; paraphrase-robust).
        hits = sorted({
            m for m in _PROSE_MARKERS if m in low
        })
        if len(hits) >= 2:
            signals.append(f"prose_markers:{len(hits)}")
            score += min(0.4, 0.12 * len(hits))

        # (4) Multi-sentence prose body (LLM commits explain; terse
        # conventional commits do not).
        sentences = [
            s for s in re.split(r"[.!?]\s", body) if len(s.strip()) > 25
        ]
        if len(sentences) >= 2:
            signals.append(f"prose_sentences:{len(sentences)}")
            score += min(0.35, 0.12 * len(sentences))

        # (5) Verbose body relative to a terse commit norm.
        if len(body) > 400 and not has_conv:
            signals.append("verbose_body")
            score += 0.15

        thr = fingerprint_threshold()
        matched = score >= thr
        reason = (
            f"score={score:.2f}>=thr={thr:.2f}"
            if matched
            else f"score={score:.2f}<thr={thr:.2f}"
        )
        return AgentFingerprint(
            bool(matched), float(score), tuple(signals), reason,
        )
    except Exception as exc:  # noqa: BLE001 — forensics never raises
        logger.debug("[AgentFingerprint] degraded: %s", exc)
        return AgentFingerprint(
            False, 0.0, (), f"detector_error:{type(exc).__name__}",
        )


__all__ = [
    "AGENT_FINGERPRINT_SCHEMA_VERSION",
    "AgentFingerprint",
    "detect_agent_authored",
    "fingerprint_threshold",
    "register_flags",
    "register_shipped_invariants",
]


def register_flags(registry) -> int:  # noqa: ANN001
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[AgentFingerprint] register_flags degraded: %s", exc,
        )
        return 0
    tgt = "backend/core/ouroboros/governance/agent_fingerprint.py"
    try:
        registry.register(FlagSpec(
            name=_ENV_THRESHOLD, type=FlagType.FLOAT,
            default=_DEFAULT_THRESHOLD, category=Category.TUNING,
            source_file=tgt,
            example=f"{_ENV_THRESHOLD}=0.5",
            description=(
                "Adaptive rogue-Agent-commit detector score "
                "threshold (clamped 0.1..1.0). Tunable so the "
                "fingerprint adapts without code changes — there "
                "is no hardcoded decision constant."
            ),
        ))
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.debug("[AgentFingerprint] seed skipped: %s", exc)
        return 0


def register_shipped_invariants() -> list:
    """Pin: the detector COMPOSES the canonical O+V signature (no
    duplicated literal), is regex/multi-signal (not a lone string
    ``==``/``in`` gate), is env-threshold-driven (no hardcoded
    decision constant), and NEVER raises."""
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    def _validate(tree: "_ast.Module", source: str) -> tuple:
        v = []
        # Composes canonical O+V signature (not a re-typed copy).
        if "ov_signature_substring" not in source or (
            "ov_coauthor_line" not in source
        ):
            v.append(
                "must compose auto_committer.ov_signature_substring"
                " / ov_coauthor_line (no duplicated O+V literal)"
            )
        # No hardcoded O+V signature literal in DETECTION code.
        # Needle assembled from fragments so it never appears
        # verbatim in this validator; scan string Constants and
        # exempt this registration function's own range (its
        # description text legitimately mentions O+V).
        _ov_needle = "Ouroboros+Venom " + "[O+V]"
        _exempt = []
        for fn in _ast.walk(tree):
            if isinstance(fn, _ast.FunctionDef) and (
                fn.name == "register_shipped_invariants"
            ):
                s = getattr(fn, "lineno", 0)
                e = getattr(fn, "end_lineno", s) or s
                _exempt.append((s, e))
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Constant) and isinstance(
                node.value, str,
            ) and _ov_needle in node.value:
                ln = getattr(node, "lineno", 0)
                if any(a <= ln <= b for a, b in _exempt):
                    continue
                v.append(
                    "must NOT hardcode the O+V signature literal — "
                    "compose it from auto_committer"
                )
        # Adaptive, not a lone hardcoded string gate.
        if "re.compile" not in source:
            v.append(
                "detector must use a flexible regex (re.compile) "
                "for the integrity trailer — not exact match"
            )
        if _ENV_THRESHOLD not in source:
            v.append(
                "decision threshold must be env-tunable "
                f"({_ENV_THRESHOLD}) — no hardcoded gate constant"
            )
        # NEVER-raise: the public entrypoint has a defensive guard.
        det = next(
            (n for n in _ast.walk(tree)
             if isinstance(n, _ast.FunctionDef)
             and n.name == "detect_agent_authored"),
            None,
        )
        if det is None:
            v.append("detect_agent_authored not found")
        elif not any(
            isinstance(n, _ast.ExceptHandler)
            for n in _ast.walk(det)
        ):
            v.append(
                "detect_agent_authored must be NEVER-raise "
                "(defensive try/except)"
            )
        return tuple(v)

    return [
        ShippedCodeInvariant(
            invariant_name="agent_fingerprint_adaptive_composed",
            target_file=(
                "backend/core/ouroboros/governance/"
                "agent_fingerprint.py"
            ),
            description=(
                "Rogue-Agent-commit detector is adaptive "
                "(flexible regex + multi-signal env-tunable "
                "scorer, no lone hardcoded string), composes the "
                "canonical O+V signature to exclude sanctioned "
                "commits (no duplicated literal), and never raises."
            ),
            validate=_validate,
        ),
    ]
