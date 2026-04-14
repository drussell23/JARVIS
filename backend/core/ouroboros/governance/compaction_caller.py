"""
CompactionCaller — Functions-not-Agents Phase 0
===============================================

First production caller of :meth:`DoublewordProvider.complete_sync` — the
non-streaming path established after bt-2026-04-14-182446 and
bt-2026-04-14-203740 proved DoubleWord's SSE streaming endpoint stalls
post-accept across both Qwen 397B and Gemma 4 31B.

Design
------
The compaction task is an ideal Gemma test bed because it is structurally
**bounded**:

1. Input is already in memory (entries to compact, never more than a few
   dozen dicts).
2. Output is short (<1KB summary text).
3. The anti-hallucination check is trivial — every referenced entry key or
   phase name in the model's output must be a member of the input entry set.
4. A deterministic fallback already exists in ``context_compaction.py`` so
   any failure (timeout, schema_invalid, hallucinated ref, circuit-open) is
   gracefully recoverable.

Operating modes
---------------
- ``disabled``: The caller is inert. ``summarize()`` returns ``None`` without
  any network call. Default.
- ``shadow``: Runs in parallel with the deterministic summarizer. Writes
  comparison telemetry to a JSONL file and pushes a one-line event to the
  SerpentFlow TUI. **Still returns None** — deterministic path owns pipeline
  state. This is the observation-only mode used during initial promotion.
- ``live``: Returns the model-generated summary on success. On any failure
  (timeout, anti-hallucination rejection, circuit-open), returns ``None``
  and the caller falls back to the deterministic path.

Master switch: ``JARVIS_COMPACTION_CALLER_ENABLED`` (default ``false``).
Mode: ``JARVIS_COMPACTION_CALLER_MODE`` (default ``shadow``).

Anti-Hallucination Gate (Manifesto §6)
---------------------------------------
The model output MUST be a JSON object of the form::

    {
      "summary": "<short free-form summary>",
      "referenced_keys": ["<key1>", "<key2>", ...],
      "referenced_phases": ["<phase1>", ...]
    }

Every ``referenced_keys[i]`` must be a member of the set of entry keys
extracted from the *input* compactable entries. Every ``referenced_phases[i]``
must be a member of the input phase set. Any hallucinated reference causes
the output to be rejected — the circuit increments, the telemetry logs the
rejection reason, and the caller returns None so the deterministic path
takes over.

Circuit Breaker
---------------
Three layers:

- **per-call**: ``asyncio.wait_for`` enforces the caller-supplied timeout
  (``complete_sync`` raises ``asyncio.TimeoutError`` on expiry).
- **per-session**: After ``_MAX_SESSION_FAILURES`` (default 3) consecutive
  failures, the breaker opens for the rest of the session. ``summarize()``
  returns None immediately without any network call. Failures are
  counted from the *module-level* ``_SESSION_STATE`` so the breaker
  survives strategy re-instantiation within one process.
- **global**: ``JARVIS_COMPACTION_CALLER_ENABLED=false`` kills the feature
  entirely for the process lifetime.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.CompactionCaller")

# ---------------------------------------------------------------------------
# Environment-driven configuration
# ---------------------------------------------------------------------------

_ENABLED_ENV = "JARVIS_COMPACTION_CALLER_ENABLED"
_MODE_ENV = "JARVIS_COMPACTION_CALLER_MODE"
_TIMEOUT_ENV = "JARVIS_COMPACTION_CALLER_TIMEOUT_S"
_MAX_TOKENS_ENV = "JARVIS_COMPACTION_CALLER_MAX_TOKENS"
_MAX_FAILURES_ENV = "JARVIS_COMPACTION_CALLER_MAX_FAILURES"

_CALLER_ID = "compaction"

_MODE_DISABLED = "disabled"
_MODE_SHADOW = "shadow"
_MODE_LIVE = "live"
_VALID_MODES = {_MODE_DISABLED, _MODE_SHADOW, _MODE_LIVE}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("[CompactionCaller] %s=%r is not a float, using %.2f", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("[CompactionCaller] %s=%r is not an int, using %d", name, raw, default)
        return default


@dataclass(frozen=True)
class CompactionCallerConfig:
    """Configuration for the compaction caller strategy.

    Loaded from env vars via :meth:`from_env`. Fields are frozen so a config
    captured at init time does not drift mid-session.
    """

    enabled: bool = False
    mode: str = _MODE_SHADOW
    timeout_s: float = 2.0
    max_tokens: int = 512
    max_session_failures: int = 3

    @classmethod
    def from_env(cls) -> CompactionCallerConfig:
        enabled = _env_bool(_ENABLED_ENV, False)
        mode_raw = os.environ.get(_MODE_ENV, _MODE_SHADOW).strip().lower()
        if mode_raw not in _VALID_MODES:
            logger.warning(
                "[CompactionCaller] %s=%r invalid — falling back to shadow",
                _MODE_ENV, mode_raw,
            )
            mode_raw = _MODE_SHADOW
        if not enabled:
            mode_raw = _MODE_DISABLED
        return cls(
            enabled=enabled,
            mode=mode_raw,
            timeout_s=_env_float(_TIMEOUT_ENV, 2.0),
            max_tokens=_env_int(_MAX_TOKENS_ENV, 512),
            max_session_failures=_env_int(_MAX_FAILURES_ENV, 3),
        )


@dataclass
class _SessionState:
    """Module-level session state for the circuit breaker."""

    consecutive_failures: int = 0
    breaker_open: bool = False
    last_open_reason: str = ""


_SESSION_STATE = _SessionState()


def reset_session_state() -> None:
    """Reset the session-level circuit breaker. Used by tests and session boot."""
    _SESSION_STATE.consecutive_failures = 0
    _SESSION_STATE.breaker_open = False
    _SESSION_STATE.last_open_reason = ""


# ---------------------------------------------------------------------------
# Result + telemetry record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompactionCallerResult:
    """Outcome of a single compaction caller invocation.

    In shadow mode ``summary`` is always ``None`` (pipeline state is owned
    by the deterministic path). In live mode ``summary`` carries the
    validated model summary when ``accepted=True``.
    """

    accepted: bool
    summary: Optional[str]
    rejection_reason: Optional[str]
    latency_s: float
    input_tokens: int
    output_tokens: int
    cost_usd: float
    model: str


@dataclass
class _TelemetryRecord:
    timestamp: float
    mode: str
    entries_in: int
    accepted: bool
    rejection_reason: Optional[str]
    latency_s: float
    cost_usd: float
    input_tokens: int
    output_tokens: int
    model: str
    deterministic_summary: str
    semantic_summary: Optional[str]

    def to_jsonl_line(self) -> str:
        return json.dumps(
            {
                "ts": round(self.timestamp, 3),
                "caller": _CALLER_ID,
                "mode": self.mode,
                "entries_in": self.entries_in,
                "accepted": self.accepted,
                "rejection_reason": self.rejection_reason,
                "latency_s": round(self.latency_s, 3),
                "cost_usd": round(self.cost_usd, 6),
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "model": self.model,
                "deterministic_summary": self.deterministic_summary,
                "semantic_summary": self.semantic_summary,
            },
            ensure_ascii=True,
        )


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT = (
    "You compress lists of dialogue metadata into a single short summary. "
    "You MUST return ONLY a JSON object. No prose, no markdown, no code fences. "
    "Schema: "
    "{\"summary\": <string, <=400 chars>, "
    "\"referenced_keys\": [<string>, ...], "
    "\"referenced_phases\": [<string>, ...]}. "
    "CRITICAL: every string in referenced_keys MUST appear verbatim in the "
    "input 'entries' list's 'key' fields. Every string in referenced_phases "
    "MUST appear verbatim in the input 'entries' list's 'phase' fields. "
    "If you cannot cite a key or phase from the input, do not invent one — "
    "leave the array empty for that field. Summaries citing invented "
    "identifiers will be rejected."
)


class CompactionCallerStrategy:
    """Non-streaming Gemma caller for context compaction summaries.

    Intended to be injected into :class:`ContextCompactor` via its
    ``semantic_strategy`` constructor argument. The compactor calls
    :meth:`summarize` with the list of compactable entries. In shadow mode
    the call is observation-only and the compactor ignores the return
    value. In live mode the compactor uses the returned summary in place
    of the deterministic one.
    """

    def __init__(
        self,
        provider: Any,
        *,
        config: Optional[CompactionCallerConfig] = None,
        session_dir: Optional[Path] = None,
        flow: Any = None,
    ) -> None:
        """
        Parameters
        ----------
        provider:
            A ``DoublewordProvider`` instance exposing ``complete_sync``.
        config:
            Optional explicit config. Defaults to ``CompactionCallerConfig.from_env``.
        session_dir:
            Directory where the shadow telemetry JSONL is written. When
            None, telemetry is emitted to the logger only.
        flow:
            Optional SerpentFlow instance. When present, accepted/rejected
            events are pushed as one-line info alerts.
        """
        self._provider = provider
        self._config: CompactionCallerConfig = config or CompactionCallerConfig.from_env()
        self._session_dir: Optional[Path] = session_dir
        self._flow = flow
        self._model: Optional[str] = self._resolve_model()
        self._jsonl_path: Optional[Path] = None
        if self._session_dir is not None:
            try:
                self._session_dir.mkdir(parents=True, exist_ok=True)
                self._jsonl_path = self._session_dir / "compaction_shadow.jsonl"
            except OSError as exc:
                logger.warning(
                    "[CompactionCaller] cannot create session_dir %s: %s",
                    self._session_dir, exc,
                )
        if self._config.enabled:
            logger.info(
                "[CompactionCaller] init enabled=%s mode=%s model=%s timeout=%.1fs jsonl=%s",
                self._config.enabled,
                self._config.mode,
                self._model or "<unresolved>",
                self._config.timeout_s,
                self._jsonl_path or "<none>",
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._config.enabled and self._config.mode != _MODE_DISABLED

    @property
    def mode(self) -> str:
        return self._config.mode

    async def summarize(
        self,
        entries: List[Dict[str, Any]],
        deterministic_summary: str,
    ) -> CompactionCallerResult:
        """Produce a semantic summary of *entries*.

        In shadow mode, ``result.summary`` is always ``None`` regardless of
        acceptance — the deterministic summary owns pipeline state. The
        acceptance/rejection still flows into telemetry and the circuit
        breaker counters so we learn whether the model would have been
        correct if we had trusted it.

        In live mode, ``result.summary`` carries the model summary when
        ``result.accepted=True``, and ``None`` otherwise.

        Never raises. All failure modes are caught and returned as rejected
        results with a ``rejection_reason``.
        """
        t0 = time.monotonic()

        if not self.enabled:
            return _inert_result("disabled", time.monotonic() - t0)

        if _SESSION_STATE.breaker_open:
            logger.debug(
                "[CompactionCaller] breaker_open — skipping (reason=%s)",
                _SESSION_STATE.last_open_reason,
            )
            return _inert_result("breaker_open", time.monotonic() - t0)

        if not entries:
            return _inert_result("no_entries", time.monotonic() - t0)

        if self._model is None:
            self._model = self._resolve_model()
            if self._model is None:
                return _inert_result("no_model_in_topology", time.monotonic() - t0)

        # --- Build prompt ---
        input_keys, input_phases, prompt = self._build_prompt(entries)

        # --- Invoke provider ---
        try:
            response = await self._provider.complete_sync(
                prompt=prompt,
                system_prompt=_SYSTEM_PROMPT,
                caller_id=_CALLER_ID,
                model=self._model,
                max_tokens=self._config.max_tokens,
                timeout_s=self._config.timeout_s,
                response_format={"type": "json_object"},
            )
        except asyncio.TimeoutError:
            result = _failure_result("timeout", time.monotonic() - t0, self._model or "")
            self._record_failure(result, entries, deterministic_summary)
            return result
        except Exception as exc:
            result = _failure_result(
                f"provider_error:{type(exc).__name__}",
                time.monotonic() - t0,
                self._model or "",
            )
            self._record_failure(result, entries, deterministic_summary)
            return result

        # --- Parse + validate ---
        raw_content = getattr(response, "content", "") or ""
        parse_result = _parse_and_validate(
            raw_content=raw_content,
            input_keys=input_keys,
            input_phases=input_phases,
        )
        if not parse_result.ok:
            result = CompactionCallerResult(
                accepted=False,
                summary=None,
                rejection_reason=parse_result.reason,
                latency_s=response.latency_s,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cost_usd=response.cost_usd,
                model=response.model,
            )
            self._record_failure(result, entries, deterministic_summary)
            return result

        # --- Success ---
        _SESSION_STATE.consecutive_failures = 0

        visible_summary = parse_result.summary
        result = CompactionCallerResult(
            accepted=True,
            summary=visible_summary if self._config.mode == _MODE_LIVE else None,
            rejection_reason=None,
            latency_s=response.latency_s,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_usd=response.cost_usd,
            model=response.model,
        )
        self._record_success(
            result,
            entries,
            deterministic_summary,
            semantic_summary=visible_summary,
        )
        return result

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _resolve_model(self) -> Optional[str]:
        try:
            from backend.core.ouroboros.governance.provider_topology import (
                get_topology,
            )
            return get_topology().model_for_caller(_CALLER_ID)
        except Exception:
            return None

    def _build_prompt(
        self,
        entries: List[Dict[str, Any]],
    ) -> Tuple[set, set, str]:
        """Serialize entry metadata for the model and return (keys, phases, prompt)."""
        input_keys: set = set()
        input_phases: set = set()
        compact_entries: List[Dict[str, Any]] = []
        for entry in entries:
            key = _entry_key(entry)
            input_keys.add(key)
            phase = str(entry.get("phase") or "").strip()
            if phase:
                input_phases.add(phase)
            compact_entries.append(
                {
                    "key": key,
                    "phase": phase,
                    "type": str(entry.get("type") or entry.get("role") or "unknown"),
                }
            )

        payload = {
            "task": "compact_dialogue_metadata",
            "entries": compact_entries,
        }
        prompt = json.dumps(payload, ensure_ascii=True)
        return input_keys, input_phases, prompt

    def _record_failure(
        self,
        result: CompactionCallerResult,
        entries: List[Dict[str, Any]],
        deterministic_summary: str,
    ) -> None:
        _SESSION_STATE.consecutive_failures += 1
        if _SESSION_STATE.consecutive_failures >= self._config.max_session_failures:
            _SESSION_STATE.breaker_open = True
            _SESSION_STATE.last_open_reason = result.rejection_reason or "unknown"
            logger.warning(
                "[CompactionCaller] circuit_breaker OPEN after %d failures (reason=%s)",
                _SESSION_STATE.consecutive_failures,
                _SESSION_STATE.last_open_reason,
            )
        self._emit_telemetry(
            result=result,
            entries=entries,
            deterministic_summary=deterministic_summary,
            semantic_summary=None,
        )

    def _record_success(
        self,
        result: CompactionCallerResult,
        entries: List[Dict[str, Any]],
        deterministic_summary: str,
        semantic_summary: Optional[str],
    ) -> None:
        self._emit_telemetry(
            result=result,
            entries=entries,
            deterministic_summary=deterministic_summary,
            semantic_summary=semantic_summary,
        )

    def _emit_telemetry(
        self,
        *,
        result: CompactionCallerResult,
        entries: List[Dict[str, Any]],
        deterministic_summary: str,
        semantic_summary: Optional[str],
    ) -> None:
        record = _TelemetryRecord(
            timestamp=time.time(),
            mode=self._config.mode,
            entries_in=len(entries),
            accepted=result.accepted,
            rejection_reason=result.rejection_reason,
            latency_s=result.latency_s,
            cost_usd=result.cost_usd,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            model=result.model,
            deterministic_summary=deterministic_summary,
            semantic_summary=semantic_summary,
        )

        if self._jsonl_path is not None:
            try:
                with self._jsonl_path.open("a", encoding="utf-8") as fh:
                    fh.write(record.to_jsonl_line() + "\n")
            except OSError as exc:
                logger.warning(
                    "[CompactionCaller] jsonl write failed (%s): %s",
                    self._jsonl_path, exc,
                )

        verdict = "accepted" if result.accepted else f"rejected:{result.rejection_reason}"
        logger.info(
            "[CompactionCaller] telemetry mode=%s entries=%d verdict=%s latency=%.2fs cost=$%.5f",
            self._config.mode,
            len(entries),
            verdict,
            result.latency_s,
            result.cost_usd,
        )

        if self._flow is not None:
            try:
                icon = "OK" if result.accepted else "REJ"
                title = f"compaction[{self._config.mode}]"
                body = (
                    f"{icon} {verdict} entries={len(entries)} "
                    f"latency={result.latency_s:.2f}s cost=${result.cost_usd:.5f}"
                )
                self._flow.emit_proactive_alert(
                    title=title,
                    body=body,
                    severity="info",
                    source="CompactionCaller",
                )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Pure helpers (tested in isolation)
# ---------------------------------------------------------------------------


@dataclass
class _ParseResult:
    ok: bool
    summary: str = ""
    reason: Optional[str] = None


def _parse_and_validate(
    *,
    raw_content: str,
    input_keys: set,
    input_phases: set,
) -> _ParseResult:
    """Parse the model's JSON output and run the anti-hallucination gate.

    The contract: every string in ``referenced_keys`` must be in ``input_keys``,
    and every string in ``referenced_phases`` must be in ``input_phases``.
    Any violation → rejection with a specific reason.
    """
    if not raw_content.strip():
        return _ParseResult(ok=False, reason="empty_content")

    try:
        obj = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        return _ParseResult(ok=False, reason=f"json_decode:{exc.msg[:40]}")

    if not isinstance(obj, dict):
        return _ParseResult(ok=False, reason="not_an_object")

    summary = obj.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return _ParseResult(ok=False, reason="missing_summary")
    if len(summary) > 800:
        return _ParseResult(ok=False, reason="summary_too_long")

    referenced_keys = obj.get("referenced_keys", [])
    if not isinstance(referenced_keys, list):
        return _ParseResult(ok=False, reason="referenced_keys_not_list")
    for k in referenced_keys:
        if not isinstance(k, str):
            return _ParseResult(ok=False, reason="referenced_keys_non_string")
        if k not in input_keys:
            return _ParseResult(ok=False, reason=f"hallucinated_key:{k[:40]}")

    referenced_phases = obj.get("referenced_phases", [])
    if not isinstance(referenced_phases, list):
        return _ParseResult(ok=False, reason="referenced_phases_not_list")
    for p in referenced_phases:
        if not isinstance(p, str):
            return _ParseResult(ok=False, reason="referenced_phases_non_string")
        if p not in input_phases:
            return _ParseResult(ok=False, reason=f"hallucinated_phase:{p[:40]}")

    return _ParseResult(ok=True, summary=summary.strip())


def _entry_key(entry: Dict[str, Any]) -> str:
    """Match ContextCompactor._entry_key for consistent reference strings."""
    for key in ("op_id", "phase", "type", "role", "id"):
        val = entry.get(key)
        if val is not None:
            return f"{key}={val}"
    return f"idx={id(entry)}"


def _inert_result(reason: str, latency_s: float) -> CompactionCallerResult:
    return CompactionCallerResult(
        accepted=False,
        summary=None,
        rejection_reason=reason,
        latency_s=latency_s,
        input_tokens=0,
        output_tokens=0,
        cost_usd=0.0,
        model="",
    )


def _failure_result(reason: str, latency_s: float, model: str) -> CompactionCallerResult:
    return CompactionCallerResult(
        accepted=False,
        summary=None,
        rejection_reason=reason,
        latency_s=latency_s,
        input_tokens=0,
        output_tokens=0,
        cost_usd=0.0,
        model=model,
    )


__all__ = [
    "CompactionCallerConfig",
    "CompactionCallerResult",
    "CompactionCallerStrategy",
    "reset_session_state",
]
