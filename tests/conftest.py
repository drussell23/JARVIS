"""
Pytest configuration and shared fixtures for JARVIS AI Agent tests.

This file contains:
- Shared fixtures available to all tests
- Test hooks and configuration
- Common test utilities
"""

import pytest
import sys
import os
from pathlib import Path

# Register additional fixture modules
pytest_plugins = [
    "tests.conftest_gmd_ferrari",
]

# Add project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "backend"))


@pytest.fixture(scope="session")
def project_root_path():
    """Return the project root directory path."""
    return project_root


@pytest.fixture(scope="session")
def backend_path():
    """Return the backend directory path."""
    return project_root / "backend"


@pytest.fixture(scope="session")
def frontend_path():
    """Return the frontend directory path."""
    return project_root / "frontend"


@pytest.fixture(scope="function")
def mock_env_vars(monkeypatch):
    """Fixture to set mock environment variables for testing."""
    test_vars = {
        "ANTHROPIC_API_KEY": "test_api_key_placeholder",
        "JARVIS_ENV": "test",
    }
    for key, value in test_vars.items():
        monkeypatch.setenv(key, value)
    return test_vars


@pytest.fixture(scope="function")
def temp_test_dir(tmp_path):
    """Provide a temporary directory for test file operations."""
    return tmp_path


def pytest_configure(config):
    """Configure pytest with custom settings."""
    # Register custom markers
    config.addinivalue_line(
        "markers", "unit: mark test as a unit test"
    )
    config.addinivalue_line(
        "markers", "integration: mark test as an integration test"
    )
    config.addinivalue_line(
        "markers", "functional: mark test as a functional test"
    )
    config.addinivalue_line(
        "markers", "performance: mark test as a performance test"
    )
    config.addinivalue_line(
        "markers", "e2e: mark test as an end-to-end test"
    )
    config.addinivalue_line(
        "markers", "vision: mark test as vision system related"
    )
    config.addinivalue_line(
        "markers", "voice: mark test as voice system related"
    )
    config.addinivalue_line(
        "markers", "backend: mark test as backend related"
    )
    config.addinivalue_line(
        "markers", "frontend: mark test as frontend related"
    )
    config.addinivalue_line(
        "markers", "slow: mark test as slow running"
    )
    config.addinivalue_line(
        "markers", "api: mark test as requiring API keys"
    )
    config.addinivalue_line(
        "markers", "permissions: mark test as requiring system permissions"
    )


def pytest_collection_modifyitems(config, items):
    """Modify test items during collection."""
    # Add markers based on test location
    for item in items:
        # Add markers based on path
        test_path = str(item.fspath)

        if "/unit/" in test_path:
            item.add_marker(pytest.mark.unit)
        if "/integration/" in test_path:
            item.add_marker(pytest.mark.integration)
        if "/functional/" in test_path:
            item.add_marker(pytest.mark.functional)
        if "/performance/" in test_path:
            item.add_marker(pytest.mark.performance)
            item.add_marker(pytest.mark.slow)
        if "/e2e/" in test_path:
            item.add_marker(pytest.mark.e2e)
            item.add_marker(pytest.mark.slow)

        # Component markers
        if "/vision/" in test_path:
            item.add_marker(pytest.mark.vision)
        if "/voice/" in test_path:
            item.add_marker(pytest.mark.voice)
        if "/backend/" in test_path:
            item.add_marker(pytest.mark.backend)


def pytest_report_header(config):
    """Add custom header to pytest report."""
    return [
        "JARVIS AI Agent Test Suite",
        f"Project Root: {project_root}",
    ]
