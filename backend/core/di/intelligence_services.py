"""
Intelligence Services Registration
===================================

Registers all intelligence layer services with the DI container.
This replaces the manual instantiation in run_supervisor.py.

The module provides declarative service registration with:
- Environment-driven enablement
- Proper dependency injection (config -> engine -> coordinator)
- Graceful degradation for optional services
- Correct parameter passing (config, not engine instances)

Author: JARVIS Intelligence System
Version: 1.0.0
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Type

logger = logging.getLogger("jarvis.di.intelligence")


def register_intelligence_services(container: Any) -> Dict[str, bool]:
    """
    Register all intelligence layer services with the container.

    This function replaces the manual service instantiation in
    run_supervisor.py lines 12620-12820, fixing the 4 initialization bugs:

    1. Parameter mismatch (collaboration_engine= -> config=)
    2. Wrong method call (.start() -> .initialize())
    3. Manual ordering (now handled by container)
    4. Uncoordinated factories (now unified via container)

    Args:
        container: The ServiceContainer instance

    Returns:
        Dict mapping service name to registration success
    """
    from backend.core.di.protocols import Scope, ServiceCriticality, DependencySpec, DependencyType

    registered: Dict[str, bool] = {}

    # =========================================================================
    # COLLABORATION ENGINE
    # =========================================================================
    if os.getenv("JARVIS_COLLABORATION_ENABLED", "true").lower() == "true":
        try:
            from backend.intelligence.collaboration_engine import (
                CollaborationConfig,
                CollaborationEngine,
                CrossRepoCollaborationCoordinator,
            )

            # Register config (no dependencies)
            container.register(
                CollaborationConfig,
                scope=Scope.SINGLETON,
                factory=lambda: CollaborationConfig.from_env(),
            )

            # Register engine (depends on config)
            container.register(
                CollaborationEngine,
                scope=Scope.SINGLETON,
                criticality=ServiceCriticality.OPTIONAL,
                dependencies=[DependencySpec(CollaborationConfig, DependencyType.REQUIRED, param_name="config")],
                factory=lambda config: CollaborationEngine(config=config),
            )

            # Register cross-repo coordinator (depends on config, NOT engine)
            # FIX: The coordinator expects config, not engine instance
            if os.getenv("JARVIS_CROSS_REPO_COLLAB", "true").lower() == "true":
                container.register(
                    CrossRepoCollaborationCoordinator,
                    scope=Scope.SINGLETON,
                    criticality=ServiceCriticality.OPTIONAL,
                    dependencies=[DependencySpec(CollaborationConfig, DependencyType.REQUIRED, param_name="config")],
                    factory=lambda config: CrossRepoCollaborationCoordinator(config=config),
                )
                registered["CrossRepoCollaborationCoordinator"] = True

            registered["CollaborationEngine"] = True
            logger.info("Registered collaboration services")

        except ImportError as e:
            logger.info(f"Collaboration engine not available: {e}")
            registered["CollaborationEngine"] = False

    # =========================================================================
    # CODE OWNERSHIP ENGINE
    # =========================================================================
    if os.getenv("JARVIS_CODE_OWNERSHIP_ENABLED", "true").lower() == "true":
        try:
            from backend.intelligence.code_ownership import (
                OwnershipConfig,
                CodeOwnershipEngine,
                CrossRepoOwnershipCoordinator,
            )

            # Register config
            container.register(
                OwnershipConfig,
                scope=Scope.SINGLETON,
                factory=lambda: OwnershipConfig.from_env(),
            )

            # Register engine
            container.register(
                CodeOwnershipEngine,
                scope=Scope.SINGLETON,
                criticality=ServiceCriticality.OPTIONAL,
                dependencies=[DependencySpec(OwnershipConfig, DependencyType.REQUIRED, param_name="config")],
                factory=lambda config: CodeOwnershipEngine(config=config),
            )

            # Register cross-repo coordinator (depends on config, NOT engine)
            if os.getenv("JARVIS_CROSS_REPO_OWNERSHIP", "true").lower() == "true":
                container.register(
                    CrossRepoOwnershipCoordinator,
                    scope=Scope.SINGLETON,
                    criticality=ServiceCriticality.OPTIONAL,
                    dependencies=[DependencySpec(OwnershipConfig, DependencyType.REQUIRED, param_name="config")],
                    factory=lambda config: CrossRepoOwnershipCoordinator(config=config),
                )
                registered["CrossRepoOwnershipCoordinator"] = True

            registered["CodeOwnershipEngine"] = True
            logger.info("Registered code ownership services")

        except ImportError as e:
            logger.info(f"Code ownership engine not available: {e}")
            registered["CodeOwnershipEngine"] = False

    # =========================================================================
    # REVIEW WORKFLOW ENGINE
    # =========================================================================
    if os.getenv("JARVIS_REVIEW_WORKFLOW_ENABLED", "true").lower() == "true":
        try:
            from backend.intelligence.review_workflow import (
                ReviewWorkflowConfig,
                ReviewWorkflowEngine,
                CrossRepoReviewCoordinator,
            )

            # Register config
            container.register(
                ReviewWorkflowConfig,
                scope=Scope.SINGLETON,
                factory=lambda: ReviewWorkflowConfig.from_env(),
            )

            # Register engine
            container.register(
                ReviewWorkflowEngine,
                scope=Scope.SINGLETON,
                criticality=ServiceCriticality.OPTIONAL,
                dependencies=[DependencySpec(ReviewWorkflowConfig, DependencyType.REQUIRED, param_name="config")],
                factory=lambda config: ReviewWorkflowEngine(config=config),
            )

            # Register cross-repo coordinator (depends on config, NOT engine)
            if os.getenv("JARVIS_CROSS_REPO_REVIEW", "true").lower() == "true":
                container.register(
                    CrossRepoReviewCoordinator,
                    scope=Scope.SINGLETON,
                    criticality=ServiceCriticality.OPTIONAL,
                    dependencies=[DependencySpec(ReviewWorkflowConfig, DependencyType.REQUIRED, param_name="config")],
                    factory=lambda config: CrossRepoReviewCoordinator(config=config),
                )
                registered["CrossRepoReviewCoordinator"] = True

            registered["ReviewWorkflowEngine"] = True
            logger.info("Registered review workflow services")

        except ImportError as e:
            logger.info(f"Review workflow engine not available: {e}")
            registered["ReviewWorkflowEngine"] = False

    # =========================================================================
    # LSP SERVER
    # =========================================================================
    if os.getenv("JARVIS_LSP_SERVER_ENABLED", "true").lower() == "true":
        try:
            from backend.intelligence.lsp_server import (
                LSPServerConfig,
                JARVISLSPServer,
            )

            # Register config
            container.register(
                LSPServerConfig,
                scope=Scope.SINGLETON,
                factory=lambda: LSPServerConfig.from_env(),
            )

            # Register LSP server
            container.register(
                JARVISLSPServer,
                scope=Scope.SINGLETON,
                criticality=ServiceCriticality.OPTIONAL,
                dependencies=[DependencySpec(LSPServerConfig, DependencyType.REQUIRED, param_name="config")],
                factory=lambda config: JARVISLSPServer(config=config),
            )

            registered["JARVISLSPServer"] = True
            logger.info("Registered LSP server")

        except ImportError as e:
            logger.info(f"LSP server not available: {e}")
            registered["JARVISLSPServer"] = False

    # =========================================================================
    # IDE INTEGRATION ENGINE
    # =========================================================================
    if os.getenv("JARVIS_IDE_INTEGRATION_ENABLED", "true").lower() == "true":
        try:
            from backend.intelligence.ide_integration import (
                IDEIntegrationConfig,
                IDEIntegrationEngine,
                CrossRepoIDECoordinator,
            )

            # Register config
            container.register(
                IDEIntegrationConfig,
                scope=Scope.SINGLETON,
                factory=lambda: IDEIntegrationConfig.from_env(),
            )

            # Register engine
            container.register(
                IDEIntegrationEngine,
                scope=Scope.SINGLETON,
                criticality=ServiceCriticality.OPTIONAL,
                dependencies=[DependencySpec(IDEIntegrationConfig, DependencyType.REQUIRED, param_name="config")],
                factory=lambda config: IDEIntegrationEngine(config=config),
            )

            # Register cross-repo coordinator (depends on config, NOT engine)
            if os.getenv("JARVIS_CROSS_REPO_IDE", "true").lower() == "true":
                container.register(
                    CrossRepoIDECoordinator,
                    scope=Scope.SINGLETON,
                    criticality=ServiceCriticality.OPTIONAL,
                    dependencies=[DependencySpec(IDEIntegrationConfig, DependencyType.REQUIRED, param_name="config")],
                    factory=lambda config: CrossRepoIDECoordinator(config=config),
                )
                registered["CrossRepoIDECoordinator"] = True

            registered["IDEIntegrationEngine"] = True
            logger.info("Registered IDE integration services")

        except ImportError as e:
            logger.info(f"IDE integration engine not available: {e}")
            registered["IDEIntegrationEngine"] = False

    return registered


async def initialize_intelligence_services(container: Any) -> Dict[str, str]:
    """
    Initialize all registered intelligence services.

    The container handles:
    - Dependency ordering (topological sort)
    - Parallel initialization where possible
    - Graceful degradation for optional services
    - Proper lifecycle method calls (.initialize(), not .start())

    Args:
        container: The initialized ServiceContainer

    Returns:
        Dict mapping service name to status string
    """
    status: Dict[str, str] = {}

    try:
        # Container handles ordering and parallelization
        await container.initialize_all()

        # Collect status for each registered service
        for service_type in container.get_registered_services():
            try:
                # Just verify the service is resolvable (async resolve)
                instance = await container.resolve(service_type)
                if instance is not None:
                    status[service_type.__name__] = "initialized"
                else:
                    status[service_type.__name__] = "not available"
            except Exception as e:
                status[service_type.__name__] = f"failed: {e}"

    except Exception as e:
        logger.error(f"Intelligence services initialization failed: {e}")
        status["_container"] = f"initialization_failed: {e}"

    return status


async def shutdown_intelligence_services(container: Any) -> None:
    """
    Shutdown all intelligence services gracefully.

    The container handles:
    - Reverse dependency ordering (LIFO)
    - Timeout handling
    - Error isolation (one failure doesn't stop others)

    Args:
        container: The ServiceContainer to shutdown
    """
    try:
        await container.shutdown_all()
        logger.info("Intelligence services shutdown complete")
    except Exception as e:
        logger.error(f"Error during intelligence services shutdown: {e}")


def get_service_status_display(status: Dict[str, str]) -> List[str]:
    """
    Format service status for terminal display.

    Args:
        status: Dict from initialize_intelligence_services

    Returns:
        List of formatted status lines
    """
    lines = []
    for service_name, result in sorted(status.items()):
        if result == "initialized":
            lines.append(f"  ✓ {service_name}: Ready")
        elif result == "not available":
            lines.append(f"  ⚠️ {service_name}: Not available")
        elif result.startswith("failed:"):
            error = result.replace("failed: ", "")
            lines.append(f"  ❌ {service_name}: Failed ({error})")
        else:
            lines.append(f"  ? {service_name}: {result}")
    return lines
