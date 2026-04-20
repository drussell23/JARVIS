"""GENERAL subagent LLM driver (Phase C Slice 1a Step 3).

The canonical ``llm_driver`` implementation for
``AgenticGeneralSubagent``. Wires together:

  * ``ScopedToolBackend`` (pre-linguistic tool allowlist — the
    mechanical lock)
  * ``render_general_system_prompt(invocation)`` (the cognitive map)
  * ``ToolLoopCoordinator`` (Venom's multi-turn tool loop)
  * A provider resolved from ``payload["primary_provider_name"]`` via
    the factory's injected provider registry

Contract: ``run_general_tool_loop(payload, project_root,
provider_registry, policy=None) -> exec_trace: Dict[str, Any]``. The
``exec_trace`` shape matches what ``AgenticGeneralSubagent._execute_body``
expects: ``status / raw_output / tool_calls_made / tool_diversity /
cost_usd / provider_used / fallback_triggered``.

Failure modes are all structured:

  * No provider resolvable → ``status="no_provider_wired"``, raw_output
    names the missing provider.
  * Tool loop raises → ``status="tool_loop_error"``, raw_output carries
    sanitized exception class + message.
  * Final text unparsable as general.final.v1 → ``status="malformed_final"``,
    raw_output carries the raw text truncated for audit.
  * Tool loop hits round ceiling without final answer →
    ``status="max_rounds_exhausted"``, raw_output carries last raw.

Every path is observer-safe: no exceptions propagate past the driver
boundary. The AgenticGeneralSubagent's outer ``try/except`` treats any
escape as ``_internal_failure_result``, but the driver itself converts
every expected failure into a structured exec_trace so the observer
pipeline produces a normal-shaped record.

Flag: ``JARVIS_GENERAL_LLM_DRIVER_ENABLED`` (default false) controls
whether the factory ships this driver or leaves the stub path
active. When false, ``AgenticGeneralSubagent.llm_driver`` stays None
and ``_execute_body`` returns the existing
``NOT_IMPLEMENTED_NEEDS_LLM_WIRING`` placeholder.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------

_DRIVER_FLAG = "JARVIS_GENERAL_LLM_DRIVER_ENABLED"
_DEFAULT_MAX_ROUNDS = int(
    os.environ.get("JARVIS_GENERAL_LLM_MAX_ROUNDS", "6")
)
_DEFAULT_TOOL_TIMEOUT_S = float(
    os.environ.get("JARVIS_GENERAL_LLM_TOOL_TIMEOUT_S", "30.0")
)
_FINAL_SCHEMA_VERSION = "general.final.v1"


def driver_enabled() -> bool:
    """Re-read ``JARVIS_GENERAL_LLM_DRIVER_ENABLED`` at call-time.

    Default: **``true``** (graduated 2026-04-20 after the Phase C
    Slice 1b live battle test matrix — 3/3 safety properties proven
    against the real Claude API: allowlist enforcement, scope
    containment, mutation cap honored). Set the env var explicitly to
    ``"false"`` to opt back into the Phase B stub path.
    """
    return os.environ.get(_DRIVER_FLAG, "true").strip().lower() in (
        "true", "1", "yes",
    )


# ---------------------------------------------------------------------------
# Final-answer parser
# ---------------------------------------------------------------------------

def parse_general_final_answer(text: str) -> Optional[Dict[str, Any]]:
    """Parse a GENERAL-schema final-answer JSON string.

    Returns a dict on success; ``None`` on any parse/validation failure.
    Caller treats ``None`` as "not a final answer yet" — the tool loop
    already handled intermediate tool-call rounds via
    ``_parse_tool_call_response``.

    The expected shape is the ``general.final.v1`` contract the system
    prompt pins:
      ``status``: one of {"completed", "blocked_by_scope",
                          "blocked_by_tools", "aborted"}
      ``summary``: str, caller truncates downstream
      ``findings``: list of {file, evidence} dicts (may be empty)
      ``mutations_performed``: int
      ``blocked_reason``: str (non-empty when status != completed)

    Strict on schema_version — anything other than ``general.final.v1``
    returns None so a stray tool-call response or unrelated JSON can't
    masquerade as a final answer.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    # Be generous about stripping prose wrapping — many models emit
    # ```json ... ``` despite being told not to. Find the first '{' and
    # the matching last '}' and try that.
    stripped = text.strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        return None
    blob = stripped[start : end + 1]
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema_version") != _FINAL_SCHEMA_VERSION:
        return None
    status = data.get("status")
    if status not in ("completed", "blocked_by_scope",
                      "blocked_by_tools", "aborted"):
        return None
    return data


def final_answer_to_exec_trace(
    final: Dict[str, Any],
    *,
    tool_calls_made: int,
    tool_diversity: int,
    cost_usd: float,
    provider_used: str,
    raw_text: str,
) -> Dict[str, Any]:
    """Map a parsed ``general.final.v1`` + tool-loop metrics into the
    ``exec_trace`` dict the AgenticGeneralSubagent executor expects.

    The executor then wraps ``raw_output`` in the quarantine fence and
    emits a SubagentResult. This function is pure — no I/O, no
    exception handling beyond what's needed for the str() coercions.
    """
    return {
        "status": "completed" if str(final.get("status")) == "completed"
                   else f"final_{final.get('status', 'unknown')}",
        "raw_output": raw_text,  # the model's own JSON — preserved for audit
        "tool_calls_made": int(tool_calls_made),
        "tool_diversity": int(tool_diversity),
        "cost_usd": float(cost_usd),
        "provider_used": str(provider_used or "llm_driver"),
        "fallback_triggered": False,
        # Additional fields carried for downstream observability:
        "final_status": str(final.get("status", "")),
        "final_summary": str(final.get("summary", ""))[:500],
        "final_findings_count": len(final.get("findings", []) or []),
        "final_mutations_performed": int(final.get("mutations_performed", 0) or 0),
        "final_blocked_reason": str(final.get("blocked_reason", "") or ""),
    }


def _failure_exec_trace(
    *, status: str, raw_output: str, provider_used: str = "llm_driver",
) -> Dict[str, Any]:
    """Uniform shape for driver-side failures."""
    return {
        "status": status,
        "raw_output": raw_output[:2048],
        "tool_calls_made": 0,
        "tool_diversity": 0,
        "cost_usd": 0.0,
        "provider_used": provider_used,
        "fallback_triggered": False,
    }


# ---------------------------------------------------------------------------
# Core entrypoint — wired into AgenticGeneralSubagent via factory
# ---------------------------------------------------------------------------

async def run_general_tool_loop(
    payload: Dict[str, Any],
    *,
    project_root: Path,
    provider_registry: Callable[[str], Any],
    policy: Optional[Any] = None,
) -> Dict[str, Any]:
    """The canonical GENERAL LLM driver.

    Orchestrates system-prompt rendering + scope-gated backend +
    tool-loop + final-answer parsing. Returns the ``exec_trace`` dict
    the executor feeds into its output-quarantine + SubagentResult
    pipeline.

    Parameters
    ----------
    payload:
        Shape emitted by ``AgenticGeneralSubagent._execute_body``:
            sub_id: str
            invocation: Dict with operation_scope / allowed_tools /
                        max_mutations / invocation_reason / goal /
                        parent_op_risk_tier
            project_root: str
            primary_provider_name: str  (enriched — this slice adds it)
            fallback_provider_name: str (enriched)
            deadline: Optional[float]    (monotonic; enriched)
            max_rounds: Optional[int]    (enriched, defaults to 6)
            tool_timeout_s: Optional[float] (enriched, defaults to 30)
    project_root:
        Closed over by the factory at construction time. Passed
        explicitly here too so the helper stays standalone-testable.
    provider_registry:
        ``Callable[[str], provider]`` — given a provider name, returns
        an object with an ``async generate(ctx, deadline) -> str``
        (or equivalent) method. Resolved lazily so provider imports
        don't happen at module load.
    policy:
        The global ``GoverningToolPolicy`` instance. Passed through to
        ``ToolLoopCoordinator`` unchanged. When ``None`` (unit tests),
        a permissive stub is used — the ScopedToolBackend is the only
        gate in that configuration, which is exactly what the tests
        pin. Production callers always supply the real policy.
    """
    # Lazy imports so this module stays cheap at import-time. Heavy
    # modules (tool_executor) only come in when driver is actually run.
    from backend.core.ouroboros.governance.scoped_tool_access import (
        ScopedToolGate,
        ToolScope,
    )
    from backend.core.ouroboros.governance.scoped_tool_backend import (
        ScopedToolBackend,
    )
    from backend.core.ouroboros.governance.subagent_contracts import (
        render_general_system_prompt,
    )
    from backend.core.ouroboros.governance.tool_executor import (
        AsyncProcessToolBackend,
        GoverningToolPolicy,
        ToolLoopCoordinator,
    )

    sub_id = str(payload.get("sub_id", "sub-unknown"))
    invocation = dict(payload.get("invocation", {}) or {})
    primary_name = str(payload.get("primary_provider_name", "") or "")
    # Defensive default-resolution: ``AgenticGeneralSubagent._execute_body``
    # passes max_rounds/tool_timeout_s/deadline as explicit None when
    # ctx doesn't carry overrides, signalling "driver picks its own
    # default". ``dict.get(key, default)`` doesn't fall back when the
    # key is present with a None value, so we treat-None-as-absent
    # explicitly here. Caught by Slice 1b live test; prior unit tests
    # masked it by supplying explicit int/float values.
    _mr = payload.get("max_rounds")
    max_rounds = int(_mr) if _mr is not None else _DEFAULT_MAX_ROUNDS
    _tt = payload.get("tool_timeout_s")
    tool_timeout_s = float(_tt) if _tt is not None else _DEFAULT_TOOL_TIMEOUT_S
    deadline = payload.get("deadline")  # monotonic float or None
    if deadline is None:
        # Conservative default: now + (max_rounds * tool_timeout_s) + 10s slack.
        deadline = time.monotonic() + max_rounds * tool_timeout_s + 10.0

    # 1. Resolve provider via registry. Missing provider is a structured
    # failure, not an exception.
    try:
        provider = provider_registry(primary_name)
    except Exception as exc:  # noqa: BLE001 — never raise past driver
        return _failure_exec_trace(
            status="no_provider_wired",
            raw_output=(
                f"provider_registry({primary_name!r}) raised "
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ),
        )
    if provider is None:
        return _failure_exec_trace(
            status="no_provider_wired",
            raw_output=(
                f"provider_registry returned None for {primary_name!r} — "
                "caller must wire a provider before driver is invoked"
            ),
        )

    # 2. Build the ScopedToolScope from invocation's allowed_tools.
    allowed_tools: FrozenSet[str] = frozenset(
        str(t) for t in (invocation.get("allowed_tools", ()) or ())
    )
    max_mutations = int(invocation.get("max_mutations", 0) or 0)
    scope = ToolScope(
        allowed_tools=allowed_tools,
        read_only=(max_mutations == 0),
    )
    gate = ScopedToolGate(scope)

    # 3. Wrap the real backend with the scope gate. ScopedToolBackend
    # rejects any tool NOT in allowed_tools at the backend boundary —
    # BEFORE the global policy engine sees the call. Defense in depth.
    #
    # AsyncProcessToolBackend requires a concurrency semaphore (not a
    # project_root — the repo root flows through the policy context
    # per-call). Size via JARVIS_GENERAL_LLM_BACKEND_CONCURRENCY
    # (default 2) — bounded because GENERAL is a single-task subagent,
    # not a throughput layer.
    try:
        import asyncio as _asyncio
        backend_concurrency = max(1, int(os.environ.get(
            "JARVIS_GENERAL_LLM_BACKEND_CONCURRENCY", "2",
        )))
        inner_backend = AsyncProcessToolBackend(
            semaphore=_asyncio.Semaphore(backend_concurrency),
        )
    except Exception as exc:  # noqa: BLE001
        return _failure_exec_trace(
            status="backend_init_error",
            raw_output=(
                f"AsyncProcessToolBackend init failed: "
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ),
        )
    scoped_backend = ScopedToolBackend(inner=inner_backend, gate=gate)

    # 4. Build a policy if caller didn't supply one (test-only path).
    effective_policy = policy
    if effective_policy is None:
        try:
            effective_policy = GoverningToolPolicy(
                repo_roots={"jarvis": project_root},
            )
        except Exception as exc:  # noqa: BLE001
            return _failure_exec_trace(
                status="policy_init_error",
                raw_output=(
                    f"GoverningToolPolicy init failed: "
                    f"{type(exc).__name__}: {str(exc)[:200]}"
                ),
            )

    # 5. Construct the tool loop.
    coordinator = ToolLoopCoordinator(
        backend=scoped_backend,
        policy=effective_policy,
        max_rounds=max_rounds,
        tool_timeout_s=tool_timeout_s,
    )

    # 6. Render system prompt and build the generate_fn that bridges
    # the provider's text-in-text-out SDK surface to
    # ToolLoopCoordinator's (str) -> str contract.
    #
    # TECH DEBT — ClaudeProvider doesn't expose a public
    # generate_text(prompt, system) method; we reach into
    # ``provider._client`` (the AsyncAnthropic SDK instance) directly.
    # Tracked in project_phase_b_step2_deferred.md ticket 6.
    # Signature-drift pin: tests/governance/test_general_driver.py
    # asserts _client exposes ``.messages.create`` so a provider
    # refactor that moves the SDK surface elsewhere fails loudly
    # rather than silently breaking GENERAL.
    system_prompt = render_general_system_prompt(invocation)

    async def _generate_fn(prompt: str) -> str:
        """Reach into provider._client for text-in-text-out SDK access.

        Phase B pattern: the existing ``_generate_raw`` closures in
        providers.py:3712+ use the same inner-client pattern. Here it's
        a private attribute because ClaudeProvider's high-level
        ``generate(ctx, deadline)`` isn't a fit for the tool loop's
        string-based contract.

        ClaudeProvider lazy-initializes ``_client`` on first access via
        ``_ensure_client()`` — we call it explicitly here because the
        driver path never triggers the high-level ``generate()`` that
        would otherwise force the init as a side effect.

        Returns the concatenated text content of all ``TextBlock``s
        in the response. Raises on any SDK error; caller's exception
        handler catches and emits ``status=tool_loop_error``.
        """
        # Force lazy init if available (ClaudeProvider pattern).
        ensure = getattr(provider, "_ensure_client", None)
        if callable(ensure):
            try:
                ensure()
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"provider {primary_name!r} _ensure_client failed: "
                    f"{type(exc).__name__}: {str(exc)[:200]}"
                ) from exc
        client = getattr(provider, "_client", None)
        if client is None:
            raise RuntimeError(
                f"provider {primary_name!r} ._client is None after "
                "_ensure_client — likely recycled or uninitialized"
            )
        model_name = str(getattr(provider, "_model", "") or "claude-sonnet-4-5-20250929")
        # GENERAL tool rounds vary widely. Most tool calls (read_file /
        # search_code args) are ~1K tokens. But ``edit_file`` ships the
        # FULL file content in its arguments — a 500-line file is easily
        # 6-8K tokens. Slice 1b live Test 2 hit truncation at 4096 mid-
        # edit_file JSON; bump the default to 8192 so typical full-file
        # writes complete. Still bounded to keep cost predictable;
        # operators can tune via env.
        max_output = int(os.environ.get(
            "JARVIS_GENERAL_LLM_MAX_OUTPUT_TOKENS", "8192",
        ))
        msg = await client.messages.create(
            model=model_name,
            max_tokens=max_output,
            system=system_prompt,
            messages=[{"role": "user", "content": prompt}],
        )
        # Extract text from all text content blocks.
        parts: List[str] = []
        for block in getattr(msg, "content", []) or []:
            txt = getattr(block, "text", None)
            if isinstance(txt, str):
                parts.append(txt)
        return "".join(parts)

    user_prompt = (
        f"# GENERAL subagent {sub_id}\n\n"
        f"Your work starts below. Call tools from the allowlist only. "
        f"Emit the final-answer JSON when done.\n\n"
        f"## Goal (restated)\n"
        f"{invocation.get('goal', '<missing>')}"
    )

    # 7. Lazy import the canonical tool-call response parser.
    from backend.core.ouroboros.governance.providers import (
        _parse_tool_call_response,
    )

    # 8. Run the tool loop.
    repo = str(invocation.get("primary_repo", "jarvis") or "jarvis")

    try:
        final_text, records = await coordinator.run(
            prompt=user_prompt,
            generate_fn=_generate_fn,
            parse_fn=_parse_tool_call_response,
            repo=repo,
            op_id=sub_id,
            deadline=deadline,
            risk_tier=None,  # GENERAL ignores risk_tier (scope is the gate)
            is_read_only=(max_mutations == 0),
        )
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "[GeneralDriver] tool-loop error sub=%s exc=%s msg=%.200s",
            sub_id, type(exc).__name__, str(exc),
        )
        return _failure_exec_trace(
            status="tool_loop_error",
            raw_output=(
                f"ToolLoopCoordinator.run raised "
                f"{type(exc).__name__}: {str(exc)[:512]}"
            ),
            provider_used=primary_name,
        )

    # 9. Aggregate tool-loop metrics.
    tool_calls_made = len(records) if records else 0
    tool_names = tuple(
        getattr(r, "tool_name", "") for r in (records or ())
        if getattr(r, "tool_name", "")
    )
    # Reuse the existing classify_tools helper if available; compute
    # unique class count from our own minimal map otherwise.
    try:
        from backend.core.ouroboros.governance.subagent_contracts import (
            classify_tools,
        )
        tool_diversity = len(classify_tools(tool_names))
    except Exception:
        tool_diversity = len(set(tool_names))
    cost_usd = 0.0  # Coordinator.run doesn't return cost; provider
                    # aggregation is parent's concern. Driver reports
                    # 0.0 and lets the executor report provider-level
                    # metrics separately if needed.

    # 10. Parse final answer.
    final = parse_general_final_answer(final_text)
    if final is None:
        logger.info(
            "[GeneralDriver] malformed-final sub=%s tool_calls=%d "
            "final_text_head=%.200s",
            sub_id, tool_calls_made, final_text or "",
        )
        # Preserve tool-loop metrics even on malformed final so operator
        # observability survives parse failure. _failure_exec_trace
        # defaults tool_calls_made to 0; override here.
        trace = _failure_exec_trace(
            status="malformed_final",
            raw_output=final_text or "",
            provider_used=primary_name,
        )
        trace["tool_calls_made"] = tool_calls_made
        trace["tool_diversity"] = tool_diversity
        return trace

    # 11. Map to exec_trace and return.
    return final_answer_to_exec_trace(
        final,
        tool_calls_made=tool_calls_made,
        tool_diversity=tool_diversity,
        cost_usd=cost_usd,
        provider_used=primary_name,
        raw_text=final_text,
    )
