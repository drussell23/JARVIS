"""Phase 12 Slice G — Dynamic Capability Verification + Modality Micro-Probe.

Two ground-truth modality signals (operator-mandated 2026-04-27 — no
regex on model_id allowed):

  1. **Metadata extraction** — parse the raw ``/models`` response per
     model for explicit capability flags. Look at multiple shapes:
       * ``capabilities`` (array of strings or dict of bools)
       * ``architecture`` / ``architectures`` (HF-style)
       * ``task`` / ``pipeline_tag`` (HF-compat)
       * ``endpoints`` (which endpoints DW exposes for this model)

  2. **Micro-probe fallback** — when metadata is absent or ambiguous,
     fire a 1-token ``/chat/completions`` request to discover the
     model's actual modality verdict from the server itself.

Both signals feed the ``ModalityLedger``. The classifier reads the
ledger to exclude NON_CHAT models from generative routes.

Authority surface:
  * ``extract_metadata_verdict(card) -> Optional[bool]`` — pure func
    over a ``ModelCard``'s ``raw_metadata_json``
  * ``async micro_probe(...) -> ProbeResult`` — fires 1-token probe
  * ``async verify_catalog_modalities(...) -> VerifyResult`` —
    end-to-end verification of a fresh catalog snapshot

NEVER reads model_id strings to infer modality. Verdicts come from
either the server's metadata payload OR the server's response to an
actual chat-completions request — both ground truth.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from backend.core.ouroboros.governance.dw_catalog_client import (
    CatalogSnapshot, ModelCard,
)
from backend.core.ouroboros.governance.dw_modality_ledger import (
    ModalityLedger,
    SOURCE_PROBE_2XX,
    SOURCE_PROBE_4XX,
    VERDICT_CHAT_CAPABLE,
    VERDICT_NON_CHAT,
    VERDICT_UNKNOWN,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Metadata extraction (no regex on model_id)
# ---------------------------------------------------------------------------


# Capability tokens that explicitly indicate chat-completions support.
# These are checked against EXPLICIT METADATA FIELDS, never substring-
# matched against model_id.
_CHAT_CAPABLE_TOKENS = frozenset({
    "chat", "chat_completion", "chat_completions",
    "text-generation", "text_generation",
    "conversational", "instruct", "instruction-following",
})


# Tokens that indicate the model is NOT a chat-completions endpoint.
# Same rule: matched against METADATA fields only.
_NON_CHAT_TOKENS = frozenset({
    "embedding", "embeddings", "feature-extraction",
    "image-classification", "object-detection", "image-segmentation",
    "depth-estimation", "ocr", "vision-only",
    "speech-recognition", "audio-classification",
    "text-to-speech", "automatic-speech-recognition",
    "fill-mask",  # masked-language-modeling, not generative
})


def _tokenize_capability_field(value: Any) -> List[str]:
    """Normalize whatever shape DW exposes (list, dict, single string)
    into a flat list of lowercase tokens. NEVER raises."""
    out: List[str] = []
    try:
        if isinstance(value, str):
            out.append(value.strip().lower())
        elif isinstance(value, list):
            for v in value:
                if isinstance(v, str):
                    out.append(v.strip().lower())
        elif isinstance(value, Mapping):
            for k, v in value.items():
                # dict shape: {"chat": true, "embedding": false}
                if v is True or v == "true":
                    if isinstance(k, str):
                        out.append(k.strip().lower())
    except Exception:  # noqa: BLE001 — defensive
        pass
    return [t for t in out if t]


def extract_metadata_verdict(card: ModelCard) -> Optional[bool]:
    """Return a verdict from explicit metadata fields, or ``None`` when
    metadata is absent / ambiguous (caller should fall back to the
    micro-probe).

    Ground-truth signals checked, in order:
      1. ``endpoints`` — if it explicitly lists ``/chat/completions``
         (or chat as a string), CHAT_CAPABLE
      2. ``capabilities`` — token in _CHAT_CAPABLE_TOKENS → True;
         token in _NON_CHAT_TOKENS → False
      3. ``architecture`` / ``architectures`` — same token rules
      4. ``task`` / ``pipeline_tag`` — same token rules

    Returns:
      * ``True`` — explicit chat capability signal in metadata
      * ``False`` — explicit non-chat signal in metadata
      * ``None`` — no recognized signal (defer to micro-probe)

    NEVER reads the model id field for inference. NEVER raises."""
    try:
        raw = json.loads(card.raw_metadata_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(raw, Mapping):
        return None

    # 1. Explicit endpoints list
    endpoints = raw.get("endpoints")
    if isinstance(endpoints, (list, tuple)):
        for ep in endpoints:
            if not isinstance(ep, str):
                continue
            ep_lower = ep.lower()
            if "/chat/completions" in ep_lower or ep_lower.strip() == "chat":
                return True
            if (
                "/embeddings" in ep_lower
                or "/audio" in ep_lower
                or "/images" in ep_lower
            ) and "/chat" not in ep_lower:
                # Endpoint list explicitly excludes chat
                pass  # don't return False yet — continue to other fields

    # Aggregate tokens from all known fields
    fields_to_check: List[List[str]] = []
    for fname in (
        "capabilities", "architecture", "architectures",
        "task", "pipeline_tag", "tasks",
    ):
        value = raw.get(fname)
        tokens = _tokenize_capability_field(value)
        if tokens:
            fields_to_check.append(tokens)

    if not fields_to_check:
        return None  # no recognized metadata fields

    all_tokens = [t for tokens in fields_to_check for t in tokens]
    has_chat = any(t in _CHAT_CAPABLE_TOKENS for t in all_tokens)
    has_non_chat = any(t in _NON_CHAT_TOKENS for t in all_tokens)

    # If both signals present → ambiguous (defer to probe)
    if has_chat and has_non_chat:
        return None
    if has_chat:
        return True
    if has_non_chat:
        return False
    return None  # nothing recognized → defer to probe


# ---------------------------------------------------------------------------
# Micro-probe — 1-token /chat/completions request
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of one micro-probe attempt against a single model.

    ``verdict`` is one of VERDICT_CHAT_CAPABLE / VERDICT_NON_CHAT /
    VERDICT_UNKNOWN. UNKNOWN means transport-level failure (5xx,
    timeout, DNS) — caller should retry on next discovery cycle, NOT
    classify the model.
    """
    model_id: str
    verdict: str
    status_code: int
    response_body_excerpt: str
    latency_ms: int
    failure_reason: Optional[str] = None


_PROBE_TIMEOUT_S = 15.0
# Body markers re-used from DoublewordInfraError.is_modality_error()
# so the verdict logic is consistent across the probe path AND the
# real-dispatch path (both classify the same response the same way).
_MODALITY_MARKERS = (
    "does not support chat",
    "not a chat model",
    "endpoint not supported",
    "embedding only",
    "model_not_chat",
    "task mismatch",
    "wrong endpoint",
    "unsupported endpoint",
    "model is not available for chat",
)


async def micro_probe(
    *,
    session: Any,
    base_url: str,
    api_key: str,
    model_id: str,
    timeout_s: float = _PROBE_TIMEOUT_S,
) -> ProbeResult:
    """Fire a 1-token ``/chat/completions`` request against ``model_id``.

    Verdict logic:
      * 2xx → CHAT_CAPABLE
      * 4xx (400/404/422) + body marker → NON_CHAT
      * 4xx without marker → UNKNOWN (don't permanently exclude on
        ambiguous 4xx — could be transient quota/auth)
      * 401/403 → UNKNOWN with auth-failure reason (don't blame the
        model for our credential problem)
      * 5xx/timeout/transport → UNKNOWN (retry on next cycle)

    NEVER raises — all errors return UNKNOWN with failure_reason set."""
    t0 = time.monotonic()
    body = {
        "model": model_id,
        "messages": [
            {"role": "user", "content": "1"},
        ],
        "max_tokens": 1,
        "temperature": 0,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    url = f"{base_url.rstrip('/')}/chat/completions"

    def _build_unknown(reason: str) -> ProbeResult:
        return ProbeResult(
            model_id=model_id,
            verdict=VERDICT_UNKNOWN,
            status_code=0,
            response_body_excerpt="",
            latency_ms=int((time.monotonic() - t0) * 1000),
            failure_reason=reason,
        )

    try:
        async with session.post(
            url, headers=headers, json=body, timeout=timeout_s,
        ) as resp:
            status = resp.status
            try:
                resp_text = await resp.text()
            except Exception:  # noqa: BLE001 — defensive
                resp_text = ""
            latency_ms = int((time.monotonic() - t0) * 1000)
            body_excerpt = (resp_text or "")[:512]
            body_lower = body_excerpt.lower()
            if 200 <= status < 300:
                return ProbeResult(
                    model_id=model_id,
                    verdict=VERDICT_CHAT_CAPABLE,
                    status_code=status,
                    response_body_excerpt=body_excerpt,
                    latency_ms=latency_ms,
                )
            if status in (401, 403):
                # Auth failure — don't blame the model
                return ProbeResult(
                    model_id=model_id,
                    verdict=VERDICT_UNKNOWN,
                    status_code=status,
                    response_body_excerpt=body_excerpt,
                    latency_ms=latency_ms,
                    failure_reason=f"auth_{status}",
                )
            if status in (400, 404, 422):
                if any(m in body_lower for m in _MODALITY_MARKERS):
                    return ProbeResult(
                        model_id=model_id,
                        verdict=VERDICT_NON_CHAT,
                        status_code=status,
                        response_body_excerpt=body_excerpt,
                        latency_ms=latency_ms,
                    )
                # 4xx without marker → ambiguous, leave unknown
                return ProbeResult(
                    model_id=model_id,
                    verdict=VERDICT_UNKNOWN,
                    status_code=status,
                    response_body_excerpt=body_excerpt,
                    latency_ms=latency_ms,
                    failure_reason=f"4xx_no_marker_{status}",
                )
            if status in (429, 500, 502, 503, 504):
                # Transient — retry next cycle
                return ProbeResult(
                    model_id=model_id,
                    verdict=VERDICT_UNKNOWN,
                    status_code=status,
                    response_body_excerpt=body_excerpt,
                    latency_ms=latency_ms,
                    failure_reason=f"transient_{status}",
                )
            return ProbeResult(
                model_id=model_id,
                verdict=VERDICT_UNKNOWN,
                status_code=status,
                response_body_excerpt=body_excerpt,
                latency_ms=latency_ms,
                failure_reason=f"http_{status}",
            )
    except asyncio.TimeoutError:
        return _build_unknown("timeout")
    except Exception as exc:  # noqa: BLE001 — defensive
        return _build_unknown(
            f"{type(exc).__name__}:{str(exc)[:80]}",
        )


# ---------------------------------------------------------------------------
# Catalog-wide verification (combines metadata extraction + micro-probe)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifyResult:
    """Aggregate outcome of verifying a full catalog snapshot."""
    metadata_verdicts: int   # decided from metadata alone
    probes_fired: int        # micro-probes issued
    probes_succeeded: int    # CHAT_CAPABLE outcomes from probe
    probes_rejected: int     # NON_CHAT outcomes from probe
    probes_inconclusive: int  # UNKNOWN outcomes (retry next cycle)
    skipped_already_known: int  # had a non-UNKNOWN verdict; not re-probed
    duration_ms: int

    def total_resolved(self) -> int:
        return (
            self.metadata_verdicts
            + self.probes_succeeded
            + self.probes_rejected
        )


async def verify_catalog_modalities(
    *,
    snapshot: CatalogSnapshot,
    ledger: ModalityLedger,
    session: Any,
    base_url: str,
    api_key: str,
    catalog_snapshot_id: str = "",
    probe_concurrency: int = 4,
    probe_timeout_s: float = _PROBE_TIMEOUT_S,
) -> VerifyResult:
    """End-to-end modality verification for a fresh catalog snapshot.

    Flow per model:
      1. Try ``extract_metadata_verdict`` — if conclusive, record and skip
      2. Else if ledger already has a verdict for this snapshot_id, skip
      3. Else fire ``micro_probe`` and record the result

    Probes run concurrently up to ``probe_concurrency``. Returns a
    summary tally; per-model verdicts land in the ledger.

    NEVER raises out of any path. Per-probe failures are isolated."""
    t0 = time.monotonic()
    metadata_verdicts = 0
    probes_fired = 0
    probes_succeeded = 0
    probes_rejected = 0
    probes_inconclusive = 0
    skipped = 0

    # Reset stale verdicts for this snapshot before recording new ones.
    # Operator overrides + verdicts pinned to empty snapshot_id survive.
    if catalog_snapshot_id:
        ledger.reset_for_catalog_refresh(catalog_snapshot_id)

    # Phase 1 — metadata pass (synchronous, fast)
    needs_probe: List[ModelCard] = []
    for card in snapshot.models:
        # Skip models already classified by THIS catalog snapshot
        existing = ledger.snapshot(card.model_id)
        if (
            existing is not None
            and existing.verdict in (VERDICT_CHAT_CAPABLE, VERDICT_NON_CHAT)
            and existing.catalog_snapshot_id == catalog_snapshot_id
        ):
            skipped += 1
            continue

        meta_verdict = extract_metadata_verdict(card)
        if meta_verdict is True:
            ledger.record_metadata_verdict(
                card.model_id,
                is_chat_capable=True,
                catalog_snapshot_id=catalog_snapshot_id,
            )
            metadata_verdicts += 1
        elif meta_verdict is False:
            ledger.record_metadata_verdict(
                card.model_id,
                is_chat_capable=False,
                catalog_snapshot_id=catalog_snapshot_id,
            )
            metadata_verdicts += 1
        else:
            # Ambiguous metadata → register UNKNOWN so the classifier
            # can route it to SPECULATIVE quarantine in this cycle,
            # then queue a micro-probe.
            ledger.register_unknown(
                card.model_id,
                catalog_snapshot_id=catalog_snapshot_id,
            )
            needs_probe.append(card)

    if not needs_probe:
        return VerifyResult(
            metadata_verdicts=metadata_verdicts,
            probes_fired=0,
            probes_succeeded=0,
            probes_rejected=0,
            probes_inconclusive=0,
            skipped_already_known=skipped,
            duration_ms=int((time.monotonic() - t0) * 1000),
        )

    # Phase 2 — concurrent probes
    sem = asyncio.Semaphore(max(1, probe_concurrency))

    async def _probe_one(card: ModelCard) -> ProbeResult:
        async with sem:
            return await micro_probe(
                session=session,
                base_url=base_url,
                api_key=api_key,
                model_id=card.model_id,
                timeout_s=probe_timeout_s,
            )

    probe_results = await asyncio.gather(
        *(_probe_one(c) for c in needs_probe),
        return_exceptions=True,
    )

    for r in probe_results:
        if isinstance(r, BaseException):
            probes_inconclusive += 1
            continue
        probes_fired += 1
        if r.verdict == VERDICT_CHAT_CAPABLE:
            ledger.record_probe_result(
                r.model_id,
                is_chat_capable=True,
                response_body_excerpt=r.response_body_excerpt,
                catalog_snapshot_id=catalog_snapshot_id,
            )
            probes_succeeded += 1
        elif r.verdict == VERDICT_NON_CHAT:
            ledger.record_probe_result(
                r.model_id,
                is_chat_capable=False,
                response_body_excerpt=r.response_body_excerpt,
                catalog_snapshot_id=catalog_snapshot_id,
            )
            probes_rejected += 1
        else:
            probes_inconclusive += 1
            logger.debug(
                "[ModalityProbe] inconclusive: model=%s status=%d reason=%s",
                r.model_id, r.status_code, r.failure_reason,
            )

    return VerifyResult(
        metadata_verdicts=metadata_verdicts,
        probes_fired=probes_fired,
        probes_succeeded=probes_succeeded,
        probes_rejected=probes_rejected,
        probes_inconclusive=probes_inconclusive,
        skipped_already_known=skipped,
        duration_ms=int((time.monotonic() - t0) * 1000),
    )


__all__ = [
    "ProbeResult",
    "VerifyResult",
    "extract_metadata_verdict",
    "micro_probe",
    "verify_catalog_modalities",
]
