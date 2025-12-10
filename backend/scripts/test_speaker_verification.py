#!/usr/bin/env python3
"""
🔐 SPEAKER VERIFICATION TEST
═══════════════════════════════════════════════════════════════════════

Comprehensive test suite for speaker verification that works with or without
numpy/torch dependencies. Uses the lightweight SpeakerProfileStore for
environments without the full ML stack.

Usage:
    python3 scripts/test_speaker_verification.py
    python3 scripts/test_speaker_verification.py --verbose
    python3 scripts/test_speaker_verification.py --json

Author: JARVIS System
Version: 2.0.0
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import struct
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Add backend to path
BACKEND_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BACKEND_DIR))


# =============================================================================
# OUTPUT FORMATTING
# =============================================================================

class Colors:
    """ANSI color codes for terminal output."""
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'
    END = '\033[0m'
    
    @classmethod
    def disable(cls):
        """Disable colors for non-terminal output."""
        cls.HEADER = cls.BLUE = cls.CYAN = cls.GREEN = ''
        cls.YELLOW = cls.RED = cls.BOLD = cls.UNDERLINE = cls.END = ''


def print_header(title: str):
    """Print a major section header."""
    print(f"\n{Colors.BOLD}{'═' * 70}")
    print(f"  {title}")
    print(f"{'═' * 70}{Colors.END}\n")


def print_section(title: str):
    """Print a subsection header."""
    print(f"\n{Colors.CYAN}{'─' * 50}")
    print(f"  {title}")
    print(f"{'─' * 50}{Colors.END}")


def print_ok(msg: str):
    """Print success message."""
    print(f"{Colors.GREEN}✅ {msg}{Colors.END}")


def print_warn(msg: str):
    """Print warning message."""
    print(f"{Colors.YELLOW}⚠️  {msg}{Colors.END}")


def print_err(msg: str):
    """Print error message."""
    print(f"{Colors.RED}❌ {msg}{Colors.END}")


def print_info(msg: str):
    """Print info message."""
    print(f"{Colors.BLUE}ℹ️  {msg}{Colors.END}")


# =============================================================================
# TEST RESULTS
# =============================================================================

@dataclass
class TestResult:
    """Result of a single test."""
    name: str
    passed: bool
    message: str
    details: Dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0


@dataclass
class TestSuite:
    """Collection of test results."""
    results: List[TestResult] = field(default_factory=list)
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    
    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)
    
    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.passed)
    
    @property
    def total(self) -> int:
        return len(self.results)
    
    @property
    def all_passed(self) -> bool:
        return self.failed == 0
    
    @property
    def duration_ms(self) -> float:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds() * 1000
        return 0.0
    
    def add(self, result: TestResult):
        self.results.append(result)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "failed": self.failed,
            "total": self.total,
            "all_passed": self.all_passed,
            "duration_ms": round(self.duration_ms, 2),
            "results": [
                {
                    "name": r.name,
                    "passed": r.passed,
                    "message": r.message,
                    "details": r.details,
                    "duration_ms": round(r.duration_ms, 2),
                }
                for r in self.results
            ]
        }


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def cosine_similarity(a: List[float], b: List[float]) -> float:
    """Calculate cosine similarity between two vectors."""
    if len(a) != len(b):
        return 0.0
    
    dot_product = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    
    if norm_a < 1e-10 or norm_b < 1e-10:
        return 0.0
    
    return dot_product / (norm_a * norm_b)


def l2_norm(vec: List[float]) -> float:
    """Calculate L2 norm of a vector."""
    return math.sqrt(sum(x * x for x in vec))


def bytes_to_floats(data: bytes) -> List[float]:
    """Convert bytes to list of floats."""
    num_floats = len(data) // 4
    return list(struct.unpack(f'<{num_floats}f', data))


# =============================================================================
# TESTS
# =============================================================================

def test_database_exists() -> TestResult:
    """Test 1: Check if the database file exists."""
    import time
    start = time.time()
    
    db_path = os.path.expanduser("~/.jarvis/learning/jarvis_learning.db")
    
    if os.path.exists(db_path):
        size_kb = os.path.getsize(db_path) / 1024
        return TestResult(
            name="Database Exists",
            passed=True,
            message=f"Database found at {db_path} ({size_kb:.1f} KB)",
            details={"path": db_path, "size_kb": size_kb},
            duration_ms=(time.time() - start) * 1000
        )
    else:
        return TestResult(
            name="Database Exists",
            passed=False,
            message=f"Database not found at {db_path}",
            details={"path": db_path},
            duration_ms=(time.time() - start) * 1000
        )


def test_direct_sqlite_access() -> TestResult:
    """Test 2: Direct SQLite access to speaker_profiles table."""
    import time
    start = time.time()
    
    db_path = os.path.expanduser("~/.jarvis/learning/jarvis_learning.db")
    
    if not os.path.exists(db_path):
        return TestResult(
            name="Direct SQLite Access",
            passed=False,
            message="Database file not found",
            duration_ms=(time.time() - start) * 1000
        )
    
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Check if table exists
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='speaker_profiles'
        """)
        
        if not cursor.fetchone():
            conn.close()
            return TestResult(
                name="Direct SQLite Access",
                passed=False,
                message="speaker_profiles table not found",
                duration_ms=(time.time() - start) * 1000
            )
        
        # Get profiles
        cursor.execute("""
            SELECT 
                speaker_name,
                voiceprint_embedding,
                embedding_dimension,
                total_samples,
                recognition_confidence,
                is_primary_user
            FROM speaker_profiles
            WHERE voiceprint_embedding IS NOT NULL
        """)
        
        rows = cursor.fetchall()
        conn.close()
        
        if not rows:
            return TestResult(
                name="Direct SQLite Access",
                passed=False,
                message="No profiles with embeddings found",
                duration_ms=(time.time() - start) * 1000
            )
        
        profiles = []
        for row in rows:
            emb_bytes = row["voiceprint_embedding"]
            embedding = bytes_to_floats(emb_bytes) if emb_bytes else []
            
            profiles.append({
                "speaker_name": row["speaker_name"],
                "embedding_dim": len(embedding),
                "stored_dim": row["embedding_dimension"],
                "samples": row["total_samples"],
                "confidence": row["recognition_confidence"],
                "is_primary": bool(row["is_primary_user"]),
                "norm": l2_norm(embedding) if embedding else 0,
            })
        
        return TestResult(
            name="Direct SQLite Access",
            passed=True,
            message=f"Found {len(profiles)} profile(s) in database",
            details={"profiles": profiles},
            duration_ms=(time.time() - start) * 1000
        )
        
    except Exception as e:
        return TestResult(
            name="Direct SQLite Access",
            passed=False,
            message=f"SQLite error: {e}",
            duration_ms=(time.time() - start) * 1000
        )


def test_speaker_profile_store() -> TestResult:
    """Test 3: Test the lightweight SpeakerProfileStore."""
    import time
    start = time.time()
    
    try:
        from intelligence.speaker_profile_store import (
            get_speaker_profile_store,
            SpeakerProfile,
        )
        
        store = get_speaker_profile_store()
        
        if not store.db_exists:
            return TestResult(
                name="SpeakerProfileStore",
                passed=False,
                message="Database not found",
                duration_ms=(time.time() - start) * 1000
            )
        
        profiles = store.get_all_profiles(use_cache=False)
        
        if not profiles:
            return TestResult(
                name="SpeakerProfileStore",
                passed=False,
                message="No profiles loaded",
                duration_ms=(time.time() - start) * 1000
            )
        
        profile_info = []
        for p in profiles:
            profile_info.append({
                "name": p.speaker_name,
                "valid": p.is_valid,
                "normalized": p.is_normalized,
                "dim": p.embedding_dimension,
                "norm": round(p.embedding_norm, 6),
                "is_primary": p.is_primary_user,
            })
        
        return TestResult(
            name="SpeakerProfileStore",
            passed=True,
            message=f"Loaded {len(profiles)} profile(s) via SpeakerProfileStore",
            details={"profiles": profile_info},
            duration_ms=(time.time() - start) * 1000
        )
        
    except Exception as e:
        return TestResult(
            name="SpeakerProfileStore",
            passed=False,
            message=f"Error: {e}",
            duration_ms=(time.time() - start) * 1000
        )


def test_embedding_quality() -> TestResult:
    """Test 4: Verify embedding quality (normalized, no NaN/Inf)."""
    import time
    start = time.time()
    
    try:
        from intelligence.speaker_profile_store import get_speaker_profile_store
        
        store = get_speaker_profile_store()
        profiles = store.get_all_profiles()
        
        if not profiles:
            return TestResult(
                name="Embedding Quality",
                passed=False,
                message="No profiles to test",
                duration_ms=(time.time() - start) * 1000
            )
        
        issues = []
        for p in profiles:
            if not p.embedding:
                issues.append(f"{p.speaker_name}: Empty embedding")
                continue
            
            # Check for NaN/Inf
            has_nan = any(math.isnan(x) for x in p.embedding)
            has_inf = any(math.isinf(x) for x in p.embedding)
            
            if has_nan:
                issues.append(f"{p.speaker_name}: Contains NaN values")
            if has_inf:
                issues.append(f"{p.speaker_name}: Contains Inf values")
            
            # Check normalization
            if not p.is_normalized:
                issues.append(
                    f"{p.speaker_name}: Not normalized (norm={p.embedding_norm:.4f})"
                )
            
            # Check dimension
            if p.embedding_dimension != 192:
                issues.append(
                    f"{p.speaker_name}: Unusual dimension ({p.embedding_dimension})"
                )
        
        if issues:
            return TestResult(
                name="Embedding Quality",
                passed=False,
                message=f"Found {len(issues)} issue(s)",
                details={"issues": issues},
                duration_ms=(time.time() - start) * 1000
            )
        
        return TestResult(
            name="Embedding Quality",
            passed=True,
            message=f"All {len(profiles)} embedding(s) are valid and normalized",
            duration_ms=(time.time() - start) * 1000
        )
        
    except Exception as e:
        return TestResult(
            name="Embedding Quality",
            passed=False,
            message=f"Error: {e}",
            duration_ms=(time.time() - start) * 1000
        )


def test_cosine_similarity_math() -> TestResult:
    """Test 5: Verify cosine similarity calculation works correctly."""
    import time
    start = time.time()
    
    try:
        from intelligence.speaker_profile_store import get_speaker_profile_store
        
        store = get_speaker_profile_store()
        profile = store.get_primary_user()
        
        if not profile:
            return TestResult(
                name="Cosine Similarity Math",
                passed=False,
                message="No profile to test with",
                duration_ms=(time.time() - start) * 1000
            )
        
        emb = profile.embedding
        
        # Test 1: Same embedding should give 1.0
        sim_same = cosine_similarity(emb, emb)
        test1_pass = abs(sim_same - 1.0) < 0.0001
        
        # Test 2: Slightly perturbed should give high similarity
        perturbed = [x + 0.01 for x in emb]
        sim_perturbed = cosine_similarity(emb, perturbed)
        test2_pass = sim_perturbed > 0.95
        
        # Test 3: Random embedding should give low similarity
        random.seed(42)
        random_emb = [random.gauss(0, 0.1) for _ in range(len(emb))]
        sim_random = cosine_similarity(emb, random_emb)
        test3_pass = sim_random < 0.5
        
        # Test 4: Orthogonal should give ~0
        orthogonal = [0.0] * len(emb)
        if len(emb) >= 2:
            orthogonal[0] = 1.0
        sim_orthogonal = cosine_similarity(emb, orthogonal)
        # Note: This might not be exactly 0 depending on the embedding
        
        all_pass = test1_pass and test2_pass and test3_pass
        
        details = {
            "same_embedding": {"similarity": round(sim_same, 6), "passed": test1_pass},
            "perturbed": {"similarity": round(sim_perturbed, 6), "passed": test2_pass},
            "random": {"similarity": round(sim_random, 6), "passed": test3_pass},
        }
        
        if all_pass:
            return TestResult(
                name="Cosine Similarity Math",
                passed=True,
                message="All similarity calculations correct",
                details=details,
                duration_ms=(time.time() - start) * 1000
            )
        else:
            return TestResult(
                name="Cosine Similarity Math",
                passed=False,
                message="Some similarity tests failed",
                details=details,
                duration_ms=(time.time() - start) * 1000
            )
        
    except Exception as e:
        return TestResult(
            name="Cosine Similarity Math",
            passed=False,
            message=f"Error: {e}",
            duration_ms=(time.time() - start) * 1000
        )


def test_learning_database_import() -> TestResult:
    """Test 6: Test if learning_database can be imported (with lazy imports)."""
    import time
    start = time.time()
    
    try:
        # This should work with lazy imports even without numpy
        from intelligence.learning_database import get_learning_database
        
        return TestResult(
            name="Learning Database Import",
            passed=True,
            message="learning_database module imported successfully",
            duration_ms=(time.time() - start) * 1000
        )
        
    except ImportError as e:
        # Check if it's a missing dependency
        missing_dep = str(e)
        return TestResult(
            name="Learning Database Import",
            passed=False,
            message=f"Import failed: {missing_dep}",
            details={"error": str(e), "type": "ImportError"},
            duration_ms=(time.time() - start) * 1000
        )
        
    except Exception as e:
        return TestResult(
            name="Learning Database Import",
            passed=False,
            message=f"Error: {e}",
            duration_ms=(time.time() - start) * 1000
        )


async def test_learning_database_profiles() -> TestResult:
    """Test 7: Test get_all_speaker_profiles from learning database."""
    import time
    start = time.time()
    
    try:
        from intelligence.learning_database import get_learning_database
        
        db = await get_learning_database()
        profiles = await db.get_all_speaker_profiles()
        
        if not profiles:
            return TestResult(
                name="Learning Database Profiles",
                passed=False,
                message="No profiles returned from learning database",
                duration_ms=(time.time() - start) * 1000
            )
        
        profile_info = []
        for p in profiles:
            name = p.get("speaker_name", "Unknown")
            emb = p.get("embedding")
            vp = p.get("voiceprint_embedding")
            
            info = {
                "name": name,
                "has_embedding": emb is not None,
                "embedding_type": type(emb).__name__ if emb else None,
                "has_voiceprint": vp is not None,
                "voiceprint_type": type(vp).__name__ if vp else None,
            }
            
            if isinstance(emb, list):
                info["embedding_len"] = len(emb)
                info["embedding_norm"] = round(l2_norm(emb), 6)
            
            profile_info.append(info)
        
        # Check if embedding is properly converted
        first_profile = profiles[0]
        emb = first_profile.get("embedding")
        
        if isinstance(emb, list) and len(emb) > 0:
            return TestResult(
                name="Learning Database Profiles",
                passed=True,
                message=f"Loaded {len(profiles)} profile(s) with correct embedding format",
                details={"profiles": profile_info},
                duration_ms=(time.time() - start) * 1000
            )
        elif isinstance(emb, bytes):
            return TestResult(
                name="Learning Database Profiles",
                passed=False,
                message="Embedding is bytes instead of list - conversion not working",
                details={"profiles": profile_info},
                duration_ms=(time.time() - start) * 1000
            )
        else:
            return TestResult(
                name="Learning Database Profiles",
                passed=False,
                message=f"Unexpected embedding type: {type(emb)}",
                details={"profiles": profile_info},
                duration_ms=(time.time() - start) * 1000
            )
        
    except ImportError as e:
        return TestResult(
            name="Learning Database Profiles",
            passed=False,
            message=f"Import failed (missing dependency): {e}",
            details={"error": str(e), "suggestion": "Install numpy: pip install numpy"},
            duration_ms=(time.time() - start) * 1000
        )
        
    except Exception as e:
        return TestResult(
            name="Learning Database Profiles",
            passed=False,
            message=f"Error: {e}",
            duration_ms=(time.time() - start) * 1000
        )


def test_voice_profile_startup_service() -> TestResult:
    """Test 8: Test VoiceProfileStartupService (if available)."""
    import time
    start = time.time()
    
    try:
        from voice_unlock.voice_profile_startup_service import (
            get_voice_profile_service,
            is_voice_profile_ready,
        )
        
        service = get_voice_profile_service()
        
        if is_voice_profile_ready():
            count = service.profile_count
            return TestResult(
                name="VoiceProfileStartupService",
                passed=True,
                message=f"Service ready with {count} profile(s)",
                details={
                    "is_ready": True,
                    "profile_count": count,
                    "metrics": service.metrics.to_dict(),
                },
                duration_ms=(time.time() - start) * 1000
            )
        else:
            return TestResult(
                name="VoiceProfileStartupService",
                passed=False,
                message="Service not ready (profiles not loaded)",
                details={"is_ready": False},
                duration_ms=(time.time() - start) * 1000
            )
        
    except ImportError as e:
        return TestResult(
            name="VoiceProfileStartupService",
            passed=False,
            message=f"Import failed: {e}",
            details={"error": str(e)},
            duration_ms=(time.time() - start) * 1000
        )
        
    except Exception as e:
        return TestResult(
            name="VoiceProfileStartupService",
            passed=False,
            message=f"Error: {e}",
            duration_ms=(time.time() - start) * 1000
        )


# =============================================================================
# MAIN
# =============================================================================

async def run_tests(verbose: bool = False, output_json: bool = False) -> TestSuite:
    """Run all tests and return results."""
    suite = TestSuite()
    suite.start_time = datetime.now()
    
    # Sync tests
    sync_tests = [
        test_database_exists,
        test_direct_sqlite_access,
        test_speaker_profile_store,
        test_embedding_quality,
        test_cosine_similarity_math,
        test_learning_database_import,
        test_voice_profile_startup_service,
    ]
    
    for test_fn in sync_tests:
        result = test_fn()
        suite.add(result)
        
        if not output_json:
            if result.passed:
                print_ok(f"{result.name}: {result.message}")
            else:
                print_err(f"{result.name}: {result.message}")
            
            if verbose and result.details:
                for key, value in result.details.items():
                    print(f"      {key}: {value}")
    
    # Async tests
    async_tests = [
        test_learning_database_profiles,
    ]
    
    for test_fn in async_tests:
        result = await test_fn()
        suite.add(result)
        
        if not output_json:
            if result.passed:
                print_ok(f"{result.name}: {result.message}")
            else:
                print_err(f"{result.name}: {result.message}")
            
            if verbose and result.details:
                for key, value in result.details.items():
                    print(f"      {key}: {value}")
    
    suite.end_time = datetime.now()
    return suite


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Speaker Verification Test Suite"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Show detailed output"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON"
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output"
    )
    
    args = parser.parse_args()
    
    if args.no_color or args.json:
        Colors.disable()
    
    if not args.json:
        print_header("🔐 SPEAKER VERIFICATION TEST SUITE")
    
    # Run tests
    suite = asyncio.run(run_tests(verbose=args.verbose, output_json=args.json))
    
    if args.json:
        print(json.dumps(suite.to_dict(), indent=2, default=str))
    else:
        # Print summary
        print_header("📋 SUMMARY")
        
        print(f"Tests Run: {suite.total}")
        print(f"Passed:    {Colors.GREEN}{suite.passed}{Colors.END}")
        print(f"Failed:    {Colors.RED}{suite.failed}{Colors.END}")
        print(f"Duration:  {suite.duration_ms:.0f}ms")
        print()
        
        if suite.all_passed:
            print_ok("All tests passed! Speaker verification should work correctly.")
        else:
            print_err(f"{suite.failed} test(s) failed - see details above.")
            print()
            print_info("Common fixes:")
            print("  1. Ensure ~/.jarvis/learning/jarvis_learning.db exists")
            print("  2. Run voice enrollment to create a profile")
            print("  3. Install dependencies: pip install numpy")
            print("  4. Restart JARVIS: python3 start_system.py --restart")
        
        print()
    
    # Exit with error code if tests failed
    sys.exit(0 if suite.all_passed else 1)


if __name__ == "__main__":
    main()
