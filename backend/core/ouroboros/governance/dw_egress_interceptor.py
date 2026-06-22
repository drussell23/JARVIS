"""dw_egress_interceptor.py -- Sovereign Egress Interceptor: local pre-flight guard.

Sanitizes and weight-checks request bodies BEFORE they leave the process toward
DoubleWord. Zero network I/O on the hot path. Fail-soft asymmetry (binding):
  - sanitize/estimate ERROR -> pass-through (never blocks)
  - CONFIRMED overweight     -> raise LocalEgressOverweightError

Lazy imports of doubleword_provider/_dw_model_min_effort/_clamp_up_to_min and
dw_catalog_client.ModelCard are done INSIDE functions (not at module top) to
avoid a circular-import cycle: T2 will make doubleword_provider import this module.

All public functions are wrapped fail-soft per the I2 asymmetry requirement.
ASCII-only source. Python 3.9+.
"""
from __future__ import annotations

import json
import os
from typing import Dict


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class LocalEgressOverweightError(Exception):
    """Raised when the estimated egress body size exceeds the local ceiling.

    Carries enough math for the caller to decide on a remediation strategy
    (e.g. trim messages, compress, or route to a larger context model).

    ``required_compression_ratio`` is guaranted non-ZeroDivisionError:
    when ``max_allowed_size==0`` we clamp to ``attempted_size`` (ratio >= 1.0).
    """

    def __init__(
        self,
        *,
        attempted_size: int,
        max_allowed_size: int,
        model: str,
    ) -> None:
        self.attempted_size: int = attempted_size
        self.max_allowed_size: int = max_allowed_size
        self.model: str = model
        # Guard /0: if ceiling is 0 treat it as 1 so ratio is always >= 1.
        divisor = max(1, max_allowed_size)
        self.required_compression_ratio: float = attempted_size / divisor
        super().__init__(
            f"Egress body too large for model={model!r}: "
            f"{attempted_size} chars > ceiling {max_allowed_size} "
            f"(need {self.required_compression_ratio:.2f}x compression)"
        )


# ---------------------------------------------------------------------------
# Master switch
# ---------------------------------------------------------------------------


def egress_interceptor_enabled() -> bool:
    """Return True unless JARVIS_DW_EGRESS_INTERCEPTOR_ENABLED is falsy.

    Default TRUE (safe-by-default: guard is ON unless the operator opts out).
    NEVER raises.
    """
    return os.environ.get(
        "JARVIS_DW_EGRESS_INTERCEPTOR_ENABLED", "true"
    ).strip().lower() not in ("0", "false", "no", "off")


# ---------------------------------------------------------------------------
# Estimation
# ---------------------------------------------------------------------------


def estimate_body_chars(body: dict) -> int:
    """Estimate the character footprint of a request body.

    Sums len(str(content)) for every message in body["messages"] and also
    the top-level "prompt" key when present. Fail-soft: any error -> 0.
    """
    try:
        total = 0
        msgs = body.get("messages")
        if isinstance(msgs, list):
            for msg in msgs:
                if isinstance(msg, dict):
                    content = msg.get("content")
                    if content is not None:
                        total += len(str(content))
        # Also count a top-level "prompt" key if present (some DW routes use it).
        prompt = body.get("prompt")
        if prompt is not None:
            total += len(str(prompt))
        return total
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------------------
# Ceiling
# ---------------------------------------------------------------------------

_DEFAULT_EGRESS_MAX_CHARS: int = 600_000
_DEFAULT_CHARS_PER_TOKEN: int = 4


def _model_card_char_budget(model: str) -> int | None:
    """Return the char budget derived from the catalog ModelCard for ``model``.

    Uses load_cached_snapshot (no network I/O) so this is fast and fail-soft.
    Returns None when the card or context_window is unavailable.
    """
    try:
        from backend.core.ouroboros.governance.dw_catalog_client import (  # noqa: PLC0415
            load_cached_snapshot,
        )
        snapshot = load_cached_snapshot()
        if snapshot is None:
            return None
        chars_per_token = int(
            os.environ.get("JARVIS_DW_EGRESS_CHARS_PER_TOKEN", str(_DEFAULT_CHARS_PER_TOKEN))
        )
        mid_lower = model.strip().lower()
        for card in snapshot.models:
            if card.model_id.strip().lower() == mid_lower:
                if isinstance(card.context_window, int) and card.context_window > 0:
                    return card.context_window * chars_per_token
                return None
        return None
    except Exception:  # noqa: BLE001
        return None


def egress_char_ceiling(model: str) -> int:
    """Return the char ceiling for ``model``.

    min(env JARVIS_DW_EGRESS_MAX_CHARS, ModelCard budget when available).
    Fail-soft -> env cap.
    """
    try:
        env_cap = int(
            os.environ.get("JARVIS_DW_EGRESS_MAX_CHARS", str(_DEFAULT_EGRESS_MAX_CHARS))
        )
    except (ValueError, TypeError):
        env_cap = _DEFAULT_EGRESS_MAX_CHARS
    try:
        card_budget = _model_card_char_budget(model)
        if card_budget is not None:
            return min(env_cap, card_budget)
        return env_cap
    except Exception:  # noqa: BLE001
        return env_cap


# ---------------------------------------------------------------------------
# Sanitize registry
# ---------------------------------------------------------------------------


_BUILTIN_RULES: Dict[str, dict] = {
    # gpt-oss family: cannot disable reasoning -> must floor effort UP.
    "gpt-oss": {"floor_reasoning": True},
}


def _sanitize_rules() -> Dict[str, dict]:
    """Return the merged sanitize-rule map: built-ins + env overrides.

    Env format (JARVIS_DW_EGRESS_SANITIZE_RULES):
        "substr={strip:p1|p2;floor_reasoning:true},..."

    Example:
        JARVIS_DW_EGRESS_SANITIZE_RULES="my-model={strip:top_p|temperature}"

    Built-in rules are always included; env adds or overrides by substr key.
    Unknown / malformed entries are skipped (fail-soft).
    NEVER raises.
    """
    rules: Dict[str, dict] = dict(_BUILTIN_RULES)
    raw = os.environ.get("JARVIS_DW_EGRESS_SANITIZE_RULES", "").strip()
    if not raw:
        return rules
    try:
        # Simple bespoke parser: key={directive;directive,...},...
        # We consume token by token (no regex dep needed).
        remaining = raw
        while remaining:
            remaining = remaining.strip()
            if not remaining:
                break
            # Find '=' that precedes a '{'
            eq_pos = remaining.find("={")
            if eq_pos < 0:
                break
            substr_key = remaining[:eq_pos].strip().lower()
            remaining = remaining[eq_pos + 1:]  # starts with '{'
            # Find matching '}'
            close = remaining.find("}")
            if close < 0:
                break
            directive_str = remaining[1:close]  # inside the braces
            remaining = remaining[close + 1:].lstrip(",")
            # Parse directives: semicolon-separated key:value pairs
            entry: dict = {}
            for directive in directive_str.split(";"):
                directive = directive.strip()
                if not directive:
                    continue
                if directive.startswith("strip:"):
                    params = [p.strip() for p in directive[6:].split("|") if p.strip()]
                    entry["strip"] = params
                elif directive.startswith("floor_reasoning:"):
                    val = directive[16:].strip().lower()
                    entry["floor_reasoning"] = val not in ("0", "false", "no", "off")
            if substr_key and entry:
                rules[substr_key] = entry
    except Exception:  # noqa: BLE001
        pass
    return rules


def _apply_rule(body: dict, rule: dict) -> dict:
    """Apply a single sanitize rule to a copy of ``body``.

    Handles two directives:
      - strip: [param, ...] — removes listed top-level keys
      - floor_reasoning: True — delegates to doubleword_provider reasoning floor

    Lazy import of doubleword_provider to avoid circular import cycle.
    NEVER raises (fail-soft -> return body unchanged on any error).
    """
    try:
        out = dict(body)
        # Strip unsupported params.
        for param in rule.get("strip", []):
            out.pop(param, None)
        # Reasoning floor: delegate to the existing floor logic (no reimplementation).
        if rule.get("floor_reasoning"):
            try:
                from backend.core.ouroboros.governance.doubleword_provider import (  # noqa: PLC0415
                    _dw_model_min_effort,
                    _clamp_up_to_min,
                )
                model = body.get("model", "")
                current_effort = out.get("reasoning_effort", "none")
                floor = _dw_model_min_effort(model)
                floored = _clamp_up_to_min(str(current_effort), floor)
                out["reasoning_effort"] = floored
            except Exception:  # noqa: BLE001
                pass  # fail-soft: leave reasoning_effort unchanged
        return out
    except Exception:  # noqa: BLE001
        return body


def sanitize_egress_body(body: dict, model: str) -> dict:
    """Sanitize ``body`` for ``model`` using the extensible rule registry.

    Applies rules whose substring key appears in ``model`` (case-insensitive).
    Unknown model (no matching rule) -> returns ``body`` unchanged.
    Fail-soft: any error -> return original ``body`` unchanged.
    Data-driven: NO hardcoded model if/elif chains in the apply loop.
    """
    try:
        rules = _sanitize_rules()
        mid_lower = model.strip().lower()
        out = body
        matched = False
        for substr, rule in rules.items():
            if substr in mid_lower:
                out = _apply_rule(out, rule)
                matched = True
        # Unknown model (no rule matched) -> pass-through.
        if not matched:
            return body
        return out
    except Exception:  # noqa: BLE001
        return body


# ---------------------------------------------------------------------------
# Weight assertion
# ---------------------------------------------------------------------------


def assert_egress_weight(body: dict, model: str) -> None:
    """Raise LocalEgressOverweightError if body exceeds the char ceiling.

    Estimation errors -> no raise (fail-soft asymmetry: only CONFIRMED
    overweight blocks; uncertainty never blocks).
    """
    try:
        weight = estimate_body_chars(body)
    except Exception:  # noqa: BLE001
        return  # estimation failed -> fail-soft, no block
    try:
        ceiling = egress_char_ceiling(model)
    except Exception:  # noqa: BLE001
        return  # ceiling lookup failed -> fail-soft, no block
    if weight > ceiling:
        raise LocalEgressOverweightError(
            attempted_size=weight,
            max_allowed_size=ceiling,
            model=model,
        )
