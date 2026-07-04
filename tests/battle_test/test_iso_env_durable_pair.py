"""tests/battle_test/test_iso_env_durable_pair.py -- Task 3 (durability
substrate, "the HARNESS declares both ends of the durable-reroot overlay
pair").

Pins the harness-side declaration of the durable-reroot overlay pair that
``workspace_resolver.resolve_durable_path`` (Task 1) consumes:

  * ``JARVIS_TRINITY_ROOT`` -- the writable per-run state root (pre-existing,
    Blocker #4 fix) -- resolve_durable_path's TO side.
  * ``JARVIS_DURABLE_REROOT_FROM`` -- the isomorphic env's OWN overlay-root
    variable (``env_ctx.root``, e.g. the live-shaped ``/opt/trinity/jarvis``
    symlink the organism believes it lives at), never a hardcoded literal --
    resolve_durable_path's FROM side.

Both keys must land in the SAME composed env dict the organism subprocess
receives, and the FROM value must equal the driver's own ``env_ctx.root``
(proven by varying the fake IsomorphicEnv's root across runs and asserting
equality each time -- a literal would fail on the second run).

Test conventions mirror ``tests/integration/test_isomorphic_a1_e2e.py``'s
``TestSubprocessIsomorphismPropagation`` group (same script-loading shim,
same capturing-compose_env pattern, same stub-soak driver invocation).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Path bootstrap -- scripts are not packages; add them to sys.path
# ---------------------------------------------------------------------------
_REPO_ROOT = str((Path(__file__).parent.parent.parent).resolve())
_SCRIPTS_DIR = str((Path(__file__).parent.parent.parent / "scripts").resolve())

for _p in (_REPO_ROOT, _SCRIPTS_DIR, os.path.join(_REPO_ROOT, "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_script(name: str) -> Any:
    """Load a script by name; return cached module if already in sys.modules.

    Cache-first is CRITICAL: patch.object(mod, attr) must patch the SAME
    object that the driver's _load_module() returns -- both must be the
    same sys.modules entry.
    """
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(_SCRIPTS_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, "Cannot load %s from %s" % (name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # store BEFORE exec (handle circular refs)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_driver_mod = _load_script("isomorphic_a1_local")
IsomorphicA1Driver = _driver_mod.IsomorphicA1Driver


class _MockAdversary:
    async def start(self) -> Dict[str, str]:
        return {"doubleword": "http://127.0.0.1:19996/dw"}

    async def stop(self) -> None:
        pass

    def env_overrides(self) -> Dict[str, str]:
        return {}

    def schedule(self, **_: Any) -> None:
        pass

    def set_simulate_zero_shot(self, enabled: bool) -> None:
        pass


class _NoopEnv:
    """Stand-in for IsomorphicEnv -- ``root`` is the ONLY attribute the
    driver reads off it for the durable-reroot pair, so this fake stays
    deliberately minimal (mirrors the precedent fake in
    test_isomorphic_a1_e2e.py's TestSubprocessIsomorphismPropagation)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def __enter__(self) -> "_NoopEnv":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False


class _CapturingChaos:
    def status(self) -> Dict[str, Any]:
        return {"active": False}

    def inject(self, seed: int) -> bool:
        return True

    def revert(self) -> bool:
        return True


class _CapturingAuditor:
    def watch(self, **kwargs: Any) -> Dict[str, Any]:
        vpath = kwargs.get("verdict_out", "")
        v: Dict[str, Any] = {"proven": False, "failure_locus": "test"}
        if vpath:
            Path(vpath).parent.mkdir(parents=True, exist_ok=True)
            Path(vpath).write_text(json.dumps(v))
        return v


async def _run_driver_and_capture_env(
    tmp_path: Path, overlay_root: Path,
) -> Dict[str, str]:
    """Runs the stub-soak driver end to end and returns the FINAL composed
    env dict (post in-place mutations) the organism subprocess would see."""
    harness_mod = _load_script("a1_live_fire_chaos_harness")
    original_compose = harness_mod.compose_env
    # Capture the dict REFERENCE returned by compose_env so we see the
    # driver's in-place mutations (env["JARVIS_TRINITY_ROOT"] = ...,
    # env["JARVIS_DURABLE_REROOT_FROM"] = ...) that happen AFTER compose_env
    # returns.
    env_ref: List[Dict[str, str]] = []

    def _capturing_compose(**kwargs: Any) -> Dict[str, str]:
        env = original_compose(**kwargs)
        env_ref.append(env)  # store reference; driver mutates in-place
        return env

    driver = IsomorphicA1Driver(
        repo_root=str(tmp_path),
        stub_soak=True,
        seed=0,
        run_root=str(tmp_path / "runs"),
        enable_failover=False,  # default
        _adversary_factory=lambda: _MockAdversary(),
    )

    with (
        patch(
            "backend.core.ouroboros.battle_test.isomorphic_env.IsomorphicEnv",
            return_value=_NoopEnv(overlay_root),
        ),
        patch.object(harness_mod, "compose_env", _capturing_compose),
        patch.object(harness_mod, "ChaosController", lambda **kw: _CapturingChaos()),
        patch.object(harness_mod, "StubAuditorRunner", lambda **kw: _CapturingAuditor()),
    ):
        await driver.run()

    assert env_ref, "compose_env must have been called"
    return env_ref[0]


async def test_composed_env_carries_both_durable_reroot_keys(
    tmp_path: Path,
) -> None:
    """The composed env MUST carry both halves of the durable-reroot overlay
    pair: JARVIS_TRINITY_ROOT (the writable TO side, pre-existing Blocker #4
    fix) and JARVIS_DURABLE_REROOT_FROM (the overlay FROM side, this task)."""
    overlay_root = tmp_path / "opt" / "trinity" / "jarvis"
    overlay_root.mkdir(parents=True, exist_ok=True)

    final_env = await _run_driver_and_capture_env(tmp_path, overlay_root)

    assert "JARVIS_TRINITY_ROOT" in final_env, (
        "JARVIS_TRINITY_ROOT must remain composed (pre-existing Blocker #4 "
        "fix) -- got keys: %r" % sorted(final_env.keys())
    )
    assert "JARVIS_DURABLE_REROOT_FROM" in final_env, (
        "JARVIS_DURABLE_REROOT_FROM must be composed alongside "
        "JARVIS_TRINITY_ROOT so resolve_durable_path() has both halves "
        "of the overlay pair"
    )


async def test_durable_reroot_from_equals_driver_own_overlay_root(
    tmp_path: Path,
) -> None:
    """JARVIS_DURABLE_REROOT_FROM must equal the driver's OWN overlay-root
    variable (env_ctx.root) -- never a hardcoded literal. Proven by varying
    the fake IsomorphicEnv's root across two independent runs and asserting
    the composed value tracks it each time; a literal (e.g. a bare
    "/opt/trinity/jarvis" string) would fail this on the second run because
    the fake root here is a tmp_path-scoped directory, not that literal."""
    for i in range(2):
        overlay_root = tmp_path / ("run_%d" % i) / "opt" / "trinity" / "jarvis"
        overlay_root.mkdir(parents=True, exist_ok=True)

        final_env = await _run_driver_and_capture_env(tmp_path, overlay_root)

        assert final_env["JARVIS_DURABLE_REROOT_FROM"] == str(overlay_root), (
            "FROM must derive from the driver's OWN env_ctx.root variable, "
            "never a literal; got %r for overlay_root=%r"
            % (final_env.get("JARVIS_DURABLE_REROOT_FROM"), overlay_root)
        )
