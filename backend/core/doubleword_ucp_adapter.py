"""DoubleWord UCP Adapter + Adaptive Context Transformer (Phase 12, Slice A).

A LOCALIZED anti-corruption layer (mandate 1): it does NOT modify or
duplicate the 7,000-line governance ``doubleword_provider`` — it wraps the
EXISTING ``DoublewordProvider().complete_sync`` (the Functions-not-Agents
path), leaving the Aegis transport + token leases fully intact. The UCP
command path talks to this adapter through the provider-agnostic
``active_failover`` router; the adapter maps a plain text-completion
request onto ``complete_sync`` and returns the answer text.

Adaptive Context Transformer (mandate 2a — Anti-Drift Guard): Claude and
DoubleWord speak different dialects. A Claude prompt carries XML reasoning
scaffolding (``<thinking>`` / ``<scratchpad>``), instruction tags, and
markdown fences that would fragment DoubleWord's context window and garble
UCP parsing on a live failover. The transformer strips the Claude-only
scaffolding, isolates the semantic core, and hands DoubleWord clean,
role-bound text (``prompt`` + ``system_prompt``) native to its
``/v1/chat/completions`` schema — no hardcoded model overrides.

DRY (mandate 3): reuses ``DoublewordProvider`` (the same construction the
rest of the repo uses) + ``complete_sync`` + its lease managers. No second
transport wrapper.

Every public entry point NEVER raises out of the adapter (failures surface
as a raised provider error the failover router classifies, or an empty
result that advances the router).
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger("Jarvis.DoubleWordUCPAdapter")


# ---------------------------------------------------------------------------
# Adaptive Context Transformer (Anti-Drift Guard)
# ---------------------------------------------------------------------------

@dataclass
class TransformedContext:
    prompt: str
    system_prompt: str


class AdaptiveContextTransformer:
    """Sanitize a Claude-dialect payload into DoubleWord-native role-bound
    text. Pure + stateless; NEVER raises."""

    # Claude-only reasoning blocks — dropped ENTIRELY (they are not the
    # request; forwarding them fragments DW's window + confuses parsing).
    _REASONING_BLOCK_RE = re.compile(
        r"<\s*(thinking|scratchpad|reasoning|internal|monologue|plan)\s*>"
        r".*?</\s*\1\s*>", re.DOTALL | re.IGNORECASE)
    # Residual XML instruction tags — stripped, INNER TEXT KEPT
    # (``<instruction>do X</instruction>`` → ``do X``).
    _XML_TAG_RE = re.compile(r"</?\s*[a-zA-Z_][\w:-]*\s*/?>")
    # Markdown code fences — the fence markers only (keep the code text).
    _MD_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n?|~~~[a-zA-Z0-9_+-]*\n?")
    _MULTI_BLANK_RE = re.compile(r"\n{3,}")

    def _sanitize(self, text: Optional[str]) -> str:
        try:
            t = text or ""
            t = self._REASONING_BLOCK_RE.sub("", t)   # drop Claude reasoning
            t = self._XML_TAG_RE.sub("", t)           # strip tags, keep inner
            t = self._MD_FENCE_RE.sub("", t)          # strip fence markers
            t = self._MULTI_BLANK_RE.sub("\n\n", t)   # collapse blank runs
            return t.strip()
        except Exception:  # noqa: BLE001
            return (text or "").strip()

    def transform(
        self, prompt: str, system_prompt: str = "",
    ) -> TransformedContext:
        """Isolate the semantic core → clean (prompt, system_prompt) for
        ``complete_sync``. NEVER raises."""
        return TransformedContext(
            prompt=self._sanitize(prompt),
            system_prompt=self._sanitize(system_prompt),
        )


# ---------------------------------------------------------------------------
# DoubleWord UCP Adapter (anti-corruption layer over complete_sync)
# ---------------------------------------------------------------------------

def _ucp_max_tokens() -> int:
    try:
        return max(64, int(os.environ.get("JARVIS_UCP_DW_MAX_TOKENS", "512")))
    except (TypeError, ValueError):
        return 512


def _ucp_timeout_s() -> float:
    try:
        return max(2.0, float(os.environ.get("JARVIS_UCP_DW_TIMEOUT_S", "20")))
    except (TypeError, ValueError):
        return 20.0


class DoubleWordUCPAdapter:
    """Maps a UCP text-completion request onto the EXISTING
    ``DoublewordProvider.complete_sync``. Lazily constructs the provider
    the same way the rest of the repo does (``DoublewordProvider()`` reads
    ``DOUBLEWORD_API_KEY`` + Aegis at instance time). Inject ``provider``
    for tests."""

    def __init__(self, *, provider: Any = None,
                 transformer: Optional[AdaptiveContextTransformer] = None,
                 caller_id: str = "ucp_command_failover") -> None:
        self._provider = provider
        self._transformer = transformer or AdaptiveContextTransformer()
        self._caller_id = caller_id

    def _get_provider(self) -> Any:
        if self._provider is None:
            # DRY — the same construction backend/main.py + intent_prompter
            # use; env + Aegis resolve the transport + leases.
            from backend.core.ouroboros.governance.doubleword_provider import (
                DoublewordProvider,
            )
            self._provider = DoublewordProvider()
        return self._provider

    @staticmethod
    def _extract_fields(context: Any) -> TransformedContext:
        """Pull (prompt, system_prompt) out of the UCP context (dict or
        string), tolerating the common key names."""
        if isinstance(context, str):
            return TransformedContext(prompt=context, system_prompt="")
        if isinstance(context, dict):
            prompt = (context.get("prompt") or context.get("text")
                      or context.get("command") or context.get("message") or "")
            system = (context.get("system_prompt") or context.get("system")
                      or context.get("system_message") or "")
            return TransformedContext(prompt=str(prompt), system_prompt=str(system))
        return TransformedContext(prompt=str(context or ""), system_prompt="")

    async def complete(self, context: Any) -> str:
        """The failover ``Provider.call``: transform the Claude-dialect
        context → DoubleWord-native, run ``complete_sync``, return the
        answer text. Raises on a provider fault so the failover router can
        classify it (a raised fault is the contract for retriable
        detection)."""
        raw = self._extract_fields(context)
        clean = self._transformer.transform(raw.prompt, raw.system_prompt)
        provider = self._get_provider()
        result = await provider.complete_sync(
            clean.prompt,
            system_prompt=clean.system_prompt,
            caller_id=self._caller_id,
            max_tokens=_ucp_max_tokens(),
            timeout_s=_ucp_timeout_s(),
        )
        text = getattr(result, "content", None)
        if text is None and isinstance(result, dict):
            text = result.get("content")
        return str(text or "").strip()

    def as_provider(self):
        """A ``active_failover.Provider`` named 'doubleword' bound to this
        adapter — the failover tier for the UCP."""
        from backend.core.active_failover import Provider
        return Provider(name="doubleword", call=self.complete)


def build_ucp_failover_providers(claude_call, *, dw_adapter=None):
    """The UCP provider chain: [Claude primary, DoubleWord failover]. The
    router reroutes the SAME context to DoubleWord on a retriable Claude
    fault (402/429/5xx or the credit-balance 400). NEVER raises."""
    from backend.core.active_failover import Provider
    adapter = dw_adapter or DoubleWordUCPAdapter()
    return [
        Provider(name="claude", call=claude_call),
        adapter.as_provider(),
    ]


__all__ = [
    "TransformedContext", "AdaptiveContextTransformer",
    "DoubleWordUCPAdapter", "build_ucp_failover_providers",
]
