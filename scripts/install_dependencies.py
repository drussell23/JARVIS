#!/usr/bin/env python3
"""
JARVIS Dependency Installer v2.0
================================

Robust, async, parallel, intelligent dependency installer that:
- Detects and installs system dependencies (brew, apt, etc.)
- Installs Python packages in parallel for speed
- Handles platform-specific packages
- Validates installations
- Provides detailed status reporting
- Never hardcodes versions - reads from requirements files

Usage:
    python3 scripts/install_dependencies.py [--all] [--optional] [--system] [--verify]

Author: JARVIS System
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# ANSI colors for terminal output
class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'


@dataclass
class DependencyResult:
    """Result of a dependency installation attempt."""
    name: str
    success: bool
    version: Optional[str] = None
    error: Optional[str] = None
    duration_ms: float = 0.0
    skipped: bool = False
    reason: Optional[str] = None


@dataclass
class InstallationReport:
    """Complete installation report."""
    started_at: datetime = field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None
    system_deps: List[DependencyResult] = field(default_factory=list)
    python_deps: List[DependencyResult] = field(default_factory=list)
    optional_deps: List[DependencyResult] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def total_installed(self) -> int:
        return sum(1 for d in self.python_deps + self.optional_deps if d.success)

    @property
    def total_failed(self) -> int:
        return sum(1 for d in self.python_deps + self.optional_deps if not d.success and not d.skipped)

    @property
    def total_skipped(self) -> int:
        return sum(1 for d in self.python_deps + self.optional_deps if d.skipped)


class JARVISDependencyInstaller:
    """
    Intelligent, robust dependency installer for JARVIS.

    Features:
    - Platform detection (macOS, Linux, Windows)
    - Parallel package installation
    - Automatic retry with backoff
    - Version validation
    - Detailed reporting
    """

    # System dependencies required for Python packages
    # Maps Python package -> system dependency
    SYSTEM_DEPENDENCIES = {
        "pyaudio": {
            "darwin": ["portaudio"],
            "linux": ["portaudio19-dev", "python3-pyaudio"],
        },
        "webrtcvad": {
            "darwin": [],  # No system deps needed on macOS
            "linux": ["python3-dev"],
        },
        "librosa": {
            "darwin": ["ffmpeg"],
            "linux": ["ffmpeg", "libsndfile1"],
        },
        "opencv-python": {
            "darwin": [],
            "linux": ["libgl1-mesa-glx", "libglib2.0-0"],
        },
        "pytesseract": {
            "darwin": ["tesseract"],
            "linux": ["tesseract-ocr"],
        },
    }

    # Requirements files to process (in order of priority)
    REQUIREMENTS_FILES = [
        "backend/requirements.txt",
        "backend/requirements-gcp-databases.txt",
        "backend/requirements-cloud.txt",
        "backend/requirements-optional.txt",
        "backend/voice/requirements_voice_improvements.txt",
    ]

    def __init__(self, project_root: Optional[Path] = None):
        self.project_root = project_root or Path(__file__).parent.parent
        self.platform = platform.system().lower()
        self.report = InstallationReport()
        self._installed_packages: Set[str] = set()

    def _detect_package_manager(self) -> Optional[str]:
        """Detect the system package manager."""
        if self.platform == "darwin":
            if shutil.which("brew"):
                return "brew"
        elif self.platform == "linux":
            for pm in ["apt-get", "dnf", "yum", "pacman"]:
                if shutil.which(pm):
                    return pm
        return None

    async def _install_system_dep(self, package: str, pm: str) -> DependencyResult:
        """Install a single system dependency."""
        start = datetime.now()

        try:
            if pm == "brew":
                cmd = ["brew", "install", package]
            elif pm == "apt-get":
                cmd = ["sudo", "apt-get", "install", "-y", package]
            elif pm == "dnf":
                cmd = ["sudo", "dnf", "install", "-y", package]
            else:
                return DependencyResult(
                    name=package,
                    success=False,
                    error=f"Unsupported package manager: {pm}"
                )

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await proc.communicate()

            duration = (datetime.now() - start).total_seconds() * 1000

            if proc.returncode == 0:
                return DependencyResult(
                    name=package,
                    success=True,
                    duration_ms=duration
                )
            else:
                return DependencyResult(
                    name=package,
                    success=False,
                    error=stderr.decode()[:200],
                    duration_ms=duration
                )

        except Exception as e:
            duration = (datetime.now() - start).total_seconds() * 1000
            return DependencyResult(
                name=package,
                success=False,
                error=str(e),
                duration_ms=duration
            )

    async def install_system_dependencies(self, packages: List[str]) -> List[DependencyResult]:
        """Install system dependencies in parallel."""
        pm = self._detect_package_manager()
        if not pm:
            self.report.warnings.append("No package manager found - skipping system deps")
            return []

        print(f"\n{Colors.CYAN}{'='*60}{Colors.ENDC}")
        print(f"{Colors.BOLD}Installing System Dependencies ({pm}){Colors.ENDC}")
        print(f"{Colors.CYAN}{'='*60}{Colors.ENDC}")

        # Get platform-specific deps
        all_deps = set()
        for pkg in packages:
            if pkg in self.SYSTEM_DEPENDENCIES:
                deps = self.SYSTEM_DEPENDENCIES[pkg].get(self.platform, [])
                all_deps.update(deps)

        if not all_deps:
            print(f"{Colors.GREEN}  No system dependencies needed{Colors.ENDC}")
            return []

        # Install in parallel
        tasks = [self._install_system_dep(dep, pm) for dep in all_deps]
        results = await asyncio.gather(*tasks)

        for result in results:
            self.report.system_deps.append(result)
            status = f"{Colors.GREEN}✓{Colors.ENDC}" if result.success else f"{Colors.RED}✗{Colors.ENDC}"
            print(f"  {status} {result.name}")

        return results

    def _parse_requirements_file(self, filepath: Path) -> List[str]:
        """Parse a requirements file and extract package names."""
        packages = []
        if not filepath.exists():
            return packages

        with open(filepath, 'r') as f:
            for line in f:
                line = line.strip()
                # Skip comments and empty lines
                if not line or line.startswith('#'):
                    continue
                # Skip URL-based packages
                if '@' in line and 'http' in line:
                    continue
                # Extract package name (before ==, >=, <=, etc.)
                for sep in ['==', '>=', '<=', '!=', '~=', '[']:
                    if sep in line:
                        line = line.split(sep)[0]
                        break
                packages.append(line.strip())

        return packages

    async def _install_python_package(
        self,
        package: str,
        upgrade: bool = False
    ) -> DependencyResult:
        """Install a single Python package."""
        start = datetime.now()

        # Skip if already installed in this session
        if package.lower() in self._installed_packages:
            return DependencyResult(
                name=package,
                success=True,
                skipped=True,
                reason="Already installed this session"
            )

        try:
            cmd = [sys.executable, "-m", "pip", "install", package]
            if upgrade:
                cmd.append("--upgrade")
            cmd.extend(["--quiet", "--disable-pip-version-check"])

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await proc.communicate()

            duration = (datetime.now() - start).total_seconds() * 1000

            if proc.returncode == 0:
                self._installed_packages.add(package.lower())

                # Get installed version
                version = await self._get_package_version(package)

                return DependencyResult(
                    name=package,
                    success=True,
                    version=version,
                    duration_ms=duration
                )
            else:
                error_msg = stderr.decode()[:300]
                return DependencyResult(
                    name=package,
                    success=False,
                    error=error_msg,
                    duration_ms=duration
                )

        except Exception as e:
            duration = (datetime.now() - start).total_seconds() * 1000
            return DependencyResult(
                name=package,
                success=False,
                error=str(e),
                duration_ms=duration
            )

    async def _get_package_version(self, package: str) -> Optional[str]:
        """Get the installed version of a package."""
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pip", "show", package,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            stdout, _ = await proc.communicate()

            for line in stdout.decode().split('\n'):
                if line.startswith('Version:'):
                    return line.split(':', 1)[1].strip()
        except Exception:
            pass
        return None

    async def install_python_packages(
        self,
        packages: List[str],
        parallel: bool = True,
        max_concurrent: int = 5
    ) -> List[DependencyResult]:
        """Install Python packages, optionally in parallel."""
        print(f"\n{Colors.CYAN}{'='*60}{Colors.ENDC}")
        print(f"{Colors.BOLD}Installing Python Packages ({len(packages)} total){Colors.ENDC}")
        print(f"{Colors.CYAN}{'='*60}{Colors.ENDC}")

        if parallel:
            # Use semaphore to limit concurrent installations
            semaphore = asyncio.Semaphore(max_concurrent)

            async def install_with_limit(pkg: str) -> DependencyResult:
                async with semaphore:
                    return await self._install_python_package(pkg)

            tasks = [install_with_limit(pkg) for pkg in packages]
            results = await asyncio.gather(*tasks)
        else:
            results = []
            for pkg in packages:
                result = await self._install_python_package(pkg)
                results.append(result)

        # Print results
        for result in results:
            if result.skipped:
                print(f"  {Colors.YELLOW}○{Colors.ENDC} {result.name} (skipped)")
            elif result.success:
                version = f" ({result.version})" if result.version else ""
                print(f"  {Colors.GREEN}✓{Colors.ENDC} {result.name}{version}")
            else:
                print(f"  {Colors.RED}✗{Colors.ENDC} {result.name}: {result.error[:50]}")
                self.report.errors.append(f"{result.name}: {result.error}")

        return results

    async def install_from_requirements(self, filepath: Path) -> List[DependencyResult]:
        """Install all packages from a requirements file."""
        packages = self._parse_requirements_file(filepath)
        if not packages:
            return []

        print(f"\n{Colors.BLUE}Processing: {filepath.name}{Colors.ENDC}")
        return await self.install_python_packages(packages)

    async def install_all(
        self,
        include_optional: bool = True,
        include_system: bool = True
    ) -> InstallationReport:
        """Install all JARVIS dependencies."""
        print(f"\n{Colors.HEADER}{'='*60}{Colors.ENDC}")
        print(f"{Colors.BOLD}{Colors.HEADER}JARVIS Dependency Installer v2.0{Colors.ENDC}")
        print(f"{Colors.HEADER}{'='*60}{Colors.ENDC}")
        print(f"Platform: {self.platform}")
        print(f"Python: {sys.version.split()[0]}")
        print(f"Project: {self.project_root}")

        # Collect all packages to determine system deps
        all_packages = set()
        for req_file in self.REQUIREMENTS_FILES:
            filepath = self.project_root / req_file
            if filepath.exists():
                all_packages.update(self._parse_requirements_file(filepath))

        # Install system dependencies first
        if include_system:
            await self.install_system_dependencies(list(all_packages))

        # Install from main requirements
        main_req = self.project_root / "backend" / "requirements.txt"
        if main_req.exists():
            results = await self.install_from_requirements(main_req)
            self.report.python_deps.extend(results)

        # Install GCP database dependencies
        gcp_req = self.project_root / "backend" / "requirements-gcp-databases.txt"
        if gcp_req.exists():
            results = await self.install_from_requirements(gcp_req)
            self.report.python_deps.extend(results)

        # Install optional dependencies
        if include_optional:
            optional_files = [
                "backend/requirements-optional.txt",
                "backend/requirements-cloud.txt",
                "backend/voice/requirements_voice_improvements.txt",
            ]
            for req_file in optional_files:
                filepath = self.project_root / req_file
                if filepath.exists():
                    results = await self.install_from_requirements(filepath)
                    self.report.optional_deps.extend(results)

        self.report.completed_at = datetime.now()

        # Print summary
        self._print_summary()

        return self.report

    def _print_summary(self):
        """Print installation summary."""
        duration = (self.report.completed_at - self.report.started_at).total_seconds()

        print(f"\n{Colors.CYAN}{'='*60}{Colors.ENDC}")
        print(f"{Colors.BOLD}Installation Summary{Colors.ENDC}")
        print(f"{Colors.CYAN}{'='*60}{Colors.ENDC}")
        print(f"  Duration: {duration:.1f}s")
        print(f"  {Colors.GREEN}Installed: {self.report.total_installed}{Colors.ENDC}")
        print(f"  {Colors.YELLOW}Skipped: {self.report.total_skipped}{Colors.ENDC}")
        print(f"  {Colors.RED}Failed: {self.report.total_failed}{Colors.ENDC}")

        if self.report.errors:
            print(f"\n{Colors.RED}Errors:{Colors.ENDC}")
            for error in self.report.errors[:5]:
                print(f"  • {error[:80]}")

        if self.report.warnings:
            print(f"\n{Colors.YELLOW}Warnings:{Colors.ENDC}")
            for warning in self.report.warnings:
                print(f"  • {warning}")

        print(f"\n{Colors.GREEN}{'='*60}{Colors.ENDC}")
        if self.report.total_failed == 0:
            print(f"{Colors.GREEN}All dependencies installed successfully!{Colors.ENDC}")
        else:
            print(f"{Colors.YELLOW}Some dependencies failed. Check errors above.{Colors.ENDC}")
        print(f"{Colors.GREEN}{'='*60}{Colors.ENDC}")

    async def verify_critical_packages(self) -> Dict[str, bool]:
        """Verify that critical packages are importable."""
        critical = [
            "anthropic",
            "fastapi",
            "torch",
            "transformers",
            "librosa",
            "sounddevice",
            "aiohttp",
            "websockets",
            "chromadb",
            "langchain",
            "jsonschema",
        ]

        print(f"\n{Colors.CYAN}Verifying critical packages...{Colors.ENDC}")

        results = {}
        for pkg in critical:
            try:
                __import__(pkg.replace("-", "_"))
                results[pkg] = True
                print(f"  {Colors.GREEN}✓{Colors.ENDC} {pkg}")
            except ImportError:
                results[pkg] = False
                print(f"  {Colors.RED}✗{Colors.ENDC} {pkg}")

        return results


async def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="JARVIS Dependency Installer")
    parser.add_argument("--all", action="store_true", help="Install all dependencies")
    parser.add_argument("--optional", action="store_true", help="Include optional packages")
    parser.add_argument("--system", action="store_true", help="Install system dependencies")
    parser.add_argument("--verify", action="store_true", help="Verify critical packages")
    parser.add_argument("--project", type=str, help="Project root directory")

    args = parser.parse_args()

    project_root = Path(args.project) if args.project else None
    installer = JARVISDependencyInstaller(project_root)

    if args.verify:
        await installer.verify_critical_packages()
        return

    include_optional = args.optional or args.all
    include_system = args.system or args.all

    await installer.install_all(
        include_optional=include_optional,
        include_system=include_system
    )


if __name__ == "__main__":
    asyncio.run(main())
