"""VoiceCommandRouter -- classifies voice commands and routes to the right executor.

Uses Doubleword 35B for fast intent classification, then dispatches to:
  - AppleScriptExecutor (app/navigation -- deterministic, no LLM)
  - VLAExecutor (vision actions -- JarvisCU pipeline)
  - ToolUseOrchestrator (composite -- 397B tool loop)
  - QueryExecutor (questions -- 35B response)

The Swift HUD sends raw commands here. This is the brain's front door.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, List, Optional

from backend.hud.applescript_executor import AppleScriptExecutor
from backend.hud.query_executor import QueryExecutor
from backend.hud.tool_use_orchestrator import CommandResult, ToolUseOrchestrator

logger = logging.getLogger(__name__)

_CLASSIFIER_MODEL = os.environ.get("JARVIS_VOICE_ROUTER_MODEL", "Qwen/Qwen3.5-35B-A3B-FP8")

_CLASSIFY_PROMPT = """Given this voice command, classify the intent. Return ONLY a JSON object.

Command: "{command}"

Categories:
- "app_action": open/close/switch/launch an application (e.g., "open chrome", "close Safari", "launch Spotify")
- "navigation": go to a website/URL (e.g., "go to LinkedIn", "open google.com", "search YouTube for music")
- "vision_action": interact with something visible on screen that requires seeing it (e.g., "click the send button", "scroll down", "select the text")
- "composite": multi-step task combining multiple actions (e.g., "open chrome and go to LinkedIn", "send a message on WhatsApp saying hello")
- "code_action": modify code, fix bugs, system development tasks (e.g., "fix the parser bug", "refactor the login module")
- "query": answer a question, provide information, no action needed (e.g., "what time is it", "what's on my screen", "how does the vision loop work")

Return: {{"category": "...", "needs_vision": true/false, "needs_tools": true/false}}"""


#: What JARVIS has said recently, as token sets, newest last. Bounded.
_SPOKEN: List[tuple] = []
_SPOKEN_LOCK = threading.Lock()


def echo_grace_s() -> float:
    """Extra time past the end of speech in which an echo can still arrive.

    NEVER raises. ``0`` disables echo suppression entirely.

    WHY 4 SECONDS AND NOT 1.5
    ---------------------------
    This was 1.5s, chosen as "recogniser latency", and it was too small by a
    factor that let a real echo through. Measured 2026-08-03 08:47:47 —
    JARVIS said "🔒 Locking the screen now, Derek. See you soon." (estimated
    5.33s of speech), and at 08:47:56 the microphone delivered "See you soon"
    as a COMMAND. The window had closed 2.17s earlier.

    The gap is not latency, it is the recogniser's own design.
    `WakeWordListener.commandSilenceTimeout` is 2.5s: it waits for two and a
    half seconds of silence before deciding an utterance has ended, and only
    then finalises and dispatches. So a transcript necessarily arrives at
    least 2.5s AFTER the audio stopped, plus recognition time.

    4s covers that with room, and the cost of being generous is bounded and
    small: the only thing suppressed is a phrase JARVIS itself said, in that
    order, within seconds. The cost of being stingy is JARVIS obeying its own
    voice — which is how "See you soon" became an unlock attempt.
    """
    try:
        raw = (os.environ.get("JARVIS_ECHO_GRACE_S", "") or "").strip()
        return max(0.0, min(30.0, float(raw))) if raw else 4.0
    except (TypeError, ValueError):
        return 4.0


def _echo_expiry(text: str) -> float:
    """When this utterance stops being a plausible echo. NEVER raises.

    Bounded by HOW LONG JARVIS ACTUALLY SPEAKS, not a flat window, and reusing
    `speech_bridge.estimate_speech_ms` rather than a second estimate of the
    same thing.

    A flat window was wrong in a way that mattered. The pre-narration is "On
    it — lock screen", which is precisely what an operator repeating
    themselves also says, so a fixed 8s window deafened JARVIS to a genuine
    second attempt for eight seconds after acknowledging the first. An echo
    can only arrive while the audio is playing (plus recogniser latency); a
    person repeating themselves waits for JARVIS to finish. Tying the window
    to the utterance separates them.
    """
    try:
        from backend.hud.speech_bridge import estimate_speech_ms
        return time.time() + (estimate_speech_ms(text) / 1000.0) + echo_grace_s()
    except Exception:  # noqa: BLE001
        return time.time() + 3.0 + echo_grace_s()


def reset_spoken() -> None:
    """Forget what JARVIS has said. Testing seam. NEVER raises."""
    try:
        with _SPOKEN_LOCK:
            _SPOKEN.clear()
    except Exception:  # noqa: BLE001
        pass


def note_spoken(text: str) -> None:
    """Record that JARVIS said this. NEVER raises.

    Called from the ONE place all HUD speech passes through, so nothing it
    utters can be missed. Stores tokens rather than the sentence: speech
    recognition returns "Lock screen" for a synthesiser that said "On it —
    lock screen", and comparing strings would match neither.
    """
    try:
        from backend.system_control.capability_reflex import content_tokens
        toks = tuple(content_tokens(text or ""))
        if not toks:
            return
        with _SPOKEN_LOCK:
            # ORDER IS KEPT, not a set. See `is_own_echo` — order is the only
            # thing separating an echo from a command that happens to reuse
            # JARVIS's own words.
            _SPOKEN.append((toks, _echo_expiry(text)))
            del _SPOKEN[:-12]
    except Exception:  # noqa: BLE001
        pass


def is_own_echo(command: str) -> str:
    """Whether JARVIS is hearing itself. "" when it is not. NEVER raises.

    THE BUG THIS ANSWERS
    ----------------------
    Measured 2026-08-03: one spoken "lock my screen" locked the screen TWICE.

        [SpeechGate] claim backend for 4.3s backend:system
        [SpeechGate] EXPIRED backend — its owner never released it
        [SpeechGate] mic LIVE
        [JARVIS Voice] [partial] "Lock screen"        ← its own voice

    `speech_bridge` derives a mute deadline from character count
    (`1500ms + chars/12 * 1000`). That is an ESTIMATE of how long speech will
    take, and when the synthesiser runs slower than the estimate the claim
    expires mid-sentence, the microphone goes live, and JARVIS transcribes
    itself. The deadline is deliberate — a claim on a microphone must never be
    able to outlive its own plausible duration — so the answer is not a longer
    guess; it is recognising the echo when it arrives.

    ORDERED subsequence, not a subset — and the difference is not pedantry.
    JARVIS's own contextual narration is "Your screen is locked, let me unlock
    it first", which CONTAINS both `unlock` and `screen`. As a set test, that
    sentence suppresses a genuine "unlock my screen" from the operator for the
    whole window: the assistant explains what it is about to do and is thereby
    deafened to the very command it just described. Caught by
    `test_the_opposite_command_is_not_an_echo`.

    In that sentence the words arrive as `screen … unlock`; a person asking
    says `unlock … screen`. A recogniser mishearing a synthesiser drops words,
    it does not reorder them — so order is exactly the signal that separates
    an echo from a command reusing JARVIS's vocabulary.

    Deliberately NOT a general repeat-suppressor. "volume up, volume up" is a
    real thing a person does, and it only matches here if JARVIS itself said
    "volume up", in that order, within the window.
    """
    try:
        if echo_grace_s() <= 0.0:
            return ""
        from backend.system_control.capability_reflex import content_tokens
        said = tuple(content_tokens(command or ""))
        if not said:
            return ""
        now = time.time()
        with _SPOKEN_LOCK:
            live = [(t, exp) for t, exp in _SPOKEN if exp > now]
            _SPOKEN[:] = live
        for toks, exp in reversed(live):
            if _ordered_within(said, toks):
                return (f"JARVIS is still saying it "
                        f"({exp - now:.1f}s left)")
        return ""
    except Exception:  # noqa: BLE001
        return ""


def _ordered_within(needle: tuple, haystack: tuple) -> bool:
    """Whether *needle* appears in *haystack* in order. NEVER raises."""
    try:
        it = iter(haystack)
        return all(any(h == n for h in it) for n in needle)
    except Exception:  # noqa: BLE001
        return False


#: Capabilities currently executing, name → started-at. Bounded by the number
#: of things that can be in flight, which is small.
_IN_FLIGHT: dict = {}
_IN_FLIGHT_LOCK = threading.Lock()


def in_flight_window_s() -> float:
    """How long a capability counts as still running. NEVER raises.

    A ceiling, not a measurement — the entry is removed when the call
    finishes. This only stops a crashed or wedged call from blocking its own
    capability forever.
    """
    try:
        raw = (os.environ.get("JARVIS_IN_FLIGHT_WINDOW_S", "") or "").strip()
        return max(1.0, min(300.0, float(raw))) if raw else 45.0
    except (TypeError, ValueError):
        return 45.0


def _claim_in_flight(capability: str) -> bool:
    """Take the slot for *capability*. False if it is already running.

    NEVER raises.

    THE REPEAT ECHO SUPPRESSION CANNOT SEE
    ----------------------------------------
    Measured 2026-08-03: boot held the event loop, so "lock my screen" took
    31 seconds to produce a sound. Derek did what anyone does when nothing
    happens — he said it again. Both ran; the screen locked twice.

    `is_own_echo` is blind to this by construction: the repeat arrived at
    01:40:49 and JARVIS did not speak until 01:41:05, so there was nothing to
    be an echo OF. They are different problems with different fixes — one is
    the assistant hearing itself, this is the operator being ignored long
    enough to ask twice.

    Keyed on the CAPABILITY rather than the words, so "lock my screen" and
    "Lock screen" coalesce — they resolve to the same act, which is the thing
    that must not happen twice. Two people asking for two different things
    still get both.
    """
    try:
        now = time.time()
        with _IN_FLIGHT_LOCK:
            started = _IN_FLIGHT.get(capability)
            if started is not None and (now - started) < in_flight_window_s():
                return False
            _IN_FLIGHT[capability] = now
            # Sweep anything that outlived the ceiling; a wedged call must not
            # disable its capability for the rest of the session.
            for k, t in list(_IN_FLIGHT.items()):
                if (now - t) > in_flight_window_s():
                    _IN_FLIGHT.pop(k, None)
        return True
    except Exception:  # noqa: BLE001 — never block a command on bookkeeping
        return True


def _release_in_flight(capability: str) -> None:
    """Give the slot back. NEVER raises."""
    try:
        with _IN_FLIGHT_LOCK:
            _IN_FLIGHT.pop(capability, None)
    except Exception:  # noqa: BLE001
        pass


def _reads_like_a_sentence(text: str) -> bool:
    """Whether *text* was written for a person to hear. NEVER raises.

    The test is shape, not vocabulary: a sentence starts with a capital, ends
    in punctuation, and has several words. `capability_router` details come in
    both kinds — "I don't have a voiceprint for you on this Mac yet." is meant
    for the operator's ears, while "consent channel unreachable —
    HUDConsentProvider needs screen_unlocked" is meant for the log.

    Deciding by shape rather than by matching known phrases is what keeps this
    correct when a new authority is added: an authority that writes properly
    for a person is understood without this function learning about it.
    """
    try:
        t = (text or "").strip()
        if len(t) < 12 or len(t.split()) < 4:
            return False
        if not t[0].isupper():
            return False
        return t.endswith((".", "!", "?"))
    except Exception:  # noqa: BLE001
        return False


def _humanise(capability: str) -> str:
    """A capability name as a person would say it. NEVER raises.

    `video.start_streaming` → "start streaming". Derived from the name rather
    than looked up, for the same reason the reflex derives its lexicon: a
    table of spoken forms is a table that goes stale the first time somebody
    adds a capability without reading this file.
    """
    try:
        bare = str(capability or "").split(".")[-1]
        return " ".join(bare.replace("_", " ").split()) or "that"
    except Exception:  # noqa: BLE001
        return "that"


def _read_controller_result(result: Any, human: str) -> tuple:
    """(succeeded, what to say) from whatever a capability returned.

    NEVER raises. `MacOSController` methods answer `(bool, str)` and the
    string is already a sentence written for a person — "🔒 Locking the screen
    now, Derek. See you soon." Synthesising our own narration on top of that
    would be a second voice for the same event, and the two would eventually
    disagree about what happened.
    """
    try:
        if isinstance(result, tuple) and len(result) == 2:
            ok, msg = result
            text = str(msg or "").strip()
            if isinstance(ok, bool):
                return ok, (text or (f"Done — {human}." if ok
                                     else f"I couldn't {human}."))
        if isinstance(result, str) and result.strip():
            return True, result.strip()[:400]
        if result is False:
            return False, f"I couldn't {human}."
        return True, f"Done — {human}."
    except Exception:  # noqa: BLE001
        return True, f"Done — {human}."


def _why_the_model_could_not_answer(detail: str) -> str:
    """Turn a provider fault into a sentence. NEVER raises.

    The operator asked for their screen to be locked and JARVIS said, out
    loud, through a speech synthesiser:

        "Model error: session_budget_preflight_refused:soak_circuit_tripped:
         soak_cost_cap_exceeded:$238111.9488>=$2.0000:on_boot: provider=
         doubleword est=$0.1000 > session_remaining=$0.0000"

    Absolute observability means every autonomous decision is VISIBLE, and a
    diagnostic read aloud to a person is not visibility — it is the log
    escaping onto the one surface that cannot skim. The full string stays in
    `error` for the log; this is what gets spoken.

    Classified by SHAPE rather than by matching a provider's exact wording, so
    a reworded refusal from a new provider still lands in the right sentence.
    """
    try:
        d = (detail or "").lower()
        if "budget" in d or "cost_cap" in d or "circuit" in d or "spend" in d:
            return ("I've hit my own spending limit, so I can't think that "
                    "one through right now.")
        if ("403" in d or "entitlement" in d or "unauthor" in d
                or "api key" in d or "credential" in d):
            return "I'm not authorised to reach my language model right now."
        if "timeout" in d or "timed out" in d:
            return "My language model took too long to answer."
        if ("connect" in d or "unreachable" in d or "network" in d
                or "outage" in d or "502" in d or "503" in d):
            return "I can't reach my language model right now."
        return "My language model isn't answering right now."
    except Exception:  # noqa: BLE001
        return "My language model isn't answering right now."


def _near_miss_offer(reflex: Any) -> str:
    """What the reflex ALMOST understood, phrased as an offer. "" if nothing.

    NEVER raises. When the cortex is unreachable, "I don't know" and "I think
    you meant lock screen but I wasn't sure enough to act" are very different
    answers, and the second one lets the operator fix it in one word. The
    reflex still does not act on it — this only says what it saw.
    """
    try:
        if reflex is None or getattr(reflex, "resolved", False):
            return ""
        cap = str(getattr(reflex, "capability", "") or "")
        outcome = str(getattr(reflex, "outcome", "") or "")
        if not cap:
            return ""
        if outcome in ("low_confidence", "ambiguous"):
            return f" Did you mean {_humanise(cap)}?"
        if outcome == "needs_args":
            return (f" I can {_humanise(cap)}, but I need you to tell me "
                    f"what to run it on.")
        return ""
    except Exception:  # noqa: BLE001
        return ""


def _make_failure_speakable(result: Any, reflex: Any) -> Any:
    """Rewrite a failed result's spoken half. NEVER raises.

    Applied HERE rather than inside `ToolUseOrchestrator` on purpose: the
    orchestrator has other callers and its `error` string is a diagnostic that
    logs and tests depend on. Only this path ends at a speech synthesiser, so
    only this path translates. `error` is left exactly as it was.
    """
    try:
        if result is None or getattr(result, "success", False):
            return result
        if getattr(result, "pending", False):
            return result
        # NEVER touch a result the reflex produced. Measured: the capability
        # path answered "I couldn't ask anyone to approve lock screen, so I
        # didn't do it" — true, specific, and actionable — and this function
        # saw the word "provider" in `no approval provider available` and
        # replaced it with "My language model isn't answering right now",
        # which is a plain falsehood; no model was involved in that failure at
        # all. A translator that can overwrite a true sentence with a false
        # one is worse than the diagnostic it was written to replace.
        #
        # The gate is the PATH, not a better keyword list: the capability path
        # already writes sentences for people, so it needs no translation, and
        # anything this function did to one could only be damage.
        if reflex is not None and getattr(reflex, "resolved", False):
            return result
        detail = str(getattr(result, "error", "")
                     or getattr(result, "response_text", "") or "")
        # Only rewrite when the failure was the MODEL being unavailable. A
        # genuine task failure ("couldn't find Chrome") is already a sentence
        # a person can act on, and replacing it would lose the only useful
        # thing in it.
        markers = ("model error", "preflight", "provider", "circuit", "budget",
                   "timeout", "connect", "unauthor", "403", "502", "503",
                   "entitlement", "outage")
        if not any(m in detail.lower() for m in markers):
            return result
        result.response_text = (_why_the_model_could_not_answer(detail)
                                + _near_miss_offer(reflex))
        logger.warning("[VoiceRouter] model path unavailable — spoken: %s "
                       "| detail: %s", result.response_text, detail[:200])
        return result
    except Exception:  # noqa: BLE001
        return result


class VoiceCommandRouter:
    """Routes voice commands through Ouroboros for intelligent execution."""

    def __init__(self, doubleword: Any, narrate_fn: Optional[Any] = None) -> None:
        self._dw = doubleword
        self._narrate_fn = narrate_fn
        self._applescript = AppleScriptExecutor()
        self._query = QueryExecutor(doubleword)
        self._tool_orchestrator = ToolUseOrchestrator(doubleword, narrate_fn=narrate_fn)

    async def route(self, command: str, screenshot_b64: Optional[str] = None,
                    intent_id: Optional[str] = None) -> CommandResult:
        """Classify and route a voice command.

        WRITE-AHEAD. The raw command is journalled before anything executes, so
        a process death mid-flight leaves the operator's sentence recoverable
        rather than gone. Nothing else in the HUD path checkpoints — the
        Ouroboros FSM's `.ouroboros/checkpoints` resume machinery covers the
        governed loop, which `route()` never enters.

        Classification is journalled as a PURE node: deterministic given the
        same command, no outside effect, so a resumed intent reuses the recorded
        category instead of paying the model again. Execution deliberately is
        NOT — see `intent_journal`: the CU steps are type/click/drag/scroll,
        and replaying one does not recompute a value, it sends a second
        message.
        """
        logger.info("[VoiceRouter] Command: %s", command[:100])
        # HEARING YOURSELF IS NOT BEING TOLD SOMETHING.
        # Checked before the journal opens: an echo is not an intent, and
        # recording one would put JARVIS's own voice in the replay queue.
        _echo = is_own_echo(command)
        if _echo:
            logger.warning("[VoiceRouter] IGNORED '%s' — %s", command[:60], _echo)
            return CommandResult(
                success=True, category="ignored_echo", steps_completed=0,
                steps_total=0, response_text=None, error=None)
        _journal = None
        reflex = None
        try:
            from backend.hud.intent_journal import (
                NodeKind, get_intent_journal,
            )
            _journal = get_intent_journal()
            if intent_id is None:
                intent_id = await _journal.open_intent(
                    command, payload={"has_screenshot": bool(screenshot_b64)})
        except Exception:  # noqa: BLE001 — a journal never blocks a command
            _journal = None

        # Step 0: THE REFLEX. Deterministic, ~0.1ms, zero LLM, zero I/O.
        #
        # This runs BEFORE classification rather than as its fallback, and the
        # order is the whole point. As a fallback it would only ever run after
        # a model call had already been attempted — so a sentence naming a
        # capability outright would still cost two round-trips when the model
        # is up, and would still be at the mercy of the model being up at all.
        #
        # It fires only when it is certain, and every uncertain outcome falls
        # through to exactly the path that ran before, so nothing it declines
        # is lost. See `capability_reflex` for the four separate refusals.
        reflex = self._reflex(command)
        if reflex is not None and reflex.resolved:
            logger.info("[VoiceRouter] REFLEX resolved '%s' -> %s in %.2fms "
                        "(no model call) — %s", command[:60],
                        reflex.capability, reflex.elapsed_ms, reflex.reason)
            # SAY WHAT YOU ARE ABOUT TO DO, not what you heard.
            #
            # The HUD narrates "On it. Executing: lock my screen" BEFORE
            # routing, so it can only ever echo the raw transcript back — it
            # is speaking before anything has been understood. Now that the
            # reflex has resolved a NAME deterministically and for free, the
            # organism can say what it is actually about to do.
            #
            # Said here rather than after the call because a NOTIFY_APPLY
            # capability may remove the surface the sentence would have
            # arrived on: `lock_screen` returns, and the screen is gone.
            await self._say(f"On it — {_humanise(reflex.capability)}.")

        # Step 1: Classify intent via 35B — PURE, so a resume can reuse it.
        classification = None
        if reflex is not None and reflex.resolved:
            # Skip the classifier entirely. Asking a model to categorise a
            # sentence we have already resolved to a named capability is a
            # round-trip whose answer we would discard.
            classification = {"category": "system_action",
                              "needs_vision": False, "needs_tools": False}
        if _journal is not None and intent_id:
            try:
                _v = _journal.resume_plan(intent_id).for_node("classify")
                if _v.result is not None and _v.action.value == "skip":
                    classification = _v.result
                    logger.info("[VoiceRouter] reusing journalled "
                                "classification (no model call)")
            except Exception:  # noqa: BLE001
                classification = None
        if classification is None:
            if _journal is not None and intent_id:
                await _journal.node_started(intent_id, "classify", NodeKind.PURE)
            classification = await self._classify(command)
            if _journal is not None and intent_id:
                await _journal.node_completed(
                    intent_id, "classify", classification, NodeKind.PURE)
        category = classification.get("category", "composite")
        needs_vision = classification.get("needs_vision", False)
        needs_tools = classification.get("needs_tools", False)

        logger.info("[VoiceRouter] Classified: %s (vision=%s, tools=%s)", category, needs_vision, needs_tools)

        # Step 2: Route to executor.
        #
        # The dispatch is EFFECTFUL — every branch below can touch the world —
        # so the journal records that it began and how it ended, and never
        # offers to replay it. An interrupted dispatch resolves to CONFIRM, not
        # to a silent second attempt.
        if _journal is not None and intent_id:
            try:
                await _journal.node_started(intent_id, "dispatch",
                                            NodeKind.EFFECTFUL)
            except Exception:  # noqa: BLE001
                pass
        result: Optional[CommandResult] = None
        try:
            # CONTEXTUAL AWARENESS. Clear the way before acting, the way a
            # person would — unlock the screen before searching the web,
            # without being told to. Returns non-None only when the way could
            # not be cleared and the original command must not proceed.
            blocked_by = await self._clear_the_way(command, category, reflex)
            if blocked_by is not None:
                result = blocked_by
                return result

            if reflex is not None and reflex.resolved:
                result = await self._execute_capability(reflex)
            elif category == "app_action":
                result = await self._execute_app_action(command)
            elif category == "navigation":
                result = await self._execute_navigation(command)
            elif category == "query":
                result = await self._execute_query(command, screenshot_b64)
            elif category == "vision_action":
                result = await self._execute_vision(command, screenshot_b64)
            elif category == "code_action":
                result = await self._execute_code_action(command)
            else:
                # composite or unknown -> tool-use loop (397B)
                result = await self._tool_orchestrator.execute(
                    command, screenshot_b64)
            return _make_failure_speakable(result, reflex)
        except Exception as exc:  # noqa: BLE001 — record, then propagate
            if _journal is not None and intent_id:
                try:
                    # Record the failure and leave the intent OPEN.
                    #
                    # Closing here would be the natural-looking mistake: an
                    # intent is closed when we have stopped caring about it,
                    # and `unfinished()` is precisely the replay queue. A
                    # first-attempt timeout that closed its own intent would
                    # be unrecoverable — measured, when the end-to-end proof
                    # reported `unfinished intents: 0` after a crash.
                    # Retention bounds how long it stays open.
                    await _journal.node_failed(intent_id, "dispatch",
                                               repr(exc), NodeKind.EFFECTFUL)
                except Exception:  # noqa: BLE001
                    pass
            raise
        finally:
            if _journal is not None and intent_id and result is not None:
                try:
                    await _journal.node_completed(intent_id, "dispatch", None,
                                                  NodeKind.EFFECTFUL)
                    await _journal.close_intent(
                        intent_id, success=bool(getattr(result, "success", True)))
                except Exception:  # noqa: BLE001
                    pass

    # ── Contextual awareness: the flow a person would follow ────────────

    async def _clear_the_way(self, command: str, category: str,
                             reflex: Any) -> Optional[CommandResult]:
        """Satisfy what this command needs before running it. NEVER raises.

        Returns a CommandResult ONLY when the way could not be cleared and the
        caller must stop; ``None`` means proceed.

        THE FLOW A PERSON WOULD FOLLOW
        --------------------------------
        Told "search for dogs" at a locked Mac, nobody types the query into the
        lock screen and reports failure. They unlock it first, because the
        precondition is obvious to them and they never mention it. That is what
        this does, and the reason it is not a special case in a branch:

          * WHAT a command needs is a world-state predicate.
          * WHO can supply it is a registry question — "which capability
            declares `provides=screen_unlocked`?" — so the remedy is DERIVED.
            Nothing here names `unlock_screen`, and a capability that gains
            that declaration tomorrow becomes the remedy tomorrow.
          * WHETHER it worked is re-observed, never assumed.

        IT ACTS ONLY ON POSITIVE KNOWLEDGE
        ------------------------------------
        The chain engages when the world model KNOWS a requirement is false —
        `refutes`, not `not satisfies`. UNKNOWN proceeds exactly as before.

        That asymmetry is load-bearing rather than cautious. `CGSession`
        answers only inside a GUI session, so the probe is legitimately blind
        in a shell, in CI, and anywhere the backend is not a child of the
        window server. Treating "I cannot see" as "it is locked" would make
        JARVIS try to unlock a Mac that was never locked, every time it could
        not look — a fabricated remedy for an imagined problem, which is the
        blast-radius fabrication class in a new costume.
        """
        try:
            from backend.system_control.world_state import get_world_state
            world = get_world_state()
            needs = self._requirements(category, reflex)
            if not needs:
                return None

            blocked = [p for p in needs if await world.refutes(p)]
            if not blocked:
                return None

            for predicate in blocked:
                remedy = self._who_provides(predicate)
                if remedy is None:
                    logger.info("[VoiceRouter] '%s' is unmet and nothing "
                                "declares it — proceeding anyway", predicate)
                    continue

                await self._say(self._explain_detour(predicate, remedy, command))
                logger.info("[VoiceRouter] CAI: '%s' unmet → running '%s' "
                            "first", predicate, remedy)

                step = await self._execute_capability(
                    type("_R", (), {"capability": remedy, "resolved": True})())

                # OBSERVE, NEVER PREDICT. `provides` is a claim the capability
                # makes about itself; the only evidence it worked is looking
                # again. `fresh=True` because a cached reading is precisely the
                # answer we are trying to replace.
                world.invalidate()
                if await world.satisfies(predicate, fresh=True):
                    logger.info("[VoiceRouter] CAI: '%s' now satisfied — "
                                "continuing with the original command",
                                predicate)
                    continue

                # It did not work. Stop, and say which half failed — "I
                # couldn't unlock" and "I unlocked but still can't see the
                # screen" need different responses from a person.
                logger.warning("[VoiceRouter] CAI: '%s' still unmet after "
                               "'%s' — abandoning the original command",
                               predicate, remedy)
                spoken = (getattr(step, "response_text", "") or "").strip()
                if getattr(step, "pending", False):
                    return step
                return CommandResult(
                    success=False, category="system_action", steps_completed=0,
                    steps_total=2, error=f"precondition {predicate} unmet",
                    response_text=(
                        f"{spoken} I couldn't {_humanise(remedy)}, so I've "
                        f"left the rest alone." if spoken else
                        f"I couldn't {_humanise(remedy)} first, so I've left "
                        f"the rest alone."))
            return None
        except Exception:  # noqa: BLE001 — CAI never blocks a command
            logger.debug("[VoiceRouter] precondition pass degraded",
                         exc_info=True)
            return None

    @staticmethod
    def _requirements(category: str, reflex: Any) -> tuple:
        """What this command needs to be true. NEVER raises.

        Two sources, neither of them a phrase table:

        * a reflex-resolved capability DECLARES its own `requires` — the exact
          answer, from the definition site;
        * otherwise the ROUTER'S OWN CATEGORY answers it. The classifier
          already decided whether this drives the UI; `app_action`,
          `navigation`, `vision_action` and `composite` all mean "something is
          going to happen on screen", and `query` means it is not.

        The dead `ScreenLockContextDetector._command_requires_screen` answered
        this by matching ~30 hardcoded substrings — including a bare `'open'`,
        and an exception list for `'lock screen'` to stop it unlocking the Mac
        in order to lock it. Reusing a decision the router has already made is
        both more accurate and one fewer vocabulary to keep in sync.
        """
        try:
            if reflex is not None and getattr(reflex, "resolved", False):
                from backend.system_control.capability_registry import (
                    get_capability_registry,
                )
                cap = get_capability_registry().get(reflex.capability)
                # A declaring capability speaks for itself — including by
                # declaring NOTHING. `lock_screen` needs no unlocked screen,
                # and inferring one from its category would deadlock it
                # against itself.
                return tuple(getattr(cap, "requires", ()) or ()) if cap else ()
            if category in ("app_action", "navigation", "vision_action",
                            "composite", "code_action"):
                return ("screen_unlocked",)
            return ()
        except Exception:  # noqa: BLE001
            return ()

    @staticmethod
    def _who_provides(predicate: str) -> Optional[str]:
        """The capability that establishes *predicate*, or None. NEVER raises.

        Derived from the registry. Ambiguity is a refusal, not a coin toss: two
        capabilities claiming the same effect is a declaration bug, and picking
        one silently is how the wrong one runs for a year.
        """
        try:
            from backend.system_control.capability_registry import (
                get_capability_registry,
            )
            from backend.system_control.world_state import canonical
            want = canonical(predicate)
            found = [d.name for d in get_capability_registry().all()
                     if any(canonical(p) == want
                            for p in (getattr(d, "provides", ()) or ()))]
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                logger.warning("[VoiceRouter] %d capabilities claim to provide "
                               "'%s' (%s) — refusing to choose",
                               len(found), predicate, ", ".join(sorted(found)))
            return None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _explain_detour(predicate: str, remedy: str, command: str) -> str:
        """Say WHY there is about to be an extra step. NEVER raises.

        Spoken before the detour rather than after, because the operator is
        about to watch their Mac do something they did not ask for, and an
        unexplained action is indistinguishable from a malfunction.
        """
        try:
            # Say the SITUATION, then the remedy, then the intent to continue.
            # A capability name read aloud ("I'll unlock screen first") is the
            # machine's vocabulary leaking into the room; the operator wants
            # the sentence a colleague would say.
            if "screen" in predicate:
                return ("Your screen is locked — let me unlock it first, "
                        "then I'll carry on.")
            state = predicate.replace("_", " ")
            return (f"I need {state} first, so I'll {_humanise(remedy)} — "
                    f"then I'll carry on.")
        except Exception:  # noqa: BLE001
            return "One moment — I need to sort something out first."

    async def _say(self, text: str) -> None:
        """Narrate, if anything is listening. NEVER raises."""
        try:
            if self._narrate_fn and text:
                note_spoken(text)
                await self._narrate_fn(text)
        except Exception:  # noqa: BLE001
            pass

    # ── The reflex arc ──────────────────────────────────────────────────

    def _reflex(self, command: str) -> Optional[Any]:
        """Resolve the sentence to a named capability, or None. NEVER raises.

        Wrapped rather than called inline so that a fault in the reflex costs
        the model round-trip it was meant to save, and nothing else. A reflex
        that can take the voice path down with it is worse than no reflex.
        """
        try:
            from backend.system_control.capability_reflex import (
                get_capability_reflex,
            )
            out = get_capability_reflex().resolve(command)
            if not out.resolved:
                logger.debug("[VoiceRouter] reflex declined (%s): %s",
                             out.outcome, out.reason)
            return out
        except Exception:  # noqa: BLE001
            logger.debug("[VoiceRouter] reflex unavailable", exc_info=True)
            return None

    async def _execute_capability(self, reflex: Any) -> CommandResult:
        """Run a reflex-resolved capability through the consent boundary.

        Goes through `CapabilityRouter.route`, never around it. The reflex
        decided WHICH capability; the registry still decides whether it needs
        the Iron Gate, the router still mints the nonce and suspends, and the
        operator still sees the Touch ID prompt. Arriving at the gate without
        a model does not mean arriving past it.

        Called with NO arguments, always — `capability_reflex` refuses to
        resolve anything whose signature has a required parameter, so an empty
        call here is a guarantee rather than an omission.
        """
        capability = str(getattr(reflex, "capability", "") or "")
        if not _claim_in_flight(capability):
            logger.warning("[VoiceRouter] COALESCED '%s' — the same action is "
                           "already running; not starting a second one",
                           capability)
            return CommandResult(
                success=True, category="system_action", steps_completed=0,
                steps_total=0, error=None,
                response_text=f"Already {_humanise(capability)} — one moment.")
        try:
            return await self._route_capability(reflex, capability)
        finally:
            # Released on EVERY path, including the suspended one. A slot that
            # leaks disables its own capability until the ceiling sweeps it,
            # which would turn a crash into "JARVIS will not lock my screen
            # any more" — a worse failure than the double-lock it prevents.
            _release_in_flight(capability)

    async def _route_capability(self, reflex: Any,
                                capability: str) -> CommandResult:
        """Route one already-claimed capability. NEVER raises."""
        try:
            from backend.system_control.capability_router import (
                Outcome, get_capability_router,
            )
            routed = await get_capability_router().route(
                reflex.capability, {}, op_id=f"reflex:{reflex.capability}")
        except Exception as exc:  # noqa: BLE001
            logger.error("[VoiceRouter] capability route failed: %s", exc)
            return CommandResult(
                success=False, category="system_action", steps_completed=0,
                steps_total=1, error=str(exc),
                response_text=f"I couldn't run {_humanise(reflex.capability)}.")

        human = _humanise(reflex.capability)
        outcome = routed.outcome

        if outcome == Outcome.EXECUTED.value:
            # HONOUR THE RETURN VALUE. `_execute` reports EXECUTED when the
            # call did not RAISE — but `lock_screen` answers
            # `(False, "Unable to lock screen (all methods failed)")` without
            # raising anything. Reading only the router's outcome would report
            # a locked screen to an operator looking at an unlocked one, which
            # is the one failure mode worse than not acting.
            ok, spoken = _read_controller_result(routed.result, human)
            return CommandResult(
                success=ok, category="system_action",
                steps_completed=1 if ok else 0, steps_total=1,
                response_text=spoken, error=None if ok else spoken)

        if outcome == Outcome.SUSPENDED.value:
            logger.info("[VoiceRouter] '%s' awaiting operator consent "
                        "(request=%s)", reflex.capability,
                        (routed.request_id or "")[:12])
            return CommandResult(
                success=False, pending=True, category="system_action",
                steps_completed=0, steps_total=1, error=None,
                response_text=f"I need your approval to {human}. "
                              f"Check the prompt on screen.")

        if outcome == Outcome.DENIED.value:
            # PREFER THE REASON THE AUTHORITY GAVE.
            #
            # DENIED covers many situations and only one of them is "you said
            # no". Measured 2026-08-03 01:41:27 — the voice authority answered
            # "I don't have a voiceprint for you on this Mac yet", and this
            # branch recognised only the no-provider case, fell through, and
            # said:
            #
            #     "You declined unlock screen, so I left it alone."
            #
            # Derek declined nothing. The true reason was already a finished
            # sentence in `routed.detail` and was overwritten with a
            # fabrication about the operator's own behaviour — the same defect
            # as the consent verdict reporting "operator declined" for a
            # prompt nobody drew, one layer up and in my own code.
            #
            # So: if the authority wrote a sentence, that IS the answer.
            # Matching keywords to pick a canned line is what produced the lie,
            # and a longer keyword list would only postpone the next one.
            detail = (routed.detail or "").strip()
            if _reads_like_a_sentence(detail):
                spoken = detail
            elif "no approval provider" in detail.lower():
                spoken = (f"I couldn't ask anyone to approve {human}, so I "
                          f"didn't do it.")
            else:
                spoken = f"I couldn't {human}: {detail or 'it was refused'}."
            return CommandResult(
                success=False, category="system_action", steps_completed=0,
                steps_total=1, error=detail or "denied", response_text=spoken)

        if outcome == Outcome.EXPIRED.value:
            return CommandResult(
                success=False, category="system_action", steps_completed=0,
                steps_total=1, error=routed.detail or "expired",
                response_text=f"The approval for {human} timed out.")

        return CommandResult(
            success=False, category="system_action", steps_completed=0,
            steps_total=1, error=routed.detail or outcome,
            response_text=f"I couldn't {human}: {routed.detail or outcome}.")

    async def _classify(self, command: str) -> dict:
        """Classify intent via Doubleword 35B."""
        try:
            prompt = _CLASSIFY_PROMPT.format(command=command)
            raw = await self._dw.prompt_only(
                prompt,
                model=_CLASSIFIER_MODEL,
                caller_id="voice_classifier",
                max_tokens=200,
            )
            if not raw:
                return {"category": "composite", "needs_vision": False, "needs_tools": True}

            # Parse JSON
            text = raw.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:]).strip()

            try:
                return json.loads(text)
            except json.JSONDecodeError:
                json_match = re.search(r'\{[^{}]*\}', text)
                if json_match:
                    return json.loads(json_match.group())

        except Exception as exc:
            logger.warning("[VoiceRouter] Classification failed: %s -- falling back to composite", exc)

        return {"category": "composite", "needs_vision": False, "needs_tools": True}

    async def _execute_app_action(self, command: str) -> CommandResult:
        """Extract app name and open/close it."""
        lower = command.lower()
        # Extract app name from command
        app_match = re.search(
            r"(?:open|launch|start|close|quit)\s+(?:the\s+)?(.+?)(?:\s+app)?$",
            lower,
            re.IGNORECASE,
        )
        app_name = app_match.group(1).strip() if app_match else command

        if "close" in lower or "quit" in lower:
            result = await self._applescript.run_script(
                f'tell application "{self._applescript.discover_app(app_name)}" to quit'
            )
            return CommandResult(
                success=result.success,
                category="app_action",
                steps_completed=1,
                steps_total=1,
                response_text=f"Closed {app_name}." if result.success else f"Couldn't close {app_name}.",
                error=result.error,
            )

        result = await self._applescript.open_app(app_name)
        return CommandResult(
            success=result.success,
            category="app_action",
            steps_completed=1,
            steps_total=1,
            response_text=result.output if result.success else f"Couldn't open {app_name}.",
            error=result.error,
        )

    async def _execute_navigation(self, command: str) -> CommandResult:
        """Extract URL/site and navigate to it."""
        lower = command.lower()
        # Remove "go to", "navigate to", "open" prefix
        site = re.sub(r"^(go\s+to|navigate\s+to|open)\s+", "", lower).strip()
        url = self._applescript.infer_url(site)
        result = await self._applescript.open_url(url)
        return CommandResult(
            success=result.success,
            category="navigation",
            steps_completed=1,
            steps_total=1,
            response_text=result.output if result.success else f"Couldn't navigate to {site}.",
            error=result.error,
        )

    async def _execute_query(self, command: str, screenshot_b64: Optional[str]) -> CommandResult:
        """Answer a question via LLM."""
        answer = await self._query.answer(command)
        return CommandResult(
            success=True,
            category="query",
            steps_completed=1,
            steps_total=1,
            response_text=answer,
            error=None,
        )

    async def _execute_vision(self, command: str, screenshot_b64: Optional[str]) -> CommandResult:
        """Dispatch to VLA pipeline (JarvisCU) for vision-dependent actions."""
        try:
            from backend.vision.jarvis_cu import JarvisCU
            import numpy as np
            from PIL import Image
            import base64
            import io

            cu = JarvisCU()
            frame = None
            if screenshot_b64:
                img = Image.open(io.BytesIO(base64.b64decode(screenshot_b64)))
                frame = np.array(img.convert("RGB"))

            result = await cu.run(command, initial_frame=frame)
            success = result.get("success", False)
            steps = result.get("steps_completed", 0)
            total = result.get("steps_total", 0)
            error = result.get("error")

            return CommandResult(
                success=success,
                category="vision_action",
                steps_completed=steps,
                steps_total=total,
                response_text=f"Completed {steps}/{total} steps." if success else f"Vision task failed: {error}",
                error=error,
            )
        except Exception as exc:
            return CommandResult(
                success=False,
                category="vision_action",
                steps_completed=0,
                steps_total=0,
                response_text=f"Vision system error: {exc}",
                error=str(exc),
            )

    async def _execute_code_action(self, command: str) -> CommandResult:
        """Route to Ouroboros governance pipeline for code tasks."""
        # For now, route through the tool-use orchestrator which can use bash tools
        # Full GovernedLoopService integration is a future enhancement
        return await self._tool_orchestrator.execute(command)
