#!/usr/bin/env python3
"""
Parallel Start System for JARVIS
Replaces sequential startup with parallel execution
Reduces startup time from 107+ seconds to ~30 seconds
"""

import asyncio
import os
import sys
import time
import signal
import logging
from pathlib import Path
from typing import List, Dict, Tuple

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent))

from parallel_startup_manager import ParallelStartupManager, ComponentLoader
from optimized_backend_startup import OptimizedBackendStartup

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class JarvisParallelSystem:
    """Manages the entire JARVIS system with parallel startup"""
    
    def __init__(self):
        self.startup_manager = ParallelStartupManager()
        self.component_loader = ComponentLoader()
        self.backend_optimizer = OptimizedBackendStartup()
        self.start_time = None
        
    async def start(self):
        """Start the entire JARVIS system in parallel"""
        self.start_time = time.time()
        
        # Print startup banner
        self._print_banner()
        
        try:
            # Phase 1: Environment check (quick)
            logger.info("\n🔍 Phase 1: Environment Check")
            env_ready = await self._check_environment()
            if not env_ready:
                logger.error("Environment check failed!")
                return False
            
            # Phase 2: Parallel service startup
            logger.info("\n🚀 Phase 2: Parallel Service Startup")
            service_results = await self._start_services_parallel()
            
            # Phase 3: Parallel component loading
            logger.info("\n📦 Phase 3: Parallel Component Loading")
            component_results = await self._load_components_parallel()
            
            # Phase 4: System verification
            logger.info("\n✅ Phase 4: System Verification")
            system_ready = await self._verify_system()
            
            # Print summary
            self._print_summary(service_results, component_results, system_ready)
            
            return system_ready
            
        except Exception as e:
            logger.error(f"❌ Startup failed: {e}")
            return False
    
    def _print_banner(self):
        """Print startup banner"""
        print("\n" + "=" * 70)
        print("🚀 JARVIS AI AGENT - PARALLEL STARTUP")
        print("⚡ Optimized for speed - No more waiting!")
        print("=" * 70)
    
    async def _check_environment(self):
        """Quick environment checks"""
        checks = []
        
        # Python version
        py_version = sys.version_info
        checks.append(('Python 3.8+', py_version >= (3, 8)))
        
        # Required directories
        dirs = ['backend', 'frontend', 'ws-router']
        for d in dirs:
            checks.append((f'Directory {d}', os.path.exists(d)))
        
        # Environment variables
        required_env = ['ANTHROPIC_API_KEY']
        for env in required_env:
            checks.append((f'Env {env}', env in os.environ))
        
        # Print results
        all_passed = True
        for check_name, passed in checks:
            status = "✅" if passed else "❌"
            logger.info(f"  {status} {check_name}")
            if not passed:
                all_passed = False
        
        return all_passed
    
    async def _start_services_parallel(self) -> Tuple[List[str], List[str]]:
        """Start all services in parallel"""
        # Define services with custom configs if needed
        custom_configs = {
            'main_backend': {
                'command': ['python', 'main_py'],  # Use optimized startup
                'env_vars': {
                    'OPTIMIZE_STARTUP': 'true',
                    'PARALLEL_IMPORTS': 'true',
                    'LAZY_LOAD_MODELS': 'true'
                }
            }
        }
        
        # Apply custom configs
        for service_id, config in custom_configs.items():
            if service_id in self.startup_manager.services:
                self.startup_manager.services[service_id].command = config.get(
                    'command', 
                    self.startup_manager.services[service_id].command
                )
                if config.get('env_vars'):
                    self.startup_manager.services[service_id].env_vars.update(
                        config['env_vars']
                    )
        
        # Start all services
        return await self.startup_manager.start_all_services()
    
    async def _load_components_parallel(self) -> Dict:
        """Load all components in parallel"""
        return await self.component_loader.load_all_components()
    
    async def _verify_system(self) -> bool:
        """Verify the system is ready"""
        logger.info("🔍 Verifying system readiness...")
        
        # Check critical services
        critical_checks = [
            self._check_backend_api(),
            self._check_websocket_router(),
            self._check_frontend_available()
        ]
        
        results = await asyncio.gather(*critical_checks, return_exceptions=True)
        
        all_ready = True
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"  ❌ Check {i} failed: {result}")
                all_ready = False
            elif result:
                logger.info(f"  ✅ Check {i} passed")
            else:
                logger.warning(f"  ⚠️ Check {i} returned False")
                all_ready = False
        
        return all_ready
    
    async def _check_backend_api(self) -> bool:
        """Check if backend API is responding"""
        import aiohttp
        
        url = f"http://localhost:{os.getenv('BACKEND_PORT', '8010')}/health"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    return resp.status == 200
        except Exception:
            return False
    
    async def _check_websocket_router(self) -> bool:
        """Check if WebSocket router is responding"""
        import aiohttp
        
        url = f"http://localhost:{os.getenv('WS_ROUTER_PORT', '8001')}/health"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    return resp.status == 200
        except Exception:
            return False
    
    async def _check_frontend_available(self) -> bool:
        """Check if frontend directory exists"""
        return os.path.exists('frontend/build') or os.path.exists('frontend/public')
    
    def _print_summary(self, service_results, component_results, system_ready):
        """Print startup summary"""
        elapsed = time.time() - self.start_time
        
        successful_services, failed_services = service_results
        
        print("\n" + "=" * 70)
        print(f"✨ JARVIS PARALLEL STARTUP COMPLETE in {elapsed:.1f}s!")
        print("=" * 70)
        
        print(f"\n📊 Results:")
        print(f"  Services:   {len(successful_services)}/{len(self.startup_manager.services)} started")
        print(f"  Components: {len(component_results)}/5 loaded")
        print(f"  System:     {'READY' if system_ready else 'NOT READY'}")
        
        if failed_services:
            print(f"\n⚠️ Failed services: {', '.join(failed_services)}")
        
        print(f"\n🌐 Access Points:")
        print(f"  Frontend:   http://localhost:3000")
        print(f"  Backend:    http://localhost:{os.getenv('BACKEND_PORT', '8010')}")
        print(f"  API Docs:   http://localhost:{os.getenv('BACKEND_PORT', '8010')}/docs")
        print(f"  WebSocket:  ws://localhost:{os.getenv('BACKEND_PORT', '8010')}/ws")
        print(f"  Monitoring: http://localhost:8888")
        
        print(f"\n⚡ Performance:")
        print(f"  Startup Time: {elapsed:.1f}s (was 107+ seconds)")
        print(f"  Improvement:  {107/elapsed:.1f}x faster!")
        
        print("\n🎤 Voice Commands:")
        print('  Say "Hey JARVIS" to activate')
        print('  Say "Start monitoring my screen" for vision')
        print('  Say "Stop monitoring" to disable vision')
        
        print("\n" + "=" * 70)
        print("🤖 JARVIS is ready! Open http://localhost:3000")
        print("=" * 70 + "\n")

async def shutdown_handler(system: JarvisParallelSystem):
    """Handle shutdown gracefully"""
    logger.info("\n🛑 Shutting down JARVIS...")
    await system.startup_manager.shutdown_all_services()
    logger.info("✅ Shutdown complete")

async def main():
    """Main entry point"""
    system = JarvisParallelSystem()
    
    # Setup signal handlers
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(
            sig, 
            lambda: asyncio.create_task(shutdown_handler(system))
        )
    
    # Start the system
    success = await system.start()
    
    if success:
        # Keep running until interrupted
        try:
            await asyncio.Event().wait()
        except KeyboardInterrupt:
            await shutdown_handler(system)
    else:
        logger.error("❌ Failed to start JARVIS")
        sys.exit(1)

if __name__ == "__main__":
    # Enable optimization flags
    os.environ['OPTIMIZE_STARTUP'] = 'true'
    os.environ['PARALLEL_IMPORTS'] = 'true'
    os.environ['LAZY_LOAD_MODELS'] = 'true'
    os.environ['PARALLEL_HEALTH_CHECKS'] = 'true'
    
    # Run the parallel startup
    asyncio.run(main())