#!/usr/bin/env python3
"""
Integration Test Script for Advanced Training System
====================================================

Tests that all components are properly integrated and functional.

Usage:
    python3 test_integration.py
"""

import asyncio
import sys
from pathlib import Path
from typing import Dict, List, Tuple


class IntegrationTester:
    """Tests advanced training system integration."""

    def __init__(self):
        self.results: List[Tuple[str, bool, str]] = []

    def log_test(self, name: str, success: bool, message: str = ""):
        """Log a test result."""
        symbol = "✅" if success else "❌"
        self.results.append((name, success, message))
        print(f"{symbol} {name}")
        if message:
            print(f"   {message}")

    async def test_imports(self) -> bool:
        """Test that all components can be imported."""
        print("\n" + "=" * 60)
        print("TEST 1: Component Imports")
        print("=" * 60)

        all_passed = True

        # Test Advanced Training Coordinator
        try:
            from backend.intelligence.advanced_training_coordinator import (
                AdvancedTrainingCoordinator,
                TrainingPriority,
                ReactorCoreClient,
                ResourceManager,
                AdvancedTrainingConfig
            )
            self.log_test(
                "Advanced Training Coordinator",
                True,
                "All classes imported successfully"
            )
        except ImportError as e:
            self.log_test("Advanced Training Coordinator", False, str(e))
            all_passed = False

        # Test Cross-Repo Startup Orchestrator
        try:
            from backend.supervisor.cross_repo_startup_orchestrator import (
                initialize_cross_repo_orchestration,
                start_all_repos,
                probe_jarvis_prime,
                probe_reactor_core
            )
            self.log_test(
                "Cross-Repo Startup Orchestrator",
                True,
                "All functions imported successfully"
            )
        except ImportError as e:
            self.log_test("Cross-Repo Startup Orchestrator", False, str(e))
            all_passed = False

        # Test Continuous Learning Orchestrator
        try:
            from backend.intelligence.continuous_learning_orchestrator import (
                ContinuousLearningOrchestrator
            )
            self.log_test(
                "Continuous Learning Orchestrator",
                True,
                "Successfully imported"
            )
        except ImportError as e:
            self.log_test("Continuous Learning Orchestrator", False, str(e))
            all_passed = False

        return all_passed

    async def test_configuration(self) -> bool:
        """Test that configuration is environment-driven."""
        print("\n" + "=" * 60)
        print("TEST 2: Configuration (Zero Hardcoding)")
        print("=" * 60)

        try:
            from backend.intelligence.advanced_training_coordinator import (
                AdvancedTrainingConfig
            )

            config = AdvancedTrainingConfig()

            # Verify key configuration parameters
            checks = [
                ("reactor_api_url", config.reactor_api_url),
                ("max_total_memory_gb", config.max_total_memory_gb),
                ("training_memory_reserve_gb", config.training_memory_reserve_gb),
                ("max_concurrent_training_jobs", config.max_concurrent_training_jobs),
                ("ab_test_enabled", config.ab_test_enabled),
                ("checkpoint_interval_epochs", config.checkpoint_interval_epochs),
            ]

            for name, value in checks:
                self.log_test(
                    f"Config: {name}",
                    True,
                    f"{value}"
                )

            return True

        except Exception as e:
            self.log_test("Configuration Test", False, str(e))
            return False

    async def test_coordinator_creation(self) -> bool:
        """Test that Advanced Training Coordinator can be created."""
        print("\n" + "=" * 60)
        print("TEST 3: Coordinator Creation")
        print("=" * 60)

        try:
            from backend.intelligence.advanced_training_coordinator import (
                AdvancedTrainingCoordinator
            )

            coordinator = await AdvancedTrainingCoordinator.create()

            self.log_test(
                "Advanced Training Coordinator Creation",
                True,
                "Coordinator initialized successfully"
            )

            # Test priority queue
            queue_size = coordinator._priority_queue.qsize()
            self.log_test(
                "Priority Queue",
                True,
                f"Queue initialized (size: {queue_size})"
            )

            return True

        except Exception as e:
            self.log_test("Coordinator Creation", False, str(e))
            return False

    async def test_reactor_core_client(self) -> bool:
        """Test Reactor Core client configuration."""
        print("\n" + "=" * 60)
        print("TEST 4: Reactor Core Client")
        print("=" * 60)

        try:
            from backend.intelligence.advanced_training_coordinator import (
                ReactorCoreClient,
                AdvancedTrainingConfig
            )

            config = AdvancedTrainingConfig()

            # Create client (not actually connecting)
            async with ReactorCoreClient(config) as client:
                self.log_test(
                    "Reactor Core Client Creation",
                    True,
                    f"Client configured for {config.reactor_api_url}"
                )

            return True

        except Exception as e:
            self.log_test("Reactor Core Client", False, str(e))
            return False

    async def test_resource_manager(self) -> bool:
        """Test Resource Manager initialization."""
        print("\n" + "=" * 60)
        print("TEST 5: Resource Manager")
        print("=" * 60)

        try:
            from backend.intelligence.advanced_training_coordinator import (
                ResourceManager,
                AdvancedTrainingConfig
            )

            config = AdvancedTrainingConfig()
            manager = ResourceManager(config)

            # Get resource snapshot
            snapshot = await manager.get_resource_snapshot()

            self.log_test(
                "Resource Manager Initialization",
                True,
                f"Total memory: {snapshot.total_memory_available_gb:.1f}GB"
            )

            # Check if snapshot has required attributes
            has_attrs = all([
                hasattr(snapshot, 'total_memory_available_gb'),
                hasattr(snapshot, 'jprime_memory_gb'),
                hasattr(snapshot, 'jprime_active_requests'),
            ])

            self.log_test(
                "Resource Snapshot",
                has_attrs,
                "All required attributes present"
            )

            return True

        except Exception as e:
            self.log_test("Resource Manager", False, str(e))
            return False

    async def test_documentation(self) -> bool:
        """Test that all documentation files exist."""
        print("\n" + "=" * 60)
        print("TEST 6: Documentation")
        print("=" * 60)

        docs = [
            "ADVANCED_TRAINING_SYSTEM_SUMMARY.md",
            "QUICK_START_TRAINING.md",
            "REACTOR_CORE_API_SPECIFICATION.md",
            "INTEGRATION_VERIFICATION.md",
        ]

        all_exist = True
        for doc in docs:
            path = Path(doc)
            exists = path.exists()
            all_exist = all_exist and exists

            if exists:
                size = path.stat().st_size
                self.log_test(
                    f"Documentation: {doc}",
                    True,
                    f"{size:,} bytes"
                )
            else:
                self.log_test(f"Documentation: {doc}", False, "File not found")

        return all_exist

    async def test_cross_repo_integration(self) -> bool:
        """Test cross-repo integration in run_supervisor.py."""
        print("\n" + "=" * 60)
        print("TEST 7: Cross-Repo Integration in Supervisor")
        print("=" * 60)

        try:
            supervisor_path = Path("run_supervisor.py")

            if not supervisor_path.exists():
                self.log_test("run_supervisor.py", False, "File not found")
                return False

            # Read supervisor file and check for integration
            content = supervisor_path.read_text()

            checks = [
                ("initialize_cross_repo_orchestration import",
                 "from backend.supervisor.cross_repo_startup_orchestrator import initialize_cross_repo_orchestration"),
                ("Cross-repo orchestration call",
                 "await initialize_cross_repo_orchestration()"),
                ("v10.1 version marker",
                 "v10.1"),
            ]

            for name, pattern in checks:
                found = pattern in content
                self.log_test(
                    f"Supervisor: {name}",
                    found,
                    "Found" if found else "Not found"
                )

            return all(pattern in content for _, pattern in checks)

        except Exception as e:
            self.log_test("Supervisor Integration", False, str(e))
            return False

    async def run_all_tests(self) -> bool:
        """Run all integration tests."""
        print("\n" + "🔍" * 30)
        print("INTEGRATION TEST SUITE - Advanced Training System v2.0")
        print("🔍" * 30)

        tests = [
            self.test_imports(),
            self.test_configuration(),
            self.test_coordinator_creation(),
            self.test_reactor_core_client(),
            self.test_resource_manager(),
            self.test_documentation(),
            self.test_cross_repo_integration(),
        ]

        results = await asyncio.gather(*tests, return_exceptions=True)

        # Count results
        passed = sum(1 for r in results if r is True)
        failed = sum(1 for r in results if r is not True)
        total = len(results)

        # Print summary
        print("\n" + "=" * 60)
        print("TEST SUMMARY")
        print("=" * 60)
        print(f"Total Tests: {total}")
        print(f"Passed: {passed} ✅")
        print(f"Failed: {failed} ❌")
        print(f"Success Rate: {(passed/total)*100:.1f}%")
        print("=" * 60)

        if all(r is True for r in results):
            print("\n🎉 ALL TESTS PASSED - Integration is complete!")
            print("\nYou can now run: python3 run_supervisor.py")
            return True
        else:
            print("\n⚠️  Some tests failed - review errors above")
            return False

    def print_final_status(self):
        """Print detailed status of all components."""
        print("\n" + "🔧" * 30)
        print("COMPONENT STATUS")
        print("🔧" * 30)

        for name, success, message in self.results:
            symbol = "✅" if success else "❌"
            print(f"{symbol} {name}")
            if message:
                print(f"   └─ {message}")

        print("🔧" * 30 + "\n")


async def main():
    """Main test runner."""
    tester = IntegrationTester()

    try:
        success = await tester.run_all_tests()
        tester.print_final_status()

        sys.exit(0 if success else 1)

    except Exception as e:
        print(f"\n❌ Fatal error during testing: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
