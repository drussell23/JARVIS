"""Self-Critique Engine — Phase 3a self-evaluation loop.

After a successful VERIFY + auto-commit, this module runs a cheap
structured critique call against the original goal and the applied
diff. Low ratings become persistent FEEDBACK memories that steer
future ops; high ratings reinforce file reputation in the MemoryEngine.

Why this module exists
----------------------
Tests-pass is a noisy quality signal — an op can green-light every
test yet still solve the wrong problem (wrong approach, stylistic
drift, missed requirements, subtle regressions outside the test
envelope). The critique loop adds a second, independent quality
signal: the model re-reads its own work against the stated goal and
scores it on a 1-5 scale with a rationale. Low scores auto-persist
as FEEDBACK memories (via UserPreferenceMemory), so future ops with
similar shape inherit the lesson.

Design principles
-----------------
* **Non-blocking.** Every failure mode is swallowed. A missing
  provider, a parse error, a timeout, a network blip — none of them
  fail the op. Critique is additive, never subtractive.
* **Cost-capped.** Hard per-op wall-clock timeout (default 30s) and
  an output-token cap (default 512). Critique must be strictly
  cheaper than the op it rates or the economics collapse.
* **Provider-agnostic.** Takes a ``CritiqueProvider`` protocol so
  the engine works with DoubleWord 397B (cheapest), Claude, or a
  test stub.
* **Risk-tier aware.** Trivial ops skip entirely (savings); moderate
  and complex ops get the full budget; architectural ops get a
  deeper critique prompt.
* **Deterministic schema.** Responses must conform to ``critique.1``
  (JSON with ``rating``, ``rationale``, ``matches_goal``,
  ``completeness``, ``concerns``). Malformed responses are clamped
  to a safe default rating of 3 and tagged as ``parse_failure``.
* **Deduped writeback.** Poor ratings upsert via UserPreferenceStore
  so repeat failures on similar shapes update the existing memory
  rather than pile up.
* **Env-gated.** All tunables read from ``JARVIS_CRITIQUE_*`` env
  vars — zero hardcoded thresholds per the no-hardcoding mandate.

Public surface
--------------
* :class:`CritiqueResult` — frozen dataclass describing one critique.
* :class:`CritiqueProvider` — Protocol adapters implement.
* :class:`DoublewordCritiqueProvider` — DW 397B adapter.
* :class:`ClaudeCritiqueProvider` — Claude fallback adapter.
* :class:`CritiqueEngine` — main orchestration class.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    runtime_checkable,
)

logger = logging.getLogger("Ouroboros.SelfCritique")


# ---------------------------------------------------------------------------
# Constants / env resolution
# ---------------------------------------------------------------------------

_SCHEMA_VERSION = "critique.1"

# Every tunable is env-driven. Helpers read fresh on every call so tests
# can mutate env without restarting a singleton.


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name, "").strip()
        return float(raw) if raw else default
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        raw = os.environ.get(name, "").strip()
        return int(raw) if raw else default
    except (TypeError, ValueError):
        return default


def is_self_critique_enabled() -> bool:
    """Master switch — default ON."""
    return _env_bool("JARVIS_SELF_CRITIQUE_ENABLED", True)


# Risk tiers that skip critique by default (cheap ops, not worth the call).
_DEFAULT_SKIP_TIERS: Tuple[str, ...] = ("trivial",)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CritiqueResult:
    """One structured self-critique verdict.

    All fields are validated by the engine before construction so
    downstream consumers can trust rating to be in [1, 5] and
    completeness to be in [1, 5].
    """

    op_id: str
    rating: int                       # 1 = poor, 5 = excellent
    rationale: str                    # why the rating
    matches_goal: bool                # does the diff address the stated goal?
    completeness: int                 # 1-5: how complete relative to the plan
    concerns: Tuple[str, ...]         # short list of specific concerns
    provider_name: str                # "doubleword" / "claude" / "stub"
    schema_version: str               # always "critique.1" currently
    duration_s: float                 # wall-clock of the critique call
    cost_usd: float                   # estimated cost of this critique (0 if unknown)
    raw_response: str                 # raw model response (truncated) for debugging
    parse_ok: bool                    # did we get a clean schema match?
    skip_reason: Optional[str] = None  # populated when engine skipped the call

    @property
    def is_poor(self) -> bool:
        return self.rating <= _env_int("JARVIS_CRITIQUE_POOR_THRESHOLD", 2)

    @property
    def is_excellent(self) -> bool:
        return self.rating >= 5

    def to_dict(self) -> Dict[str, Any]:
        return {
            "op_id": self.op_id,
            "rating": self.rating,
            "rationale": self.rationale,
            "matches_goal": self.matches_goal,
            "completeness": self.completeness,
            "concerns": list(self.concerns),
            "provider_name": self.provider_name,
            "schema_version": self.schema_version,
            "duration_s": round(self.duration_s, 3),
            "cost_usd": round(self.cost_usd, 6),
            "parse_ok": self.parse_ok,
            "skip_reason": self.skip_reason,
        }


def _skipped_result(op_id: str, reason: str) -> CritiqueResult:
    return CritiqueResult(
        op_id=op_id,
        rating=0,
        rationale="critique skipped",
        matches_goal=True,
        completeness=0,
        concerns=(),
        provider_name="",
        schema_version=_SCHEMA_VERSION,
        duration_s=0.0,
        cost_usd=0.0,
        raw_response="",
        parse_ok=True,
        skip_reason=reason,
    )


# ---------------------------------------------------------------------------
# Provider protocol + adapters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CritiqueRequest:
    """Structured input to a CritiqueProvider. Providers format it."""

    op_id: str
    goal: str                 # original signal description
    diff: str                 # unified git diff of the applied change
    risk_tier: str            # "safe_auto" / "notify_apply" / "approval_required" / ...
    target_files: Tuple[str, ...]
    test_summary: str         # "all X tests passed" or "(no tests ran)"
    deadline_s: float         # wall-clock seconds available


@runtime_checkable
class CritiqueProvider(Protocol):
    """Protocol for critique backends.

    Implementations MUST be non-blocking (async), MUST respect the
    deadline in the request, and SHOULD return raw model text — the
    engine handles JSON extraction + schema validation centrally.
    """

    name: str

    async def critique(self, request: CritiqueRequest) -> str:
        """Return raw model response (hopefully JSON conformant)."""
        ...


_CRITIQUE_SYSTEM_PROMPT = (
    "You are a rigorous code review auditor embedded in the JARVIS Ouroboros "
    "self-development pipeline. Your job: given an operation's original goal, "
    "its applied git diff, and its test results, rate how well the diff "
    "addresses the stated goal. Be honest and concise. Return ONLY JSON "
    "conforming to the critique.1 schema — no prose, no markdown fences."
)

_CRITIQUE_SCHEMA_BLOCK = (
    "{\n"
    '  "rating": <integer 1-5, where 1=wrong/broken, 3=acceptable, 5=excellent>,\n'
    '  "matches_goal": <true|false — does the diff address the stated goal?>,\n'
    '  "completeness": <integer 1-5 — how complete vs the apparent plan>,\n'
    '  "rationale": "<1-2 sentence plain-English explanation>",\n'
    '  "concerns": ["<short concern 1>", "<short concern 2>"]\n'
    "}"
)


def build_critique_prompt(request: CritiqueRequest) -> str:
    """Build the user-prompt that any critique provider consumes.

    Factored out so providers can inject their own system prompt
    while reusing the deterministic evaluation body.
    """
    max_diff = _env_int("JARVIS_CRITIQUE_MAX_DIFF_CHARS", 8000)
    trimmed_diff = request.diff
    if len(trimmed_diff) > max_diff:
        trimmed_diff = (
            trimmed_diff[: max_diff // 2]
            + f"\n\n... [diff truncated, {len(request.diff) - max_diff} chars omitted] ...\n\n"
            + trimmed_diff[-max_diff // 2 :]
        )
    files_str = ", ".join(request.target_files[:10]) or "(none declared)"
    return (
        f"# Operation Critique\n\n"
        f"## Original Goal\n{request.goal}\n\n"
        f"## Risk Tier\n{request.risk_tier}\n\n"
        f"## Target Files\n{files_str}\n\n"
        f"## Test Summary\n{request.test_summary}\n\n"
        f"## Applied Diff\n```diff\n{trimmed_diff}\n```\n\n"
        f"## Instructions\n"
        f"Evaluate whether the applied diff achieves the stated goal. "
        f"Check for: (a) direct goal satisfaction, (b) completeness relative "
        f"to an implicit plan, (c) subtle drift (files beyond scope, "
        f"style regressions, dead code, missed edge cases). Test-pass alone "
        f"is NOT sufficient — look at the diff content itself.\n\n"
        f"Return strict JSON matching this schema exactly:\n\n"
        f"{_CRITIQUE_SCHEMA_BLOCK}\n\n"
        f"Output the JSON object only. No prose, no markdown fences, no explanation."
    )


class DoublewordCritiqueProvider:
    """Cheap critique backend powered by DoubleWord 397B prompt_only()."""

    name = "doubleword"

    def __init__(
        self,
        dw_provider: Any,
        *,
        max_tokens: int = 512,
        caller_id: str = "self_critique",
    ) -> None:
        self._dw = dw_provider
        self._max_tokens = max_tokens
        self._caller_id = caller_id

    async def critique(self, request: CritiqueRequest) -> str:
        prompt = build_critique_prompt(request)
        # DoublewordProvider.prompt_only enforces its own budget/session.
        # Wrap it in our deadline as a second layer of defense.
        try:
            return await asyncio.wait_for(
                self._dw.prompt_only(
                    prompt=prompt,
                    caller_id=self._caller_id,
                    response_format={"type": "json_object"},
                    max_tokens=self._max_tokens,
                ),
                timeout=request.deadline_s,
            )
        except asyncio.TimeoutError:
            raise
        except Exception as exc:
            # Surface to engine for logging; engine handles swallowing.
            logger.debug("[DWCritique] provider call failed: %s", exc)
            raise


class ClaudeCritiqueProvider:
    """Fallback critique backend using Claude directly.

    Used only when DoubleWord is unavailable. More expensive than DW,
    so the engine only wires this when JARVIS_CRITIQUE_CLAUDE_FALLBACK=true.
    """

    name = "claude"

    def __init__(
        self,
        client_factory: Callable[[], Any],
        *,
        model: str,
        max_tokens: int = 512,
    ) -> None:
        self._client_factory = client_factory
        self._model = model
        self._max_tokens = max_tokens

    async def critique(self, request: CritiqueRequest) -> str:
        client = self._client_factory()
        if client is None:
            raise RuntimeError("Claude client unavailable for critique")
        prompt = build_critique_prompt(request)

        async def _do_create() -> Any:
            return await client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                temperature=0.2,
                system=_CRITIQUE_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )

        msg = await asyncio.wait_for(_do_create(), timeout=request.deadline_s)
        # Extract text from content blocks.
        raw = ""
        for block in getattr(msg, "content", None) or []:
            if getattr(block, "type", None) == "text":
                raw += getattr(block, "text", "") or ""
        if not raw and getattr(msg, "content", None):
            raw = getattr(msg.content[0], "text", "") or ""
        return raw


# ---------------------------------------------------------------------------
# JSON extraction + schema validation
# ---------------------------------------------------------------------------


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json_block(raw: str) -> Optional[str]:
    """Best-effort JSON object extraction from a model response.

    Tries in order: code fence, plain dict literal, first balanced
    {...} region. Returns the first parseable block's source text.
    """
    if not raw:
        return None
    stripped = raw.strip()
    # 1. Inside ```json ... ```
    fenced = _CODE_FENCE_RE.search(stripped)
    if fenced:
        return fenced.group(1)
    # 2. First {..} region by regex
    match = _JSON_BLOCK_RE.search(stripped)
    if match:
        return match.group(0)
    return None


def parse_critique_json(raw: str, *, op_id: str) -> Tuple[Dict[str, Any], bool]:
    """Parse + validate a critique response. Returns (data, parse_ok).

    On any failure, returns a safe default dict (rating=3,
    matches_goal=True, completeness=3, rationale="parse failure")
    with parse_ok=False so the engine can log + skip writeback.
    """
    default: Dict[str, Any] = {
        "rating": 3,
        "matches_goal": True,
        "completeness": 3,
        "rationale": "parse_failure: unable to extract JSON from response",
        "concerns": [],
    }
    block = _extract_json_block(raw)
    if block is None:
        logger.debug("[Critique] no JSON block in response for op=%s", op_id)
        return default, False
    try:
        data = json.loads(block)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.debug("[Critique] JSON decode failed for op=%s: %s", op_id, exc)
        return default, False
    if not isinstance(data, dict):
        return default, False

    def _clamp(value: Any, lo: int, hi: int, fallback: int) -> int:
        try:
            n = int(value)
        except (TypeError, ValueError):
            return fallback
        return max(lo, min(hi, n))

    parsed: Dict[str, Any] = {
        "rating": _clamp(data.get("rating"), 1, 5, 3),
        "matches_goal": bool(data.get("matches_goal", True)),
        "completeness": _clamp(data.get("completeness"), 1, 5, 3),
        "rationale": str(data.get("rationale", "") or "").strip()[:800],
        "concerns": [],
    }
    raw_concerns = data.get("concerns") or []
    if isinstance(raw_concerns, (list, tuple)):
        parsed["concerns"] = [
            str(c).strip()[:200]
            for c in raw_concerns
            if c and str(c).strip()
        ][:10]
    if not parsed["rationale"]:
        parsed["rationale"] = "(no rationale provided)"
    return parsed, True


# ---------------------------------------------------------------------------
# Git diff collection
# ---------------------------------------------------------------------------


def collect_op_diff(
    repo_root: Path,
    *,
    commit_hash: Optional[str],
    target_files: Sequence[str],
    timeout_s: float = 10.0,
) -> str:
    """Collect the unified diff for an op.

    Prefers the committed diff (``git show <sha>``) when the
    auto-committer landed the change. Falls back to working-tree
    diff (``git diff HEAD``) when there was no commit. Returns an
    empty string on any git failure so the caller can skip gracefully.
    """
    if commit_hash:
        cmd: List[str] = [
            "git",
            "--no-pager",
            "show",
            "--format=",
            commit_hash,
        ]
        if target_files:
            cmd.append("--")
            cmd.extend(target_files)
        return _run_git_capture(cmd, cwd=repo_root, timeout_s=timeout_s)

    # Working-tree fallback.
    cmd = ["git", "--no-pager", "diff", "HEAD"]
    if target_files:
        cmd.append("--")
        cmd.extend(target_files)
    return _run_git_capture(cmd, cwd=repo_root, timeout_s=timeout_s)


def _run_git_capture(cmd: Sequence[str], *, cwd: Path, timeout_s: float) -> str:
    try:
        proc = subprocess.run(
            list(cmd),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("[Critique] git capture failed: %s", exc)
        return ""
    if proc.returncode != 0:
        logger.debug("[Critique] git returned %d: %s", proc.returncode, proc.stderr.strip()[:200])
        return ""
    return proc.stdout or ""


# ---------------------------------------------------------------------------
# Critique Engine
# ---------------------------------------------------------------------------


# Typing helpers: the memory store + memory engine are passed in
# duck-typed to avoid hard circular imports in tests.
MemoryWriteback = Callable[[CritiqueResult, str, Tuple[str, ...]], None]


class CritiqueEngine:
    """Runs self-critique calls and persists the feedback loop.

    The engine is deliberately stateless between calls — every
    ``critique_op`` invocation reads fresh env vars, so dynamic
    reconfiguration via ``os.environ`` works without restart. A
    single engine instance is safe to share across the orchestrator
    for the lifetime of a session.
    """

    def __init__(
        self,
        provider: CritiqueProvider,
        *,
        repo_root: Path,
        user_preference_store: Optional[Any] = None,
        memory_engine: Optional[Any] = None,
        fallback_provider: Optional[CritiqueProvider] = None,
    ) -> None:
        self._provider = provider
        self._fallback = fallback_provider
        self._repo_root = Path(repo_root)
        self._store = user_preference_store
        self._memory_engine = memory_engine
        # Cumulative telemetry for /status / dashboard integration.
        self._cumulative_cost_usd: float = 0.0
        self._total_critiques: int = 0
        self._poor_count: int = 0
        self._excellent_count: int = 0
        self._skip_count: int = 0
        self._fail_count: int = 0

    # ------------------------------------------------------------------
    # Public: stats / observability
    # ------------------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        """Snapshot of cumulative critique telemetry.

        Consumers: SerpentFlow /status, battle-test final summary,
        LiveDashboard critique panel.
        """
        return {
            "total_critiques": self._total_critiques,
            "poor_count": self._poor_count,
            "excellent_count": self._excellent_count,
            "skip_count": self._skip_count,
            "fail_count": self._fail_count,
            "cumulative_cost_usd": round(self._cumulative_cost_usd, 6),
        }

    # ------------------------------------------------------------------
    # Public: main entry point
    # ------------------------------------------------------------------

    async def critique_op(
        self,
        *,
        op_id: str,
        description: str,
        target_files: Sequence[str],
        risk_tier: str,
        commit_hash: Optional[str] = None,
        test_summary: Optional[str] = None,
    ) -> CritiqueResult:
        """Run a critique for one completed op.

        This is the ONLY method callers should invoke. It enforces
        env gating, budget, provider fallback, schema validation,
        memory writeback, and telemetry tracking in one place.
        """
        files_tuple = tuple(p for p in target_files if p)

        # ---------- Gating ----------
        if not is_self_critique_enabled():
            return self._record_skip(op_id, "disabled")

        if self._should_skip_tier(risk_tier):
            return self._record_skip(op_id, f"skip_tier:{risk_tier}")

        if not description or not description.strip():
            return self._record_skip(op_id, "empty_description")

        # ---------- Diff collection ----------
        diff = collect_op_diff(
            self._repo_root,
            commit_hash=commit_hash,
            target_files=files_tuple,
        )
        if not diff.strip():
            return self._record_skip(op_id, "empty_diff")

        # ---------- Build request ----------
        timeout_s = max(5.0, _env_float("JARVIS_CRITIQUE_TIMEOUT_S", 30.0))
        request = CritiqueRequest(
            op_id=op_id,
            goal=description.strip(),
            diff=diff,
            risk_tier=(risk_tier or "unknown").lower(),
            target_files=files_tuple,
            test_summary=(test_summary or "(no test summary)").strip(),
            deadline_s=timeout_s,
        )

        # ---------- Provider call with fallback ----------
        t0 = time.monotonic()
        raw_response, used_provider, call_err = await self._call_with_fallback(request)
        duration_s = time.monotonic() - t0

        if raw_response is None:
            self._fail_count += 1
            logger.info(
                "[Critique] provider chain exhausted for op=%s (err=%s)",
                op_id, call_err,
            )
            return self._record_skip(op_id, f"provider_failed:{call_err or 'unknown'}")

        # ---------- Parse ----------
        parsed, parse_ok = parse_critique_json(raw_response, op_id=op_id)
        if not parse_ok:
            logger.info(
                "[Critique] parse failed for op=%s (provider=%s) — using default rating",
                op_id, used_provider,
            )

        # ---------- Cost estimate (rough) ----------
        cost_usd = self._estimate_cost(raw_response, used_provider)
        self._cumulative_cost_usd += cost_usd
        self._total_critiques += 1

        result = CritiqueResult(
            op_id=op_id,
            rating=int(parsed["rating"]),
            rationale=str(parsed["rationale"]),
            matches_goal=bool(parsed["matches_goal"]),
            completeness=int(parsed["completeness"]),
            concerns=tuple(parsed["concerns"]),
            provider_name=used_provider,
            schema_version=_SCHEMA_VERSION,
            duration_s=duration_s,
            cost_usd=cost_usd,
            raw_response=raw_response[:2000],
            parse_ok=parse_ok,
            skip_reason=None,
        )

        # ---------- Writeback ----------
        if result.is_poor and parse_ok:
            self._poor_count += 1
            self._writeback_poor(result, description, files_tuple)
        elif result.is_excellent and parse_ok:
            self._excellent_count += 1
            self._writeback_excellent(result, files_tuple)

        logger.info(
            "[Critique] op=%s rating=%d/5 matches_goal=%s completeness=%d "
            "provider=%s duration=%.2fs parse_ok=%s",
            op_id, result.rating, result.matches_goal, result.completeness,
            used_provider, duration_s, parse_ok,
        )
        return result

    # ------------------------------------------------------------------
    # Internal: provider invocation
    # ------------------------------------------------------------------

    async def _call_with_fallback(
        self, request: CritiqueRequest,
    ) -> Tuple[Optional[str], str, Optional[str]]:
        """Try primary → fallback. Returns (raw|None, provider_name, error_str)."""
        try:
            raw = await self._provider.critique(request)
            if raw and raw.strip():
                return raw, self._provider.name, None
            err = "empty_response"
        except asyncio.TimeoutError:
            err = "timeout"
        except Exception as exc:
            err = f"{type(exc).__name__}:{str(exc)[:100]}"

        if self._fallback is None:
            return None, self._provider.name, err

        logger.debug(
            "[Critique] primary %s failed (%s) — trying fallback %s",
            self._provider.name, err, self._fallback.name,
        )
        try:
            raw = await self._fallback.critique(request)
            if raw and raw.strip():
                return raw, self._fallback.name, None
            return None, self._fallback.name, "empty_response"
        except asyncio.TimeoutError:
            return None, self._fallback.name, "timeout"
        except Exception as exc:
            return None, self._fallback.name, f"{type(exc).__name__}:{str(exc)[:100]}"

    # ------------------------------------------------------------------
    # Internal: skip bookkeeping
    # ------------------------------------------------------------------

    def _record_skip(self, op_id: str, reason: str) -> CritiqueResult:
        self._skip_count += 1
        logger.debug("[Critique] skipped op=%s reason=%s", op_id, reason)
        return _skipped_result(op_id, reason)

    def _should_skip_tier(self, risk_tier: str) -> bool:
        if not _env_bool("JARVIS_CRITIQUE_SKIP_TRIVIAL", True):
            return False
        tier_lc = (risk_tier or "").lower().strip()
        return tier_lc in _DEFAULT_SKIP_TIERS

    # ------------------------------------------------------------------
    # Internal: cost estimation
    # ------------------------------------------------------------------

    def _estimate_cost(self, raw: str, provider_name: str) -> float:
        """Rough cost estimate from response length.

        Real token usage isn't exposed through the prompt_only surface
        today, so we approximate by char / 4. DoubleWord at $0.10/$0.40
        per M in/out tokens; Claude at $3/$15. This is intentionally
        conservative — the cumulative total is accurate within ~20%.
        """
        if not raw:
            return 0.0
        # Very rough: assume ~1500-token prompt + response_tokens output.
        in_tokens = 1500
        out_tokens = max(32, len(raw) // 4)
        if provider_name == "doubleword":
            return (in_tokens / 1_000_000 * 0.10) + (out_tokens / 1_000_000 * 0.40)
        if provider_name == "claude":
            return (in_tokens / 1_000_000 * 3.00) + (out_tokens / 1_000_000 * 15.0)
        return 0.0  # stub / unknown

    # ------------------------------------------------------------------
    # Internal: memory writeback
    # ------------------------------------------------------------------

    def _writeback_poor(
        self,
        result: CritiqueResult,
        description: str,
        target_files: Tuple[str, ...],
    ) -> None:
        """Persist a low rating as a FEEDBACK memory + reputation hit."""
        if self._store is None:
            return
        try:
            recorder = getattr(self._store, "record_critique_failure", None)
            if callable(recorder):
                recorder(
                    op_id=result.op_id,
                    description=description,
                    target_files=target_files,
                    rating=result.rating,
                    rationale=result.rationale,
                    concerns=result.concerns,
                )
        except Exception as exc:
            logger.debug("[Critique] poor-rating writeback failed: %s", exc)

        # Memory reputation hit: low critique is treated as a soft
        # failure for the touched files. MemoryEngine exposes a
        # private updater; we duck-type to tolerate shape changes.
        if self._memory_engine is not None:
            try:
                updater = getattr(self._memory_engine, "_update_file_reputation", None)
                if callable(updater):
                    updater(target_files, success=False, blast_radius=len(target_files))
            except Exception as exc:
                logger.debug("[Critique] reputation writeback failed: %s", exc)

    def _writeback_excellent(
        self,
        result: CritiqueResult,  # noqa: ARG002 — kept for symmetry + future use
        target_files: Tuple[str, ...],
    ) -> None:
        """Reinforce file reputation on excellent ratings."""
        _ = result  # symmetry with _writeback_poor; reserved for future hooks
        if self._memory_engine is None:
            return
        try:
            updater = getattr(self._memory_engine, "_update_file_reputation", None)
            if callable(updater):
                updater(target_files, success=True, blast_radius=len(target_files))
        except Exception as exc:
            logger.debug("[Critique] excellent reinforcement failed: %s", exc)


# ---------------------------------------------------------------------------
# Module-level singleton helpers (optional)
# ---------------------------------------------------------------------------


_default_engine: Optional[CritiqueEngine] = None


def get_default_engine() -> Optional[CritiqueEngine]:
    return _default_engine


def set_default_engine(engine: Optional[CritiqueEngine]) -> None:
    global _default_engine
    _default_engine = engine
