from __future__ import annotations

"""Sovereign Fleet Evaluator — async idle-driven DW model calibration driver.

Probes DoubleWord (DW) models on idle cycles, AST-validates their output IN
MEMORY (never executes), scores quality via the sibling quality battery, records
into the sibling calibration store, and auto-graduates a quality-aware routing
flip once a winner is stable across consecutive cycles.

Design ref: docs/superpowers/specs/2026-06-19-sovereign-fleet-evaluator-design.md §4.3

Fail-soft everywhere. Master switch ``JARVIS_FLEET_EVALUATOR_ENABLED`` (default
false). The routing flip is gated behind ``JARVIS_FLEET_EVALUATOR_AUTHORITATIVE``
(default false), persisted via the bounded credential-safe .env writer.

Sandbox-safe: imports only the two sibling fleet leaves + stdlib at module top.
The optional live model caller, snapshot loader, .env writer and SSE broker are
LATE-imported inside try/except so this module imports cleanly with no network or
real-file side effects.
"""

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Sequence

from backend.core.ouroboros.governance.fleet_quality_battery import (
    CODEGEN_PROMPT,
    CLASSIFY_PROMPT,
    EXPECTED_LABEL,
    code_quality_pass,
    label_adherence,
)
from backend.core.ouroboros.governance.fleet_calibration_store import (
    FleetCalibrationStore,
    graduation_ready,
    valid_tok_per_s,
)

logger = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------- #
# Probe result
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ProbeResult:
    """One model probe outcome. ``ok=False`` means transport/HTTP failure."""

    text: str
    ttft_ms: float
    total_ms: float
    completion_tokens: int
    ok: bool
    error: str


# --------------------------------------------------------------------------- #
# Env gates / knobs (re-read at call time so monkeypatch + hot-flip work)
# --------------------------------------------------------------------------- #
def fleet_evaluator_enabled() -> bool:
    return os.environ.get("JARVIS_FLEET_EVALUATOR_ENABLED", "").strip().lower() in _TRUE


def fleet_authoritative_enabled() -> bool:
    return (
        os.environ.get("JARVIS_FLEET_EVALUATOR_AUTHORITATIVE", "").strip().lower()
        in _TRUE
    )


def _int_env(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default


def _max_models_per_cycle() -> int:
    return _int_env("JARVIS_FLEET_MAX_MODELS_PER_CYCLE", 4)


def _probe_max_tokens() -> int:
    return _int_env("JARVIS_FLEET_PROBE_MAX_TOKENS", 512)


def _stable_cycles() -> int:
    return _int_env("JARVIS_FLEET_GRAD_STABLE_CYCLES", 2)


def _grad_min_samples() -> int:
    return _int_env("JARVIS_FLEET_GRAD_MIN_SAMPLES", 5)


def _grad_margin() -> float:
    return _float_env("JARVIS_FLEET_GRAD_MARGIN", 1.5)


def _default_model() -> str:
    return os.environ.get("DOUBLEWORD_MODEL", "Qwen/Qwen3.5-397B-A17B-FP8")


# --------------------------------------------------------------------------- #
# Default collaborators (all LATE-imported, fail-soft)
# --------------------------------------------------------------------------- #
def _default_snapshot_loader() -> List[str]:
    """Load DW model ids from the cached catalog snapshot. Fail-soft → []."""
    try:
        from backend.core.ouroboros.governance.dw_catalog_client import (
            load_cached_snapshot,
        )

        snap = load_cached_snapshot()
        return list(snap.model_ids()) if snap else []
    except Exception as exc:  # noqa: BLE001
        logger.debug("[FleetEvaluator] snapshot load skipped: %s", exc)
        return []


def _default_flag_persister(name: str, value: str) -> None:
    """Durably persist a flag via the bounded credential-safe .env writer the
    graduation orchestrator owns. Fail-soft → process-local env on any error.
    Never raises."""
    try:
        from backend.core.ouroboros.governance.graduation_orchestrator import (
            persist_flag_to_env,
        )

        ok = persist_flag_to_env(name, value)
        if not ok:
            os.environ[name] = value
            logger.warning(
                "[FleetEvaluator] durable .env persist refused/failed for %s; "
                "set process-local only",
                name,
            )
    except Exception as exc:  # noqa: BLE001
        try:
            os.environ[name] = value
        except Exception:  # noqa: BLE001
            pass
        logger.warning(
            "[FleetEvaluator] durable .env persist unavailable (%s); "
            "set process-local only for %s",
            exc,
            name,
        )


# --------------------------------------------------------------------------- #
# Evaluator
# --------------------------------------------------------------------------- #
class FleetEvaluator:
    """Async idle-driven calibration driver + auto-graduation.

    All collaborators are injectable for testing; the live defaults are
    late-bound and fail-soft so unit tests never touch network or real files.
    """

    def __init__(
        self,
        *,
        model_caller: Callable[..., Any],
        store: Optional[FleetCalibrationStore] = None,
        idle_check: Optional[Callable[[], bool]] = None,
        clock: Optional[Callable[[], float]] = None,
        snapshot_loader: Optional[Callable[[], Sequence[str]]] = None,
        default_model: Optional[str] = None,
        flag_persister: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.model_caller = model_caller
        self.store = store or FleetCalibrationStore()
        self.idle_check = idle_check or (lambda: True)
        self.clock = clock or time.time
        self.snapshot_loader = snapshot_loader or _default_snapshot_loader
        self.default_model = default_model or _default_model()
        self.flag_persister = flag_persister or _default_flag_persister
        self._consecutive_wins = 0
        self._last_winner: Optional[str] = None

    # -- single probe ------------------------------------------------------- #
    async def _probe_one(
        self, model_id: str, *, prompt: str, max_tokens: int
    ) -> ProbeResult:
        messages = [{"role": "user", "content": prompt}]
        try:
            return await self.model_caller(model_id, messages, max_tokens=max_tokens)
        except Exception as exc:  # noqa: BLE001
            return ProbeResult("", 0.0, 0.0, 0, False, repr(exc))

    # -- calibrate a list --------------------------------------------------- #
    async def calibrate_models(self, model_ids: Sequence[str]) -> None:
        max_tokens = _probe_max_tokens()
        for model_id in model_ids:
            try:
                # Codegen probe (AST-validated in memory, never executed).
                r = await self._probe_one(
                    model_id, prompt=CODEGEN_PROMPT, max_tokens=max_tokens
                )
                tok_per_s = r.completion_tokens / max(r.total_ms / 1000.0, 1e-3)
                self.store.record_probe(
                    model_id,
                    kind="code",
                    code_pass=(code_quality_pass(r.text) if r.ok else False),
                    ttft_ms=r.ttft_ms,
                    tok_per_s=tok_per_s,
                    now=self.clock(),
                )

                # Classify (triage) probe.
                c = await self._probe_one(
                    model_id, prompt=CLASSIFY_PROMPT, max_tokens=max_tokens
                )
                tok_per_s_c = c.completion_tokens / max(c.total_ms / 1000.0, 1e-3)
                self.store.record_probe(
                    model_id,
                    kind="triage",
                    label_score=(
                        label_adherence(c.text, EXPECTED_LABEL) if c.ok else 0.0
                    ),
                    ttft_ms=c.ttft_ms,
                    tok_per_s=tok_per_s_c,
                    now=self.clock(),
                )

                sc = self.store.score(model_id)
                if sc is not None:
                    logger.info(
                        "[FleetEvaluator] model=%s ast=%.2f label=%.2f vtps=%.1f "
                        "samples=%d",
                        model_id,
                        sc.ast_pass_rate,
                        sc.label_adherence,
                        valid_tok_per_s(sc),
                        sc.sample_count,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[FleetEvaluator] calibrate skipped model=%s: %s", model_id, exc
                )
                continue

        try:
            self.store.save()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[FleetEvaluator] store save skipped: %s", exc)

    # -- idle entry point --------------------------------------------------- #
    async def maybe_calibrate(self, *, now: float) -> None:
        """Idle-cycle entry. Short-circuits before any probe when the master
        switch is off or the system is not idle. Never raises."""
        try:
            if not fleet_evaluator_enabled():
                return
            if not self.idle_check():
                return
            models = list(self.snapshot_loader() or [])
            if not models:
                return
            # Prefer least-recently-benchmarked (unscored first).
            def _key(m: str) -> float:
                sc = self.store.score(m)
                return sc.updated_at if sc is not None else -1.0

            models.sort(key=_key)
            picked = models[: _max_models_per_cycle()]
            await self.calibrate_models(picked)
            self._maybe_graduate(now=now)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[FleetEvaluator] maybe_calibrate skipped: %s", exc)

    # -- graduation --------------------------------------------------------- #
    def _maybe_graduate(self, *, now: float) -> None:
        """Propose + (after stable cycles) flip the authoritative routing gate.
        Never raises."""
        try:
            winner = graduation_ready(
                self.store.all_scores(),
                default_model=self.default_model,
                min_samples=_grad_min_samples(),
                min_margin=_grad_margin(),
            )
            if winner is None:
                self._consecutive_wins = 0
                self._last_winner = None
                return

            logger.info(
                "[FleetEvaluator] proposed coder=%s (advisory)", winner
            )

            if winner == self._last_winner:
                self._consecutive_wins += 1
            else:
                self._consecutive_wins = 1
                self._last_winner = winner

            if (
                self._consecutive_wins >= _stable_cycles()
                and not fleet_authoritative_enabled()
            ):
                self.flag_persister("JARVIS_FLEET_EVALUATOR_AUTHORITATIVE", "true")
                logger.info(
                    "[FleetEvaluator] GRADUATED authoritative coder=%s", winner
                )
                self._emit(
                    "fleet_graduated",
                    {"coder": winner, "default": self.default_model, "now": now},
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[FleetEvaluator] graduation skipped: %s", exc)

    # -- best-effort SSE ---------------------------------------------------- #
    def _emit(self, event_type: str, payload: Any) -> None:
        """Best-effort SSE emission. Late-imports the broker; never raises."""
        try:
            from backend.core.ouroboros.governance.ide_observability_stream import (
                get_default_broker,
            )

            broker = get_default_broker()
            broker.publish(event_type, f"fleet-{event_type}", payload)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[FleetEvaluator] SSE emit skipped: %s", exc)


# --------------------------------------------------------------------------- #
# Optional live model caller (NOT exercised by unit tests — keep simple + guarded)
# --------------------------------------------------------------------------- #
async def default_model_caller(
    model_id: str, messages: list, *, max_tokens: int
) -> ProbeResult:
    """Live DW probe via ``/v1/chat/completions``, mirroring dw_catalog_client's
    HTTP idiom (base URL + Bearer auth + key env). Non-streaming: ttft_ms is
    approximated by total_ms. Fail-soft → ProbeResult(ok=False) on any error.
    aiohttp is late-imported inside the function so this module imports cleanly
    in the sandbox."""
    t0 = time.monotonic()
    try:
        import aiohttp  # late import — sandbox-safe

        base_url = os.environ.get(
            "DOUBLEWORD_BASE_URL", "https://api.doubleword.ai/v1"
        ).rstrip("/")
        api_key = os.environ.get("DOUBLEWORD_API_KEY", "")
        timeout_s = _float_env("JARVIS_FLEET_PROBE_TIMEOUT_S", 60.0)
        url = f"{base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        body = {
            "model": model_id,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False,
        }
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=body) as resp:
                if resp.status != 200:
                    return ProbeResult("", 0.0, 0.0, 0, False, f"http_{resp.status}")
                data = await resp.json()
        total_ms = (time.monotonic() - t0) * 1000.0
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            text = ""
        completion_tokens = 0
        try:
            completion_tokens = int(
                data.get("usage", {}).get("completion_tokens", 0) or 0
            )
        except (TypeError, ValueError, AttributeError):
            completion_tokens = 0
        return ProbeResult(
            text=text,
            ttft_ms=total_ms,
            total_ms=total_ms,
            completion_tokens=completion_tokens,
            ok=True,
            error="",
        )
    except Exception as exc:  # noqa: BLE001
        return ProbeResult("", 0.0, 0.0, 0, False, repr(exc))
