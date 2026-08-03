# 🚨 SECURITY CLEANUP PLAN - JARVIS Repository

## CRITICAL: Exposed Secrets Found in Git History

### What Was Found

1. **Anthropic API Key Prefix**
   - File: `docs/getting-started/claude-integration-success.md`
   - Line 11: `sk-ant-api03-AqIrRCst...`
   - Commits: c21b92d, 6baa194, b88a844, 42c2025, 90501d9, 7bb92b5

2. **Database Password (FULL)**
   - Files:
     - `CLOUD_SQL_PROXY_SETUP.md`
     - `VOICE_UNLOCK_OPTIMIZATION.md`
   - Password: `JarvisSecure2025!`
   - Commits: 1c3fc70, 09f8eca, e3ce07d, dcbb97e

---

## IMMEDIATE ACTIONS REQUIRED

### 1️⃣ ROTATE ALL CREDENTIALS (Do This FIRST)

```bash
# A. Rotate Anthropic API Key
# Visit: https://console.anthropic.com/settings/keys
# 1. Delete old key: sk-ant-api03-AqIrRCst...
# 2. Create new key
# 3. Update .env and backend/.env with new key

# B. Change Database Password
gcloud sql users set-password jarvis \
  --instance=jarvis-learning-db \
  --password=NEW_SECURE_PASSWORD_HERE

# C. Update .env files with new password
# Edit .env and backend/.env:
# JARVIS_DB_PASSWORD=NEW_SECURE_PASSWORD_HERE
```

### 2️⃣ CLEAN GIT HISTORY (Before Public Push)

**WARNING**: Only do this if you haven't pushed to public yet!

```bash
# Method 1: BFG Repo-Cleaner (Easiest)
# Install BFG
brew install bfg  # macOS
# or download from: https://rtyley.github.io/bfg-repo-cleaner/

# Create replacement file
cat > replacements.txt << 'EOF'
JarvisSecure2025!==>***REMOVED***
sk-ant-api03-AqIrRCst==>***REMOVED***
EOF

# Clean the repo
bfg --replace-text replacements.txt --no-blob-protection .git

# Clean up
git reflog expire --expire=now --all
git gc --prune=now --aggressive

# Force push (ONLY if not public yet!)
git push origin --force --all
```

**Method 2: git-filter-repo (More Control)**

```bash
# Install git-filter-repo
pip install git-filter-repo

# Create callback script
cat > filter_secrets.py << 'EOF'
#!/usr/bin/env python3
import re

def replace_secrets(blob, callback_metadata):
    blob.data = blob.data.replace(b'JarvisSecure2025!', b'***REMOVED***')
    blob.data = blob.data.replace(b'sk-ant-api03-AqIrRCst', b'***REMOVED***')

git-filter-repo --blob-callback '
import re
def replace_secrets(blob, callback_metadata):
    blob.data = blob.data.replace(b"JarvisSecure2025!", b"***REMOVED***")
    blob.data = blob.data.replace(b"sk-ant-api03-AqIrRCst", b"***REMOVED***")
' --force
EOF

# Run cleanup
python3 filter_secrets.py
```

### 3️⃣ SANITIZE CURRENT FILES

```bash
# Remove exposed secrets from current files
sed -i '' 's/JarvisSecure2025!/\*\*\*REMOVED\*\*\*/g' CLOUD_SQL_PROXY_SETUP.md
sed -i '' 's/JarvisSecure2025!/\*\*\*REMOVED\*\*\*/g' VOICE_UNLOCK_OPTIMIZATION.md
sed -i '' 's/sk-ant-api03-AqIrRCst/\*\*\*REMOVED\*\*\*/g' docs/getting-started/claude-integration-success.md

# Or replace with placeholders
sed -i '' 's/JarvisSecure2025!/YOUR_DB_PASSWORD_HERE/g' CLOUD_SQL_PROXY_SETUP.md
sed -i '' 's/JarvisSecure2025!/YOUR_DB_PASSWORD_HERE/g' VOICE_UNLOCK_OPTIMIZATION.md
sed -i '' 's/sk-ant-api03-AqIrRCst/YOUR_API_KEY_PREFIX/g' docs/getting-started/claude-integration-success.md

# Commit the sanitization
git add .
git commit -m "security: Remove exposed credentials from documentation"
```

### 4️⃣ STRENGTHEN .gitignore

Your `.gitignore` is already good, but add:

```bash
cat >> .gitignore << 'EOF'

# Documentation with secrets
SECURITY_CLEANUP_PLAN.md
replacements.txt

# Backup files
*.md.bak
*.bak
EOF

git add .gitignore
git commit -m "security: Strengthen .gitignore"
```

### 5️⃣ ADD PRE-COMMIT HOOK (Optional but Recommended)

```bash
# Install gitleaks
brew install gitleaks

# Create pre-commit hook
cat > .git/hooks/pre-commit << 'EOF'
#!/bin/bash
gitleaks protect --staged --verbose
if [ $? -ne 0 ]; then
    echo "❌ Gitleaks detected secrets! Commit blocked."
    exit 1
fi
EOF

chmod +x .git/hooks/pre-commit
```

---

## IF ALREADY PUSHED TO PUBLIC GITHUB

If you've already pushed to public, history rewriting won't help. Do this instead:

### 1. Rotate credentials IMMEDIATELY (see step 1 above)

### 2. Make repository private temporarily
```bash
gh repo edit --visibility private
```

### 3. Clean history (see step 2 above)

### 4. Force push cleaned history
```bash
git push origin --force --all
```

### 5. Make public again
```bash
gh repo edit --visibility public
```

### 6. Report the breach (if needed)
- Anthropic: support@anthropic.com
- GCP: Check for unauthorized access in Cloud Console

---

## VERIFICATION STEPS

After cleanup:

```bash
# 1. Verify no secrets in current files
grep -r "JarvisSecure2025!" . --exclude-dir=.git
grep -r "sk-ant-api03-AqIrRCst" . --exclude-dir=.git

# 2. Verify no secrets in git history
git log --all -S "JarvisSecure2025!" --source --all
git log --all -S "sk-ant-api03-AqIrRCst" --source --all

# 3. Scan with gitleaks
gitleaks detect --source . --verbose

# 4. Test new credentials work
python backend/test_cloud_sql.py
python backend/verify_claude_setup.py
```

---

## PREVENTION CHECKLIST

- [ ] All credentials rotated
- [ ] Git history cleaned
- [ ] Current files sanitized
- [ ] Pre-commit hook installed
- [ ] .env files in .gitignore
- [ ] Repository scanned with gitleaks
- [ ] No secrets in git log
- [ ] Team educated on secret management

---

## SAFE ALTERNATIVES

Instead of hardcoding in docs, use:

```markdown
# Bad
JARVIS_DB_PASSWORD=JarvisSecure2025!

# Good
JARVIS_DB_PASSWORD=your_secure_password_here
JARVIS_DB_PASSWORD=${JARVIS_DB_PASSWORD}
JARVIS_DB_PASSWORD=$(gcloud secrets versions access latest --secret="jarvis-db-password")
```

---

**Priority**: 🔴 CRITICAL - Do this before pushing to public!

**Time Estimate**: 30-45 minutes

**Risk if Ignored**: Full compromise of JARVIS infrastructure
