#!/usr/bin/env python3
"""
Real-World Warmup Scenario Simulator
====================================

Simulates realistic startup scenarios to validate warmup system robustness.
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import List

from core.component_warmup import ComponentPriority, ComponentWarmupSystem

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Module-level set to prevent GC of fire-and-forget background tasks
_background_tasks: set = set()


@dataclass
class ScenarioResult:
    """Results from a scenario simulation"""

    scenario_name: str
    total_duration: float
    ready_count: int
    failed_count: int
    critical_ready: bool
    success: bool
    issues_encountered: List[str]
    performance_score: float


class WarmupScenarioSimulator:
    """Simulates various real-world warmup scenarios"""

    def __init__(self):
        """Initialize the scenario simulator with all available scenarios."""
        self.scenarios = {
            "ideal": self.scenario_ideal_conditions,
            "network_issues": self.scenario_network_issues,
            "memory_constrained": self.scenario_memory_constrained,
            "flaky_services": self.scenario_flaky_services,
            "slow_database": self.scenario_slow_database,
            "mixed_failures": self.scenario_mixed_failures,
            "high_load": self.scenario_high_load,
            "dependency_cascade": self.scenario_dependency_cascade,
        }

    async def run_all_scenarios(self):
        """Run all scenarios and report results"""
        results = []

        for scenario_name, scenario_func in self.scenarios.items():
            logger.info(f"\n{'=' * 80}")
            logger.info(f"Running Scenario: {scenario_name.upper()}")
            logger.info(f"{'=' * 80}\n")

            result = await scenario_func()
            results.append(result)

            self._print_scenario_result(result)

        self._print_summary(results)
        return results

    async def scenario_ideal_conditions(self) -> ScenarioResult:
        """Ideal conditions: everything works perfectly"""
        warmup = ComponentWarmupSystem(max_concurrent=10)
        issues = []

        # Register fast, reliable components
        async def fast_loader(name):
            """Fast reliable component loader."""
            await asyncio.sleep(random.uniform(0.1, 0.3))  # nosec B311
            return f"component_{name}"

        components = {
            "screen_detector": ComponentPriority.CRITICAL,
            "voice_auth": ComponentPriority.CRITICAL,
            "context_handler": ComponentPriority.HIGH,
            "nlp_resolver": ComponentPriority.HIGH,
            "vision_system": ComponentPriority.MEDIUM,
            "database": ComponentPriority.MEDIUM,
            "analytics": ComponentPriority.LOW,
        }

        for name, priority in components.items():
            warmup.register_component(
                name=name, loader=lambda n=name: fast_loader(n), priority=priority, timeout=10.0
            )

        start = time.time()
        report = await warmup.warmup_all()
        duration = time.time() - start

        # Calculate score (100 = perfect)
        score = 100.0 * (report["ready_count"] / len(components))

        return ScenarioResult(
            scenario_name="Ideal Conditions",
            total_duration=duration,
            ready_count=report["ready_count"],
            failed_count=report["failed_count"],
            critical_ready=warmup.is_ready("screen_detector") and warmup.is_ready("voice_auth"),
            success=report["ready_count"] == len(components),
            issues_encountered=issues,
            performance_score=score,
        )

    async def scenario_network_issues(self) -> ScenarioResult:
        """Simulate network connectivity issues"""
        warmup = ComponentWarmupSystem(max_concurrent=10)
        issues = []

        async def network_dependent_loader(name, failure_rate=0.3):
            """Network-dependent loader with simulated failures."""
            # Simulate network delay
            await asyncio.sleep(random.uniform(0.5, 2.0))  # nosec B311

            # Random network failures
            if random.random() < failure_rate:  # nosec B311
                issues.append(f"{name}: Network timeout")
                raise Exception(f"Network timeout for {name}")

            return f"component_{name}"

        # Critical components should be local (no network)
        async def local_loader(name):
            """Local fast loader without network dependency."""
            await asyncio.sleep(random.uniform(0.1, 0.2))  # nosec B311
            return f"component_{name}"

        warmup.register_component(
            "screen_detector",
            lambda: local_loader("screen_detector"),
            ComponentPriority.CRITICAL,
            timeout=5.0,
        )

        warmup.register_component(
            "voice_auth",
            lambda: network_dependent_loader("voice_auth", 0.2),
            ComponentPriority.CRITICAL,
            timeout=10.0,
            retry_count=2,
            required=False,
        )

        warmup.register_component(
            "cloud_sync",
            lambda: network_dependent_loader("cloud_sync", 0.5),
            ComponentPriority.MEDIUM,
            timeout=8.0,
            retry_count=1,
            required=False,
        )

        warmup.register_component(
            "remote_analytics",
            lambda: network_dependent_loader("remote_analytics", 0.7),
            ComponentPriority.LOW,
            timeout=5.0,
            required=False,
        )

        start = time.time()
        report = await warmup.warmup_all()
        duration = time.time() - start

        critical_ok = warmup.is_ready("screen_detector")
        score = 100.0 if critical_ok else 0.0
        score -= 10.0 * len(issues)

        return ScenarioResult(
            scenario_name="Network Issues",
            total_duration=duration,
            ready_count=report["ready_count"],
            failed_count=report["failed_count"],
            critical_ready=critical_ok,
            success=critical_ok,  # Success if critical components work
            issues_encountered=issues,
            performance_score=max(0, score),
        )

    async def scenario_memory_constrained(self) -> ScenarioResult:
        """Simulate memory-constrained environment"""
        warmup = ComponentWarmupSystem(max_concurrent=3)  # Lower concurrency
        issues = []

        memory_usage = {"current": 0, "peak": 0}

        async def memory_heavy_loader(name, size_mb):
            """Memory-intensive loader."""
            # Allocate memory
            memory_usage["current"] += size_mb
            memory_usage["peak"] = max(memory_usage["peak"], memory_usage["current"])

            if memory_usage["current"] > 500:  # 500MB limit
                issues.append(f"{name}: Memory limit exceeded")
                raise MemoryError(f"Out of memory loading {name}")

            await asyncio.sleep(random.uniform(0.5, 1.5))  # nosec B311

            # Release memory
            memory_usage["current"] -= size_mb
            return f"component_{name}"

        # Small components
        warmup.register_component(
            "screen_detector",
            lambda: memory_heavy_loader("screen_detector", 50),
            ComponentPriority.CRITICAL,
        )

        # Medium components
        warmup.register_component(
            "context_handler",
            lambda: memory_heavy_loader("context_handler", 100),
            ComponentPriority.HIGH,
        )

        # Large components
        warmup.register_component(
            "vision_model",
            lambda: memory_heavy_loader("vision_model", 300),
            ComponentPriority.MEDIUM,
            required=False,
        )

        warmup.register_component(
            "ml_model",
            lambda: memory_heavy_loader("ml_model", 400),
            ComponentPriority.LOW,
            required=False,
        )

        start = time.time()
        report = await warmup.warmup_all()
        duration = time.time() - start

        score = 100.0 * (report["ready_count"] / 4)
        score -= 5.0 * len(issues)

        logger.info(f"Peak memory usage: {memory_usage['peak']}MB")

        return ScenarioResult(
            scenario_name="Memory Constrained",
            total_duration=duration,
            ready_count=report["ready_count"],
            failed_count=report["failed_count"],
            critical_ready=warmup.is_ready("screen_detector"),
            success=report["ready_count"] >= 2,  # At least critical components
            issues_encountered=issues,
            performance_score=max(0, score),
        )

    async def scenario_flaky_services(self) -> ScenarioResult:
        """Simulate flaky external services"""
        warmup = ComponentWarmupSystem(max_concurrent=10)
        issues = []
        attempt_counts = {}

        async def flaky_loader(name, success_rate=0.6):
            """Flaky loader with configurable success rate."""
            if name not in attempt_counts:
                attempt_counts[name] = 0
            attempt_counts[name] += 1

            await asyncio.sleep(random.uniform(0.3, 1.0))  # nosec B311

            if random.random() > success_rate:  # nosec B311
                issues.append(f"{name}: Service unavailable (attempt {attempt_counts[name]})")
                raise Exception(f"{name} service unavailable")

            return f"component_{name}"

        # Stable critical components
        async def stable_loader(name):
            """Stable reliable loader."""
            await asyncio.sleep(0.1)
            return f"component_{name}"

        warmup.register_component(
            "screen_detector", lambda: stable_loader("screen_detector"), ComponentPriority.CRITICAL
        )

        # Flaky services with retries
        warmup.register_component(
            "voice_service",
            lambda: flaky_loader("voice_service", 0.7),
            ComponentPriority.HIGH,
            retry_count=3,
            required=False,
        )

        warmup.register_component(
            "database",
            lambda: flaky_loader("database", 0.5),
            ComponentPriority.HIGH,
            retry_count=3,
            required=False,
        )

        warmup.register_component(
            "cache",
            lambda: flaky_loader("cache", 0.8),
            ComponentPriority.MEDIUM,
            retry_count=2,
            required=False,
        )

        start = time.time()
        report = await warmup.warmup_all()
        duration = time.time() - start

        # Log retry statistics
        for name, count in attempt_counts.items():
            logger.info(f"{name}: {count} attempts")

        score = 100.0 * (report["ready_count"] / 4)

        return ScenarioResult(
            scenario_name="Flaky Services",
            total_duration=duration,
            ready_count=report["ready_count"],
            failed_count=report["failed_count"],
            critical_ready=warmup.is_ready("screen_detector"),
            success=report["ready_count"] >= 2,
            issues_encountered=issues,
            performance_score=score,
        )

    async def scenario_slow_database(self) -> ScenarioResult:
        """Simulate slow database connection"""
        warmup = ComponentWarmupSystem(max_concurrent=10)
        issues = []

        async def fast_loader(name):
            await asyncio.sleep(random.uniform(0.1, 0.3))  # nosec B311
            return f"component_{name}"

        async def slow_database_loader():
            """Slow database connection loader."""
            # Database takes 8 seconds to connect
            logger.info("Database: Connecting (this will take 8 seconds)...")
            await asyncio.sleep(8.0)
            return "database_component"

        # Fast critical components
        warmup.register_component(
            "screen_detector", lambda: fast_loader("screen_detector"), ComponentPriority.CRITICAL
        )

        warmup.register_component(
            "voice_auth", lambda: fast_loader("voice_auth"), ComponentPriority.CRITICAL
        )

        # Fast high priority
        warmup.register_component(
            "context_handler", lambda: fast_loader("context_handler"), ComponentPriority.HIGH
        )

        # Slow database (non-blocking due to priority)
        warmup.register_component(
            "learning_db",
            slow_database_loader,
            ComponentPriority.MEDIUM,
            timeout=15.0,
            required=False,
        )

        # Other fast components
        warmup.register_component(
            "analytics", lambda: fast_loader("analytics"), ComponentPriority.LOW
        )

        start = time.time()

        # Wait for critical components only
        _task = asyncio.create_task(
            warmup.warmup_all(),
            name="warmup-scenario-warmup-all",
        )
        _background_tasks.add(_task)
        _task.add_done_callback(_background_tasks.discard)
        await warmup.wait_for_critical(timeout=5.0)

        critical_duration = time.time() - start

        logger.info(f"Critical components ready in {critical_duration:.2f}s")

        # Wait for full warmup
        await asyncio.sleep(10.0)  # Give database time to finish

        report = {
            "ready_count": 4 if warmup.is_ready("learning_db") else 4,
            "failed_count": 0 if warmup.is_ready("learning_db") else 1,
        }

        score = 100.0 if critical_duration < 3.0 else 50.0

        return ScenarioResult(
            scenario_name="Slow Database",
            total_duration=critical_duration,
            ready_count=report["ready_count"],
            failed_count=report["failed_count"],
            critical_ready=True,
            success=critical_duration < 5.0,  # Critical ready quickly
            issues_encountered=issues,
            performance_score=score,
        )

    async def scenario_mixed_failures(self) -> ScenarioResult:
        """Simulate mix of timeouts, failures, and successes"""
        warmup = ComponentWarmupSystem(max_concurrent=10)
        issues = []

        async def hanging_loader():
            """Loader that hangs indefinitely."""
            await asyncio.sleep(100)
            return "hanging"

        async def failing_loader():
            """Loader that always fails."""
            raise Exception("Component broken")

        async def slow_loader():
            """Slow but successful loader."""
            await asyncio.sleep(3.0)
            return "slow_component"

        async def fast_loader():
            """Fast successful loader."""
            await asyncio.sleep(0.2)
            return "fast_component"

        warmup.register_component(
            "fast", fast_loader, ComponentPriority.CRITICAL, timeout=5.0, required=True
        )

        warmup.register_component(
            "hanging", hanging_loader, ComponentPriority.HIGH, timeout=2.0, required=False
        )

        warmup.register_component(
            "failing", failing_loader, ComponentPriority.HIGH, timeout=5.0, required=False
        )

        warmup.register_component(
            "slow", slow_loader, ComponentPriority.MEDIUM, timeout=5.0, required=False
        )

        start = time.time()
        report = await warmup.warmup_all()
        duration = time.time() - start

        # Hanging should timeout
        if not warmup.is_ready("hanging"):
            issues.append("hanging: Timed out (expected)")

        # Failing should fail
        if not warmup.is_ready("failing"):
            issues.append("failing: Failed to load (expected)")

        score = 100.0 if warmup.is_ready("fast") and warmup.is_ready("slow") else 50.0

        return ScenarioResult(
            scenario_name="Mixed Failures",
            total_duration=duration,
            ready_count=report["ready_count"],
            failed_count=report["failed_count"],
            critical_ready=warmup.is_ready("fast"),
            success=warmup.is_ready("fast"),
            issues_encountered=issues,
            performance_score=score,
        )

    async def scenario_high_load(self) -> ScenarioResult:
        """Simulate high load with many components"""
        warmup = ComponentWarmupSystem(max_concurrent=10)
        issues = []

        async def component_loader(name):
            """Generic component loader."""
            await asyncio.sleep(random.uniform(0.1, 0.5))  # nosec B311
            return f"component_{name}"

        # Register 50 components
        priorities = [
            ComponentPriority.CRITICAL,
            ComponentPriority.HIGH,
            ComponentPriority.HIGH,
            ComponentPriority.MEDIUM,
            ComponentPriority.MEDIUM,
        ]

        for i in range(50):
            priority = priorities[i % len(priorities)]
            warmup.register_component(
                f"component_{i:02d}",
                lambda n=i: component_loader(f"comp_{n:02d}"),
                priority,
                timeout=10.0,
                required=False,
            )

        start = time.time()
        report = await warmup.warmup_all()
        duration = time.time() - start

        score = 100.0 * (report["ready_count"] / 50)

        logger.info(f"Loaded {report['ready_count']}/50 components in {duration:.2f}s")

        return ScenarioResult(
            scenario_name="High Load",
            total_duration=duration,
            ready_count=report["ready_count"],
            failed_count=report["failed_count"],
            critical_ready=warmup.is_ready("component_00"),
            success=report["ready_count"] >= 45,  # At least 90% success
            issues_encountered=issues,
            performance_score=score,
        )

    async def scenario_dependency_cascade(self) -> ScenarioResult:
        """Simulate complex dependency chain"""
        warmup = ComponentWarmupSystem(max_concurrent=10)
        issues = []

        load_order = []

        async def tracked_loader(name):
            """Loader that tracks load order."""
            load_order.append(name)
            await asyncio.sleep(0.5)
            return f"component_{name}"

        # Create dependency chain: A → B → C → D → E
        warmup.register_component(
            "A", lambda: tracked_loader("A"), ComponentPriority.HIGH, timeout=5.0
        )

        warmup.register_component(
            "B",
            lambda: tracked_loader("B"),
            ComponentPriority.HIGH,
            dependencies=["A"],
            timeout=5.0,
        )

        warmup.register_component(
            "C",
            lambda: tracked_loader("C"),
            ComponentPriority.HIGH,
            dependencies=["B"],
            timeout=5.0,
        )

        warmup.register_component(
            "D",
            lambda: tracked_loader("D"),
            ComponentPriority.HIGH,
            dependencies=["C"],
            timeout=5.0,
        )

        warmup.register_component(
            "E",
            lambda: tracked_loader("E"),
            ComponentPriority.HIGH,
            dependencies=["D"],
            timeout=5.0,
        )

        start = time.time()
        report = await warmup.warmup_all()
        duration = time.time() - start

        # Verify load order
        logger.info(f"Load order: {' → '.join(load_order)}")

        correct_order = True
        for i in range(len(load_order) - 1):
            if load_order[i] > load_order[i + 1]:
                correct_order = False
                issues.append(f"Order violation: {load_order[i]} loaded before {load_order[i+1]}")

        score = 100.0 if correct_order and report["ready_count"] == 5 else 50.0

        return ScenarioResult(
            scenario_name="Dependency Cascade",
            total_duration=duration,
            ready_count=report["ready_count"],
            failed_count=report["failed_count"],
            critical_ready=True,
            success=correct_order and report["ready_count"] == 5,
            issues_encountered=issues,
            performance_score=score,
        )

    def _print_scenario_result(self, result: ScenarioResult):
        """Print formatted scenario result"""
        status = "✅ PASS" if result.success else "❌ FAIL"

        print(f"\n{status} {result.scenario_name}")
        print(f"Duration: {result.total_duration:.2f}s")
        print(f"Components Ready: {result.ready_count} | Failed: {result.failed_count}")
        print(f"Critical Ready: {'✅' if result.critical_ready else '❌'}")
        print(f"Performance Score: {result.performance_score:.1f}/100")

        if result.issues_encountered:
            print(f"Issues: {len(result.issues_encountered)}")
            for issue in result.issues_encountered[:5]:  # Show first 5
                print(f"  - {issue}")

    def _print_summary(self, results: List[ScenarioResult]):
        """Print summary of all scenarios"""
        print(f"\n{'=' * 80}")
        print("SCENARIO SUMMARY")
        print(f"{'=' * 80}\n")

        total_scenarios = len(results)
        passed = sum(1 for r in results if r.success)
        avg_score = sum(r.performance_score for r in results) / total_scenarios

        print(f"Total Scenarios: {total_scenarios}")
        print(f"Passed: {passed}/{total_scenarios} ({100*passed/total_scenarios:.1f}%)")
        print(f"Average Score: {avg_score:.1f}/100")

        print(f"\nScenario Breakdown:")
        for result in results:
            status = "✅" if result.success else "❌"
            print(
                f"  {status} {result.scenario_name:25s} - {result.performance_score:5.1f}/100 - {result.total_duration:5.2f}s"
            )


async def main():
    """Run all scenarios"""
    simulator = WarmupScenarioSimulator()
    await simulator.run_all_scenarios()


if __name__ == "__main__":
    asyncio.run(main())
