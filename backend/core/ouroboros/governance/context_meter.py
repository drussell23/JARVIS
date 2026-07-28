"""How close is this op to losing its context — and to WHICH wall.

Claude Code's answer is one line: ``Context left until auto-compact: 23%``.
O+V has had the ingredients for that line and never assembled them, and the
piece it did surface measures something subtly different from what it says.

What was already true
---------------------
``tool_executor`` keeps a ``_CONTEXT_GAUGE``: per-op ``len(prompt)`` over
``JARVIS_TOOL_LOOP_MAX_PROMPT_CHARS``. ``attach_heartbeat`` carries that
fraction and the attach pulse renders ``ctx 78% · compacting`` once it nears
the compaction threshold. That is real, correct, and it is a CHARACTER budget
this process enforces on itself.

The wall it does not measure
-----------------------------
The other wall is the provider's TOKEN window, and nothing compares against
it. An op can sit at 40% of the local char budget and 95% of a model's
context window — the two are not proportional, because they are denominated
in different units over content whose ratio varies by a factor of two between
prose and minified code. The operator would get no warning at all before the
provider truncated or refused.

So this reports BOTH and names which one binds. Those two walls call for
different actions — approaching compaction means earlier rounds are about to
be summarised away, approaching the window means the request itself is about
to fail — and a single blended percentage would tell the operator neither.

Tokens are MEASURED, not assumed
---------------------------------
Converting chars to tokens needs a ratio, and the usual constant of 4.0 is a
prose average. Python with long identifiers runs nearer 3.2; a base64 blob or
a minified bundle runs past 6. At a 131k budget that spread is tens of
thousands of tokens of error, in the direction that matters.

The organism already knows the true answer: every provider call returns its
own server-side tokenizer count (``usage.input_tokens`` / DW's
``input_tokens``). Pairing that with the prompt chars this process measured
for the same op yields the ratio for the content this organism is actually
sending — per provider, updated as it goes. The configured default is the
prior, used until enough real pairs have been seen, and the reading always
says which of the two it used.

Unknown is an answer
--------------------
A window that cannot be resolved is reported as unknown — never defaulted to
somebody's 200k. This is the discipline the advisor locality arc arrived at
the expensive way: a fabricated measurement presented as a real one is worse
than no measurement, because it is acted on. An unknown window simply means
the compaction wall is the only one this meter can speak about, and it says
so.

Every threshold is an env knob; nothing in the code paths inlines a number.
NEVER raises — a meter that can break a render is not worth having.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("Ouroboros.ContextMeter")

CONTEXT_METER_SCHEMA_VERSION = "context_meter.v1"

MASTER_FLAG_ENV_VAR = "JARVIS_CONTEXT_METER_ENABLED"
WINDOW_OVERRIDE_ENV_VAR = "JARVIS_CONTEXT_WINDOW_TOKENS"
MIN_SAMPLES_ENV_VAR = "JARVIS_CONTEXT_RATIO_MIN_SAMPLES"
EWMA_ALPHA_ENV_VAR = "JARVIS_CONTEXT_RATIO_ALPHA"
WARN_FRACTION_ENV_VAR = "JARVIS_CONTEXT_WARN_FRACTION"

#: Binding-limit names. Which wall is nearer decides what the operator does.
BINDING_COMPACTION = "compaction"
BINDING_WINDOW = "window"
BINDING_UNKNOWN = "unknown"


def meter_enabled() -> bool:
    """Master flag — default ON. Re-read per call so a flip hot-reverts."""
    return os.environ.get(
        MASTER_FLAG_ENV_VAR, "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(os.environ.get(name, "") or default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.environ.get(name, "") or default)))
    except (TypeError, ValueError):
        return default


def min_ratio_samples() -> int:
    """Real (chars, tokens) pairs before the measured ratio outranks the
    configured prior. Two samples can agree by luck; a handful cannot."""
    return _env_int(MIN_SAMPLES_ENV_VAR, 5, 1, 1000)


def ratio_alpha() -> float:
    """EWMA weight on the newest observation.

    An average over the whole session would be dragged for hours by a single
    early op that read a lockfile. Exponential decay lets the ratio follow
    what the organism is working on NOW, which is the thing the operator's
    next round will be made of.
    """
    return _env_float(EWMA_ALPHA_ENV_VAR, 0.25, 0.01, 1.0)


def warn_fraction() -> float:
    """Fraction of the binding limit at which the meter becomes worth showing.

    Below this, every op starts near empty and a permanent 3% would teach the
    operator to ignore the field — the surest way to make it useless on the
    day it says 91%.
    """
    return _env_float(WARN_FRACTION_ENV_VAR, 0.60, 0.0, 1.0)


# ---------------------------------------------------------------------------
# The calibrator — real token counts against real char counts
# ---------------------------------------------------------------------------


_LOCK = threading.Lock()
#: op_id → prompt chars, most recently measured by the tool loop.
_CHARS: Dict[str, int] = {}
#: provider → (ewma_ratio, samples)
_RATIO: Dict[str, Tuple[float, int]] = {}
_CHARS_MAX = 64


def note_prompt_chars(op_id: str, chars: int) -> None:
    """Half one of a calibration pair, from the seam that already measures it.

    Fed by ``tool_executor.note_context_utilisation`` — the loop counts these
    characters every round regardless, so this adds a dict write, not a
    measurement. NEVER raises.
    """
    try:
        key = str(op_id or "")
        if not key or int(chars) <= 0:
            return
        with _LOCK:
            _CHARS.pop(key, None)
            _CHARS[key] = int(chars)
            while len(_CHARS) > _CHARS_MAX:
                _CHARS.pop(next(iter(_CHARS)), None)
    except Exception:  # noqa: BLE001
        pass


def note_prompt_tokens(op_id: str, provider: str, tokens: int) -> None:
    """Half two — the provider's OWN tokenizer count, closing the pair.

    This is ground truth, not an estimate: Claude's ``usage.input_tokens``
    and DW's ``input_tokens`` are what those services actually charged for.
    Pairing it with the chars this process measured for the SAME op gives the
    ratio for the content this organism really sends.

    A pair is only formed when both halves exist for one op. A token count
    with no matching char count is discarded rather than attributed to
    whatever op was measured last — a mis-paired sample poisons the ratio in
    a way nothing downstream could detect. NEVER raises.
    """
    try:
        key = str(op_id or "")
        tok = int(tokens or 0)
        if not key or tok <= 0:
            return
        with _LOCK:
            chars = _CHARS.get(key, 0)
            if chars <= 0:
                return
            observed = float(chars) / float(tok)
            # A ratio outside this range is not content, it is a bug — a
            # mismatched pair, a truncated prompt, a cached-token response
            # that reports a fraction of what was sent. Folding it in would
            # move the estimate for every op that follows.
            if not 1.0 <= observed <= 20.0:
                return
            name = (str(provider or "") or "default").strip().lower()
            prior, samples = _RATIO.get(name, (0.0, 0))
            alpha = ratio_alpha()
            blended = observed if samples == 0 else (
                alpha * observed + (1.0 - alpha) * prior
            )
            _RATIO[name] = (blended, samples + 1)
    except Exception:  # noqa: BLE001
        pass


def chars_per_token(provider: str = "") -> Tuple[float, str]:
    """``(ratio, provenance)`` — measured when it can be, configured until.

    Provenance is returned rather than hidden because the two are not equally
    trustworthy, and a surface that shows a token count has to be able to say
    whether it counted or estimated.
    """
    try:
        name = (str(provider or "") or "default").strip().lower()
        with _LOCK:
            ratio, samples = _RATIO.get(name, (0.0, 0))
            if samples == 0 and name != "default":
                ratio, samples = _RATIO.get("default", (0.0, 0))
        if samples >= min_ratio_samples() and ratio > 0.0:
            return ratio, f"observed:{samples}"
        # The configured prior lives in s2_predictive_budget, which already
        # owns this knob for cost forecasting. A second default here would be
        # a second answer to the same question.
        from backend.core.ouroboros.governance.s2_predictive_budget import (
            chars_per_token as configured,
        )
        return float(configured()), "configured"
    except Exception:  # noqa: BLE001
        return 4.0, "fallback"


def reset_for_tests() -> None:
    with _LOCK:
        _CHARS.clear()
        _RATIO.clear()


# ---------------------------------------------------------------------------
# Window resolution — a cascade of real sources, then honest ignorance
# ---------------------------------------------------------------------------


def window_tokens(
    provider: str = "", model: str = "",
) -> Tuple[Optional[int], str]:
    """``(window, provenance)`` for the active model, or ``(None, "unknown")``.

    Ordered by authority, and every rung is a source that already exists:

      1. ``JARVIS_CONTEXT_WINDOW_TOKENS`` — the operator's explicit answer,
         which outranks anything inferred.
      2. The live DW catalog. ``dw_catalog_client`` already fetches
         ``context_window`` per model from DW's ``/models`` endpoint, so for
         the tier-0 provider this is measured, current, and free.
      3. ``brain_selection_policy.yaml``. The file spells the same concept
         two ways — ``context_window`` under hosted candidates,
         ``max_prompt_tokens`` under brains — so both are read rather than
         picking one and silently missing every model declared the other way.
      4. Unknown. NOT a default: see the module docstring.
    """
    try:
        override = _env_int(WINDOW_OVERRIDE_ENV_VAR, 0, 0, 1 << 30)
        if override > 0:
            return override, "env"

        found = _window_from_dw_catalog(model)
        if found:
            return found, "dw_catalog"

        found = _window_from_policy(model)
        if found:
            return found, "policy_yaml"

        return None, "unknown"
    except Exception:  # noqa: BLE001
        return None, "unknown"


def _window_from_dw_catalog(model: str) -> Optional[int]:
    try:
        from backend.core.ouroboros.governance.dw_catalog_client import (
            load_cached_snapshot,
        )
        # The DISK-cached snapshot, never a live fetch. A meter is read on
        # every repaint; reaching for the network from a render path would
        # put provider latency inside the frame budget, and a stale window is
        # a far smaller error than a stalled cockpit.
        snapshot = load_cached_snapshot(None)
        catalog = getattr(snapshot, "models", None) or ()
        wanted = str(model or "").strip().lower()
        for entry in catalog:
            name = str(getattr(entry, "model_id", "")
                       or getattr(entry, "model_name", "")).strip().lower()
            window = getattr(entry, "context_window", None)
            if not window:
                continue
            if not wanted or wanted in name or name in wanted:
                return int(window)
    except Exception:  # noqa: BLE001
        return None
    return None


def _window_from_policy(model: str) -> Optional[int]:
    try:
        import yaml
        from pathlib import Path

        path = Path(__file__).parent / "brain_selection_policy.yaml"
        if not path.is_file():
            return None
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        wanted = str(model or "").strip().lower()
        if not wanted:
            return None
        for name, window in _walk_windows(data):
            if window and name and wanted in name:
                return int(window)
        # NO MATCH IS NOT A LICENCE TO GUESS.
        #
        # This returned the smallest declared window when the model was
        # unknown, on the reasoning that the smallest is the "safe" reading.
        # It is not safe, it is fabricated: this file declares 4,096 for a
        # local 1B llama, so an op running against a remote 397B was told it
        # had 0% of its window left and the meter named `window` as the
        # binding wall. A confident alarm about a limit nobody measured is
        # worse than silence, because silence does not get acted on.
        #
        # Unknown propagates instead, and the compaction wall — which IS
        # measured — becomes the only one the reading speaks about.
        return None
    except Exception:  # noqa: BLE001
        return None


def _walk_windows(node: Any, name: str = ""):
    """Yield ``(model_name, window)`` for both spellings, at any depth."""
    try:
        if isinstance(node, dict):
            label = str(node.get("model_name") or node.get("model_id")
                        or name or "").strip().lower()
            for key in ("context_window", "max_prompt_tokens"):
                raw = node.get(key)
                if isinstance(raw, int) and raw > 0:
                    yield label, raw
            for key, value in node.items():
                yield from _walk_windows(value, str(key))
        elif isinstance(node, list):
            for item in node:
                yield from _walk_windows(item, name)
    except Exception:  # noqa: BLE001
        return


# ---------------------------------------------------------------------------
# The reading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextReading:
    """What is known about one op's context pressure, with its provenance."""

    op_id: str
    used_chars: int
    used_tokens: int
    compaction_pct: float
    window_tokens: Optional[int]
    window_pct: Optional[float]
    binding: str
    binding_pct: float
    ratio: float
    ratio_provenance: str
    window_provenance: str
    schema_version: str = CONTEXT_METER_SCHEMA_VERSION

    @property
    def worth_showing(self) -> bool:
        """Below the warn fraction this is noise — see :func:`warn_fraction`."""
        return self.binding_pct >= warn_fraction()

    @property
    def headroom_pct(self) -> float:
        return max(0.0, 1.0 - self.binding_pct)


def read(op_id: str, *, provider: str = "", model: str = "") -> Optional[
    ContextReading
]:
    """Compose a reading for ``op_id``, or None when nothing is known.

    None rather than a zeroed reading: "this op has used no context" and "we
    have never measured this op" are different facts, and a caller that
    cannot tell them apart will render the first when it means the second.
    """
    try:
        if not meter_enabled():
            return None
        from backend.core.ouroboros.governance.tool_executor import (
            compaction_threshold_fraction,
            context_utilisation,
        )
        pct = float(context_utilisation(op_id) or 0.0)
        with _LOCK:
            chars = int(_CHARS.get(str(op_id or ""), 0))
        if pct <= 0.0 and chars <= 0:
            return None

        ratio, ratio_prov = chars_per_token(provider)
        tokens = int(chars / ratio) if chars > 0 and ratio > 0 else 0

        # Against the LOCAL wall: the fraction of the compaction threshold
        # consumed, not of the hard ceiling. Compaction is the event the
        # operator experiences, so it is the one the meter counts down to.
        floor = max(0.01, float(compaction_threshold_fraction()))
        compaction_pct = max(0.0, min(1.0, pct / floor))

        window, window_prov = window_tokens(provider, model)
        window_pct: Optional[float] = None
        if window and tokens > 0:
            window_pct = max(0.0, min(1.0, float(tokens) / float(window)))

        if window_pct is not None and window_pct > compaction_pct:
            binding, binding_pct = BINDING_WINDOW, window_pct
        elif compaction_pct > 0.0:
            binding, binding_pct = BINDING_COMPACTION, compaction_pct
        else:
            binding, binding_pct = BINDING_UNKNOWN, 0.0

        return ContextReading(
            op_id=str(op_id or ""),
            used_chars=chars,
            used_tokens=tokens,
            compaction_pct=round(compaction_pct, 4),
            window_tokens=window,
            window_pct=None if window_pct is None else round(window_pct, 4),
            binding=binding,
            binding_pct=round(binding_pct, 4),
            ratio=round(ratio, 3),
            ratio_provenance=ratio_prov,
            window_provenance=window_prov,
        )
    except Exception:  # noqa: BLE001
        logger.debug("[ContextMeter] read degraded", exc_info=True)
        return None


def render(reading: Optional[ContextReading], *, verbose: bool = False) -> str:
    """The operator-facing line, or "" when there is nothing worth saying.

    Names the WALL, because the action differs: compaction means earlier
    rounds are about to be summarised away and the fix is to narrow the next
    fetch; the window means the request itself is about to be refused and the
    fix is a different model or a smaller scope.
    """
    try:
        if reading is None or not reading.worth_showing:
            return ""
        left = int(round(reading.headroom_pct * 100))
        if reading.binding == BINDING_WINDOW:
            head = f"ctx {left}% left of window"
        else:
            head = f"ctx {left}% left until compact"
        if not verbose:
            return head
        bits = [head, f"~{reading.used_tokens:,} tok"]
        if reading.window_tokens:
            bits.append(f"of {reading.window_tokens:,}"
                        f" ({reading.window_provenance})")
        bits.append(f"{reading.ratio} ch/tok ({reading.ratio_provenance})")
        return " · ".join(bits)
    except Exception:  # noqa: BLE001
        return ""


def as_payload(reading: Optional[ContextReading]) -> Optional[dict]:
    """Transport-safe dict for the heartbeat. None stays None."""
    try:
        if reading is None:
            return None
        return {
            "schema_version": CONTEXT_METER_SCHEMA_VERSION,
            "used_tokens": reading.used_tokens,
            "compaction_pct": reading.compaction_pct,
            "window_tokens": reading.window_tokens,
            "window_pct": reading.window_pct,
            "binding": reading.binding,
            "binding_pct": reading.binding_pct,
            "ratio": reading.ratio,
            "ratio_provenance": reading.ratio_provenance,
            "window_provenance": reading.window_provenance,
        }
    except Exception:  # noqa: BLE001
        return None


def render_payload(payload: Optional[dict], *, verbose: bool = False) -> str:
    """Render a reading that arrived over the bridge.

    A remote cockpit holds a dict, not a dataclass. Rehydrating here means the
    two surfaces share one renderer — the lesson the agent roster paid for.
    """
    try:
        if not isinstance(payload, dict):
            return ""
        return render(ContextReading(
            op_id="",
            used_chars=0,
            used_tokens=int(payload.get("used_tokens") or 0),
            compaction_pct=float(payload.get("compaction_pct") or 0.0),
            window_tokens=payload.get("window_tokens"),
            window_pct=payload.get("window_pct"),
            binding=str(payload.get("binding") or BINDING_UNKNOWN),
            binding_pct=float(payload.get("binding_pct") or 0.0),
            ratio=float(payload.get("ratio") or 0.0),
            ratio_provenance=str(payload.get("ratio_provenance") or ""),
            window_provenance=str(payload.get("window_provenance") or ""),
        ), verbose=verbose)
    except Exception:  # noqa: BLE001
        return ""


__all__ = [
    "BINDING_COMPACTION",
    "BINDING_UNKNOWN",
    "BINDING_WINDOW",
    "CONTEXT_METER_SCHEMA_VERSION",
    "ContextReading",
    "as_payload",
    "chars_per_token",
    "meter_enabled",
    "min_ratio_samples",
    "note_prompt_chars",
    "note_prompt_tokens",
    "ratio_alpha",
    "read",
    "render",
    "render_payload",
    "reset_for_tests",
    "warn_fraction",
    "window_tokens",
]
