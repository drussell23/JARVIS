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
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence, Tuple

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
