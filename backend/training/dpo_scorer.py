"""
DPO Preference Scorer — Doubleword Batch API
=============================================

Uses Doubleword's 397B MoE model to score N candidate responses against a
reference in a single coalesced batch job. Designed for Reactor-Core's DPO
training pipeline.

Key Design:
  - Batch coalescing: all candidates go into one JSONL → one batch → one poll.
  - Reasoning extraction: the 397B's chain-of-thought rationale is preserved
    alongside the numeric score for richer training signal.
  - 24h SLA acceptable — called once per nightly training epoch.

Usage:
    scorer = DPOScorer(api_key="sk-...")
    results = await scorer.score_candidates(
        reference="def add(a, b): return a + b",
        candidates=["def add(a,b):return a+b", "def add(x,y):\\n  return x+y", ...],
        scoring_criteria="code quality, readability, correctness",
    )
    # results[i].score, results[i].rationale, results[i].candidate_id
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration — all env-driven
# ---------------------------------------------------------------------------

_DW_API_KEY = os.environ.get("DOUBLEWORD_API_KEY", "")
_DW_BASE_URL = os.environ.get("DOUBLEWORD_BASE_URL", "https://api.doubleword.ai/v1")
_DW_DPO_MODEL = os.environ.get(
    "DOUBLEWORD_DPO_MODEL", "Qwen/Qwen3.5-397B-A17B-FP8"
)
_DW_DPO_MAX_TOKENS = int(os.environ.get("DOUBLEWORD_DPO_MAX_TOKENS", "5000"))
_DW_DPO_WINDOW = os.environ.get("DOUBLEWORD_DPO_WINDOW", "24h")
_DW_POLL_INTERVAL_S = float(os.environ.get("DOUBLEWORD_DPO_POLL_INTERVAL_S", "30"))
_DW_MAX_WAIT_S = float(os.environ.get("DOUBLEWORD_DPO_MAX_WAIT_S", "86400"))
_DW_TEMPERATURE = float(os.environ.get("DOUBLEWORD_DPO_TEMPERATURE", "0.1"))

# Pricing
_DW_INPUT_COST_PER_M = float(os.environ.get("DOUBLEWORD_INPUT_COST_PER_M", "0.10"))
_DW_OUTPUT_COST_PER_M = float(os.environ.get("DOUBLEWORD_OUTPUT_COST_PER_M", "0.40"))

_DPO_SYSTEM_PROMPT = (
    "You are a code quality judge for a DPO training pipeline. "
    "You will be given a REFERENCE response and a CANDIDATE response. "
    "Score the candidate on a scale of 0.0 to 1.0 where:\n"
    "  1.0 = candidate is equal or superior to reference\n"
    "  0.5 = candidate is acceptable but clearly worse\n"
    "  0.0 = candidate is incorrect or harmful\n\n"
    "Respond with ONLY a JSON object:\n"
    '{"score": <float 0.0-1.0>, "rationale": "<brief explanation>"}\n\n'
    "No markdown, no preamble. Only the JSON object."
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ScoredCandidate:
    """Result of scoring a single candidate against the reference."""
    candidate_id: str
    candidate_text: str
    score: float
    rationale: str
    reasoning_content: str  # full chain-of-thought (for training signal)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class DPOScoringResult:
    """Aggregate result from a DPO scoring batch."""
    batch_id: str
    model: str
    candidates: List[ScoredCandidate]
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    wall_time_s: float = 0.0
    scoring_criteria: str = ""

    def preference_pairs(self) -> List[Tuple[ScoredCandidate, ScoredCandidate]]:
        """Generate all (winner, loser) pairs for DPO training.

        Returns pairs where winner.score > loser.score. Each pair becomes
        one training example: (prompt, chosen=winner, rejected=loser).
        """
        sorted_candidates = sorted(self.candidates, key=lambda c: c.score, reverse=True)
        pairs = []
        for i, winner in enumerate(sorted_candidates):
            for loser in sorted_candidates[i + 1:]:
                if winner.score > loser.score:
                    pairs.append((winner, loser))
        return pairs


# ---------------------------------------------------------------------------
# DPO Scorer
# ---------------------------------------------------------------------------


class DPOScorer:
    """Score N candidates against a reference using Doubleword's 397B model.

    Coalesces all candidates into a single JSONL batch for efficiency.
    """

    def __init__(
        self,
        api_key: str = _DW_API_KEY,
        base_url: str = _DW_BASE_URL,
        model: str = _DW_DPO_MODEL,
        max_tokens: int = _DW_DPO_MAX_TOKENS,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._max_tokens = max_tokens
        self._session: Optional[Any] = None

    @property
    def is_available(self) -> bool:
        return bool(self._api_key)

    async def _get_session(self) -> Any:
        if self._session is None or self._session.closed:
            import aiohttp
            self._session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def score_candidates(
        self,
        reference: str,
        candidates: List[str],
        *,
        scoring_criteria: str = "code quality, correctness, readability",
        candidate_ids: Optional[List[str]] = None,
    ) -> DPOScoringResult:
        """Score N candidates against a reference in a single coalesced batch.

        Parameters
        ----------
        reference:
            The reference/gold response to compare against.
        candidates:
            List of candidate responses to score.
        scoring_criteria:
            Human-readable criteria for the judge model.
        candidate_ids:
            Optional IDs for each candidate. Defaults to "candidate-0", etc.

        Returns
        -------
        DPOScoringResult with scored candidates and preference pairs.
        """
        if not self.is_available:
            raise RuntimeError("DPOScorer: DOUBLEWORD_API_KEY not configured")

        if not candidates:
            raise ValueError("DPOScorer: no candidates to score")

        ids = candidate_ids or [f"candidate-{i}" for i in range(len(candidates))]
        if len(ids) != len(candidates):
            raise ValueError("candidate_ids length must match candidates length")

        t0 = time.monotonic()

        # Build coalesced JSONL — one scoring request per candidate
        jsonl_lines = []
        for cid, candidate_text in zip(ids, candidates):
            user_prompt = (
                f"SCORING CRITERIA: {scoring_criteria}\n\n"
                f"REFERENCE:\n```\n{reference}\n```\n\n"
                f"CANDIDATE:\n```\n{candidate_text}\n```\n\n"
                "Score the CANDIDATE against the REFERENCE. "
                "Respond with only the JSON object."
            )
            jsonl_lines.append(json.dumps({
                "custom_id": cid,
                "method": "POST",
                "url": "/v1/chat/completions",
                "body": {
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": _DPO_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "max_tokens": self._max_tokens,
                    "temperature": _DW_TEMPERATURE,
                },
            }))

        jsonl_content = "\n".join(jsonl_lines)

        # Stage 1: Upload
        file_id = await self._upload_file(jsonl_content)
        if not file_id:
            raise RuntimeError("DPOScorer: JSONL upload failed")

        # Stage 2: Create batch
        batch_id = await self._create_batch(file_id)
        if not batch_id:
            raise RuntimeError("DPOScorer: batch creation failed")

        logger.info(
            "[DPOScorer] Batch %s submitted: %d candidates, model=%s, window=%s",
            batch_id, len(candidates), self._model, _DW_DPO_WINDOW,
        )

        # Stage 3: Poll
        output_file_id = await self._poll_batch(batch_id)
        if not output_file_id:
            raise RuntimeError(f"DPOScorer: batch {batch_id} failed or timed out")

        # Stage 4: Retrieve and parse
        raw_results = await self._retrieve_results(output_file_id)
        wall_time = time.monotonic() - t0

        # Parse scores
        scored: List[ScoredCandidate] = []
        total_in = 0
        total_out = 0

        for cid, candidate_text in zip(ids, candidates):
            entry = raw_results.get(cid)
            if entry is None:
                logger.warning("[DPOScorer] Missing result for %s", cid)
                scored.append(ScoredCandidate(
                    candidate_id=cid,
                    candidate_text=candidate_text,
                    score=0.0,
                    rationale="Missing from batch output",
                    reasoning_content="",
                ))
                continue

            body = entry.get("response", {}).get("body", {})
            usage = body.get("usage", {})
            in_t = usage.get("prompt_tokens", 0)
            out_t = usage.get("completion_tokens", 0)
            total_in += in_t
            total_out += out_t

            choices = body.get("choices", [])
            if not choices:
                scored.append(ScoredCandidate(
                    candidate_id=cid,
                    candidate_text=candidate_text,
                    score=0.0,
                    rationale="No choices in response",
                    reasoning_content="",
                    input_tokens=in_t,
                    output_tokens=out_t,
                ))
                continue

            message = choices[0].get("message", {})
            content = message.get("content", "") or ""
            reasoning = message.get("reasoning_content", "") or ""

            # Parse score from JSON content
            score, rationale = self._parse_score(content)

            cost = (
                in_t * _DW_INPUT_COST_PER_M / 1_000_000
                + out_t * _DW_OUTPUT_COST_PER_M / 1_000_000
            )
            scored.append(ScoredCandidate(
                candidate_id=cid,
                candidate_text=candidate_text,
                score=score,
                rationale=rationale,
                reasoning_content=reasoning,
                input_tokens=in_t,
                output_tokens=out_t,
                cost_usd=cost,
            ))

        total_cost = (
            total_in * _DW_INPUT_COST_PER_M / 1_000_000
            + total_out * _DW_OUTPUT_COST_PER_M / 1_000_000
        )

        logger.info(
            "[DPOScorer] Batch %s complete: %d candidates scored in %.1fs, "
            "cost=$%.6f, %d preference pairs",
            batch_id, len(scored), wall_time, total_cost,
            len(DPOScoringResult(
                batch_id=batch_id, model=self._model, candidates=scored,
            ).preference_pairs()),
        )

        return DPOScoringResult(
            batch_id=batch_id,
            model=self._model,
            candidates=scored,
            total_input_tokens=total_in,
            total_output_tokens=total_out,
            total_cost_usd=total_cost,
            wall_time_s=wall_time,
            scoring_criteria=scoring_criteria,
        )

    # ------------------------------------------------------------------
    # Score parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_score(content: str) -> Tuple[float, str]:
        """Extract score and rationale from model JSON output."""
        if not content.strip():
            return 0.0, "Empty content (reasoning exhausted token budget)"

        try:
            # Try direct JSON parse
            data = json.loads(content.strip())
            score = float(data.get("score", 0.0))
            rationale = str(data.get("rationale", ""))
            return max(0.0, min(1.0, score)), rationale
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

        # Fallback: try to extract JSON from markdown code block
        import re
        match = re.search(r'\{[^}]*"score"\s*:\s*([0-9.]+)[^}]*\}', content)
        if match:
            try:
                score = float(match.group(1))
                return max(0.0, min(1.0, score)), "Extracted from partial JSON"
            except ValueError:
                pass

        logger.warning("[DPOScorer] Could not parse score from: %s", content[:200])
        return 0.0, f"Unparseable: {content[:100]}"

    # ------------------------------------------------------------------
    # Batch API stages (reuse same protocol as DoublewordProvider)
    # ------------------------------------------------------------------

    async def _upload_file(self, jsonl_content: str) -> Optional[str]:
        import aiohttp
        import io
        session = await self._get_session()
        data = aiohttp.FormData()
        data.add_field(
            "file",
            io.BytesIO(jsonl_content.encode()),
            filename="dpo_scoring.jsonl",
            content_type="application/jsonl",
        )
        data.add_field("purpose", "batch")
        try:
            async with session.post(
                f"{self._base_url}/files",
                data=data,
                headers={"Authorization": f"Bearer {self._api_key}"},
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error("[DPOScorer] Upload failed: %s %s", resp.status, body[:500])
                    return None
                return (await resp.json()).get("id")
        except Exception:
            logger.exception("[DPOScorer] Upload error")
            return None

    async def _create_batch(self, input_file_id: str) -> Optional[str]:
        session = await self._get_session()
        try:
            async with session.post(
                f"{self._base_url}/batches",
                json={
                    "input_file_id": input_file_id,
                    "endpoint": "/v1/chat/completions",
                    "completion_window": _DW_DPO_WINDOW,
                },
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error("[DPOScorer] Batch create failed: %s %s", resp.status, body[:500])
                    return None
                return (await resp.json()).get("id")
        except Exception:
            logger.exception("[DPOScorer] Batch create error")
            return None

    async def _poll_batch(self, batch_id: str) -> Optional[str]:
        session = await self._get_session()
        deadline = time.monotonic() + _DW_MAX_WAIT_S
        while time.monotonic() < deadline:
            try:
                async with session.get(f"{self._base_url}/batches/{batch_id}") as resp:
                    if resp.status != 200:
                        await asyncio.sleep(_DW_POLL_INTERVAL_S)
                        continue
                    data = await resp.json()
                    status = data.get("status", "unknown")
                    if status == "completed":
                        return data.get("output_file_id")
                    elif status in ("failed", "expired", "cancelled"):
                        logger.error("[DPOScorer] Batch %s: %s", batch_id, status)
                        return None
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("[DPOScorer] Poll exception", exc_info=True)
            await asyncio.sleep(_DW_POLL_INTERVAL_S)

        logger.error("[DPOScorer] Batch %s timed out", batch_id)
        return None

    async def _retrieve_results(self, output_file_id: str) -> Dict[str, Any]:
        session = await self._get_session()
        try:
            async with session.get(
                f"{self._base_url}/files/{output_file_id}/content",
            ) as resp:
                if resp.status != 200:
                    logger.error("[DPOScorer] Retrieve failed: %s", resp.status)
                    return {}
                raw = await resp.text()
            parsed: Dict[str, Any] = {}
            for line in raw.strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    parsed[entry.get("custom_id", "")] = entry
                except json.JSONDecodeError:
                    continue
            return parsed
        except Exception:
            logger.exception("[DPOScorer] Retrieve error")
            return {}

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
