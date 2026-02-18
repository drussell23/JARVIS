#!/usr/bin/env python3
"""
Auto-generate scripts/gcp_startup.sh from start_system.py

This ensures the standalone startup script is always in sync with the
embedded version in HybridWorkloadRouter._generate_startup_script()
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from start_system import HybridWorkloadRouter


def generate_startup_script():
    """Generate the GCP startup script from Python source"""
    print("🔨 Generating scripts/gcp_startup.sh from start_system.py...")

    # Create a minimal router instance (we only need the method)
    # Pass None for ram_monitor since we're just generating the script
    router = HybridWorkloadRouter(ram_monitor=None)

    # Generate script with default config
    script = router._generate_startup_script(
        {
            "repo_url": "https://github.com/drussell23/JARVIS-AI-Agent.git",
            "branch": "multi-monitor-support",
        }
    )

    # Write to file
    output_path = project_root / "scripts" / "gcp_startup.sh"
    output_path.write_text(script)

    # Make executable
    output_path.chmod(0o700)

    print(f"✅ Generated {output_path}")
    print(f"   Source: start_system.py:_generate_startup_script()")
    print(f"   Lines: {len(script.splitlines())}")


if __name__ == "__main__":
    try:
        generate_startup_script()
    except Exception as e:
        print(f"❌ Failed to generate startup script: {e}")
        sys.exit(1)
