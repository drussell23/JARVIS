"""
Fast startup wrapper for JARVIS vision system.
Defers Rust building to background and uses Python fallbacks initially.
"""

import os
import asyncio
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

class FastStartupWrapper:
    """Wrapper to enable fast startup by deferring Rust builds."""
    
    def __init__(self):
        self.rust_build_task: Optional[asyncio.Task] = None
        self.rust_available = False
        self._building = False
        
    async def initialize_with_fallbacks(self) -> Dict[str, Any]:
        """Initialize vision system with Python fallbacks for fast startup."""
        logger.info("🚀 Fast startup: Using Python implementations initially")
        
        # Set environment to skip Rust checks during startup
        os.environ['JARVIS_SKIP_RUST_BUILD'] = 'true'
        os.environ['JARVIS_USE_PYTHON_FALLBACKS'] = 'true'
        
        config = {
            'available': True,
            'implementation': 'python',
            'performance_boost': {},
            'components': {
                'bloom_filter': 'python',
                'sliding_window': 'python',
                'memory_pool': 'python',
                'image_processor': 'python'
            }
        }
        
        # Schedule Rust build in background after startup
        if not os.environ.get('JARVIS_DISABLE_RUST_COMPLETELY', '').lower() == 'true':
            self.rust_build_task = asyncio.create_task(self._build_rust_in_background())
        
        return config
        
    async def _build_rust_in_background(self):
        """Build Rust components in background after startup."""
        # Wait a bit for system to stabilize
        await asyncio.sleep(10)
        
        if self._building:
            return
            
        self._building = True
        logger.info("🔧 Starting background Rust component build...")
        
        try:
            # Import the self-healer
            from .rust_self_healer import RustSelfHealer
            
            # Create a new instance with faster settings
            healer = RustSelfHealer(
                check_interval=600,  # 10 minutes between checks
                max_retries=1  # Only try once in background
            )
            
            # Try to build once
            success = await healer.diagnose_and_fix()
            
            if success:
                logger.info("✅ Rust components built successfully in background!")
                self.rust_available = True
                
                # Notify the dynamic loader to upgrade components
                try:
                    from .dynamic_component_loader import get_component_loader
                    loader = get_component_loader()
                    if loader and loader._running:
                        await loader._check_all_components()
                except Exception:
                    pass
            else:
                logger.info("⚠️ Rust build failed in background, continuing with Python implementations")
                
        except Exception as e:
            logger.error(f"Background Rust build error: {e}")
        finally:
            self._building = False
            
    def get_status(self) -> Dict[str, Any]:
        """Get current status of Rust components."""
        return {
            'rust_available': self.rust_available,
            'building': self._building,
            'build_task_done': self.rust_build_task.done() if self.rust_build_task else None
        }

# Global instance
_fast_wrapper: Optional[FastStartupWrapper] = None

def get_fast_startup_wrapper() -> FastStartupWrapper:
    """Get the global fast startup wrapper."""
    global _fast_wrapper
    if _fast_wrapper is None:
        _fast_wrapper = FastStartupWrapper()
    return _fast_wrapper

async def initialize_rust_acceleration_fast() -> Dict[str, Any]:
    """
    Fast initialization that uses Python fallbacks and builds Rust in background.
    This replaces the slow initialize_rust_acceleration during startup.
    """
    wrapper = get_fast_startup_wrapper()
    return await wrapper.initialize_with_fallbacks()