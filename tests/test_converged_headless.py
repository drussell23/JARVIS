# [Ouroboros] Modified by Ouroboros (op=op-01a1218b-) at 2026-10-09 16:43 UTC
# Reason: `backend/api/converged_headless.py` has no corresponding test module. CREATE `tests/test_converged_headless.py` containi

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

# Import the module under test
from backend.api.converged_headless import (
    create_converged_app,
    default_subsystems,
    main,
)


class TestConvergedHeadless:
    """Tests for converged headless functionality."""

    def test_import_smoke(self) -> None:
        """Test that the module can be imported without errors."""
        # This test ensures no syntax or import errors in the module
        assert default_subsystems is not None
        assert create_converged_app is not None
        assert main is not None

    def test_default_subsystems_structure(self) -> None:
        """Test that default_subsystems returns expected structure."""
        subsystems = default_subsystems()
        
        assert isinstance(subsystems, list)
        assert len(subsystems) == 2
        
        # Check first subsystem
        assert subsystems[0].name == "governance_bridge"
        assert subsystems[0].label == "O+V telemetry bridge"
        assert callable(subsystems[0].loader)
        
        # Check second subsystem
        assert subsystems[1].name == "ouroboros_daemon"
        assert subsystems[1].label == "OuroborosDaemon (Actor)"
        assert callable(subsystems[1].loader)
        assert subsystems[1].depends_on == ("governance_bridge",)

    @pytest.mark.asyncio
    async def test_create_converged_app_basic(self) -> None:
        """Test basic create_converged_app functionality."""
        app = create_converged_app()
        
        assert isinstance(app, FastAPI)
        assert app.title == "JARVIS Converged Headless"
        
        # Test that the system_status endpoint exists
        routes = [route for route in app.routes if hasattr(route, 'path') and route.path == '/api/system/status']
        assert len(routes) == 1

    @pytest.mark.asyncio
    async def test_create_converged_app_with_custom_params(self) -> None:
        """Test create_converged_app with custom parameters."""
        # Mock the subsystems to avoid actual loading
        mock_subsystems = [
            MagicMock(name="test_subsystem", label="Test Subsystem", loader=AsyncMock())
        ]
        
        app = create_converged_app(
            subsystems=mock_subsystems,
            dw_provider=MagicMock(),
            recovery=MagicMock(),
            run_selftest=False,
            mount_router=False
        )
        
        assert isinstance(app, FastAPI)
        # Should not have router mounted when mount_router=False
        routes = [route for route in app.routes if hasattr(route, 'path') and route.path == '/api/system/status']
        assert len(routes) == 1  # The status endpoint should still exist

    @pytest.mark.asyncio
    async def test_create_converged_app_with_none_recovery(self) -> None:
        """Test create_converged_app with recovery=None and run_selftest=True."""
        app = create_converged_app(recovery=None, run_selftest=True)
        assert isinstance(app, FastAPI)

    @pytest.mark.asyncio
    async def test_create_converged_app_with_no_selftest(self) -> None:
        """Test create_converged_app with run_selftest=False."""
        app = create_converged_app(run_selftest=False)
        assert isinstance(app, FastAPI)

    @pytest.mark.asyncio
    async def test_main_function_smoke(self) -> None:
        """Test main function executes without raising exceptions."""
        # Mock the sys.argv to avoid actual argument parsing
        with patch('sys.argv', ['converged_headless.py']):
            with patch('backend.core.env_bootstrap.load_env_once') as mock_load_env:
                with patch('uvicorn.run') as mock_run:
                    # Mock return values to avoid actual execution
                    mock_load_env.return_value = True
                    mock_run.return_value = None
                    
                    # This should not raise an exception
                    try:
                        result = main()
                        assert result == 0  # Should return 0 on success
                    except Exception as e:
                        # If it raises, we want to know what it is
                        pytest.fail(f"main() raised {type(e).__name__}: {e}")

    @pytest.mark.asyncio
    async def test_main_function_with_env_vars(self) -> None:
        """Test main function with environment variables set."""
        # Set up environment variables like the real main would
        original_env = {}
        for key in ['JARVIS_SERVICE_MODE', 'JARVIS_ENABLE_SLIM_MODE', 'OUROBOROS_BATTLE_HEADLESS']:
            original_env[key] = os.environ.get(key)
            os.environ[key] = 'test'
        
        try:
            with patch('sys.argv', ['converged_headless.py']):
                with patch('backend.core.env_bootstrap.load_env_once') as mock_load_env:
                    with patch('uvicorn.run') as mock_run:
                        mock_load_env.return_value = True
                        mock_run.return_value = None
                        
                        result = main()
                        assert result == 0
        finally:
            # Restore original environment
            for key, value in original_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value