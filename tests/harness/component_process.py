"""ComponentProcess ABC and MockComponentProcess for lifecycle simulation.

Provides an abstract base class for managed component processes and
a mock implementation that drives status transitions through the
StateOracle.

Task 7 of the Disease 9 cross-repo integration test harness.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from tests.harness.types import ComponentStatus


class ComponentProcess(ABC):
    """Abstract base class for a managed component process.

    Parameters
    ----------
    name:
        The component name, used as the key in the StateOracle.
    oracle:
        A StateOracle (or MockStateOracle) for recording status transitions.
    """

    def __init__(self, name: str, oracle: Any) -> None:
        self._name = name
        self._oracle = oracle

    @property
    def name(self) -> str:
        return self._name

    @abstractmethod
    async def start(self) -> None:
        """Start the component, transitioning through STARTING -> HANDSHAKING -> READY."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully stop the component, transitioning through DRAINING -> STOPPING -> STOPPED."""
        ...

    @abstractmethod
    async def kill(self) -> None:
        """Forcefully terminate the component, transitioning directly to FAILED."""
        ...


class MockComponentProcess(ComponentProcess):
    """In-memory mock that drives status transitions via the oracle.

    Suitable for unit tests and mock-mode integration scenarios.
    Status transitions are synchronous (no real process management).
    """

    async def start(self) -> None:
        """Transition: STARTING -> HANDSHAKING -> READY."""
        self._oracle.set_component_status(self._name, ComponentStatus.STARTING)
        self._oracle.set_component_status(self._name, ComponentStatus.HANDSHAKING)
        self._oracle.set_component_status(self._name, ComponentStatus.READY)

    async def stop(self) -> None:
        """Transition: DRAINING -> STOPPING -> STOPPED."""
        self._oracle.set_component_status(self._name, ComponentStatus.DRAINING)
        self._oracle.set_component_status(self._name, ComponentStatus.STOPPING)
        self._oracle.set_component_status(self._name, ComponentStatus.STOPPED)

    async def kill(self) -> None:
        """Transition: directly to FAILED."""
        self._oracle.set_component_status(self._name, ComponentStatus.FAILED)
