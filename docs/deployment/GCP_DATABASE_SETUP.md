# 🌐 JARVIS Centralized GCP Database Setup

This guide will help you set up centralized Google Cloud SQL (PostgreSQL) and Cloud Storage databases so your local Mac and GCP VM share the same data in real-time.

---

## 🎯 **What You're Building**

### **Before:**
```
Local Mac                          GCP VM
├─ SQLite (local only) ❌          ├─ SQLite (cloud only) ❌
└─ ChromaDB (local only) ❌        └─ ChromaDB (cloud only) ❌

NO DATA SHARING
```

### **After:**
```
Local Mac                          GCP Cloud SQL (PostgreSQL)
Connect to ──────────────────────► ├─ Shared learning data
cloud DB in real-time              ├─ Goals, patterns, actions
                                   └─ Auto-backups, 99.95% uptime
GCP VM
Connect to ──────────────────────► Same cloud database
same DB                            ↓
                                   GCS Bucket (ChromaDB)
                                   └─ Shared vector embeddings
```

---

## 📋 **Prerequisites**

1. ✅ GCP account with project `jarvis-473803`
2. ✅ `gcloud` CLI installed and authenticated
3. ✅ Local JARVIS working with SQLite
4. ✅ GCP VM running JARVIS backend

---

## 🚀 **Quick Start (15 minutes)**

### **Step 1: Install Required Packages**

```bash
cd backend
pip install -r requirements-gcp-databases.txt
```

This installs:
- `asyncpg` - PostgreSQL async driver
- `cloud-sql-python-connector` - Secure Cloud SQL connections
- `google-cloud-storage` - For ChromaDB in cloud

---

### **Step 2: Run GCP Setup Script**

This creates Cloud SQL instance + Storage buckets:

```bash
./scripts/setup_gcp_databases.sh
```

**What it does:**
1. ✅ Creates Cloud SQL PostgreSQL instance (db-f1-micro, ~$7/month)
2. ✅ Creates Cloud Storage buckets for ChromaDB and backups
3. ✅ Generates secure passwords
4. ✅ Saves credentials to `~/.jarvis/gcp/database_config.json`
5. ✅ Creates `.env.gcp` file for environment variables
6. ✅ Installs Cloud SQL Proxy for local development

**This takes 5-10 minutes** (Cloud SQL instance creation is slow)

---

### **Step 3: Start Cloud SQL Proxy (For Local Mac)**

The Cloud SQL Proxy provides secure access from your local Mac:

```bash
# Get connection name from output of Step 2
cloud_sql_proxy -instances=jarvis-473803:us-central1:jarvis-learning-db=tcp:5432
```

**Leave this running in a separate terminal** while developing locally.

---

### **Step 4: Migrate Local Data to Cloud**

Transfer your existing local learning data to Cloud SQL:

```bash
python scripts/migrate_to_cloud.py
```

**What it does:**
1. ✅ Reads local SQLite database
2. ✅ Converts schema to PostgreSQL
3. ✅ Migrates all data to Cloud SQL
4. ✅ Verifies migration success

**This takes 1-2 minutes** depending on data size.

---

### **Step 5: Configure JARVIS to Use Cloud SQL**

#### **Option A: Environment Variables**

```bash
# Add to your ~/.zshrc or ~/.bashrc
export JARVIS_DB_TYPE=cloudsql
export JARVIS_DB_CONNECTION_NAME="jarvis-473803:us-central1:jarvis-learning-db"
```

#### **Option B: Load from .env.gcp**

```bash
# In your JARVIS startup script
source .env.gcp
```

#### **Option C: Automatic (Recommended)**

The database adapter automatically detects Cloud SQL config from:
- `~/.jarvis/gcp/database_config.json`
- Environment variables
- Falls back to local SQLite if cloud unavailable

---

### **Step 6: Test Connection**

```bash
# Test local connection
python -c "
import asyncio
from backend.intelligence.cloud_database_adapter import get_database_adapter

async def test():
    adapter = await get_database_adapter()
    print(f'✅ Using: {\"Cloud SQL\" if adapter.is_cloud else \"SQLite\"}')
    await adapter.close()

asyncio.run(test())
"
```

Should output: `✅ Using: Cloud SQL`

---

## 🔧 **Configuration Details**

### **Automatic Fallback**

JARVIS automatically falls back to local SQLite if:
- Cloud SQL is unavailable
- Credentials are missing
- No internet connection
- Cloud SQL Proxy not running (for local)

### **Connection Modes**

| Environment | Connection Method |
|-------------|-------------------|
| **Local Mac** | Via Cloud SQL Proxy (localhost:5432) |
| **GCP VM** | Direct private IP connection |
| **Fallback** | Local SQLite (~/.jarvis/learning/) |

---

## 📊 **Verify Everything Works**

### **1. Check Cloud SQL Instance**

```bash
# List instances
gcloud sql instances list

# Check status
gcloud sql instances describe jarvis-learning-db
```

### **2. Connect to Database Directly**

```bash
# Using psql
psql -h 127.0.0.1 -U jarvis -d jarvis_learning

# List tables
\dt

# Query data
SELECT COUNT(*) FROM goals;
```

### **3. Check Data Sync**

```python
# On Local Mac
from backend.intelligence.learning_database import get_learning_database
import asyncio

async def test():
    db = await get_learning_database()
    metrics = await db.get_learning_metrics()
    print(f"Total patterns: {metrics['patterns']['total_patterns']}")
    await db.close()

asyncio.run(test())
```

**Run the same script on GCP VM** - should show same data! 🎉

---

## 💰 **Cost Breakdown**

| Service | Tier | Monthly Cost |
|---------|------|--------------|
| Cloud SQL (db-f1-micro) | 0.6GB RAM, shared CPU | ~$7-9 |
| Cloud Storage (10GB) | Standard storage | ~$0.20 |
| Cloud SQL Backups (7 days) | Automated backups | ~$1 |
| **Total** | | **~$8-12/month** |

**Free Tier:**
- First 90 days: $300 free credits
- Covers ~30 months of this setup!

---

## 🔐 **Security Best Practices**

### **Credentials Storage**

✅ **DO:**
- Store in `~/.jarvis/gcp/database_config.json` (mode 600)
- Use environment variables
- Use Cloud SQL Connector for encrypted connections

❌ **DON'T:**
- Commit credentials to git
- Share credentials files
- Use passwords in code

### **Network Security**

The setup uses:
- ✅ Private IP (no public access)
- ✅ Encrypted connections via Cloud SQL Proxy
- ✅ IAM authentication (optional)
- ✅ VPC network isolation

---

## 🐛 **Troubleshooting**

### **"Can't connect to Cloud SQL"**

1. Is Cloud SQL Proxy running?
   ```bash
   ps aux | grep cloud_sql_proxy
   ```

2. Check instance status:
   ```bash
   gcloud sql instances describe jarvis-learning-db
   ```

3. Verify credentials:
   ```bash
   cat ~/.jarvis/gcp/database_config.json
   ```

### **"Migration failed"**

1. Check local database exists:
   ```bash
   ls -lh ~/.jarvis/learning/jarvis_learning.db
   ```

2. Verify Cloud SQL is ready:
   ```bash
   gcloud sql operations list --instance=jarvis-learning-db
   ```

3. Run with debug logging:
   ```bash
   python scripts/migrate_to_cloud.py --verbose
   ```

### **"Using SQLite instead of Cloud SQL"**

This is the automatic fallback. Check:
1. Environment variable set: `echo $JARVIS_DB_TYPE`
2. Cloud SQL Proxy running: `lsof -i:5432`
3. Network connectivity: `ping 8.8.8.8`

---

## 🔄 **Switching Between Local and Cloud**

### **Use Cloud SQL:**
```bash
export JARVIS_DB_TYPE=cloudsql
# Start Cloud SQL Proxy
cloud_sql_proxy -instances=jarvis-473803:us-central1:jarvis-learning-db=tcp:5432
# Start JARVIS
python main.py
```

### **Use Local SQLite:**
```bash
export JARVIS_DB_TYPE=sqlite
# Or just unset it:
unset JARVIS_DB_TYPE
# Start JARVIS
python main.py
```

The adapter automatically uses the correct database!

---

## 📈 **Monitoring & Maintenance**

### **Database Metrics**

```bash
# Cloud SQL metrics
gcloud sql operations list --instance=jarvis-learning-db --limit=10

# Storage usage
gcloud sql instances describe jarvis-learning-db \
  --format="value(settings.dataDiskSizeGb)"
```

### **Automated Backups**

Configured automatically:
- ✅ Daily backups at 3:00 AM
- ✅ Retained for 7 days
- ✅ Point-in-time recovery enabled

### **Manual Backup**

```bash
# Create backup
gcloud sql backups create --instance=jarvis-learning-db

# List backups
gcloud sql backups list --instance=jarvis-learning-db

# Restore from backup
gcloud sql backups restore BACKUP_ID \
  --backup-instance=jarvis-learning-db \
  --backup-id=BACKUP_ID
```

---

## 🚀 **Next Steps**

After setup:

1. ✅ **Test on Local Mac** - Verify cloud connection works
2. ✅ **Test on GCP VM** - Verify same data appears
3. ✅ **Monitor for 24 hours** - Check stability
4. ✅ **Set up ChromaDB cloud sync** (optional, advanced)
5. ✅ **Configure auto-backups** to Cloud Storage

---

## 🎯 **Benefits You Now Have**

✅ **Single source of truth** - One database, everywhere
✅ **Real-time sync** - Changes appear instantly
✅ **99.95% uptime** - Google's SLA guarantee
✅ **Automatic backups** - Daily, 7-day retention
✅ **Point-in-time recovery** - Restore to any moment
✅ **Scalable** - Upgrade database size anytime
✅ **Secure** - Encrypted connections, private IP
✅ **Professional** - Production-grade setup

---

## 📚 **Additional Resources**

- [Cloud SQL Documentation](https://cloud.google.com/sql/docs)
- [Cloud SQL Proxy Guide](https://cloud.google.com/sql/docs/postgres/sql-proxy)
- [asyncpg Documentation](https://magicstack.github.io/asyncpg/)
- [JARVIS Hybrid Architecture](../architecture/HYBRID_ARCHITECTURE.md)

---

**Last Updated:** 2025-10-24
**Version:** 1.0
