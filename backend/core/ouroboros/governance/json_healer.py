"""Slice 20B — Asynchronous JSON Healing via Qwen3.5-35B repair fallback.

# What this closes

v15 soak ``bt-2026-05-26-184355`` exposed DW 397B occasionally emitting
malformed JSON candidate payloads (``JSONDecodeError`` at L7:C6408 on a
14 KB body). The existing ``providers._repair_json()`` (regex-only sweep
covering trailing commas, control chars, escaped newlines, single→double
quotes, unquoted keys, and unbalanced containers) is the deterministic
first line of defense and stays the load-bearing fast path. When that
also fails, we currently raise ``json_parse_error`` and lose the op.

Slice 20B adds a **second, last-resort, zero-governance** repair attempt
that fires AFTER ``_repair_json()`` exhausts its deterministic patterns:
a single-shot call to ``Qwen/Qwen3.5-35B-A3B-FP8`` (the workhorse from
§46 fleet inventory) with an immutable system prompt asking for syntax-
only repair while preserving the exact semantic code modifications.

# Architectural discipline

* **No provider import**: this module accepts a `heal_call` callable
  that the caller injects. The caller passes ``DoublewordProvider.prompt_only``
  bound, but this module has zero coupling to providers.py — keeps the
  test surface fast (no DW init) and keeps composition direction acyclic.
* **Zero-governance**: the injected call MUST be ``DoublewordProvider.prompt_only``
  which already bypasses ``OperationContext``, ``UrgencyRouter``, Venom
  tool loop, batch governance — exactly what's needed for a sub-second
  syntax-repair fast path (audit Surface 2: ``doubleword_provider.py:2895``).
* **Hard bounds**: timeout via ``asyncio.wait_for``, token cap, output
  validation (the healer's output is re-parsed; if it's still malformed
  we return None and the original parse error propagates).
* **Hard-fail-silent on infra**: any exception inside the heal path
  returns None — the heal is an enhancement, not a correctness
  dependency. Caller's existing ``json_parse_error`` raise remains the
  authoritative failure mode when heal cannot recover.
* **Master flag default-FALSE**: ``JARVIS_JSON_HEAL_LLM_ENABLED`` opts
  in. Graduate after a v16+ soak proves at least one healed candidate
  flowed APPLY → VERIFY → RESOLVED on pure-DW.
* **Audit ledger**: every heal attempt appends a row to
  ``.jarvis/json_heal_audit.jsonl`` for forensic verification — at
  graduation time we can prove the healer fixed real malformations vs.
  just adding cost.

# Immutable system prompt

The operator-specified system prompt is treated as a constant string
(``_HEAL_SYSTEM_PROMPT``) and is AST-pinned so future edits don't
silently weaken the heal contract. The user message is the raw
malformed payload, nothing else — minimum cognitive surface.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────

#: Operator-attested immutable system prompt for the heal call.
#: AST-pinned: this exact string must remain in source so the healer's
#: contract cannot silently drift. Future copy edits go through their
#: own slice + soak validation.
_HEAL_SYSTEM_PROMPT = (
    "Repair the syntax of this malformed JSON patch block. "
    "Output ONLY valid JSON. "
    "Preserve the exact semantic code modifications."
)

#: Env var names — single source of truth for the gate surface.
_ENV_MASTER = "JARVIS_JSON_HEAL_LLM_ENABLED"
_ENV_MODEL = "JARVIS_JSON_HEAL_MODEL"
_ENV_TIMEOUT_S = "JARVIS_JSON_HEAL_TIMEOUT_S"
_ENV_MAX_TOKENS = "JARVIS_JSON_HEAL_MAX_TOKENS"
_ENV_AUDIT_PATH = "JARVIS_JSON_HEAL_AUDIT_PATH"

#: Hardware-conservative defaults sized for §47.5 16GB M1 envelope.
_DEFAULT_HEAL_MODEL = "Qwen/Qwen3.5-35B-A3B-FP8"
_DEFAULT_TIMEOUT_S = 30.0
_DEFAULT_MAX_TOKENS = 8192
_DEFAULT_AUDIT_PATH = ".jarvis/json_heal_audit.jsonl"

#: Hard upper-bound on input payload — heal call costs O(input_tokens),
#: and a payload larger than this is almost certainly a structural
#: rather than syntactical failure; skip the LLM and let the original
#: error propagate.
_MAX_INPUT_BYTES = 64 * 1024  # 64 KiB

#: Markdown code-fence detection — Qwen often wraps JSON in
#: ``` ```json ... ``` ``` despite the "Output ONLY valid JSON" prompt;
#: we strip these before re-parsing.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


# ──────────────────────────────────────────────────────────────────────
# Types
# ──────────────────────────────────────────────────────────────────────


class HealCall(Protocol):
    """The callable contract the caller injects.

    Matches ``DoublewordProvider.prompt_only`` (audit Surface 2).
    Returning ``""`` on failure is the existing prompt_only contract —
    healer treats empty string as heal-failed.
    """

    async def __call__(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        caller_id: str = "json_healer",
        response_format: Optional[dict] = None,
        max_tokens: Optional[int] = None,
    ) -> str: ...


@dataclass(frozen=True)
class HealOutcome:
    """Frozen result of one heal attempt — what landed in the audit ledger.

    `repaired_text` is None when the heal failed for any reason
    (master-off / disabled / oversized input / timeout / LLM returned
    non-JSON / infra exception). Callers MUST check `repaired_text is
    not None` before using it.
    """

    op_id: str
    provider_name: str
    input_len: int
    success: bool
    repaired_text: Optional[str]
    duration_s: float
    failure_reason: Optional[str]


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _envb(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("true", "1", "yes", "on")


def _envs(name: str, default: str) -> str:
    raw = os.environ.get(name, "").strip()
    return raw if raw else default


def _envf(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _envi(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _strip_markdown_fence(text: str) -> str:
    """Strip ``` ... ``` wrappers from healer output.

    Qwen3.5-35B sometimes returns wrapped JSON despite the "Output ONLY
    valid JSON" prompt; this is a known pattern across Qwen lineage. We
    accept both ```json``` and bare ``` ``` fences. Returns the inner
    payload, or the original text if no fence is detected.
    """
    if "```" not in text:
        return text
    m = _FENCE_RE.match(text.strip())
    if m:
        return m.group(1).strip()
    return text


def _audit_append(outcome: HealOutcome) -> None:
    """Append one heal attempt to the audit ledger. Hard-fail-silent.

    The ledger is forensic-only — failure to write does NOT propagate.
    Path is configurable so test runs can isolate to a tmpdir.
    """
    path_str = _envs(_ENV_AUDIT_PATH, _DEFAULT_AUDIT_PATH)
    try:
        path = Path(path_str)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": time.time(),
            "op_id": outcome.op_id,
            "provider_name": outcome.provider_name,
            "input_len": outcome.input_len,
            "success": outcome.success,
            "duration_s": round(outcome.duration_s, 3),
            "failure_reason": outcome.failure_reason,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
    except Exception:  # noqa: BLE001 — audit MUST NOT raise
        pass


def _validate_repaired(text: str) -> Optional[str]:
    """Try to parse the repaired text. Returns the validated string or None.

    The healer's output earns trust ONLY when it round-trips through
    ``json.loads``. The reparser at the call site will do its own parse
    but we double-check here so the audit ledger records a clean
    success/failure verdict.
    """
    if not text:
        return None
    stripped = _strip_markdown_fence(text)
    if not stripped:
        return None
    try:
        json.loads(stripped)
        return stripped
    except (json.JSONDecodeError, ValueError):
        return None


# ──────────────────────────────────────────────────────────────────────
# Public entry point
# ──────────────────────────────────────────────────────────────────────


def is_llm_heal_enabled() -> bool:
    """Master gate — read at every call so toggles take effect immediately."""
    return _envb(_ENV_MASTER, default=False)


async def heal_json_with_llm(
    malformed_text: str,
    *,
    heal_call: HealCall,
    op_id: str = "",
    provider_name: str = "",
) -> HealOutcome:
    """Last-resort LLM-driven JSON syntax repair.

    Fired ONLY after the deterministic ``_repair_json()`` regex sweep
    has exhausted its patterns. Calls the injected ``heal_call`` (typically
    ``DoublewordProvider.prompt_only``) with the immutable repair system
    prompt and hard-bounded timeout/token cap.

    The contract:

    * Returns a ``HealOutcome`` always (never raises) — caller checks
      ``outcome.repaired_text is not None`` before using.
    * Master gate ``JARVIS_JSON_HEAL_LLM_ENABLED=false`` short-circuits
      with ``failure_reason="master_off"`` and no LLM call. Zero cost.
    * Oversized input (> 64 KiB) short-circuits with
      ``failure_reason="oversized_input"`` — semantic failure, not
      syntactic; LLM repair is the wrong tool.
    * Timeout / heal_call raise / non-JSON output → ``success=False``,
      ``repaired_text=None``, ``failure_reason`` populated.
    * Every attempt — including short-circuits — writes one audit row.
    """
    start = time.perf_counter()
    input_len = len(malformed_text or "")

    # Gate 1: master flag
    if not is_llm_heal_enabled():
        outcome = HealOutcome(
            op_id=op_id, provider_name=provider_name,
            input_len=input_len, success=False, repaired_text=None,
            duration_s=time.perf_counter() - start,
            failure_reason="master_off",
        )
        _audit_append(outcome)
        return outcome

    # Gate 2: oversized input — almost certainly structural, not syntactic
    if input_len > _MAX_INPUT_BYTES:
        outcome = HealOutcome(
            op_id=op_id, provider_name=provider_name,
            input_len=input_len, success=False, repaired_text=None,
            duration_s=time.perf_counter() - start,
            failure_reason=f"oversized_input:{input_len}>{_MAX_INPUT_BYTES}",
        )
        _audit_append(outcome)
        return outcome

    # Gate 3: empty input — nothing to heal
    if not malformed_text or not malformed_text.strip():
        outcome = HealOutcome(
            op_id=op_id, provider_name=provider_name,
            input_len=input_len, success=False, repaired_text=None,
            duration_s=time.perf_counter() - start,
            failure_reason="empty_input",
        )
        _audit_append(outcome)
        return outcome

    # All gates passed — issue the heal call.
    model = _envs(_ENV_MODEL, _DEFAULT_HEAL_MODEL)
    timeout_s = _envf(_ENV_TIMEOUT_S, _DEFAULT_TIMEOUT_S)
    max_tokens = _envi(_ENV_MAX_TOKENS, _DEFAULT_MAX_TOKENS)

    # The user message is the immutable system prompt + the malformed
    # payload. We concatenate explicitly because ``prompt_only`` takes
    # a single ``prompt`` parameter and applies a default system message
    # internally — putting our repair contract at the head of the user
    # prompt guarantees it sticks.
    user_message = (
        f"{_HEAL_SYSTEM_PROMPT}\n\n"
        f"---\n"
        f"MALFORMED JSON PAYLOAD:\n"
        f"{malformed_text}"
    )

    raw_response: str = ""
    failure_reason: Optional[str] = None

    try:
        raw_response = await asyncio.wait_for(
            heal_call(
                prompt=user_message,
                model=model,
                caller_id="json_healer",
                max_tokens=max_tokens,
            ),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        failure_reason = f"timeout:{timeout_s}s"
    except Exception as exc:  # noqa: BLE001 — heal MUST NOT raise
        failure_reason = f"heal_call_raised:{type(exc).__name__}"
        logger.debug(
            "[json_healer] op=%s heal_call raised %s: %s",
            op_id, type(exc).__name__, exc,
        )

    repaired = _validate_repaired(raw_response) if not failure_reason else None
    if not failure_reason and repaired is None:
        # Heal call returned, but output didn't parse cleanly
        failure_reason = (
            "empty_response" if not raw_response.strip()
            else "output_not_valid_json"
        )

    outcome = HealOutcome(
        op_id=op_id,
        provider_name=provider_name,
        input_len=input_len,
        success=repaired is not None,
        repaired_text=repaired,
        duration_s=time.perf_counter() - start,
        failure_reason=failure_reason,
    )
    _audit_append(outcome)

    if outcome.success:
        logger.info(
            "[json_healer] op=%s provider=%s input_len=%d duration=%.2fs "
            "→ healed (%d bytes valid JSON)",
            op_id, provider_name, input_len, outcome.duration_s,
            len(repaired or ""),
        )
    else:
        logger.info(
            "[json_healer] op=%s provider=%s input_len=%d duration=%.2fs "
            "→ heal failed: %s",
            op_id, provider_name, input_len, outcome.duration_s,
            outcome.failure_reason,
        )

    return outcome


# ──────────────────────────────────────────────────────────────────────
# Parse+heal+retry composition helper (used by DW provider call sites)
# ──────────────────────────────────────────────────────────────────────


async def heal_and_retry_parse(
    *,
    raw: str,
    parse_fn,
    heal_call: HealCall,
    op_id: str = "",
    provider_name: str = "",
    model_id: str = "",
):
    """Compose ``parse_fn(raw)`` with an LLM-heal retry on json_parse_error.

    Behavior:

    1. Call ``parse_fn(raw)`` (sync). On success, return the result.
    2. On ``RuntimeError`` whose message contains ``json_parse_error``,
       attempt to heal ``raw`` via :func:`heal_json_with_llm` (gated by
       ``JARVIS_JSON_HEAL_LLM_ENABLED``).
    3. If heal succeeds, call ``parse_fn(healed)``. Return its result.
    4. If heal returns nothing OR the retry parse also raises, **record
       a Slice 20C drift event** (model_id has produced unrepairable
       JSON on this op_id; the dispatcher should rotate to a sibling
       on the next attempt) AND re-raise the ORIGINAL parse error —
       never the heal-failure error (heal is an enhancement, not a
       correctness path; preserve the existing diagnostic chain for
       callers / postmortems).
    5. Any non-``json_parse_error`` RuntimeError propagates unchanged.

    The caller is responsible for binding ``parse_fn`` to a single-arg
    closure that injects whatever ctx / kwargs the underlying parser
    needs — keeps this helper agnostic of the parser signature.

    Drift recording is **gated separately** by
    ``JARVIS_SCHEMA_DRIFT_ROTATION_ENABLED`` (the tracker's
    ``has_drifted()`` consultation short-circuits to False when off,
    so recording in a master-off world is harmless but pointless;
    callers may pass ``model_id=""`` to skip the record explicitly).
    """
    try:
        return parse_fn(raw)
    except RuntimeError as parse_exc:
        if "json_parse_error" not in str(parse_exc):
            raise
        # If LLM heal is off, the parse error is final — but still
        # record drift so Slice 20C rotation has a signal on the next
        # retry (the regex-only repair already exhausted; same model
        # will likely fail again).
        if not is_llm_heal_enabled():
            _record_drift_after_parse_failure(
                op_id=op_id,
                model_id=model_id,
                raw_excerpt=str(parse_exc)[:200],
            )
            raise
        # Heal attempt — bounded by env timeout, never raises
        outcome = await heal_json_with_llm(
            raw,
            heal_call=heal_call,
            op_id=op_id,
            provider_name=provider_name,
        )
        if outcome.repaired_text is None:
            _record_drift_after_parse_failure(
                op_id=op_id,
                model_id=model_id,
                raw_excerpt=str(parse_exc)[:200],
            )
            raise parse_exc
        try:
            return parse_fn(outcome.repaired_text)
        except RuntimeError as retry_exc:
            # Retry parse failed — record drift + preserve the
            # ORIGINAL error so the caller's diagnostic chain (which
            # already includes a ``_log_parse_failure`` call) stays
            # consistent.
            logger.warning(
                "[json_healer] op=%s heal returned valid JSON but "
                "second parse still raised: %s — propagating original",
                op_id, retry_exc,
            )
            _record_drift_after_parse_failure(
                op_id=op_id,
                model_id=model_id,
                raw_excerpt=str(parse_exc)[:200],
            )
            raise parse_exc from retry_exc


def _record_drift_after_parse_failure(
    *,
    op_id: str,
    model_id: str,
    raw_excerpt: str,
) -> None:
    """Slice 20C drift bridge — best-effort, never raises.

    Lazy import keeps this module's substrate independent of the
    drift tracker module (so a circular-import collapse can't take
    down json healing). Empty model_id / op_id is the explicit caller
    signal "don't record" — we honor it without inspecting the
    tracker's own master flag.
    """
    if not op_id or not model_id:
        return
    try:
        from backend.core.ouroboros.governance.schema_drift_tracker import (
            DriftType,
            get_default_tracker,
        )
        get_default_tracker().record(
            op_id=op_id,
            model_id=model_id,
            drift_type=DriftType.JSON_PARSE_ERROR_AFTER_HEAL,
            raw_excerpt=raw_excerpt,
        )
    except Exception:  # noqa: BLE001 — drift recording is enhancement
        logger.debug(
            "[json_healer] drift recording failed for op=%s model=%s",
            op_id, model_id, exc_info=True,
        )
