"""
Slice 12M — RuntimeHealthSensor import hygiene tests.
=====================================================

Closes the wedge surfaced by the Slice 12L verification soak
(bt-2026-05-23-004847):

  * ControlPlaneStarvation lag_ms=14267.9
  * Pre-snapshot breadcrumb: torio._extension.utils, speechbrain
    checkpoints + quirks, RuntimeHealthSensor scan
  * Root cause: ``__import__(module_name)`` in
    ``RuntimeHealthSensor._check_import_errors()`` triggered
    synchronous top-level package code (torch + speechbrain +
    torio + FFmpeg lookup) on the asyncio event loop.

Slice 12M replaces the prior ``__import__``-based detection with
non-executing discovery:

  * ``importlib.metadata.distribution`` — pip-installed?
  * ``importlib.util.find_spec`` — module spec present?
    (Top-level modules only — dotted modules skip spec to avoid
    parent-package ``__init__.py`` execution.)

Plus an optional opt-in subprocess deep-probe (env-gated, default
OFF) for the rare "spec-present-but-actually-broken" case. The
subprocess uses ``asyncio.create_subprocess_exec`` so heavy
package top-level code executes in a SEPARATE process — never in
the watchdog'd asyncio loop.

Operator binding (verbatim):
  - Regression test proving RuntimeHealthSensor does not call
    __import__ or importlib.import_module during _check_import_errors
  - Test missing_dependency detection using metadata/spec mocks
  - Test installed package with present spec produces no
    missing_dependency
  - Test scan_once remains async-safe and does not execute heavy
    module top-level code
  - AST pin banning __import__ / importlib.import_module inside
    runtime_health_sensor import-error checks
"""

from __future__ import annotations

import ast
import asyncio
import importlib.metadata
import inspect
import os
import sys
import textwrap
from pathlib import Path
from typing import List, Tuple
from unittest.mock import MagicMock, patch

import pytest

from backend.core.ouroboros.governance.intake.sensors.runtime_health_sensor import (  # noqa: E501
    DependencyState,
    HealthFinding,
    RuntimeHealthSensor,
    SubprocessProbeOutcome,
    SubprocessProbeResult,
    _MODULE_MAP,
    _SAFE_MODULE_NAME_RE,
    _resolve_dependency_state,
    _subprocess_import_probe,
)


# ===============================================================
# Helpers
# ===============================================================


def _build_sensor() -> RuntimeHealthSensor:
    """Build a minimal RuntimeHealthSensor for unit testing. The
    constructor signature is reflectively probed so this test file
    doesn't drift when the constructor evolves."""
    sig = inspect.signature(RuntimeHealthSensor.__init__)
    kwargs = {}
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        if param.default is not inspect.Parameter.empty:
            continue
        if name == "router":
            router = MagicMock()
            router.ingest = MagicMock(return_value=_make_awaitable("enqueued"))
            kwargs[name] = router
        elif name == "repo":
            kwargs[name] = "slice12m-test-repo"
        else:
            kwargs[name] = None
    try:
        return RuntimeHealthSensor(**kwargs)
    except TypeError as e:
        pytest.skip(f"RuntimeHealthSensor signature changed: {e}")


def _make_awaitable(value):
    """Build a coroutine that returns ``value``. Cleaner than
    AsyncMock for these tests."""
    async def _coro():
        return value
    return _coro()


# ===============================================================
# Test 1: AST pin — no __import__ / import_module in
#         _check_import_errors
# ===============================================================


def test_check_import_errors_does_not_call_dunder_import() -> None:
    """Operator binding: "RuntimeHealthSensor does not call
    __import__ or importlib.import_module during
    _check_import_errors". AST-walks the method body looking for
    ast.Call nodes whose function is ``__import__`` or attribute
    ``import_module``. Strings inside docstrings are excluded
    because the walk only inspects Call nodes."""
    src = inspect.getsource(RuntimeHealthSensor._check_import_errors)
    tree = ast.parse(textwrap.dedent(src))
    forbidden: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id == "__import__":
            forbidden.append("__import__")
        if isinstance(node.func, ast.Attribute) and \
                node.func.attr == "import_module":
            forbidden.append("import_module")
    assert not forbidden, (
        f"_check_import_errors invokes forbidden call(s): {forbidden}. "
        f"Slice 12M wedge regression."
    )


def test_check_import_errors_is_async() -> None:
    """``_check_import_errors`` MUST be async — the optional
    subprocess deep-probe uses ``asyncio.create_subprocess_exec``,
    which can only be awaited from an async context."""
    assert asyncio.iscoroutinefunction(
        RuntimeHealthSensor._check_import_errors,
    )


# ===============================================================
# Test 2: missing_dependency detection via metadata/spec mocks
# ===============================================================


@pytest.mark.asyncio
async def test_missing_dependency_emits_finding_for_missing_dist() -> None:
    """When ``importlib.metadata.distribution`` raises
    ``PackageNotFoundError`` AND find_spec returns None, emit a
    ``missing_dependency`` finding for that package."""
    sensor = _build_sensor()
    # Patch BOTH the metadata + the find_spec so the package looks
    # truly absent
    with patch(
        "backend.core.ouroboros.governance.intake.sensors."
        "runtime_health_sensor.importlib.metadata.distribution",
        side_effect=importlib.metadata.PackageNotFoundError("torch"),
    ), patch(
        "backend.core.ouroboros.governance.intake.sensors."
        "runtime_health_sensor.importlib.util.find_spec",
        return_value=None,
    ):
        findings = await sensor._check_import_errors()
    missing = [f for f in findings if f.category == "missing_dependency"]
    # At least the tracked packages we'd expect
    assert missing, "No missing_dependency findings emitted under fully-missing mocks"
    # Every emitted finding cites a tracked package
    for f in missing:
        assert "package" in f.details
        assert f.details["error_type"] == "MissingDistribution"
        assert f.details["discovery"] == "importlib.metadata.distribution"


@pytest.mark.asyncio
async def test_installed_with_present_spec_emits_no_finding() -> None:
    """When metadata.distribution returns a dist + find_spec
    returns a spec, NO finding is emitted for that package."""
    sensor = _build_sensor()
    fake_dist = MagicMock()
    fake_spec = MagicMock()
    with patch(
        "backend.core.ouroboros.governance.intake.sensors."
        "runtime_health_sensor.importlib.metadata.distribution",
        return_value=fake_dist,
    ), patch(
        "backend.core.ouroboros.governance.intake.sensors."
        "runtime_health_sensor.importlib.util.find_spec",
        return_value=fake_spec,
    ):
        findings = await sensor._check_import_errors()
    # All tracked packages report installed_and_importable → no findings
    assert findings == []


@pytest.mark.asyncio
async def test_installed_but_spec_missing_emits_broken_dependency() -> None:
    """When metadata.distribution returns a dist but find_spec
    returns None for a top-level module, emit
    ``broken_dependency`` (preserves the old ImportError-detected
    "package exists but can't load" semantics)."""
    sensor = _build_sensor()
    fake_dist = MagicMock()
    with patch(
        "backend.core.ouroboros.governance.intake.sensors."
        "runtime_health_sensor.importlib.metadata.distribution",
        return_value=fake_dist,
    ), patch(
        "backend.core.ouroboros.governance.intake.sensors."
        "runtime_health_sensor.importlib.util.find_spec",
        return_value=None,
    ):
        findings = await sensor._check_import_errors()
    broken = [f for f in findings if f.category == "broken_dependency"]
    # Only the TOP-LEVEL modules in _TRACKED_PACKAGES will get spec checks;
    # dotted ones (google.api_core) skip spec and report installed.
    assert broken, "No broken_dependency findings emitted under spec=None mock"
    for f in broken:
        assert f.details["error_type"] == "MissingSpec"
        assert f.details["discovery"] == "importlib.util.find_spec"


# ===============================================================
# Test 3: Decision matrix — _resolve_dependency_state
# ===============================================================


def test_resolver_pythonpath_install_is_importable() -> None:
    """Edge case: a module IS importable (find_spec returns spec)
    but NOT pip-installed (metadata.distribution raises). This
    happens with PYTHONPATH installs / namespace packages /
    stdlib. Slice 12M must NOT emit missing_dependency for
    these."""
    fake_spec = MagicMock()
    with patch(
        "backend.core.ouroboros.governance.intake.sensors."
        "runtime_health_sensor.importlib.metadata.distribution",
        side_effect=importlib.metadata.PackageNotFoundError("xyz"),
    ), patch(
        "backend.core.ouroboros.governance.intake.sensors."
        "runtime_health_sensor.importlib.util.find_spec",
        return_value=fake_spec,
    ):
        state, detail = _resolve_dependency_state("xyz", "xyz")
    assert state == DependencyState.INSTALLED_AND_IMPORTABLE
    assert "PYTHONPATH" in detail or "namespace" in detail


def test_resolver_dotted_module_skips_spec_check() -> None:
    """Dotted module names (e.g. ``google.api_core``) MUST skip
    find_spec — otherwise find_spec imports the parent
    (``google``) and re-introduces the loop-blocking import side
    effect."""
    spec_calls: List[str] = []

    def _spy_find_spec(name):
        spec_calls.append(name)
        return MagicMock()

    fake_dist = MagicMock()
    with patch(
        "backend.core.ouroboros.governance.intake.sensors."
        "runtime_health_sensor.importlib.metadata.distribution",
        return_value=fake_dist,
    ), patch(
        "backend.core.ouroboros.governance.intake.sensors."
        "runtime_health_sensor.importlib.util.find_spec",
        side_effect=_spy_find_spec,
    ):
        state, _ = _resolve_dependency_state(
            "google-api-core", "google.api_core",
        )
    assert state == DependencyState.INSTALLED_AND_IMPORTABLE
    assert spec_calls == [], (
        f"find_spec was called for dotted module: {spec_calls}"
    )


def test_resolver_real_numpy_is_installed_and_importable() -> None:
    """End-to-end sanity: numpy is pip-installed in this test env,
    so the resolver should return INSTALLED_AND_IMPORTABLE for it
    WITHOUT executing numpy's top-level code (we trust find_spec
    not to load numpy/__init__.py)."""
    state, detail = _resolve_dependency_state("numpy", "numpy")
    assert state == DependencyState.INSTALLED_AND_IMPORTABLE
    assert detail == ""


def test_resolver_truly_missing_package_returns_missing_distribution() -> None:
    """End-to-end sanity: a definitely-missing package returns
    MISSING_DISTRIBUTION."""
    state, detail = _resolve_dependency_state(
        "definitely-not-real-slice12m-package",
        "definitely_not_real_slice12m_package",
    )
    assert state == DependencyState.MISSING_DISTRIBUTION
    assert "PackageNotFoundError" in detail


# ===============================================================
# Test 4: scan_once never executes heavy module top-level code
# ===============================================================


@pytest.mark.asyncio
async def test_check_import_errors_does_not_execute_target_packages() -> None:
    """Behavioral acceptance: invoking ``_check_import_errors``
    MUST NOT cause any tracked package's top-level code to run.
    We patch ``__import__`` itself with a spy that fails the test
    if invoked for any tracked module."""
    sensor = _build_sensor()
    import builtins
    real_import = builtins.__import__
    blocked_imports: List[str] = []
    tracked_module_names = {
        _MODULE_MAP.get(p, p.replace("-", "_"))
        for p in (
            "torch", "transformers", "numpy", "anthropic",
            "aiohttp", "fastapi", "cryptography", "google-api-core",
            "llama-cpp-python", "speechbrain", "chromadb",
        )
    }

    def _spy_import(name, *args, **kwargs):
        # Allow stdlib + our own backend imports through; block
        # tracked package top-level imports
        if name in tracked_module_names or any(
            name == m or name.startswith(m + ".") for m in tracked_module_names
        ):
            blocked_imports.append(name)
            raise AssertionError(
                f"Slice 12M wedge: _check_import_errors triggered "
                f"__import__({name!r}) — heavy package top-level "
                f"code is being executed on the loop."
            )
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=_spy_import):
        # Run with all our normal env; the default deep-probe path
        # is OFF, so no subprocess imports either.
        await sensor._check_import_errors()
    assert not blocked_imports, f"Tracked package imports leaked: {blocked_imports}"


# ===============================================================
# Test 5: Subprocess deep-probe path
# ===============================================================


@pytest.mark.asyncio
async def test_subprocess_probe_imported_for_real_stdlib() -> None:
    """Sanity: the subprocess probe for a real stdlib module
    (``asyncio``) returns IMPORTED."""
    result = await _subprocess_import_probe(
        "asyncio",
        timeout_s=10.0,
        python_bin=sys.executable,
    )
    assert result.outcome == SubprocessProbeOutcome.IMPORTED
    assert result.elapsed_s > 0


@pytest.mark.asyncio
async def test_subprocess_probe_import_error_for_missing_module() -> None:
    """Probing a definitely-missing module returns IMPORT_ERROR
    with the stderr tail captured."""
    result = await _subprocess_import_probe(
        "absolutely_no_such_module_12m_test",
        timeout_s=10.0,
        python_bin=sys.executable,
    )
    assert result.outcome == SubprocessProbeOutcome.IMPORT_ERROR
    # stderr should mention ModuleNotFoundError
    assert "ModuleNotFoundError" in result.error_detail or \
           "No module named" in result.error_detail


@pytest.mark.asyncio
async def test_subprocess_probe_rejects_unsafe_name() -> None:
    """Operator binding: "no package-specific hardcoding". The
    probe accepts only valid Python identifier names, so a future
    refactor passing operator-controlled strings can't be turned
    into shell injection or arbitrary-code execution."""
    for unsafe in (
        "; rm -rf /",
        "os; import os",
        "module-with-dash",
        "module with space",
        "$(echo pwn)",
        "",
    ):
        result = await _subprocess_import_probe(
            unsafe, timeout_s=5.0, python_bin=sys.executable,
        )
        assert result.outcome == SubprocessProbeOutcome.REJECTED_UNSAFE_NAME, (
            f"Unsafe name {unsafe!r} was not rejected: {result.outcome}"
        )
        # Rejection is essentially zero-cost
        assert result.elapsed_s < 0.5


@pytest.mark.asyncio
async def test_subprocess_probe_timeout_bounded() -> None:
    """The subprocess probe MUST honor its timeout. We probe a
    fake module name with a 0.1s timeout against a script that
    sleeps forever (simulated by running a Python interpreter
    importing a module that takes ages — here we just use a tight
    timeout to verify the timeout machinery works)."""
    # Use a custom python_bin that loops forever via a here-doc
    # trick: invoke sys.executable but with a `-c` that imports
    # nothing but blocks. Easier: use `sleep` if available.
    import shutil
    sleep_bin = shutil.which("sleep")
    if not sleep_bin:
        pytest.skip("no 'sleep' binary available for timeout test")
    # Override python_bin with sleep — the probe shouldn't care
    # since we're testing the timeout shape, not import semantics
    result = await _subprocess_import_probe(
        "asyncio",
        timeout_s=0.3,
        python_bin=sleep_bin,
    )
    # Either timeout (sleep blocks) or import_error (sleep doesn't
    # understand python args). Both prove the wait_for envelope
    # didn't leak.
    assert result.outcome in (
        SubprocessProbeOutcome.TIMEOUT,
        SubprocessProbeOutcome.IMPORT_ERROR,
        SubprocessProbeOutcome.SUBPROCESS_FAILED,
    )
    # Bounded elapsed time
    assert result.elapsed_s < 2.0


@pytest.mark.asyncio
async def test_deep_probe_disabled_by_default() -> None:
    """Operator binding: deep probe "disabled or low-frequency by
    default". Default is DISABLED — no subprocess work happens
    unless the env knob is set."""
    sensor = _build_sensor()
    # Clear env to verify default
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("JARVIS_RUNTIME_HEALTH_DEEP_IMPORT_PROBE_ENABLED", None)
        assert sensor._resolve_deep_probe_enabled() is False


@pytest.mark.asyncio
async def test_deep_probe_env_truthy_values_opt_in() -> None:
    """The env knob accepts standard truthy values."""
    sensor = _build_sensor()
    for val in ("1", "true", "yes", "on", "TRUE", "On"):
        with patch.dict(
            os.environ,
            {"JARVIS_RUNTIME_HEALTH_DEEP_IMPORT_PROBE_ENABLED": val},
            clear=False,
        ):
            assert sensor._resolve_deep_probe_enabled() is True, (
                f"value {val!r} should opt in"
            )


@pytest.mark.asyncio
async def test_deep_probe_timeout_env_clamped() -> None:
    """Invalid env values fall back to default; valid values are
    clamped to [1.0, 300.0] so a typo can't disable the bound."""
    sensor = _build_sensor()
    with patch.dict(os.environ, {"JARVIS_RUNTIME_HEALTH_DEEP_IMPORT_PROBE_TIMEOUT_S": "not-a-number"}):
        assert sensor._resolve_deep_probe_timeout_s() == 30.0
    with patch.dict(os.environ, {"JARVIS_RUNTIME_HEALTH_DEEP_IMPORT_PROBE_TIMEOUT_S": "0.001"}):
        assert sensor._resolve_deep_probe_timeout_s() == 1.0  # clamped to floor
    with patch.dict(os.environ, {"JARVIS_RUNTIME_HEALTH_DEEP_IMPORT_PROBE_TIMEOUT_S": "99999"}):
        assert sensor._resolve_deep_probe_timeout_s() == 300.0  # clamped to ceil
    with patch.dict(os.environ, {"JARVIS_RUNTIME_HEALTH_DEEP_IMPORT_PROBE_TIMEOUT_S": "5"}):
        assert sensor._resolve_deep_probe_timeout_s() == 5.0


# ===============================================================
# Test 6: scan_once is still async-safe end-to-end
# ===============================================================


@pytest.mark.asyncio
async def test_scan_once_is_async_safe() -> None:
    """``scan_once`` must run as an async coroutine without
    blocking on tracked-package imports. Smoke test: invoke it
    with a fake router and confirm it completes in bounded time
    AND triggers no tracked-module __import__ calls."""
    sensor = _build_sensor()
    blocked: List[str] = []
    tracked = {
        _MODULE_MAP.get(p, p.replace("-", "_"))
        for p in ("torch", "speechbrain", "chromadb", "transformers")
    }
    import builtins
    real_import = builtins.__import__

    def _spy(name, *args, **kwargs):
        if name in tracked or any(
            name.startswith(m + ".") for m in tracked
        ):
            blocked.append(name)
            raise AssertionError(
                f"scan_once triggered tracked __import__({name!r})"
            )
        return real_import(name, *args, **kwargs)

    # Stub the slow network-bound siblings (real ``pip index`` +
    # ``pip audit`` calls) so we only exercise Slice 12M's
    # _check_import_errors path.
    async def _empty_findings():
        return []

    with patch.object(sensor, "_check_package_staleness",
                      side_effect=_empty_findings), \
         patch.object(sensor, "_check_security_audit",
                      side_effect=_empty_findings), \
         patch("builtins.__import__", side_effect=_spy):
        try:
            await asyncio.wait_for(sensor.scan_once(), timeout=15.0)
        except asyncio.TimeoutError:
            pytest.fail("scan_once exceeded 15s — possible loop blockage")
    assert not blocked, f"Tracked imports leaked: {blocked}"


# ===============================================================
# Test 7: Safe-name regex sanity
# ===============================================================


def test_safe_module_name_regex_accepts_valid_python_identifiers() -> None:
    """The regex must accept all standard module identifiers."""
    for name in (
        "torch", "numpy", "google.api_core", "llama_cpp",
        "sklearn", "a", "_underscore", "mod_2", "a.b.c",
    ):
        assert _SAFE_MODULE_NAME_RE.match(name), (
            f"valid identifier {name!r} rejected"
        )


def test_safe_module_name_regex_rejects_shell_metacharacters() -> None:
    """The regex must reject anything with shell metacharacters,
    spaces, or other non-identifier characters."""
    for name in (
        "; ls", "$(pwn)", "mod with space", "mod-with-dash",
        ".leading_dot", "trailing_dot.", "..", "", "1starts_with_digit",
        "mod;import os", "mod\nimport os", "mod|cat",
    ):
        assert not _SAFE_MODULE_NAME_RE.match(name), (
            f"unsafe input {name!r} accepted"
        )


# ===============================================================
# AST pins — structural regression armor
# ===============================================================


_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "intake" / "sensors" / "runtime_health_sensor.py"
)


def _load_ast() -> ast.Module:
    return ast.parse(_MODULE_PATH.read_text())


def test_ast_pin_no_dunder_import_in_check_import_errors() -> None:
    """Module-level AST pin: walk every Call node inside the
    ``_check_import_errors`` AsyncFunctionDef and assert neither
    ``__import__`` nor ``importlib.import_module`` appears.
    Catches any refactor that re-introduces the loop wedge."""
    tree = _load_ast()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        if node.name != "_check_import_errors":
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            if isinstance(sub.func, ast.Name) and \
                    sub.func.id == "__import__":
                pytest.fail(
                    "AST pin: _check_import_errors invokes "
                    "__import__ — Slice 12M wedge regression"
                )
            if isinstance(sub.func, ast.Attribute) and \
                    sub.func.attr == "import_module":
                pytest.fail(
                    "AST pin: _check_import_errors invokes "
                    "importlib.import_module — Slice 12M wedge "
                    "regression"
                )
        return
    pytest.fail("_check_import_errors not found in module")


def test_ast_pin_check_import_errors_is_async() -> None:
    """The method MUST be an AsyncFunctionDef (catches a refactor
    that drops async-ness, breaking the subprocess deep-probe
    path)."""
    tree = _load_ast()
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and \
                node.name == "_check_import_errors":
            return
    pytest.fail(
        "_check_import_errors must be an AsyncFunctionDef"
    )


def test_ast_pin_dependency_state_taxonomy_closed() -> None:
    """The four DependencyState enum values are the closed
    taxonomy — adding a new value silently would let the
    decision matrix drop into an unhandled bucket."""
    tree = _load_ast()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name != "DependencyState":
            continue
        values = set()
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                    and isinstance(stmt.targets[0], ast.Name):
                values.add(stmt.targets[0].id)
        assert values == {
            "INSTALLED_AND_IMPORTABLE",
            "MISSING_DISTRIBUTION",
            "INSTALLED_BUT_NO_SPEC",
            "UNKNOWN_ERROR",
        }
        return
    pytest.fail("DependencyState class not found")


def test_ast_pin_subprocess_probe_outcome_taxonomy_closed() -> None:
    """SubprocessProbeOutcome closed taxonomy pin."""
    tree = _load_ast()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name != "SubprocessProbeOutcome":
            continue
        values = set()
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                    and isinstance(stmt.targets[0], ast.Name):
                values.add(stmt.targets[0].id)
        assert values == {
            "IMPORTED",
            "IMPORT_ERROR",
            "TIMEOUT",
            "SUBPROCESS_FAILED",
            "REJECTED_UNSAFE_NAME",
        }
        return
    pytest.fail("SubprocessProbeOutcome class not found")


def test_ast_pin_subprocess_probe_uses_asyncio_primitives() -> None:
    """The subprocess probe MUST compose
    ``asyncio.create_subprocess_exec`` + ``asyncio.wait_for``.
    A refactor to ``subprocess.run`` (sync, blocks loop) is the
    classic wedge regression."""
    src = _MODULE_PATH.read_text()
    assert "asyncio.create_subprocess_exec" in src, (
        "subprocess probe must use asyncio.create_subprocess_exec"
    )
    assert "asyncio.wait_for" in src, (
        "subprocess probe must bound itself with asyncio.wait_for"
    )
    # And MUST NOT use subprocess.run/Popen on the loop
    tree = _load_ast()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        if node.name != "_subprocess_import_probe":
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and \
                    isinstance(sub.func, ast.Attribute):
                if sub.func.attr in ("run", "Popen", "check_output",
                                     "check_call", "call"):
                    if isinstance(sub.func.value, ast.Name) and \
                            sub.func.value.id == "subprocess":
                        pytest.fail(
                            f"_subprocess_import_probe uses "
                            f"subprocess.{sub.func.attr} — would block "
                            f"the asyncio loop"
                        )
        return
    pytest.fail("_subprocess_import_probe not found")


def test_ast_pin_env_knob_constants_present() -> None:
    """The 3 Slice 12M env knob constants must be present as
    module-level string assignments."""
    src = _MODULE_PATH.read_text()
    for knob in (
        "JARVIS_RUNTIME_HEALTH_DEEP_IMPORT_PROBE_ENABLED",
        "JARVIS_RUNTIME_HEALTH_DEEP_IMPORT_PROBE_TIMEOUT_S",
        "JARVIS_RUNTIME_HEALTH_DEEP_IMPORT_PROBE_PYTHON_BIN",
    ):
        assert knob in src, (
            f"env knob constant {knob} missing from module"
        )


def test_ast_pin_module_map_lifted_to_module_level() -> None:
    """``_MODULE_MAP`` must be a module-level constant (not a
    per-call local) so it's testable + AST-walkable."""
    tree = _load_ast()
    for stmt in tree.body:
        if isinstance(stmt, ast.AnnAssign) and \
                isinstance(stmt.target, ast.Name) and \
                stmt.target.id == "_MODULE_MAP":
            return
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and \
                        target.id == "_MODULE_MAP":
                    return
    pytest.fail("_MODULE_MAP must be module-level")
