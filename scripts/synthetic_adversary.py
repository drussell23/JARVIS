"""synthetic_adversary.py -- deterministic provider chaos proxy for the Isomorphic
Local Sandbox (Task 3).

REWORK 2026-06-26 (Layer A fix — faithful DW mock):
  Clears "Active provider fleet empty" by serving a real OpenAI-compat
  /v1/models + /v1/chat/completions that the provider preflight accepts:
    * Dynamic model list from JARVIS_DW_TRUSTED_MODELS (the authoritative env the
      ledger iterates via dw_promotion_ledger.py → preflight_probe.py).
    * SSE shape exactly matching dw_heavy_probe._do_probe: data: JSON chunk with
      choices[0].delta.content non-empty, then data: [DONE].
    * Dedicated-thread isolation: server runs in its OWN OS thread with its OWN
      asyncio event loop — the driver's main event loop cannot starve it.
    * HEALTHY / DEGRADED / OUTAGE state machine (set_state()).
    * env_overrides() sets DOUBLEWORD_BASE_URL=.../v1 +
      JARVIS_AEGIS_UPSTREAM_DOUBLEWORD_URL (Aegis upstream).
  Legacy /dw/* routes kept byte-identical for backward compat.

ZERO-SHOT ("cocky") PROFILE (2026-06-28):
  Reproduces the gpt-oss-120b failure mode: model returns a final repair
  candidate with ZERO exploration tool calls — bypassing the Iron Gate
  exploration requirement.

  Toggle:
    * env  JARVIS_ADVERSARY_SIMULATE_ZERO_SHOT=1  (any truthy string: "1","true","yes")
    * API  adv.set_simulate_zero_shot(True)        (thread-safe, same lock as set_state)

  Behaviour when active (is_repair=True branch only):
    * Request body has tools + NO tool_choice="required" → returns a FINAL 2b.1
      candidates response immediately (0 exploration tool calls).  This reproduces
      the "cocky model bypasses Iron Gate" failure.  response_kind = "zero_shot_bypass".
    * Request body has tool_choice="required" → returns a NATIVE OpenAI-compat
      tool_calls delta (read_file) in the SSE stream.  Models "endpoint honors
      forcing — even a cocky model is forced to call a tool."
      response_kind = "zero_shot_forced_tool_calls".

  Probe requests (is_repair=False) are unaffected regardless of the flag.
  Existing explore-first behaviour is byte-identical when the profile is OFF.

REUSE (per plan spec -- no duplication)
-----------------------------------------
* FakeClock  (scripts/chaos_injector.py:76)    -- injectable deterministic clock,
  no real sleeps; the adversary reads time ONLY through the injected clock_fn.
* FaultInjector + FaultType  (tests/adversarial/fault_injector.py:56)  -- used
  as boundary-based per-request fault dispatch registry; FailureSource values are
  stored in FaultSpec.params["failure_source"] and consumed via check().
* FailureSource  (topology_sentinel.py:429)    -- the SINGLE source of truth for
  the DW HTTP failure taxonomy; no parallel enum invented here.

ARCHITECTURE
------------
One aiohttp server on a random localhost port, two route prefixes:
  /v1/*      -- Faithful DW mock (DOUBLEWORD_BASE_URL points here).
               GET /v1/models   -- dynamic model list from JARVIS_DW_TRUSTED_MODELS
               POST /v1/chat/completions -- OpenAI-compat SSE/JSON (probe-accepted)
  /dw/*      -- Legacy routes (backward compat; same handlers, same fault logic).
  /prime/*   -- J-Prime  (JARVIS_PRIME_URL)
  /reactor/* -- Reactor  (JARVIS_REACTOR_URL / REACTOR_CORE_API_URL)

Per (route, endpoint) fault schedule: list of _ScheduledFault entries checked
against the FakeClock.  The /models (HeavyProbe path) and the
/chat/completions (generation path) are INDEPENDENTLY controllable (by design --
that is exactly the run-#11 condition Task 4 needs to reproduce).

Usage
-----
    from scripts.synthetic_adversary import SyntheticAdversary, AdversaryState
    from scripts.chaos_injector import FakeClock
    from backend.core.ouroboros.governance.topology_sentinel import FailureSource

    clock = FakeClock(start=0.0)
    adv = SyntheticAdversary(clock=clock)
    adv.schedule(route="doubleword", endpoint="/chat/completions",
                 fault=FailureSource.LIVE_HTTP_5XX, at=0.0)
    urls = await adv.start()   # {"doubleword": "http://127.0.0.1:PORT/v1", ...}
    os.environ.update(adv.env_overrides())  # {DOUBLEWORD_BASE_URL: ".../v1", ...}
    adv.set_state(AdversaryState.OUTAGE)    # switch to outage mid-test
    # ... run providers ...
    await adv.stop()
"""
from __future__ import annotations

import asyncio
import dataclasses
import enum
import json
import logging
import os
import re
import sys
import threading
import time
from typing import Any, Dict, List, Optional

# Repo-root bootstrap so the module works both as a script and as an import.
# Must be at sys.path[0] -- _ensure_backend_on_path() in isomorphic_a1_local.py
# inserts backend/ at index 0 after repo-root, which shadows the top-level
# tests/ package with backend/tests/ (no adversarial sub-package there).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Always ensure repo root is at position 0, not just anywhere in sys.path.
if sys.path[:1] != [_REPO_ROOT]:
    try:
        sys.path.remove(_REPO_ROOT)
    except ValueError:
        pass
    sys.path.insert(0, _REPO_ROOT)
# Evict a stale tests namespace that may have been pinned to backend/tests/
# before the repo root landed at position 0.
_TESTS_ROOT = os.path.join(_REPO_ROOT, "tests")
if "tests" in sys.modules and (
    getattr(sys.modules["tests"], "__path__", [""])[0] != _TESTS_ROOT
):
    for _k in [k for k in list(sys.modules) if k == "tests" or k.startswith("tests.")]:
        del sys.modules[_k]

import aiohttp.web  # noqa: E402  (already a dep per plan spec)

# ── reused modules (do NOT duplicate) ──────────────────────────────────────── #
from scripts.chaos_injector import FakeClock  # noqa: E402
try:
    from tests.adversarial.fault_injector import (  # noqa: E402
        FaultInjector,
        FaultSpec,
        FaultType,
    )
    _FAULT_INJECTOR_AVAILABLE = True
except ImportError:
    # Fail-soft: tests/ stripped from a deployed image.  The adversary still
    # serves faults via FailureSource string values; FaultInjector boundary-
    # dispatch is a thin helper that is skipped in this mode.
    FaultInjector = None  # type: ignore[assignment,misc]
    FaultSpec = None  # type: ignore[assignment,misc]
    FaultType = None  # type: ignore[assignment,misc]
    _FAULT_INJECTOR_AVAILABLE = False

try:
    from backend.core.ouroboros.governance.topology_sentinel import FailureSource
except ImportError:  # fail-soft: tests that mock the import still work
    FailureSource = None  # type: ignore[assignment,misc]

# ──────────────────────────────────────────────────────────────────────────── #

_log = logging.getLogger(__name__)

# How long (seconds) the LIVE_STREAM_STALL handler holds the SSE connection open.
# Set JARVIS_ADVERSARY_STALL_S to a small value in tests to avoid blocking.
_STALL_S: float = float(os.environ.get("JARVIS_ADVERSARY_STALL_S", "30.0"))

# Zero-shot ("cocky") profile: env-level default.
# Set JARVIS_ADVERSARY_SIMULATE_ZERO_SHOT=1 (or "true"/"yes") to enable globally.
# The per-instance flag (set_simulate_zero_shot) always wins once the server starts.
_ZERO_SHOT_ENV_DEFAULT: bool = os.environ.get(
    "JARVIS_ADVERSARY_SIMULATE_ZERO_SHOT", ""
).lower() in ("1", "true", "yes")


# ── Per-request diagnostic logging for soak debugging ────────────────────────── #

def _log_adversary_request(
    path: str,
    state: str,
    has_tools: bool,
    manifest_present: bool,
    is_repair: bool,
    is_2b1: bool,
    target_file: str,
    prompt_len: int,
    prompt_head: str,
    response_kind: str,
) -> None:
    """Emit a structured diagnostic log line for this request.

    Fields:
      - path: request path (e.g. "/v1/chat/completions")
      - state: "healthy" | "degraded" | "outage"
      - has_tools: bool — request has non-empty `tools` array
      - manifest_present: bool — `_load_chaos_manifest()` returned non-None
      - is_repair: bool — `_is_repair_prompt()` result
      - is_2b1: bool — `_prompt_is_2b1()` result
      - target_file: from manifest, or ""
      - prompt_len: len of concatenated prompt
      - prompt_head: first 200 chars of prompt (repr'd for safe escaping)
      - response_kind: "probe_ok" | "repair_candidates_2b1" | "repair_tool_call:<name>" | "outage" | "degraded"

    Write to file at JARVIS_ADVERSARY_REQ_LOG or .jarvis/adversary_requests.log.
    Fail-soft: logging errors never break the response.
    Thread-safe: append mode + short atomic writes.
    """
    try:
        log_dir = os.path.join(_REPO_ROOT, ".jarvis")
        os.makedirs(log_dir, exist_ok=True)

        log_path = os.environ.get(
            "JARVIS_ADVERSARY_REQ_LOG",
            os.path.join(log_dir, "adversary_requests.log"),
        )

        # Build the structured log line
        log_line = {
            "path": path,
            "state": state,
            "has_tools": has_tools,
            "manifest_present": manifest_present,
            "is_repair": is_repair,
            "is_2b1": is_2b1,
            "target_file": target_file,
            "prompt_len": prompt_len,
            "prompt_head": prompt_head,
            "response_kind": response_kind,
        }

        # Append to file (thread-safe for short writes in append mode)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(log_line) + "\n")

        # Also log to stderr via the module logger
        _log.info(
            "[SyntheticAdversary] req path=%s state=%s has_tools=%s "
            "is_repair=%s is_2b1=%s response_kind=%s",
            path,
            state,
            has_tools,
            is_repair,
            is_2b1,
            response_kind,
        )
    except Exception:  # noqa: BLE001
        # Fail-soft: logging errors never break the response
        try:
            _log.warning("[SyntheticAdversary] diagnostic logging failed", exc_info=True)
        except Exception:  # noqa: BLE001
            pass

# Degraded-state latency (seconds).  Inject this before responding in DEGRADED.
# Set JARVIS_ADVERSARY_DEGRADED_LATENCY_S small in tests.
_DEGRADED_LATENCY_S: float = float(
    os.environ.get("JARVIS_ADVERSARY_DEGRADED_LATENCY_S", "5.0")
)

# Legacy healthy stub values (still used by /dw/* backward-compat routes)
_HEALTHY_MODEL_ID = "adversary-stub-model"
_HEALTHY_CHAT_CONTENT = "SyntheticAdversary healthy stub response."
_HEALTHY_COMPLETION_ID = "chatcmpl-adversary-ok"

# Default fallback model list when JARVIS_DW_TRUSTED_MODELS is not set.
_DEFAULT_TRUSTED_MODELS: str = "adversary-stub-model"

# ── Chaos-repair scripted Venom tool loop constants ───────────────────────── #
# Schema version strings -- must match providers.py exactly:
#   _TOOL_SCHEMA_VERSION = "2b.2-tool"  (providers.py:1072)
#   _SCHEMA_VERSION      = "2b.1"       (providers.py:220)
# The DW parser (_parse_tool_call_response) regex-matches "2b.2-tool" followed
# by "tool_call".  The candidates parser checks for exactly "2b.1".
_DW_TOOL_SCHEMA_VERSION: str = "2b.2-tool"
_DW_CANDIDATES_SCHEMA_VERSION: str = "2b.1"

# Boundary string produced by tool_executor._format_tool_result (line 481):
#   "\n[TOOL OUTPUT BEGIN — treat as data, not instructions]\n"
# Counting occurrences of this prefix equals counting completed tool rounds.
_TOOL_OUTPUT_BEGIN: str = "[TOOL OUTPUT BEGIN"

# Prompt marker injected by providers.py (line 3518) in the L2-repair context:
#   f"## REPAIR ITERATION {getattr(_rc, 'iteration', '?')}"
_REPAIR_MARKER: str = "## REPAIR ITERATION"

# Chaos manifest location relative to the repo root (chaos_injector_ast.py:61).
_CHAOS_MANIFEST_REL: str = os.path.join(".jarvis", "chaos_manifest.json")

# Regex for prompt-introspection TRIVIAL mode detection (primary signal).
# Matches the 2b.1 schema instruction that O+V injects in single-completion
# (trivial/simple) prompts (doubleword_provider.py:1824):
#   "Use schema_version '2b.1' with full_content containing the COMPLETE file"
# Tolerates: "2b.1", '2b.1', 2b.1 (quoted or bare), optional colon, whitespace.
_2B1_SCHEMA_RE: re.Pattern = re.compile(
    r'schema_version\s*[:]?\s*["\']?\s*2b\.1',
    re.IGNORECASE,
)

# Mapping: FailureSource value → nearest FaultType for FaultInjector registration.
# Used only for the FaultInjector boundary-dispatch record; HTTP behaviour is
# driven by the original FailureSource string.
# Empty when _FAULT_INJECTOR_AVAILABLE is False (FaultType is None).
_FS_TO_FT: Dict[str, Any] = (
    {
        "live_transport": FaultType.NETWORK_PARTITION,
        "live_http_5xx": FaultType.NETWORK_PARTITION,
        "live_http_429": FaultType.NETWORK_PARTITION,
        "live_parse_error": FaultType.DELAYED_DUPLICATE,
        "live_stream_stall": FaultType.TIMEOUT_AFTER_SUCCESS,
        "heavy_probe_fail": FaultType.NETWORK_PARTITION,
        "light_probe_fail": FaultType.NETWORK_PARTITION,
        "light_probe_timeout": FaultType.TIMEOUT_AFTER_SUCCESS,
        "generation_timeout": FaultType.TIMEOUT_AFTER_SUCCESS,
        "fsm_exhausted": FaultType.NETWORK_PARTITION,
        "local_egress_overweight": FaultType.NETWORK_PARTITION,
    }
    if _FAULT_INJECTOR_AVAILABLE
    else {}
)


# ── Public state enum ─────────────────────────────────────────────────────── #

class AdversaryState(enum.Enum):
    """Global state of the SyntheticAdversary.  Layered on top of per-route
    fault scheduling — both are checked; the per-route fault wins if active.

    HEALTHY  -- valid 200 SSE/JSON completions + /v1/models 200.
    DEGRADED -- inject artificial latency (JARVIS_ADVERSARY_DEGRADED_LATENCY_S,
                default 5s) before responding.
    OUTAGE   -- return hard 502/503 on /v1/chat/completions and /v1/models
                (tests DW → J-Prime failover trigger).
    """
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    OUTAGE = "outage"


# ── Pure helpers (testable without a server) ──────────────────────────────── #

def _parse_trusted_model_ids() -> List[str]:
    """Parse JARVIS_DW_TRUSTED_MODELS (comma-separated) into a list of model
    IDs.  Falls back to ``_DEFAULT_TRUSTED_MODELS`` when the env is unset or
    empty.  Pure — reads env at call time so tests can monkeypatch before
    calling.  NEVER raises."""
    raw = os.environ.get("JARVIS_DW_TRUSTED_MODELS", "").strip()
    if not raw:
        raw = _DEFAULT_TRUSTED_MODELS
    return [mid.strip() for mid in raw.split(",") if mid.strip()]


def _build_models_body() -> Dict[str, Any]:
    """Build the OpenAI-compat /v1/models response body.  Dynamic: reads
    JARVIS_DW_TRUSTED_MODELS at call time.  Pure (no I/O).

    Schema matches dw_catalog_client.ModelCard.from_api_dict expectations:
      * Required: ``"id"``
      * Optional: ``"object"``, ``"family"``, ``"parameter_count_b"``,
                  ``"context_window"``, ``"supports_streaming"``
    """
    created = int(time.time())
    model_ids = _parse_trusted_model_ids()
    entries = [
        {
            "id": mid,
            "object": "model",
            "family": "adversary",
            "parameter_count_b": 397.0,
            "context_window": 32768,
            "supports_streaming": True,
            "created": created,
        }
        for mid in model_ids
    ]
    return {"data": entries, "object": "list"}


def _build_healthy_chat_json(model: str) -> Dict[str, Any]:
    """Build a healthy non-streaming chat completion JSON body.  Pure."""
    return {
        "id": _HEALTHY_COMPLETION_ID,
        "object": "chat.completion",
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": _HEALTHY_CHAT_CONTENT},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": 8,
            "completion_tokens": 8,
            "total_tokens": 16,
        },
    }


def _build_healthy_sse_chunks(model: str) -> List[bytes]:
    """Build the SSE byte frames that dw_heavy_probe._do_probe accepts as ACTIVE.

    The probe reads ``choices[0].delta.content`` and considers the model ACTIVE
    when it finds a non-empty value before ``data: [DONE]`` (dw_heavy_probe.py
    lines 779-791).  We send:
      1. A content chunk with ``choices[0].delta.content = "ok"``
      2. A usage chunk (empty choices, usage populated)
      3. ``data: [DONE]``
    Pure — no I/O.
    """
    created = int(time.time())
    content_chunk = {
        "id": "adv",
        "object": "text_completion.chunk",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"content": "ok"},
            "finish_reason": None,
        }],
    }
    usage_chunk = {
        "id": "adv",
        "object": "text_completion.chunk",
        "created": created,
        "model": model,
        "choices": [],
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
        },
    }
    return [
        f"data: {json.dumps(content_chunk)}\n\n".encode(),
        f"data: {json.dumps(usage_chunk)}\n\n".encode(),
        b"data: [DONE]\n\n",
    ]


def _build_env_overrides(port: int) -> Dict[str, str]:
    """Pure function: build env-var overrides for the given bound port.

    DOUBLEWORD_BASE_URL  = http://127.0.0.1:<port>/v1
       -- Direct provider calls ``{base_url}/chat/completions`` which resolves
          to /v1/chat/completions on the mock.
    JARVIS_AEGIS_UPSTREAM_DOUBLEWORD_URL = http://127.0.0.1:<port>
       -- Aegis strips /v1 from DOUBLEWORD_BASE_URL → this upstream, then
          appends /v1/... → same mock routes (prefix-invariant).
    JARVIS_AEGIS_URL = http://127.0.0.1:<port>/v1
       -- Aegis bridge URL (legacy consumers).
    """
    base = f"http://127.0.0.1:{port}"
    return {
        "DOUBLEWORD_BASE_URL": f"{base}/v1",
        "JARVIS_AEGIS_UPSTREAM_DOUBLEWORD_URL": base,
        "JARVIS_AEGIS_URL": f"{base}/v1",
        "JARVIS_PRIME_URL": f"{base}/prime",
        "JARVIS_REACTOR_URL": f"{base}/reactor",
        "REACTOR_CORE_API_URL": f"{base}/reactor",
    }


# ── Chaos-repair pure helpers (testable without a server) ────────────────── #

def _load_chaos_manifest() -> Optional[Dict[str, Any]]:
    """Read ``.jarvis/chaos_manifest.json`` from the repo root.

    Returns the parsed dict on success, or None if the file is missing or
    malformed.  Thread-safe (read-only filesystem access).
    """
    manifest_path = os.path.join(_REPO_ROOT, _CHAOS_MANIFEST_REL)
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _is_repair_prompt(
    prompt: str,
    manifest: Optional[Dict[str, Any]],
) -> bool:
    """Return True if ``prompt`` is a REPAIR/generation request for the chaos target.

    Both conditions must hold:
      1. A chaos manifest is present (``manifest`` is not None).
      2. The prompt references the repair context — it contains the
         ``"## REPAIR ITERATION"`` marker (injected by providers.py:3518)
         OR the manifest's ``target_file`` relative path.

    Probe (preflight) requests are short strings without either marker, so they
    always fall through to the generic HEALTHY path (probe path unchanged).
    """
    if manifest is None:
        return False
    if _REPAIR_MARKER in prompt:
        return True
    target_file = manifest.get("target_file", "")
    if target_file and target_file in prompt:
        return True
    return False


def _prompt_is_2b1(prompt: str) -> bool:
    """Return True if ``prompt`` contains the 2b.1 schema instruction.

    This is the **primary** TRIVIAL-mode signal in ``build_repair_completion``.
    O+V injects a system rule (doubleword_provider.py:1824) — e.g.
    ``"Use schema_version '2b.1' with full_content containing the COMPLETE file"``
    — only for ``complexity=trivial / simple`` single-completion requests where
    the Venom tool loop is skipped.  Detecting this instruction is more robust
    than relying solely on the tools-array presence because it keys off what the
    provider actually requested, not a side-effect of the request body shape.

    Tolerant match: handles ``"2b.1"``, ``'2b.1'``, ``2b.1`` (with/without
    quotes/colons/whitespace separators).  Thread-safe (no mutable state).
    """
    return bool(_2B1_SCHEMA_RE.search(prompt))


def _count_prior_tool_results(messages: List[Dict[str, Any]]) -> int:
    """Count completed tool-result blocks in the incoming DW messages list.

    The DoubleWord Venom tool loop accumulates all state in a single growing
    user-message string (``current_prompt``).  Each completed tool round appends
    one ``_format_tool_result`` block whose text begins with
    ``"[TOOL OUTPUT BEGIN"`` (tool_executor.py:481).  Counting that prefix
    across all message content strings gives the number of rounds completed.

    Handles both plain-string content and multi-modal content lists
    (``[{"type": "text", "text": ...}, ...]``).
    """
    count = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            count += content.count(_TOOL_OUTPUT_BEGIN)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text", "")
                    if isinstance(text, str):
                        count += text.count(_TOOL_OUTPUT_BEGIN)
    return count


def build_repair_completion(
    prompt: str,
    messages: List[Dict[str, Any]],
    manifest: Dict[str, Any],
    has_tools: bool = True,
    simulate_zero_shot: bool = False,
) -> str:
    """Build the scripted assistant content string for the chaos-repair path.

    **Zero-shot ("cocky") path** (``simulate_zero_shot=True``):
      Reproduces the gpt-oss-120b failure mode: returns a FINAL 2b.1 repair
      candidate immediately with ZERO exploration tool calls — bypassing the
      Iron Gate requirement.  The candidate content is ``original_source`` (a
      valid repair) so the only failing dimension is the missing exploration.
      Note: this path is entered for both has_tools=True and has_tools=False
      when simulate_zero_shot=True, because a "cocky" model skips exploration
      regardless of whether tools are available.  The caller is responsible for
      the ``tool_choice="required"`` branch (native tool_calls SSE) — that
      case never reaches this function.

    **No-tools path** (``has_tools=False``):
      The Venom tool loop is skipped for ``complexity=trivial/simple`` ops.
      Iron Gate exploration does not apply.  Emit the repair candidate directly
      as a single-completion ``2b.1`` candidates JSON.

    **Tool-loop path** (``has_tools=True``):
      The Iron Gate (orchestrator post-GENERATE, pre-VALIDATE) requires ≥2
      exploration tool calls (``read_file`` / ``search_code`` / ``get_callers``)
      BEFORE any patch.  This rule fires whenever the tool loop is engaged,
      regardless of any ``2b.1`` schema instruction in the prompt.

      The ``2b.1`` schema instruction only governs the **final-step format**:
      when ``prior >= 2`` AND the prompt contains the ``2b.1`` instruction,
      emit final candidates directly (no tool_call → Venom exits the loop,
      Iron Gate sees the 2 prior explorations → satisfied).  When there is no
      ``2b.1`` instruction, the classic write_file → candidates path is used.

      Stateless step inference from ``messages`` — counts ``[TOOL OUTPUT BEGIN``
      markers to determine how many tool rounds have completed:

        Step 0 (0 prior):  ``read_file`` on ``target_file``   [exploration 1]
        Step 1 (1 prior):  ``search_code`` on function name   [exploration 2, Iron Gate ≥2]
        Step ≥2 + 2b.1:   final ``2b.1`` candidates JSON (Iron Gate satisfied)
        Step 2, no 2b.1:  ``write_file`` with ``original_source``  [the repair patch]
        Step ≥3, no 2b.1: final ``2b.1`` candidates JSON

    Returns the JSON string that becomes the assistant message ``content`` field.
    The DW provider's ``_parse_tool_call_response`` regex-matches
    ``schema_version: "2b.2-tool"`` to detect tool calls; a response without
    that key is treated as the final answer and validated as ``2b.1`` candidates.

    Thread-safe: pure function, no shared mutable state.
    """
    target_file = manifest.get("target_file", "")
    original_source = manifest.get("original_source", "")
    function_name = str(manifest.get("function", "repaired_function"))

    # ── Zero-shot ("cocky") path: skip ALL exploration, return final candidate ─ #
    # Reproduces gpt-oss-120b: model submits a patch on turn 0 with zero
    # read_file/search_code → Iron Gate exploration_insufficient.
    # The tool_choice="required" branch is handled in the HTTP handler (not here).
    if simulate_zero_shot:
        return json.dumps({
            "schema_version": _DW_CANDIDATES_SCHEMA_VERSION,
            "candidates": [
                {
                    "candidate_id": "c1",
                    "file_path": target_file,
                    "full_content": original_source,
                    "rationale": (
                        "Zero-shot bypass: returning candidate without prior exploration "
                        "(simulates cocky gpt-oss-120b behaviour — no read_file/search_code)."
                    ),
                }
            ],
        })

    # ── No-tools path: trivial single-completion → direct 2b.1 candidates ─── #
    # The Venom tool loop is skipped; Iron Gate exploration does not apply.
    if not has_tools:
        return json.dumps({
            "schema_version": _DW_CANDIDATES_SCHEMA_VERSION,
            "candidates": [
                {
                    "candidate_id": "c1",
                    "file_path": target_file,
                    "full_content": original_source,
                    "rationale": (
                        "Revert chaos mutation to restore the green test."
                    ),
                }
            ],
        })

    # ── Tool-loop path (has_tools=True): explore-first, then final ─────────── #
    # Iron Gate requires ≥2 exploration calls BEFORE any patch, regardless of
    # any 2b.1 schema instruction in the prompt.  The 2b.1 instruction only
    # determines the FINAL-STEP FORMAT, not whether to skip exploration.
    prior = _count_prior_tool_results(messages)

    if prior == 0:
        # Exploration round 1 — read the chaos target to satisfy Iron Gate later.
        return json.dumps({
            "schema_version": _DW_TOOL_SCHEMA_VERSION,
            "tool_call": {
                "name": "read_file",
                "arguments": {"path": target_file},
            },
        })

    if prior == 1:
        # Exploration round 2 — search for the mutated function (Iron Gate ≥2 read).
        return json.dumps({
            "schema_version": _DW_TOOL_SCHEMA_VERSION,
            "tool_call": {
                "name": "search_code",
                "arguments": {"query": function_name},
            },
        })

    # prior >= 2: Iron Gate exploration satisfied (≥2 exploration calls recorded).
    # Determine the final-step format from the schema instruction in the prompt:
    #   2b.1 in prompt → emit candidates directly (Venom exits the loop; Iron
    #   Gate sees the 2 prior explorations → satisfied).
    #   No 2b.1 → write_file at prior==2 (repair patch), then final candidates.
    if _prompt_is_2b1(prompt):
        return json.dumps({
            "schema_version": _DW_CANDIDATES_SCHEMA_VERSION,
            "candidates": [
                {
                    "candidate_id": "c1",
                    "file_path": target_file,
                    "full_content": original_source,
                    "rationale": (
                        "Revert chaos mutation to restore the green test."
                    ),
                }
            ],
        })

    if prior == 2:
        # Repair round — write original source back, reverting the chaos mutation.
        return json.dumps({
            "schema_version": _DW_TOOL_SCHEMA_VERSION,
            "tool_call": {
                "name": "write_file",
                "arguments": {"path": target_file, "content": original_source},
            },
        })

    # Final round (≥3 prior results or write already completed) — emit candidates.
    # schema_version "2b.1" with candidate_id/file_path/full_content/rationale
    # satisfies providers.py:4919-4928 per-candidate required-fields check.
    return json.dumps({
        "schema_version": _DW_CANDIDATES_SCHEMA_VERSION,
        "candidates": [
            {
                "candidate_id": "c1",
                "file_path": target_file,
                "full_content": original_source,
                "rationale": (
                    "Revert chaos mutation to restore the green test."
                ),
            }
        ],
    })


def _build_repair_sse_chunks(content: str, model: str) -> List[bytes]:
    """Build SSE byte frames for a scripted repair completion step.

    Emits a single ``choices[0].delta.content`` chunk containing ``content``
    (the tool-call or final-candidates JSON string), then ``data: [DONE]``.
    The DW provider accumulates raw SSE token text via ``_dw_rt_consume_stream``
    and passes the concatenated result to ``_parse_tool_call_response`` /
    ``_extract_json_block``, so a single large content token is equivalent to
    streaming it piecemeal.
    """
    created = int(time.time())
    content_chunk = {
        "id": "adv-repair",
        "object": "text_completion.chunk",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"content": content},
            "finish_reason": None,
        }],
    }
    return [
        f"data: {json.dumps(content_chunk)}\n\n".encode(),
        b"data: [DONE]\n\n",
    ]


def _build_forced_tool_call_sse_chunks(target_file: str, model: str) -> List[bytes]:
    """Build SSE byte frames for a native tool_calls response (tool_choice=required honored).

    When the zero-shot profile is active AND the request has ``tool_choice="required"``,
    the endpoint MUST return a native OpenAI-compat ``tool_calls`` delta — it cannot
    return a bare content string (tool_choice=required makes non-tool messages
    protocol-invalid on a conformant endpoint).  This models "the endpoint honors
    forcing; even a cocky model is forced to call a tool."

    Emits one SSE chunk with ``choices[0].delta.tool_calls`` (native format) containing
    a ``read_file`` call on the chaos ``target_file``, then ``data: [DONE]``.

    Pure — no I/O.  Thread-safe.
    """
    created = int(time.time())
    tool_call_chunk = {
        "id": "adv-forced",
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_adv_forced_read",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": target_file}),
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }],
    }
    return [
        f"data: {json.dumps(tool_call_chunk)}\n\n".encode(),
        b"data: [DONE]\n\n",
    ]


def build_batch_output_line(
    custom_id: str,
    input_line_json: str,
    manifest: Optional[Dict[str, Any]],
    simulate_zero_shot: bool = False,
) -> str:
    """Build one JSONL output line for a /v1/files batch output (Stage 4 retrieve).

    Repair-detection: if ``manifest`` is not None AND the serialised input line
    contains the manifest's ``target_file`` path, the 2b.1 schema instruction
    (always present in the DW batch system message), or the REPAIR_ITERATION
    marker, emit a ``2b.1`` candidate JSON with
    ``full_content = manifest["original_source"]``.  Otherwise emit a generic
    valid completion string.

    ``simulate_zero_shot``: when True the zero-shot profile is active.  In the
    batch path there is no tool loop and no ``tool_choice`` header, so zero-shot
    means the same as the no-tools branch: return the 2b.1 candidate directly.
    The ``simulate_zero_shot`` flag is forwarded to ``build_repair_completion``.

    Output shape matches ``doubleword_provider._retrieve_result`` parse
    (lines 4974-5007):
        entry["custom_id"] == custom_id
        entry["response"]["body"]["choices"][0]["message"]["content"]  → text
        entry["response"]["body"]["usage"]  → usage dict

    Pure function: no I/O, no shared mutable state.  Thread-safe.
    """
    is_repair = False
    if manifest is not None:
        target_file = manifest.get("target_file", "") or ""
        # The DW batch system message ALWAYS embeds the 2b.1 schema instruction
        # (doubleword_provider.py:1824), so _prompt_is_2b1 is the primary signal.
        # The target_file and REPAIR_MARKER checks are belt-and-braces for callers
        # that construct a partial body string without the system message.
        is_repair = (
            _prompt_is_2b1(input_line_json)
            or (bool(target_file) and target_file in input_line_json)
            or _REPAIR_MARKER in input_line_json
        )

    if is_repair:
        # batch path = single-completion (no Venom tool loop); has_tools=False
        # forces the trivial 2b.1 direct-candidates branch of build_repair_completion.
        # simulate_zero_shot is forwarded so the zero-shot profile also exercises
        # the batch path (though in batch the effect is the same as has_tools=False
        # since there is no tool loop to bypass).
        content = build_repair_completion(
            prompt=input_line_json,
            messages=[],
            manifest=manifest,  # type: ignore[arg-type]
            has_tools=False,
            simulate_zero_shot=simulate_zero_shot,
        )
    else:
        content = "adversary batch stub"

    return json.dumps({
        "custom_id": custom_id,
        "response": {
            "status_code": 200,
            "body": {
                "choices": [
                    {"message": {"content": content, "role": "assistant"}}
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 200,
                    "total_tokens": 300,
                },
            },
        },
    })


# ──────────────────────────────────────────────────────────────────────────── #


def _fault_str(fault: Any) -> str:
    """Normalise a FailureSource (or string) to its string value."""
    if hasattr(fault, "value"):
        return str(fault.value)
    return str(fault)


@dataclasses.dataclass
class _ScheduledFault:
    """One scheduled fault for a specific (route, endpoint) pair."""

    route: str        # "doubleword" | "prime" | "reactor"
    endpoint: str     # "/chat/completions" | "/models" | ...
    fault: Any        # FailureSource instance (or string matching its value)
    at: float         # FakeClock time >= at → fault is active
    remaining: Optional[int]  # None = infinite; decremented on each use


class SyntheticAdversary:
    """Localhost aiohttp chaos proxy that is a faithful DoubleWord mock.

    Serves OpenAI-compat /v1/models + /v1/chat/completions so O+V's provider
    preflight passes locally (clears "Active provider fleet empty", Layer A).

    The /models (HeavyProbe) path and /chat/completions (real-generation) path
    are **independently** controllable via schedule(): each (route, endpoint)
    tuple has its own fault list so probe-healthy + generation-failing is trivial
    to reproduce (the exact run-#11 condition).

    Time is controlled by the injected FakeClock (no real sleeps in fault logic).
    Fault dispatch is routed through FaultInjector (fault_injector.py) so the
    boundary-level record/check mechanism is exercised.

    Thread isolation
    ----------------
    The aiohttp server runs in a dedicated OS thread with its OWN asyncio event
    loop.  The caller's main event loop cannot starve it.  start() spins the
    thread, awaits a threading.Event until the server is listening, then returns
    the bound URLs.  stop() signals the thread's loop and joins.  State reads and
    writes are protected by _state_lock.
    """

    def __init__(self, *, clock: Optional[FakeClock] = None) -> None:
        # FakeClock (chaos_injector.py:76) -- injectable, no real sleeps
        self._clock: FakeClock = clock if clock is not None else FakeClock()
        self._faults: List[_ScheduledFault] = []
        self._faults_lock: threading.Lock = threading.Lock()
        # FaultInjector (fault_injector.py:56) -- per-request boundary dispatch.
        # None when _FAULT_INJECTOR_AVAILABLE is False (graceful degrade).
        self._injector: Optional[Any] = (
            FaultInjector(seed=0) if _FAULT_INJECTOR_AVAILABLE else None
        )

        # State machine (thread-safe via _state_lock)
        self._state_lock: threading.Lock = threading.Lock()
        self._state: AdversaryState = AdversaryState.HEALTHY
        # Zero-shot ("cocky") profile flag — guarded by _state_lock.
        # Initialised from JARVIS_ADVERSARY_SIMULATE_ZERO_SHOT env; togglable at
        # runtime via set_simulate_zero_shot() (same pattern as set_state()).
        self._simulate_zero_shot: bool = _ZERO_SHOT_ENV_DEFAULT

        # Dedicated server thread + port (set once the thread is up)
        self._port: Optional[int] = None
        self._server_thread: Optional[threading.Thread] = None
        self._thread_stop_event: Optional[threading.Event] = None
        self._thread_loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_future: Optional[asyncio.Future] = None  # lives in thread's loop

        # Legacy aiohttp objects (used by thread's loop, not the caller's loop)
        self._app: Optional[aiohttp.web.Application] = None
        self._runner: Optional[aiohttp.web.AppRunner] = None
        self._site: Optional[aiohttp.web.TCPSite] = None

        # /v1/* batch API file storage — keyed by file_id (thread-safe via _files_lock).
        # Stores both input JSONL uploads and synthesised output JSONL blobs so
        # GET /v1/files/{file_id}/content is one universal lookup.
        self._uploaded_files: Dict[str, str] = {}   # file_id → raw JSONL text
        self._v1_batches: Dict[str, str] = {}        # batch_id → input_file_id
        self._v1_file_counter: int = 0
        self._files_lock: threading.Lock = threading.Lock()

    # ── public API ──────────────────────────────────────────────────────────── #

    def set_state(self, state: AdversaryState) -> None:
        """Set the global state machine.  Thread-safe; callable at any time.

        HEALTHY  → valid 200 responses on all /v1/* routes.
        DEGRADED → inject JARVIS_ADVERSARY_DEGRADED_LATENCY_S before responding.
        OUTAGE   → return 502 on /v1/chat/completions, 503 on /v1/models.

        The per-route fault schedule (schedule()) is checked FIRST; the global
        state applies only when no active per-route fault is found.
        """
        with self._state_lock:
            old = self._state
            self._state = state
        _log.info(
            "[SyntheticAdversary] state %s → %s", old.value, state.value,
        )

    def set_simulate_zero_shot(self, enabled: bool) -> None:
        """Enable or disable the zero-shot ("cocky") adversary profile.

        When enabled: the repair branch skips all exploration tool calls and
        returns a final 2b.1 candidate immediately (0 read_file/search_code
        rounds), unless the request carries ``tool_choice="required"`` — in
        that case a native ``tool_calls`` SSE response is returned instead
        (models "endpoint honors forcing").

        Thread-safe; can be called at any time, including after ``start()``.
        Consistent with ``set_state()``: guarded by the same ``_state_lock``.
        """
        with self._state_lock:
            old = self._simulate_zero_shot
            self._simulate_zero_shot = bool(enabled)
        _log.info(
            "[SyntheticAdversary] simulate_zero_shot %s → %s",
            old, bool(enabled),
        )

    def schedule(
        self,
        *,
        route: str,
        endpoint: str,
        fault: Any,       # FailureSource | str  (FailureSource is the SoT)
        at: float = 0.0,
        count: Optional[int] = None,
    ) -> None:
        """Schedule a deterministic fault for (route, endpoint).

        Args:
            route:    Provider name  -- "doubleword" | "prime" | "reactor".
            endpoint: Path suffix    -- "/chat/completions" | "/models" | ...
            fault:    FailureSource  -- LIVE_TRANSPORT / LIVE_HTTP_5XX /
                      LIVE_HTTP_429 / LIVE_PARSE_ERROR / LIVE_STREAM_STALL
                      (or any other FailureSource value; string accepted too).
            at:       FakeClock time when fault activates (default 0 = instant).
            count:    Max injections; None = unlimited.  After count is exhausted
                      the slot is skipped and subsequent requests see healthy.
        """
        entry = _ScheduledFault(
            route=route,
            endpoint=endpoint,
            fault=fault,
            at=float(at),
            remaining=count,
        )
        with self._faults_lock:
            self._faults.append(entry)
        _log.debug(
            "[SyntheticAdversary] scheduled %s@%s=%s at=%.1f count=%s",
            route, endpoint, _fault_str(fault), at, count,
        )

    async def start(self) -> Dict[str, str]:
        """Start the adversary server in a dedicated OS thread with its own loop.

        Spins up the thread, waits (via run_in_executor to avoid blocking the
        caller's event loop) until the server is actually listening, then returns
        URL dict:
          {"doubleword": "http://127.0.0.1:PORT/v1",
           "prime":      "http://127.0.0.1:PORT/prime",
           "reactor":    "http://127.0.0.1:PORT/reactor"}

        DOUBLEWORD_BASE_URL = .../v1 so that:
          direct provider: {base_url}/chat/completions → /v1/chat/completions
          Aegis: strips /v1, appends /v1/... → same routes
        """
        if self._server_thread is not None:
            raise RuntimeError(
                "SyntheticAdversary.start() already called; call stop() first."
            )
        ready_event = threading.Event()
        self._thread_stop_event = threading.Event()
        self._server_thread = threading.Thread(
            target=self._run_server_thread,
            args=(ready_event,),
            daemon=True,
            name="synthetic-adversary-server",
        )
        self._server_thread.start()

        # Wait for the server to bind — run in executor so we don't block
        # the caller's event loop while the thread is starting.
        caller_loop = asyncio.get_event_loop()
        await caller_loop.run_in_executor(
            None, lambda: ready_event.wait(10.0)
        )

        if self._port is None:
            raise RuntimeError(
                "SyntheticAdversary: server thread failed to bind within 10s"
            )
        base = self._base_url()
        _log.info("[SyntheticAdversary] listening on %s/v1", base)
        return {
            "doubleword": f"{base}/v1",
            "prime": f"{base}/prime",
            "reactor": f"{base}/reactor",
        }

    async def stop(self) -> None:
        """Shut down the adversary server and join the dedicated thread."""
        # Signal the server's loop-level stop future (thread-safe)
        thread_loop = self._thread_loop
        stop_future = self._stop_future
        if thread_loop is not None and stop_future is not None:
            def _resolve() -> None:
                if not stop_future.done():
                    stop_future.set_result(None)
            thread_loop.call_soon_threadsafe(_resolve)

        # Fallback: signal the threading.Event for the _run_server_thread wrapper
        if self._thread_stop_event is not None:
            self._thread_stop_event.set()

        # Join the dedicated thread (in executor to not block the caller's loop)
        server_thread = self._server_thread
        if server_thread is not None:
            caller_loop = asyncio.get_event_loop()
            await caller_loop.run_in_executor(
                None, lambda: server_thread.join(timeout=10.0)
            )

        self._server_thread = None
        self._thread_loop = None
        self._stop_future = None
        with self._state_lock:
            self._port = None
        _log.info("[SyntheticAdversary] stopped")

    def env_overrides(self) -> Dict[str, str]:
        """Return env-var overrides to point providers at this server.

        DOUBLEWORD_BASE_URL            = http://127.0.0.1:<port>/v1
        JARVIS_AEGIS_UPSTREAM_DOUBLEWORD_URL = http://127.0.0.1:<port>
        JARVIS_AEGIS_URL               = http://127.0.0.1:<port>/v1
        JARVIS_PRIME_URL               = http://127.0.0.1:<port>/prime
        JARVIS_REACTOR_URL             = http://127.0.0.1:<port>/reactor
        REACTOR_CORE_API_URL           = http://127.0.0.1:<port>/reactor

        Raises RuntimeError if start() has not been called.
        """
        if self._port is None:
            raise RuntimeError(
                "SyntheticAdversary.start() must be awaited before env_overrides()"
            )
        return _build_env_overrides(self._port)

    # ── dedicated server thread ──────────────────────────────────────────────── #

    def _run_server_thread(self, ready_event: threading.Event) -> None:
        """Thread entry-point: create a fresh event loop, run the server,
        close the loop on exit.  Never raises (errors logged)."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._thread_loop = loop
        try:
            loop.run_until_complete(self._serve_until_stopped(ready_event))
        except Exception as exc:  # noqa: BLE001
            _log.error("[SyntheticAdversary] server thread crashed: %r", exc)
            ready_event.set()  # Unblock start() so it doesn't hang
        finally:
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass
            self._thread_loop = None

    async def _serve_until_stopped(self, ready_event: threading.Event) -> None:
        """Async server lifecycle — runs entirely in the dedicated thread's loop.

        Creates the aiohttp AppRunner/TCPSite, signals ready_event once bound,
        then awaits the stop future (does NOT block the loop — other handler
        coroutines can still run freely).
        """
        self._app = self._make_app()
        self._runner = aiohttp.web.AppRunner(
            self._app,
            access_log=None,  # silence per-request logs in tests
        )
        await self._runner.setup()
        self._site = aiohttp.web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()

        # Read the OS-assigned port (thread-safe write; start() reads after event)
        for sock in self._site._server.sockets:  # type: ignore[union-attr]
            with self._state_lock:
                self._port = sock.getsockname()[1]
            break

        # Create the stop future in THIS loop (the thread's loop)
        loop = asyncio.get_event_loop()
        self._stop_future = loop.create_future()
        ready_event.set()  # Unblock start()

        # Await stop signal — does not block the event loop so handlers keep
        # serving requests on this same loop concurrently.
        await self._stop_future

        # Cleanup
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None
        self._app = None
        self._site = None

    # ── internals ───────────────────────────────────────────────────────────── #

    def _base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def _make_app(self) -> aiohttp.web.Application:
        app = aiohttp.web.Application()
        r = app.router

        # ── NEW /v1/* faithful DW mock routes ──────────────────────────────── #
        r.add_get("/v1/models", self._handle_v1_models)
        r.add_post("/v1/chat/completions", self._handle_v1_chat)
        # 4-stage batch API: upload → create → poll → retrieve
        r.add_post("/v1/files", self._handle_v1_files)
        r.add_post("/v1/batches", self._handle_v1_batch_create)
        r.add_get("/v1/batches/{batch_id}", self._handle_v1_batch_get)
        r.add_get("/v1/files/{file_id}/content", self._handle_v1_file_content)

        # ── LEGACY /dw/* backward-compat routes ────────────────────────────── #
        r.add_post("/dw/chat/completions", self._handle_dw_chat)
        r.add_get("/dw/models", self._handle_dw_models)
        # DW batch / file stubs (pass-through healthy or fault)
        r.add_post("/dw/batches", self._handle_dw_batch_create)
        r.add_get("/dw/batches/{batch_id}", self._handle_dw_batch_get)
        r.add_get("/dw/files/{file_id}/content", self._handle_dw_file_content)
        r.add_post("/dw/files", self._handle_dw_files)

        # ── Prime / Reactor catch-all ───────────────────────────────────────── #
        r.add_route("*", "/prime/{path_info:.*}", self._handle_prime)
        r.add_route("*", "/reactor/{path_info:.*}", self._handle_reactor)
        return app

    def _get_active_fault(self, route: str, endpoint: str) -> Optional[Any]:
        """Return the active FailureSource for (route, endpoint), consuming count.

        Implementation note: uses FaultInjector (fault_injector.py) as the
        boundary-based dispatch registry.  When a clock-active fault is found it
        is registered in self._injector under the key "{route}:{endpoint}" and
        immediately consumed via check() -- this exercises the FaultInjector
        register/check contract while the HTTP-level behaviour is driven by the
        returned FailureSource.
        """
        now = self._clock()
        with self._faults_lock:
            faults_snapshot = list(self._faults)
        for entry in faults_snapshot:
            if entry.route != route or entry.endpoint != endpoint:
                continue
            if now < entry.at:
                continue
            if entry.remaining is not None and entry.remaining <= 0:
                continue
            # Active: consume one count slot (thread-safe decrement)
            if entry.remaining is not None:
                with self._faults_lock:
                    if entry.remaining > 0:
                        entry.remaining -= 1
                    else:
                        continue
            # Register in FaultInjector for boundary-dispatch record/check.
            # Skipped when _FAULT_INJECTOR_AVAILABLE is False (no tests/ available);
            # HTTP-level fault behaviour is still driven by the returned fault value.
            fs_str = _fault_str(entry.fault)
            if self._injector is not None and _FAULT_INJECTOR_AVAILABLE:
                ft = _FS_TO_FT.get(
                    fs_str,
                    FaultType.NETWORK_PARTITION,  # type: ignore[union-attr]
                )
                boundary_key = f"{route}:{endpoint}"
                self._injector.register(
                    boundary_key, ft,
                    params={"failure_source": fs_str},
                    one_shot=True,
                )
                spec: Optional[Any] = self._injector.check(boundary_key)
                if spec is not None:
                    _log.debug(
                        "[SyntheticAdversary] dispatching %s@%s fault=%s",
                        route, endpoint, fs_str,
                    )
            else:
                _log.debug(
                    "[SyntheticAdversary] dispatching %s@%s fault=%s (no FaultInjector)",
                    route, endpoint, fs_str,
                )
            return entry.fault
        return None

    async def _apply_fault(
        self,
        request: aiohttp.web.Request,
        fault: Any,
    ) -> Optional[aiohttp.web.Response]:
        """Map FailureSource → HTTP-level behaviour.  Returns Response or None (= healthy).

        FailureSource taxonomy (topology_sentinel.py:429-472):
          LIVE_TRANSPORT    → abort TCP connection (no HTTP response)
          LIVE_HTTP_5XX     → 503 Service Unavailable
          LIVE_HTTP_429     → 429 Too Many Requests + Retry-After
          LIVE_PARSE_ERROR  → 200 with malformed / truncated JSON body
          LIVE_STREAM_STALL → SSE headers sent; tokens never arrive (stall)
        """
        fs = _fault_str(fault)

        if fs == "live_transport":
            # Abort the TCP transport so no HTTP response reaches the client.
            # The client sees ServerDisconnectedError / ClientConnectionError.
            try:
                if request.transport is not None:
                    request.transport.abort()
            except Exception:  # noqa: BLE001
                pass
            # Raising HTTPException stops handler; since the transport is
            # already aborted the error body cannot be written.
            raise aiohttp.web.HTTPServiceUnavailable(
                reason="LIVE_TRANSPORT: connection aborted by adversary"
            )

        if fs == "live_http_5xx":
            return aiohttp.web.Response(
                status=503,
                content_type="application/json",
                text=json.dumps({
                    "error": {
                        "message": "Service unavailable (LIVE_HTTP_5XX injected)",
                        "type": "server_error",
                        "code": "service_unavailable",
                    }
                }),
            )

        if fs == "live_http_429":
            return aiohttp.web.Response(
                status=429,
                headers={"Retry-After": "5", "X-RateLimit-Remaining": "0"},
                content_type="application/json",
                text=json.dumps({
                    "error": {
                        "message": "Rate limit exceeded (LIVE_HTTP_429 injected)",
                        "type": "rate_limit_error",
                        "code": "rate_limit_exceeded",
                    }
                }),
            )

        if fs == "live_parse_error":
            # 200 OK but body is malformed JSON / truncated completion
            return aiohttp.web.Response(
                status=200,
                content_type="application/json",
                text='{"id":"adversary-parse-error","object":"chat.comp',  # truncated
            )

        if fs == "live_stream_stall":
            # Open an SSE connection, send keep-alive comment, then stall.
            # Client will eventually time out (LIVE_STREAM_STALL semantics).
            # JARVIS_ADVERSARY_STALL_S controls how long to hold (default 30s;
            # set small in tests to avoid blocking).
            stall_s = float(os.environ.get("JARVIS_ADVERSARY_STALL_S", str(_STALL_S)))
            resp = aiohttp.web.StreamResponse(
                headers={
                    "Content-Type": "text/event-stream",
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "X-Adversary-Fault": "live_stream_stall",
                }
            )
            await resp.prepare(request)
            # Send a keep-alive comment so the client knows the connection is
            # open but no tokens follow (distinguishes from immediate close)
            await resp.write(b": adversary-stall keep-alive\n\n")
            try:
                await asyncio.sleep(stall_s)
            except asyncio.CancelledError:
                pass
            return resp

        # Unknown / telemetry-only FailureSource (GENERATION_TIMEOUT etc.) --
        # treat as healthy (fail-soft, don't block the request)
        _log.warning(
            "[SyntheticAdversary] unhandled FailureSource %r -- serving healthy", fs
        )
        return None

    # ── /v1/* faithful DW mock handlers ─────────────────────────────────────── #

    async def _handle_v1_models(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """GET /v1/models -- dynamic model list from JARVIS_DW_TRUSTED_MODELS.

        Checks per-route fault first, then global state machine.
        Returns OpenAI-compat {"data": [...]} with one entry per trusted model.
        The probe (dw_catalog_client) reads raw["id"] + optional metadata.
        """
        fault = self._get_active_fault("doubleword", "/models")
        if fault is not None:
            result = await self._apply_fault(request, fault)
            if result is not None:
                return result

        with self._state_lock:
            state = self._state

        if state == AdversaryState.OUTAGE:
            return aiohttp.web.Response(
                status=503,
                content_type="application/json",
                text=json.dumps({"error": {"message": "outage", "code": "outage"}}),
            )

        if state == AdversaryState.DEGRADED:
            latency_s = float(
                os.environ.get("JARVIS_ADVERSARY_DEGRADED_LATENCY_S", str(_DEGRADED_LATENCY_S))
            )
            await asyncio.sleep(latency_s)

        # HEALTHY (or post-DEGRADED delay) — dynamic model list
        body = _build_models_body()
        return aiohttp.web.Response(
            status=200,
            content_type="application/json",
            text=json.dumps(body),
        )

    async def _handle_v1_chat(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.StreamResponse:
        """POST /v1/chat/completions -- faithful DW mock for the preflight probe.

        SSE shape matches dw_heavy_probe._do_probe expectations:
          * HTTP 200
          * Content-Type: text/event-stream
          * data: JSON chunk with choices[0].delta.content non-empty  ← probe triggers ACTIVE
          * data: usage chunk
          * data: [DONE]

        Non-streaming (stream=false): standard JSON completion body.
        Checks per-route fault first, then global state machine.
        """
        fault = self._get_active_fault("doubleword", "/chat/completions")
        if fault is not None:
            result = await self._apply_fault(request, fault)
            if result is not None:
                _log_adversary_request(
                    path="/v1/chat/completions",
                    state="outage",
                    has_tools=False,
                    manifest_present=False,
                    is_repair=False,
                    is_2b1=False,
                    target_file="",
                    prompt_len=0,
                    prompt_head="",
                    response_kind="outage",
                )
                return result  # type: ignore[return-value]

        with self._state_lock:
            state = self._state

        if state == AdversaryState.OUTAGE:
            _log_adversary_request(
                path="/v1/chat/completions",
                state="outage",
                has_tools=False,
                manifest_present=False,
                is_repair=False,
                is_2b1=False,
                target_file="",
                prompt_len=0,
                prompt_head="",
                response_kind="outage",
            )
            return aiohttp.web.Response(  # type: ignore[return-value]
                status=502,
                content_type="application/json",
                text=json.dumps({"error": {"message": "outage", "code": "bad_gateway"}}),
            )

        state_str = state.value  # "healthy" | "degraded" | "outage"

        if state == AdversaryState.DEGRADED:
            latency_s = float(
                os.environ.get("JARVIS_ADVERSARY_DEGRADED_LATENCY_S", str(_DEGRADED_LATENCY_S))
            )
            await asyncio.sleep(latency_s)

        # Parse the request body
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        stream = body.get("stream", True)
        model = body.get("model", _HEALTHY_MODEL_ID)
        messages: List[Dict[str, Any]] = body.get("messages", [])

        # Read tool_choice for zero-shot forcing-honored branch.
        # tool_choice="required" (OpenAI-compat) means the endpoint MUST return
        # a tool_calls response — even a "cocky" model cannot bypass it.
        tool_choice = body.get("tool_choice")
        tool_choice_required = (tool_choice == "required")

        # Read zero-shot flag under lock (same lock as _state, consistent).
        with self._state_lock:
            simulate_zero_shot = self._simulate_zero_shot

        # Early diagnostic fields for logging
        has_tools = bool(body.get("tools"))
        _manifest = _load_chaos_manifest()
        manifest_present = _manifest is not None
        prompt_str = ""
        prompt_len = 0
        prompt_head = ""
        is_repair = False
        is_2b1 = False
        target_file = ""

        # ── Chaos-repair scripted Venom tool loop (Isomorphic Sandbox Task 3b) ── #
        # Detect REPAIR/generation requests for the chaos target and drive the
        # scripted explore(×2) → write_file → candidates sequence.
        # Probe (preflight) requests have short prompts without REPAIR markers
        # and fall through to the generic HEALTHY path unchanged.
        if _manifest is not None:
            # Concatenate all message content strings to build the full prompt
            # (mirrors what the DW provider does: all history is in one user msg).
            _parts: List[str] = []
            for _msg in messages:
                _c = _msg.get("content", "")
                if isinstance(_c, str):
                    _parts.append(_c)
                elif isinstance(_c, list):
                    for _blk in _c:
                        if isinstance(_blk, dict):
                            _t = _blk.get("text", "")
                            if isinstance(_t, str):
                                _parts.append(_t)
            prompt_str = " ".join(_parts)
            prompt_len = len(prompt_str)
            prompt_head = repr(prompt_str[:200])
            is_repair = _is_repair_prompt(prompt_str, _manifest)
            is_2b1 = _prompt_is_2b1(prompt_str)
            target_file = _manifest.get("target_file", "")

            if is_repair:
                # ── Zero-shot ("cocky") profile — repair branch ────────────────── #
                # When active, the model bypasses Iron Gate by submitting a patch
                # with zero exploration tool calls.  BUT: if the request carries
                # tool_choice="required" (OpenAI-compat forcing), a conformant
                # endpoint CANNOT return a non-tool message — so we emit a native
                # tool_calls response instead (models "forcing honored").
                if simulate_zero_shot and tool_choice_required:
                    # Forcing honored: return a native tool_calls SSE delta.
                    # Even a "cocky" model is forced to call a tool by the endpoint.
                    response_kind = "zero_shot_forced_tool_calls"
                    _log_adversary_request(
                        path="/v1/chat/completions",
                        state=state_str,
                        has_tools=has_tools,
                        manifest_present=True,
                        is_repair=True,
                        is_2b1=is_2b1,
                        target_file=target_file,
                        prompt_len=prompt_len,
                        prompt_head=prompt_head,
                        response_kind=response_kind,
                    )
                    _forced_resp = aiohttp.web.StreamResponse(
                        status=200,
                        headers={
                            "Content-Type": "text/event-stream",
                            "Cache-Control": "no-cache",
                            "Connection": "keep-alive",
                            "X-Accel-Buffering": "no",
                            "X-Adversary-Profile": "zero_shot_forced",
                        }
                    )
                    await _forced_resp.prepare(request)
                    for _chunk_bytes in _build_forced_tool_call_sse_chunks(
                        target_file, model
                    ):
                        await _forced_resp.write(_chunk_bytes)
                    await _forced_resp.write_eof()
                    return _forced_resp

                # has_tools=True only when the request includes a non-empty
                # tools array (Venom multi-turn path).  Trivial/simple ops
                # send no tools → single-completion 2b.1 candidates direct.
                # simulate_zero_shot is forwarded: when True, build_repair_completion
                # skips exploration and returns a bare 2b.1 candidate on turn 0.
                _repair_content = build_repair_completion(
                    prompt_str, messages, _manifest,
                    has_tools=has_tools,
                    simulate_zero_shot=simulate_zero_shot,
                )
                _log.debug(
                    "[SyntheticAdversary] repair-branch step=%d content_len=%d zero_shot=%s",
                    _count_prior_tool_results(messages),
                    len(_repair_content),
                    simulate_zero_shot,
                )

                # Determine response_kind based on what build_repair_completion returned.
                if simulate_zero_shot:
                    # Zero-shot bypass: bare 2b.1 candidate with 0 exploration calls.
                    response_kind = "zero_shot_bypass"
                elif not has_tools:
                    response_kind = "repair_candidates_2b1"
                else:
                    prior = _count_prior_tool_results(messages)
                    if prior == 0:
                        response_kind = "repair_tool_call:read_file"
                    elif prior == 1:
                        response_kind = "repair_tool_call:search_code"
                    elif is_2b1:
                        # prior >= 2 + 2b.1 in prompt → final candidates (2b.1 format)
                        response_kind = "repair_candidates_2b1"
                    elif prior == 2:
                        response_kind = "repair_tool_call:write_file"
                    else:
                        response_kind = "repair_candidates_2b1"

                if not stream:
                    _log_adversary_request(
                        path="/v1/chat/completions",
                        state=state_str,
                        has_tools=has_tools,
                        manifest_present=True,
                        is_repair=True,
                        is_2b1=is_2b1,
                        target_file=target_file,
                        prompt_len=prompt_len,
                        prompt_head=prompt_head,
                        response_kind=response_kind,
                    )
                    return aiohttp.web.Response(  # type: ignore[return-value]
                        status=200,
                        content_type="application/json",
                        text=json.dumps({
                            "id": "adv-repair",
                            "object": "chat.completion",
                            "model": model,
                            "choices": [{
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": _repair_content,
                                },
                                "finish_reason": "stop",
                            }],
                            "usage": {
                                "prompt_tokens": 1,
                                "completion_tokens": 1,
                                "total_tokens": 2,
                            },
                        }),
                    )
                # SSE repair response — same frame structure as healthy SSE
                # but content carries the scripted tool-call / candidates JSON.
                _log_adversary_request(
                    path="/v1/chat/completions",
                    state=state_str,
                    has_tools=has_tools,
                    manifest_present=True,
                    is_repair=True,
                    is_2b1=is_2b1,
                    target_file=target_file,
                    prompt_len=prompt_len,
                    prompt_head=prompt_head,
                    response_kind=response_kind,
                )
                _repair_resp = aiohttp.web.StreamResponse(
                    status=200,
                    headers={
                        "Content-Type": "text/event-stream",
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                    }
                )
                await _repair_resp.prepare(request)
                for _chunk_bytes in _build_repair_sse_chunks(_repair_content, model):
                    await _repair_resp.write(_chunk_bytes)
                await _repair_resp.write_eof()
                return _repair_resp
        # ── End chaos-repair branch; probe path falls through unchanged ────────── #

        # Normal healthy path (probe)
        response_kind = "probe_ok"
        if not stream:
            _log_adversary_request(
                path="/v1/chat/completions",
                state=state_str,
                has_tools=has_tools,
                manifest_present=manifest_present,
                is_repair=False,
                is_2b1=False,
                target_file="",
                prompt_len=prompt_len,
                prompt_head=prompt_head,
                response_kind=response_kind,
            )
            return aiohttp.web.Response(  # type: ignore[return-value]
                status=200,
                content_type="application/json",
                text=json.dumps(_build_healthy_chat_json(model)),
            )

        # SSE streaming response
        _log_adversary_request(
            path="/v1/chat/completions",
            state=state_str,
            has_tools=has_tools,
            manifest_present=manifest_present,
            is_repair=False,
            is_2b1=False,
            target_file="",
            prompt_len=prompt_len,
            prompt_head=prompt_head,
            response_kind=response_kind,
        )
        resp = aiohttp.web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            }
        )
        await resp.prepare(request)
        for chunk_bytes in _build_healthy_sse_chunks(model):
            await resp.write(chunk_bytes)
        await resp.write_eof()
        return resp

    # ── /v1/* batch API handlers (4-stage: upload → create → poll → retrieve) ── #

    async def _handle_v1_files(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """POST /v1/files -- Stage 1: receive multipart JSONL upload, store it.

        The DW provider (doubleword_provider._upload_file) sends:
          multipart/form-data with field "file" (JSONL bytes) + field "purpose"
        Returns: {"id": "<file_id>", "object": "file", "purpose": "batch"}
        """
        fault = self._get_active_fault("doubleword", "/files")
        if fault is not None:
            result = await self._apply_fault(request, fault)
            if result is not None:
                return result

        with self._state_lock:
            state = self._state
        if state == AdversaryState.OUTAGE:
            return aiohttp.web.Response(
                status=503,
                content_type="application/json",
                text=json.dumps({"error": {"message": "outage", "code": "outage"}}),
            )

        # Read multipart form data; fall back to raw body (e.g. in unit tests).
        jsonl_content = ""
        try:
            reader = await request.multipart()
            async for field in reader:
                if field.name == "file":
                    raw = await field.read()
                    jsonl_content = raw.decode("utf-8", errors="replace")
                    break
        except Exception:  # noqa: BLE001 — non-multipart upload (test helpers)
            try:
                jsonl_content = (await request.text()) or ""
            except Exception:  # noqa: BLE001
                jsonl_content = ""

        with self._files_lock:
            self._v1_file_counter += 1
            file_id = f"v1_file_{self._v1_file_counter}"
            self._uploaded_files[file_id] = jsonl_content

        return aiohttp.web.Response(
            status=200,
            content_type="application/json",
            text=json.dumps({"id": file_id, "object": "file", "purpose": "batch"}),
        )

    async def _handle_v1_batch_create(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """POST /v1/batches -- Stage 2: create batch job referencing uploaded file.

        The DW provider (doubleword_provider._create_batch) sends JSON:
          {"input_file_id": "<file_id>", "endpoint": "/v1/chat/completions",
           "completion_window": "1h"}
        Returns: {"id": "<batch_id>", "status": "in_progress"}
        """
        fault = self._get_active_fault("doubleword", "/batches")
        if fault is not None:
            result = await self._apply_fault(request, fault)
            if result is not None:
                return result

        with self._state_lock:
            state = self._state
        if state == AdversaryState.OUTAGE:
            return aiohttp.web.Response(
                status=503,
                content_type="application/json",
                text=json.dumps({"error": {"message": "outage", "code": "outage"}}),
            )

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}

        input_file_id = body.get("input_file_id", "")

        with self._files_lock:
            self._v1_file_counter += 1
            batch_id = f"v1_batch_{self._v1_file_counter}"
            self._v1_batches[batch_id] = input_file_id

        return aiohttp.web.Response(
            status=200,
            content_type="application/json",
            text=json.dumps({"id": batch_id, "status": "in_progress"}),
        )

    async def _handle_v1_batch_get(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """GET /v1/batches/{batch_id} -- Stage 3: poll batch; always returns completed.

        On first call for a given batch_id, synthesises the output JSONL from
        the stored input file (via build_batch_output_line) and caches it under
        a derived output_file_id so Stage 4 can retrieve it.

        The DW provider (doubleword_provider._adaptive_poll_batch) expects:
          {"id": "<batch_id>", "status": "completed",
           "output_file_id": "<output_file_id>"}
        """
        fault = self._get_active_fault("doubleword", "/batches")
        if fault is not None:
            result = await self._apply_fault(request, fault)
            if result is not None:
                return result

        with self._state_lock:
            state = self._state
        if state == AdversaryState.OUTAGE:
            return aiohttp.web.Response(
                status=503,
                content_type="application/json",
                text=json.dumps({"error": {"message": "outage", "code": "outage"}}),
            )

        batch_id = request.match_info.get("batch_id", "unknown")
        output_file_id = f"v1_out_{batch_id}"

        # Read zero-shot flag for the batch synthesis path.
        with self._state_lock:
            _batch_simulate_zero_shot = self._simulate_zero_shot

        # Synthesise and cache the output JSONL the first time this batch is polled.
        with self._files_lock:
            if output_file_id not in self._uploaded_files:
                input_file_id = self._v1_batches.get(batch_id, "")
                input_content = self._uploaded_files.get(input_file_id, "")
                manifest = _load_chaos_manifest()
                output_lines: List[str] = []
                for raw_line in input_content.strip().split("\n"):
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        custom_id = json.loads(raw_line).get("custom_id", "unknown")
                    except (json.JSONDecodeError, AttributeError):
                        custom_id = "unknown"
                    output_lines.append(
                        build_batch_output_line(
                            custom_id, raw_line, manifest,
                            simulate_zero_shot=_batch_simulate_zero_shot,
                        )
                    )
                if not output_lines:
                    # No stored input (e.g. legacy /dw/* batch IDs): emit one generic line.
                    output_lines.append(
                        build_batch_output_line("unknown", "", None)
                    )
                self._uploaded_files[output_file_id] = "\n".join(output_lines) + "\n"

        return aiohttp.web.Response(
            status=200,
            content_type="application/json",
            text=json.dumps({
                "id": batch_id,
                "status": "completed",
                "output_file_id": output_file_id,
            }),
        )

    async def _handle_v1_file_content(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """GET /v1/files/{file_id}/content -- Stage 4: retrieve stored file content.

        Works for both input JSONL files and synthesised output JSONL files.
        The DW provider (doubleword_provider._retrieve_result) parses the
        response body as JSONL, finds the line where
        entry["custom_id"] == operation_id, then reads
        entry["response"]["body"]["choices"][0]["message"]["content"].
        """
        fault = self._get_active_fault("doubleword", "/files")
        if fault is not None:
            result = await self._apply_fault(request, fault)
            if result is not None:
                return result

        with self._state_lock:
            state = self._state
        if state == AdversaryState.OUTAGE:
            return aiohttp.web.Response(
                status=503,
                content_type="application/json",
                text=json.dumps({"error": {"message": "outage", "code": "outage"}}),
            )

        file_id = request.match_info.get("file_id", "")
        with self._files_lock:
            content = self._uploaded_files.get(file_id)

        if content is None:
            return aiohttp.web.Response(
                status=404,
                content_type="application/json",
                text=json.dumps({
                    "error": {"message": f"File {file_id!r} not found", "code": "not_found"}
                }),
            )

        return aiohttp.web.Response(
            status=200,
            content_type="application/octet-stream",
            body=content.encode("utf-8"),
        )

    # ── LEGACY /dw/* handlers (byte-identical backward compat) ──────────────── #

    async def _handle_dw_chat(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.StreamResponse:
        """POST /dw/chat/completions -- legacy backward-compat route."""
        fault = self._get_active_fault("doubleword", "/chat/completions")
        if fault is not None:
            result = await self._apply_fault(request, fault)
            if result is not None:
                return result  # type: ignore[return-value]

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        stream = body.get("stream", True)
        model = body.get("model", _HEALTHY_MODEL_ID)

        if not stream:
            return aiohttp.web.Response(  # type: ignore[return-value]
                status=200,
                content_type="application/json",
                text=json.dumps({
                    "id": _HEALTHY_COMPLETION_ID,
                    "object": "chat.completion",
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": _HEALTHY_CHAT_CONTENT},
                        "finish_reason": "stop",
                    }],
                    "usage": {
                        "prompt_tokens": 8,
                        "completion_tokens": 8,
                        "total_tokens": 16,
                    },
                }),
            )

        resp = aiohttp.web.StreamResponse(
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
        )
        await resp.prepare(request)
        chunk = {
            "id": _HEALTHY_COMPLETION_ID,
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {"role": "assistant", "content": _HEALTHY_CHAT_CONTENT},
                "finish_reason": None,
            }],
        }
        await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
        await resp.write(b"data: [DONE]\n\n")
        return resp

    async def _handle_dw_models(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """GET /dw/models -- legacy backward-compat route."""
        fault = self._get_active_fault("doubleword", "/models")
        if fault is not None:
            result = await self._apply_fault(request, fault)
            if result is not None:
                return result

        return aiohttp.web.Response(
            status=200,
            content_type="application/json",
            text=json.dumps({
                "object": "list",
                "data": [
                    {
                        "id": _HEALTHY_MODEL_ID,
                        "object": "model",
                        "created": 1700000000,
                        "owned_by": "adversary-stub",
                    }
                ],
            }),
        )

    async def _handle_dw_batch_create(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        fault = self._get_active_fault("doubleword", "/batches")
        if fault is not None:
            result = await self._apply_fault(request, fault)
            if result is not None:
                return result
        return aiohttp.web.Response(
            status=200,
            content_type="application/json",
            text=json.dumps({"id": "batch_adversary_ok", "status": "in_progress"}),
        )

    async def _handle_dw_batch_get(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        fault = self._get_active_fault("doubleword", "/batches")
        if fault is not None:
            result = await self._apply_fault(request, fault)
            if result is not None:
                return result
        batch_id = request.match_info.get("batch_id", "unknown")
        return aiohttp.web.Response(
            status=200,
            content_type="application/json",
            text=json.dumps({
                "id": batch_id,
                "status": "completed",
                "output_file_id": "file_adversary_ok",
            }),
        )

    async def _handle_dw_file_content(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        fault = self._get_active_fault("doubleword", "/files")
        if fault is not None:
            result = await self._apply_fault(request, fault)
            if result is not None:
                return result
        return aiohttp.web.Response(
            status=200,
            content_type="application/octet-stream",
            body=b'{"choices":[{"message":{"content":"adversary stub"}}]}\n',
        )

    async def _handle_dw_files(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        fault = self._get_active_fault("doubleword", "/files")
        if fault is not None:
            result = await self._apply_fault(request, fault)
            if result is not None:
                return result
        return aiohttp.web.Response(
            status=200,
            content_type="application/json",
            text=json.dumps({"id": "file_adversary_ok", "object": "file", "purpose": "batch"}),
        )

    # ── Prime / Reactor handlers ─────────────────────────────────────────────── #

    async def _handle_prime(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        path_info = request.match_info.get("path_info", "")
        endpoint = f"/{path_info}".rstrip("/") or "/"
        fault = self._get_active_fault("prime", endpoint)
        if fault is not None:
            result = await self._apply_fault(request, fault)
            if result is not None:
                return result
        return aiohttp.web.Response(
            status=200,
            content_type="application/json",
            text=json.dumps({"status": "ok", "provider": "prime-adversary-stub"}),
        )

    async def _handle_reactor(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        path_info = request.match_info.get("path_info", "")
        endpoint = f"/{path_info}".rstrip("/") or "/"
        fault = self._get_active_fault("reactor", endpoint)
        if fault is not None:
            result = await self._apply_fault(request, fault)
            if result is not None:
                return result
        return aiohttp.web.Response(
            status=200,
            content_type="application/json",
            text=json.dumps({"status": "ok", "provider": "reactor-adversary-stub"}),
        )
