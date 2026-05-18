# CI/CD Failure Analysis Report

## Executive Summary

- **Workflow**: Priority 2 - Biometric Voice Unlock E2E Testing
- **Run Number**: #534
- **Branch**: `dependabot/pip/backend/numpy-2.4.5`
- **Commit**: `4d70496eb4fd59a33678b95f07edaf6ee372ee81`
- **Status**: ❌ FAILED
- **Timestamp**: 2026-05-18T16:21:47Z
- **Triggered By**: @dependabot[bot]
- **Workflow URL**: [View Run](https://github.com/drussell23/JARVIS/actions/runs/26046023776)

## Failure Overview

Total Failed Jobs: **17**

| # | Job Name | Category | Severity | Duration |
|---|----------|----------|----------|----------|
| 1 | Mock Biometric Tests - voice-verification | timeout | high | 31s |
| 2 | Mock Biometric Tests - embedding-validation | timeout | high | 34s |
| 3 | Mock Biometric Tests - stt-transcription | timeout | high | 30s |
| 4 | Mock Biometric Tests - dimension-adaptation | timeout | high | 33s |
| 5 | Mock Biometric Tests - anti-spoofing | timeout | high | 35s |
| 6 | Mock Biometric Tests - profile-quality-assessment | timeout | high | 36s |
| 7 | Mock Biometric Tests - edge-case-noise | timeout | high | 34s |
| 8 | Mock Biometric Tests - edge-case-cold-start | timeout | high | 32s |
| 9 | Mock Biometric Tests - end-to-end-flow | timeout | high | 38s |
| 10 | Mock Biometric Tests - edge-case-voice-drift | timeout | high | 38s |
| 11 | Mock Biometric Tests - edge-case-database-failure | test_failure | high | 31s |
| 12 | Mock Biometric Tests - wake-word-detection | timeout | high | 30s |
| 13 | Mock Biometric Tests - voice-synthesis-detection | timeout | high | 31s |
| 14 | Mock Biometric Tests - performance-baseline | timeout | high | 35s |
| 15 | Mock Biometric Tests - security-validation | timeout | high | 35s |
| 16 | Mock Biometric Tests - replay-attack-detection | timeout | high | 36s |
| 17 | Mock Biometric Tests - adaptive-thresholds | timeout | high | 35s |

## Detailed Analysis

### 1. Mock Biometric Tests - voice-verification

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2026-05-18T17:23:36Z
**Completed**: 2026-05-18T17:24:07Z
**Duration**: 31 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26046023776/job/76575098145)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 63: `2026-05-18T17:24:05.6136149Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 64: `2026-05-18T17:24:05.6137217Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 65: `2026-05-18T17:24:05.6611647Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 96: `2026-05-18T17:24:06.0613423Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 4
  - Sample matches:
    - Line 71: `2026-05-18T17:24:05.6719597Z   if-no-files-found: warn`
    - Line 86: `2026-05-18T17:24:05.9007207Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 96: `2026-05-18T17:24:06.0613423Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `timeout|timed out`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2026-05-18T17:23:55.8743591Z   Using cached pytest_timeout-2.4.0-py3-none-any.whl.metadata (20 kB)`
    - Line 40: `2026-05-18T17:23:57.3308215Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`
    - Line 60: `2026-05-18T17:23:57.6430065Z Installing collected packages: typing-extensions, pygments, propcache, `

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 2. Mock Biometric Tests - embedding-validation

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2026-05-18T17:19:32Z
**Completed**: 2026-05-18T17:20:06Z
**Duration**: 34 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26046023776/job/76575098164)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 63: `2026-05-18T17:20:04.0892807Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 64: `2026-05-18T17:20:04.0894932Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 65: `2026-05-18T17:20:04.1296270Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 96: `2026-05-18T17:20:04.5112466Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 4
  - Sample matches:
    - Line 71: `2026-05-18T17:20:04.1403767Z   if-no-files-found: warn`
    - Line 86: `2026-05-18T17:20:04.3546311Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 96: `2026-05-18T17:20:04.5112466Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `timeout|timed out`
  - Occurrences: 3
  - Sample matches:
    - Line 38: `2026-05-18T17:19:55.8371577Z Downloading pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`
    - Line 60: `2026-05-18T17:19:56.6223137Z Installing collected packages: typing-extensions, pygments, propcache, `
    - Line 62: `2026-05-18T17:20:03.5027318Z Successfully installed aiohappyeyeballs-2.6.1 aiohttp-3.13.5 aiosignal-`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 3. Mock Biometric Tests - stt-transcription

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2026-05-18T17:23:18Z
**Completed**: 2026-05-18T17:23:48Z
**Duration**: 30 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26046023776/job/76575098181)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 63: `2026-05-18T17:23:46.8546030Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 64: `2026-05-18T17:23:46.8547122Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 65: `2026-05-18T17:23:46.8993054Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 96: `2026-05-18T17:23:47.2867565Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 4
  - Sample matches:
    - Line 71: `2026-05-18T17:23:46.9100393Z   if-no-files-found: warn`
    - Line 86: `2026-05-18T17:23:47.1275292Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 96: `2026-05-18T17:23:47.2867565Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `timeout|timed out`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2026-05-18T17:23:37.3035545Z   Using cached pytest_timeout-2.4.0-py3-none-any.whl.metadata (20 kB)`
    - Line 40: `2026-05-18T17:23:38.7815154Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`
    - Line 60: `2026-05-18T17:23:39.1168459Z Installing collected packages: typing-extensions, pygments, propcache, `

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 4. Mock Biometric Tests - dimension-adaptation

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2026-05-18T17:22:20Z
**Completed**: 2026-05-18T17:22:53Z
**Duration**: 33 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26046023776/job/76575098192)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 63: `2026-05-18T17:22:51.4181526Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 64: `2026-05-18T17:22:51.4182636Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 65: `2026-05-18T17:22:51.4531715Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 96: `2026-05-18T17:22:51.7557783Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 4
  - Sample matches:
    - Line 71: `2026-05-18T17:22:51.4618098Z   if-no-files-found: warn`
    - Line 86: `2026-05-18T17:22:51.6304622Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 96: `2026-05-18T17:22:51.7557783Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `timeout|timed out`
  - Occurrences: 3
  - Sample matches:
    - Line 38: `2026-05-18T17:22:43.7910795Z Downloading pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`
    - Line 60: `2026-05-18T17:22:44.9672681Z Installing collected packages: typing-extensions, pygments, propcache, `
    - Line 62: `2026-05-18T17:22:50.8633751Z Successfully installed aiohappyeyeballs-2.6.1 aiohttp-3.13.5 aiosignal-`

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 5. Mock Biometric Tests - anti-spoofing

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2026-05-18T17:23:30Z
**Completed**: 2026-05-18T17:24:05Z
**Duration**: 35 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26046023776/job/76575098199)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 63: `2026-05-18T17:24:02.8586188Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 64: `2026-05-18T17:24:02.8588595Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 65: `2026-05-18T17:24:02.8995698Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 96: `2026-05-18T17:24:03.2892247Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 4
  - Sample matches:
    - Line 71: `2026-05-18T17:24:02.9102287Z   if-no-files-found: warn`
    - Line 86: `2026-05-18T17:24:03.1358475Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 96: `2026-05-18T17:24:03.2892247Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `timeout|timed out`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2026-05-18T17:23:52.0317892Z   Using cached pytest_timeout-2.4.0-py3-none-any.whl.metadata (20 kB)`
    - Line 40: `2026-05-18T17:23:53.9407567Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`
    - Line 60: `2026-05-18T17:23:55.4468994Z Installing collected packages: typing-extensions, pygments, propcache, `

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 6. Mock Biometric Tests - profile-quality-assessment

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2026-05-18T17:26:36Z
**Completed**: 2026-05-18T17:27:12Z
**Duration**: 36 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26046023776/job/76575098200)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 63: `2026-05-18T17:27:10.3836610Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 64: `2026-05-18T17:27:10.3838274Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 65: `2026-05-18T17:27:10.4447403Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 96: `2026-05-18T17:27:10.8381392Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 4
  - Sample matches:
    - Line 71: `2026-05-18T17:27:10.4559820Z   if-no-files-found: warn`
    - Line 86: `2026-05-18T17:27:10.6779416Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 96: `2026-05-18T17:27:10.8381392Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `timeout|timed out`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2026-05-18T17:27:00.4708015Z   Using cached pytest_timeout-2.4.0-py3-none-any.whl.metadata (20 kB)`
    - Line 40: `2026-05-18T17:27:02.0962703Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`
    - Line 60: `2026-05-18T17:27:02.5525983Z Installing collected packages: typing-extensions, pygments, propcache, `

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 7. Mock Biometric Tests - edge-case-noise

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2026-05-18T17:23:01Z
**Completed**: 2026-05-18T17:23:35Z
**Duration**: 34 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26046023776/job/76575098226)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 63: `2026-05-18T17:23:32.4629874Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 64: `2026-05-18T17:23:32.4631202Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 65: `2026-05-18T17:23:32.4997344Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 96: `2026-05-18T17:23:32.8766984Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 4
  - Sample matches:
    - Line 71: `2026-05-18T17:23:32.5107212Z   if-no-files-found: warn`
    - Line 86: `2026-05-18T17:23:32.7243799Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 96: `2026-05-18T17:23:32.8766984Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `timeout|timed out`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2026-05-18T17:23:23.1268481Z   Using cached pytest_timeout-2.4.0-py3-none-any.whl.metadata (20 kB)`
    - Line 40: `2026-05-18T17:23:24.6435146Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`
    - Line 60: `2026-05-18T17:23:25.0659285Z Installing collected packages: typing-extensions, pygments, propcache, `

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 8. Mock Biometric Tests - edge-case-cold-start

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2026-05-18T17:25:40Z
**Completed**: 2026-05-18T17:26:12Z
**Duration**: 32 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26046023776/job/76575098247)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 63: `2026-05-18T17:26:10.5499921Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 64: `2026-05-18T17:26:10.5501630Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 65: `2026-05-18T17:26:10.5881334Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 96: `2026-05-18T17:26:10.9694987Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 4
  - Sample matches:
    - Line 71: `2026-05-18T17:26:10.5991147Z   if-no-files-found: warn`
    - Line 86: `2026-05-18T17:26:10.8155623Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 96: `2026-05-18T17:26:10.9694987Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `timeout|timed out`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2026-05-18T17:26:00.9223503Z   Using cached pytest_timeout-2.4.0-py3-none-any.whl.metadata (20 kB)`
    - Line 40: `2026-05-18T17:26:02.4717177Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`
    - Line 60: `2026-05-18T17:26:03.1406769Z Installing collected packages: typing-extensions, pygments, propcache, `

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 9. Mock Biometric Tests - end-to-end-flow

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2026-05-18T17:31:40Z
**Completed**: 2026-05-18T17:32:18Z
**Duration**: 38 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26046023776/job/76575098256)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 63: `2026-05-18T17:32:15.9513337Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 64: `2026-05-18T17:32:15.9514306Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 65: `2026-05-18T17:32:15.9875801Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 96: `2026-05-18T17:32:16.2881858Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 4
  - Sample matches:
    - Line 71: `2026-05-18T17:32:15.9962498Z   if-no-files-found: warn`
    - Line 86: `2026-05-18T17:32:16.1654479Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 96: `2026-05-18T17:32:16.2881858Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `timeout|timed out`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2026-05-18T17:32:03.3851864Z   Using cached pytest_timeout-2.4.0-py3-none-any.whl.metadata (20 kB)`
    - Line 40: `2026-05-18T17:32:06.3630228Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`
    - Line 60: `2026-05-18T17:32:07.3790173Z Installing collected packages: typing-extensions, pygments, propcache, `

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 10. Mock Biometric Tests - edge-case-voice-drift

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2026-05-18T17:26:49Z
**Completed**: 2026-05-18T17:27:27Z
**Duration**: 38 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26046023776/job/76575098258)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 63: `2026-05-18T17:27:25.3369820Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 64: `2026-05-18T17:27:25.3371325Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 65: `2026-05-18T17:27:25.3797846Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 96: `2026-05-18T17:27:25.7677775Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 4
  - Sample matches:
    - Line 71: `2026-05-18T17:27:25.3910372Z   if-no-files-found: warn`
    - Line 86: `2026-05-18T17:27:25.6106011Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 96: `2026-05-18T17:27:25.7677775Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `timeout|timed out`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2026-05-18T17:27:15.7437951Z   Using cached pytest_timeout-2.4.0-py3-none-any.whl.metadata (20 kB)`
    - Line 40: `2026-05-18T17:27:17.2773156Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`
    - Line 60: `2026-05-18T17:27:17.6392292Z Installing collected packages: typing-extensions, pygments, propcache, `

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 11. Mock Biometric Tests - edge-case-database-failure

**Status**: ❌ failure
**Category**: Test Failure
**Severity**: HIGH
**Started**: 2026-05-18T17:25:49Z
**Completed**: 2026-05-18T17:26:20Z
**Duration**: 31 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26046023776/job/76575098261)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 63: `2026-05-18T17:26:18.9286982Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 64: `2026-05-18T17:26:18.9288702Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 65: `2026-05-18T17:26:18.9789833Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 2
  - Sample matches:
    - Line 68: `2026-05-18T17:26:18.9900998Z   name: test-results-biometric-mock-edge-case-database-failure`
    - Line 96: `2026-05-18T17:26:19.3753230Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 4
  - Sample matches:
    - Line 71: `2026-05-18T17:26:18.9901847Z   if-no-files-found: warn`
    - Line 86: `2026-05-18T17:26:19.2159846Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 96: `2026-05-18T17:26:19.3753230Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `timeout|timed out`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2026-05-18T17:26:08.9261514Z   Using cached pytest_timeout-2.4.0-py3-none-any.whl.metadata (20 kB)`
    - Line 40: `2026-05-18T17:26:10.4716273Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`
    - Line 60: `2026-05-18T17:26:10.8711908Z Installing collected packages: typing-extensions, pygments, propcache, `

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 12. Mock Biometric Tests - wake-word-detection

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2026-05-18T17:32:20Z
**Completed**: 2026-05-18T17:32:50Z
**Duration**: 30 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26046023776/job/76575098262)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 63: `2026-05-18T17:32:48.6683900Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 64: `2026-05-18T17:32:48.6685500Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 65: `2026-05-18T17:32:48.7081201Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 96: `2026-05-18T17:32:49.0937813Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 4
  - Sample matches:
    - Line 71: `2026-05-18T17:32:48.7194463Z   if-no-files-found: warn`
    - Line 86: `2026-05-18T17:32:48.9350542Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 96: `2026-05-18T17:32:49.0937813Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `timeout|timed out`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2026-05-18T17:32:39.0742050Z   Using cached pytest_timeout-2.4.0-py3-none-any.whl.metadata (20 kB)`
    - Line 40: `2026-05-18T17:32:40.6802025Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`
    - Line 60: `2026-05-18T17:32:41.3183683Z Installing collected packages: typing-extensions, pygments, propcache, `

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 13. Mock Biometric Tests - voice-synthesis-detection

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2026-05-18T17:27:37Z
**Completed**: 2026-05-18T17:28:08Z
**Duration**: 31 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26046023776/job/76575098263)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 63: `2026-05-18T17:28:06.2602468Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 64: `2026-05-18T17:28:06.2603445Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 65: `2026-05-18T17:28:06.3154738Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 96: `2026-05-18T17:28:06.7223928Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 4
  - Sample matches:
    - Line 71: `2026-05-18T17:28:06.3267775Z   if-no-files-found: warn`
    - Line 86: `2026-05-18T17:28:06.5549220Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 96: `2026-05-18T17:28:06.7223928Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `timeout|timed out`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2026-05-18T17:27:56.4386426Z   Using cached pytest_timeout-2.4.0-py3-none-any.whl.metadata (20 kB)`
    - Line 40: `2026-05-18T17:27:57.9090568Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`
    - Line 60: `2026-05-18T17:27:58.2618539Z Installing collected packages: typing-extensions, pygments, propcache, `

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 14. Mock Biometric Tests - performance-baseline

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2026-05-18T17:27:50Z
**Completed**: 2026-05-18T17:28:25Z
**Duration**: 35 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26046023776/job/76575098264)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 63: `2026-05-18T17:28:23.0238386Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 64: `2026-05-18T17:28:23.0240396Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 65: `2026-05-18T17:28:23.0693058Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 96: `2026-05-18T17:28:23.4564537Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 4
  - Sample matches:
    - Line 71: `2026-05-18T17:28:23.0806055Z   if-no-files-found: warn`
    - Line 86: `2026-05-18T17:28:23.3016019Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 96: `2026-05-18T17:28:23.4564537Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `timeout|timed out`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2026-05-18T17:28:13.5668282Z   Using cached pytest_timeout-2.4.0-py3-none-any.whl.metadata (20 kB)`
    - Line 40: `2026-05-18T17:28:15.1001167Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`
    - Line 60: `2026-05-18T17:28:15.4304016Z Installing collected packages: typing-extensions, pygments, propcache, `

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 15. Mock Biometric Tests - security-validation

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2026-05-18T17:27:25Z
**Completed**: 2026-05-18T17:28:00Z
**Duration**: 35 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26046023776/job/76575098274)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 63: `2026-05-18T17:27:57.8484949Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 64: `2026-05-18T17:27:57.8486515Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 65: `2026-05-18T17:27:57.8962330Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 96: `2026-05-18T17:27:58.2828546Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 4
  - Sample matches:
    - Line 71: `2026-05-18T17:27:57.9073520Z   if-no-files-found: warn`
    - Line 86: `2026-05-18T17:27:58.1249350Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 96: `2026-05-18T17:27:58.2828546Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `timeout|timed out`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2026-05-18T17:27:48.6665058Z   Using cached pytest_timeout-2.4.0-py3-none-any.whl.metadata (20 kB)`
    - Line 40: `2026-05-18T17:27:50.1452814Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`
    - Line 60: `2026-05-18T17:27:50.4605301Z Installing collected packages: typing-extensions, pygments, propcache, `

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 16. Mock Biometric Tests - replay-attack-detection

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2026-05-18T17:29:59Z
**Completed**: 2026-05-18T17:30:35Z
**Duration**: 36 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26046023776/job/76575098276)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 63: `2026-05-18T17:30:32.1735002Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 64: `2026-05-18T17:30:32.1736952Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 65: `2026-05-18T17:30:32.2171153Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 96: `2026-05-18T17:30:32.6122839Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 4
  - Sample matches:
    - Line 71: `2026-05-18T17:30:32.2285862Z   if-no-files-found: warn`
    - Line 86: `2026-05-18T17:30:32.4529695Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 96: `2026-05-18T17:30:32.6122839Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `timeout|timed out`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2026-05-18T17:30:22.8813180Z   Using cached pytest_timeout-2.4.0-py3-none-any.whl.metadata (20 kB)`
    - Line 40: `2026-05-18T17:30:24.4075821Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`
    - Line 60: `2026-05-18T17:30:24.7678675Z Installing collected packages: typing-extensions, pygments, propcache, `

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

---

### 17. Mock Biometric Tests - adaptive-thresholds

**Status**: ❌ failure
**Category**: Timeout
**Severity**: HIGH
**Started**: 2026-05-18T17:28:12Z
**Completed**: 2026-05-18T17:28:47Z
**Duration**: 35 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/26046023776/job/76575098281)

#### Failed Steps

- **Step 4**: Install Dependencies

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 3
  - Sample matches:
    - Line 63: `2026-05-18T17:28:44.8468766Z ERROR: Could not find a version that satisfies the requirement google-c`
    - Line 64: `2026-05-18T17:28:44.8470721Z ERROR: No matching distribution found for google-cloud-sql-python-conne`
    - Line 65: `2026-05-18T17:28:44.8979234Z ##[error]Process completed with exit code 1.`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 1
  - Sample matches:
    - Line 96: `2026-05-18T17:28:45.2644610Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 4
  - Sample matches:
    - Line 71: `2026-05-18T17:28:44.9086627Z   if-no-files-found: warn`
    - Line 86: `2026-05-18T17:28:45.1177793Z ##[warning]No files were found with the provided path: test-results/bio`
    - Line 96: `2026-05-18T17:28:45.2644610Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `timeout|timed out`
  - Occurrences: 4
  - Sample matches:
    - Line 0: `2026-05-18T17:28:35.6128675Z   Using cached pytest_timeout-2.4.0-py3-none-any.whl.metadata (20 kB)`
    - Line 40: `2026-05-18T17:28:37.2866788Z Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)`
    - Line 60: `2026-05-18T17:28:37.7457286Z Installing collected packages: typing-extensions, pygments, propcache, `

#### Suggested Fixes

1. Consider increasing timeout values or optimizing slow operations

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

📊 *Report generated on 2026-05-18T17:54:49.572406*
🤖 *JARVIS CI/CD Auto-PR Manager*
