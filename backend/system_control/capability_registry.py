"""One derived answer to "what can this machine do".

Three hand-kept vocabularies describe one Mac today: the HUD declares 9 tools in
`hud/tool_definitions.py`, Venom declares 16 in `tool_executor.py`, and the web
app reaches ~18 named capabilities through 42 routers. `macos_controller` can
lock the screen; the HUD cannot, because nobody wrote `lock_screen` into its
list. The capability was never missing — its NAME was.

A fourth hand-written list would be the same defect in a new costume. So this
DERIVES the vocabulary from the implementation, the way
`repl_dispatch_registry` derives verbs from `dispatch_<verb>_command` methods
rather than keeping a table beside them.

THE INVERSION THAT MAKES RISK SAFE
------------------------------------
The obvious rule is "classify a method as mutating if it looks mutating".
Every version of that is a guess, and it fails in the direction that hurts: a
method named `get_display_config` that quietly changes the display would be
auto-approved forever, and nothing would ever say so.

So the default is REVERSED. A capability requires approval unless it has
explicitly declared itself read-only. Silence means APPROVAL_REQUIRED, not
SAFE_AUTO — the same rule as `unverified != safe` in the coordination probe and
`UNKNOWN != done` in the intent journal, applied where the cost of being wrong
is somebody's screen locking mid-sentence.

Declaring is cheap and local:

    @capability(reads_only=True)
    async def get_battery(self) -> dict: ...

    @capability(mutates=True, tier="approval_required")
    async def lock_screen(self) -> tuple: ...

...and a docstring tag (``Capability: read-only``) works for methods whose
module should not import this one. Every classification carries its PROVENANCE
— declared, tagged, or defaulted — so "we decided this is safe" and "nobody has
looked at this yet" never render the same, and `unclassified()` gives a ratchet
a number to drive to zero.

SCHEMA
--------
Emits the exact shape `hud/tool_definitions.TOOL_SCHEMAS` already uses
(``name`` / ``description`` / ``parameters``), so a consumer swaps its source
without changing how it reads one. Parameters come from `inspect.signature`
with types mapped to JSON-schema names, and descriptions are lifted from the
Google-style ``Args:`` block the controller already writes.

Callback and internal parameters are EXCLUDED: `lock_screen` takes a
`progress_callback`, and offering a language model a parameter it can only fill
with nonsense is how a tool loop wastes a turn.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import enum
import inspect
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, NamedTuple, Optional

logger = logging.getLogger("JARVIS.CapabilityRegistry")

CAPABILITY_REGISTRY_SCHEMA_VERSION: str = "capability_registry.v1"


def registry_enabled() -> bool:
    """Master gate. Default TRUE — pure reflection, no side effects. NEVER raises."""
    return (os.environ.get("JARVIS_CAPABILITY_REGISTRY_ENABLED", "true")
            or "").strip().lower() not in ("0", "false", "no", "off")


class Provenance(str, enum.Enum):
    """HOW a capability's risk was decided — never conflated with the decision."""

    DECLARED = "declared"      # an explicit @capability(...) on the method
    TAGGED = "tagged"          # a `Capability:` line in the docstring
    DEFAULTED = "defaulted"    # nobody has said — treated as needing approval


class Tier(str, enum.Enum):
    """Mirrors `risk_engine.RiskTier` by VALUE rather than importing it.

    A leaf reflection service that imports the governance risk engine drags the
    whole pipeline into any process that wants to ask what a Mac can do — the
    same dependency inversion that kept `lock_manager` out of the CU sensor.
    The strings are the contract; `iron_gate_required` is the only question a
    caller actually asks.
    """

    SAFE_AUTO = "safe_auto"
    NOTIFY_APPLY = "notify_apply"
    APPROVAL_REQUIRED = "approval_required"
    BLOCKED = "blocked"


#: The tier a capability gets when nobody has classified it. APPROVAL_REQUIRED
#: rather than SAFE_AUTO, deliberately: see the module docstring.
_DEFAULT_TIER = Tier.APPROVAL_REQUIRED

#: Parameters a language model must never be offered. Callbacks, sinks and
#: internals it can only fill with a hallucinated value.
_EXCLUDED_PARAMS = frozenset({"self", "cls", "progress_callback", "callback",
                              "on_progress", "loop", "session", "ctx",
                              "context", "_internal"})

#: Parameters a language model must NEVER be offered, because the only values
#: it can supply are invented or stolen.
#:
#: `unlock_screen(password=None)` was reaching the model with `password` as a
#: fillable string. A model asked to unlock a Mac will either hallucinate a
#: password — a guaranteed failed auth attempt — or, on a screen carrying
#: injected text, be induced to echo a REAL one into a tool call that is then
#: written to the conversation log and the intent journal in plaintext.
#:
#: The authority for these values is never the model. `unlock_screen` already
#: defaults to None and reads the macOS Keychain; every capability that takes a
#: secret has the same shape, because a secret a caller must pass in is a
#: secret that has already leaked.
#:
#: Matched by SUBSTRING on a normalised name, so `password`, `db_password`,
#: `apiKey` and `auth_token` are all covered without a list of spellings to
#: keep — the same reason this module derives its vocabulary instead of
#: declaring it. Silence is not the default here: this is a DENY list applied
#: on top of a schema that is otherwise complete.
_SECRET_PARAM_MARKERS = ("password", "passwd", "secret", "token", "apikey",
                         "api_key", "credential", "passphrase", "private_key")


def _is_secret_param(name: str) -> bool:
    """Whether a parameter carries a credential. NEVER raises."""
    try:
        n = (name or "").strip().lower().replace("-", "_")
        flat = n.replace("_", "")
        return any(m.replace("_", "") in flat for m in _SECRET_PARAM_MARKERS)
    except Exception:  # noqa: BLE001
        return True   # unreadable name -> withhold, never offer

_DOC_TAG = re.compile(r"^\s*Capability:\s*(?P<body>.+)$",
                      re.IGNORECASE | re.MULTILINE)
_ARGS_BLOCK = re.compile(
    r"^\s*Args?:\s*$(?P<body>.*?)(?=^\s*(?:Returns?|Raises?|Yields?|Examples?|Note)s?:|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL)
_ARG_LINE = re.compile(r"^\s{2,}(?P<name>\*{0,2}\w+)\s*(?:\([^)]*\))?\s*:\s*(?P<desc>.+)$")

_JSON_TYPES: Dict[Any, str] = {
    str: "string", int: "integer", float: "number", bool: "boolean",
    list: "array", dict: "object",
}


class Session(str, enum.Enum):
    """A capability that OUTLIVES its own call.

    `lock_screen` finishes when it returns. `start_streaming` returns
    immediately and then holds the display capture open forever. The registry
    had no word for the difference, and without one every long-running claim
    looks like a completed action — which is how a video stream survives the
    HUD that asked for it.

    Two forced rules make the lease design safe, and both are stated here
    because they are load-bearing rather than stylistic:

    * **START is never SAFE_AUTO**, even tagged read-only. One screenshot and a
      permanent screen recording differ in KIND, not degree; duration is itself
      the risk. A continuous observer gets asked for, every time.
    * **END is always SAFE_AUTO.** If stopping needed consent, a leaked session
      could not be reaped without a human — and the reaper runs precisely when
      no human is there. A gate on the release path is a deadlock wearing the
      costume of caution.
    """

    NONE = ""
    START = "start"
    END = "end"


class Effect(str, enum.Enum):
    """PURE observes; EFFECTFUL changes the world.

    Deliberately the same two words `intent_journal.NodeKind` uses, and for the
    same reason: a node that only reads may be replayed and auto-approved, one
    that touched the world may be neither. Two vocabularies for one distinction
    would guarantee they eventually disagree about the same method.

    Mirrored by VALUE rather than imported — `system_control` importing
    `hud.intent_journal` would invert the layering to share two strings.
    `test_the_taxonomies_agree` pins them together.
    """

    PURE = "pure"
    EFFECTFUL = "effectful"


def os_capability(effect: "Effect", *, description: str = "") -> Callable:
    """Declare an OS capability's effect at its definition site.

    The name the annotation ratchet uses on `macos_controller`. It is a thin
    spelling of :func:`capability` rather than a second decorator with its own
    rules — one concept, one implementation, so a future change to how risk is
    resolved cannot apply to half the codebase.
    """
    return capability(
        reads_only=(effect is Effect.PURE),
        mutates=(effect is Effect.EFFECTFUL),
        description=description,
    )


def capability(*, mutates: Optional[bool] = None,
               reads_only: Optional[bool] = None,
               tier: Optional[str] = None,
               description: str = "",
               session: str = Session.NONE.value,
               release: str = "",
               alias: str = "",
               phrases: Any = (),
               requires: Any = (),
               provides: Any = ()) -> Callable:
    """Declare a method's risk where the method is written. NEVER raises.

    A decorator rather than a central table for the reason `repl_dispatch_
    registry` gives about verbs: a table beside the code is a second thing to
    update, and the update that gets forgotten is invisible.
    """
    def _wrap(fn: Callable) -> Callable:
        try:
            resolved = tier
            if resolved is None:
                if reads_only is True or mutates is False:
                    resolved = Tier.SAFE_AUTO.value
                elif mutates is True:
                    resolved = _DEFAULT_TIER.value
            setattr(fn, "__capability__", {
                "tier": resolved or _DEFAULT_TIER.value,
                "declared": True,
                "description": description,
                "session": session,
                "release": release,
                "alias": alias,
                "phrases": _normalise_phrases(phrases),
                "requires": _normalise_predicates(requires),
                "provides": _normalise_predicates(provides),
            })
        except Exception:  # noqa: BLE001 — a decorator never breaks an import
            pass
        return fn
    return _wrap


@dataclass
class CapabilityDef:
    """One machine capability, as both a tool schema and a risk decision."""

    name: str
    description: str
    parameters: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    tier: str = _DEFAULT_TIER.value
    provenance: str = Provenance.DEFAULTED.value
    is_async: bool = True
    #: "" | "start" | "end" — see :class:`Session`.
    session: str = Session.NONE.value
    #: For a START, the capability that releases it. The lease reaper calls
    #: THIS, so a start whose release is unnamed cannot be cleaned up — which
    #: `Session.START` refuses to be, at hydrate time rather than at 3am.
    release: str = ""
    #: How an operator might SAY this, when the method name is not how anyone
    #: says it. Optional and usually empty — `lock_screen` needs none, because
    #: its name already IS the phrase. Declared at the method (``say=...``)
    #: rather than in a lookup table elsewhere, for the same reason `alias`
    #: is: a table beside the code is a second thing to update, and the update
    #: that gets forgotten is invisible.
    phrases: tuple = ()
    #: World-state predicates that must hold BEFORE this can run, and the ones
    #: it establishes by running. Declared at the method, like everything else
    #: here, so the chain that unlocks a screen before searching the web is
    #: DERIVED — "who provides `screen_unlocked`?" is a question the registry
    #: answers, not a branch somebody wrote in the voice router.
    #:
    #: `provides` is a claim, never a proof. The world model re-observes after
    #: the call; a capability that says it unlocks the screen and did not is
    #: exactly the case the re-observation exists to catch.
    requires: tuple = ()
    provides: tuple = ()
    #: The name this capability is EXPORTED as, when its method name would
    #: collide with another provider's in the same namespace. Declared at the
    #: method (``as=stop_actuator``) rather than in a table elsewhere, so the
    #: method keeps the name its existing callers already use and the rename is
    #: readable from the thing being renamed.
    alias: str = ""
    schema_version: str = CAPABILITY_REGISTRY_SCHEMA_VERSION

    @property
    def export_name(self) -> str:
        """What a federated vocabulary should call this. NEVER raises."""
        return self.alias or self.name

    @property
    def starts_session(self) -> bool:
        return self.session == Session.START.value

    @property
    def ends_session(self) -> bool:
        return self.session == Session.END.value

    @property
    def iron_gate_required(self) -> bool:
        """Whether this is GOVERNED at all — i.e. not plain SAFE_AUTO.

        Anything above SAFE_AUTO goes through the gate. A defaulted capability
        answers True — the point of the inversion.

        This is NOT the same question as "must a human be asked", and the two
        were conflated for as long as there was only one of them. See
        :attr:`requires_consent`.
        """
        return self.tier != Tier.SAFE_AUTO.value

    @property
    def requires_consent(self) -> bool:
        """Whether a HUMAN must say yes before this runs.

        WHY THIS IS SEPARATE FROM `iron_gate_required`
        ------------------------------------------------
        The registry declares four tiers, mirroring `risk_engine.RiskTier` by
        value. The router asked exactly one question — `iron_gate_required` —
        and that question is False only for SAFE_AUTO. So NOTIFY_APPLY and
        APPROVAL_REQUIRED produced byte-identical behaviour, and a four-word
        vocabulary described two behaviours.

        A word that cannot change what happens is not a word. Worse, it is a
        word an author will reach for believing it means something: declaring
        a capability NOTIFY_APPLY looked like "apply it and tell me" and
        delivered "block until Touch ID", which is what APPROVAL_REQUIRED was
        already for.

        The tiers now mean what the rest of the codebase has always meant by
        them (CLAUDE.md: "Green/Yellow auto-apply, Orange blocks for human"):

            SAFE_AUTO          run it
            NOTIFY_APPLY       run it, and SAY SO
            APPROVAL_REQUIRED  ask first
            BLOCKED            refuse, and do not even ask

        BLOCKED answering True here would be a bug in the safe direction and
        is still wrong: a blocked capability must never reach an operator as a
        prompt, because a prompt is an invitation to approve it. See
        :attr:`blocked`, which the router checks first.
        """
        return self.tier in (Tier.APPROVAL_REQUIRED.value, Tier.BLOCKED.value)

    @property
    def notifies(self) -> bool:
        """Runs without asking, but never silently. NEVER raises."""
        return self.tier == Tier.NOTIFY_APPLY.value

    @property
    def blocked(self) -> bool:
        """Refused outright. Not a question to put to anyone. NEVER raises."""
        return self.tier == Tier.BLOCKED.value

    @property
    def classified(self) -> bool:
        return self.provenance != Provenance.DEFAULTED.value

    @property
    def required_parameters(self) -> List[str]:
        """Parameters with no default — the call cannot be made without them.

        NEVER raises. Note what is NOT here: a withheld credential parameter
        was dropped from `parameters` entirely, so it cannot appear as
        "required" and cause a caller to go looking for a password. The method
        sources it from the Keychain, which is the only correct answer.
        """
        try:
            return sorted(n for n, p in (self.parameters or {}).items()
                          if bool(p.get("required")))
        except Exception:  # noqa: BLE001
            return []

    @property
    def callable_with_no_args(self) -> bool:
        """Whether ``fn()`` is a complete call. NEVER raises.

        The deterministic reflex's licence to fire. A capability that needs an
        argument needs something extracted from a sentence, and extracting a
        value from a sentence is exactly the job that belongs to a model — so
        the reflex declines rather than guessing.
        """
        return not self.required_parameters

    def to_tool_schema(self) -> Dict[str, Any]:
        """The exact shape `TOOL_SCHEMAS` already uses. NEVER raises."""
        return {"name": self.name, "description": self.description,
                "parameters": dict(self.parameters)}


def _json_type(annotation: Any) -> str:
    """Best JSON-schema name for an annotation. NEVER raises."""
    try:
        if annotation is inspect.Parameter.empty:
            return "string"
        if annotation in _JSON_TYPES:
            return _JSON_TYPES[annotation]
        text = str(annotation)
        # `Optional[str]` / `Union[str, None]` / `List[int]` — read the inside.
        for py, js in _JSON_TYPES.items():
            if getattr(py, "__name__", "") and f"{py.__name__}" in text:
                return js
        return "string"
    except Exception:  # noqa: BLE001
        return "string"


def _arg_descriptions(doc: str) -> Dict[str, str]:
    """Parameter descriptions from a Google-style ``Args:`` block. NEVER raises.

    Continuation lines are JOINED onto the parameter they belong to. Google
    style wraps a long description across lines, and matching only the first one
    cut every wrapped description at the wrap — the model was being handed
    "What to find out across the desktop spaces, in plain", which reads as a
    sentence that simply stops. Silent truncation of the text whose entire job
    is to tell a model what to put in a field.
    """
    out: Dict[str, str] = {}
    try:
        m = _ARGS_BLOCK.search(doc or "")
        if not m:
            return out
        current = ""
        for line in (m.group("body") or "").splitlines():
            am = _ARG_LINE.match(line)
            if am:
                current = am.group("name").lstrip("*")
                out[current] = am.group("desc").strip()
                continue
            # An indented line that is not a new parameter continues the last
            # one. A blank line ends the run, so a stray paragraph after the
            # block cannot graft itself onto the final argument.
            text = line.strip()
            if current and text and line[:1].isspace():
                out[current] = f"{out[current]} {text}".strip()
            elif not text:
                current = ""
    except Exception:  # noqa: BLE001
        pass
    return out


def _summary(doc: str, fallback: str) -> str:
    """First meaningful docstring line. NEVER raises."""
    try:
        for line in (doc or "").strip().splitlines():
            line = line.strip()
            if line:
                return line
    except Exception:  # noqa: BLE001
        pass
    return fallback


def _normalise_phrases(raw: Any) -> tuple:
    """A declared ``phrases``/``say=`` value as a clean tuple. NEVER raises.

    Accepts a string (``"lock the mac|lock it down"``) or any iterable of
    strings, because a decorator author and a docstring tag author naturally
    reach for different shapes and neither should have to care.
    """
    try:
        if not raw:
            return ()
        items = raw.split("|") if isinstance(raw, str) else list(raw)
        out = []
        for it in items:
            s = " ".join(str(it).strip().lower().split())
            if s and s not in out:
                out.append(s)
        return tuple(out)
    except Exception:  # noqa: BLE001
        return ()


def _normalise_predicates(raw: Any) -> tuple:
    """A declared ``requires``/``provides`` value as a clean tuple. NEVER raises.

    Deliberately does NOT import `world_state` to canonicalise the names. The
    registry describes what a method claims; resolving `screen_unlocked` to
    "not screen_locked" is the world model's job, and a leaf registry that
    imports the world model to store two strings is the layering inversion
    this module keeps refusing elsewhere.
    """
    try:
        if not raw:
            return ()
        items = raw.split(",") if isinstance(raw, str) else list(raw)
        out = []
        for it in items:
            s = " ".join(str(it).strip().lower().split())
            if s and s not in out:
                out.append(s)
        return tuple(out)
    except Exception:  # noqa: BLE001
        return ()


class _Classification(NamedTuple):
    tier: str
    provenance: str
    session: str = Session.NONE.value
    release: str = ""
    alias: str = ""
    phrases: tuple = ()
    requires: tuple = ()
    provides: tuple = ()


def _apply_session_rules(c: _Classification) -> _Classification:
    """The two rules from :class:`Session`, enforced in ONE place. NEVER raises.

    Written here rather than at each tag site so a future capability cannot opt
    out of them by being declared somewhere new.
    """
    if c.session == Session.START.value:
        # Duration is the risk. A read-only tag describes the ACTION; it says
        # nothing about holding that action open indefinitely.
        if c.tier == Tier.SAFE_AUTO.value:
            return c._replace(tier=_DEFAULT_TIER.value)
    elif c.session == Session.END.value:
        # The reaper runs when nobody is watching. It must never need consent.
        return c._replace(tier=Tier.SAFE_AUTO.value)
    return c


def _parse_tag(body: str) -> _Classification:
    """Read one ``Capability:`` tag body. NEVER raises.

    Grammar is comma-separated attributes so one line carries every fact:

        Capability: read-only
        Capability: session-start, release=stop_streaming
        Capability: session-end, as=stop_actuator

    Attributes are parsed HERE and only here. The federation used to re-read the
    same docstring with its own regex to find ``as=``, which is two parsers for
    one grammar — the arrangement that guarantees they eventually disagree about
    the same line.
    """
    parts = [p.strip() for p in (body or "").lower().split(",") if p.strip()]
    joined = " ".join(parts)
    session, release, alias = Session.NONE.value, "", ""
    phrases: tuple = ()
    requires: tuple = ()
    provides: tuple = ()
    for p in parts:
        if p.startswith("release="):
            release = p.split("=", 1)[1].strip()
        elif p.startswith("as=") or p.startswith("alias="):
            alias = p.split("=", 1)[1].strip()
        elif p.startswith("say=") or p.startswith("phrases="):
            phrases = _normalise_phrases(p.split("=", 1)[1].strip())
        elif p.startswith("needs=") or p.startswith("requires="):
            requires = _normalise_predicates(p.split("=", 1)[1].strip())
        elif p.startswith("gives=") or p.startswith("provides="):
            provides = _normalise_predicates(p.split("=", 1)[1].strip())
        elif p in ("session-start", "session_start"):
            session = Session.START.value
        elif p in ("session-end", "session_end"):
            session = Session.END.value

    tier = _DEFAULT_TIER.value
    if any(k in joined for k in ("read-only", "read only", "readonly")):
        tier = Tier.SAFE_AUTO.value
    else:
        for t in Tier:
            if t.value in joined.replace("-", "_"):
                tier = t.value
                break
    return _apply_session_rules(
        _Classification(tier, Provenance.TAGGED.value, session, release, alias,
                        phrases, requires, provides))


def _classify(fn: Any, doc: str) -> _Classification:
    """Tier, provenance and session shape for one method. NEVER raises.

    Order matters: an explicit decorator beats a docstring tag, and both beat
    the default — but the DEFAULT IS NOT SAFE. A capability nobody has looked
    at is treated exactly like one somebody flagged as dangerous, because from
    the registry's position those are the same state of knowledge.
    """
    try:
        declared = getattr(fn, "__capability__", None)
        if isinstance(declared, dict) and declared.get("declared"):
            return _apply_session_rules(_Classification(
                str(declared.get("tier") or _DEFAULT_TIER.value),
                Provenance.DECLARED.value,
                str(declared.get("session") or Session.NONE.value),
                str(declared.get("release") or ""),
                str(declared.get("alias") or ""),
                _normalise_phrases(declared.get("phrases")),
                _normalise_predicates(declared.get("requires")),
                _normalise_predicates(declared.get("provides"))))
        tag = _DOC_TAG.search(doc or "")
        if tag:
            return _parse_tag(tag.group("body") or "")
    except Exception:  # noqa: BLE001
        pass
    return _Classification(_DEFAULT_TIER.value, Provenance.DEFAULTED.value)


def describe(name: str, fn: Any) -> Optional[CapabilityDef]:
    """Turn one bound method into a CapabilityDef. None if unusable. NEVER raises."""
    try:
        doc = inspect.getdoc(fn) or ""
        cls = _classify(fn, doc)
        params: Dict[str, Dict[str, Any]] = {}
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            sig = None
        if sig is not None:
            descs = _arg_descriptions(doc)
            for pname, p in sig.parameters.items():
                if pname in _EXCLUDED_PARAMS:
                    continue
                if _is_secret_param(pname):
                    # Withheld from the schema, NOT from the call: the method
                    # keeps its default and sources the value from the Keychain.
                    continue
                if p.kind in (inspect.Parameter.VAR_POSITIONAL,
                              inspect.Parameter.VAR_KEYWORD):
                    continue
                # A callable default or annotation is a hook, not an argument a
                # model can supply.
                if callable(p.default) and p.default is not inspect.Parameter.empty:
                    continue
                if "Callable" in str(p.annotation):
                    continue
                params[pname] = {
                    "type": _json_type(p.annotation),
                    "description": descs.get(pname, f"{pname} parameter"),
                    # Whether the method genuinely cannot be called without
                    # it. Recorded rather than inferred by a consumer, because
                    # the ONE place that already holds the signature is here —
                    # and a consumer that re-introspects to find out is the
                    # second vocabulary this module exists to delete.
                    #
                    # The deterministic reflex arc reads this to decide it may
                    # not fire: a capability it cannot call COMPLETELY is one
                    # it must hand to a model rather than call with a guess.
                    "required": p.default is inspect.Parameter.empty,
                }
        declared = getattr(fn, "__capability__", None) or {}
        return CapabilityDef(
            name=name,
            description=(declared.get("description")
                         or _summary(doc, f"Invoke {name} on macOS.")),
            parameters=params,
            tier=cls.tier,
            provenance=cls.provenance,
            is_async=inspect.iscoroutinefunction(fn),
            session=cls.session,
            release=cls.release,
            alias=cls.alias,
            phrases=cls.phrases,
            requires=cls.requires,
            provides=cls.provides,
        )
    except Exception:  # noqa: BLE001
        logger.debug("[CapabilityRegistry] describe(%s) degraded", name,
                     exc_info=True)
        return None


class CapabilityRegistry:
    """Every public capability of a controller, derived. NEVER raises."""

    def __init__(self, target: Any = None, *,
                 require_declaration: bool = False) -> None:
        self._target = target
        self._defs: Dict[str, CapabilityDef] = {}
        self._hydrated = False
        #: Export ONLY what has been classified.
        #:
        #: `MacOSController` is a capability façade — every public method IS a
        #: capability, so collecting them all is right there. A federated
        #: subsystem is not: measured on the real classes, `public and callable`
        #: also yields `get_instance`, `cleanup` and `register_callback` —
        #: lifecycle plumbing that a language model can only waste a turn on.
        #:
        #: The fix is not a list of exclusions (that is the hand-kept table this
        #: module exists to delete). It is the same inversion applied one level
        #: up: a method joins the surface by SAYING SO. Silence stops meaning
        #: "gated" and starts meaning "not offered" — strictly safer, and it
        #: makes `unclassified()` the honest measure of remaining work rather
        #: than a number inflated by plumbing nobody will ever declare.
        self._require_declaration = bool(require_declaration)

    def hydrate(self) -> "CapabilityRegistry":
        """Introspect the target. Idempotent. NEVER raises."""
        self._defs = {}
        self._hydrated = True
        if not registry_enabled():
            return self
        try:
            target = self._target if self._target is not None else _default_target()
            if target is None:
                return self
            for name, member in inspect.getmembers(target, callable):
                if name.startswith("_"):
                    continue            # private is not a capability
                d = describe(name, member)
                if d is None:
                    continue
                if self._require_declaration and not d.classified:
                    continue
                self._defs[name] = d
        except Exception:  # noqa: BLE001
            logger.debug("[CapabilityRegistry] hydrate degraded", exc_info=True)
        return self

    def _ensure(self) -> None:
        if not self._hydrated:
            self.hydrate()

    def names(self) -> List[str]:
        self._ensure()
        return sorted(self._defs)

    def get(self, name: str) -> Optional[CapabilityDef]:
        self._ensure()
        return self._defs.get(name)

    def all(self) -> List[CapabilityDef]:
        self._ensure()
        return [self._defs[n] for n in sorted(self._defs)]

    def tool_schemas(self) -> Dict[str, Dict[str, Any]]:
        """Drop-in for `TOOL_SCHEMAS`. NEVER raises."""
        self._ensure()
        return {n: self._defs[n].to_tool_schema() for n in sorted(self._defs)}

    def iron_gate_required(self, name: str) -> bool:
        """True unless the capability has DECLARED itself read-only.

        An unknown name answers True as well: a caller asking about a
        capability this registry has never seen is the last place to assume
        the permissive answer.
        """
        d = self.get(name)
        return True if d is None else d.iron_gate_required

    def unclassified(self) -> List[str]:
        """Capabilities nobody has declared — the ratchet's number. NEVER raises."""
        self._ensure()
        return sorted(n for n, d in self._defs.items() if not d.classified)

    def sessions(self) -> List[CapabilityDef]:
        """Capabilities that outlive their own call. NEVER raises."""
        self._ensure()
        return [self._defs[n] for n in sorted(self._defs)
                if self._defs[n].starts_session]

    def unreleasable(self) -> List[str]:
        """Session starts whose release nobody named. NEVER raises.

        A leak that is CURRENTLY invisible: the stream runs, the lease expires,
        and the reaper has nothing to call. Surfacing it as a number means the
        defect is found at hydrate time by a test, not at 3am by a fan.
        """
        self._ensure()
        return sorted(n for n, d in self._defs.items()
                      if d.starts_session and not d.release)

    def stats(self) -> Dict[str, Any]:
        self._ensure()
        gated = [d for d in self._defs.values() if d.iron_gate_required]
        return {
            "schema_version": CAPABILITY_REGISTRY_SCHEMA_VERSION,
            "enabled": registry_enabled(),
            "capabilities": len(self._defs),
            "iron_gate_required": len(gated),
            "safe_auto": len(self._defs) - len(gated),
            "unclassified": len(self.unclassified()),
            "sessions": len(self.sessions()),
            "unreleasable": len(self.unreleasable()),
            # An empty registry MUST explain itself. Zero capabilities and
            # "the controller would not import" are different facts, and a
            # consumer that cannot tell them apart shows an operator a Mac
            # that apparently does nothing.
            "degraded_reason": (_degraded[0] if not self._defs else ""),
            "by_provenance": {
                p.value: sum(1 for d in self._defs.values()
                             if d.provenance == p.value) for p in Provenance
            },
        }


def _default_target() -> Any:
    """The macOS controller CLASS, not an instance. NEVER raises.

    Describing a capability must not require constructing the thing that has
    it. The first version instantiated `MacOSController`, whose constructor
    starts async pipeline work — it raised on this machine and the registry
    reported ZERO capabilities with no explanation, which is precisely the
    silent-emptiness failure this codebase keeps finding.

    Class-level reflection finds the same 42 methods, costs nothing, and has no
    side effects. `inspect.signature` reports `self` for an unbound function,
    which `_EXCLUDED_PARAMS` already drops.
    """
    try:
        from backend.system_control.macos_controller import MacOSController
        return MacOSController
    except Exception as exc:  # noqa: BLE001
        _degraded[0] = f"controller unimportable: {type(exc).__name__}: {exc}"
        logger.debug("[CapabilityRegistry] no controller available",
                     exc_info=True)
        return None


#: Why the registry is empty, when it is. An empty vocabulary that
#: cannot say why is indistinguishable from a Mac that can do nothing.
_degraded: list = [""]


_REGISTRY: Optional[CapabilityRegistry] = None


def get_capability_registry() -> CapabilityRegistry:
    """Process-wide registry over the live controller. NEVER raises."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = CapabilityRegistry().hydrate()
    return _REGISTRY


def reset_capability_registry() -> None:
    """Testing seam. NEVER raises."""
    global _REGISTRY
    _REGISTRY = None
