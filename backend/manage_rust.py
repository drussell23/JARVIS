#!/usr/bin/env python3
"""
Rust component management script for JARVIS.
Can be run standalone or imported by other modules.
"""

import os
import sys
import argparse
import asyncio
import json
import subprocess
from pathlib import Path
import psutil
import logging

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Color codes for output
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
RESET = "\033[0m"

class RustManager:
    """Manages Rust components for JARVIS."""
    
    def __init__(self):
        self.backend_dir = Path(__file__).parent
        self.vision_dir = self.backend_dir / "vision"
        self.rust_core_dir = self.vision_dir / "jarvis-rust-core"
        
    def check_prerequisites(self) -> bool:
        """Check if all prerequisites are installed."""
        print(f"\n{BLUE}Checking prerequisites...{RESET}")
        
        checks = [
            ("Python 3.8+", self._check_python),
            ("Rust/Cargo", self._check_rust),
            ("Maturin", self._check_maturin),
            ("Git", self._check_git)
        ]
        
        all_passed = True
        for name, check_func in checks:
            if check_func():
                print(f"  {GREEN}✓{RESET} {name}")
            else:
                print(f"  {RED}✗{RESET} {name}")
                all_passed = False
                
        return all_passed
        
    def _check_python(self) -> bool:
        version = sys.version_info
        return version.major >= 3 and version.minor >= 8
        
    def _check_rust(self) -> bool:
        try:
            result = subprocess.run(["cargo", "--version"], capture_output=True)
            return result.returncode == 0
        except FileNotFoundError:
            return False
            
    def _check_maturin(self) -> bool:
        try:
            result = subprocess.run(["maturin", "--version"], capture_output=True)
            return result.returncode == 0
        except FileNotFoundError:
            # Try to install maturin
            try:
                subprocess.run([sys.executable, "-m", "pip", "install", "maturin"], check=True)
                return True
            except Exception:
                return False

    def _check_git(self) -> bool:
        try:
            result = subprocess.run(["git", "--version"], capture_output=True)
            return result.returncode == 0
        except FileNotFoundError:
            return False
            
    def status(self) -> dict:
        """Get current Rust component status."""
        status = {
            'rust_built': False,
            'rust_imported': False,
            'components': {},
            'library_path': None,
            'system_info': {
                'platform': sys.platform,
                'cpu_count': psutil.cpu_count(),
                'ram_gb': psutil.virtual_memory().total / (1024**3),
                'is_m1': sys.platform == 'darwin' and os.uname().machine == 'arm64'
            }
        }
        
        # Check if built
        if sys.platform == "darwin":
            lib_file = self.rust_core_dir / "target" / "release" / "libjarvis_rust_core.dylib"
        elif sys.platform == "linux":
            lib_file = self.rust_core_dir / "target" / "release" / "libjarvis_rust_core.so"
        else:
            lib_file = self.rust_core_dir / "target" / "release" / "jarvis_rust_core.dll"
            
        if lib_file.exists():
            status['rust_built'] = True
            status['library_path'] = str(lib_file)
            
        # Try to import
        try:
            import jarvis_rust_core
            status['rust_imported'] = True
            
            # Check components
            components = [
                'RustAdvancedMemoryPool',
                'RustImageProcessor',
                'RustRuntimeManager',
                'bloom_filter',
                'sliding_window',
                'metal_accelerator',
                'zero_copy'
            ]
            
            for comp in components:
                status['components'][comp] = hasattr(jarvis_rust_core, comp)
                
        except ImportError:
            pass
            
        return status
        
    async def build(self, force_rebuild: bool = False) -> bool:
        """Build Rust components."""
        print(f"\n{BLUE}Building Rust components...{RESET}")
        
        # Check if already built
        if not force_rebuild:
            status = self.status()
            if status['rust_built']:
                print(f"{YELLOW}Rust components already built. Use --force to rebuild.{RESET}")
                return True
                
        # Run build script
        build_script = self.vision_dir / "build_rust_components.py"
        if not build_script.exists():
            print(f"{RED}Build script not found: {build_script}{RESET}")
            return False
            
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(build_script),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )
            
            # Stream output
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                print(line.decode().rstrip())
                
            await proc.wait()
            
            if proc.returncode == 0:
                print(f"\n{GREEN}✅ Rust components built successfully!{RESET}")
                return True
            else:
                print(f"\n{RED}✗ Build failed with code {proc.returncode}{RESET}")
                return False
                
        except Exception as e:
            print(f"{RED}Build error: {e}{RESET}")
            return False
            
    async def update_python(self) -> bool:
        """Update Python modules for Rust integration."""
        print(f"\n{BLUE}Updating Python modules...{RESET}")
        
        update_script = self.vision_dir / "update_python_rust_integration.py"
        if not update_script.exists():
            print(f"{RED}Update script not found: {update_script}{RESET}")
            return False
            
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(update_script),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )
            
            # Stream output
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                print(line.decode().rstrip())
                
            await proc.wait()
            
            if proc.returncode == 0:
                print(f"\n{GREEN}✅ Python modules updated successfully!{RESET}")
                return True
            else:
                print(f"\n{RED}✗ Update failed with code {proc.returncode}{RESET}")
                return False
                
        except Exception as e:
            print(f"{RED}Update error: {e}{RESET}")
            return False
            
    async def test(self) -> bool:
        """Test Rust components."""
        print(f"\n{BLUE}Testing Rust components...{RESET}")
        
        test_script = self.vision_dir / "test_rust_bridge.py"
        if not test_script.exists():
            print(f"{RED}Test script not found: {test_script}{RESET}")
            return False
            
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(test_script),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )
            
            # Stream output
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                print(line.decode().rstrip())
                
            await proc.wait()
            
            return proc.returncode == 0
                
        except Exception as e:
            print(f"{RED}Test error: {e}{RESET}")
            return False
            
    async def benchmark(self) -> bool:
        """Run performance benchmarks."""
        print(f"\n{BLUE}Running performance benchmarks...{RESET}")
        
        benchmark_script = self.vision_dir / "test_rust_performance.py"
        if not benchmark_script.exists():
            print(f"{RED}Benchmark script not found: {benchmark_script}{RESET}")
            return False
            
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(benchmark_script),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )
            
            # Stream output
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                print(line.decode().rstrip())
                
            await proc.wait()
            
            return proc.returncode == 0
                
        except Exception as e:
            print(f"{RED}Benchmark error: {e}{RESET}")
            return False
            
    def clean(self) -> bool:
        """Clean Rust build artifacts."""
        print(f"\n{BLUE}Cleaning Rust build artifacts...{RESET}")
        
        target_dir = self.rust_core_dir / "target"
        if target_dir.exists():
            import shutil
            try:
                shutil.rmtree(target_dir)
                print(f"{GREEN}✅ Cleaned: {target_dir}{RESET}")
            except Exception as e:
                print(f"{RED}Error cleaning {target_dir}: {e}{RESET}")
                return False
                
        # Remove update marker
        update_marker = self.vision_dir / ".rust_modules_updated"
        if update_marker.exists():
            update_marker.unlink()
            print(f"{GREEN}✅ Removed update marker{RESET}")
            
        return True
        
    def print_status(self):
        """Print detailed status information."""
        status = self.status()
        
        print(f"\n{BLUE}=== Rust Component Status ==={RESET}")
        print(f"\nSystem Information:")
        sys_info = status['system_info']
        print(f"  Platform: {sys_info['platform']}")
        print(f"  CPU cores: {sys_info['cpu_count']}")
        print(f"  RAM: {sys_info['ram_gb']:.1f}GB")
        if sys_info['is_m1']:
            print(f"  {GREEN}Apple Silicon M1 detected{RESET}")
            
        print(f"\nBuild Status:")
        if status['rust_built']:
            print(f"  {GREEN}✓ Rust library built{RESET}")
            print(f"  Path: {status['library_path']}")
        else:
            print(f"  {RED}✗ Rust library not built{RESET}")
            
        if status['rust_imported']:
            print(f"  {GREEN}✓ Rust core imported successfully{RESET}")
        else:
            print(f"  {YELLOW}⚠ Rust core not imported{RESET}")
            
        if status['components']:
            print(f"\nComponents:")
            for comp, available in status['components'].items():
                symbol = f"{GREEN}✓{RESET}" if available else f"{RED}✗{RESET}"
                print(f"  {symbol} {comp}")
                
    async def setup_all(self) -> bool:
        """Complete setup: build, update, and test."""
        print(f"\n{BLUE}=== Complete Rust Setup ==={RESET}")
        
        # Check prerequisites
        if not self.check_prerequisites():
            print(f"\n{RED}Prerequisites check failed. Please install missing components.{RESET}")
            return False
            
        # Build
        if not await self.build():
            return False
            
        # Update Python modules
        if not await self.update_python():
            return False
            
        # Test
        if not await self.test():
            return False
            
        print(f"\n{GREEN}=== ✅ Rust setup completed successfully! ==={RESET}")
        return True

async def main():
    """Main entry point for CLI."""
    parser = argparse.ArgumentParser(
        description="Manage Rust components for JARVIS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python manage_rust.py status          # Check current status
  python manage_rust.py build           # Build Rust components
  python manage_rust.py setup           # Complete setup (build + update + test)
  python manage_rust.py test            # Run tests
  python manage_rust.py benchmark       # Run performance benchmarks
  python manage_rust.py clean           # Clean build artifacts
"""
    )
    
    parser.add_argument(
        'command',
        choices=['status', 'build', 'update', 'test', 'benchmark', 'clean', 'setup'],
        help='Command to execute'
    )
    
    parser.add_argument(
        '--force',
        action='store_true',
        help='Force rebuild even if already built'
    )
    
    args = parser.parse_args()
    
    manager = RustManager()
    
    if args.command == 'status':
        manager.print_status()
    elif args.command == 'build':
        success = await manager.build(force_rebuild=args.force)
        sys.exit(0 if success else 1)
    elif args.command == 'update':
        success = await manager.update_python()
        sys.exit(0 if success else 1)
    elif args.command == 'test':
        success = await manager.test()
        sys.exit(0 if success else 1)
    elif args.command == 'benchmark':
        success = await manager.benchmark()
        sys.exit(0 if success else 1)
    elif args.command == 'clean':
        success = manager.clean()
        sys.exit(0 if success else 1)
    elif args.command == 'setup':
        success = await manager.setup_all()
        sys.exit(0 if success else 1)

if __name__ == "__main__":
    asyncio.run(main())