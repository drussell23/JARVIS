#!/usr/bin/env python3
"""
JARVIS GCP Cleanup Script - Standalone Cost Optimization
=========================================================
v1.0.0 - Independent Cleanup Edition

Run this script when JARVIS is NOT running to clean up GCP resources
and reduce costs. Can be run via cron job or manually.

Usage:
    python scripts/gcp_cleanup.py                    # Interactive mode
    python scripts/gcp_cleanup.py --all              # Full cleanup (non-interactive)
    python scripts/gcp_cleanup.py --artifacts-only   # Just clean Artifact Registry
    python scripts/gcp_cleanup.py --sql-stop         # Stop Cloud SQL (saves ~$10/month)
    python scripts/gcp_cleanup.py --report           # Show cost report only

Cost Drivers Addressed:
    1. Artifact Registry (old Docker images) - Can accumulate 50+ GB
    2. Cloud SQL (always running) - ~$10/month baseline
    3. Orphaned VMs - Should be zero if JARVIS exited cleanly
    4. Orphaned Cloud Run - Should be zero if JARVIS exited cleanly

Author: JARVIS AI System
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Color codes for terminal output
class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'

def print_header(msg: str):
    print(f"\n{Colors.HEADER}{Colors.BOLD}{'='*60}{Colors.ENDC}")
    print(f"{Colors.HEADER}{Colors.BOLD}  {msg}{Colors.ENDC}")
    print(f"{Colors.HEADER}{Colors.BOLD}{'='*60}{Colors.ENDC}\n")

def print_section(msg: str):
    print(f"\n{Colors.CYAN}{Colors.BOLD}>>> {msg}{Colors.ENDC}")

def print_success(msg: str):
    print(f"{Colors.GREEN}  ✓ {msg}{Colors.ENDC}")

def print_warning(msg: str):
    print(f"{Colors.WARNING}  ! {msg}{Colors.ENDC}")

def print_error(msg: str):
    print(f"{Colors.FAIL}  ✗ {msg}{Colors.ENDC}")

def print_info(msg: str):
    print(f"{Colors.BLUE}  ℹ {msg}{Colors.ENDC}")


def _orphan_instance_filter() -> str:
    """Return the gcloud --filter= predicate for orphaned JARVIS GCE instances.

    Root-logic fix (A1 Brain $0 mandate): Brain VMs are labelled
    ``jarvis-role=brain`` ONLY (see gcp_compute_rest._BRAIN_ROLE_LABEL_KEY /
    _BRAIN_ROLE_LABEL_VALUE) -- they carry neither ``created-by=jarvis`` nor
    ``app=jarvis``. Without the third predicate below, an orphaned Brain VM
    (e.g. left behind by a SIGKILL'd ignition driver) was invisible to this
    sweep. Extracted into a pure helper so the query logic is unit-testable
    without shelling out to real gcloud.
    """
    try:
        # Prefer the canonical label constants from the Compute REST client
        # (zero hardcoding of the label value) -- import is stdlib-only and
        # side-effect-free at module scope (see gcp_compute_rest.py docstring).
        from backend.core.ouroboros.governance.gcp_compute_rest import (
            _BRAIN_ROLE_LABEL_KEY,
            _BRAIN_ROLE_LABEL_VALUE,
        )

        brain_predicate = f"labels.{_BRAIN_ROLE_LABEL_KEY}={_BRAIN_ROLE_LABEL_VALUE}"
    except Exception:
        # Fallback literal citing the canonical source, in case the import
        # path is unavailable in this execution context (e.g. run as a bare
        # script outside the repo root).
        brain_predicate = "labels.jarvis-role=brain"  # gcp_compute_rest._BRAIN_ROLE_LABEL_KEY/_VALUE

    return f"labels.created-by=jarvis OR labels.app=jarvis OR {brain_predicate}"


@dataclass
class GCPConfig:
    """GCP configuration from environment."""
    project_id: str = field(default_factory=lambda: os.getenv(
        "GCP_PROJECT_ID", os.getenv("GOOGLE_CLOUD_PROJECT", "jarvis-473803")
    ))
    region: str = field(default_factory=lambda: os.getenv("GCP_REGION", "us-central1"))
    zone: str = field(default_factory=lambda: os.getenv("GCP_ZONE", "us-central1-a"))


class GCPCleanup:
    """Standalone GCP cleanup utility."""

    def __init__(self, config: Optional[GCPConfig] = None):
        self.config = config or GCPConfig()
        self._dry_run = False

    def set_dry_run(self, dry_run: bool):
        """Enable dry run mode (no actual deletions)."""
        self._dry_run = dry_run

    # =========================================================================
    # Cost Analysis
    # =========================================================================

    async def get_cost_report(self) -> Dict[str, Any]:
        """Generate a comprehensive cost report."""
        report = {
            "generated_at": datetime.now().isoformat(),
            "project_id": self.config.project_id,
            "resources": {},
            "total_estimated_monthly_cost_usd": 0.0,
            "potential_savings_usd": 0.0,
        }

        # Check Artifact Registry
        artifact_info = await self._get_artifact_registry_info()
        report["resources"]["artifact_registry"] = artifact_info
        # $0.10/GB/month for Artifact Registry
        artifact_cost = artifact_info.get("total_size_gb", 0) * 0.10
        report["total_estimated_monthly_cost_usd"] += artifact_cost

        # Check Cloud SQL
        sql_info = await self._get_cloud_sql_info()
        report["resources"]["cloud_sql"] = sql_info
        # db-f1-micro is ~$7-10/month
        if sql_info.get("state") == "RUNNABLE":
            sql_cost = 9.0
            report["total_estimated_monthly_cost_usd"] += sql_cost
            if sql_info.get("activation_policy") == "ALWAYS":
                report["potential_savings_usd"] += sql_cost  # Can be stopped

        # Check for orphaned VMs
        orphan_vms = await self._find_orphaned_resources()
        report["resources"]["orphaned_vms"] = orphan_vms
        # e2-medium is ~$25/month
        report["total_estimated_monthly_cost_usd"] += len(orphan_vms) * 25

        # Check Cloud Run
        cloud_run_info = await self._get_cloud_run_info()
        report["resources"]["cloud_run"] = cloud_run_info

        # Calculate potential savings from artifact cleanup
        if artifact_info.get("deletable_size_gb", 0) > 0:
            report["potential_savings_usd"] += artifact_info["deletable_size_gb"] * 0.10

        return report

    async def _get_artifact_registry_info(self) -> Dict[str, Any]:
        """Get Artifact Registry storage info."""
        info = {
            "repositories": [],
            "total_size_gb": 0.0,
            "deletable_size_gb": 0.0,
            "total_images": 0,
            "untagged_images": 0,
        }

        try:
            # Get repository sizes
            result = subprocess.run(
                [
                    "gcloud", "artifacts", "repositories", "list",
                    f"--project={self.config.project_id}",
                    "--format=json(name,sizeBytes)",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode == 0 and result.stdout.strip():
                repos = json.loads(result.stdout)
                for repo in repos:
                    name = repo.get("name", "").split("/")[-1]
                    size_bytes = repo.get("sizeBytes", 0)
                    size_gb = size_bytes / (1024 ** 3) if size_bytes else 0

                    info["repositories"].append({
                        "name": name,
                        "size_gb": round(size_gb, 2),
                    })
                    info["total_size_gb"] += size_gb

                info["total_size_gb"] = round(info["total_size_gb"], 2)
                # Estimate 80% of storage is deletable old images
                info["deletable_size_gb"] = round(info["total_size_gb"] * 0.8, 2)

        except Exception as e:
            info["error"] = str(e)

        return info

    async def _get_cloud_sql_info(self) -> Dict[str, Any]:
        """Get Cloud SQL instance info."""
        info = {
            "instances": [],
            "state": "UNKNOWN",
            "tier": "UNKNOWN",
            "activation_policy": "UNKNOWN",
        }

        try:
            result = subprocess.run(
                [
                    "gcloud", "sql", "instances", "list",
                    f"--project={self.config.project_id}",
                    "--format=json(name,state,settings.tier,settings.activationPolicy)",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode == 0 and result.stdout.strip():
                instances = json.loads(result.stdout)
                for instance in instances:
                    settings = instance.get("settings", {})
                    info["instances"].append({
                        "name": instance.get("name"),
                        "state": instance.get("state"),
                        "tier": settings.get("tier"),
                        "activation_policy": settings.get("activationPolicy"),
                    })
                    # Use first instance for summary
                    if info["state"] == "UNKNOWN":
                        info["state"] = instance.get("state")
                        info["tier"] = settings.get("tier")
                        info["activation_policy"] = settings.get("activationPolicy")

        except Exception as e:
            info["error"] = str(e)

        return info

    async def _find_orphaned_resources(self) -> List[Dict[str, Any]]:
        """Find orphaned VMs."""
        orphans = []

        try:
            # Check for JARVIS-labeled VMs
            result = subprocess.run(
                [
                    "gcloud", "compute", "instances", "list",
                    f"--project={self.config.project_id}",
                    f"--filter={_orphan_instance_filter()}",
                    "--format=json(name,zone,status,creationTimestamp)",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode == 0 and result.stdout.strip():
                vms = json.loads(result.stdout)
                for vm in vms:
                    orphans.append({
                        "name": vm.get("name"),
                        "zone": vm.get("zone", "").split("/")[-1],
                        "status": vm.get("status"),
                        "created": vm.get("creationTimestamp"),
                    })

        except Exception as e:
            pass  # Ignore errors

        return orphans

    async def _get_cloud_run_info(self) -> Dict[str, Any]:
        """Get Cloud Run services info."""
        info = {"services": []}

        try:
            result = subprocess.run(
                [
                    "gcloud", "run", "services", "list",
                    f"--project={self.config.project_id}",
                    f"--region={self.config.region}",
                    "--format=json(metadata.name,status.url)",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode == 0 and result.stdout.strip():
                services = json.loads(result.stdout)
                for svc in services:
                    info["services"].append({
                        "name": svc.get("metadata", {}).get("name"),
                        "url": svc.get("status", {}).get("url"),
                    })

        except Exception as e:
            info["error"] = str(e)

        return info

    # =========================================================================
    # Cleanup Operations
    # =========================================================================

    async def cleanup_artifact_registry(
        self,
        keep_latest_n: int = 3,
        older_than_days: int = 7,
    ) -> Dict[str, Any]:
        """Clean up old Docker images from Artifact Registry."""
        import re

        results = {
            "images_deleted": 0,
            "storage_freed_mb": 0,
            "errors": [],
        }

        print_section("Cleaning Artifact Registry")

        if self._dry_run:
            print_warning("DRY RUN MODE - No actual deletions")

        # List repositories
        try:
            result = subprocess.run(
                [
                    "gcloud", "artifacts", "repositories", "list",
                    f"--project={self.config.project_id}",
                    "--format=value(name)",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                results["errors"].append(f"Failed to list repos: {result.stderr}")
                return results

            repos = [r.strip() for r in result.stdout.strip().split("\n") if r.strip()]

        except Exception as e:
            results["errors"].append(str(e))
            return results

        for repo_path in repos:
            # Parse: projects/PROJECT/locations/REGION/repositories/REPO
            parts = repo_path.split("/")
            if len(parts) < 6:
                continue

            location = parts[3]
            repo_name = parts[5]

            print_info(f"Scanning repository: {repo_name}")

            # List images
            try:
                list_result = subprocess.run(
                    [
                        "gcloud", "artifacts", "docker", "images", "list",
                        f"{location}-docker.pkg.dev/{self.config.project_id}/{repo_name}",
                        "--format=json(package,tags,createTime)",
                        "--include-tags",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )

                if list_result.returncode != 0:
                    continue

                images = json.loads(list_result.stdout) if list_result.stdout.strip() else []

                # Sort by create time (newest first)
                images.sort(key=lambda x: x.get("createTime", ""), reverse=True)

                cutoff_date = datetime.now(timezone.utc) - timedelta(days=older_than_days)
                version_pattern = re.compile(r'^v?\d+\.\d+\.\d+$|^latest$|^main$|^master$')

                kept = 0
                for img in images:
                    tags = img.get("tags", [])
                    create_str = img.get("createTime", "")
                    package = img.get("package", "")

                    try:
                        create_time = datetime.fromisoformat(create_str.replace("Z", "+00:00"))
                    except (ValueError, TypeError):
                        continue

                    # Determine if we should keep
                    should_keep = False

                    # Keep tagged images
                    if tags:
                        for tag in tags:
                            if version_pattern.match(tag):
                                should_keep = True
                                break

                    # Keep latest N
                    if kept < keep_latest_n:
                        should_keep = True

                    # Keep recent
                    if create_time > cutoff_date:
                        should_keep = True

                    if should_keep:
                        kept += 1
                        continue

                    # Delete
                    if not self._dry_run:
                        try:
                            del_result = subprocess.run(
                                [
                                    "gcloud", "artifacts", "docker", "images", "delete",
                                    package,
                                    "--quiet",
                                    "--delete-tags",
                                ],
                                capture_output=True,
                                timeout=30,
                            )

                            if del_result.returncode == 0:
                                results["images_deleted"] += 1
                                results["storage_freed_mb"] += 2000  # Estimate
                                print_success(f"Deleted: {package.split('/')[-1][:50]}")
                        except Exception as e:
                            results["errors"].append(f"Delete failed: {e}")
                    else:
                        print_info(f"Would delete: {package.split('/')[-1][:50]}")
                        results["images_deleted"] += 1
                        results["storage_freed_mb"] += 2000

            except Exception as e:
                results["errors"].append(f"Error in {repo_name}: {e}")

        print_success(f"Deleted {results['images_deleted']} images (~{results['storage_freed_mb']/1000:.1f} GB freed)")

        return results

    async def stop_cloud_sql(self, instance_name: str = "jarvis-learning-db") -> bool:
        """Stop Cloud SQL instance."""
        print_section(f"Stopping Cloud SQL: {instance_name}")

        if self._dry_run:
            print_warning("DRY RUN - Would stop Cloud SQL")
            return True

        try:
            result = subprocess.run(
                [
                    "gcloud", "sql", "instances", "patch", instance_name,
                    f"--project={self.config.project_id}",
                    "--activation-policy=NEVER",
                    "--quiet",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )

            if result.returncode == 0:
                print_success(f"Cloud SQL {instance_name} stopped (saves ~$10/month)")
                return True
            else:
                print_error(f"Failed to stop: {result.stderr}")
                return False

        except Exception as e:
            print_error(f"Error: {e}")
            return False

    async def start_cloud_sql(self, instance_name: str = "jarvis-learning-db") -> bool:
        """Start Cloud SQL instance."""
        print_section(f"Starting Cloud SQL: {instance_name}")

        if self._dry_run:
            print_warning("DRY RUN - Would start Cloud SQL")
            return True

        try:
            result = subprocess.run(
                [
                    "gcloud", "sql", "instances", "patch", instance_name,
                    f"--project={self.config.project_id}",
                    "--activation-policy=ALWAYS",
                    "--quiet",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )

            if result.returncode == 0:
                print_success(f"Cloud SQL {instance_name} started")
                return True
            else:
                print_error(f"Failed to start: {result.stderr}")
                return False

        except Exception as e:
            print_error(f"Error: {e}")
            return False

    async def cleanup_orphaned_vms(self) -> Dict[str, Any]:
        """Delete orphaned VMs."""
        results = {"deleted": [], "errors": []}

        print_section("Cleaning Orphaned VMs")

        orphans = await self._find_orphaned_resources()

        if not orphans:
            print_success("No orphaned VMs found")
            return results

        for vm in orphans:
            vm_name = vm["name"]
            zone = vm["zone"]

            if not self._dry_run:
                try:
                    result = subprocess.run(
                        [
                            "gcloud", "compute", "instances", "delete", vm_name,
                            f"--project={self.config.project_id}",
                            f"--zone={zone}",
                            "--quiet",
                        ],
                        capture_output=True,
                        timeout=120,
                    )

                    if result.returncode == 0:
                        results["deleted"].append(vm_name)
                        print_success(f"Deleted VM: {vm_name}")
                    else:
                        results["errors"].append(f"{vm_name}: {result.stderr.decode()}")
                        print_error(f"Failed to delete {vm_name}")

                except Exception as e:
                    results["errors"].append(f"{vm_name}: {e}")
            else:
                print_info(f"Would delete: {vm_name}")
                results["deleted"].append(vm_name)

        return results

    async def full_cleanup(self) -> Dict[str, Any]:
        """Run full cleanup of all resources."""
        results = {
            "artifact_registry": {},
            "cloud_sql": False,
            "orphaned_vms": {},
            "total_savings_estimated_usd": 0.0,
        }

        # Artifact Registry cleanup
        results["artifact_registry"] = await self.cleanup_artifact_registry()

        # Orphaned VMs
        results["orphaned_vms"] = await self.cleanup_orphaned_vms()

        # Calculate savings
        # Artifact storage: ~$0.10/GB/month
        artifact_gb = results["artifact_registry"].get("storage_freed_mb", 0) / 1000
        results["total_savings_estimated_usd"] += artifact_gb * 0.10

        # Orphaned VMs: ~$25/month each
        results["total_savings_estimated_usd"] += len(results["orphaned_vms"].get("deleted", [])) * 25

        return results


def print_cost_report(report: Dict[str, Any]):
    """Print a formatted cost report."""
    print_header("JARVIS GCP Cost Report")

    print(f"  Project: {report['project_id']}")
    print(f"  Generated: {report['generated_at']}")
    print()

    # Artifact Registry
    ar = report["resources"].get("artifact_registry", {})
    print_section("Artifact Registry")
    for repo in ar.get("repositories", []):
        print(f"    {repo['name']}: {repo['size_gb']:.2f} GB")
    print(f"  {Colors.BOLD}Total: {ar.get('total_size_gb', 0):.2f} GB (~${ar.get('total_size_gb', 0) * 0.10:.2f}/month){Colors.ENDC}")

    # Cloud SQL
    sql = report["resources"].get("cloud_sql", {})
    print_section("Cloud SQL")
    for inst in sql.get("instances", []):
        status_color = Colors.GREEN if inst["activation_policy"] == "NEVER" else Colors.WARNING
        print(f"    {inst['name']}: {status_color}{inst['state']} ({inst['activation_policy']}){Colors.ENDC}")
    if sql.get("state") == "RUNNABLE" and sql.get("activation_policy") == "ALWAYS":
        print(f"  {Colors.WARNING}⚠️  Running 24/7 - costs ~$9-10/month{Colors.ENDC}")

    # Orphaned VMs
    orphans = report["resources"].get("orphaned_vms", [])
    print_section("Orphaned VMs")
    if orphans:
        for vm in orphans:
            print(f"  {Colors.FAIL}  ⚠️  {vm['name']} ({vm['status']}) - ~$25/month{Colors.ENDC}")
    else:
        print_success("No orphaned VMs")

    # Cloud Run
    cr = report["resources"].get("cloud_run", {})
    print_section("Cloud Run Services")
    for svc in cr.get("services", []):
        print(f"    {svc['name']}: Scale-to-zero (no idle cost)")

    # Summary
    print_section("Cost Summary")
    print(f"  Estimated Monthly Cost: {Colors.BOLD}${report['total_estimated_monthly_cost_usd']:.2f}{Colors.ENDC}")
    if report['potential_savings_usd'] > 0:
        print(f"  {Colors.GREEN}Potential Savings: ${report['potential_savings_usd']:.2f}/month{Colors.ENDC}")


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="JARVIS GCP Cleanup - Reduce cloud costs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python gcp_cleanup.py --report           Show cost analysis
  python gcp_cleanup.py --all              Full cleanup (non-interactive)
  python gcp_cleanup.py --artifacts-only   Just clean old Docker images
  python gcp_cleanup.py --sql-stop         Stop Cloud SQL (~$10/month saved)
  python gcp_cleanup.py --dry-run --all    Preview what would be deleted
        """,
    )

    parser.add_argument("--report", action="store_true", help="Show cost report only")
    parser.add_argument("--all", action="store_true", help="Run full cleanup")
    parser.add_argument("--artifacts-only", action="store_true", help="Clean Artifact Registry only")
    parser.add_argument("--sql-stop", action="store_true", help="Stop Cloud SQL instance")
    parser.add_argument("--sql-start", action="store_true", help="Start Cloud SQL instance")
    parser.add_argument("--vms-only", action="store_true", help="Clean orphaned VMs only")
    parser.add_argument("--dry-run", action="store_true", help="Preview without making changes")
    parser.add_argument("--keep-latest", type=int, default=3, help="Keep latest N images (default: 3)")
    parser.add_argument("--older-than", type=int, default=7, help="Delete images older than N days (default: 7)")

    args = parser.parse_args()

    cleanup = GCPCleanup()

    if args.dry_run:
        cleanup.set_dry_run(True)
        print_warning("DRY RUN MODE - No actual changes will be made\n")

    # Cost report
    if args.report or not any([args.all, args.artifacts_only, args.sql_stop, args.sql_start, args.vms_only]):
        report = await cleanup.get_cost_report()
        print_cost_report(report)

        if not any([args.all, args.artifacts_only, args.sql_stop, args.sql_start, args.vms_only]):
            print(f"\n{Colors.CYAN}Run with --all for full cleanup, or --help for options{Colors.ENDC}\n")
        return

    # Full cleanup
    if args.all:
        print_header("JARVIS GCP Full Cleanup")
        results = await cleanup.full_cleanup()
        print_section("Summary")
        print(f"  Estimated savings: ${results['total_savings_estimated_usd']:.2f}/month")
        return

    # Individual operations
    if args.artifacts_only:
        await cleanup.cleanup_artifact_registry(
            keep_latest_n=args.keep_latest,
            older_than_days=args.older_than,
        )

    if args.sql_stop:
        await cleanup.stop_cloud_sql()

    if args.sql_start:
        await cleanup.start_cloud_sql()

    if args.vms_only:
        await cleanup.cleanup_orphaned_vms()


if __name__ == "__main__":
    asyncio.run(main())
