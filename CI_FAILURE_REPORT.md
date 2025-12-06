# CI/CD Failure Analysis Report

## Executive Summary

- **Workflow**: Complete Unlock Test Suite (Master)
- **Run Number**: #236
- **Branch**: `cursor/investigate-voice-authentication-system-integration-gemini-3-pro-preview-e56d`
- **Commit**: `d4047c9067920a1dffef2b81142a39b3218d414c`
- **Status**: ❌ FAILED
- **Timestamp**: 2025-12-06T06:06:21Z
- **Triggered By**: @cursor[bot]
- **Workflow URL**: [View Run](https://github.com/drussell23/JARVIS/actions/runs/19984382024)

## Failure Overview

Total Failed Jobs: **22**

| # | Job Name | Category | Severity | Duration |
|---|----------|----------|----------|----------|
| 1 | Run Unlock Integration E2E / Mock Tests - security-checks | test_failure | high | 40s |
| 2 | Run Biometric Voice E2E / Mock Biometric Tests - embedding-validation | timeout | high | 64s |
| 3 | Run Biometric Voice E2E / Mock Biometric Tests - wake-word-detection | timeout | high | 59s |
| 4 | Run Biometric Voice E2E / Mock Biometric Tests - voice-verification | timeout | high | 41s |
| 5 | Run Biometric Voice E2E / Mock Biometric Tests - stt-transcription | timeout | high | 58s |
| 6 | Run Biometric Voice E2E / Mock Biometric Tests - edge-case-voice-drift | timeout | high | 50s |
| 7 | Run Biometric Voice E2E / Mock Biometric Tests - profile-quality-assessment | timeout | high | 38s |
| 8 | Run Biometric Voice E2E / Mock Biometric Tests - anti-spoofing | timeout | high | 41s |
| 9 | Run Biometric Voice E2E / Mock Biometric Tests - edge-case-noise | timeout | high | 59s |
| 10 | Run Biometric Voice E2E / Mock Biometric Tests - dimension-adaptation | timeout | high | 43s |
| 11 | Run Biometric Voice E2E / Mock Biometric Tests - edge-case-cold-start | timeout | high | 41s |
| 12 | Run Biometric Voice E2E / Mock Biometric Tests - replay-attack-detection | timeout | high | 48s |
| 13 | Run Biometric Voice E2E / Mock Biometric Tests - adaptive-thresholds | timeout | high | 43s |
| 14 | Run Biometric Voice E2E / Mock Biometric Tests - end-to-end-flow | timeout | high | 48s |
| 15 | Run Biometric Voice E2E / Mock Biometric Tests - security-validation | timeout | high | 46s |
| 16 | Run Biometric Voice E2E / Mock Biometric Tests - edge-case-database-failure | test_failure | high | 43s |
| 17 | Run Biometric Voice E2E / Mock Biometric Tests - performance-baseline | timeout | high | 51s |
| 18 | Run Biometric Voice E2E / Mock Biometric Tests - voice-synthesis-detection | timeout | high | 59s |
| 19 | Run Unlock Integration E2E / Generate Test Summary | test_failure | high | 6s |
| 20 | Run Biometric Voice E2E / Generate Biometric Test Summary | test_failure | high | 11s |
| 21 | Generate Combined Test Summary | test_failure | high | 4s |
| 22 | Notify Test Status | test_failure | high | 3s |

## Detailed Analysis

### 1. Run Unlock Integration E2E / Mock Tests - security-checks

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-12-06T06:09:38Z
**Completed**: 2025-12-06T06:10:18Z
**Duration**: 40 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984382024/job/57316158272)

#### Failed Steps

- **Step 6**: Run Mock Tests

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 39: `2025-12-06T06:10:15.7587400Z 2025-12-06 06:10:15,758 - __main__ - ERROR - ❌ 1 test(s) failed`
    - Line 56: `2025-12-06T06:10:15.7755190Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 3
  - Sample matches:
    - Line 39: `2025-12-06T06:10:15.7587400Z 2025-12-06 06:10:15,758 - __main__ - ERROR - ❌ 1 test(s) failed`
    - Line 48: `2025-12-06T06:10:15.7595699Z ❌ Failed: 1`
    - Line 97: `2025-12-06T06:10:16.5951341Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 2
  - Sample matches:
    - Line 62: `2025-12-06T06:10:15.7832641Z   if-no-files-found: warn`
    - Line 97: `2025-12-06T06:10:16.5951341Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations
2. Review test cases and ensure code changes haven't broken existing functionality

---

### 2. Run Biometric Voice E2E / Mock Biometric Tests - embedding-validation

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:10:07Z
**Completed**: 2025-12-06T06:11:11Z
**Duration**: 64 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984382024/job/57316213421)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:11:08.2191556Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:11:08.2200420Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:11:08.2543548Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:11:08.6258537Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:11:08.2646692Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:11:08.4762558Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:11:08.6258537Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:10:59.1967148Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:11:00.8242410Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:11:00.9833630Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:10:59.3944047Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:10:59.3989005Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:11:00.2844376Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 3. Run Biometric Voice E2E / Mock Biometric Tests - wake-word-detection

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:10:17Z
**Completed**: 2025-12-06T06:11:16Z
**Duration**: 59 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984382024/job/57316213423)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:11:13.9098323Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:11:13.9107733Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:11:13.9450188Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:11:14.3137862Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:11:13.9550259Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:11:14.1653106Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:11:14.3137862Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:11:04.6263338Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:11:06.0608913Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:11:06.2175952Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:11:04.8256114Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:11:04.8292311Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:11:05.6959253Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 4. Run Biometric Voice E2E / Mock Biometric Tests - voice-verification

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:10:06Z
**Completed**: 2025-12-06T06:10:47Z
**Duration**: 41 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984382024/job/57316213424)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:10:45.6389928Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:10:45.6400895Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:10:45.6835407Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:10:46.0764545Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:10:45.6944707Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:10:45.9230510Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:10:46.0764545Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:10:36.2673678Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:10:37.7176906Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:10:37.8833547Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:10:36.4755341Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:10:36.4797255Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:10:37.3688800Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 5. Run Biometric Voice E2E / Mock Biometric Tests - stt-transcription

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:10:10Z
**Completed**: 2025-12-06T06:11:08Z
**Duration**: 58 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984382024/job/57316213426)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:11:05.0297123Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:11:05.0306163Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:11:05.0677981Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:11:05.4405494Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:11:05.0777676Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:11:05.2890727Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:11:05.4405494Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:10:55.9531238Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:10:57.3780163Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:10:57.5414579Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:10:56.1594236Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:10:56.1626970Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:10:57.0334820Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 6. Run Biometric Voice E2E / Mock Biometric Tests - edge-case-voice-drift

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:10:08Z
**Completed**: 2025-12-06T06:10:58Z
**Duration**: 50 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984382024/job/57316213440)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:10:56.0511096Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:10:56.0521691Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:10:56.0943231Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:10:56.4829222Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:10:56.1048175Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:10:56.3271390Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:10:56.4829222Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:10:45.8451732Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:10:48.0305573Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:10:48.2008318Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:10:46.2351992Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:10:46.2546040Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:10:47.4728269Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 7. Run Biometric Voice E2E / Mock Biometric Tests - profile-quality-assessment

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:10:07Z
**Completed**: 2025-12-06T06:10:45Z
**Duration**: 38 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984382024/job/57316213446)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:10:43.4772446Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:10:43.4783735Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:10:43.5238407Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:10:43.9039212Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:10:43.5340419Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:10:43.7506116Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:10:43.9039212Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:10:34.2206126Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:10:36.0010424Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:10:36.1613563Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:10:34.4695691Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:10:34.4770509Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:10:35.3931551Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 8. Run Biometric Voice E2E / Mock Biometric Tests - anti-spoofing

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:10:07Z
**Completed**: 2025-12-06T06:10:48Z
**Duration**: 41 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984382024/job/57316213448)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:10:46.4226031Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:10:46.4251423Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:10:46.4659509Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:10:46.8416948Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:10:46.4764452Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:10:46.6903629Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:10:46.8416948Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:10:36.8821124Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:10:38.6896416Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:10:38.8502308Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:10:37.1359913Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:10:37.1437150Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:10:38.0463131Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 9. Run Biometric Voice E2E / Mock Biometric Tests - edge-case-noise

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:10:12Z
**Completed**: 2025-12-06T06:11:11Z
**Duration**: 59 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984382024/job/57316213450)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:11:08.0438115Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:11:08.0448080Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:11:08.0845331Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:11:08.4578881Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:11:08.0949803Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:11:08.3070418Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:11:08.4578881Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:10:59.1811774Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:11:00.7224000Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:11:00.8802818Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:10:59.4139536Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:10:59.4190771Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:11:00.3250733Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 10. Run Biometric Voice E2E / Mock Biometric Tests - dimension-adaptation

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:10:08Z
**Completed**: 2025-12-06T06:10:51Z
**Duration**: 43 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984382024/job/57316213451)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:10:49.4293715Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:10:49.4304007Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:10:49.4684004Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:10:49.8396257Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:10:49.4786195Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:10:49.6904610Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:10:49.8396257Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:10:40.4612880Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:10:41.8434082Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:10:42.0000379Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:10:40.6517563Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:10:40.6565014Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:10:41.5374030Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 11. Run Biometric Voice E2E / Mock Biometric Tests - edge-case-cold-start

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:10:07Z
**Completed**: 2025-12-06T06:10:48Z
**Duration**: 41 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984382024/job/57316213453)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:10:46.9719841Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:10:46.9729975Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:10:47.0114517Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:10:47.3565486Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:10:47.0214132Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:10:47.2230707Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:10:47.3565486Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:10:38.5225767Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:10:39.6711765Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:10:39.8399881Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:10:38.7022604Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:10:38.7053894Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:10:39.4604194Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 12. Run Biometric Voice E2E / Mock Biometric Tests - replay-attack-detection

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:10:07Z
**Completed**: 2025-12-06T06:10:55Z
**Duration**: 48 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984382024/job/57316213455)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:10:52.9651719Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:10:52.9660229Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:10:53.0027733Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:10:53.3890964Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:10:53.0132824Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:10:53.2305585Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:10:53.3890964Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:10:43.0699538Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:10:45.5228542Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:10:45.6838067Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:10:43.3534569Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:10:43.3653474Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:10:44.3084383Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 13. Run Biometric Voice E2E / Mock Biometric Tests - adaptive-thresholds

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:10:20Z
**Completed**: 2025-12-06T06:11:03Z
**Duration**: 43 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984382024/job/57316213459)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:11:01.4184828Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:11:01.4194927Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:11:01.4596124Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:11:01.8505273Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:11:01.4714711Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:11:01.6915852Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:11:01.8505273Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:10:51.4978338Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:10:53.6134028Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:10:53.7795785Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:10:51.7451559Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:10:51.7528923Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:10:52.6480433Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 14. Run Biometric Voice E2E / Mock Biometric Tests - end-to-end-flow

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:10:11Z
**Completed**: 2025-12-06T06:10:59Z
**Duration**: 48 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984382024/job/57316213467)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:10:57.7540127Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:10:57.7549114Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:10:57.7942442Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:10:58.1485332Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:10:57.8041745Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:10:58.0099692Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:10:58.1485332Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:10:48.6670800Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:10:50.7132026Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:10:50.8810165Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:10:49.0450693Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:10:49.0659510Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:10:49.9149811Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 15. Run Biometric Voice E2E / Mock Biometric Tests - security-validation

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:10:13Z
**Completed**: 2025-12-06T06:10:59Z
**Duration**: 46 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984382024/job/57316213469)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:10:57.0184323Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:10:57.0194012Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:10:57.0601710Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:10:57.4110796Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:10:57.0702378Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:10:57.2726362Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:10:57.4110796Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:10:47.9768337Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:10:50.0404367Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:10:50.2084520Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:10:48.3392398Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:10:48.3583493Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:10:49.2129216Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 16. Run Biometric Voice E2E / Mock Biometric Tests - edge-case-database-failure

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-12-06T06:10:58Z
**Completed**: 2025-12-06T06:11:41Z
**Duration**: 43 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984382024/job/57316213472)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:11:39.4208430Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:11:39.4219418Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:11:39.4623264Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 2
  - Sample matches:
    - Line 69: `2025-12-06T06:11:39.4726306Z   name: test-results-biometric-mock-edge-case-database-failure`
    - Line 97: `2025-12-06T06:11:39.8327510Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:11:39.4727122Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:11:39.6845034Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:11:39.8327510Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:11:30.5628608Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:11:32.0661207Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:11:32.2235847Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:11:30.7771961Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:11:30.7912410Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:11:31.6574669Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 17. Run Biometric Voice E2E / Mock Biometric Tests - performance-baseline

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:10:59Z
**Completed**: 2025-12-06T06:11:50Z
**Duration**: 51 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984382024/job/57316213473)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:11:48.4857885Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:11:48.4867550Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:11:48.5221209Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:11:48.8955080Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:11:48.5323565Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:11:48.7451261Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:11:48.8955080Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:11:38.1260688Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:11:40.6623487Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:11:40.8276628Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:11:38.4461094Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:11:38.4750222Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:11:39.8432355Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 18. Run Biometric Voice E2E / Mock Biometric Tests - voice-synthesis-detection

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2025-12-06T06:10:51Z
**Completed**: 2025-12-06T06:11:50Z
**Duration**: 59 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984382024/job/57316213483)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 64: `2025-12-06T06:11:48.0174315Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 65: `2025-12-06T06:11:48.0183815Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 66: `2025-12-06T06:11:48.0538962Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:11:48.4311156Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 3
  - Sample matches:
    - Line 72: `2025-12-06T06:11:48.0644253Z   if-no-files-found: warn`
    - Line 87: `2025-12-06T06:11:48.2786652Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 97: `2025-12-06T06:11:48.4311156Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `AssertionError|Exception`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2025-12-06T06:11:38.6629654Z   Using cached exceptiongroup-1.3.1-py3-none-any.whl.metadata (6.7 kB)`
    - Line 52: `2025-12-06T06:11:40.1556327Z Using cached exceptiongroup-1.3.1-py3-none-any.whl (16 kB)`
    - Line 61: `2025-12-06T06:11:40.3186750Z Installing collected packages: typing-extensions, tomli, pygments, prop`

- Pattern: `timeout|timed out`
  - Occurrences: 6
  - Sample matches:
    - Line 19: `2025-12-06T06:11:38.8745560Z Collecting async-timeout<6.0,>=4.0 (from aiohttp)`
    - Line 20: `2025-12-06T06:11:38.8777023Z   Downloading async_timeout-5.0.1-py3-none-any.whl.metadata (5.1 kB)`
    - Line 37: `2025-12-06T06:11:39.7635526Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 19. Run Unlock Integration E2E / Generate Test Summary

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-12-06T06:10:58Z
**Completed**: 2025-12-06T06:11:04Z
**Duration**: 6 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984382024/job/57316222814)

#### Failed Steps

- **Step 4**: Check Test Status

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:11:02.7244450Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 13
  - Sample matches:
    - Line 39: `2025-12-06T06:11:02.4921232Z [36;1mTOTAL_FAILED=0[0m`
    - Line 44: `2025-12-06T06:11:02.4928727Z [36;1m    FAILED=$(jq -r '.summary.failed' "$report")[0m`
    - Line 46: `2025-12-06T06:11:02.4932007Z [36;1m    TOTAL_FAILED=$((TOTAL_FAILED + FAILED))[0m`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 2
  - Sample matches:
    - Line 4: `2025-12-06T06:11:02.2863984Z (node:1996) [DEP0005] DeprecationWarning: Buffer() is deprecated due to`
    - Line 5: `2025-12-06T06:11:02.2870088Z (Use `node --trace-deprecation ...` to show where the warning was creat`

#### Suggested Fixes

1. Review test cases and ensure code changes haven't broken existing functionality

---

### 20. Run Biometric Voice E2E / Generate Biometric Test Summary

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-12-06T06:15:00Z
**Completed**: 2025-12-06T06:15:11Z
**Duration**: 11 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984382024/job/57316275584)

#### Failed Steps

- **Step 4**: Check Test Status

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 1
  - Sample matches:
    - Line 97: `2025-12-06T06:15:10.3905634Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 11
  - Sample matches:
    - Line 32: `2025-12-06T06:15:10.2191475Z [36;1mTOTAL_FAILED=0[0m`
    - Line 37: `2025-12-06T06:15:10.2193139Z [36;1m    FAILED=$(jq -r '.summary.failed' "$report")[0m`
    - Line 39: `2025-12-06T06:15:10.2193862Z [36;1m    TOTAL_FAILED=$((TOTAL_FAILED + FAILED))[0m`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations
2. Review test cases and ensure code changes haven't broken existing functionality

---

### 21. Generate Combined Test Summary

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-12-06T06:15:29Z
**Completed**: 2025-12-06T06:15:33Z
**Duration**: 4 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984382024/job/57316371061)

#### Failed Steps

- **Step 2**: Generate Combined Summary

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 1
  - Sample matches:
    - Line 83: `2025-12-06T06:15:31.5973167Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 9
  - Sample matches:
    - Line 47: `2025-12-06T06:15:31.5773443Z [36;1mif [ "failure" = "success" ]; then[0m`
    - Line 50: `2025-12-06T06:15:31.5775502Z [36;1m  echo "- ❌ **Unlock Integration E2E:** failure" >> $GITHUB_STEP`
    - Line 54: `2025-12-06T06:15:31.5777417Z [36;1mif [ "failure" = "success" ]; then[0m`

#### Suggested Fixes

1. Review test cases and ensure code changes haven't broken existing functionality

---

### 22. Notify Test Status

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2025-12-06T06:15:41Z
**Completed**: 2025-12-06T06:15:44Z
**Duration**: 3 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/19984382024/job/57316381772)

#### Failed Steps

- **Step 3**: Failure Notification

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 1
  - Sample matches:
    - Line -1: `2025-12-06T06:15:42.5432421Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 3
  - Sample matches:
    - Line -9: `2025-12-06T06:15:42.1380028Z ##[group]Run echo "❌ Unlock tests failed - 'unlock my screen' may be br`
    - Line -8: `2025-12-06T06:15:42.1381741Z [36;1mecho "❌ Unlock tests failed - 'unlock my screen' may be broken!"`
    - Line -3: `2025-12-06T06:15:42.5412665Z ❌ Unlock tests failed - 'unlock my screen' may be broken!`

#### Suggested Fixes

1. Review test cases and ensure code changes haven't broken existing functionality

---

## Action Items

- [ ] Review detailed logs for each failed job
- [ ] Implement suggested fixes
- [ ] Add or update tests to prevent regression
- [ ] Verify fixes locally before pushing
- [ ] Update CI/CD configuration if needed

## Additional Resources

- [Workflow File](.github/workflows/)
- [CI/CD Documentation](../../docs/ci-cd/)
- [Troubleshooting Guide](../../docs/troubleshooting/)

---

📊 *Report generated on 2025-12-06T06:18:48.172615*
🤖 *JARVIS CI/CD Auto-PR Manager*
