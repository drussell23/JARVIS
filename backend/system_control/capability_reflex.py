"""Resolve a spoken sentence to a capability WITHOUT a model.

THE DEFECT THIS EXISTS TO DELETE
---------------------------------
An operator said "lock my screen" to a Mac that has had `lock_screen` all
along, in a registry that already derives it, behind a router that already
knows how to gate and call it. Nothing happened. The measured chain::

    VoiceCommandRouter.route
      → _classify(...)                 → LLM call   → REFUSED
      → except: fall back to "composite"
      → ToolUseOrchestrator.execute    → LLM call   → REFUSED
      → CommandResult(success=False, steps_completed=0)

Two model round-trips stood between a sentence and a system call, and the
FALLBACK for the first was the second — the classifier's failure path routed
to the strictly more model-dependent branch. So one provider fault took the
whole organism from "fully capable" to "cannot lock a screen", and the four
faults visible in that single boot log (session-budget refusal, DW 403
entitlement denial, DW streaming outage, J-Prime unreachable) were each
independently sufficient to cause it.

That is a reflex routed through the cortex. A spinal reflex does not consult
the brain, and the reason is not speed — it is that the brain is the part that
can be unavailable.

WHY THIS IS NOT A PHRASE TABLE
--------------------------------
`intent_classifier.py` already has a function it calls a "reflex arc". It is
~180 lines of hand-written phrase tuples, it resolves to a CATEGORY rather
than a capability (it can say "lock screen" is `system`; it cannot name
`lock_screen` or call it), and it is not wired to the HUD at all. A fourth
hand-kept vocabulary would be the exact defect `capability_registry` was
written to delete, in a new costume.

So the lexicon is DERIVED from the same reflection the tool schemas come from.
`lock_screen` is speakable because its NAME is {lock, screen} and the operator
said both words — not because anybody wrote "lock screen" down. A capability
annotated tomorrow is speakable tomorrow, and one that is renamed changes what
it answers to in the same commit.

THE FOUR REFUSALS
-------------------
A reflex that fires when it is unsure is worse than no reflex, because the
thing on the other side is somebody's screen locking mid-sentence. This
declines in four separate ways, and every one of them hands the sentence to
the model rather than guessing:

* **Partial name coverage.** Every token of the capability's name must be
  present as a WHOLE WORD. This is what keeps `lock` out of `unlock`: "unlock
  my screen" tokenises to {unlock, screen} and contains no `lock` token at
  all, so `lock_screen` scores zero rather than 0.5. Substring matching here
  would invert the operator's intent on the single most dangerous pair on the
  surface.

* **Ambiguity.** The winner must beat the runner-up by a margin. Two plausible
  readings is not a close call to be settled by rounding; it is a question.

* **Arguments.** A capability with a required parameter needs a value pulled
  out of a sentence, and pulling values out of sentences is what a model is
  genuinely better at. `lock_screen()` is a complete call. `open_app(app_name)`
  is not, and the reflex never invents one.

* **Shape.** Negations, interrogatives and conjunctions are refused outright.
  "don't lock my screen", "how do I lock my screen" and "lock my screen and
  open Chrome" all contain a perfect match for `lock_screen` and none of them
  is a request to lock the screen right now.

CONFIDENCE IS NOT ONE NUMBER
------------------------------
The bar depends on what happens if the reflex is WRONG, which is not the same
question for every capability.

A gated capability (anything above `SAFE_AUTO`) suspends into a Touch ID
prompt that names it. A misread there is shown to the operator, in the system
dialog, before a single thing happens — the consent boundary IS the
confirmation. An ungated capability just runs. So ungated calls need the top
bar and gated ones can accept a little less, which is the opposite of the
instinct to make dangerous things need more certainty, and is right for the
same reason a seatbelt does not make you drive slower: the mitigation is
already downstream.

Session starts are exempted back up to the top bar. Duration is its own risk
class — `Session` says so, and a continuous observer opened by a misheard
sentence is not made acceptable by having been consented to once.

WHAT THIS DOES NOT DO
-----------------------
It does not execute anything. It resolves a name and hands it to
`CapabilityRouter`, which still applies the registry's tier, the Iron Gate,
consent, the nonce challenge and the lease book. The reflex is a faster way to
ARRIVE at the gate, never a way around it. There is deliberately no setting
that makes a gated capability ungated.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import enum
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("JARVIS.CapabilityReflex")

CAPABILITY_REFLEX_SCHEMA_VERSION: str = "capability_reflex.v1"


# ── Config (env-resolved at READ time, per the module convention) ───────────

def reflex_enabled() -> bool:
    """Master gate. Default TRUE. NEVER raises.

    Off means every sentence goes to the model exactly as before — which is
    the behaviour that produced the outage this module answers, so `off` is a
    rollback switch rather than a supported posture.
    """
    return (os.environ.get("JARVIS_CAPABILITY_REFLEX_ENABLED", "true")
            or "").strip().lower() not in ("0", "false", "no", "off")


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    """A clamped float from env. NEVER raises."""
    try:
        raw = (os.environ.get(name, "") or "").strip()
        return max(lo, min(hi, float(raw))) if raw else default
    except (TypeError, ValueError):
        return default


def certain_threshold() -> float:
    """Score an UNGATED capability must reach to fire. NEVER raises."""
    return _env_float("JARVIS_REFLEX_CERTAIN_SCORE", 0.85, 0.5, 1.0)


def gated_threshold() -> float:
    """Score a GATED capability must reach — see the module docstring."""
    return _env_float("JARVIS_REFLEX_GATED_SCORE", 0.70, 0.5, 1.0)


def ambiguity_margin() -> float:
    """How far the winner must beat the runner-up. NEVER raises."""
    return _env_float("JARVIS_REFLEX_MARGIN", 0.15, 0.0, 1.0)


def lexicon_ttl_s() -> float:
    """How long a derived lexicon is reused. NEVER raises.

    Short, because a federated namespace finishing its 8.4 s hydration should
    become speakable within seconds rather than at the next restart — the same
    reason `derived_tool_names` reads per call instead of snapshotting.
    """
    return _env_float("JARVIS_REFLEX_LEXICON_TTL_S", 30.0, 1.0, 3600.0)


# ── Linguistic constants ────────────────────────────────────────────────────
#
# These ARE hardcoded, and the distinction from a phrase table matters. A
# phrase table encodes WHAT THE MACHINE CAN DO, which is exactly the knowledge
# that goes stale the moment a capability is added — that is derived, below,
# and never written here. These encode HOW ENGLISH WORKS, which does not
# change when somebody annotates a method. The one is a duplicate of the
# codebase; the other is a property of the language it is being spoken in.

#: Function words carrying no capability signal. Applied to BOTH sides — the
#: utterance AND the derived name/description tokens — because stripping
#: "get" from what the operator said while keeping it in `get_battery` would
#: make the capability permanently unreachable by its own name.
_FILLER = frozenset("""
a an the my mine your yours our its it this that these those please just now
right away then some any all for to of on in at with from up down out off
i you we he she they me us him her them am is are was were be been being do
does did done have has had will would shall should can could may might must
jarvis hey ok okay computer thanks thank kindly again also very really quite
""".split())

#: Refusal triggers. Each is a shape whose presence means the sentence is not
#: a bare imperative, however well its content words match.
_NEGATIONS = frozenset("""
not no never dont don t cant cannot can wont won shouldnt stop cancel without
undo nevermind
""".split())

#: Interrogative CONTENT words. Deliberately excludes modal politeness — "can
#: you lock my screen" is an imperative wearing a question mark, while "how do
#: I lock my screen" is a request for instructions and must never lock it.
_INTERROGATIVES = frozenset(
    "what whats how why when where who whom whose which".split())

#: Multi-step joiners. Their presence alongside unexplained content words says
#: the sentence contains more than one act, which is a plan, not a reflex.
_CONJUNCTIONS = frozenset("and then after before while also plus".split())

_WORD = re.compile(r"[a-z0-9]+")


def _extra_filler() -> frozenset:
    """Operator-supplied filler, e.g. a nickname the wake word misses."""
    try:
        raw = os.environ.get("JARVIS_REFLEX_EXTRA_FILLER", "") or ""
        return frozenset(t for t in _WORD.findall(raw.lower()) if t)
    except Exception:  # noqa: BLE001
        return frozenset()


def tokenize(text: str) -> List[str]:
    """Whole-word tokens, lowercased. NEVER raises.

    Word boundaries rather than substrings, and that is the load-bearing
    property of this whole module: `"lock" in "unlock my screen"` is True and
    `"lock" in ["unlock", "my", "screen"]` is False. The first would have
    locked a screen the operator asked to unlock.
    """
    try:
        return _WORD.findall((text or "").lower())
    except Exception:  # noqa: BLE001
        return []


def content_tokens(text: str) -> List[str]:
    """Tokens that carry capability signal, in order. NEVER raises."""
    filler = _FILLER | _extra_filler()
    return [t for t in tokenize(text) if t not in filler]


# ── The derived lexicon ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Signature:
    """One capability's speakable shape, derived from its own definition."""

    name: str
    #: Content tokens of the EXPORT name — `video.start_streaming` becomes
    #: {start, streaming}. The namespace is dropped: an operator says "start
    #: streaming", never "video dot start streaming".
    name_tokens: Tuple[str, ...]
    #: Content tokens of the docstring summary. Support only, never sufficient
    #: on its own — a description is prose and prose overlaps by accident.
    desc_tokens: frozenset
    #: Explicitly declared spellings. Empty for almost everything.
    phrases: Tuple[str, ...]
    tier: str
    #: Whether a HUMAN is asked before this runs — `requires_consent`, NOT
    #: `iron_gate_required`.
    #:
    #: The whole reason the confidence bar can be lower here is that the
    #: operator SEES a system dialog naming the capability, so a misread is
    #: caught before anything happens. `iron_gate_required` is True for
    #: NOTIFY_APPLY as well, and a NOTIFY_APPLY capability shows nobody
    #: anything — it runs and reports. Keying on it would have quietly relaxed
    #: the bar for exactly the tier that lost the safeguard the relaxation was
    #: justified by.
    gated: bool
    starts_session: bool
    no_args: bool
    required_args: Tuple[str, ...]

    @property
    def speakable(self) -> bool:
        """Whether this can be reached by voice at all. NEVER raises.

        A capability whose name is entirely function words has no signature to
        match, and one that needs arguments cannot be completed by a reflex.
        Both are simply not offered here; neither is an error, and both remain
        fully reachable through the model.
        """
        return bool(self.name_tokens or self.phrases)


def _signature_for(cap: Any) -> Optional[Signature]:
    """Derive one Signature from a CapabilityDef. None if unusable. NEVER raises."""
    try:
        export = str(getattr(cap, "export_name", "")
                     or getattr(cap, "name", "") or "")
        if not export:
            return None
        # Split on BOTH separators so a namespaced export contributes its verb
        # rather than its namespace.
        bare = export.split(".")[-1]
        name_tokens = tuple(content_tokens(bare.replace("_", " ")))
        desc = str(getattr(cap, "description", "") or "")
        tier = str(getattr(cap, "tier", "") or "")
        return Signature(
            name=export,
            name_tokens=name_tokens,
            desc_tokens=frozenset(content_tokens(desc)),
            phrases=tuple(getattr(cap, "phrases", ()) or ()),
            tier=tier,
            # Fall back to `iron_gate_required` only for a federated def that
            # predates `requires_consent`, and to True if it has neither: an
            # unknown consent posture is never the permissive answer.
            gated=bool(getattr(cap, "requires_consent",
                               getattr(cap, "iron_gate_required", True))),
            starts_session=bool(getattr(cap, "starts_session", False)),
            no_args=bool(getattr(cap, "callable_with_no_args", False)),
            required_args=tuple(getattr(cap, "required_parameters", ()) or ()),
        )
    except Exception:  # noqa: BLE001
        return None


class Lexicon:
    """Every speakable capability, derived and briefly cached. NEVER raises.

    Cached because a sentence is scored against the whole surface and the
    surface is stable between utterances; re-derived on a TTL rather than
    pinned at import because the surface GROWS — a federated namespace that
    finishes hydrating mid-session must become speakable without a restart.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sigs: List[Signature] = []
        self._built_at = 0.0
        self._degraded = ""

    def _derive(self) -> List[Signature]:
        out: List[Signature] = []
        seen = set()
        try:
            from backend.system_control.capability_registry import (
                get_capability_registry,
            )
            for cap in get_capability_registry().all():
                s = _signature_for(cap)
                if s is not None and s.speakable and s.name not in seen:
                    seen.add(s.name)
                    out.append(s)
        except Exception as exc:  # noqa: BLE001
            self._degraded = f"registry: {type(exc).__name__}"
            logger.debug("[Reflex] registry lexicon degraded", exc_info=True)
        try:
            from backend.system_control.capability_federation import (
                federation_enabled, get_federation,
            )
            if federation_enabled():
                fed = get_federation()
                for name in fed.names():
                    cap = fed.get(name)
                    s = _signature_for(cap) if cap is not None else None
                    if s is not None and s.speakable and s.name not in seen:
                        seen.add(s.name)
                        out.append(s)
        except Exception as exc:  # noqa: BLE001
            self._degraded = f"federation: {type(exc).__name__}"
            logger.debug("[Reflex] federated lexicon degraded", exc_info=True)
        return out

    def signatures(self) -> List[Signature]:
        """The current surface. NEVER raises."""
        try:
            now = time.monotonic()
            with self._lock:
                fresh = self._sigs and (now - self._built_at) < lexicon_ttl_s()
                if fresh:
                    return list(self._sigs)
            built = self._derive()
            with self._lock:
                self._sigs = built
                self._built_at = now
            return list(built)
        except Exception:  # noqa: BLE001
            return []

    def invalidate(self) -> None:
        """Force a re-derive. NEVER raises."""
        try:
            with self._lock:
                self._built_at = 0.0
        except Exception:  # noqa: BLE001
            pass

    def stats(self) -> Dict[str, Any]:
        sigs = self.signatures()
        return {
            "schema_version": CAPABILITY_REFLEX_SCHEMA_VERSION,
            "enabled": reflex_enabled(),
            "speakable": len(sigs),
            "gated": sum(1 for s in sigs if s.gated),
            "callable_with_no_args": sum(1 for s in sigs if s.no_args),
            "degraded_reason": self._degraded,
        }


# ── Scoring ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Candidate:
    """One capability's fit against one sentence, with its arithmetic shown."""

    name: str
    score: float
    coverage: float
    explained: float
    ordered: bool
    phrase_hit: bool
    signature: Optional[Signature] = None

    def why(self) -> str:
        return (f"{self.name} score={self.score:.2f} "
                f"(coverage={self.coverage:.2f} explained={self.explained:.2f}"
                f"{' ordered' if self.ordered else ''}"
                f"{' phrase' if self.phrase_hit else ''})")


#: Weights. They sum to 1.0 so a score is readable as a fraction.
#:
#: `explained` carries real weight rather than being a tie-break because it is
#: the term that distinguishes a capability that accounts for the WHOLE
#: sentence from one that happens to appear inside it. Given "lock my screen",
#: a hypothetical `screen` capability has perfect name coverage too — it is
#: `explained` that says `lock_screen` accounts for everything said and
#: `screen` accounts for half.
_W_COVERAGE, _W_EXPLAINED, _W_ORDERED = 0.55, 0.35, 0.10


def _ordered_subsequence(needles: Sequence[str], haystack: Sequence[str]) -> bool:
    """Whether *needles* appear in *haystack* in order. NEVER raises."""
    try:
        it = iter(haystack)
        return all(any(h == n for h in it) for n in needles)
    except Exception:  # noqa: BLE001
        return False


def score_candidate(sig: Signature, said: Sequence[str],
                    normalised: str) -> Optional[Candidate]:
    """Score one capability against one sentence. None if it does not qualify.

    NEVER raises. The qualification gate is absolute: EVERY token of the
    capability's name must have been said, or one of its declared phrases must
    appear verbatim. Nothing partial is scored at all — a capability that half
    matches does not compete, it is absent.
    """
    try:
        said_set = set(said)
        phrase_hit = any(p and p in normalised for p in sig.phrases)
        n = sig.name_tokens
        coverage = (sum(1 for t in n if t in said_set) / len(n)) if n else 0.0
        if coverage < 1.0 and not phrase_hit:
            return None
        matched = sum(1 for t in said if t in set(n))
        explained = (matched / len(said)) if said else 0.0
        if phrase_hit:
            # A declared phrase is a direct statement about this sentence, so
            # it satisfies both structural terms on its own.
            coverage = max(coverage, 1.0)
            explained = max(explained, 1.0)
        ordered = bool(n) and _ordered_subsequence(n, said)
        score = (_W_COVERAGE * coverage
                 + _W_EXPLAINED * min(1.0, explained)
                 + _W_ORDERED * (1.0 if ordered else 0.0))
        return Candidate(name=sig.name, score=round(min(1.0, score), 4),
                         coverage=round(coverage, 4),
                         explained=round(min(1.0, explained), 4),
                         ordered=ordered, phrase_hit=phrase_hit, signature=sig)
    except Exception:  # noqa: BLE001
        return None


# ── The resolution ──────────────────────────────────────────────────────────


class Outcome(str, enum.Enum):
    """What the reflex concluded. Every non-RESOLVED value means "ask the model"."""

    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"            # more than one plausible reading
    NEEDS_ARGS = "needs_args"          # matched, but cannot be called complete
    LOW_CONFIDENCE = "low_confidence"  # matched, not well enough
    NOT_IMPERATIVE = "not_imperative"  # a question, a negation, or a plan
    UNRECOGNIZED = "unrecognized"      # nothing named matches
    DISABLED = "disabled"


@dataclass
class Reflex:
    """The reflex's answer, with enough detail to explain itself out loud."""

    outcome: str
    capability: str = ""
    score: float = 0.0
    margin: float = 0.0
    tier: str = ""
    gated: bool = False
    #: Why, in one sentence, phrased for a log and for a person.
    reason: str = ""
    #: The top few, scored — so `/observability` can show near misses.
    candidates: List[Candidate] = field(default_factory=list)
    elapsed_ms: float = 0.0
    schema_version: str = CAPABILITY_REFLEX_SCHEMA_VERSION

    @property
    def resolved(self) -> bool:
        return self.outcome == Outcome.RESOLVED.value

    @property
    def runner_up(self) -> str:
        return self.candidates[1].name if len(self.candidates) > 1 else ""


class CapabilityReflex:
    """Sentence → capability name, deterministically. NEVER raises."""

    def __init__(self, lexicon: Optional[Lexicon] = None) -> None:
        self._lex = lexicon if lexicon is not None else Lexicon()
        self._counts: Dict[str, int] = {}

    def lexicon(self) -> Lexicon:
        return self._lex

    def resolve(self, utterance: str) -> Reflex:
        """Resolve a sentence. Sub-millisecond, zero LLM, zero I/O.

        NEVER raises — a reflex that can throw is a reflex that can take the
        voice path down with it, which is the failure it exists to prevent.
        """
        t0 = time.monotonic()
        try:
            out = self._resolve(utterance or "")
        except Exception as exc:  # noqa: BLE001
            logger.debug("[Reflex] resolve degraded", exc_info=True)
            out = Reflex(outcome=Outcome.UNRECOGNIZED.value,
                         reason=f"reflex degraded: {type(exc).__name__}")
        out.elapsed_ms = round((time.monotonic() - t0) * 1000.0, 3)
        try:
            self._counts[out.outcome] = self._counts.get(out.outcome, 0) + 1
        except Exception:  # noqa: BLE001
            pass
        return out

    def _resolve(self, utterance: str) -> Reflex:
        if not reflex_enabled():
            return Reflex(outcome=Outcome.DISABLED.value,
                          reason="reflex disabled by env")

        raw = tokenize(utterance)
        if not raw:
            return Reflex(outcome=Outcome.UNRECOGNIZED.value,
                          reason="nothing was said")

        # SHAPE FIRST, before any scoring. "don't lock my screen" contains a
        # perfect match for `lock_screen`, and scoring it first would mean the
        # only thing standing between the operator and the opposite of what
        # they asked for is a subtraction at the end.
        shape = self._non_imperative_reason(raw)
        if shape:
            return Reflex(outcome=Outcome.NOT_IMPERATIVE.value, reason=shape)

        said = content_tokens(utterance)
        if not said:
            return Reflex(outcome=Outcome.UNRECOGNIZED.value,
                          reason="no content words after filler")
        normalised = " ".join(raw)

        scored = [c for c in (score_candidate(s, said, normalised)
                              for s in self._lex.signatures()) if c is not None]
        if not scored:
            return Reflex(outcome=Outcome.UNRECOGNIZED.value,
                          reason="no capability's full name was said")
        scored.sort(key=lambda c: (-c.score, c.name))
        top = scored[:5]
        best = scored[0]
        margin = best.score - (scored[1].score if len(scored) > 1 else 0.0)
        sig = best.signature

        def _no(outcome: Outcome, reason: str) -> Reflex:
            return Reflex(outcome=outcome.value, capability=best.name,
                          score=best.score, margin=round(margin, 4),
                          tier=(sig.tier if sig else ""),
                          gated=bool(sig and sig.gated),
                          reason=reason, candidates=top)

        # A sentence that reads two ways is a question, not a rounding error.
        if len(scored) > 1 and margin < ambiguity_margin():
            return _no(Outcome.AMBIGUOUS,
                       f"'{best.name}' and '{scored[1].name}' both fit "
                       f"(margin {margin:.2f} < {ambiguity_margin():.2f})")

        if sig is None:
            return _no(Outcome.UNRECOGNIZED, "candidate lost its signature")

        # "lock my screen and open Chrome" matches `lock_screen` perfectly and
        # is not a request to lock the screen — it is a request to do that AND
        # something else, and a reflex that serves the first half silently
        # drops the second. Checked here rather than with the other shape
        # refusals because it is the only one that needs to know which tokens
        # the winning capability already accounts for.
        if self._conjunction_split(raw, sig):
            return _no(Outcome.NOT_IMPERATIVE,
                       f"'{best.name}' fits, but the sentence joins it to "
                       f"another act — a plan belongs to the model, which can "
                       f"carry out both halves")

        # Never invent an argument. See the module docstring.
        if not sig.no_args:
            return _no(Outcome.NEEDS_ARGS,
                       f"'{best.name}' requires "
                       f"{', '.join(sig.required_args) or 'arguments'} — a "
                       f"value has to be read out of the sentence, which is "
                       f"the model's job")

        # The bar depends on whether a human will see this before it happens.
        # A session start is pushed back up to the top bar regardless: duration
        # is its own risk class, and one consent does not cover forever.
        needs = (gated_threshold() if (sig.gated and not sig.starts_session)
                 else certain_threshold())
        if best.score < needs:
            kind = ("gated" if sig.gated else "ungated")
            if sig.starts_session:
                kind = "session-opening"
            return _no(Outcome.LOW_CONFIDENCE,
                       f"'{best.name}' scored {best.score:.2f}, below the "
                       f"{needs:.2f} a {kind} capability needs")

        return Reflex(
            outcome=Outcome.RESOLVED.value, capability=best.name,
            score=best.score, margin=round(margin, 4), tier=sig.tier,
            gated=sig.gated, candidates=top,
            reason=(f"{best.why()}; "
                    + ("gated — the operator sees a consent prompt naming it"
                       if sig.gated else "ungated — executes directly")))

    @staticmethod
    def _non_imperative_reason(raw: Sequence[str]) -> str:
        """Why this sentence is not a bare command. "" when it is. NEVER raises.

        Reads RAW tokens, not content tokens: every marker here is a function
        word, so filtering filler first would delete the entire signal.
        """
        try:
            toks = list(raw)
            hit = next((t for t in toks if t in _NEGATIONS), "")
            if hit:
                return (f"'{hit}' — a negation or a cancellation, not a "
                        f"command to carry out")
            # Only a LEADING interrogative. "lock the screen, which is faster"
            # is still an instruction; "which screen do I lock" is not.
            for t in toks[:2]:
                if t in _INTERROGATIVES:
                    return f"'{t}' — a question about a capability, not a request to run it"
            return ""
        except Exception:  # noqa: BLE001
            return ""

    def _conjunction_split(self, raw: Sequence[str], sig: Signature) -> bool:
        """Whether the sentence contains an act beyond this capability."""
        try:
            rest = [t for t in raw if t not in set(sig.name_tokens)]
            if not any(t in _CONJUNCTIONS for t in rest):
                return False
            filler = _FILLER | _extra_filler()
            leftover = [t for t in rest
                        if t not in filler and t not in _CONJUNCTIONS]
            return bool(leftover)
        except Exception:  # noqa: BLE001
            return False

    def stats(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "schema_version": CAPABILITY_REFLEX_SCHEMA_VERSION,
            "outcomes": dict(self._counts),
            "certain_score": certain_threshold(),
            "gated_score": gated_threshold(),
            "margin": ambiguity_margin(),
        }
        try:
            out["lexicon"] = self._lex.stats()
        except Exception:  # noqa: BLE001
            pass
        return out


_REFLEX: Optional[CapabilityReflex] = None
_REFLEX_LOCK = threading.Lock()


def get_capability_reflex() -> CapabilityReflex:
    """Process-wide reflex. NEVER raises."""
    global _REFLEX
    with _REFLEX_LOCK:
        if _REFLEX is None:
            _REFLEX = CapabilityReflex()
        return _REFLEX


def reset_capability_reflex() -> None:
    """Testing seam. NEVER raises."""
    global _REFLEX
    with _REFLEX_LOCK:
        _REFLEX = None
