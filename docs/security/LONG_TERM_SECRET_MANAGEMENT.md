# 🔐 Long-Term Secret Management Strategy for JARVIS

## Philosophy: Zero Secrets in Repository

**Goal**: Never store secrets in the repository - not in code, not in docs, not in history.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                    Secret Sources                        │
├─────────────────────────────────────────────────────────┤
│  GCP Secret Manager  │  macOS Keychain  │  GitHub Secrets│
└──────────┬───────────┴─────────┬────────┴───────┬───────┘
           │                     │                 │
           ▼                     ▼                 ▼
    ┌──────────────────────────────────────────────────┐
    │         Secret Retrieval Layer (Python)          │
    │  - Auto-fetch on startup                         │
    │  - Cache with TTL                                │
    │  - Automatic rotation handling                   │
    └──────────────────────────────────────────────────┘
           │
           ▼
    ┌──────────────────────────────────────────────────┐
    │              JARVIS Application                  │
    │  - Uses secrets from memory only                 │
    │  - Never writes secrets to disk                  │
    └──────────────────────────────────────────────────┘
```

---

## 1. GCP Secret Manager Integration

### Why GCP Secret Manager?

- ✅ **Centralized**: Single source of truth
- ✅ **Versioned**: Automatic secret rotation
- ✅ **Audited**: Who accessed what and when
- ✅ **Encrypted**: At rest and in transit
- ✅ **IAM-controlled**: Fine-grained permissions
- ✅ **Free tier**: 6 active secrets, 10k operations/month

### Setup

```bash
# Enable Secret Manager API
gcloud services enable secretmanager.googleapis.com

# Create secrets
gcloud secrets create anthropic-api-key \
  --replication-policy="automatic" \
  --data-file=- <<< "your-actual-key-here"

gcloud secrets create jarvis-db-password \
  --replication-policy="automatic" \
  --data-file=- <<< "your-actual-password-here"

gcloud secrets create picovoice-access-key \
  --replication-policy="automatic" \
  --data-file=- <<< "your-actual-key-here"

# Grant access to your service account
gcloud secrets add-iam-policy-binding anthropic-api-key \
  --member="serviceAccount:jarvis@jarvis-473803.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

# Repeat for all secrets...
```

### Python Integration

```python
# backend/core/secret_manager.py
from google.cloud import secretmanager
from functools import lru_cache
import os
from typing import Optional
import logging

logger = logging.getLogger(__name__)

class SecretManager:
    """Centralized secret management for JARVIS"""

    def __init__(self, project_id: str = "jarvis-473803"):
        self.project_id = project_id
        self.client = secretmanager.SecretManagerServiceClient()
        self._cache = {}
        self._cache_ttl = 3600  # 1 hour

    @lru_cache(maxsize=32)
    def get_secret(self, secret_id: str, version: str = "latest") -> Optional[str]:
        """
        Retrieve secret from GCP Secret Manager
        Falls back to environment variable if Secret Manager unavailable
        """
        try:
            # Try GCP Secret Manager first
            name = f"projects/{self.project_id}/secrets/{secret_id}/versions/{version}"
            response = self.client.access_secret_version(request={"name": name})
            secret_value = response.payload.data.decode("UTF-8")
            logger.info(f"✅ Retrieved secret '{secret_id}' from GCP Secret Manager")
            return secret_value

        except Exception as e:
            logger.warning(f"⚠️  Failed to get secret '{secret_id}' from GCP: {e}")

            # Fallback to environment variable
            env_var = secret_id.upper().replace("-", "_")
            env_value = os.getenv(env_var)

            if env_value:
                logger.info(f"✅ Using secret '{secret_id}' from environment variable")
                return env_value

            logger.error(f"❌ Secret '{secret_id}' not found in GCP or environment")
            return None

    def get_anthropic_key(self) -> str:
        """Get Anthropic API key"""
        return self.get_secret("anthropic-api-key")

    def get_db_password(self) -> str:
        """Get database password"""
        return self.get_secret("jarvis-db-password")

    def get_picovoice_key(self) -> str:
        """Get Picovoice access key"""
        return self.get_secret("picovoice-access-key")

    def rotate_secret(self, secret_id: str, new_value: str) -> bool:
        """Add new secret version (rotation)"""
        try:
            parent = f"projects/{self.project_id}/secrets/{secret_id}"
            payload = {"data": new_value.encode("UTF-8")}

            self.client.add_secret_version(
                request={"parent": parent, "payload": payload}
            )

            # Clear cache
            self.get_secret.cache_clear()
            logger.info(f"✅ Rotated secret '{secret_id}'")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to rotate secret '{secret_id}': {e}")
            return False


# Global instance
_secret_manager = None

def get_secret_manager() -> SecretManager:
    """Get or create global SecretManager instance"""
    global _secret_manager
    if _secret_manager is None:
        _secret_manager = SecretManager()
    return _secret_manager
```

### Update Application Code

```python
# backend/chatbots/claude_chatbot.py
from backend.core.secret_manager import get_secret_manager

class ClaudeChatbot:
    def __init__(self):
        # OLD: self.api_key = os.getenv("ANTHROPIC_API_KEY")
        # NEW:
        secret_mgr = get_secret_manager()
        self.api_key = secret_mgr.get_anthropic_key()

        self.client = anthropic.Anthropic(api_key=self.api_key)

# backend/intelligence/cloud_database_adapter.py
from backend.core.secret_manager import get_secret_manager

def get_db_connection():
    secret_mgr = get_secret_manager()

    # OLD: password = os.getenv("JARVIS_DB_PASSWORD")
    # NEW:
    password = secret_mgr.get_db_password()

    return psycopg2.connect(
        host=os.getenv("JARVIS_DB_HOST"),
        port=os.getenv("JARVIS_DB_PORT"),
        database=os.getenv("JARVIS_DB_NAME"),
        user=os.getenv("JARVIS_DB_USER"),
        password=password
    )
```

---

## 2. macOS Keychain for Local Development

### Why Keychain?

- ✅ **Secure**: OS-level encryption
- ✅ **Convenient**: No .env files to manage
- ✅ **Access-controlled**: Requires user authentication

### Setup Script

```python
# backend/scripts/setup_local_secrets.py
import keyring
import getpass
import sys

SECRETS = {
    "anthropic-api-key": "Anthropic API Key",
    "jarvis-db-password": "Database Password",
    "picovoice-access-key": "Picovoice Access Key",
}

def setup_keychain_secrets():
    """Store secrets in macOS Keychain"""
    print("🔐 JARVIS Local Secret Setup\n")
    print("This will store secrets securely in your macOS Keychain.\n")

    for secret_id, description in SECRETS.items():
        print(f"\n📝 {description}")
        print(f"   Secret ID: {secret_id}")

        # Check if already exists
        existing = keyring.get_password("JARVIS", secret_id)
        if existing:
            update = input("   Secret already exists. Update? (y/N): ").lower()
            if update != 'y':
                print("   ⏭️  Skipped")
                continue

        # Get new value
        secret_value = getpass.getpass(f"   Enter {description}: ")

        if not secret_value:
            print("   ⚠️  Empty value, skipping")
            continue

        # Store in keychain
        keyring.set_password("JARVIS", secret_id, secret_value)
        print("   ✅ Stored securely in Keychain")

    print("\n✅ All secrets configured!")
    print("\nTo use these secrets, your code will automatically fetch from Keychain.")

if __name__ == "__main__":
    setup_keychain_secrets()
```

### Keychain Integration

```python
# backend/core/secret_manager.py (enhanced)
import keyring

class SecretManager:
    def get_secret(self, secret_id: str, version: str = "latest") -> Optional[str]:
        """
        Retrieve secret with fallback chain:
        1. GCP Secret Manager (production)
        2. macOS Keychain (local development)
        3. Environment variable (CI/CD)
        """
        try:
            # Try GCP Secret Manager first (production)
            name = f"projects/{self.project_id}/secrets/{secret_id}/versions/{version}"
            response = self.client.access_secret_version(request={"name": name})
            return response.payload.data.decode("UTF-8")

        except Exception as gcp_error:
            logger.debug(f"GCP Secret Manager unavailable: {gcp_error}")

            try:
                # Try macOS Keychain (local development)
                keychain_value = keyring.get_password("JARVIS", secret_id)
                if keychain_value:
                    logger.info(f"✅ Using secret '{secret_id}' from Keychain")
                    return keychain_value

            except Exception as keychain_error:
                logger.debug(f"Keychain unavailable: {keychain_error}")

            # Final fallback: environment variable
            env_var = secret_id.upper().replace("-", "_")
            env_value = os.getenv(env_var)

            if env_value:
                logger.info(f"✅ Using secret '{secret_id}' from environment")
                return env_value

            logger.error(f"❌ Secret '{secret_id}' not found anywhere")
            return None
```

---

## 3. GitHub Actions Secret Management

### Setup GitHub Secrets

```bash
# Using GitHub CLI
gh secret set ANTHROPIC_API_KEY --body "your-key-here"
gh secret set JARVIS_DB_PASSWORD --body "your-password-here"
gh secret set PICOVOICE_ACCESS_KEY --body "your-key-here"

# Or sync from GCP Secret Manager
gcloud secrets versions access latest --secret="anthropic-api-key" | \
  gh secret set ANTHROPIC_API_KEY

gcloud secrets versions access latest --secret="jarvis-db-password" | \
  gh secret set JARVIS_DB_PASSWORD
```

### Workflow Configuration

```yaml
# .github/workflows/ci-cd-pipeline.yml
name: CI/CD Pipeline

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3

      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.10'

      - name: Install dependencies
        run: pip install -r backend/requirements.txt

      - name: Run tests
        env:
          # Secrets from GitHub Secrets
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          JARVIS_DB_PASSWORD: ${{ secrets.JARVIS_DB_PASSWORD }}
          PICOVOICE_ACCESS_KEY: ${{ secrets.PICOVOICE_ACCESS_KEY }}
        run: pytest backend/tests/
```

---

## 4. Automated Secret Scanning

### Pre-commit Hook with Gitleaks

```bash
# Install gitleaks
brew install gitleaks

# Create .gitleaks.toml
cat > .gitleaks.toml << 'EOF'
title = "JARVIS Secret Scanner"

[extend]
useDefault = true

[[rules]]
id = "anthropic-api-key"
description = "Anthropic API Key"
regex = '''sk-ant-api03-[a-zA-Z0-9_-]{95}'''
tags = ["key", "anthropic"]

[[rules]]
id = "generic-api-key"
description = "Generic API Key"
regex = '''(?i)(api[_-]?key|apikey|access[_-]?key)["\s:=]+[a-zA-Z0-9_-]{20,}'''
tags = ["key"]

[[rules]]
id = "password-in-code"
description = "Password in code"
regex = '''(?i)(password|passwd|pwd)["\s:=]+[^"\s]{8,}'''
tags = ["password"]

[allowlist]
description = "Allowlist for safe patterns"
regexes = [
  '''YOUR_.*_HERE''',
  '''example\.com''',
  '''\*\*\*REMOVED\*\*\*''',
]

paths = [
  '''.env.example''',
  '''SECURITY_CLEANUP_PLAN.md''',
  '''LONG_TERM_SECRET_MANAGEMENT.md''',
]
EOF

# Install pre-commit hook
cat > .git/hooks/pre-commit << 'EOF'
#!/bin/bash

echo "🔍 Scanning for secrets with Gitleaks..."

gitleaks protect --staged --verbose --config .gitleaks.toml

if [ $? -ne 0 ]; then
    echo ""
    echo "❌ COMMIT BLOCKED: Secrets detected!"
    echo ""
    echo "Options:"
    echo "  1. Remove the secret and use Secret Manager instead"
    echo "  2. Add to .gitleaks.toml allowlist if false positive"
    echo "  3. Use --no-verify to bypass (NOT RECOMMENDED)"
    exit 1
fi

echo "✅ No secrets detected"
exit 0
EOF

chmod +x .git/hooks/pre-commit
```

### GitHub Action for Secret Scanning

```yaml
# .github/workflows/secret-scan.yml
name: Secret Scanning

on:
  push:
    branches: [ main, develop ]
  pull_request:
    branches: [ main, develop ]

jobs:
  gitleaks:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
        with:
          fetch-depth: 0  # Full history for scanning

      - name: Run Gitleaks
        uses: gitleaks/gitleaks-action@v2
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          GITLEAKS_CONFIG: .gitleaks.toml
```

---

## 5. Automatic Secret Rotation

### Rotation Script

```python
# backend/scripts/rotate_secrets.py
from backend.core.secret_manager import get_secret_manager
import secrets
import string
import anthropic
from datetime import datetime

def generate_secure_password(length=32):
    """Generate cryptographically secure password"""
    alphabet = string.ascii_letters + string.digits + string.punctuation
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def rotate_db_password():
    """Rotate database password"""
    secret_mgr = get_secret_manager()

    # Generate new password
    new_password = generate_secure_password()

    # Update GCP Secret Manager
    secret_mgr.rotate_secret("jarvis-db-password", new_password)

    # Update Cloud SQL
    import subprocess
    subprocess.run([
        "gcloud", "sql", "users", "set-password", "jarvis",
        "--instance=jarvis-learning-db",
        f"--password={new_password}"
    ], check=True)

    print(f"✅ Database password rotated at {datetime.now()}")

def rotate_anthropic_key():
    """Rotate Anthropic API key (manual step required)"""
    print("⚠️  Anthropic API key rotation requires manual steps:")
    print("1. Visit https://console.anthropic.com/settings/keys")
    print("2. Create new API key")
    print("3. Run: python rotate_secrets.py --set-anthropic-key")

def set_anthropic_key(new_key: str):
    """Set new Anthropic API key after manual rotation"""
    secret_mgr = get_secret_manager()

    # Test key first
    try:
        client = anthropic.Anthropic(api_key=new_key)
        client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=10,
            messages=[{"role": "user", "content": "test"}]
        )
        print("✅ New API key validated")

    except Exception as e:
        print(f"❌ Invalid API key: {e}")
        return False

    # Update Secret Manager
    secret_mgr.rotate_secret("anthropic-api-key", new_key)
    print("✅ Anthropic API key rotated")
    return True

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--rotate-db", action="store_true")
    parser.add_argument("--rotate-anthropic", action="store_true")
    parser.add_argument("--set-anthropic-key")

    args = parser.parse_args()

    if args.rotate_db:
        rotate_db_password()

    if args.rotate_anthropic:
        rotate_anthropic_key()

    if args.set_anthropic_key:
        set_anthropic_key(args.set_anthropic_key)
```

### Scheduled Rotation (GitHub Action)

```yaml
# .github/workflows/secret-rotation.yml
name: Automatic Secret Rotation

on:
  schedule:
    # Every 90 days at 2 AM UTC
    - cron: '0 2 */90 * *'
  workflow_dispatch:  # Manual trigger

jobs:
  rotate-db-password:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3

      - name: Authenticate to GCP
        uses: google-github-actions/auth@v1
        with:
          credentials_json: ${{ secrets.GCP_SA_KEY }}

      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.10'

      - name: Install dependencies
        run: pip install google-cloud-secret-manager psycopg2-binary

      - name: Rotate database password
        run: python backend/scripts/rotate_secrets.py --rotate-db

      - name: Notify team
        uses: slackapi/slack-github-action@v1
        with:
          payload: |
            {
              "text": "🔐 JARVIS database password rotated successfully"
            }
        env:
          SLACK_WEBHOOK_URL: ${{ secrets.SLACK_WEBHOOK_URL }}
```

---

## 6. Documentation Best Practices

### Safe Documentation Templates

```markdown
# ✅ GOOD - Using placeholders

## Setup

```bash
# Set your credentials
export ANTHROPIC_API_KEY="your-api-key-here"
export JARVIS_DB_PASSWORD="your-secure-password"

# Or fetch from Secret Manager
export ANTHROPIC_API_KEY=$(gcloud secrets versions access latest --secret="anthropic-api-key")
```

## ❌ BAD - Hardcoded secrets

```bash
# DON'T DO THIS!
export ANTHROPIC_API_KEY="sk-ant-api03-AqIrRCst..."
export JARVIS_DB_PASSWORD="JarvisSecure2025!"
```
```

### .env.example Files

```bash
# .env.example - Safe to commit
# Copy to .env and fill in actual values

# Anthropic Configuration
ANTHROPIC_API_KEY=sk-ant-api03-YOUR_KEY_HERE
CLAUDE_MODEL=claude-3-5-sonnet-20241022

# Database Configuration
JARVIS_DB_PASSWORD=YOUR_SECURE_PASSWORD_HERE
JARVIS_DB_HOST=127.0.0.1
JARVIS_DB_PORT=5432

# Voice Configuration
PICOVOICE_ACCESS_KEY=YOUR_PICOVOICE_KEY_HERE
```

---

## 7. Migration Plan

### Phase 1: Immediate (Week 1)
- [ ] Set up GCP Secret Manager
- [ ] Migrate all secrets to Secret Manager
- [ ] Install gitleaks pre-commit hook
- [ ] Update all .env files to use placeholders
- [ ] Clean git history (one-time)

### Phase 2: Code Updates (Week 2)
- [ ] Implement `SecretManager` class
- [ ] Update all code to use `SecretManager`
- [ ] Add keychain fallback for local development
- [ ] Update documentation with new patterns
- [ ] Test in development environment

### Phase 3: CI/CD Integration (Week 3)
- [ ] Set up GitHub Secrets
- [ ] Update all workflows to use secrets
- [ ] Add secret scanning to CI/CD
- [ ] Test deployment with new secret management

### Phase 4: Automation (Week 4)
- [ ] Implement rotation scripts
- [ ] Set up scheduled rotation
- [ ] Configure monitoring and alerting
- [ ] Document incident response plan

---

## 8. Incident Response Plan

### If Secret is Exposed

1. **Immediate** (< 5 minutes):
   - Revoke/rotate the exposed secret
   - Check for unauthorized usage
   - Document the incident

2. **Short-term** (< 1 hour):
   - Clean git history if needed
   - Notify affected systems/users
   - Update all instances with new secret

3. **Long-term** (< 1 week):
   - Root cause analysis
   - Update processes to prevent recurrence
   - Team training on secure practices

---

## 9. Monitoring and Alerts

### GCP Audit Logs

```bash
# Monitor secret access
gcloud logging read "resource.type=secretmanager.googleapis.com/Secret" \
  --format json \
  --limit 100
```

### Alert on Suspicious Activity

```yaml
# gcp-alerts.yaml
---
alert:
  - name: secret-access-anomaly
    condition:
      - unusual_access_pattern
      - access_from_unknown_ip
    action:
      - notify_slack
      - require_mfa
      - temporary_freeze
```

---

## 10. Cost Analysis

### GCP Secret Manager Pricing

- **Storage**: $0.06 per secret version per month
- **Access**: $0.03 per 10,000 operations
- **Free tier**: 6 active secrets, 10k operations/month

**Estimated monthly cost for JARVIS**: ~$0.20 - $2.00

### ROI

- ✅ **Security**: Priceless
- ✅ **Compliance**: Required for production
- ✅ **Developer time saved**: ~4 hours/month
- ✅ **Incident prevention**: Potentially $thousands saved

---

## Summary: The Complete Solution

```
Local Development      Production             CI/CD
─────────────────     ──────────────        ──────────────
macOS Keychain    →   GCP Secret Manager ←  GitHub Secrets
       ↓                     ↓                     ↓
   .env (git-ignored)    Runtime only         Environment vars
       ↓                     ↓                     ↓
   ─────────────────────────────────────────────────
                 Secret Manager Class
                (Auto-detect environment)
   ─────────────────────────────────────────────────
                    JARVIS Application
```

**Never store secrets in**:
- ❌ Git repository
- ❌ Documentation files
- ❌ Code comments
- ❌ Slack messages
- ❌ Email
- ❌ Screenshots

**Always use**:
- ✅ Secret Manager (production)
- ✅ Keychain (local)
- ✅ Environment variables (CI/CD)
- ✅ Automated scanning
- ✅ Regular rotation

---

**Implementation Time**: 4-6 weeks
**Maintenance**: ~2 hours/month
**Security Improvement**: 🔥 → 🔐
