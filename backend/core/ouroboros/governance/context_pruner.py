"""Cognitive Context Pruning — semantic GC for the sovereign daemon.

Operator-authorized 2026-07-18. An indefinitely-looping FSM will
eventually assemble a provider payload past the model's context window
(``context_length_exceeded`` → fatal). This substrate is the defense,
built as transparent middleware at the ASSEMBLY SEAMS (Expansion /
Generate composition and Venom tool rounds) — deliberately NOT a 12th
FSM phase (a phase enum change would ripple through every AST pin for
a guard that only matters where prompts are composed).

Three organs:

1. **Limit resolution** — per-model ``context_window`` from the
   canonical ``brain_selection_policy.yaml`` model cards (zero
   hardcoded model names, per repo law), with provider-FAMILY env
   fallbacks for cards that don't declare one
   (``JARVIS_CONTEXT_LIMIT_{CLAUDE,DW,DEFAULT}``).
2. **TokenLedger** — a self-calibrating estimator: chars/ratio with the
   ratio corrected by ACTUAL usage counts observed from provider
   responses. No tokenizer dependency; converges on each provider's
   real densities.
3. **The two gates**:
   * ``prune_prompt_text`` — the SEMANTIC SLIDING WINDOW for gradual
     bloat: section-aware GC that compacts RESOLVED trajectories
     (histories, logs, prior attempts) through the existing
     :class:`ContextCompactor` machinery into a dense working-memory
     block, while the PROTECTED SET (North Star directive, active
     frontier, the live task, the plan) is structurally exempt — never
     a candidate, not merely deprioritized.
   * ``ballistic_intercept`` — the BALLISTIC PAYLOAD INTERCEPTOR for
     instantaneous spikes the rolling threshold is blind to: a single
     tool output / file read big enough to threaten the window is
     isolated and head+tail chunked WITH a deterministic forensic
     digest (sizes, line counts, sha) BEFORE it merges into FSM state.

Master ``JARVIS_CONTEXT_PRUNE_ENABLED`` — §33.1 default-FALSE;
graduation soaks arm it. Every function NEVER raises (a pruner fault
degrades to the unpruned input — the provider error it failed to
prevent is still better than a pruner crash).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes", "on")

CONTEXT_PRUNE_SCHEMA_VERSION = "context_prune.v1"


def context_prune_enabled() -> bool:
    """Master gate — §33.1 default FALSE. NEVER raises."""
    return os.environ.get(
        "JARVIS_CONTEXT_PRUNE_ENABLED", "",
    ).strip().lower() in _TRUTHY


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def prune_threshold() -> float:
    """Fraction of the resolved limit that triggers semantic GC
    (``JARVIS_CONTEXT_PRUNE_THRESHOLD``, default 0.80). Clamped."""
    return min(0.95, max(0.3, _f("JARVIS_CONTEXT_PRUNE_THRESHOLD", 0.80)))


def ballistic_max_fraction() -> float:
    """Largest fraction of the window ONE payload may occupy before the
    interceptor fires (``JARVIS_BALLISTIC_MAX_FRACTION``, default 0.25)."""
    return min(0.8, max(0.02, _f("JARVIS_BALLISTIC_MAX_FRACTION", 0.25)))


# ---------------------------------------------------------------------------
# 1. Limit resolution — brain_selection_policy.yaml is the authority
# ---------------------------------------------------------------------------


_POLICY_CACHE: Dict[str, Any] = {"mtime": None, "windows": {}}
_POLICY_LOCK = threading.Lock()


def _policy_path() -> Path:
    return Path(os.environ.get(
        "JARVIS_BRAIN_POLICY_PATH",
        "backend/core/ouroboros/governance/brain_selection_policy.yaml",
    ))


def _load_policy_windows() -> Dict[str, int]:
    """model_name (lowercased) -> context_window from the policy cards.
    mtime-cached; NEVER raises."""
    try:
        path = _policy_path()
        mtime = path.stat().st_mtime
        with _POLICY_LOCK:
            if _POLICY_CACHE["mtime"] == mtime:
                return dict(_POLICY_CACHE["windows"])
        import yaml  # lazy — policy readers repo-wide use PyYAML
        data = yaml.safe_load(path.read_text()) or {}
        windows: Dict[str, int] = {}

        def _walk(node: Any) -> None:
            if isinstance(node, dict):
                name = node.get("model_name") or node.get("model")
                cw = node.get("context_window")
                if name and isinstance(cw, int) and cw > 0:
                    windows[str(name).lower()] = cw
                for v in node.values():
                    _walk(v)
            elif isinstance(node, list):
                for v in node:
                    _walk(v)

        _walk(data)
        with _POLICY_LOCK:
            _POLICY_CACHE["mtime"] = mtime
            _POLICY_CACHE["windows"] = dict(windows)
        return windows
    except Exception:  # noqa: BLE001
        return {}


def resolve_context_limit(
    provider: str = "", model: str = "",
) -> int:
    """The active model's context window in tokens.

    Order: exact policy card → provider-family env fallback → global
    default. Family fallbacks are env-tunable, not hardcoded model
    facts (``JARVIS_CONTEXT_LIMIT_CLAUDE`` 200000 /
    ``JARVIS_CONTEXT_LIMIT_DW`` 262144 / ``JARVIS_CONTEXT_LIMIT_DEFAULT``
    131072 — the conservative floor of the declared cards). NEVER raises."""
    try:
        m = str(model or "").lower()
        if m:
            windows = _load_policy_windows()
            if m in windows:
                return windows[m]
            for name, cw in windows.items():
                if m in name or name in m:
                    return cw
        p = str(provider or "").lower()
        if "claude" in p or "anthropic" in p:
            return _i("JARVIS_CONTEXT_LIMIT_CLAUDE", 200_000)
        if "doubleword" in p or p == "dw":
            return _i("JARVIS_CONTEXT_LIMIT_DW", 262_144)
        return _i("JARVIS_CONTEXT_LIMIT_DEFAULT", 131_072)
    except Exception:  # noqa: BLE001
        return _i("JARVIS_CONTEXT_LIMIT_DEFAULT", 131_072)


# ---------------------------------------------------------------------------
# 2. TokenLedger — self-calibrating estimator
# ---------------------------------------------------------------------------


class TokenLedger:
    """chars→tokens estimator whose ratio converges on the ACTIVE
    provider's real density via observed usage counts. Thread-safe;
    NEVER raises."""

    def __init__(self, initial_chars_per_token: float = 4.0) -> None:
        self._lock = threading.Lock()
        self._ratio = max(1.0, float(initial_chars_per_token))
        self._observations = 0

    @property
    def chars_per_token(self) -> float:
        return self._ratio

    def estimate_tokens(self, text: str) -> int:
        try:
            return max(0, int(len(str(text)) / self._ratio))
        except Exception:  # noqa: BLE001
            return 0

    def observe_actual(self, chars: int, tokens: int) -> None:
        """Feed one (prompt chars, actual prompt tokens) pair from a
        provider response's usage block. EWMA — recent providers
        dominate. NEVER raises."""
        try:
            if chars <= 0 or tokens <= 0:
                return
            observed = chars / tokens
            if not (1.0 <= observed <= 12.0):
                return                     # implausible — ignore outliers
            with self._lock:
                alpha = 0.3 if self._observations else 1.0
                self._ratio = (1 - alpha) * self._ratio + alpha * observed
                self._observations += 1
        except Exception:  # noqa: BLE001
            pass


_DEFAULT_LEDGER: Optional[TokenLedger] = None
_LEDGER_LOCK = threading.Lock()


def get_default_ledger() -> TokenLedger:
    global _DEFAULT_LEDGER
    with _LEDGER_LOCK:
        if _DEFAULT_LEDGER is None:
            _DEFAULT_LEDGER = TokenLedger()
        return _DEFAULT_LEDGER


def reset_default_ledger() -> None:
    global _DEFAULT_LEDGER
    with _LEDGER_LOCK:
        _DEFAULT_LEDGER = None


# ---------------------------------------------------------------------------
# 3a. Semantic Sliding Window — section-aware GC for gradual bloat
# ---------------------------------------------------------------------------


#: Section-name shapes that are STRUCTURALLY EXEMPT — never GC
#: candidates. The organism's identity + live intent survive any purge.
PROTECTED_SECTION_PATTERNS = (
    r"north.?star", r"strategic", r"frontier", r"manifesto",
    r"task\b", r"objective", r"directive", r"plan\b", r"target",
    r"working.?memory",
)

#: Section-name shapes that mark RESOLVED trajectories — prime GC fuel.
RESOLVED_SECTION_PATTERNS = (
    r"previous", r"history", r"prior", r"postmortem", r"session",
    r"log\b", r"logs\b", r"attempt", r"retry", r"momentum",
    r"lessons", r"recent", r"digest",
)

_SECTION_SPLIT = re.compile(r"(?=^## )", re.MULTILINE)


def _matches(name: str, patterns: Tuple[str, ...]) -> bool:
    low = name.lower()
    return any(re.search(p, low) for p in patterns)


def _section_name(block: str) -> str:
    first = block.split("\n", 1)[0]
    return first.lstrip("# ").strip()


def _deterministic_digest(text: str, *, max_chars: int) -> str:
    """Zero-LLM fallback compression: leading summary slice + forensic
    stats. Deterministic; NEVER raises."""
    body = str(text)
    sha = hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()[:12]
    lines = body.count("\n") + 1
    head = body[: max(0, max_chars - 160)].rstrip()
    return (
        f"{head}\n[working-memory digest: compressed from "
        f"{len(body):,} chars / {lines:,} lines · sha256:{sha}]"
    )


async def _semantic_or_deterministic(text: str, *, max_chars: int) -> str:
    """Compression core — composes the existing ContextCompactor's
    semantic summarizer when available, deterministic digest otherwise.
    NEVER raises."""
    try:
        from backend.core.ouroboros.governance.context_compaction import (
            ContextCompactor,
        )
        compactor = ContextCompactor()
        build = getattr(compactor, "_build_semantic_or_fallback", None)
        if callable(build):
            entries = [{"content": text, "tool": "resolved_trajectory"}]
            deterministic = _deterministic_digest(text, max_chars=max_chars)
            summary = await build(entries, deterministic)
            if isinstance(summary, str) and summary.strip():
                return summary[:max_chars]
    except Exception:  # noqa: BLE001
        pass
    return _deterministic_digest(text, max_chars=max_chars)


async def prune_prompt_text(
    text: str,
    *,
    limit_tokens: int,
    ledger: Optional[TokenLedger] = None,
) -> Tuple[str, Dict[str, Any]]:
    """The CONTEXT_PRUNE gate for one assembled prompt.

    Under the threshold → byte-identical passthrough. Over it →
    ``##``-section-aware GC: RESOLVED sections compact (largest first)
    into ONE ``## Working Memory (compressed trajectories)`` block until
    the estimate is back under threshold; PROTECTED sections are never
    candidates. Unclassified sections compact only if the resolved set
    alone was insufficient (still never protected ones). Returns
    ``(pruned_text, telemetry)``. NEVER raises."""
    telemetry: Dict[str, Any] = {
        "schema_version": CONTEXT_PRUNE_SCHEMA_VERSION,
        "fired": False, "sections_compacted": 0,
        "tokens_before": 0, "tokens_after": 0, "limit": limit_tokens,
    }
    try:
        if not context_prune_enabled() or limit_tokens <= 0:
            return text, telemetry
        led = ledger or get_default_ledger()
        before = led.estimate_tokens(text)
        telemetry["tokens_before"] = telemetry["tokens_after"] = before
        budget = int(limit_tokens * prune_threshold())
        if before <= budget:
            return text, telemetry

        blocks = [b for b in _SECTION_SPLIT.split(text) if b]
        if len(blocks) <= 1:
            # No section structure — whole-text digest of the middle,
            # preserving head (identity) + tail (live task).
            keep = max(2_000, int(budget * led.chars_per_token * 0.45))
            head, tail = text[:keep], text[-keep:]
            middle = text[keep:-keep]
            digest = await _semantic_or_deterministic(
                middle, max_chars=4_000,
            )
            pruned = (
                f"{head}\n\n## Working Memory (compressed trajectories)\n"
                f"{digest}\n\n{tail}"
            )
            telemetry.update(
                fired=True, sections_compacted=1,
                tokens_after=led.estimate_tokens(pruned),
            )
            return pruned, telemetry

        classified = []
        for b in blocks:
            name = _section_name(b)
            if _matches(name, PROTECTED_SECTION_PATTERNS):
                cls = "protected"
            elif _matches(name, RESOLVED_SECTION_PATTERNS):
                cls = "resolved"
            else:
                cls = "other"
            classified.append([cls, name, b])

        digests: List[str] = []

        async def _compact_class(target_cls: str) -> None:
            nonlocal classified
            rows = sorted(
                (r for r in classified if r[0] == target_cls),
                key=lambda r: len(r[2]), reverse=True,
            )
            for row in rows:
                current = "".join(r[2] for r in classified)
                if led.estimate_tokens(current) <= budget:
                    return
                digest = await _semantic_or_deterministic(
                    row[2], max_chars=2_000,
                )
                digests.append(f"### {row[1]}\n{digest}")
                row[2] = ""
                row[0] = "compacted"
                telemetry["sections_compacted"] += 1

        await _compact_class("resolved")
        await _compact_class("other")          # protected NEVER visited

        body = "".join(r[2] for r in classified)
        if digests:
            body += (
                "\n\n## Working Memory (compressed trajectories)\n"
                + "\n\n".join(digests) + "\n"
            )
        telemetry["fired"] = telemetry["sections_compacted"] > 0
        telemetry["tokens_after"] = led.estimate_tokens(body)
        if telemetry["fired"]:
            logger.info(
                "[ContextPrune] semantic GC — tokens %d -> %d "
                "(budget=%d, sections=%d)",
                before, telemetry["tokens_after"], budget,
                telemetry["sections_compacted"],
            )
        return body, telemetry
    except Exception:  # noqa: BLE001 — degrade to the unpruned input
        logger.debug("[ContextPrune] gate degraded", exc_info=True)
        return text, telemetry


# ---------------------------------------------------------------------------
# 3b. Ballistic Payload Interceptor — instantaneous spike defense
# ---------------------------------------------------------------------------


def ballistic_char_budget(
    limit_tokens: int, ledger: Optional[TokenLedger] = None,
) -> int:
    """Max chars ONE payload may contribute before interception."""
    led = ledger or get_default_ledger()
    return max(4_000, int(
        limit_tokens * ballistic_max_fraction() * led.chars_per_token,
    ))


def ballistic_intercept(
    payload: str,
    *,
    limit_tokens: int,
    label: str = "payload",
    ledger: Optional[TokenLedger] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Pre-ingestion single-payload gate.

    A payload whose token estimate exceeds ``ballistic_max_fraction`` of
    the active window is isolated BEFORE merging into FSM state:
    head + tail slices survive (context at both boundaries) joined by a
    deterministic forensic digest (sizes, line count, sha256) so the
    model knows exactly what was withheld and can re-read surgically.
    Returns ``(safe_payload, telemetry)``. NEVER raises."""
    telemetry: Dict[str, Any] = {
        "schema_version": CONTEXT_PRUNE_SCHEMA_VERSION,
        "intercepted": False, "label": label,
        "chars_before": 0, "chars_after": 0, "limit": limit_tokens,
    }
    try:
        body = str(payload or "")
        telemetry["chars_before"] = telemetry["chars_after"] = len(body)
        if not context_prune_enabled() or limit_tokens <= 0:
            return payload, telemetry
        led = ledger or get_default_ledger()
        budget_chars = ballistic_char_budget(limit_tokens, led)
        if len(body) <= budget_chars:
            return payload, telemetry

        head_n = int(budget_chars * 0.6)
        tail_n = max(0, budget_chars - head_n - 300)
        sha = hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()[:12]
        omitted = len(body) - head_n - tail_n
        safe = (
            body[:head_n]
            + (
                f"\n\n[ballistic-intercept: {label} was "
                f"{len(body):,} chars / ~{led.estimate_tokens(body):,} "
                f"tokens ({body.count(chr(10)) + 1:,} lines) — "
                f"{omitted:,} chars withheld to protect the "
                f"{limit_tokens:,}-token window · sha256:{sha} · "
                f"re-read narrower ranges if the elided region matters]"
                "\n\n"
            )
            + (body[-tail_n:] if tail_n > 0 else "")
        )
        telemetry.update(intercepted=True, chars_after=len(safe))
        logger.info(
            "[ContextPrune] BALLISTIC intercept — %s: %d chars "
            "(~%d tokens) -> %d chars (budget=%d chars, window=%d tokens)",
            label, len(body), led.estimate_tokens(body), len(safe),
            budget_chars, limit_tokens,
        )
        return safe, telemetry
    except Exception:  # noqa: BLE001
        logger.debug("[ContextPrune] ballistic degraded", exc_info=True)
        return payload, telemetry


__all__ = [
    "CONTEXT_PRUNE_SCHEMA_VERSION",
    "PROTECTED_SECTION_PATTERNS",
    "RESOLVED_SECTION_PATTERNS",
    "TokenLedger",
    "ballistic_char_budget",
    "ballistic_intercept",
    "ballistic_max_fraction",
    "context_prune_enabled",
    "get_default_ledger",
    "prune_prompt_text",
    "prune_threshold",
    "reset_default_ledger",
    "resolve_context_limit",
]
