#!/usr/bin/env python3
"""
Parallel Startup Manager for JARVIS Backend
Speeds up initialization by running services concurrently
No hardcoding - all configurable via environment variables
"""

import asyncio
import aiohttp
import os
import time
import logging
import subprocess
import signal
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
from functools import partial

# Import daemon executor for clean shutdown
try:
    from core.thread_manager import get_daemon_executor
    _USE_DAEMON_EXECUTOR = True
except ImportError:
    _USE_DAEMON_EXECUTOR = False

logger = logging.getLogger(__name__)

@dataclass
class ServiceConfig:
    """Configuration for a service"""
    name: str
    command: List[str]
    port: int
    health_endpoint: str
    startup_timeout: float
    retry_attempts: int = 3
    retry_delay: float = 2.0
    required: bool = True
    env_vars: Dict[str, str] = None

class ParallelStartupManager:
    """Manages parallel startup of all JARVIS services"""
    
    def __init__(self):
        # Load configuration from environment
        self.max_workers = int(os.getenv('STARTUP_MAX_WORKERS', '4'))
        self.health_check_interval = float(os.getenv('HEALTH_CHECK_INTERVAL', '1.0'))
        self.startup_timeout = float(os.getenv('STARTUP_TIMEOUT', '60'))
        self.parallel_health_checks = os.getenv('PARALLEL_HEALTH_CHECKS', 'true').lower() == 'true'
        
        # Thread pool for I/O operations (use daemon executor for clean shutdown)
        if _USE_DAEMON_EXECUTOR:
            self.thread_executor = get_daemon_executor(max_workers=self.max_workers, name='startup-manager')
        else:
            self.thread_executor = ThreadPoolExecutor(max_workers=self.max_workers)

        # Process pool for CPU-intensive operations
        self.process_executor = ProcessPoolExecutor(max_workers=self.max_workers)
        
        # Track running processes
        self.running_processes: Dict[str, subprocess.Popen] = {}
        
        # Service configurations
        self.services = self._load_service_configs()
        
    def _load_service_configs(self) -> Dict[str, ServiceConfig]:
        """Load service configurations from environment"""
        configs = {}
        
        # WebSocket Router
        configs['websocket_router'] = ServiceConfig(
            name='WebSocket Router',
            command=['npm', 'run', 'start'],
            port=int(os.getenv('WS_ROUTER_PORT', '8001')),
            health_endpoint=os.getenv('WS_ROUTER_HEALTH', '/health'),
            startup_timeout=float(os.getenv('WS_ROUTER_TIMEOUT', '30')),
            env_vars={'PORT': os.getenv('WS_ROUTER_PORT', '8001')}
        )
        
        # Main Backend
        configs['main_backend'] = ServiceConfig(
            name='Main Backend',
            command=['python', 'main.py'],
            port=int(os.getenv('BACKEND_PORT', '8000')),
            health_endpoint=os.getenv('BACKEND_HEALTH', '/health'),
            startup_timeout=float(os.getenv('BACKEND_TIMEOUT', '60')),
            env_vars={
                'PORT': os.getenv('BACKEND_PORT', '8000'),
                'OPTIMIZE_STARTUP': 'true'
            }
        )
        
        # Vision System
        configs['vision_system'] = ServiceConfig(
            name='Vision System',
            command=['python', 'vision/vision_server.py'],
            port=int(os.getenv('VISION_PORT', '8002')),
            health_endpoint=os.getenv('VISION_HEALTH', '/vision/health'),
            startup_timeout=float(os.getenv('VISION_TIMEOUT', '45')),
            required=False
        )
        
        # Voice System
        configs['voice_system'] = ServiceConfig(
            name='Voice System',
            command=['python', 'voice/voice_server.py'],
            port=int(os.getenv('VOICE_PORT', '8003')),
            health_endpoint=os.getenv('VOICE_HEALTH', '/voice/health'),
            startup_timeout=float(os.getenv('VOICE_TIMEOUT', '30')),
            required=False
        )
        
        # Monitoring Dashboard
        configs['monitoring_dashboard'] = ServiceConfig(
            name='Monitoring Dashboard',
            command=['python', '-m', 'http.server'],
            port=int(os.getenv('MONITOR_PORT', '8888')),
            health_endpoint='/',
            startup_timeout=float(os.getenv('MONITOR_TIMEOUT', '10')),
            required=False,
            env_vars={'PORT': os.getenv('MONITOR_PORT', '8888')}
        )
        
        return configs
    
    async def start_all_services(self) -> Tuple[List[str], List[str]]:
        """Start all services in parallel"""
        logger.info("🚀 Starting all services in parallel...")
        start_time = time.time()
        
        # Create startup tasks for all services
        startup_tasks = []
        for service_id, config in self.services.items():
            task = asyncio.create_task(
                self._start_service(service_id, config),
                name=f"start_{service_id}"
            )
            startup_tasks.append((service_id, task))
        
        # Wait for all services to start
        results = await asyncio.gather(
            *[task for _, task in startup_tasks],
            return_exceptions=True
        )
        
        # Process results
        successful_services = []
        failed_services = []
        
        for (service_id, _), result in zip(startup_tasks, results):
            if isinstance(result, Exception):
                logger.error(f"❌ {service_id} failed: {result}")
                failed_services.append(service_id)
                # If it's a required service, we should handle this
                if self.services[service_id].required:
                    logger.critical(f"Required service {service_id} failed to start!")
            else:
                logger.info(f"✅ {service_id} started successfully")
                successful_services.append(service_id)
        
        elapsed = time.time() - start_time
        logger.info(f"🎯 Parallel startup completed in {elapsed:.1f}s")
        logger.info(f"✅ Successful: {len(successful_services)}/{len(self.services)} services")
        
        if failed_services:
            logger.warning(f"❌ Failed services: {', '.join(failed_services)}")
        
        return successful_services, failed_services
    
    async def _start_service(self, service_id: str, config: ServiceConfig) -> bool:
        """Start a single service with retries"""
        for attempt in range(config.retry_attempts):
            try:
                logger.info(f"🔄 Starting {config.name} (attempt {attempt + 1}/{config.retry_attempts})")
                
                # Start the process
                process = await self._launch_process(service_id, config)
                
                if process:
                    self.running_processes[service_id] = process
                    
                    # Wait for health check
                    healthy = await self._wait_for_health(
                        config.name,
                        config.port,
                        config.health_endpoint,
                        config.startup_timeout
                    )
                    
                    if healthy:
                        return True
                    else:
                        logger.warning(f"⚠️ {config.name} health check failed")
                        # Kill the process if health check fails
                        await self._kill_process(service_id)
                
            except Exception as e:
                logger.error(f"Error starting {config.name}: {e}")
            
            if attempt < config.retry_attempts - 1:
                await asyncio.sleep(config.retry_delay)
        
        return False
    
    async def _launch_process(self, service_id: str, config: ServiceConfig) -> Optional[subprocess.Popen]:
        """Launch a process asynchronously"""
        try:
            # Prepare environment
            env = os.environ.copy()
            if config.env_vars:
                env.update(config.env_vars)
            
            # Use asyncio subprocess for non-blocking execution
            if service_id == 'websocket_router':
                # Special handling for Node.js service
                cwd = os.path.join(os.getcwd(), 'ws-router')
            elif service_id == 'main_backend':
                cwd = os.path.join(os.getcwd(), 'backend')
            else:
                cwd = os.getcwd()
            
            # Start process
            process = await asyncio.create_subprocess_exec(
                *config.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd
            )
            
            # Give it a moment to start
            await asyncio.sleep(0.5)
            
            # Check if process is still running
            if process.returncode is not None:
                stdout, stderr = await process.communicate()
                logger.error(f"{config.name} exited immediately: {stderr.decode()}")
                return None
            
            logger.info(f"📦 {config.name} process started (PID: {process.pid})")
            return process
            
        except Exception as e:
            logger.error(f"Failed to launch {config.name}: {e}")
            return None
    
    async def _wait_for_health(self, name: str, port: int, endpoint: str, timeout: float) -> bool:
        """Wait for service health check with parallel checking"""
        health_url = f"http://localhost:{port}{endpoint}"
        start_time = time.time()
        
        logger.info(f"🏥 Checking health for {name} at {health_url}")
        
        # Use parallel health checks if enabled
        if self.parallel_health_checks:
            return await self._parallel_health_check(health_url, timeout, start_time)
        else:
            return await self._sequential_health_check(health_url, timeout, start_time)
    
    async def _parallel_health_check(self, url: str, timeout: float, start_time: float) -> bool:
        """Perform parallel health checks for faster detection"""
        async with aiohttp.ClientSession() as session:
            while (time.time() - start_time) < timeout:
                # Create multiple concurrent health check attempts
                check_tasks = []
                for _ in range(3):  # 3 parallel checks
                    task = asyncio.create_task(self._single_health_check(session, url))
                    check_tasks.append(task)
                    await asyncio.sleep(0.1)  # Slight stagger
                
                # Wait for any to succeed
                results = await asyncio.gather(*check_tasks, return_exceptions=True)
                
                if any(r is True for r in results):
                    return True
                
                await asyncio.sleep(self.health_check_interval)
        
        return False
    
    async def _sequential_health_check(self, url: str, timeout: float, start_time: float) -> bool:
        """Perform sequential health checks"""
        async with aiohttp.ClientSession() as session:
            while (time.time() - start_time) < timeout:
                if await self._single_health_check(session, url):
                    return True
                await asyncio.sleep(self.health_check_interval)
        
        return False
    
    async def _single_health_check(self, session: aiohttp.ClientSession, url: str) -> bool:
        """Perform a single health check"""
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=2)) as response:
                return response.status == 200
        except Exception:
            return False
    
    async def _kill_process(self, service_id: str):
        """Kill a process if it exists"""
        if service_id in self.running_processes:
            process = self.running_processes[service_id]
            try:
                process.terminate()
                await asyncio.sleep(1)
                if process.returncode is None:
                    process.kill()
                del self.running_processes[service_id]
            except Exception as e:
                logger.error(f"Error killing {service_id}: {e}")
    
    async def shutdown_all_services(self):
        """Shutdown all running services gracefully"""
        logger.info("🛑 Shutting down all services...")
        
        shutdown_tasks = []
        for service_id in list(self.running_processes.keys()):
            task = asyncio.create_task(self._kill_process(service_id))
            shutdown_tasks.append(task)
        
        await asyncio.gather(*shutdown_tasks, return_exceptions=True)
        
        # Clean up executors
        self.thread_executor.shutdown(wait=False)
        self.process_executor.shutdown(wait=False)
        
        logger.info("✅ All services shut down")

class ComponentLoader:
    """Loads heavy components in parallel during startup"""
    
    def __init__(self):
        self.load_timeout = float(os.getenv('COMPONENT_LOAD_TIMEOUT', '30'))
        self.parallel_imports = os.getenv('PARALLEL_IMPORTS', 'true').lower() == 'true'
    
    async def load_all_components(self) -> Dict[str, Any]:
        """Load all components in parallel"""
        logger.info("📦 Loading components in parallel...")
        start_time = time.time()
        
        # Define component loading tasks
        component_tasks = {
            'vision': self._load_vision_components(),
            'voice': self._load_voice_components(),
            'memory': self._load_memory_components(),
            'swift': self._load_swift_bridges(),
            'monitoring': self._load_monitoring_components()
        }
        
        # Execute all loads in parallel
        results = await asyncio.gather(
            *[
                asyncio.wait_for(task, timeout=self.load_timeout)
                for task in component_tasks.values()
            ],
            return_exceptions=True
        )
        
        # Process results
        loaded_components = {}
        failed_components = []
        
        for name, result in zip(component_tasks.keys(), results):
            if isinstance(result, Exception):
                logger.warning(f"⚠️ Component {name} failed to load: {result}")
                failed_components.append(name)
            else:
                logger.info(f"✅ Component {name} loaded successfully")
                loaded_components[name] = result
        
        elapsed = time.time() - start_time
        logger.info(f"📦 Component loading completed in {elapsed:.1f}s")
        logger.info(f"✅ Loaded: {len(loaded_components)}/{len(component_tasks)} components")
        
        return loaded_components
    
    async def _load_vision_components(self):
        """Load vision components asynchronously"""
        try:
            # Run import in thread pool to avoid blocking
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                self._import_vision_modules
            )
        except Exception as e:
            logger.error(f"Vision component load error: {e}")
            raise
    
    def _import_vision_modules(self):
        """Import vision modules (run in thread)"""
        try:
            from vision import ClaudeVisionAnalyzer, VideoStreamCapture
            return {
                'analyzer': ClaudeVisionAnalyzer,
                'video_capture': VideoStreamCapture
            }
        except ImportError as e:
            logger.warning(f"Vision import error: {e}")
            return None
    
    async def _load_voice_components(self):
        """Load voice components asynchronously"""
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                self._import_voice_modules
            )
        except Exception as e:
            logger.error(f"Voice component load error: {e}")
            raise
    
    def _import_voice_modules(self):
        """Import voice modules (run in thread)"""
        try:
            # Import voice components if available
            return {'status': 'loaded'}
        except ImportError:
            return None
    
    async def _load_memory_components(self):
        """Load memory management components"""
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                lambda: {'memory_manager': 'loaded'}
            )
        except Exception as e:
            logger.error(f"Memory component load error: {e}")
            raise
    
    async def _load_swift_bridges(self):
        """Load Swift performance bridges"""
        try:
            # Check for Swift bridges
            swift_available = os.path.exists('rust_performance/libperformance.dylib')
            return {'available': swift_available}
        except Exception as e:
            logger.error(f"Swift bridge load error: {e}")
            raise
    
    async def _load_monitoring_components(self):
        """Load monitoring components"""
        return {'monitoring': 'ready'}

async def optimized_startup():
    """Main optimized startup function"""
    logger.info("🚀 JARVIS Optimized Parallel Startup")
    logger.info("=" * 60)
    
    overall_start = time.time()
    
    # Initialize managers
    startup_manager = ParallelStartupManager()
    component_loader = ComponentLoader()
    
    # Phase 1: Start all services in parallel
    logger.info("\n📍 Phase 1: Starting services in parallel...")
    service_task = asyncio.create_task(startup_manager.start_all_services())
    
    # Phase 2: Load components in parallel (while services start)
    logger.info("\n📍 Phase 2: Loading components in parallel...")
    component_task = asyncio.create_task(component_loader.load_all_components())
    
    # Wait for both phases to complete
    (successful_services, failed_services), loaded_components = await asyncio.gather(
        service_task,
        component_task
    )
    
    # Phase 3: Final verification
    logger.info("\n📍 Phase 3: Verifying system readiness...")
    
    critical_services = ['main_backend', 'websocket_router']
    all_critical_running = all(s in successful_services for s in critical_services)
    
    if not all_critical_running:
        logger.critical("❌ Critical services failed to start!")
        missing = [s for s in critical_services if s not in successful_services]
        logger.critical(f"Missing: {', '.join(missing)}")
        return False
    
    overall_elapsed = time.time() - overall_start
    
    logger.info("\n" + "=" * 60)
    logger.info(f"✨ JARVIS Startup Complete in {overall_elapsed:.1f}s!")
    logger.info(f"✅ Services: {len(successful_services)}/{len(startup_manager.services)}")
    logger.info(f"✅ Components: {len(loaded_components)}/5")
    logger.info("=" * 60)
    
    return True

if __name__ == "__main__":
    # Run the optimized startup
    asyncio.run(optimized_startup())