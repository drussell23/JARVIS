#!/usr/bin/env python3
"""
JARVIS Log Analyzer - CLI Tool
================================

Powerful CLI tool for analyzing JARVIS structured JSON logs.

Features:
- Query logs by level, time range, module, message pattern
- Real-time log tailing with filtering
- Error pattern analysis and aggregation
- Performance metrics and slow operation detection
- Export filtered logs to CSV/JSON
- Generate summary reports

Usage:
    # Show recent errors
    python tools/analyze_logs.py errors --last 1h

    # Tail logs in real-time
    python tools/analyze_logs.py tail --level ERROR

    # Show error statistics
    python tools/analyze_logs.py stats --errors

    # Show performance metrics
    python tools/analyze_logs.py stats --performance

    # Query logs by pattern
    python tools/analyze_logs.py query --module supervisor --message "failed"

    # Export logs to CSV
    python tools/analyze_logs.py export --format csv --output /tmp/logs.csv

    # Generate summary report
    python tools/analyze_logs.py report --last 24h
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
import time

# =============================================================================
# CONFIGURATION
# =============================================================================

DEFAULT_LOG_DIR = Path.home() / ".jarvis" / "logs"


# =============================================================================
# LOG PARSER
# =============================================================================

class LogEntry:
    """Represents a parsed log entry."""

    def __init__(self, raw_line: str):
        try:
            self.data = json.loads(raw_line)
            self.timestamp = datetime.fromisoformat(
                self.data["timestamp"].replace("Z", "+00:00")
            )
            self.level = self.data.get("level", "UNKNOWN")
            self.logger = self.data.get("logger", "unknown")
            self.module = self.data.get("module", "unknown")
            self.message = self.data.get("message", "")
            self.exception = self.data.get("exception")
            self.context = self.data.get("context", {})
            self.valid = True
        except (json.JSONDecodeError, KeyError) as e:
            self.valid = False
            self.raw = raw_line
            self.error = str(e)

    def matches_filter(
        self,
        level: Optional[str] = None,
        module: Optional[str] = None,
        message_pattern: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> bool:
        """Check if log entry matches filter criteria."""
        if not self.valid:
            return False

        if level and self.level != level:
            return False

        if module and module.lower() not in self.module.lower():
            return False

        if message_pattern and message_pattern.lower() not in self.message.lower():
            return False

        if start_time and self.timestamp < start_time:
            return False

        if end_time and self.timestamp > end_time:
            return False

        return True

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for export."""
        return self.data if self.valid else {"error": self.error, "raw": self.raw}


class LogReader:
    """Reads and parses JARVIS log files."""

    def __init__(self, log_dir: Path = DEFAULT_LOG_DIR):
        self.log_dir = log_dir

    def get_log_files(self, pattern: str = "*.jsonl") -> List[Path]:
        """Get all log files matching pattern."""
        if not self.log_dir.exists():
            return []
        return sorted(self.log_dir.glob(pattern))

    def read_logs(
        self,
        log_file: Path,
        max_lines: Optional[int] = None,
        reverse: bool = False,
    ) -> List[LogEntry]:
        """Read log entries from file."""
        if not log_file.exists():
            return []

        with open(log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()

        if reverse:
            lines = reversed(lines)

        entries = []
        for line in lines:
            if max_lines and len(entries) >= max_lines:
                break
            entry = LogEntry(line.strip())
            if entry.valid:
                entries.append(entry)

        return entries

    def tail_logs(
        self,
        log_file: Path,
        callback: callable,
        poll_interval: float = 0.5,
    ) -> None:
        """Tail log file in real-time."""
        if not log_file.exists():
            print(f"Log file not found: {log_file}")
            return

        with open(log_file, "r", encoding="utf-8") as f:
            # Seek to end
            f.seek(0, 2)

            try:
                while True:
                    line = f.readline()
                    if line:
                        entry = LogEntry(line.strip())
                        if entry.valid:
                            callback(entry)
                    else:
                        time.sleep(poll_interval)
            except KeyboardInterrupt:
                pass


# =============================================================================
# ANALYZERS
# =============================================================================

class ErrorAnalyzer:
    """Analyzes error patterns in logs."""

    def __init__(self, entries: List[LogEntry]):
        self.entries = [e for e in entries if e.level in ("ERROR", "CRITICAL")]

    def get_error_stats(self) -> Dict[str, Any]:
        """Get error statistics."""
        if not self.entries:
            return {
                "total_errors": 0,
                "error_types": {},
                "errors_by_module": {},
                "errors_over_time": {},
            }

        # Count by error type
        error_types = Counter()
        for entry in self.entries:
            if entry.exception:
                error_types[entry.exception.get("type", "Unknown")] += 1
            else:
                error_types["LoggedError"] += 1

        # Count by module
        errors_by_module = Counter(e.module for e in self.entries)

        # Count by hour
        errors_by_hour = Counter()
        for entry in self.entries:
            hour = entry.timestamp.strftime("%Y-%m-%d %H:00")
            errors_by_hour[hour] += 1

        return {
            "total_errors": len(self.entries),
            "error_types": dict(error_types.most_common(10)),
            "errors_by_module": dict(errors_by_module.most_common(10)),
            "errors_over_time": dict(sorted(errors_by_hour.items())),
        }

    def get_error_patterns(self) -> List[Dict[str, Any]]:
        """Group similar errors together."""
        patterns = defaultdict(list)

        for entry in self.entries:
            # Group by error message (first 100 chars)
            key = entry.message[:100]
            patterns[key].append(entry)

        # Sort by frequency
        sorted_patterns = sorted(
            patterns.items(),
            key=lambda x: len(x[1]),
            reverse=True,
        )

        return [
            {
                "message": msg,
                "count": len(entries),
                "first_seen": min(e.timestamp for e in entries).isoformat(),
                "last_seen": max(e.timestamp for e in entries).isoformat(),
                "affected_modules": list(set(e.module for e in entries)),
                "sample": entries[0].to_dict(),
            }
            for msg, entries in sorted_patterns[:10]
        ]


class PerformanceAnalyzer:
    """Analyzes performance metrics from logs."""

    def __init__(self, entries: List[LogEntry]):
        self.entries = entries

    def get_slow_operations(self, threshold_ms: float = 1000.0) -> List[Dict[str, Any]]:
        """Find operations that exceeded threshold."""
        slow_ops = []

        for entry in self.entries:
            duration = entry.context.get("duration_ms")
            operation = entry.context.get("operation")

            if duration and operation and duration > threshold_ms:
                slow_ops.append({
                    "timestamp": entry.timestamp.isoformat(),
                    "operation": operation,
                    "duration_ms": duration,
                    "module": entry.module,
                    "message": entry.message,
                })

        return sorted(slow_ops, key=lambda x: x["duration_ms"], reverse=True)

    def get_performance_stats(self) -> Dict[str, Any]:
        """Get performance statistics."""
        operations = defaultdict(list)

        for entry in self.entries:
            duration = entry.context.get("duration_ms")
            operation = entry.context.get("operation")

            if duration and operation:
                operations[operation].append(duration)

        stats = {}
        for op, durations in operations.items():
            stats[op] = {
                "count": len(durations),
                "min_ms": min(durations),
                "max_ms": max(durations),
                "avg_ms": sum(durations) / len(durations),
                "p95_ms": sorted(durations)[int(len(durations) * 0.95)]
                if len(durations) > 1
                else durations[0],
            }

        return stats


# =============================================================================
# FORMATTERS
# =============================================================================

class ConsoleFormatter:
    """Formats log entries for console display."""

    COLORS = {
        "DEBUG": "\033[36m",  # Cyan
        "INFO": "\033[32m",  # Green
        "WARNING": "\033[33m",  # Yellow
        "ERROR": "\033[31m",  # Red
        "CRITICAL": "\033[35m",  # Magenta
        "RESET": "\033[0m",
    }

    @classmethod
    def format_entry(cls, entry: LogEntry, show_context: bool = False) -> str:
        """Format single log entry."""
        color = cls.COLORS.get(entry.level, cls.COLORS["RESET"])
        reset = cls.COLORS["RESET"]

        timestamp = entry.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        level = entry.level.ljust(8)
        module = entry.module[:20].ljust(20)

        line = f"{timestamp} {color}{level}{reset} {module} {entry.message}"

        if entry.exception:
            line += f"\n  Exception: {entry.exception['type']}: {entry.exception['message']}"

        if show_context and entry.context:
            line += f"\n  Context: {entry.context}"

        return line


class ReportGenerator:
    """Generates summary reports."""

    @staticmethod
    def generate_text_report(
        entries: List[LogEntry],
        time_range: str,
    ) -> str:
        """Generate text summary report."""
        if not entries:
            return "No log entries found for the specified criteria."

        # Basic stats
        total = len(entries)
        levels = Counter(e.level for e in entries)
        modules = Counter(e.module for e in entries)

        # Time range
        if entries:
            start = min(e.timestamp for e in entries)
            end = max(e.timestamp for e in entries)
            time_span = f"{start.strftime('%Y-%m-%d %H:%M:%S')} to {end.strftime('%Y-%m-%d %H:%M:%S')}"
        else:
            time_span = "N/A"

        # Error analysis
        error_analyzer = ErrorAnalyzer(entries)
        error_stats = error_analyzer.get_error_stats()
        error_patterns = error_analyzer.get_error_patterns()

        # Performance analysis
        perf_analyzer = PerformanceAnalyzer(entries)
        slow_ops = perf_analyzer.get_slow_operations()

        # Build report
        report = []
        report.append("=" * 80)
        report.append("JARVIS LOG ANALYSIS REPORT")
        report.append("=" * 80)
        report.append(f"Time Range: {time_span}")
        report.append(f"Total Entries: {total}")
        report.append("")

        report.append("LOG LEVELS:")
        for level, count in levels.most_common():
            percentage = (count / total) * 100
            report.append(f"  {level.ljust(10)}: {count:5d} ({percentage:5.1f}%)")
        report.append("")

        report.append("TOP MODULES:")
        for module, count in modules.most_common(10):
            percentage = (count / total) * 100
            report.append(f"  {module.ljust(30)}: {count:5d} ({percentage:5.1f}%)")
        report.append("")

        if error_stats["total_errors"] > 0:
            report.append("ERROR SUMMARY:")
            report.append(f"  Total Errors: {error_stats['total_errors']}")
            report.append("")

            report.append("  Top Error Types:")
            for error_type, count in list(error_stats["error_types"].items())[:5]:
                report.append(f"    {error_type}: {count}")
            report.append("")

            report.append("  Top Error Patterns:")
            for i, pattern in enumerate(error_patterns[:5], 1):
                report.append(f"    {i}. {pattern['message'][:60]}")
                report.append(f"       Count: {pattern['count']}, Modules: {', '.join(pattern['affected_modules'][:3])}")
                report.append(f"       First: {pattern['first_seen']}, Last: {pattern['last_seen']}")
            report.append("")

        if slow_ops:
            report.append("SLOW OPERATIONS (>1000ms):")
            for op in slow_ops[:10]:
                report.append(f"  {op['operation']}: {op['duration_ms']:.0f}ms ({op['module']})")
            report.append("")

        report.append("=" * 80)

        return "\n".join(report)


# =============================================================================
# CLI COMMANDS
# =============================================================================

def cmd_errors(args):
    """Show recent errors."""
    reader = LogReader(args.log_dir)
    log_files = reader.get_log_files("*_errors.jsonl")

    if not log_files:
        print(f"No error log files found in {args.log_dir}")
        return

    # Parse time range
    if args.last:
        end_time = datetime.now()
        if args.last.endswith("h"):
            hours = int(args.last[:-1])
            start_time = end_time - timedelta(hours=hours)
        elif args.last.endswith("d"):
            days = int(args.last[:-1])
            start_time = end_time - timedelta(days=days)
        elif args.last.endswith("m"):
            minutes = int(args.last[:-1])
            start_time = end_time - timedelta(minutes=minutes)
        else:
            start_time = None
    else:
        start_time = None
        end_time = None

    # Read errors
    all_entries = []
    for log_file in log_files:
        entries = reader.read_logs(log_file, reverse=True)
        all_entries.extend(entries)

    # Filter by time
    if start_time or end_time:
        all_entries = [
            e
            for e in all_entries
            if e.matches_filter(start_time=start_time, end_time=end_time)
        ]

    # Sort by timestamp (newest first)
    all_entries.sort(key=lambda e: e.timestamp, reverse=True)

    # Limit
    if args.limit:
        all_entries = all_entries[: args.limit]

    # Display
    print(f"Found {len(all_entries)} error(s)")
    print()
    for entry in all_entries:
        print(ConsoleFormatter.format_entry(entry, show_context=True))
        print()


def cmd_tail(args):
    """Tail logs in real-time."""
    reader = LogReader(args.log_dir)

    # Find log file
    if args.module:
        log_file = args.log_dir / f"{args.module}.jsonl"
    else:
        log_files = reader.get_log_files("*.jsonl")
        if not log_files:
            print(f"No log files found in {args.log_dir}")
            return
        log_file = log_files[0]  # Use first file

    print(f"Tailing {log_file}...")
    print(f"Filter: level={args.level or 'ALL'}")
    print()

    def print_entry(entry: LogEntry):
        if args.level and entry.level != args.level:
            return
        print(ConsoleFormatter.format_entry(entry))

    reader.tail_logs(log_file, print_entry)


def cmd_stats(args):
    """Show log statistics."""
    reader = LogReader(args.log_dir)
    log_files = reader.get_log_files("*.jsonl")

    if not log_files:
        print(f"No log files found in {args.log_dir}")
        return

    # Read all logs
    all_entries = []
    for log_file in log_files:
        entries = reader.read_logs(log_file)
        all_entries.extend(entries)

    if args.errors:
        # Error statistics
        analyzer = ErrorAnalyzer(all_entries)
        stats = analyzer.get_error_stats()
        patterns = analyzer.get_error_patterns()

        print("ERROR STATISTICS")
        print("=" * 80)
        print(f"Total Errors: {stats['total_errors']}")
        print()

        print("Error Types:")
        for error_type, count in stats["error_types"].items():
            print(f"  {error_type}: {count}")
        print()

        print("Errors by Module:")
        for module, count in stats["errors_by_module"].items():
            print(f"  {module}: {count}")
        print()

        print("Top Error Patterns:")
        for i, pattern in enumerate(patterns, 1):
            print(f"{i}. {pattern['message'][:60]}")
            print(f"   Count: {pattern['count']}, Modules: {', '.join(pattern['affected_modules'])}")
            print()

    if args.performance:
        # Performance statistics
        analyzer = PerformanceAnalyzer(all_entries)
        stats = analyzer.get_performance_stats()
        slow_ops = analyzer.get_slow_operations()

        print("PERFORMANCE STATISTICS")
        print("=" * 80)

        print("Operations:")
        for op, metrics in sorted(stats.items(), key=lambda x: x[1]["avg_ms"], reverse=True)[:20]:
            print(f"  {op}:")
            print(f"    Count: {metrics['count']}, Avg: {metrics['avg_ms']:.2f}ms, "
                  f"P95: {metrics['p95_ms']:.2f}ms, Max: {metrics['max_ms']:.2f}ms")
        print()

        if slow_ops:
            print(f"Slow Operations (>{1000}ms):")
            for op in slow_ops[:10]:
                print(f"  {op['operation']}: {op['duration_ms']:.0f}ms at {op['timestamp']}")
            print()


def cmd_query(args):
    """Query logs with filters."""
    reader = LogReader(args.log_dir)
    log_files = reader.get_log_files("*.jsonl")

    if not log_files:
        print(f"No log files found in {args.log_dir}")
        return

    # Read all logs
    all_entries = []
    for log_file in log_files:
        entries = reader.read_logs(log_file)
        all_entries.extend(entries)

    # Filter
    filtered = [
        e
        for e in all_entries
        if e.matches_filter(
            level=args.level,
            module=args.module,
            message_pattern=args.message,
        )
    ]

    # Sort and limit
    filtered.sort(key=lambda e: e.timestamp, reverse=True)
    if args.limit:
        filtered = filtered[: args.limit]

    # Display
    print(f"Found {len(filtered)} matching entries")
    print()
    for entry in filtered:
        print(ConsoleFormatter.format_entry(entry, show_context=args.context))


def cmd_report(args):
    """Generate summary report."""
    reader = LogReader(args.log_dir)
    log_files = reader.get_log_files("*.jsonl")

    if not log_files:
        print(f"No log files found in {args.log_dir}")
        return

    # Parse time range
    if args.last:
        end_time = datetime.now()
        if args.last.endswith("h"):
            hours = int(args.last[:-1])
            start_time = end_time - timedelta(hours=hours)
        elif args.last.endswith("d"):
            days = int(args.last[:-1])
            start_time = end_time - timedelta(days=days)
        else:
            start_time = None
    else:
        start_time = None
        end_time = None

    # Read all logs
    all_entries = []
    for log_file in log_files:
        entries = reader.read_logs(log_file)
        all_entries.extend(entries)

    # Filter by time
    if start_time or end_time:
        all_entries = [
            e
            for e in all_entries
            if e.matches_filter(start_time=start_time, end_time=end_time)
        ]

    # Generate report
    report = ReportGenerator.generate_text_report(all_entries, args.last or "all time")

    if args.output:
        with open(args.output, "w") as f:
            f.write(report)
        print(f"Report saved to {args.output}")
    else:
        print(report)


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="JARVIS Log Analyzer - Analyze structured JSON logs"
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=DEFAULT_LOG_DIR,
        help=f"Log directory (default: {DEFAULT_LOG_DIR})",
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # errors command
    errors_parser = subparsers.add_parser("errors", help="Show recent errors")
    errors_parser.add_argument("--last", help="Time range (e.g., 1h, 24h, 7d)")
    errors_parser.add_argument("--limit", type=int, help="Max number of errors to show")

    # tail command
    tail_parser = subparsers.add_parser("tail", help="Tail logs in real-time")
    tail_parser.add_argument("--module", help="Module name to tail")
    tail_parser.add_argument("--level", help="Filter by log level")

    # stats command
    stats_parser = subparsers.add_parser("stats", help="Show log statistics")
    stats_parser.add_argument("--errors", action="store_true", help="Show error statistics")
    stats_parser.add_argument(
        "--performance", action="store_true", help="Show performance statistics"
    )

    # query command
    query_parser = subparsers.add_parser("query", help="Query logs with filters")
    query_parser.add_argument("--level", help="Filter by log level")
    query_parser.add_argument("--module", help="Filter by module name")
    query_parser.add_argument("--message", help="Filter by message pattern")
    query_parser.add_argument("--limit", type=int, default=100, help="Max results")
    query_parser.add_argument("--context", action="store_true", help="Show context")

    # report command
    report_parser = subparsers.add_parser("report", help="Generate summary report")
    report_parser.add_argument("--last", help="Time range (e.g., 1h, 24h, 7d)")
    report_parser.add_argument("--output", type=Path, help="Output file")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # Execute command
    if args.command == "errors":
        cmd_errors(args)
    elif args.command == "tail":
        cmd_tail(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "query":
        cmd_query(args)
    elif args.command == "report":
        cmd_report(args)


if __name__ == "__main__":
    main()
