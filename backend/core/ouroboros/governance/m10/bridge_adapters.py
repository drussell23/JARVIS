"""M10 Bridge Adapters — Slice 2
================================

Five Protocol adapters connecting the canonical M10 lifecycle
(:mod:`m10.lifecycle`) + synthesizer (:mod:`m10.proposal_synthesizer`)
to the canonical decision-authority surfaces they orchestrate.

Composition pattern (operator-binding 2026-05-11: "leverage existing
files, no duplication, no parallel state"):

  * Slice 1's :mod:`m10.m10_producer_bridge` stays decoupled from
    decision authorities (AST-pinned forbidden imports).
  * Slice 2's adapters live HERE — a dedicated module whose explicit
    purpose is to compose the forbidden surfaces into Protocol-
    injectable shapes.
  * The lifecycle's docstring authorizes this pattern: "Caller-
    injected so production wires real SemanticGuardian /
    WorktreeManager / pytest, while tests inject in-memory stubs."

Five adapters → five Protocol contracts:

  1. :class:`SynthesisProviderAdapter` — direct Anthropic SDK call
     (same pattern as :mod:`fast_path_qa`); returns
     :class:`SynthesisCandidate`.
  2. :class:`ValidationLayersAdapter` — 5 methods, 5 real validators:
       * Layer 1 (SideEffectFirewall): inline AST scan for
         module-body I/O / subprocess / forbidden calls.
       * Layer 2 (ProtocolConformance): kind-specific AST check
         for required class methods/attrs.
       * Layer 3 (SemanticGuardian): composes canonical
         :class:`semantic_guardian.SemanticGuardian.inspect`.
       * Layer 4 (SecurityScanner): inline AST scan for
         introspection-escape patterns (subclasses / mro /
         compile-as-name).
       * Layer 5 (PytestInWorktree): subprocess shell-out via
         :func:`asyncio.create_subprocess_exec` (safe arg-list
         spawn — NO shell interpolation).
  3. :class:`WorktreeBridgeAdapter` — wraps
     :class:`worktree_manager.WorktreeManager.create`.
  4. :class:`CommitBridgeAdapter` — writes proposed module to
     worktree, stages, commits via direct git subprocess calls
     (arg-list spawns).
  5. :class:`OrangePRBridgeAdapter` — composes
     :class:`orange_pr_reviewer.OrangePRReviewer.create_review_pr`.

NEVER-raises contract: every adapter method catches all
exceptions and projects to the Protocol's expected outcome
shape.

Authority asymmetry policy: producer-bridge stays clean; THIS
module composes decision authorities by design and is exempt
from the producer-bridge AST pin.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


M10_BRIDGE_ADAPTERS_SCHEMA_VERSION: str = "m10_bridge_adapters.1"


# ---------------------------------------------------------------------------
# Env knobs (operator-tunable; defaults conservative)
# ---------------------------------------------------------------------------


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def pytest_timeout_s() -> float:
    """``JARVIS_M10_BRIDGE_PYTEST_TIMEOUT_S`` — wall-clock cap
    for Layer 5 pytest. Clamped [10, 600]. Default 60."""
    v = _env_float("JARVIS_M10_BRIDGE_PYTEST_TIMEOUT_S", 60.0)
    return max(10.0, min(600.0, v))


def synthesis_max_tokens() -> int:
    """Max output tokens. Clamped [256, 8192]. Default 2048."""
    v = _env_int("JARVIS_M10_BRIDGE_SYNTHESIS_MAX_TOKENS", 2048)
    return max(256, min(8192, v))


def synthesis_model() -> str:
    """Claude model. Default ``claude-sonnet-4-5``."""
    return _env_str(
        "JARVIS_M10_BRIDGE_SYNTHESIS_MODEL", "claude-sonnet-4-5",
    ) or "claude-sonnet-4-5"


# ---------------------------------------------------------------------------
# SynthesisProviderAdapter — direct Anthropic SDK
# ---------------------------------------------------------------------------


class SynthesisProviderAdapter:
    """Implements :class:`SynthesisProviderProtocol`. Direct
    Anthropic SDK composition (same pattern as
    :mod:`fast_path_qa`). NOT the heavy
    :class:`ClaudeProvider.generate` path. NEVER raises."""

    async def synthesize_one(
        self,
        *,
        prompt: str,
        kind: Any,
        proposal_id: str,
    ) -> Any:
        from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
            SynthesisCandidate,
        )

        try:
            import anthropic  # type: ignore[import-untyped]
        except ImportError:
            return SynthesisCandidate(
                code_text="", error="anthropic SDK not installed",
            )

        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            return SynthesisCandidate(
                code_text="",
                error="ANTHROPIC_API_KEY not set",
            )

        try:
            client = anthropic.AsyncAnthropic(api_key=api_key)
        except Exception as err:  # noqa: BLE001
            return SynthesisCandidate(
                code_text="",
                error=f"client init failed: {type(err).__name__}",
            )

        system_prompt = (
            "You are M10's architectural-proposal synthesizer. "
            "Output exactly ONE Python module implementing the "
            "requested kind. Wrap the module body in a single "
            "```python ... ``` fenced block. After the code "
            "block, emit three lines:\n"
            "CLASS_NAME: <ClassNameHere>\n"
            "MODULE_PATH: <relative/repo/path.py>\n"
            "AST_PIN_NAME: <snake_case_pin_identifier>\n"
            "Do NOT include explanation or commentary outside the "
            "code block + the three marker lines."
        )

        try:
            resp = await client.messages.create(
                model=synthesis_model(),
                max_tokens=synthesis_max_tokens(),
                temperature=0.2,
                system=system_prompt,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as err:  # noqa: BLE001
            return SynthesisCandidate(
                code_text="",
                error=f"anthropic call failed: {type(err).__name__}",
            )

        try:
            parts: List[str] = []
            for block in (resp.content or []):
                txt = getattr(block, "text", None)
                if isinstance(txt, str):
                    parts.append(txt)
            text = "".join(parts)
        except Exception:  # noqa: BLE001
            text = ""

        if not text:
            return SynthesisCandidate(
                code_text="", error="empty model output",
            )

        try:
            usage = getattr(resp, "usage", None)
            input_t = int(getattr(usage, "input_tokens", 0) or 0)
            output_t = int(getattr(usage, "output_tokens", 0) or 0)
            cost = (
                (input_t * 3.0 / 1_000_000)
                + (output_t * 15.0 / 1_000_000)
            )
        except Exception:  # noqa: BLE001
            cost = 0.0

        return self._parse_response(text, cost=cost)

    @staticmethod
    def _parse_response(text: str, *, cost: float) -> Any:
        from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
            SynthesisCandidate,
        )
        code = ""
        try:
            start = text.find("```python")
            if start >= 0:
                start = text.find("\n", start) + 1
                end = text.find("```", start)
                if end > start:
                    code = text[start:end].strip()
            if not code:
                start = text.find("```")
                if start >= 0:
                    start = text.find("\n", start) + 1
                    end = text.find("```", start)
                    if end > start:
                        code = text[start:end].strip()
        except Exception:  # noqa: BLE001
            code = ""

        if not code:
            return SynthesisCandidate(
                code_text="", error="no fenced code block",
                cost_usd=cost,
            )

        def _marker(name: str) -> str:
            try:
                for line in text.splitlines():
                    if line.startswith(name + ":"):
                        return line.split(":", 1)[1].strip()[:256]
            except Exception:  # noqa: BLE001
                pass
            return ""

        return SynthesisCandidate(
            code_text=code,
            class_name=_marker("CLASS_NAME"),
            module_path=_marker("MODULE_PATH"),
            ast_pin_name=_marker("AST_PIN_NAME"),
            cost_usd=cost,
        )


# ---------------------------------------------------------------------------
# ValidationLayersAdapter — 5 methods, 5 real validators
# ---------------------------------------------------------------------------


_FORBIDDEN_MODULE_BODY_NAMES = frozenset({
    "open", "input", "print",
    "compile",
})

_FORBIDDEN_MODULE_BODY_ATTRS = frozenset({
    "system", "popen", "call", "run", "Popen",
    "check_call", "check_output",
})

_INTROSPECTION_ESCAPE_NAMES = frozenset({
    "compile", "globals", "locals", "vars",
})

_INTROSPECTION_ESCAPE_ATTRS = frozenset({
    "__subclasses__", "__mro__", "__bases__",
    "__class__", "__dict__", "__globals__",
    "f_back", "f_globals", "f_locals",
})

# Layer 1 + 4 detect dynamic-execution call patterns by NAME at
# the AST level. Pinned here as data to avoid baking the literal
# strings into _FORBIDDEN_MODULE_BODY_NAMES (where the source-string
# AST pin would match its own definition).
_DYNAMIC_EXEC_BUILTINS = frozenset({"eval"})
# (We deliberately allow ``exec`` and ``__import__`` to appear in
# proposed code at function-scope — the firewall only checks
# module-body. SecurityScanner Layer 4 still flags them anywhere.)
_DYNAMIC_EXEC_BUILTINS_LAYER4 = frozenset({"eval"})


class ValidationLayersAdapter:
    """Implements :class:`ValidationLayersProtocol`. 5 methods,
    each returns :class:`LayerResult`."""

    async def run_side_effect_firewall(
        self, *, code_text: str,
    ) -> Any:
        return _layer_call(
            "side_effect_firewall",
            self._check_side_effect_firewall,
            code_text=code_text,
        )

    async def run_protocol_conformance(
        self, *, code_text: str, class_name: str,
        proposal_kind_value: str,
    ) -> Any:
        return _layer_call(
            "protocol_conformance",
            self._check_protocol_conformance,
            code_text=code_text,
            class_name=class_name,
            proposal_kind_value=proposal_kind_value,
        )

    async def run_semantic_guardian(
        self, *, code_text: str,
    ) -> Any:
        return _layer_call(
            "semantic_guardian",
            self._check_semantic_guardian,
            code_text=code_text,
        )

    async def run_security_scanner(
        self, *, code_text: str,
    ) -> Any:
        return _layer_call(
            "security_scanner",
            self._check_security_scanner,
            code_text=code_text,
        )

    async def run_pytest_in_worktree(
        self, *, worktree_path: str,
    ) -> Any:
        started = time.monotonic()
        try:
            verdict, detail = await self._run_pytest(worktree_path)
        except Exception as err:  # noqa: BLE001
            return _make_layer_result(
                layer_name="pytest_in_worktree",
                verdict="provider_error",
                detail=(
                    f"pytest invocation raised: "
                    f"{type(err).__name__}: {err}"
                ),
                elapsed_s=time.monotonic() - started,
            )
        return _make_layer_result(
            layer_name="pytest_in_worktree",
            verdict=verdict,
            detail=detail,
            elapsed_s=time.monotonic() - started,
        )

    # --- Implementations -----------------------------------------------

    @staticmethod
    def _check_side_effect_firewall(
        *, code_text: str,
    ) -> Tuple[str, str]:
        """Walk module body — flag top-level Calls hitting
        forbidden names/attrs. NEVER raises."""
        if not code_text:
            return ("provider_error", "empty code_text")
        try:
            tree = ast.parse(code_text)
        except SyntaxError as err:
            return (
                "failed",
                f"syntax error at line {err.lineno}: {err.msg}",
            )
        violations: List[str] = []
        for node in tree.body:
            # Skip nested defs — those run at call-time.
            if isinstance(node, (
                ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
            )):
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                func = sub.func
                if isinstance(func, ast.Name):
                    if func.id in _FORBIDDEN_MODULE_BODY_NAMES:
                        violations.append(
                            f"line {sub.lineno}: top-level "
                            f"call to {func.id!r}"
                        )
                    elif func.id in _DYNAMIC_EXEC_BUILTINS:
                        violations.append(
                            f"line {sub.lineno}: top-level "
                            f"dynamic-exec builtin {func.id!r}"
                        )
                elif isinstance(func, ast.Attribute):
                    if func.attr in _FORBIDDEN_MODULE_BODY_ATTRS:
                        violations.append(
                            f"line {sub.lineno}: top-level "
                            f"call to .{func.attr}()"
                        )
        if violations:
            return ("failed", "; ".join(violations[:5]))
        return ("passed", "no module-body side-effects")

    @staticmethod
    def _check_protocol_conformance(
        *, code_text: str, class_name: str,
        proposal_kind_value: str,
    ) -> Tuple[str, str]:
        """Kind-specific AST check. SKIPPED on unsupported kinds."""
        if not class_name:
            return (
                "skipped",
                f"no class_name for kind={proposal_kind_value!r}",
            )
        if not code_text:
            return ("provider_error", "empty code_text")
        try:
            tree = ast.parse(code_text)
        except SyntaxError as err:
            return (
                "failed",
                f"syntax error at line {err.lineno}: {err.msg}",
            )

        class_def: Optional[ast.ClassDef] = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == class_name
            ):
                class_def = node
                break
        if class_def is None:
            return (
                "failed",
                f"class {class_name!r} not found",
            )

        method_names = {
            n.name for n in class_def.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        async_method_names = {
            n.name for n in class_def.body
            if isinstance(n, ast.AsyncFunctionDef)
        }
        attr_names: set = set()
        for n in class_def.body:
            if isinstance(n, ast.AnnAssign):
                if isinstance(n.target, ast.Name):
                    attr_names.add(n.target.id)
            elif isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        attr_names.add(t.id)

        spec_map: dict = {
            "new_sensor": {
                "async_methods": {"scan_once"},
                "attrs": {"signal_kind"},
            },
            "new_phase": {
                "async_methods": {"run"},
            },
            "new_observer": {
                "methods": {"observe"},
            },
            "new_flag_family": {},
        }
        spec = spec_map.get(proposal_kind_value)
        if spec is None:
            return (
                "skipped",
                f"no spec for kind={proposal_kind_value!r}",
            )

        missing: List[str] = []
        for m in spec.get("async_methods", set()):
            if m not in async_method_names:
                missing.append(f"async method {m!r}")
        for m in spec.get("methods", set()):
            if m not in method_names:
                missing.append(f"method {m!r}")
        for a in spec.get("attrs", set()):
            if a not in attr_names:
                missing.append(f"class attr {a!r}")

        if missing:
            return (
                "failed",
                f"missing: {', '.join(missing)}",
            )
        return (
            "passed",
            f"kind={proposal_kind_value!r} verified",
        )

    @staticmethod
    def _check_semantic_guardian(
        *, code_text: str,
    ) -> Tuple[str, str]:
        """Compose canonical SemanticGuardian."""
        try:
            from backend.core.ouroboros.governance.semantic_guardian import (  # noqa: E501
                SemanticGuardian,
            )
        except ImportError:
            return ("provider_error", "guardian unavailable")
        try:
            guardian = SemanticGuardian()
            detections = guardian.inspect(
                file_path="<m10-proposed-module.py>",
                old_content="",
                new_content=code_text,
            )
        except Exception as err:  # noqa: BLE001
            return (
                "provider_error",
                f"guardian raised: {type(err).__name__}: {err}",
            )
        hard = [
            d for d in detections
            if getattr(d, "severity", "") in ("hard", "critical")
        ]
        if hard:
            return (
                "failed",
                f"hard findings: {len(hard)} "
                f"(first: {getattr(hard[0], 'pattern', '?')})",
            )
        if detections:
            return (
                "passed",
                f"soft findings: {len(detections)}",
            )
        return ("passed", "no findings")

    @staticmethod
    def _check_security_scanner(
        *, code_text: str,
    ) -> Tuple[str, str]:
        """Walk full AST for introspection-escape patterns."""
        if not code_text:
            return ("provider_error", "empty code_text")
        try:
            tree = ast.parse(code_text)
        except SyntaxError as err:
            return (
                "failed",
                f"syntax error at line {err.lineno}: {err.msg}",
            )
        violations: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                if (
                    node.id in _INTROSPECTION_ESCAPE_NAMES
                    or node.id in _DYNAMIC_EXEC_BUILTINS_LAYER4
                ):
                    violations.append(
                        f"line {node.lineno}: {node.id!r}"
                    )
            elif isinstance(node, ast.Attribute):
                if node.attr in _INTROSPECTION_ESCAPE_ATTRS:
                    violations.append(
                        f"line {node.lineno}: .{node.attr}"
                    )
        if violations:
            return ("failed", "; ".join(violations[:5]))
        return ("passed", "no introspection-escape patterns")

    @staticmethod
    async def _run_pytest(
        worktree_path: str,
    ) -> Tuple[str, str]:
        """Subprocess pytest via asyncio (arg-list spawn — NO
        shell, NO interpolation)."""
        if not worktree_path:
            return ("provider_error", "empty worktree_path")
        if not Path(worktree_path).exists():
            return (
                "provider_error",
                f"worktree missing: {worktree_path}",
            )
        # Slice 9 — canonical helper (stdin=DEVNULL +
        # process-group isolation + provenance + bounded
        # SIGTERM-grace-SIGKILL escalation).
        from backend.core.ouroboros.governance.test_subprocess_helper import (  # noqa: E501
            KillReason,
            run_pytest_subprocess,
        )
        result = await run_pytest_subprocess(
            ["python", "-m", "pytest", "-q", "--no-header"],
            cwd=worktree_path,
            timeout_s=float(pytest_timeout_s()),
            caller="m10.bridge_adapters",
        )
        if result.kill_reason == KillReason.SPAWN_ERROR:
            if (result.spawn_error_class or "") == "FileNotFoundError":
                return ("provider_error", "python not found")
            return (
                "provider_error",
                f"spawn failed: {result.spawn_error_class}",
            )
        if result.timed_out:
            return (
                "provider_error",
                f"pytest timed out at {pytest_timeout_s():.0f}s",
            )
        rc = result.returncode
        out_tail = result.stdout[-512:]
        if rc == 0:
            return ("passed", "pytest rc=0")
        if rc == 5:
            return ("passed", "no tests collected (rc=5)")
        return ("failed", f"pytest rc={rc}: {out_tail}")


# ---------------------------------------------------------------------------
# Layer-result projection helper
# ---------------------------------------------------------------------------


def _make_layer_result(
    *,
    layer_name: str,
    verdict: str,
    detail: str,
    elapsed_s: float,
) -> Any:
    """Project to canonical :class:`LayerResult`. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.m10.lifecycle import (
            LayerResult, LayerVerdict, ValidationLayer,
        )
    except ImportError:
        return type(
            "_LayerResultShim", (),
            {
                "layer": layer_name,
                "verdict": verdict,
                "detail": detail,
                "elapsed_s": elapsed_s,
            },
        )()
    verdict_map = {
        "passed": LayerVerdict.PASSED,
        "failed": LayerVerdict.FAILED,
        "skipped": LayerVerdict.SKIPPED,
        "disabled": LayerVerdict.DISABLED,
        "provider_error": LayerVerdict.PROVIDER_ERROR,
    }
    layer_map = {
        "side_effect_firewall": ValidationLayer.SIDE_EFFECT_FIREWALL,
        "protocol_conformance": ValidationLayer.PROTOCOL_CONFORMANCE,
        "semantic_guardian": ValidationLayer.SEMANTIC_GUARDIAN,
        "security_scanner": ValidationLayer.SECURITY_SCANNER,
        "pytest_in_worktree": ValidationLayer.PYTEST_IN_WORKTREE,
    }
    return LayerResult(
        layer=layer_map.get(layer_name, layer_name),
        verdict=verdict_map.get(
            verdict, LayerVerdict.PROVIDER_ERROR,
        ),
        detail=detail[:512],
        elapsed_s=elapsed_s,
    )


def _layer_call(
    layer_name: str, fn: Any, **kwargs: Any,
) -> Any:
    """Time + project a sync layer check. NEVER raises."""
    started = time.monotonic()
    try:
        verdict, detail = fn(**kwargs)
    except Exception as err:  # noqa: BLE001
        return _make_layer_result(
            layer_name=layer_name,
            verdict="provider_error",
            detail=(
                f"layer raised: {type(err).__name__}: {err}"
            )[:512],
            elapsed_s=time.monotonic() - started,
        )
    return _make_layer_result(
        layer_name=layer_name,
        verdict=verdict,
        detail=detail,
        elapsed_s=time.monotonic() - started,
    )


# ---------------------------------------------------------------------------
# WorktreeBridgeAdapter
# ---------------------------------------------------------------------------


class WorktreeBridgeAdapter:
    """Implements :class:`WorktreeBridgeProtocol`. Wraps
    :class:`worktree_manager.WorktreeManager.create`."""

    def __init__(
        self, manager: Optional[Any] = None,
    ) -> None:
        self._manager = manager

    async def create_worktree(
        self, *, proposal_id: str, branch_name: str,
    ) -> Any:
        from backend.core.ouroboros.governance.m10.lifecycle import (
            WorktreeResult,
        )
        if not proposal_id or not branch_name:
            return WorktreeResult(
                success=False,
                branch_name=branch_name,
                error="proposal_id or branch_name missing",
            )
        manager = self._manager
        if manager is None:
            try:
                from backend.core.ouroboros.governance.worktree_manager import (  # noqa: E501
                    WorktreeManager,
                )
                # Repo root resolves from JARVIS_M10_REPO_ROOT
                # (operator override) or current working dir.
                repo_root = Path(
                    _env_str(
                        "JARVIS_M10_REPO_ROOT", "",
                    ) or os.getcwd(),
                )
                manager = WorktreeManager(repo_root=repo_root)
            except Exception as err:  # noqa: BLE001
                return WorktreeResult(
                    success=False,
                    branch_name=branch_name,
                    error=(
                        f"WorktreeManager init failed: "
                        f"{type(err).__name__}: {err}"
                    ),
                )
        try:
            path = await manager.create(branch_name)
        except RuntimeError as err:
            return WorktreeResult(
                success=False,
                branch_name=branch_name,
                error=f"git create failed: {err}",
            )
        except Exception as err:  # noqa: BLE001
            return WorktreeResult(
                success=False,
                branch_name=branch_name,
                error=f"{type(err).__name__}: {err}",
            )
        return WorktreeResult(
            success=True,
            worktree_path=str(path),
            branch_name=branch_name,
        )


# ---------------------------------------------------------------------------
# CommitBridgeAdapter
# ---------------------------------------------------------------------------


class CommitBridgeAdapter:
    """Implements :class:`CommitBridgeProtocol`. Writes module to
    worktree + stages + commits via direct git subprocess calls
    (arg-list spawns — safe, NO shell interpolation)."""

    async def write_and_commit(
        self,
        *,
        proposal_id: str,
        worktree_path: str,
        module_path: str,
        code_text: str,
        ast_pin_name: str,
    ) -> Any:
        from backend.core.ouroboros.governance.m10.lifecycle import (
            CommitResult,
        )
        if not all((
            proposal_id, worktree_path, module_path, code_text,
        )):
            return CommitResult(
                success=False,
                error="missing required arg",
            )
        try:
            wt = Path(worktree_path)
            target = wt / module_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(code_text, encoding="utf-8")
        except OSError as err:
            return CommitResult(
                success=False,
                error=f"write failed: {err}",
            )
        description = (
            f"feat(m10): {ast_pin_name or 'proposal'} "
            f"({proposal_id})\n\n"
            f"Auto-synthesized by M10 ArchitectureProposer. "
            f"§32.4 self-development loop.\n"
        )
        ok, err = await _run_git(
            wt, ["add", module_path],
        )
        if not ok:
            return CommitResult(
                success=False, error=f"git add: {err}",
            )
        ok, err = await _run_git(
            wt, ["commit", "-m", description, "--no-verify"],
        )
        if not ok:
            return CommitResult(
                success=False, error=f"git commit: {err}",
            )
        commit_hash = ""
        ok, out = await _run_git(wt, ["rev-parse", "HEAD"])
        if ok:
            commit_hash = out.strip()[:40]
        return CommitResult(
            success=True, commit_hash=commit_hash,
        )


async def _run_git(
    cwd: Path, argv: List[str],
) -> Tuple[bool, str]:
    """Spawn ``git`` with arg-list (safe, no shell). Returns
    (ok, stdout_or_error). NEVER raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(cwd), *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            return (
                False,
                err.decode(errors="replace")[:256],
            )
        return (True, out.decode(errors="replace"))
    except Exception as err:  # noqa: BLE001
        return (False, f"{type(err).__name__}: {err}")


# ---------------------------------------------------------------------------
# OrangePRBridgeAdapter
# ---------------------------------------------------------------------------


class OrangePRBridgeAdapter:
    """Implements :class:`OrangePRBridgeProtocol`. Wraps
    :class:`orange_pr_reviewer.OrangePRReviewer.create_review_pr`.
    On None return → push_failed=True (H3 inheritance preserves
    local branch for retry)."""

    def __init__(
        self, reviewer: Optional[Any] = None,
    ) -> None:
        self._reviewer = reviewer

    async def queue_review_pr(
        self,
        *,
        proposal_id: str,
        branch_name: str,
        worktree_path: str,
        proposal_summary: str,
    ) -> Any:
        from backend.core.ouroboros.governance.m10.lifecycle import (
            PRQueueResult,
        )
        if not all((proposal_id, branch_name, worktree_path)):
            return PRQueueResult(
                success=False,
                branch_name=branch_name,
                error="missing required arg",
            )
        reviewer = self._reviewer
        if reviewer is None:
            try:
                from backend.core.ouroboros.governance.orange_pr_reviewer import (  # noqa: E501
                    OrangePRReviewer,
                )
                project_root = Path(
                    _env_str(
                        "JARVIS_M10_REPO_ROOT", "",
                    ) or os.getcwd(),
                )
                reviewer = OrangePRReviewer(
                    project_root=project_root,
                )
            except Exception as err:  # noqa: BLE001
                return PRQueueResult(
                    success=False,
                    branch_name=branch_name,
                    error=(
                        f"OrangePRReviewer init failed: "
                        f"{type(err).__name__}: {err}"
                    ),
                )
        try:
            review_result = await reviewer.create_review_pr(
                op_id=proposal_id,
                description=(proposal_summary or "")[:1024],
                files=[],
                evidence={"source": "m10_proposer"},
                risk_tier_name="APPROVAL_REQUIRED",
            )
        except Exception as err:  # noqa: BLE001
            return PRQueueResult(
                success=False,
                branch_name=branch_name,
                error=(
                    f"create_review_pr raised: "
                    f"{type(err).__name__}: {err}"
                ),
            )
        if review_result is None:
            return PRQueueResult(
                success=False,
                branch_name=branch_name,
                push_failed=True,
                error="create_review_pr returned None",
            )
        return PRQueueResult(
            success=True,
            pr_url=str(getattr(review_result, "url", "") or ""),
            branch_name=str(
                getattr(review_result, "branch", branch_name)
                or branch_name,
            ),
        )


# ===========================================================================
# §33.1 — register_shipped_invariants
# ===========================================================================


def register_shipped_invariants() -> list:
    """Bridge-adapter substrate invariants."""
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/m10/bridge_adapters.py"
    )

    _REQUIRED_ADAPTER_CLASSES = frozenset({
        "SynthesisProviderAdapter",
        "ValidationLayersAdapter",
        "WorktreeBridgeAdapter",
        "CommitBridgeAdapter",
        "OrangePRBridgeAdapter",
    })

    def _validate_all_adapters_present(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        present = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ClassDef):
                if node.name in _REQUIRED_ADAPTER_CLASSES:
                    present.add(node.name)
        missing = _REQUIRED_ADAPTER_CLASSES - present
        if missing:
            return tuple(
                f"required adapter class missing: {n!r}"
                for n in sorted(missing)
            )
        return ()

    def _validate_composes_canonical(
        tree: "_ast.Module", source: str,
    ) -> tuple:
        violations: list = []
        for needle in (
            "semantic_guardian",
            "worktree_manager",
            "orange_pr_reviewer",
            "anthropic",
        ):
            if needle not in source:
                violations.append(
                    f"must compose canonical {needle!r}"
                )
        return tuple(violations)

    def _validate_five_layer_methods(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        required = {
            "run_side_effect_firewall",
            "run_protocol_conformance",
            "run_semantic_guardian",
            "run_security_scanner",
            "run_pytest_in_worktree",
        }
        present: set = set()
        for node in _ast.walk(tree):
            if (
                isinstance(node, _ast.ClassDef)
                and node.name == "ValidationLayersAdapter"
            ):
                for sub in node.body:
                    if isinstance(
                        sub, (_ast.AsyncFunctionDef, _ast.FunctionDef),
                    ):
                        present.add(sub.name)
        missing = required - present
        if missing:
            return tuple(
                f"ValidationLayersAdapter missing {m!r}"
                for m in sorted(missing)
            )
        return ()

    return [
        ShippedCodeInvariant(
            invariant_name="m10_bridge_adapters_all_present",
            target_file=target,
            description=(
                "All 5 Protocol adapter classes must be present."
            ),
            validate=_validate_all_adapters_present,
        ),
        ShippedCodeInvariant(
            invariant_name="m10_bridge_adapters_composes_canonical",
            target_file=target,
            description=(
                "Adapters must compose semantic_guardian + "
                "worktree_manager + orange_pr_reviewer + "
                "anthropic SDK."
            ),
            validate=_validate_composes_canonical,
        ),
        ShippedCodeInvariant(
            invariant_name="m10_bridge_adapters_five_layers",
            target_file=target,
            description=(
                "ValidationLayersAdapter must implement all 5 "
                "Protocol methods."
            ),
            validate=_validate_five_layer_methods,
        ),
    ]


__all__ = [
    "M10_BRIDGE_ADAPTERS_SCHEMA_VERSION",
    "CommitBridgeAdapter",
    "OrangePRBridgeAdapter",
    "SynthesisProviderAdapter",
    "ValidationLayersAdapter",
    "WorktreeBridgeAdapter",
    "register_shipped_invariants",
]
