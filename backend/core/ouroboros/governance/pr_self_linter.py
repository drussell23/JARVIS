from __future__ import annotations
import hashlib
import logging
import os
from typing import Awaitable, Callable, Optional

from .dag_capability_token import (
    CapabilityToken, DAGProofChain, LintClearedToken, TokenKind,
)

logger = logging.getLogger(__name__)

_RULES = (
    "Critique this diff against the repository's architectural rules. Return JSON "
    '{"rating": 1-5, "concerns": [..]}. Rules: (1) NO hardcoding -- values/paths/'
    "models must be env/config-derived; (2) DRY -- no duplicated logic that an "
    "existing helper covers; (3) explicit error handling -- no bare/silent excepts; "
    "(4) async-first -- no blocking calls on the event loop. Rate 5 only if all hold."
)


def linter_enabled() -> bool:
    return os.environ.get("JARVIS_A1_PR_LINTER_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def _threshold() -> int:
    try:
        return int(os.environ.get("JARVIS_A1_PR_LINTER_THRESHOLD", "4"))
    except ValueError:
        return 4


class LintRejected(RuntimeError):
    """The model's own architectural critique rejected the diff -- no PR."""


async def default_critique_fn(diff: str) -> dict:
    """Bounded, structured one-shot critique via the cheapest existing provider."""
    from .doubleword_provider import DoublewordProvider
    from .self_critique import parse_critique_json
    provider = DoublewordProvider()
    raw = await provider.prompt_only(
        prompt=f"{_RULES}\n\nDIFF:\n{diff}",
        caller_id="pr_self_linter",
        response_format={"type": "json_object"},
        max_tokens=512,
    )
    parsed, ok = parse_critique_json(raw, op_id="pr_self_linter")
    if not ok:
        logger.warning("[Gate3] pr_self_linter: critique parse failed; fail-closed rating=0")
        return {"rating": 0, "concerns": ["parse_failure"]}
    return parsed


async def acquire_lint_cleared_token(
    *,
    op_id: str,
    diff: str,
    chain: DAGProofChain,
    prev_token: CapabilityToken,
    critique_fn: Optional[Callable[[str], Awaitable[dict]]] = None,
    threshold: Optional[int] = None,
    branch_context: str = "",
) -> LintClearedToken:
    _crit = critique_fn or default_critique_fn
    _thr = threshold if threshold is not None else _threshold()
    verdict = await _crit(diff)
    try:
        rating = int(verdict.get("rating") or 0)
    except (TypeError, ValueError):
        rating = 0
    if rating < _thr:
        raise LintRejected(
            f"op={op_id} rating={rating}<{_thr} concerns={verdict.get('concerns')}")
    token = chain.mint(
        kind=TokenKind.LINT_CLEARED,
        op_id=op_id,
        state_binding=hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        payload={"rating": str(rating)},
        prev=prev_token,
        branch_context=branch_context,
    )
    return token  # type: ignore[return-value]
