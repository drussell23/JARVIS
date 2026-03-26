"""
Multi-Language Test Adapters — JS/TS, Rust, Go support for TestRunner.

Closes the multi-language fluency gap. Each adapter implements the
LanguageAdapter protocol: resolve(changed_files) -> test_files,
run(test_files, sandbox_dir, timeout_s) -> AdapterResult.

Boundary Principle:
  Deterministic: File pattern matching, subprocess invocation (argv-based, no shell).
  Agentic: Test content generation (if needed) via the governance pipeline.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class AdapterResult:
    """Result from a language adapter test run."""
    passed: bool
    exit_code: int
    stdout: str
    stderr: str
    test_count: int
    failure_count: int
    duration_s: float
    adapter_name: str


class JavaScriptAdapter:
    """JS/TS test adapter (jest, vitest, node:test). Argv-based, no shell."""

    EXTENSIONS = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"})
    TEST_PATTERNS = frozenset({"test", "spec", "__tests__"})

    async def resolve(self, changed_files: List[str], repo_root: Path) -> Tuple[Path, ...]:
        test_files = []
        for f in changed_files:
            p = Path(f)
            if p.suffix not in self.EXTENSIONS:
                continue
            if any(pat in p.stem.lower() or pat in str(p.parent).lower() for pat in self.TEST_PATTERNS):
                full = repo_root / f
                if full.exists():
                    test_files.append(full)
                continue
            for c in [
                repo_root / p.parent / f"{p.stem}.test{p.suffix}",
                repo_root / p.parent / f"{p.stem}.spec{p.suffix}",
                repo_root / p.parent / "__tests__" / f"{p.stem}.test{p.suffix}",
            ]:
                if c.exists():
                    test_files.append(c)
        return tuple(test_files)

    async def run(self, test_files: Tuple[Path, ...], sandbox_dir: Path, timeout_s: float, op_id: str = "") -> AdapterResult:
        t0 = time.monotonic()
        runner = await self._detect_runner(sandbox_dir)
        argv = self._build_argv(runner, test_files)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=str(sandbox_dir),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            return AdapterResult(
                passed=(proc.returncode == 0), exit_code=proc.returncode or 0,
                stdout=stdout.decode(errors="replace")[-8000:], stderr=stderr.decode(errors="replace")[-4000:],
                test_count=len(test_files), failure_count=0 if proc.returncode == 0 else 1,
                duration_s=time.monotonic() - t0, adapter_name="javascript",
            )
        except asyncio.TimeoutError:
            return AdapterResult(passed=False, exit_code=-1, stdout="", stderr=f"TIMEOUT after {timeout_s}s",
                                 test_count=len(test_files), failure_count=len(test_files),
                                 duration_s=time.monotonic() - t0, adapter_name="javascript")

    async def _detect_runner(self, project_root: Path) -> str:
        pkg_json = project_root / "package.json"
        if pkg_json.exists():
            try:
                data = json.loads(pkg_json.read_text())
                all_deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
                if "vitest" in all_deps: return "vitest"
                if "jest" in all_deps: return "jest"
            except Exception: pass
        return "node_test"

    @staticmethod
    def _build_argv(runner: str, test_files: Tuple[Path, ...]) -> List[str]:
        files = [str(f) for f in test_files]
        if runner == "vitest": return ["npx", "vitest", "run", "--reporter=verbose"] + files
        if runner == "jest": return ["npx", "jest", "--verbose", "--no-cache"] + files
        return ["node", "--test"] + files


class RustAdapter:
    """Rust test adapter (cargo test). Argv-based, no shell."""

    async def resolve(self, changed_files: List[str], repo_root: Path) -> Tuple[Path, ...]:
        if any(f.endswith(".rs") for f in changed_files):
            if (repo_root / "Cargo.toml").exists():
                return (repo_root / "Cargo.toml",)
        return ()

    async def run(self, test_files: Tuple[Path, ...], sandbox_dir: Path, timeout_s: float, op_id: str = "") -> AdapterResult:
        t0 = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                "cargo", "test", "--", "--test-threads=1",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=str(sandbox_dir),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            stdout_str = stdout.decode(errors="replace")
            fail_match = re.search(r"(\d+) failed", stdout_str)
            return AdapterResult(
                passed=(proc.returncode == 0), exit_code=proc.returncode or 0,
                stdout=stdout_str[-8000:], stderr=stderr.decode(errors="replace")[-4000:],
                test_count=max(1, stdout_str.count("test result:")),
                failure_count=int(fail_match.group(1)) if fail_match else (1 if proc.returncode else 0),
                duration_s=time.monotonic() - t0, adapter_name="rust",
            )
        except asyncio.TimeoutError:
            return AdapterResult(passed=False, exit_code=-1, stdout="", stderr=f"TIMEOUT after {timeout_s}s",
                                 test_count=1, failure_count=1, duration_s=time.monotonic() - t0, adapter_name="rust")


class GoAdapter:
    """Go test adapter (go test). Argv-based, no shell."""

    async def resolve(self, changed_files: List[str], repo_root: Path) -> Tuple[Path, ...]:
        test_dirs: set[Path] = set()
        for f in changed_files:
            if not f.endswith(".go"): continue
            d = repo_root / Path(f).parent
            if d.exists() and any(tf.name.endswith("_test.go") for tf in d.iterdir() if tf.is_file()):
                test_dirs.add(d)
        return tuple(test_dirs)

    async def run(self, test_files: Tuple[Path, ...], sandbox_dir: Path, timeout_s: float, op_id: str = "") -> AdapterResult:
        t0 = time.monotonic()
        packages = ["./..."] if not test_files else [f"./{p.relative_to(sandbox_dir)}/..." for p in test_files]
        try:
            proc = await asyncio.create_subprocess_exec(
                "go", "test", "-v", "-count=1", *packages,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=str(sandbox_dir),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            stdout_str = stdout.decode(errors="replace")
            return AdapterResult(
                passed=(proc.returncode == 0), exit_code=proc.returncode or 0,
                stdout=stdout_str[-8000:], stderr=stderr.decode(errors="replace")[-4000:],
                test_count=max(1, stdout_str.count("--- PASS:") + stdout_str.count("--- FAIL:")),
                failure_count=stdout_str.count("--- FAIL:"),
                duration_s=time.monotonic() - t0, adapter_name="go",
            )
        except asyncio.TimeoutError:
            return AdapterResult(passed=False, exit_code=-1, stdout="", stderr=f"TIMEOUT after {timeout_s}s",
                                 test_count=1, failure_count=1, duration_s=time.monotonic() - t0, adapter_name="go")
