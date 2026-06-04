"""Containerized test-execution backend for SWE-Bench-Pro scoring (Slice 65).

The barrier that blocked every prior soak: local scoring runs ``pytest`` in the
bare host Python env, which lacks each benchmark repo's dependencies (qutebrowser
needs PyQt, NodeBB needs Node, etc.), so the gold/model tests can't even import
→ ``unresolved``/``scoring_error``. SWE-Bench-Pro ships a prepared Docker image
per problem (``dockerhub_tag``) with the FULL environment for exactly this.

This module is an ALTERNATIVE execution backend for the scorer's apply+run step
— NOT a new scorer. It is gated (``JARVIS_SWE_BENCH_PRO_CONTAINER_EVAL_ENABLED``,
default-OFF → the existing local :func:`scorer.score_evaluation` path is byte-
identical), composes the same ``asyncio.create_subprocess_exec`` shape the scorer
already uses for ``git apply`` (no new hard dependency on the docker SDK), and
hardcodes NOTHING:

  * image namespace is env-overridable (``JARVIS_SWE_BENCH_PRO_IMAGE_NAMESPACE``,
    default ``jefzda/sweap-images`` per the official scaleapi/SWE-bench_Pro-os
    harness); the per-image tag comes from the problem's ``dockerhub_tag``;
  * the ``--platform`` emulation flag is DERIVED from the host architecture
    (Apple-Silicon/aarch64 → ``linux/amd64``; native x86 → none), so the same
    code runs un-emulated on an x86 CI box;
  * the test ids come from the problem's ``fail_to_pass`` + ``pass_to_pass``;
  * the repo root + entrypoint match the verified image contract (``/app``,
    ``--entrypoint bash``), both env-overridable for future image families.

Verified flow (bt-2026-06-02, qutebrowser, gold patch → ``test_error`` PASSED):
``cd /app && git apply <test_patch> && git apply <model_patch> &&
python -m pytest <fail_to_pass ∪ pass_to_pass> -rA``.

The docker invocation is driven through an injectable async runner so the
orchestration is unit-testable without a daemon. NEVER raises into the scorer —
any infra/daemon error surfaces as a populated :class:`ContainerScoreResult`
with ``error`` set (the scorer maps that to ``SCORING_ERROR``, never a crash).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.SWEBenchPro.ContainerEngine")

# --- env knobs (all default-safe; nothing hardcoded at a call site) ---------
CONTAINER_EVAL_ENABLED_ENV_VAR: str = "JARVIS_SWE_BENCH_PRO_CONTAINER_EVAL_ENABLED"
IMAGE_NAMESPACE_ENV_VAR: str = "JARVIS_SWE_BENCH_PRO_IMAGE_NAMESPACE"
REPO_ROOT_ENV_VAR: str = "JARVIS_SWE_BENCH_PRO_CONTAINER_REPO_ROOT"
ENTRYPOINT_ENV_VAR: str = "JARVIS_SWE_BENCH_PRO_CONTAINER_ENTRYPOINT"
DOCKER_BIN_ENV_VAR: str = "JARVIS_SWE_BENCH_PRO_DOCKER_BIN"

_DEFAULT_IMAGE_NAMESPACE: str = "jefzda/sweap-images"   # scaleapi/SWE-bench_Pro-os
_DEFAULT_REPO_ROOT: str = "/app"                        # verified image WorkingDir + git root
_DEFAULT_ENTRYPOINT: str = "bash"                       # images set ENTRYPOINT=[/bin/bash]
_DEFAULT_DOCKER_BIN: str = "docker"

# Host architectures that cannot natively run the linux/amd64 benchmark images
# and therefore require emulation (QEMU/Rosetta via Docker Desktop).
_EMULATION_ARCHES: frozenset = frozenset({"arm64", "aarch64"})

_TRUTHY: frozenset = frozenset({"1", "true", "yes", "on"})

# A pytest -rA result line: "PASSED nodeid" / "FAILED nodeid" / "ERROR nodeid".
_RESULT_LINE = re.compile(r"^(PASSED|FAILED|ERROR)\s+(\S+)", re.MULTILINE)
# Sentinels emitted by the in-container script for non-test failures.
_APPLY_FAIL_SENTINEL = "JARVIS_APPLY_FAIL"
_SETUP_FAIL_SENTINEL = "JARVIS_SETUP_FAIL"


# ---------------------------------------------------------------------------
# Config predicates (pure)
# ---------------------------------------------------------------------------

def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def container_eval_enabled() -> bool:
    """Master switch (§33.1 default-FALSE). OFF → scorer uses its local path."""
    return _env_truthy(CONTAINER_EVAL_ENABLED_ENV_VAR)


def image_namespace() -> str:
    raw = os.environ.get(IMAGE_NAMESPACE_ENV_VAR, "").strip()
    return raw or _DEFAULT_IMAGE_NAMESPACE


def container_repo_root() -> str:
    raw = os.environ.get(REPO_ROOT_ENV_VAR, "").strip()
    return raw or _DEFAULT_REPO_ROOT


def container_entrypoint() -> str:
    raw = os.environ.get(ENTRYPOINT_ENV_VAR, "").strip()
    return raw or _DEFAULT_ENTRYPOINT


def docker_bin() -> str:
    raw = os.environ.get(DOCKER_BIN_ENV_VAR, "").strip()
    return raw or _DEFAULT_DOCKER_BIN


def resolve_image(dockerhub_tag: str) -> str:
    """``<namespace>:<dockerhub_tag>`` — the full pullable reference."""
    return f"{image_namespace()}:{dockerhub_tag}"


def host_platform_flag() -> Optional[str]:
    """``"linux/amd64"`` when the host arch can't natively run the amd64 images
    (Apple Silicon), else ``None`` (native x86 — no emulation flag needed).
    Derived from the host, never hardcoded — same code path on x86 CI."""
    try:
        machine = (platform.machine() or "").strip().lower()
    except Exception:  # noqa: BLE001 — platform probe must never break scoring
        return None
    return "linux/amd64" if machine in _EMULATION_ARCHES else None


# ---------------------------------------------------------------------------
# Problem field access (the dataset folds these into ProblemSpec.metadata)
# ---------------------------------------------------------------------------

def problem_image_tag(problem: Any) -> Optional[str]:
    """The problem's ``dockerhub_tag`` (lives in ``ProblemSpec.metadata``)."""
    meta = getattr(problem, "metadata", None) or {}
    tag = meta.get("dockerhub_tag")
    return tag if isinstance(tag, str) and tag.strip() else None


def should_use_container(problem: Any) -> bool:
    """True iff container eval is enabled AND the problem carries an image tag.
    Pure gate — the scorer falls back to its local path when False."""
    return container_eval_enabled() and problem_image_tag(problem) is not None


def _coerce_id_list(value: Any) -> List[str]:
    """Accept a real list, a JSON-string, OR a Python-repr string; return clean
    ids. The SWE-bench-Pro dataset is INCONSISTENT — ``pass_to_pass`` is JSON
    (double-quoted) but ``fail_to_pass`` is a Python ``repr`` (single-quoted),
    which ``json.loads`` rejects. So try JSON first, then ``ast.literal_eval``
    (literals only — safe), before treating the raw string as a single id."""
    if value is None:
        return []
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        parsed: Any = None
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            try:
                import ast
                parsed = ast.literal_eval(value)
            except (ValueError, SyntaxError, TypeError):
                return [value]
        value = parsed
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    return [str(value).strip()] if str(value).strip() else []


def target_test_ids(problem: Any) -> List[str]:
    """Deduped union of ``fail_to_pass`` + ``pass_to_pass`` test node-ids
    (order-preserving). These are the tests the patch must satisfy."""
    meta = getattr(problem, "metadata", None) or {}
    ids: List[str] = []
    seen: set = set()
    for key in ("fail_to_pass", "pass_to_pass"):
        for tid in _coerce_id_list(meta.get(key)):
            if tid not in seen:
                seen.add(tid)
                ids.append(tid)
    return ids


# ---------------------------------------------------------------------------
# Result model + parsing (pure)
# ---------------------------------------------------------------------------

@dataclass
class ContainerScoreResult:
    """Scorer-compatible shape (mirrors TestRunner.run's total/failed/
    failed_tests) plus an ``error`` channel for infra failures."""
    passed: int = 0
    failed: int = 0
    total: int = 0
    failed_tests: Tuple[str, ...] = ()
    error: Optional[str] = None
    raw_tail: str = ""

    # TestRunner-compatible aliases (so the scorer's getattr() reads work
    # whether it sees a TestRunner result or this one).
    @property
    def tests_total(self) -> int:
        return self.total


def parse_pytest_text(stdout: str, expected_ids: Sequence[str]) -> ContainerScoreResult:
    """Parse ``pytest -rA`` text into pass/fail counts scoped to ``expected_ids``.

    Pure + deterministic. Recognises the in-container failure sentinels first
    (apply/setup), then maps each expected test id to PASSED/FAILED/ERROR from
    the ``-rA`` summary. A required test with no result line counts as failed
    (conservative — a test that never ran is not a pass)."""
    text = stdout or ""
    if _APPLY_FAIL_SENTINEL in text:
        return ContainerScoreResult(error="model_patch_apply_failed", raw_tail=text[-400:])
    if _SETUP_FAIL_SENTINEL in text:
        return ContainerScoreResult(error="container_setup_failed", raw_tail=text[-400:])

    status_by_id: dict = {}
    for m in _RESULT_LINE.finditer(text):
        status_by_id[m.group(2)] = m.group(1)

    expected = [e for e in expected_ids if e]
    if not expected:
        # No explicit ids — fall back to the summary counts.
        passed = len(re.findall(r"^PASSED ", text, re.MULTILINE))
        failed = len(re.findall(r"^(FAILED|ERROR) ", text, re.MULTILINE))
        total = passed + failed
        return ContainerScoreResult(passed=passed, failed=failed, total=total,
                                    raw_tail=text[-400:])

    failed_tests: List[str] = []
    passed = 0
    for tid in expected:
        status = status_by_id.get(tid)
        if status == "PASSED":
            passed += 1
        else:
            failed_tests.append(tid)
    failed = len(failed_tests)
    return ContainerScoreResult(
        passed=passed, failed=failed, total=len(expected),
        failed_tests=tuple(failed_tests), raw_tail=text[-400:],
    )


# ---------------------------------------------------------------------------
# In-container eval script (pure builder, grounded in the verified flow)
# ---------------------------------------------------------------------------

def build_eval_script(
    *, repo_root: str, fail_to_pass: Sequence[str], pass_to_pass: Sequence[str],
    test_patch_path: str = "/jarvis/test.patch",
    model_patch_path: str = "/jarvis/model.patch",
) -> str:
    """Bash script run inside the container (entrypoint=bash, via ``-lc``).

    Order matters and matches the verified flow:
      1. cd to the repo root + resolve the actual git toplevel (adaptive — the
         image's WorkingDir is the git root, but we re-derive it so a different
         image family still works).
      2. ``git apply`` the dataset's ``test_patch`` to MATERIALISE the new tests
         (idempotent — ``|| true`` because some images pre-bake them).
      3. ``git apply`` the candidate model patch; a hard failure emits the
         apply sentinel (so the parser scores it SCORING_ERROR, not a silent 0).
      4. ``pytest`` the explicit fail_to_pass ∪ pass_to_pass node-ids with
         ``-rA`` so every result is a parseable line.
    All test ids are shell-quoted; nothing is interpolated unquoted."""
    union: List[str] = []
    seen: set = set()
    for tid in list(fail_to_pass) + list(pass_to_pass):
        t = (tid or "").strip()
        if t and t not in seen:
            seen.add(t)
            union.append(t)
    quoted_ids = " ".join(shlex.quote(t) for t in union)
    root_q = shlex.quote(repo_root)
    tp_q = shlex.quote(test_patch_path)
    mp_q = shlex.quote(model_patch_path)
    return (
        "set -o pipefail; "
        f"cd {root_q} 2>/dev/null || true; "
        "ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd); cd \"$ROOT\" || "
        f"{{ echo {_SETUP_FAIL_SENTINEL}; exit 4; }}; "
        f"git apply {tp_q} 2>/dev/null || true; "
        f"git apply {mp_q} || {{ echo {_APPLY_FAIL_SENTINEL}; exit 3; }}; "
        f"python -m pytest {quoted_ids} -rA --tb=no -q 2>&1 || true"
    )


# ---------------------------------------------------------------------------
# Docker invocation (injectable async runner; real = `docker` CLI subprocess)
# ---------------------------------------------------------------------------

# (return_code, stdout, stderr)
DockerRun = Callable[[Sequence[str], float], Awaitable[Tuple[int, str, str]]]


async def _real_docker_run(argv: Sequence[str], timeout_s: float) -> Tuple[int, str, str]:
    """Run ``docker ...`` via asyncio subprocess (same shape as
    scorer._git_apply_patch). Bounded by ``timeout_s``; kills + drains on
    timeout. NEVER leaves a zombie — ``--rm`` on the run + proc kill here."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return 124, "", f"docker run exceeded {timeout_s}s"
    return (
        proc.returncode if proc.returncode is not None else -1,
        (out or b"").decode("utf-8", "replace"),
        (err or b"").decode("utf-8", "replace"),
    )


# Slice 92 — zero-trust container hardening profile. These flags neutralize the
# CAPABILITIES of the 5 residual run_body_* runtime sinks (which §50.12 correctly
# leaves open at the STATIC AST layer): the sink may still execute inside the
# jail, but it cannot exfiltrate (no network route), reach the host filesystem
# (read-only rootfs, no host bind), escalate privilege (all caps dropped,
# no-new-privileges + Docker's default seccomp), or fork-bomb (pids-limit). The
# flags are ADDITIVE + OPT-IN: the scoring path (build_docker_argv default) is
# UNCHANGED, because --read-only / --network none would break legitimate held-out
# test execution. Only the runtime verification harness (and any future
# untrusted-eval path) opts in.
def build_hardened_security_argv(*, allow_tmp: bool = True) -> List[str]:
    """The zero-trust docker security flags. Pure (no I/O); composed into a
    ``docker run`` argv by callers that evaluate UN-VETTED code."""
    argv = [
        "--network", "none",            # drop ALL outbound — no exfil/reverse-shell
        "--cap-drop", "ALL",            # strip every Linux capability
        "--security-opt", "no-new-privileges",  # block setuid privilege gain
        "--read-only",                  # immutable rootfs — no host/system writes
        "--pids-limit", "128",          # fork-bomb ceiling
    ]
    if allow_tmp:
        # a writable scratch tmpfs so benign code still runs (capped, non-exec
        # so a dropped payload can't stage + exec a binary from /tmp)
        argv += ["--tmpfs", "/tmp:rw,noexec,nosuid,size=64m"]
    return argv


def build_docker_argv(
    image: str, script: str, patch_dir: str, *, harden: bool = False,
) -> List[str]:
    """The ``docker run`` argv (adaptive --platform, ephemeral --rm, bash
    entrypoint, patches bind-mounted read-only).

    ``harden=True`` (Slice 92) appends the zero-trust security profile. It is
    OFF by default: the scoring path must NOT enable it (--read-only / --network
    none break legitimate held-out test execution). Only callers evaluating
    un-vetted code (the runtime verification harness) opt in."""
    argv: List[str] = [docker_bin(), "run", "--rm"]
    plat = host_platform_flag()
    if plat:
        argv += ["--platform", plat]
    if harden:
        argv += build_hardened_security_argv()
    argv += [
        "--entrypoint", container_entrypoint(),
        "-v", f"{patch_dir}:/jarvis:ro",
        image,
        "-lc", script,
    ]
    return argv


async def run_container_scoring(
    problem: Any,
    model_patch: str,
    *,
    timeout_s: float,
    _docker_run: Optional[DockerRun] = None,
) -> ContainerScoreResult:
    """Score ``model_patch`` for ``problem`` inside its prepared image.

    Returns a :class:`ContainerScoreResult`. NEVER raises (except
    ``CancelledError``): infra/daemon failures come back with ``error`` set so
    the scorer maps them to ``SCORING_ERROR`` rather than crashing the loop."""
    runner = _docker_run or _real_docker_run
    tag = problem_image_tag(problem)
    if not tag:
        return ContainerScoreResult(error="no_dockerhub_tag")
    image = resolve_image(tag)
    expected = target_test_ids(problem)
    if not expected:
        return ContainerScoreResult(error="no_target_test_ids")

    test_patch = getattr(problem, "test_patch", "") or ""
    meta = getattr(problem, "metadata", None) or {}
    f2p = _coerce_id_list(meta.get("fail_to_pass"))
    p2p = _coerce_id_list(meta.get("pass_to_pass"))

    tmp = tempfile.mkdtemp(prefix="jarvis-swebp-")
    try:
        Path(tmp, "test.patch").write_text(test_patch, encoding="utf-8")
        Path(tmp, "model.patch").write_text(model_patch or "", encoding="utf-8")
        script = build_eval_script(
            repo_root=container_repo_root(), fail_to_pass=f2p, pass_to_pass=p2p,
        )
        argv = build_docker_argv(image, script, tmp)
        logger.info(
            "[ContainerEngine] scoring instance=%s image=%s platform=%s tests=%d",
            getattr(problem, "instance_id", "?"), image,
            host_platform_flag() or "native", len(expected),
        )
        try:
            rc, stdout, stderr = await runner(argv, timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — infra failure, never crash
            logger.warning(
                "[ContainerEngine] docker run raised for instance=%s: %s",
                getattr(problem, "instance_id", "?"), exc, exc_info=True,
            )
            return ContainerScoreResult(error=f"docker_run_raised:{type(exc).__name__}")

        if rc == 124:
            return ContainerScoreResult(error="container_timeout", raw_tail=stderr[-400:])
        result = parse_pytest_text(stdout, expected)
        if result.error is None and result.total == 0 and rc != 0:
            # pytest never produced parseable lines AND docker failed → infra.
            result.error = f"container_no_results:rc={rc}:{(stderr or stdout)[-200:]}"
        return result
    finally:
        try:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# FlagRegistry self-registration (auto-discovered by §33.3 walker)
# ---------------------------------------------------------------------------

def register_flags(registry: Any) -> int:
    """Register this module's env flags. Returns count registered. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except ImportError:
        return 0
    specs = [
        FlagSpec(
            name=CONTAINER_EVAL_ENABLED_ENV_VAR, type=FlagType.BOOL,
            category=Category.EXPERIMENTAL, default=False,
            source_file="swe_bench_pro/container_engine.py",
            example="JARVIS_SWE_BENCH_PRO_CONTAINER_EVAL_ENABLED=true",
            description="Score SWE-bench-Pro patches inside the per-problem "
                        "Docker image (full repo env) instead of the local "
                        "python env. Default OFF → local scoring path.",
        ),
        FlagSpec(
            name=IMAGE_NAMESPACE_ENV_VAR, type=FlagType.STR,
            category=Category.INTEGRATION, default=_DEFAULT_IMAGE_NAMESPACE,
            source_file="swe_bench_pro/container_engine.py",
            example=f"{IMAGE_NAMESPACE_ENV_VAR}={_DEFAULT_IMAGE_NAMESPACE}",
            description="Docker image namespace; <namespace>:<dockerhub_tag>.",
        ),
    ]
    n = 0
    for spec in specs:
        try:
            registry.register(spec)
            n += 1
        except Exception:  # noqa: BLE001
            pass
    return n


__all__ = [
    "container_eval_enabled", "image_namespace", "resolve_image",
    "host_platform_flag", "problem_image_tag", "should_use_container",
    "target_test_ids", "parse_pytest_text", "build_eval_script",
    "build_docker_argv", "run_container_scoring", "ContainerScoreResult",
    "register_flags",
]
