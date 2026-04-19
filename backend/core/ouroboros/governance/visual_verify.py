"""Visual VERIFY — deterministic post-APPLY hook for UI-affected ops.

Task 17 of the VisionSensor + Visual VERIFY arc. Implements the Slice 3
deterministic-only surface (app-liveness + pixel-variance + dhash-distance
sanity). The model-assisted advisory verdict is Slice 4 / Task 19.

Architectural contract
----------------------

* **Runs after existing VERIFY, not replacing it.** The TestRunner result
  is authoritative; Visual VERIFY can only **fail** an op — it cannot
  overturn a TestRunner green to red, nor a red to green (§Invariant I4).
* **Only fires on UI-affected ops.** Trigger logic (``should_run_visual_verify``)
  is three-tier deterministic routing per §VERIFY Extension (decision
  D2 in the spec):

      1. Primary — any ``target_files`` matches a frontend glob.
      2. Secondary — ``plan.ui_affected is True``, but *only* when
         ``target_files`` is empty or entirely unclassifiable.
      3. Tertiary — zero TestRunner test targets AND risk tier
         ``NOTIFY_APPLY`` or higher.

* **Consumes ``ctx.attachments``.** Pre-apply + post-apply frames are
  supplied to this module via the ``Attachment`` substrate. Visual
  VERIFY is one of two sanctioned consumers per I7 export-ban
  (``vision_sensor`` is the other).
* **Injectable probes.** Every external dependency (frame-bytes read,
  pixel variance, dhash, app-alive check) is a callable parameter so
  tests exercise the logic without touching the filesystem or Quartz.

Spec: ``docs/superpowers/specs/2026-04-18-vision-sensor-verify-design.md``
§VERIFY Extension + §Invariant I4.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.VisualVerify")


# ---------------------------------------------------------------------------
# Trigger-logic constants (reused from plan_generator — kept in sync
# intentionally; any refactor must touch both files).
# ---------------------------------------------------------------------------

_FRONTEND_EXTENSIONS: frozenset = frozenset({
    ".tsx", ".jsx", ".vue", ".svelte", ".css", ".scss", ".html", ".htm",
})

_CLASSIFIABLE_EXTENSIONS: frozenset = frozenset(
    _FRONTEND_EXTENSIONS
    | {
        ".py", ".rb", ".php",
        ".go", ".rs", ".java", ".kt", ".scala",
        ".cs", ".fs", ".fsx", ".clj", ".cljs", ".cljc",
        ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx",
        ".swift", ".m", ".mm",
        ".ts", ".js", ".mjs", ".cjs",
        ".dart", ".elm", ".ex", ".exs", ".erl", ".hrl",
        ".hs", ".lua", ".pl", ".pm", ".r", ".jl", ".nim", ".zig",
    }
)


# ---------------------------------------------------------------------------
# Config + result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VisualVerifyConfig:
    """Runtime thresholds for the deterministic check battery.

    Every value is env-tunable via ``JARVIS_VISION_VERIFY_*`` knobs at
    construction time (see :meth:`from_env`). Defaults match the spec.
    """

    min_variance: float = 0.01
    """Pixel-variance floor — anything below this is a blank / solid
    screen (all black / all white / render pipeline stalled)."""

    hash_distance_min: float = 0.0
    """Exclusive lower bound on dhash distance. ``0.0`` = identical
    (nothing changed) which is a FAIL for UI ops (we expected a render
    diff after APPLY)."""

    hash_distance_max: float = 0.9
    """Exclusive upper bound on dhash distance. ``1.0`` = total
    scramble which implies render-pipeline corruption — FAIL."""

    render_delay_s: float = 2.0
    """How long the orchestrator waits between a successful APPLY and
    the post-apply frame capture. Not read by this module — documented
    here so the spec reference is in one place."""

    max_image_bytes: int = 10 * 1024 * 1024
    """Hard cap mirroring ``Attachment.read_bytes`` default (10 MiB)."""

    @classmethod
    def from_env(cls) -> "VisualVerifyConfig":
        """Read overrides from env. Silent on malformed values → defaults."""
        def _float(key: str, default: float) -> float:
            try:
                return float(os.environ.get(key, default))
            except (TypeError, ValueError):
                return default

        def _int(key: str, default: int) -> int:
            try:
                return int(os.environ.get(key, default))
            except (TypeError, ValueError):
                return default

        return cls(
            min_variance=_float(
                "JARVIS_VISION_VERIFY_MIN_VARIANCE", cls.min_variance,
            ),
            hash_distance_min=_float(
                "JARVIS_VISION_VERIFY_HASH_DIST_MIN", cls.hash_distance_min,
            ),
            hash_distance_max=_float(
                "JARVIS_VISION_VERIFY_HASH_DIST_MAX", cls.hash_distance_max,
            ),
            render_delay_s=_float(
                "JARVIS_VISION_VERIFY_RENDER_DELAY_S", cls.render_delay_s,
            ),
            max_image_bytes=_int(
                "JARVIS_VISION_VERIFY_MAX_IMAGE_BYTES", cls.max_image_bytes,
            ),
        )


# ---------------------------------------------------------------------------
# Master switches (env-gated; orchestrator consults these before calling
# ``run_if_triggered``)
# ---------------------------------------------------------------------------
#
# Slice 3 (Task 17/18) — deterministic Visual VERIFY. Default OFF until
# the 3-session graduation arc passes (Task 18 Step 3).
# Slice 4 (Task 19/20) — model-assisted advisory. Default OFF until
# its own 3-session arc passes.


def _env_truthy(raw: str) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


def visual_verify_enabled() -> bool:
    """Master switch: ``JARVIS_VISION_VERIFY_ENABLED`` (default ``false``).

    Slice 3 entry default — flips to ``true`` as part of Slice 3
    graduation (Task 18 Step 3). Orchestrator wiring consults this
    before dispatching to :func:`run_if_triggered`.
    """
    return _env_truthy(os.environ.get("JARVIS_VISION_VERIFY_ENABLED", "false"))


def visual_verify_model_assisted_enabled() -> bool:
    """``JARVIS_VISION_VERIFY_MODEL_ASSISTED_ENABLED`` (default ``false``).

    Slice 4 scope — model-assisted advisory verdict on top of the
    deterministic battery. Stays off throughout Slice 3.
    """
    return _env_truthy(
        os.environ.get("JARVIS_VISION_VERIFY_MODEL_ASSISTED_ENABLED", "false"),
    )


# Verdict strings — public constants so orchestrator call-sites don't
# hardcode them.
VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_SKIPPED = "skipped"
_ALL_VERDICTS: frozenset = frozenset({VERDICT_PASS, VERDICT_FAIL, VERDICT_SKIPPED})

# Trigger reasons — operator-visible.
TRIGGER_UI_FILES = "ui_files"
TRIGGER_PLAN_UI_AFFECTED = "plan_ui_affected"
TRIGGER_ZERO_TEST_COVERAGE = "zero_test_coverage"
TRIGGER_NOT_UI_AFFECTED = "not_ui_affected"

# Deterministic-check names — operator-visible in failure logs.
CHECK_APP_CRASHED = "app_crashed"
CHECK_BLANK_SCREEN = "blank_screen"
CHECK_HASH_UNCHANGED = "hash_unchanged"
CHECK_HASH_SCRAMBLED = "hash_scrambled"
CHECK_NO_PRE_FRAME = "no_pre_frame"
CHECK_NO_POST_FRAME = "no_post_frame"
CHECK_DETERMINISTIC_PASS = "deterministic_pass"


@dataclass(frozen=True)
class VisualVerifyResult:
    """Outcome of one Visual VERIFY invocation.

    ``verdict`` is the authoritative signal for downstream dispatch:
    * ``pass`` — deterministic battery completed; op may proceed to
      COMPLETE.
    * ``fail`` — a check named in ``check`` failed; orchestrator routes
      to L2 with ``verify_failure_kind=visual_deterministic``.
    * ``skipped`` — either ``should_run_visual_verify`` returned
      ``False``, or the expected pre/post frames weren't attached
      (operator should check the retention path).
    """

    verdict: str
    check: str
    reasoning: str = ""
    pre_hash: Optional[str] = None
    post_hash: Optional[str] = None
    hash_distance: Optional[float] = None
    post_variance: Optional[float] = None

    def __post_init__(self) -> None:
        if self.verdict not in _ALL_VERDICTS:
            raise ValueError(
                f"VisualVerifyResult.verdict must be one of "
                f"{sorted(_ALL_VERDICTS)}; got {self.verdict!r}"
            )


# ---------------------------------------------------------------------------
# Trigger logic (D2: structured > prose)
# ---------------------------------------------------------------------------


def _ext(path: str) -> str:
    if not path:
        return ""
    norm = path.replace("\\", "/")
    base = norm.rsplit("/", 1)[-1]
    dot = base.rfind(".")
    if dot <= 0:
        return ""
    return base[dot:].lower()


def should_run_visual_verify(
    target_files: Sequence[str],
    *,
    plan_ui_affected: bool = False,
    test_targets_resolved: Optional[Sequence[str]] = None,
    risk_tier: Optional[str] = None,
) -> Tuple[bool, str]:
    """Decide whether the deterministic Visual VERIFY battery should
    run for an op.

    Decision order (D2 — structured signal authoritative, prose only
    when structured is absent):

    1. **Primary** — any ``target_files`` entry has a frontend extension
       → ``(True, TRIGGER_UI_FILES)``.
    2. **Structured-negative** — ``target_files`` non-empty AND at
       least one entry is *classifiable* (frontend or backend) but
       none is frontend → ``(False, TRIGGER_NOT_UI_AFFECTED)``. The
       structured signal wins over prose here.
    3. **Secondary** — ``target_files`` empty OR entirely
       unclassifiable, and ``plan_ui_affected`` is ``True`` →
       ``(True, TRIGGER_PLAN_UI_AFFECTED)``.
    4. **Tertiary** — TestRunner reported zero resolved test targets
       AND ``risk_tier`` is ``notify_apply`` or higher →
       ``(True, TRIGGER_ZERO_TEST_COVERAGE)``.
    5. Else → ``(False, TRIGGER_NOT_UI_AFFECTED)``.

    Returns ``(should_run, reason_tag)`` — the reason tag is logged
    into the verdict's ``reasoning`` field and surfaces in
    SerpentFlow / dashboard output.
    """
    # Primary — frontend extension wins unconditionally.
    for f in target_files:
        if _ext(f) in _FRONTEND_EXTENSIONS:
            return (True, TRIGGER_UI_FILES)

    # Structured-negative — trust the glob when any classifiable
    # evidence is present. Prose hints are ignored in this branch.
    if target_files:
        any_classifiable = any(
            _ext(f) in _CLASSIFIABLE_EXTENSIONS for f in target_files
        )
        if any_classifiable:
            return (False, TRIGGER_NOT_UI_AFFECTED)

    # Secondary — plan prose hint is consulted only when target_files
    # is empty or entirely unclassifiable.
    if plan_ui_affected:
        return (True, TRIGGER_PLAN_UI_AFFECTED)

    # Tertiary — zero test coverage on a NOTIFY_APPLY-or-higher op
    # means we have no other safety net; a visual sanity check is the
    # best thing available.
    if test_targets_resolved is not None and len(test_targets_resolved) == 0:
        if (risk_tier or "").strip().lower() in (
            "notify_apply", "approval_required", "blocked",
        ):
            return (True, TRIGGER_ZERO_TEST_COVERAGE)

    return (False, TRIGGER_NOT_UI_AFFECTED)


# ---------------------------------------------------------------------------
# Deterministic-check probe defaults
# ---------------------------------------------------------------------------


def default_hash_fn(frame_bytes: bytes) -> str:
    """Default hash: sha256 hex of frame bytes.

    Note: this is *not* a perceptual hash. The Ferrari sidecar's
    ``dhash`` field is the real perceptual hash; in production the
    orchestrator provides a dhash. For tests, a plain sha256 is
    sufficient to distinguish "bytes differ" from "bytes identical".
    """
    return hashlib.sha256(frame_bytes).hexdigest()


def default_hash_distance(a: str, b: str) -> float:
    """Default distance: ``0.0`` if strings equal, ``1.0`` otherwise.

    A real dhash distance would compute Hamming distance normalised by
    hash length. For sha256 on identical bytes the hex strings match
    exactly, which gives distance ``0.0`` — matching the "nothing
    changed" branch of the spec. For any differing content we return
    ``1.0`` which falls *above* the spec's ``< 0.9`` upper-bound
    threshold — so any orchestrator using ``default_hash_distance``
    will catch "identical" correctly but cannot distinguish
    meaningfully-different from scrambled. Production code injects a
    real ``hash_distance_fn``.
    """
    return 0.0 if a == b else 1.0


def default_variance_fn(frame_bytes: bytes) -> float:
    """Default variance: normalised byte-entropy proxy.

    Returns ``0.0`` for empty bytes or all-identical bytes;
    monotonically higher for more varied content. This is a coarse
    stand-in for true pixel variance — production injects a real
    PIL/numpy-backed implementation. The approximation is faithful
    enough for the blank-screen FAIL check (all-same-byte content
    always scores 0).
    """
    if not frame_bytes:
        return 0.0
    # Count distinct byte values; divide by 256 for a 0..1 range.
    return len(set(frame_bytes)) / 256.0


def default_app_alive_fn(app_id: Optional[str]) -> bool:
    """Default app-liveness probe: assume alive.

    Production injects a Quartz ``CGWindowListCopyWindowInfo`` query.
    The default value is permissive rather than restrictive so that
    tests and non-macOS environments don't fail Visual VERIFY just
    because the liveness probe is unavailable — production wiring is
    the trust boundary here.
    """
    _ = app_id
    return True


# ---------------------------------------------------------------------------
# Deterministic battery
# ---------------------------------------------------------------------------


def _attachment_by_kind(
    attachments: Sequence[Any], kind: str,
) -> Optional[Any]:
    for a in attachments:
        if getattr(a, "kind", None) == kind:
            return a
    return None


def _read_bytes(att: Any, cfg: VisualVerifyConfig) -> Optional[bytes]:
    """Read bytes off an Attachment, honouring the size cap."""
    try:
        # Attachment.read_bytes already caps internally; passing an
        # explicit max ties it to the VisualVerifyConfig knob.
        return att.read_bytes(max_bytes=cfg.max_image_bytes)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[VisualVerify] attachment read failed (hash8=%s): %s",
            getattr(att, "hash8", "?"), exc,
        )
        return None


def run_deterministic_checks(
    attachments: Sequence[Any],
    *,
    cfg: Optional[VisualVerifyConfig] = None,
    hash_fn: Callable[[bytes], str] = default_hash_fn,
    hash_distance_fn: Callable[[str, str], float] = default_hash_distance,
    variance_fn: Callable[[bytes], float] = default_variance_fn,
    app_alive_fn: Callable[[Optional[str]], bool] = default_app_alive_fn,
) -> VisualVerifyResult:
    """Run the deterministic check battery on a pre+post Attachment pair.

    Failure order (first miss wins — makes failure messages specific):

    1. Pre-frame missing → ``skipped``, ``no_pre_frame``.
    2. Post-frame missing → ``skipped``, ``no_post_frame``.
    3. Target app window no longer exists → ``fail``, ``app_crashed``.
    4. Post-frame variance below ``cfg.min_variance`` (all-black /
       all-white / render stall) → ``fail``, ``blank_screen``.
    5. Hash distance ``<= hash_distance_min`` (identical — nothing
       changed after APPLY) → ``fail``, ``hash_unchanged``.
    6. Hash distance ``>= hash_distance_max`` (total scramble —
       render-pipeline corruption) → ``fail``, ``hash_scrambled``.
    7. Otherwise → ``pass``, ``deterministic_pass``.

    All probe callables are injectable so tests exercise this logic
    without touching disk / Quartz / PIL.
    """
    cfg = cfg or VisualVerifyConfig()
    pre = _attachment_by_kind(attachments, "pre_apply")
    post = _attachment_by_kind(attachments, "post_apply")

    if pre is None:
        return VisualVerifyResult(
            verdict=VERDICT_SKIPPED,
            check=CHECK_NO_PRE_FRAME,
            reasoning="Visual VERIFY skipped — no pre_apply Attachment",
        )
    if post is None:
        return VisualVerifyResult(
            verdict=VERDICT_SKIPPED,
            check=CHECK_NO_POST_FRAME,
            reasoning="Visual VERIFY skipped — no post_apply Attachment",
        )

    # Check 1 — app liveness (pre_apply.app_id must still be alive).
    app_id = getattr(pre, "app_id", None) or getattr(post, "app_id", None)
    try:
        if not app_alive_fn(app_id):
            return VisualVerifyResult(
                verdict=VERDICT_FAIL,
                check=CHECK_APP_CRASHED,
                reasoning=f"target app window missing post-apply (app_id={app_id!r})",
            )
    except Exception as exc:  # noqa: BLE001
        # Treat a probe-layer error as advisory — we don't know the
        # state, and failing on probe error would block every op on
        # any transient Quartz hiccup. Log and continue.
        logger.debug("[VisualVerify] app_alive_fn raised: %s", exc)

    # Read bytes for variance + hash checks.
    pre_bytes = _read_bytes(pre, cfg)
    post_bytes = _read_bytes(post, cfg)
    if pre_bytes is None:
        return VisualVerifyResult(
            verdict=VERDICT_SKIPPED,
            check=CHECK_NO_PRE_FRAME,
            reasoning="pre_apply Attachment unreadable",
        )
    if post_bytes is None:
        return VisualVerifyResult(
            verdict=VERDICT_SKIPPED,
            check=CHECK_NO_POST_FRAME,
            reasoning="post_apply Attachment unreadable",
        )

    # Check 2 — post-frame variance (blank-screen guard).
    try:
        post_variance = float(variance_fn(post_bytes))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[VisualVerify] variance_fn raised: %s", exc)
        post_variance = 1.0   # treat probe failure as "not blank"

    if post_variance < cfg.min_variance:
        return VisualVerifyResult(
            verdict=VERDICT_FAIL,
            check=CHECK_BLANK_SCREEN,
            reasoning=(
                f"post-apply frame variance {post_variance:.4f} below "
                f"min {cfg.min_variance:.4f} — likely blank render"
            ),
            post_variance=post_variance,
        )

    # Check 3/4 — hash-distance sanity.
    try:
        pre_hash = hash_fn(pre_bytes)
        post_hash = hash_fn(post_bytes)
        distance = float(hash_distance_fn(pre_hash, post_hash))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[VisualVerify] hash probe raised: %s", exc)
        # Without hash probes we can't run checks 3/4; treat as
        # advisory-pass since app-liveness and variance already
        # guard against the worst-case regressions.
        return VisualVerifyResult(
            verdict=VERDICT_PASS,
            check=CHECK_DETERMINISTIC_PASS,
            reasoning=(
                "hash probes unavailable — proceeded on liveness + "
                "variance alone"
            ),
            post_variance=post_variance,
        )

    if distance <= cfg.hash_distance_min:
        return VisualVerifyResult(
            verdict=VERDICT_FAIL,
            check=CHECK_HASH_UNCHANGED,
            reasoning=(
                f"pre and post frames identical (distance={distance:.4f}) — "
                "UI op produced no visible change"
            ),
            pre_hash=pre_hash,
            post_hash=post_hash,
            hash_distance=distance,
            post_variance=post_variance,
        )

    if distance >= cfg.hash_distance_max:
        return VisualVerifyResult(
            verdict=VERDICT_FAIL,
            check=CHECK_HASH_SCRAMBLED,
            reasoning=(
                f"pre and post frames maximally divergent "
                f"(distance={distance:.4f}) — render-pipeline corruption"
            ),
            pre_hash=pre_hash,
            post_hash=post_hash,
            hash_distance=distance,
            post_variance=post_variance,
        )

    return VisualVerifyResult(
        verdict=VERDICT_PASS,
        check=CHECK_DETERMINISTIC_PASS,
        reasoning=(
            f"deterministic battery passed: variance={post_variance:.4f}, "
            f"hash_distance={distance:.4f}"
        ),
        pre_hash=pre_hash,
        post_hash=post_hash,
        hash_distance=distance,
        post_variance=post_variance,
    )


# ---------------------------------------------------------------------------
# Orchestrator entry point — respects I4 asymmetry
# ---------------------------------------------------------------------------


def run_if_triggered(
    *,
    target_files: Sequence[str],
    attachments: Sequence[Any],
    plan_ui_affected: bool = False,
    test_targets_resolved: Optional[Sequence[str]] = None,
    risk_tier: Optional[str] = None,
    test_runner_result: Optional[str] = None,
    cfg: Optional[VisualVerifyConfig] = None,
    hash_fn: Callable[[bytes], str] = default_hash_fn,
    hash_distance_fn: Callable[[str, str], float] = default_hash_distance,
    variance_fn: Callable[[bytes], float] = default_variance_fn,
    app_alive_fn: Callable[[Optional[str]], bool] = default_app_alive_fn,
) -> VisualVerifyResult:
    """Top-level orchestrator entry.

    Checks the trigger, runs the deterministic battery, and returns a
    :class:`VisualVerifyResult`. When ``should_run_visual_verify``
    says No, returns a ``skipped`` result naming the reason — the
    orchestrator logs it and proceeds to COMPLETE.

    **I4 asymmetry**: ``test_runner_result`` is accepted so this entry
    point can enforce the "Visual VERIFY can only FAIL an op, never
    turn a TestRunner red into green" invariant. When
    ``test_runner_result == "failed"``, the returned verdict is
    always ``fail`` (the deterministic checks still run for
    observability, but the verdict is clamped). Visual VERIFY's
    contribution is *additive failure signal*, never corrective.
    """
    should_run, reason = should_run_visual_verify(
        target_files,
        plan_ui_affected=plan_ui_affected,
        test_targets_resolved=test_targets_resolved,
        risk_tier=risk_tier,
    )
    if not should_run:
        return VisualVerifyResult(
            verdict=VERDICT_SKIPPED,
            check=reason,
            reasoning=f"trigger=no ({reason})",
        )

    result = run_deterministic_checks(
        attachments,
        cfg=cfg,
        hash_fn=hash_fn,
        hash_distance_fn=hash_distance_fn,
        variance_fn=variance_fn,
        app_alive_fn=app_alive_fn,
    )

    # I4 asymmetry enforcement: if TestRunner said the op failed,
    # Visual VERIFY cannot rescue it — clamp to fail regardless of
    # the deterministic outcome. The checks still ran for audit.
    if (test_runner_result or "").strip().lower() in ("failed", "fail", "red"):
        if result.verdict == VERDICT_PASS:
            return VisualVerifyResult(
                verdict=VERDICT_FAIL,
                check=result.check,
                reasoning=(
                    "TestRunner red + Visual VERIFY pass → clamped to "
                    "fail per I4 asymmetry (Visual VERIFY cannot "
                    "overturn TestRunner red into green)"
                ),
                pre_hash=result.pre_hash,
                post_hash=result.post_hash,
                hash_distance=result.hash_distance,
                post_variance=result.post_variance,
            )
        # Already fail/skipped — pass through unchanged.

    return result


# ===========================================================================
# Slice 4 — Model-assisted advisory verdict (Task 19)
# ===========================================================================
#
# Deterministic Visual VERIFY answers "did *something* visibly change?"
# The advisory layer answers "did the change *achieve the op's stated
# intent?*" — a VLM (Qwen3-VL-235B via lean_loop) compares pre/post
# frames against the op's description and emits a verdict.
#
# Advisory is strictly **additive failure signal**. Per I4 asymmetry
# it CAN route to L2 on a ``regressed`` verdict (+above-threshold
# confidence); it CANNOT rescue a TestRunner-or-deterministic fail.
# Advisory is gated behind its own master switch (Slice 4 graduation
# criterion, Task 20).

# Advisory verdict enum.
ADVISORY_ALIGNED = "aligned"         # pre + post match the stated intent
ADVISORY_REGRESSED = "regressed"     # post diverges from the intent
ADVISORY_UNCLEAR = "unclear"         # VLM couldn't judge
_ADVISORY_VERDICTS: frozenset = frozenset({
    ADVISORY_ALIGNED, ADVISORY_REGRESSED, ADVISORY_UNCLEAR,
})

# Confirmation values (human-provided via /verify-confirm).
CONFIRM_AGREE = "agree"
CONFIRM_DISAGREE = "disagree"
_CONFIRMATION_VALUES: frozenset = frozenset({CONFIRM_AGREE, CONFIRM_DISAGREE})

# Default confidence threshold above which a ``regressed`` verdict
# routes to L2 (spec §Graduation Slice 4).
_DEFAULT_REGRESS_CONFIDENCE = float(
    os.environ.get("JARVIS_VISION_VERIFY_REGRESS_CONFIDENCE", "0.80"),
)

# Advisory ledger — disk-persisted, per-session rolling record of
# advisory verdicts + human confirmations. Feeds Slice 4 graduation
# criteria (≥60% human-agreement on regressed verdicts) and the
# auto-demotion guardrail (FP rate ≥50% → demotion).
_DEFAULT_ADVISORY_LEDGER_PATH = ".jarvis/vision_verify_advisory_ledger.json"

# Auto-demotion thresholds.
_DEMOTION_FP_THRESHOLD = 0.50
_DEMOTION_MIN_SAMPLES = 3   # need at least 3 regressed+confirmed entries
_DEMOTION_FLAG_PATH = ".jarvis/vision_verify_model_assisted_demoted.flag"


@dataclass(frozen=True)
class AdvisoryVerdict:
    """One VLM advisory outcome (input to the L2-routing decision)."""

    verdict: str                       # aligned | regressed | unclear
    confidence: float                  # [0.0, 1.0]
    reasoning: str = ""
    model: str = "qwen3-vl-235b"

    def __post_init__(self) -> None:
        if self.verdict not in _ADVISORY_VERDICTS:
            raise ValueError(
                f"AdvisoryVerdict.verdict must be one of "
                f"{sorted(_ADVISORY_VERDICTS)}; got {self.verdict!r}"
            )
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"AdvisoryVerdict.confidence must be in [0.0, 1.0]; "
                f"got {self.confidence!r}"
            )


@dataclass(frozen=True)
class AdvisoryOutcome:
    """Outcome of the ``run_advisory`` entry point.

    ``l2_triggered`` is the single fact the orchestrator consumes —
    when True, dispatch the op to L2 Repair with ``advisory.reasoning``
    as the correction prompt. When False, the advisory is logged for
    observability and the deterministic verdict stands.
    """

    advisory: Optional[AdvisoryVerdict]
    l2_triggered: bool
    reason: str = ""


# ---------------------------------------------------------------------------
# run_advisory — the VLM entry point
# ---------------------------------------------------------------------------


def run_advisory(
    *,
    attachments: Sequence[Any],
    op_description: str,
    advisory_fn: Optional[Callable[..., Dict[str, Any]]],
    confidence_threshold: float = _DEFAULT_REGRESS_CONFIDENCE,
    cfg: Optional[VisualVerifyConfig] = None,
) -> AdvisoryOutcome:
    """Run the model-assisted advisory layer.

    ``advisory_fn`` is an injectable callable receiving
    ``(pre_bytes, post_bytes, op_intent)`` and returning a dict with
    keys ``verdict`` / ``confidence`` / ``reasoning`` / ``model``.
    ``None`` or a raising callable yields a ``skipped`` outcome.

    Never raises. I4 asymmetry: the advisory alone cannot clamp
    pass→fail; it only sets ``l2_triggered=True`` which the
    orchestrator uses to dispatch L2 Repair. The deterministic
    verdict remains authoritative.
    """
    cfg = cfg or VisualVerifyConfig()
    if advisory_fn is None:
        return AdvisoryOutcome(
            advisory=None,
            l2_triggered=False,
            reason="advisory_fn unavailable",
        )

    pre = _attachment_by_kind(attachments, "pre_apply")
    post = _attachment_by_kind(attachments, "post_apply")
    if pre is None or post is None:
        return AdvisoryOutcome(
            advisory=None,
            l2_triggered=False,
            reason="missing pre/post attachment",
        )

    pre_bytes = _read_bytes(pre, cfg)
    post_bytes = _read_bytes(post, cfg)
    if pre_bytes is None or post_bytes is None:
        return AdvisoryOutcome(
            advisory=None,
            l2_triggered=False,
            reason="attachment read failed",
        )

    try:
        raw = advisory_fn(pre_bytes, post_bytes, op_description)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[VisualVerify] advisory_fn raised: %s", exc)
        return AdvisoryOutcome(
            advisory=None,
            l2_triggered=False,
            reason=f"advisory_fn raised: {type(exc).__name__}",
        )

    if not isinstance(raw, dict):
        logger.debug(
            "[VisualVerify] advisory_fn returned non-dict: %r",
            type(raw).__name__,
        )
        return AdvisoryOutcome(
            advisory=None,
            l2_triggered=False,
            reason="advisory_fn returned non-dict",
        )

    verdict = str(raw.get("verdict", "")).strip().lower()
    if verdict not in _ADVISORY_VERDICTS:
        return AdvisoryOutcome(
            advisory=None,
            l2_triggered=False,
            reason=f"unknown advisory verdict: {verdict!r}",
        )

    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    reasoning = str(raw.get("reasoning", ""))
    # Sanitize VLM reasoning — same firewall pattern as Tier 2 classifier.
    try:
        from backend.core.ouroboros.governance.semantic_firewall import (
            sanitize_for_firewall,
        )
        sanitized = sanitize_for_firewall(reasoning, field_name="advisory_reasoning")
        if sanitized.rejected and any(
            "injection pattern hit" in r for r in sanitized.reasons
        ):
            reasoning = "[sanitized:prompt_injection_detected]"
        else:
            reasoning = sanitized.sanitized
    except Exception:  # noqa: BLE001
        pass

    model = str(raw.get("model", "qwen3-vl-235b"))[:64] or "qwen3-vl-235b"

    advisory = AdvisoryVerdict(
        verdict=verdict,
        confidence=confidence,
        reasoning=reasoning,
        model=model,
    )

    # L2 routing rule (spec §Advisory): regressed + above confidence → True.
    l2 = (
        verdict == ADVISORY_REGRESSED
        and confidence > confidence_threshold
    )
    if l2:
        reason_tag = (
            f"regressed above threshold "
            f"({confidence:.2f} > {confidence_threshold:.2f})"
        )
    elif verdict == ADVISORY_REGRESSED:
        reason_tag = (
            f"regressed below threshold "
            f"({confidence:.2f} <= {confidence_threshold:.2f})"
        )
    else:
        reason_tag = f"verdict={verdict} (no L2 dispatch)"

    return AdvisoryOutcome(
        advisory=advisory,
        l2_triggered=l2,
        reason=reason_tag,
    )


# ---------------------------------------------------------------------------
# Advisory ledger — disk-persisted verdict + confirmation record
# ---------------------------------------------------------------------------


class AdvisoryLedger:
    """Tracks advisory emissions + human confirmations.

    Schema: list of entries, each
    ``{op_id, verdict, confidence, l2_triggered, ts, human_confirmation}``.
    ``human_confirmation`` is ``None`` until the operator runs
    ``/verify-confirm <op-id> {agree|disagree}``.

    FP rate = disagreements / (agrees + disagrees) over ``regressed``
    verdicts only — aligned/unclear don't fire L2 so they don't
    contribute to the L2-accuracy metric.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = Path(
            path or (Path.cwd() / _DEFAULT_ADVISORY_LEDGER_PATH)
        )
        self._entries: List[Dict[str, Any]] = []
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def entries(self) -> List[Dict[str, Any]]:
        # Defensive copy — callers can't mutate ledger state accidentally.
        return [dict(e) for e in self._entries]

    def _load(self) -> None:
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        entries = data.get("entries")
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            op_id = entry.get("op_id")
            verdict = entry.get("verdict")
            if (
                isinstance(op_id, str) and op_id
                and verdict in _ADVISORY_VERDICTS
            ):
                self._entries.append(dict(entry))

    def _persist(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "schema_version": 1,
                "entries": self._entries,
                "last_updated_ts": time.time(),
            }
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(str(tmp), str(self._path))
        except (OSError, TypeError, ValueError):
            logger.debug(
                "[VisualVerify] advisory ledger persist failed", exc_info=True,
            )

    def record_advisory(
        self,
        *,
        op_id: str,
        advisory: AdvisoryVerdict,
        l2_triggered: bool,
    ) -> None:
        """Append a new advisory emission to the ledger."""
        if not op_id:
            raise ValueError("record_advisory requires a non-empty op_id")
        self._entries.append({
            "op_id": op_id,
            "verdict": advisory.verdict,
            "confidence": advisory.confidence,
            "model": advisory.model,
            "reasoning_hash": hashlib.sha256(
                advisory.reasoning.encode("utf-8"),
            ).hexdigest()[:16],
            "l2_triggered": bool(l2_triggered),
            "ts": time.time(),
            "human_confirmation": None,
        })
        self._persist()

    def record_confirmation(self, *, op_id: str, confirmation: str) -> bool:
        """Stamp a human confirmation onto an existing ledger entry.

        Returns ``True`` if an entry was updated, ``False`` if no
        matching op_id was found. Most-recent entry wins when an op
        has multiple emissions (the confirmation applies to the latest).
        """
        if confirmation not in _CONFIRMATION_VALUES:
            raise ValueError(
                f"confirmation must be one of {sorted(_CONFIRMATION_VALUES)}; "
                f"got {confirmation!r}"
            )
        # Find the most-recent entry matching op_id.
        for entry in reversed(self._entries):
            if entry.get("op_id") == op_id:
                entry["human_confirmation"] = confirmation
                entry["confirmed_ts"] = time.time()
                self._persist()
                return True
        return False

    def fp_rate_on_regressed(
        self, *, min_samples: int = _DEMOTION_MIN_SAMPLES,
    ) -> Optional[float]:
        """Fraction of ``regressed`` verdicts the human marked disagree.

        Returns ``None`` when the confirmed-regressed count is below
        ``min_samples`` — we need enough data to trust the rate.
        """
        total = 0
        disagreements = 0
        for entry in self._entries:
            if entry.get("verdict") != ADVISORY_REGRESSED:
                continue
            confirmation = entry.get("human_confirmation")
            if confirmation == CONFIRM_AGREE:
                total += 1
            elif confirmation == CONFIRM_DISAGREE:
                total += 1
                disagreements += 1
        if total < min_samples:
            return None
        return disagreements / total


# ---------------------------------------------------------------------------
# Auto-demotion (Slice 4 post-graduation guardrail)
# ---------------------------------------------------------------------------


def is_model_assisted_demoted(
    flag_path: Optional[str] = None,
) -> bool:
    """Return ``True`` when the persistent demotion flag is set.

    Slice 4 post-graduation guardrail: a session whose advisory
    FP rate exceeds the demotion threshold writes this flag; the
    next session boot reads it and keeps model-assisted off until
    the operator runs ``/verify-undemote`` (or the flag file is
    manually removed).
    """
    p = Path(flag_path or (Path.cwd() / _DEMOTION_FLAG_PATH))
    return p.exists()


def set_model_assisted_demoted(
    *,
    reason: str,
    flag_path: Optional[str] = None,
) -> None:
    """Atomically write the demotion flag with a reason payload."""
    p = Path(flag_path or (Path.cwd() / _DEMOTION_FLAG_PATH))
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {"demoted_at": time.time(), "reason": reason}
        p.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        logger.debug(
            "[VisualVerify] demotion flag write failed", exc_info=True,
        )


def clear_model_assisted_demotion(
    flag_path: Optional[str] = None,
) -> bool:
    """Clear the demotion flag (via ``/verify-undemote`` REPL command).

    Returns ``True`` when a flag was removed, ``False`` when none was
    present. Idempotent.
    """
    p = Path(flag_path or (Path.cwd() / _DEMOTION_FLAG_PATH))
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        logger.debug(
            "[VisualVerify] demotion flag clear failed", exc_info=True,
        )
        return False


def model_assisted_active() -> bool:
    """Effective enablement: env master switch AND no demotion flag.

    Orchestrator call-site consults this (not
    :func:`visual_verify_model_assisted_enabled` directly) so a
    demotion silently suppresses advisory dispatch without the
    operator having to unset the env.
    """
    return (
        visual_verify_model_assisted_enabled()
        and not is_model_assisted_demoted()
    )


def check_and_apply_auto_demotion(
    ledger: AdvisoryLedger,
    *,
    threshold: float = _DEMOTION_FP_THRESHOLD,
    min_samples: int = _DEMOTION_MIN_SAMPLES,
    flag_path: Optional[str] = None,
) -> Tuple[bool, Optional[float]]:
    """Check the ledger's FP rate and auto-demote if above threshold.

    Called at session end by the orchestrator (or battle-test harness)
    before shutdown. Returns ``(did_demote, measured_rate)`` — when
    ``did_demote`` is ``True``, the next session boot will see
    ``model_assisted_active() is False``.

    Idempotent: calling twice in one session where the first call
    already demoted does not double-write.
    """
    rate = ledger.fp_rate_on_regressed(min_samples=min_samples)
    if rate is None:
        return (False, None)
    if rate < threshold:
        return (False, rate)
    if is_model_assisted_demoted(flag_path=flag_path):
        return (False, rate)  # already demoted — idempotent
    set_model_assisted_demoted(
        reason=f"advisory FP rate {rate:.2%} >= {threshold:.2%}",
        flag_path=flag_path,
    )
    return (True, rate)


# ---------------------------------------------------------------------------
# REPL command handler — ``/verify-confirm <op-id> {agree|disagree}``
# ---------------------------------------------------------------------------


def handle_verify_confirm_command(
    args: str,
    *,
    ledger: Optional[AdvisoryLedger] = None,
) -> str:
    """Parse and apply a ``/verify-confirm`` REPL invocation.

    Returns a human-readable response string for SerpentFlow. Never
    raises — malformed input produces a usage hint instead.

    Input shape::

        <op-id> agree
        <op-id> disagree

    Case-insensitive on the verb; tolerant of extra whitespace.
    """
    tokens = (args or "").strip().split()
    if len(tokens) != 2:
        return (
            "usage: /verify-confirm <op-id> {agree|disagree}\n"
            "  marks the most recent advisory verdict for the given op "
            "as either confirmed (agree) or a false positive (disagree)."
        )
    op_id, verb = tokens[0], tokens[1].strip().lower()
    if verb not in _CONFIRMATION_VALUES:
        return (
            f"/verify-confirm: unknown verb {verb!r}; "
            f"must be 'agree' or 'disagree'"
        )
    led = ledger or AdvisoryLedger()
    try:
        updated = led.record_confirmation(op_id=op_id, confirmation=verb)
    except ValueError as exc:
        return f"/verify-confirm: {exc}"
    if not updated:
        return (
            f"/verify-confirm: no advisory entry found for op_id={op_id!r} "
            f"(was an advisory actually emitted for this op?)"
        )
    return f"/verify-confirm: op={op_id} marked {verb}"


def handle_verify_undemote_command(
    *,
    flag_path: Optional[str] = None,
) -> str:
    """``/verify-undemote`` — clear the Slice 4 auto-demotion flag.

    Returns a human-readable response. Idempotent.
    """
    cleared = clear_model_assisted_demotion(flag_path=flag_path)
    if cleared:
        return "/verify-undemote: demotion flag cleared; model-assisted re-armed"
    return "/verify-undemote: no demotion flag present"
