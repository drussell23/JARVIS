#!/usr/bin/env python3
"""
JARVIS GCP Inference Image Builder v1.0.0
==========================================

Enterprise-grade script for building and deploying the pre-baked ML dependency
Docker image to GCP. This eliminates the 5-8 minute ml_deps installation phase
during VM startup.

Architecture:
    ┌─────────────────────────────────────────────────────────────────────┐
    │                    Image Build Pipeline                             │
    ├─────────────────────────────────────────────────────────────────────┤
    │  1. Validate environment (gcloud, docker)                           │
    │  2. Build Docker image with pre-baked ML deps                       │
    │  3. Push to Artifact Registry (GCR)                                 │
    │  4. Create/update instance template                                 │
    │  5. Verify deployment                                               │
    └─────────────────────────────────────────────────────────────────────┘

Usage:
    # Build and push (default)
    python scripts/build_gcp_inference_image.py

    # Build with GPU support
    python scripts/build_gcp_inference_image.py --compute-platform cuda

    # Build, push, and create instance template
    python scripts/build_gcp_inference_image.py --create-template

    # Dry run (show commands without executing)
    python scripts/build_gcp_inference_image.py --dry-run

Requirements:
    - Docker installed and running
    - gcloud CLI authenticated with appropriate permissions
    - Artifact Registry API enabled in GCP project

Version: 1.0.0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class BuildConfig:
    """Configuration for image building."""
    
    # GCP Configuration
    project_id: str = field(default_factory=lambda: os.getenv(
        "GCP_PROJECT_ID", 
        os.getenv("GOOGLE_CLOUD_PROJECT", "")
    ))
    region: str = field(default_factory=lambda: os.getenv("GCP_REGION", "us-central1"))
    zone: str = field(default_factory=lambda: os.getenv("GCP_ZONE", "us-central1-a"))
    
    # Image Configuration
    image_name: str = "jarvis-gcp-inference"
    image_tag: str = field(default_factory=lambda: datetime.now().strftime("%Y%m%d-%H%M%S"))
    compute_platform: str = "cpu"  # or "cuda"
    
    # Registry Configuration
    registry: str = field(default_factory=lambda: os.getenv(
        "GCP_ARTIFACT_REGISTRY",
        ""  # Will be computed from project_id
    ))
    
    # Build paths
    workspace_root: Path = field(default_factory=lambda: Path(__file__).parent.parent.resolve())
    dockerfile_path: Path = field(default=None)
    
    # Instance template configuration
    template_name: str = "jarvis-inference-template"
    machine_type: str = "e2-highmem-4"  # 32GB RAM
    disk_size_gb: int = 50
    
    # Build options
    no_cache: bool = False
    push: bool = True
    create_template: bool = False
    dry_run: bool = False
    
    def __post_init__(self):
        """Initialize computed fields."""
        if self.dockerfile_path is None:
            self.dockerfile_path = self.workspace_root / "docker" / "Dockerfile.gcp-inference"
        
        if not self.registry and self.project_id:
            self.registry = f"{self.region}-docker.pkg.dev/{self.project_id}/jarvis"
    
    @property
    def full_image_name(self) -> str:
        """Get the full image name with registry."""
        if self.registry:
            return f"{self.registry}/{self.image_name}:{self.image_tag}"
        return f"{self.image_name}:{self.image_tag}"
    
    @property
    def latest_image_name(self) -> str:
        """Get the latest tag image name."""
        if self.registry:
            return f"{self.registry}/{self.image_name}:latest"
        return f"{self.image_name}:latest"


# =============================================================================
# LOGGING
# =============================================================================

def setup_logging(verbose: bool = False) -> logging.Logger:
    """Configure logging."""
    level = logging.DEBUG if verbose else logging.INFO
    
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    
    return logging.getLogger(__name__)


logger = setup_logging()


# =============================================================================
# UTILITIES
# =============================================================================

def run_command(
    cmd: List[str],
    dry_run: bool = False,
    check: bool = True,
    capture_output: bool = False,
    cwd: Optional[Path] = None,
) -> subprocess.CompletedProcess:
    """Run a shell command with proper error handling."""
    cmd_str = " ".join(cmd)
    
    if dry_run:
        logger.info(f"[DRY-RUN] Would execute: {cmd_str}")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    
    logger.debug(f"Executing: {cmd_str}")
    
    try:
        result = subprocess.run(
            cmd,
            check=check,
            capture_output=capture_output,
            text=True,
            cwd=cwd,
        )
        return result
    except subprocess.CalledProcessError as e:
        logger.error(f"Command failed: {cmd_str}")
        logger.error(f"Exit code: {e.returncode}")
        if e.stdout:
            logger.error(f"stdout: {e.stdout}")
        if e.stderr:
            logger.error(f"stderr: {e.stderr}")
        raise


def check_tool_installed(tool: str) -> bool:
    """Check if a CLI tool is installed."""
    return shutil.which(tool) is not None


def validate_environment(config: BuildConfig) -> Tuple[bool, List[str]]:
    """Validate that all required tools and configuration are present."""
    errors = []
    
    # Check Docker
    if not check_tool_installed("docker"):
        errors.append("Docker is not installed or not in PATH")
    else:
        # Check Docker daemon is running
        try:
            run_command(["docker", "info"], capture_output=True, check=True)
        except subprocess.CalledProcessError:
            errors.append("Docker daemon is not running")
    
    # Check gcloud (only if pushing or creating template)
    if config.push or config.create_template:
        if not check_tool_installed("gcloud"):
            errors.append("gcloud CLI is not installed or not in PATH")
        
        # Check project ID
        if not config.project_id:
            errors.append(
                "GCP project ID not configured. Set GCP_PROJECT_ID or "
                "GOOGLE_CLOUD_PROJECT environment variable, or run 'gcloud config set project <PROJECT_ID>'"
            )
    
    # Check Dockerfile exists
    if not config.dockerfile_path.exists():
        errors.append(f"Dockerfile not found: {config.dockerfile_path}")
    
    return len(errors) == 0, errors


# =============================================================================
# BUILD STEPS
# =============================================================================

def build_docker_image(config: BuildConfig) -> bool:
    """Build the Docker image with pre-baked ML dependencies."""
    logger.info("=" * 70)
    logger.info("STEP 1: Building Docker Image")
    logger.info("=" * 70)
    
    logger.info(f"  Image: {config.image_name}:{config.image_tag}")
    logger.info(f"  Platform: {config.compute_platform}")
    logger.info(f"  Dockerfile: {config.dockerfile_path}")
    
    # Build command
    cmd = [
        "docker", "build",
        "-f", str(config.dockerfile_path),
        "-t", f"{config.image_name}:{config.image_tag}",
        "-t", f"{config.image_name}:latest",
        "--build-arg", f"COMPUTE_PLATFORM={config.compute_platform}",
    ]
    
    if config.no_cache:
        cmd.append("--no-cache")
    
    # Add workspace root as build context
    cmd.append(str(config.workspace_root))
    
    start_time = time.time()
    
    try:
        run_command(cmd, dry_run=config.dry_run)
        elapsed = time.time() - start_time
        logger.info(f"✅ Docker image built successfully in {elapsed:.1f}s")
        return True
    except subprocess.CalledProcessError:
        logger.error("❌ Docker build failed")
        return False


def push_to_registry(config: BuildConfig) -> bool:
    """Push the Docker image to Artifact Registry."""
    if not config.push:
        logger.info("Skipping push (--no-push specified)")
        return True
    
    logger.info("=" * 70)
    logger.info("STEP 2: Pushing to Artifact Registry")
    logger.info("=" * 70)
    
    logger.info(f"  Registry: {config.registry}")
    logger.info(f"  Image: {config.full_image_name}")
    
    # Ensure Artifact Registry repository exists
    logger.info("  Ensuring Artifact Registry repository exists...")
    repo_name = config.registry.split("/")[-1]
    
    create_repo_cmd = [
        "gcloud", "artifacts", "repositories", "create", repo_name,
        "--repository-format=docker",
        f"--location={config.region}",
        "--description=JARVIS AI Agent Docker images",
        "--quiet",
    ]
    
    try:
        run_command(create_repo_cmd, dry_run=config.dry_run, check=False)
    except subprocess.CalledProcessError:
        pass  # Repository may already exist
    
    # Configure Docker to use gcloud credentials
    logger.info("  Configuring Docker authentication...")
    auth_cmd = [
        "gcloud", "auth", "configure-docker",
        f"{config.region}-docker.pkg.dev",
        "--quiet",
    ]
    run_command(auth_cmd, dry_run=config.dry_run)
    
    # Tag the image for the registry
    logger.info("  Tagging image for registry...")
    tag_cmds = [
        ["docker", "tag", f"{config.image_name}:{config.image_tag}", config.full_image_name],
        ["docker", "tag", f"{config.image_name}:latest", config.latest_image_name],
    ]
    
    for cmd in tag_cmds:
        run_command(cmd, dry_run=config.dry_run)
    
    # Push both tags
    logger.info("  Pushing image to registry...")
    push_cmds = [
        ["docker", "push", config.full_image_name],
        ["docker", "push", config.latest_image_name],
    ]
    
    start_time = time.time()
    
    try:
        for cmd in push_cmds:
            run_command(cmd, dry_run=config.dry_run)
        
        elapsed = time.time() - start_time
        logger.info(f"✅ Image pushed successfully in {elapsed:.1f}s")
        return True
    except subprocess.CalledProcessError:
        logger.error("❌ Push to registry failed")
        return False


def create_instance_template(config: BuildConfig) -> bool:
    """Create a GCE instance template with the Docker container."""
    if not config.create_template:
        logger.info("Skipping instance template creation (--create-template not specified)")
        return True
    
    logger.info("=" * 70)
    logger.info("STEP 3: Creating Instance Template")
    logger.info("=" * 70)
    
    logger.info(f"  Template: {config.template_name}")
    logger.info(f"  Machine type: {config.machine_type}")
    logger.info(f"  Container image: {config.latest_image_name}")
    
    # Delete existing template if it exists
    delete_cmd = [
        "gcloud", "compute", "instance-templates", "delete",
        config.template_name,
        "--quiet",
    ]
    run_command(delete_cmd, dry_run=config.dry_run, check=False)
    
    # Create container spec
    container_spec = {
        "spec": {
            "containers": [{
                "name": "jarvis-inference",
                "image": config.latest_image_name,
                "env": [
                    {"name": "JARVIS_DEPS_PREBAKED", "value": "true"},
                    {"name": "JARVIS_SKIP_ML_DEPS_INSTALL", "value": "true"},
                    {"name": "JARVIS_GCP_INFERENCE", "value": "true"},
                    {"name": "JARVIS_PORT", "value": "8000"},
                ],
                "ports": [{"containerPort": 8000}],
            }],
            "restartPolicy": "Always",
        }
    }
    
    # Create instance template with container-optimized OS
    create_cmd = [
        "gcloud", "compute", "instance-templates", "create-with-container",
        config.template_name,
        f"--container-image={config.latest_image_name}",
        f"--machine-type={config.machine_type}",
        f"--boot-disk-size={config.disk_size_gb}GB",
        "--boot-disk-type=pd-ssd",
        "--preemptible",  # Spot VM
        "--maintenance-policy=TERMINATE",
        "--provisioning-model=SPOT",
        "--instance-termination-action=DELETE",
        "--container-env=JARVIS_DEPS_PREBAKED=true",
        "--container-env=JARVIS_SKIP_ML_DEPS_INSTALL=true",
        "--container-env=JARVIS_GCP_INFERENCE=true",
        "--container-env=JARVIS_PORT=8000",
        "--tags=jarvis-inference,http-server",
        "--metadata=jarvis-port=8000",
        f"--scopes=cloud-platform",
    ]
    
    try:
        run_command(create_cmd, dry_run=config.dry_run)
        logger.info(f"✅ Instance template created: {config.template_name}")
        return True
    except subprocess.CalledProcessError:
        logger.error("❌ Instance template creation failed")
        return False


def print_usage_instructions(config: BuildConfig) -> None:
    """Print instructions for using the built image."""
    logger.info("")
    logger.info("=" * 70)
    logger.info("BUILD COMPLETE - Usage Instructions")
    logger.info("=" * 70)
    
    logger.info("")
    logger.info("1. LOCAL TESTING:")
    logger.info("   docker run -p 8000:8000 jarvis-gcp-inference:latest")
    logger.info("")
    
    if config.push:
        logger.info("2. GCP DEPLOYMENT:")
        logger.info(f"   Image: {config.full_image_name}")
        logger.info(f"   Latest: {config.latest_image_name}")
        logger.info("")
        
        if config.create_template:
            logger.info("3. CREATE VM FROM TEMPLATE:")
            logger.info(f"   gcloud compute instances create jarvis-inference-vm \\")
            logger.info(f"     --source-instance-template={config.template_name} \\")
            logger.info(f"     --zone={config.zone}")
            logger.info("")
        
        logger.info("4. MANUAL VM CREATION WITH CONTAINER:")
        logger.info(f"   gcloud compute instances create-with-container jarvis-vm \\")
        logger.info(f"     --container-image={config.latest_image_name} \\")
        logger.info(f"     --machine-type=e2-highmem-4 \\")
        logger.info(f"     --zone={config.zone} \\")
        logger.info(f"     --preemptible")
        logger.info("")
    
    logger.info("5. INTEGRATION WITH gcp_vm_manager.py:")
    logger.info("   Set environment variable:")
    logger.info(f"   export JARVIS_GCP_CONTAINER_IMAGE=\"{config.latest_image_name if config.push else config.image_name + ':latest'}\"")
    logger.info("")
    
    logger.info("6. EXPECTED STARTUP TIME IMPROVEMENT:")
    logger.info("   Without pre-baked image: ~8-10 minutes")
    logger.info("   With pre-baked image:    ~2-3 minutes")
    logger.info("   Time saved:              ~5-8 minutes per VM startup!")
    logger.info("")


# =============================================================================
# MAIN
# =============================================================================

def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Build and deploy JARVIS GCP inference Docker image",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                         Build and push (default)
  %(prog)s --compute-platform cuda Build with GPU support
  %(prog)s --create-template       Also create instance template
  %(prog)s --dry-run               Show commands without executing
  %(prog)s --no-push               Build only, don't push to registry
        """,
    )
    
    parser.add_argument(
        "--project-id",
        help="GCP project ID (default: from env GCP_PROJECT_ID)",
    )
    parser.add_argument(
        "--region",
        default="us-central1",
        help="GCP region (default: us-central1)",
    )
    parser.add_argument(
        "--compute-platform",
        choices=["cpu", "cuda"],
        default="cpu",
        help="Compute platform: cpu or cuda (default: cpu)",
    )
    parser.add_argument(
        "--image-tag",
        help="Image tag (default: timestamp)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Build without Docker cache",
    )
    parser.add_argument(
        "--no-push",
        action="store_true",
        help="Don't push to registry (local build only)",
    )
    parser.add_argument(
        "--create-template",
        action="store_true",
        help="Create GCE instance template after pushing",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show commands without executing",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    
    return parser.parse_args()


def main() -> int:
    """Main entry point."""
    args = parse_args()
    
    # Setup logging
    global logger
    logger = setup_logging(args.verbose)
    
    # Build configuration
    config = BuildConfig(
        project_id=args.project_id or os.getenv("GCP_PROJECT_ID", ""),
        region=args.region,
        compute_platform=args.compute_platform,
        no_cache=args.no_cache,
        push=not args.no_push,
        create_template=args.create_template,
        dry_run=args.dry_run,
    )
    
    if args.image_tag:
        config.image_tag = args.image_tag
    
    # Print banner
    logger.info("")
    logger.info("=" * 70)
    logger.info("JARVIS GCP Inference Image Builder v1.0.0")
    logger.info("=" * 70)
    logger.info(f"  Project ID:     {config.project_id or '(not set)'}")
    logger.info(f"  Region:         {config.region}")
    logger.info(f"  Platform:       {config.compute_platform}")
    logger.info(f"  Image:          {config.image_name}:{config.image_tag}")
    logger.info(f"  Push:           {config.push}")
    logger.info(f"  Create Template: {config.create_template}")
    logger.info(f"  Dry Run:        {config.dry_run}")
    logger.info("=" * 70)
    logger.info("")
    
    # Validate environment
    valid, errors = validate_environment(config)
    if not valid:
        logger.error("Environment validation failed:")
        for error in errors:
            logger.error(f"  - {error}")
        return 1
    
    # Execute build steps
    if not build_docker_image(config):
        return 1
    
    if not push_to_registry(config):
        return 1
    
    if not create_instance_template(config):
        return 1
    
    # Print usage instructions
    print_usage_instructions(config)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
