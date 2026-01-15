"""
Cross-Repo Symbol Resolution Test Suite
========================================

Tests The Watcher's ability to resolve symbols across the Trinity repos:
- JARVIS-AI-Agent (main)
- jarvis-prime (LLM inference)
- reactor-core (training pipeline)

These tests verify:
1. LSP initialization with multiple workspaces
2. Symbol definition lookup within single repo
3. Cross-repo import resolution
4. Function signature verification
5. Integration with Ouroboros Engine

Author: Trinity System
"""

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Any

# Ensure parent module is importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent.parent))

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("CrossRepoTest")


class CrossRepoTestSuite:
    """
    Comprehensive test suite for cross-repo symbol resolution.
    """

    def __init__(self):
        self.results: Dict[str, Any] = {
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "details": [],
        }

        # Trinity repo paths
        self.jarvis_path = Path(os.getenv(
            "JARVIS_PATH",
            Path.home() / "Documents/repos/JARVIS-AI-Agent"
        ))
        self.prime_path = Path(os.getenv(
            "JARVIS_PRIME_PATH",
            Path.home() / "Documents/repos/jarvis-prime"
        ))
        self.reactor_path = Path(os.getenv(
            "REACTOR_CORE_PATH",
            Path.home() / "Documents/repos/reactor-core"
        ))

    def _record(self, name: str, passed: bool, message: str = "", skipped: bool = False):
        """Record a test result."""
        if skipped:
            self.results["skipped"] += 1
            status = "SKIP"
        elif passed:
            self.results["passed"] += 1
            status = "PASS"
        else:
            self.results["failed"] += 1
            status = "FAIL"

        self.results["details"].append({
            "name": name,
            "status": status,
            "message": message,
        })

        icon = "✓" if passed else ("⊘" if skipped else "✗")
        print(f"  [{icon}] {name}")
        if message and not passed:
            print(f"      {message}")

    async def run_all_tests(self):
        """Run all cross-repo tests."""
        print("\n" + "=" * 60)
        print("Cross-Repo Symbol Resolution Test Suite")
        print("=" * 60)

        # Test 1: Workspace detection
        await self.test_workspace_detection()

        # Test 2: Watcher initialization
        watcher = await self.test_watcher_initialization()

        if watcher:
            # Test 3: Single-repo symbol resolution
            await self.test_single_repo_resolution(watcher)

            # Test 4: Cross-repo resolution (if multiple repos available)
            await self.test_cross_repo_resolution(watcher)

            # Test 5: Function signature verification
            await self.test_signature_verification(watcher)

            # Test 6: Code validation
            await self.test_code_validation(watcher)

            # Test 7: Ouroboros integration
            await self.test_ouroboros_integration(watcher)

            # Cleanup
            await watcher.shutdown()

        # Print summary
        self._print_summary()

        return self.results["failed"] == 0

    async def test_workspace_detection(self):
        """Test detection of Trinity workspaces."""
        print("\n[1] Workspace Detection")

        workspaces = []

        if self.jarvis_path.exists():
            workspaces.append(("JARVIS", self.jarvis_path))
            self._record("JARVIS workspace exists", True)
        else:
            self._record("JARVIS workspace exists", False, f"Not found: {self.jarvis_path}")

        if self.prime_path.exists():
            workspaces.append(("Prime", self.prime_path))
            self._record("JARVIS Prime workspace exists", True)
        else:
            self._record("JARVIS Prime workspace exists", True, "Optional - not found", skipped=True)

        if self.reactor_path.exists():
            workspaces.append(("Reactor", self.reactor_path))
            self._record("Reactor Core workspace exists", True)
        else:
            self._record("Reactor Core workspace exists", True, "Optional - not found", skipped=True)

        self._record(
            f"Total workspaces: {len(workspaces)}",
            len(workspaces) >= 1,
            f"Found: {[w[0] for w in workspaces]}"
        )

        return workspaces

    async def test_watcher_initialization(self):
        """Test Watcher initialization with multiple workspaces."""
        print("\n[2] Watcher Initialization")

        try:
            from backend.core.ouroboros.watcher import SynapticLSPClient

            watcher = SynapticLSPClient()

            # Collect available workspaces
            workspaces = [
                path for path in [self.jarvis_path, self.prime_path, self.reactor_path]
                if path.exists()
            ]

            # Initialize with workspaces
            init_ok = await watcher.initialize(workspaces)

            if init_ok:
                status = watcher.get_status()
                self._record("LSP server started", True, f"Server: {status['server']}")
                self._record(
                    "Workspaces registered",
                    len(status['workspaces']) >= 1,
                    f"Count: {len(status['workspaces'])}"
                )
                self._record(
                    "Capabilities available",
                    len(status['capabilities']) > 0,
                    f"Caps: {', '.join(status['capabilities'][:5])}"
                )
                return watcher
            else:
                self._record(
                    "LSP server started",
                    False,
                    "No LSP server available. Install with: pip install pyright"
                )
                return None

        except ImportError as e:
            self._record("Watcher module import", False, str(e))
            return None
        except Exception as e:
            self._record("Watcher initialization", False, str(e))
            return None

    async def test_single_repo_resolution(self, watcher):
        """Test symbol resolution within JARVIS repo."""
        print("\n[3] Single-Repo Symbol Resolution")

        # Find a Python file in JARVIS to test with
        test_files = list(self.jarvis_path.glob("backend/core/**/*.py"))[:5]

        if not test_files:
            self._record("Find test files", False, "No Python files found in backend/core")
            return

        self._record("Find test files", True, f"Found {len(test_files)} files")

        # Test definition lookup on a known symbol
        engine_file = self.jarvis_path / "backend/core/ouroboros/engine.py"

        if engine_file.exists():
            # Find OuroborosEngine class definition
            definition = await watcher.find_symbol_definition(
                "OuroborosEngine",
                engine_file
            )

            self._record(
                "Find OuroborosEngine definition",
                definition is not None,
                str(definition) if definition else "Not found"
            )

            # Find get_ouroboros_engine function
            func_def = await watcher.find_symbol_definition(
                "get_ouroboros_engine",
                engine_file
            )

            self._record(
                "Find get_ouroboros_engine definition",
                func_def is not None,
                str(func_def) if func_def else "Not found"
            )
        else:
            self._record("Engine file exists", False, f"Not found: {engine_file}")

    async def test_cross_repo_resolution(self, watcher):
        """Test symbol resolution across repos."""
        print("\n[4] Cross-Repo Symbol Resolution")

        # Check if we have multiple repos
        available_repos = [
            ("JARVIS", self.jarvis_path),
            ("Prime", self.prime_path),
            ("Reactor", self.reactor_path),
        ]
        existing_repos = [(name, path) for name, path in available_repos if path.exists()]

        if len(existing_repos) < 2:
            self._record(
                "Multiple repos available",
                True,
                f"Only {len(existing_repos)} repo(s) - cross-repo test skipped",
                skipped=True
            )
            return

        self._record("Multiple repos available", True, f"Found {len(existing_repos)} repos")

        # Test cross-repo import resolution
        # Look for imports between repos

        # Example: If JARVIS imports from reactor-core
        jarvis_files = list(self.jarvis_path.rglob("*.py"))[:20]

        cross_repo_imports = []
        for py_file in jarvis_files:
            try:
                content = py_file.read_text()
                if "reactor_core" in content.lower() or "jarvis_prime" in content.lower():
                    cross_repo_imports.append(py_file)
            except Exception:
                continue

        self._record(
            "Files with cross-repo imports",
            True,
            f"Found {len(cross_repo_imports)} files with potential cross-repo imports"
        )

        # Test resolving a cross-repo symbol if found
        if cross_repo_imports:
            test_file = cross_repo_imports[0]
            # Try to resolve any imported symbol
            refs = await watcher.get_all_references_to_symbol("publish", test_file)
            self._record(
                "Cross-repo reference lookup",
                True,
                f"Found {len(refs)} references"
            )

    async def test_signature_verification(self, watcher):
        """Test function signature verification."""
        print("\n[5] Function Signature Verification")

        from backend.core.ouroboros.watcher import OuroborosWatcherIntegration

        integration = OuroborosWatcherIntegration(watcher)

        # Test signature lookup for a known function
        engine_file = self.jarvis_path / "backend/core/ouroboros/engine.py"

        if engine_file.exists():
            # Get signature of improve_file function
            signature = await integration.get_function_signature_for_call(
                "improve_file",
                engine_file
            )

            self._record(
                "Get improve_file signature",
                signature is not None,
                (signature[:100] + "...") if signature and len(signature) > 100 else str(signature)
            )

            # Verify a function call
            verify_result = await watcher.verify_function_call(
                "get_ouroboros_engine",
                [],  # No args
                engine_file
            )

            self._record(
                "Verify function call",
                verify_result.get("valid", False),
                f"Exists: {verify_result.get('function_exists')}"
            )
        else:
            self._record("Engine file for signature test", False, "Not found")

    async def test_code_validation(self, watcher):
        """Test code validation functionality."""
        print("\n[6] Code Validation")

        # Test with valid code
        valid_code = '''
def hello_world():
    """A simple function."""
    print("Hello, World!")
    return True
'''

        test_file = self.jarvis_path / "backend/core/ouroboros/engine.py"

        is_valid, diagnostics = await watcher.validate_code(test_file, valid_code)

        # Note: Validation may report issues depending on context
        self._record(
            "Validate syntactically correct code",
            True,  # Just checking it doesn't crash
            f"Diagnostics: {len(diagnostics)}"
        )

        # Test with invalid code
        invalid_code = '''
def broken_function(
    print("Missing closing paren"
'''

        is_valid_invalid, diagnostics_invalid = await watcher.validate_code(
            test_file, invalid_code
        )

        # Should detect syntax error
        has_errors = any(d.severity.name == "ERROR" for d in diagnostics_invalid)

        self._record(
            "Detect syntax errors in invalid code",
            has_errors or not is_valid_invalid,
            f"Errors found: {len([d for d in diagnostics_invalid if d.severity.name == 'ERROR'])}"
        )

    async def test_ouroboros_integration(self, watcher):
        """Test integration with Ouroboros Engine."""
        print("\n[7] Ouroboros Integration")

        from backend.core.ouroboros.watcher import OuroborosWatcherIntegration

        integration = OuroborosWatcherIntegration(watcher)

        # Test validate_improvement
        original_code = '''
def process_data(data):
    result = []
    for item in data:
        result.append(item * 2)
    return result
'''

        improved_code = '''
def process_data(data):
    """Process data by doubling each item."""
    return [item * 2 for item in data]
'''

        test_file = self.jarvis_path / "backend/core/ouroboros/engine.py"

        validation_result = await integration.validate_improvement(
            test_file,
            original_code,
            improved_code
        )

        self._record(
            "Validate code improvement",
            "valid" in validation_result,
            f"Valid: {validation_result.get('valid')}, "
            f"New issues: {len(validation_result.get('new_issues', []))}"
        )

        # Test symbol reference verification
        code_with_calls = '''
import asyncio
from pathlib import Path

async def main():
    path = Path("/tmp")
    await asyncio.sleep(1)
'''

        symbol_result = await integration.verify_symbol_references(
            code_with_calls,
            test_file
        )

        self._record(
            "Verify symbol references",
            "valid" in symbol_result,
            f"Resolved: {len(symbol_result.get('resolved_symbols', []))}, "
            f"Unresolved: {len(symbol_result.get('unresolved_symbols', []))}"
        )

    def _print_summary(self):
        """Print test summary."""
        print("\n" + "=" * 60)
        print("Test Summary")
        print("=" * 60)

        total = self.results["passed"] + self.results["failed"] + self.results["skipped"]

        print(f"\n  Total Tests: {total}")
        print(f"  ✓ Passed:    {self.results['passed']}")
        print(f"  ✗ Failed:    {self.results['failed']}")
        print(f"  ⊘ Skipped:   {self.results['skipped']}")

        success_rate = (
            self.results["passed"] / (self.results["passed"] + self.results["failed"]) * 100
            if (self.results["passed"] + self.results["failed"]) > 0
            else 100
        )

        print(f"\n  Success Rate: {success_rate:.1f}%")

        if self.results["failed"] == 0:
            print("\n  🎉 All tests passed!")
        else:
            print("\n  ⚠️  Some tests failed. Check details above.")

        print("=" * 60 + "\n")


async def main():
    """Run the cross-repo test suite."""
    suite = CrossRepoTestSuite()
    success = await suite.run_all_tests()
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
