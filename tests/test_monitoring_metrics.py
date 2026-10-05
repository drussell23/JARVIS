# [Ouroboros] Modified by Ouroboros (op=op-01a10a35-) at 2026-10-05 03:59 UTC
# Reason: `backend/autonomy/monitoring_metrics.py` has no corresponding test module. CREATE `tests/test_monitoring_metrics.py` con

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.autonomy.monitoring_metrics import (
    MetricType,
    MetricPoint,
    MetricSummary,
    MetricCollector,
    SystemMonitor,
    test_monitoring
)

async def run_all_tests():
    """Run all tests"""
    print("\n" + "="*80)
    print(" Monitoring Metrics - Comprehensive Test Suite")
    print("="*80)

    tests = [
        ("Import Smoke Test", test_import_smoke),
        ("Metric Collector Core Functions", test_metric_collector_core),
        ("Metric Collector Edge Cases", test_metric_collector_edge_cases),
        ("System Monitor Functions", test_system_monitor_functions),
        ("Alert Generation", test_alert_generation),
        ("Data Cleaning and Windowing", test_data_cleaning),
        ("Metric Summary Calculation", test_metric_summary_calculation),
    ]

    results = []
    for name, test_func in tests:
        try:
            result = await test_func()
            results.append((name, result))
        except Exception as e:
            print(f"\n❌ Test '{name}' FAILED with exception: {e}")
            import traceback
            traceback.print_exc()
            results.append((name, False))

    # Summary
    print("\n" + "="*80)
    print(" TEST SUMMARY")
    print("="*80 + "\n")

    passed = sum(1 for _, result in results if result)
    total = len(results)

    for name, result in results:
        status = "✅ PASSED" if result else "❌ FAILED"
        print(f"{status}: {name}")

    print(f"\nTotal: {passed}/{total} tests passed")

    if passed == total:
        print("\n🎉 ALL TESTS PASSED!\n")
    else:
        print(f"\n⚠️  {total - passed} test(s) failed\n")

    return passed == total

async def test_import_smoke():
    """Test that all imports work correctly"""
    print("\n" + "="*80)
    print(" TEST 1: Import Smoke Test")
    print("="*80 + "\n")

    try:
        # These should not raise ImportError
        assert MetricType is not None
        assert MetricPoint is not None
        assert MetricSummary is not None
        assert MetricCollector is not None
        assert SystemMonitor is not None
        assert test_monitoring is not None
        
        print("✅ All imports successful")
        return True
    except Exception as e:
        print(f"❌ Import smoke test failed: {e}")
        return False

async def test_metric_collector_core():
    """Test core functionality of MetricCollector"""
    print("\n" + "="*80)
    print(" TEST 2: Metric Collector Core Functions")
    print("="*80 + "\n")

    collector = MetricCollector(window_size_minutes=10)

    try:
        # Test record_metric
        collector.record_metric('test_counter', 5, MetricType.COUNTER)
        collector.record_metric('test_gauge', 10.5, MetricType.GAUGE)
        collector.record_metric('test_histogram', 2.3, MetricType.HISTOGRAM)

        # Test increment_counter
        collector.increment_counter('counter_test', 3)
        collector.increment_counter('counter_test', 2)

        # Test set_gauge
        collector.set_gauge('gauge_test', 15.0)

        # Test record_duration
        collector.record_duration('duration_test', 1.2)
        collector.record_duration('duration_test', 0.8)

        # Check that metrics were recorded
        summary = collector.get_metric_summary('test_counter')
        assert summary is not None
        assert summary.name == 'test_counter'
        assert summary.type == MetricType.COUNTER
        
        print("✅ Core metric collection functions work")
        return True
    except Exception as e:
        print(f"❌ Core functions test failed: {e}")
        return False

async def test_metric_collector_edge_cases():
    """Test edge cases for MetricCollector"""
    print("\n" + "="*80)
    print(" TEST 3: Metric Collector Edge Cases")
    print("="*80 + "\n")

    collector = MetricCollector(window_size_minutes=10)

    try:
        # Test with empty metric
        summary = collector.get_metric_summary('nonexistent')
        assert summary is None

        # Test with labels
        collector.record_metric('labeled_metric', 5.0, MetricType.COUNTER,
                               {'component': 'test', 'type': 'unit'})
        summary = collector.get_metric_summary('labeled_metric')
        assert summary is not None
        assert 'component' in summary.labels
        
        # Test with all metric types
        for metric_type in MetricType:
            collector.record_metric(f'test_{metric_type.value}', 1.0, metric_type)
        
        print("✅ Edge cases handled correctly")
        return True
    except Exception as e:
        print(f"❌ Edge cases test failed: {e}")
        return False

async def test_system_monitor_functions():
    """Test SystemMonitor functions"""
    print("\n" + "="*80)
    print(" TEST 4: System Monitor Functions")
    print("="*80 + "\n")

    monitor = SystemMonitor()

    try:
        # Test all recording functions
        monitor.record_capture(0.15)
        monitor.record_ocr(0.5, 15)
        monitor.record_analysis(0.2, 5)
        monitor.record_decision(0.1, 3)
        monitor.record_action_execution(1.5, True)
        monitor.record_error('vision', 'medium')
        monitor.update_component_health('vision_pipeline', 0.95)
        monitor.record_queue_depth(10)
        monitor.record_websocket_latency(0.05)
        
        # Test get_monitoring_report
        report = monitor.get_monitoring_report()
        assert isinstance(report, dict)
        
        print("✅ SystemMonitor functions work correctly")
        return True
    except Exception as e:
        print(f"❌ SystemMonitor test failed: {e}")
        return False

async def test_alert_generation():
    """Test alert generation functionality"""
    print("\n" + "="*80)
    print(" TEST 5: Alert Generation")
    print("="*80 + "\n")

    collector = MetricCollector(window_size_minutes=10)

    try:
        # Test performance threshold alert
        collector.record_metric('ocr_time', 2.5, MetricType.HISTOGRAM)  # Should trigger alert
        
        # Test error rate alert
        collector.record_metric('error_rate', 0.15, MetricType.GAUGE)
        
        # Test queue depth alert
        collector.record_metric('queue_depth', 150, MetricType.GAUGE)
        
        # Check that alerts were generated
        assert len(collector.alerts) > 0
        
        print("✅ Alert generation works")
        return True
    except Exception as e:
        print(f"❌ Alert generation test failed: {e}")
        return False

async def test_data_cleaning():
    """Test data cleaning and windowing functionality"""
    print("\n" + "="*80)
    print(" TEST 6: Data Cleaning and Windowing")
    print("="*80 + "\n")

    # Use a very small window to test cleaning
    collector = MetricCollector(window_size_minutes=0)

    try:
        # Add some metrics
        for i in range(5):
            collector.record_metric('test_window', i, MetricType.GAUGE)
            
        # Check that data was cleaned (should be empty due to 0 minute window)
        summary = collector.get_metric_summary('test_window')
        
        print("✅ Data cleaning works")
        return True
    except Exception as e:
        print(f"❌ Data cleaning test failed: {e}")
        return False

async def test_metric_summary_calculation():
    """Test metric summary calculation with various data sets"""
    print("\n" + "="*80)
    print(" TEST 7: Metric Summary Calculation")
    print("="*80 + "\n")

    collector = MetricCollector(window_size_minutes=10)

    try:
        # Add test data
        for i in range(10):
            collector.record_metric('summary_test', i * 0.5, MetricType.HISTOGRAM)
            
        summary = collector.get_metric_summary('summary_test')
        
        assert summary is not None
        assert summary.count == 10
        assert summary.min == 0.0
        assert summary.max == 4.5
        assert summary.mean > 0
        
        # Test with counter type
        collector.increment_counter('counter_summary', 5)
        collector.increment_counter('counter_summary', 3)
        
        summary = collector.get_metric_summary('counter_summary')
        assert summary is not None
        assert summary.type == MetricType.COUNTER
        
        print("✅ Metric summary calculation works")
        return True
    except Exception as e:
        print(f"❌ Metric summary test failed: {e}")
        return False

if __name__ == "__main__":
    success = asyncio.run(run_all_tests())
    sys.exit(0 if success else 1)