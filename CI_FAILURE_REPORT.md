# CI/CD Failure Analysis Report

## Executive Summary

- **Workflow**: 🎨 Advanced Auto-Diagram Generator
- **Run Number**: #96
- **Branch**: `cursor/jarvis-voice-unlock-integration-gemini-3-pro-preview-bbfb`
- **Commit**: `ae22a46eb4817ebfc1eb39ea5b85e360da507f99`
- **Status**: ❌ FAILED
- **Timestamp**: 2025-12-08T09:15:50Z
- **Triggered By**: @drussell23
- **Workflow URL**: [View Run](https://github.com/drussell23/JARVIS/actions/runs/20022810776)

## Failure Overview

Total Failed Jobs: **1**

| # | Job Name | Category | Severity | Duration |
|---|----------|----------|----------|----------|
| 1 | 🔍 Discover & Analyze Diagrams | permission_error | high | 8s |

## Detailed Analysis

### 1. 🔍 Discover & Analyze Diagrams

**Status**: ❌ failure
**Category**: Permission Error
**Severity**: HIGH
**Started**: 2025-12-08T09:16:08Z
**Completed**: 2025-12-08T09:16:16Z
**Duration**: 8 seconds
**Job URL**: [View Logs](https://github.com/drussell23/JARVIS/actions/runs/20022810776/job/57413317130)

#### Failed Steps

- **Step 3**: 🔍 Discover diagram files

#### Error Analysis

**Detected Error Patterns:**

- Pattern: `ERROR|Error|error`
  - Occurrences: 2
  - Sample matches:
    - Line 82: `2025-12-08T09:16:14.7640680Z ##[error]Unable to process file command 'output' successfully.`
    - Line 83: `2025-12-08T09:16:14.7649138Z ##[error]Invalid format '  "COMPLETE_VIBA_PAVA_INTEGRATION_GUIDE.md",'`

- Pattern: `FAIL|Failed|failed`
  - Occurrences: 2
  - Sample matches:
    - Line 67: `2025-12-08T09:16:13.8554265Z shell: /usr/bin/bash --noprofile --norc -e -o pipefail {0}`
    - Line 93: `2025-12-08T09:16:15.1593810Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

- Pattern: `WARN|Warning|warning`
  - Occurrences: 1
  - Sample matches:
    - Line 93: `2025-12-08T09:16:15.1593810Z ##[warning]The process '/usr/bin/git' failed with exit code 128`

#### Suggested Fixes

1. Review the logs above for specific error messages

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

📊 *Report generated on 2025-12-08T09:17:42.880287*
🤖 *JARVIS CI/CD Auto-PR Manager*
