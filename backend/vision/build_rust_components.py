#!/usr/bin/env python3
"""
Build and verify Rust components for JARVIS vision system.
This script handles the complete Rust integration setup with parallel compilation.
"""

import os
import sys
import subprocess
import logging
import json
from pathlib import Path
import platform
import shutil
import asyncio
import multiprocessing
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
from typing import Dict, List, Optional, Tuple, Any
import time
import psutil
import toml
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class RustBuilder:
    def __init__(self, config_path: Optional[Path] = None):
        self.vision_dir = Path(__file__).parent
        self.rust_core_dir = self.vision_dir / "jarvis-rust-core"
        self.python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
        self.is_macos = platform.system() == "Darwin"
        self.is_m1 = self.is_macos and platform.machine() == "arm64"
        
        # Dynamic configuration
        self.config = self._load_config(config_path)
        
        # System resources
        self.cpu_count = multiprocessing.cpu_count()
        self.memory_gb = psutil.virtual_memory().total / (1024**3)
        
        # Parallel execution settings
        self.max_workers = self.config.get('build', {}).get('max_workers', min(self.cpu_count, 8))
        self.parallel_enabled = self.config.get('build', {}).get('parallel', True)
        
        # Build optimizations
        self.optimization_level = self.config.get('optimization', {}).get('level', 3)
        self.lto_enabled = self.config.get('optimization', {}).get('lto', True)
        self.native_cpu = self.config.get('optimization', {}).get('native_cpu', True)
        
        # Component list - dynamically loaded
        self.components = self._discover_components()
        
    def _load_config(self, config_path: Optional[Path] = None) -> Dict[str, Any]:
        """Load dynamic configuration from file or defaults."""
        default_config = {
            'build': {
                'parallel': True,
                'max_workers': None,  # Auto-detect
                'timeout': 600,  # 10 minutes
                'retry_count': 3,
                'cache_enabled': True
            },
            'optimization': {
                'level': 3,
                'lto': True,
                'native_cpu': True,
                'strip': True,
                'codegen_units': 1
            },
            'features': {
                'python-bindings': True,
                'simd': True,
                'metal': self.is_macos,
                'parallel-processing': True
            },
            'memory': {
                'limit_gb': max(2, self.memory_gb * 0.5),  # Use up to 50% of RAM
                'cargo_build_jobs': None  # Auto-detect
            }
        }
        
        if config_path and config_path.exists():
            try:
                if config_path.suffix == '.json':
                    with open(config_path) as f:
                        user_config = json.load(f)
                elif config_path.suffix == '.toml':
                    with open(config_path) as f:
                        user_config = toml.load(f)
                else:
                    logger.warning(f"Unknown config format: {config_path}")
                    return default_config
                    
                # Deep merge configs
                return self._merge_configs(default_config, user_config)
            except Exception as e:
                logger.warning(f"Failed to load config: {e}")
                
        return default_config
        
    def _merge_configs(self, default: Dict, user: Dict) -> Dict:
        """Deep merge user config into default config."""
        result = default.copy()
        for key, value in user.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._merge_configs(result[key], value)
            else:
                result[key] = value
        return result
        
    def _discover_components(self) -> List[Dict[str, Any]]:
        """Dynamically discover Rust components to build."""
        components = []
        
        # Check Cargo.toml for workspace members
        cargo_toml = self.rust_core_dir / "Cargo.toml"
        if cargo_toml.exists():
            try:
                with open(cargo_toml) as f:
                    cargo_data = toml.load(f)
                    
                # Check for workspace
                if 'workspace' in cargo_data and 'members' in cargo_data['workspace']:
                    for member in cargo_data['workspace']['members']:
                        components.append({
                            'name': member,
                            'path': self.rust_core_dir / member,
                            'type': 'workspace_member'
                        })
                        
                # Add main crate
                if 'package' in cargo_data:
                    components.append({
                        'name': cargo_data['package']['name'],
                        'path': self.rust_core_dir,
                        'type': 'main',
                        'features': self._get_enabled_features()
                    })
            except Exception as e:
                logger.warning(f"Failed to parse Cargo.toml: {e}")
                
        # Fallback to directory scanning
        if not components:
            components = [{
                'name': 'jarvis-rust-core',
                'path': self.rust_core_dir,
                'type': 'main',
                'features': self._get_enabled_features()
            }]
            
        return components
        
    def _get_enabled_features(self) -> List[str]:
        """Get list of enabled features from config."""
        features = []
        feature_config = self.config.get('features', {})
        
        for feature, enabled in feature_config.items():
            if enabled:
                features.append(feature)
                
        return features
        
    def check_prerequisites(self) -> bool:
        """Check if all prerequisites are installed."""
        logger.info("Checking prerequisites...")
        
        # Check Rust
        try:
            result = subprocess.run(["rustc", "--version"], capture_output=True, text=True)
            if result.returncode == 0:
                logger.info(f"✓ Rust installed: {result.stdout.strip()}")
            else:
                logger.error("✗ Rust not found. Install from https://rustup.rs/")
                return False
        except FileNotFoundError:
            logger.error("✗ Rust not found. Install from https://rustup.rs/")
            return False
            
        # Check Cargo
        try:
            result = subprocess.run(["cargo", "--version"], capture_output=True, text=True)
            if result.returncode == 0:
                logger.info(f"✓ Cargo installed: {result.stdout.strip()}")
            else:
                logger.error("✗ Cargo not found")
                return False
        except FileNotFoundError:
            logger.error("✗ Cargo not found")
            return False
            
        # Check maturin
        try:
            result = subprocess.run(["maturin", "--version"], capture_output=True, text=True)
            if result.returncode == 0:
                logger.info(f"✓ Maturin installed: {result.stdout.strip()}")
            else:
                logger.info("Maturin not found, installing...")
                subprocess.run([sys.executable, "-m", "pip", "install", "maturin"], check=True)
                logger.info("✓ Maturin installed")
        except FileNotFoundError:
            logger.info("Maturin not found, installing...")
            subprocess.run([sys.executable, "-m", "pip", "install", "maturin"], check=True)
            logger.info("✓ Maturin installed")
            
        return True
        
    def setup_rust_toolchain(self):
        """Setup Rust toolchain for the target platform."""
        logger.info("Setting up Rust toolchain...")
        
        if self.is_m1:
            # Set up for M1 Mac
            logger.info("Configuring for Apple Silicon (M1)...")
            subprocess.run(["rustup", "target", "add", "aarch64-apple-darwin"], check=True)
            
            # Create .cargo/config.toml for optimizations
            cargo_config_dir = self.rust_core_dir / ".cargo"
            cargo_config_dir.mkdir(exist_ok=True)
            
            config_content = """
[target.aarch64-apple-darwin]
rustflags = ["-C", "target-cpu=native"]

[build]
target = "aarch64-apple-darwin"

[profile.release]
opt-level = 3
lto = true
codegen-units = 1
strip = true
"""
            
            config_file = cargo_config_dir / "config.toml"
            config_file.write_text(config_content)
            logger.info("✓ Created optimized cargo config for M1")
            
    async def build_rust_library(self) -> bool:
        """Build the Rust library with parallel compilation and optimizations."""
        logger.info(f"Building Rust library with {self.max_workers} parallel workers...")
        
        if not self.rust_core_dir.exists():
            logger.error(f"Rust core directory not found: {self.rust_core_dir}")
            return False
            
        # Clean previous builds if needed
        if self.config.get('build', {}).get('clean_build', False):
            await self._clean_build_async()
            
        # Build all components in parallel
        if self.parallel_enabled and len(self.components) > 1:
            results = await self._build_components_parallel()
        else:
            results = await self._build_components_sequential()
            
        # Check results
        success = all(results.values())
        if success:
            logger.info("✅ All components built successfully")
        else:
            failed = [name for name, result in results.items() if not result]
            logger.error(f"❌ Failed to build: {', '.join(failed)}")
            
        return success
        
    async def _clean_build_async(self):
        """Clean build artifacts asynchronously."""
        logger.info("Cleaning previous builds...")
        
        async def clean_target(path: Path):
            target_dir = path / "target"
            if target_dir.exists():
                await asyncio.to_thread(shutil.rmtree, target_dir)
                logger.info(f"✓ Cleaned {target_dir}")
                
        # Clean all component targets in parallel
        tasks = [clean_target(comp['path']) for comp in self.components]
        await asyncio.gather(*tasks, return_exceptions=True)
        
    async def _build_components_parallel(self) -> Dict[str, bool]:
        """Build components in parallel."""
        logger.info(f"Building {len(self.components)} components in parallel...")
        
        # Create semaphore to limit concurrent builds
        semaphore = asyncio.Semaphore(self.max_workers)
        
        async def build_with_semaphore(component):
            async with semaphore:
                return await self._build_single_component(component)
                
        # Build all components
        tasks = []
        for component in self.components:
            task = asyncio.create_task(build_with_semaphore(component))
            tasks.append((component['name'], task))
            
        # Wait for all builds
        results = {}
        for name, task in tasks:
            try:
                results[name] = await task
            except Exception as e:
                logger.error(f"Build failed for {name}: {e}")
                results[name] = False
                
        return results
        
    async def _build_components_sequential(self) -> Dict[str, bool]:
        """Build components sequentially."""
        results = {}
        for component in self.components:
            try:
                results[component['name']] = await self._build_single_component(component)
            except Exception as e:
                logger.error(f"Build failed for {component['name']}: {e}")
                results[component['name']] = False
        return results
        
    async def _build_single_component(self, component: Dict[str, Any]) -> bool:
        """Build a single Rust component."""
        name = component['name']
        path = component['path']
        
        logger.info(f"Building {name}...")
        
        # Prepare environment
        env = await self._prepare_build_env(component)
        
        # Build steps
        steps = []
        
        # Step 1: Cargo build
        if component.get('type') != 'python_only':
            steps.append(self._cargo_build_step(component, env))
            
        # Step 2: Maturin build (if it's the main package)
        if component.get('type') == 'main':
            steps.append(self._maturin_build_step(component, env))
            
        # Execute steps
        for step_name, step_func in steps:
            success = await step_func()
            if not success:
                logger.error(f"❌ {name}: {step_name} failed")
                return False
            logger.info(f"✅ {name}: {step_name} completed")
            
        return True
        
    async def _prepare_build_env(self, component: Dict[str, Any]) -> Dict[str, str]:
        """Prepare build environment with optimizations."""
        env = os.environ.copy()
        
        # Set target for M1
        if self.is_m1:
            env["CARGO_BUILD_TARGET"] = "aarch64-apple-darwin"
            
        # Set build parallelism
        if self.config['memory']['cargo_build_jobs']:
            env["CARGO_BUILD_JOBS"] = str(self.config['memory']['cargo_build_jobs'])
        else:
            # Use available CPU cores but leave some for system
            env["CARGO_BUILD_JOBS"] = str(max(1, self.cpu_count - 2))
            
        # Set Rust flags for optimization
        rust_flags = []
        if self.native_cpu:
            rust_flags.append("-C target-cpu=native")
        if self.optimization_level:
            rust_flags.append(f"-C opt-level={self.optimization_level}")
        if rust_flags:
            env["RUSTFLAGS"] = " ".join(rust_flags)
            
        # Memory limits
        if self.config['memory']['limit_gb']:
            # This is platform-specific, simplified example
            env["CARGO_BUILD_PIPELINING"] = "true"  # Reduce memory usage
            
        return env
        
    def _cargo_build_step(self, component: Dict[str, Any], env: Dict[str, str]) -> Tuple[str, Any]:
        """Create cargo build step."""
        async def build():
            cmd = ["cargo", "build", "--release"]
            
            # Add features
            if 'features' in component:
                cmd.extend(["--features", ",".join(component['features'])])
                
            # Add workspace flag if needed
            if component.get('type') == 'workspace_member':
                cmd.extend(["-p", component['name']])
                
            # Run build
            return await self._run_command_async(cmd, component['path'], env)
            
        return ("Cargo build", build)
        
    def _maturin_build_step(self, component: Dict[str, Any], env: Dict[str, str]) -> Tuple[str, Any]:
        """Create maturin build step."""
        async def build():
            cmd = ["maturin", "develop", "--release"]
            
            # Add features
            if 'features' in component:
                cmd.extend(["--features", ",".join(component['features'])])
                
            # Run build
            return await self._run_command_async(cmd, component['path'], env)
            
        return ("Maturin build", build)
        
    async def _run_command_async(self, cmd: List[str], cwd: Path, env: Dict[str, str]) -> bool:
        """Run a command asynchronously with timeout and retry."""
        timeout = self.config['build']['timeout']
        retry_count = self.config['build']['retry_count']
        
        for attempt in range(retry_count):
            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(cwd),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                
                # Wait with timeout
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout
                )
                
                if process.returncode == 0:
                    return True
                    
                # Log error
                logger.error(f"Command failed (attempt {attempt + 1}/{retry_count}): {' '.join(cmd)}")
                if stderr:
                    logger.error(f"Error: {stderr.decode('utf-8', errors='replace')}")
                    
                # Exponential backoff
                if attempt < retry_count - 1:
                    wait_time = 2 ** attempt
                    logger.info(f"Retrying in {wait_time} seconds...")
                    await asyncio.sleep(wait_time)
                    
            except asyncio.TimeoutError:
                logger.error(f"Command timed out after {timeout}s: {' '.join(cmd)}")
                if attempt < retry_count - 1:
                    logger.info("Retrying...")
                else:
                    return False
                    
            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                return False
                
        return False
        
    def verify_installation(self) -> bool:
        """Verify the Rust library is properly installed."""
        logger.info("Verifying Rust library installation...")
        
        # Change back to vision directory
        os.chdir(self.vision_dir)
        
        # Try to import the module
        try:
            import jarvis_rust_core
            logger.info("✓ Successfully imported jarvis_rust_core")
            
            # Check available functions
            available_components = []
            if hasattr(jarvis_rust_core, 'RustImageProcessor'):
                available_components.append('RustImageProcessor')
            if hasattr(jarvis_rust_core, 'RustAdvancedMemoryPool'):
                available_components.append('RustAdvancedMemoryPool')
            if hasattr(jarvis_rust_core, 'RustRuntimeManager'):
                available_components.append('RustRuntimeManager')
            if hasattr(jarvis_rust_core, 'RustQuantizedModel'):
                available_components.append('RustQuantizedModel')
                
            logger.info(f"Available components: {', '.join(available_components)}")
            
            # Test basic functionality
            logger.info("Testing basic functionality...")
            
            # Test memory pool
            try:
                pool = jarvis_rust_core.RustAdvancedMemoryPool()
                stats = pool.stats()
                logger.info(f"✓ Memory pool working: {stats}")
            except Exception as e:
                logger.error(f"✗ Memory pool test failed: {e}")
                return False
                
            # Test runtime manager
            try:
                runtime = jarvis_rust_core.RustRuntimeManager(
                    worker_threads=4,
                    enable_cpu_affinity=True
                )
                stats = runtime.stats()
                logger.info(f"✓ Runtime manager working: {stats}")
            except Exception as e:
                logger.error(f"✗ Runtime manager test failed: {e}")
                return False
                
            return True
            
        except ImportError as e:
            logger.error(f"✗ Failed to import jarvis_rust_core: {e}")
            return False
            
    def update_python_modules(self):
        """Update Python modules to use Rust acceleration."""
        logger.info("Updating Python modules for Rust integration...")
        
        # Update rust_bridge.py
        rust_bridge_file = self.vision_dir / "rust_bridge.py"
        if rust_bridge_file.exists():
            content = rust_bridge_file.read_text()
            
            # Update RUST_AVAILABLE check
            if "RUST_AVAILABLE = False" in content:
                logger.info("Updating rust_bridge.py...")
                content = content.replace(
                    "RUST_AVAILABLE = False",
                    "RUST_AVAILABLE = True  # Updated by build script"
                )
                rust_bridge_file.write_text(content)
                logger.info("✓ Updated rust_bridge.py")
                
        # Create configuration file
        config = {
            "rust_acceleration": {
                "enabled": True,
                "components": {
                    "memory_pool": True,
                    "runtime_manager": True,
                    "image_processor": True,
                    "bloom_filter": True,
                    "metal_acceleration": self.is_macos
                },
                "memory_pool_size_mb": 2048,
                "worker_threads": 8,
                "enable_cpu_affinity": True
            }
        }
        
        config_file = self.vision_dir / "rust_config.json"
        with open(config_file, 'w') as f:
            json.dump(config, f, indent=2)
        logger.info(f"✓ Created rust_config.json")
        
    def run_performance_test(self):
        """Run a quick performance test."""
        logger.info("Running performance test...")
        
        test_script = self.vision_dir / "rust_integration.py"
        if test_script.exists():
            result = subprocess.run(
                [sys.executable, str(test_script)],
                capture_output=True,
                text=True
            )
            
            if "Benchmarking Rust acceleration" in result.stdout:
                logger.info("Performance test output:")
                print(result.stdout)
            else:
                logger.warning("Performance test did not run as expected")
                
    def create_test_script(self):
        """Create a test script for developers."""
        test_content = '''
#!/usr/bin/env python3
"""Test Rust components integration."""

import sys
import time
import numpy as np

try:
    import jarvis_rust_core
    print("✓ Rust core imported successfully")
except ImportError as e:
    print(f"✗ Failed to import Rust core: {e}")
    sys.exit(1)

# Test memory pool
print("\nTesting memory pool...")
pool = jarvis_rust_core.RustAdvancedMemoryPool()
print(f"Pool stats: {pool.stats()}")

# Test runtime manager
print("\nTesting runtime manager...")
runtime = jarvis_rust_core.RustRuntimeManager(worker_threads=4)
print(f"Runtime stats: {runtime.stats()}")

# Test image processor
print("\nTesting image processor...")
processor = jarvis_rust_core.RustImageProcessor()
test_image = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)
start = time.time()
result = processor.process_numpy_image(test_image)
print(f"Image processing took: {(time.time() - start) * 1000:.2f}ms")

print("\n✓ All tests passed!")
'''
        
        test_file = self.vision_dir / "test_rust_components.py"
        test_file.write_text(test_content)
        test_file.chmod(0o700)
        logger.info(f"✓ Created test script: {test_file}")
        
    async def main(self):
        """Main build process with parallel execution."""
        start_time = time.time()
        
        # Print system info
        logger.info("=" * 70)
        logger.info("🦀 JARVIS Rust Component Builder - Parallel Edition")
        logger.info("=" * 70)
        logger.info(f"Platform: {platform.system()} {platform.machine()}")
        logger.info(f"Python: {sys.version}")
        logger.info(f"CPU Cores: {self.cpu_count}")
        logger.info(f"Memory: {self.memory_gb:.1f} GB")
        logger.info(f"Parallel Workers: {self.max_workers}")
        logger.info("=" * 70)
        
        # Show configuration
        logger.info("\n📋 Build Configuration:")
        logger.info(f"  • Parallel Build: {self.parallel_enabled}")
        logger.info(f"  • Optimization Level: {self.optimization_level}")
        logger.info(f"  • LTO: {self.lto_enabled}")
        logger.info(f"  • Native CPU: {self.native_cpu}")
        logger.info(f"  • Features: {', '.join(self._get_enabled_features())}")
        logger.info(f"  • Components: {len(self.components)}")
        
        # Step 1: Check prerequisites
        if not self.check_prerequisites():
            logger.error("Prerequisites check failed")
            return False
            
        # Step 2: Setup toolchain (can be done in parallel with other tasks)
        setup_task = asyncio.create_task(self._setup_toolchain_async())
        
        # Step 3: Pre-build tasks in parallel
        pre_build_tasks = [
            self._check_disk_space(),
            self._validate_components(),
            self._setup_cache_dir()
        ]
        
        pre_results = await asyncio.gather(*pre_build_tasks, return_exceptions=True)
        await setup_task
        
        # Step 4: Build library with parallel compilation
        build_success = await self.build_rust_library()
        if not build_success:
            logger.error("Build failed")
            return False
            
        # Step 5: Post-build tasks in parallel
        post_build_tasks = [
            self._verify_installation_async(),
            self._update_python_modules_async(),
            self._create_test_script_async(),
            self._generate_build_report()
        ]
        
        post_results = await asyncio.gather(*post_build_tasks, return_exceptions=True)
        
        # Check if verification passed
        if not post_results[0]:  # verify_installation result
            logger.error("Verification failed")
            return False
            
        # Step 6: Run performance test
        await self._run_performance_test_async()
        
        # Calculate build time
        build_time = time.time() - start_time
        
        # Success message
        logger.info("\n" + "=" * 70)
        logger.info("✅ Rust components build completed successfully!")
        logger.info(f"⏱️  Total build time: {build_time:.1f} seconds")
        logger.info("=" * 70)
        logger.info("\n📝 Next steps:")
        logger.info("  1. Run: python test_rust_components.py")
        logger.info("  2. Rust acceleration is now enabled automatically")
        logger.info("  3. Monitor performance in JARVIS dashboard")
        
        # Save build info
        await self._save_build_info(build_time)
        
        return True
        
    async def _setup_toolchain_async(self):
        """Setup Rust toolchain asynchronously."""
        await asyncio.to_thread(self.setup_rust_toolchain)
        
    async def _check_disk_space(self):
        """Check available disk space."""
        disk_usage = psutil.disk_usage(str(self.rust_core_dir))
        free_gb = disk_usage.free / (1024**3)
        
        if free_gb < 2:
            logger.warning(f"Low disk space: {free_gb:.1f} GB available")
        else:
            logger.info(f"✓ Disk space: {free_gb:.1f} GB available")
            
    async def _validate_components(self):
        """Validate all components before building."""
        logger.info("Validating components...")
        
        for component in self.components:
            cargo_toml = component['path'] / "Cargo.toml"
            if not cargo_toml.exists():
                logger.warning(f"Missing Cargo.toml for {component['name']}")
                
    async def _setup_cache_dir(self):
        """Setup build cache directory."""
        if self.config['build']['cache_enabled']:
            cache_dir = self.rust_core_dir / ".build_cache"
            cache_dir.mkdir(exist_ok=True)
            logger.info("✓ Build cache enabled")
            
    async def _verify_installation_async(self) -> bool:
        """Verify installation asynchronously."""
        return await asyncio.to_thread(self.verify_installation)
        
    async def _update_python_modules_async(self):
        """Update Python modules asynchronously."""
        await asyncio.to_thread(self.update_python_modules)
        
    async def _create_test_script_async(self):
        """Create test script asynchronously."""
        await asyncio.to_thread(self.create_test_script)
        
    async def _generate_build_report(self):
        """Generate a build report with statistics."""
        report = {
            'timestamp': datetime.now().isoformat(),
            'platform': platform.system(),
            'architecture': platform.machine(),
            'python_version': self.python_version,
            'components_built': len(self.components),
            'parallel_workers': self.max_workers,
            'optimization_level': self.optimization_level,
            'features': self._get_enabled_features()
        }
        
        report_file = self.vision_dir / "rust_build_report.json"
        with open(report_file, 'w') as f:
            json.dump(report, f, indent=2)
            
        logger.info(f"✓ Build report saved to {report_file}")
        
    async def _run_performance_test_async(self):
        """Run performance test asynchronously."""
        await asyncio.to_thread(self.run_performance_test)
        
    async def _save_build_info(self, build_time: float):
        """Save build information for future reference."""
        build_info = {
            'last_build': datetime.now().isoformat(),
            'build_time_seconds': build_time,
            'components': [c['name'] for c in self.components],
            'success': True
        }
        
        info_file = self.vision_dir / ".rust_build_info.json"
        with open(info_file, 'w') as f:
            json.dump(build_info, f, indent=2)

async def async_main():
    """Async entry point."""
    # Support custom config file
    config_path = None
    if len(sys.argv) > 1:
        config_path = Path(sys.argv[1])
        
    builder = RustBuilder(config_path)
    success = await builder.main()
    return success

if __name__ == "__main__":
    # Run async main
    success = asyncio.run(async_main())
    sys.exit(0 if success else 1)
