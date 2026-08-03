# JARVIS Priority Roadmap
**Generated: 2025-10-25**
**Current State: Hybrid Cloud Architecture Complete, Deployment Ready**

---

## 🎯 Recommended Next Priorities

### Priority 1: **Test & Validate Full Spot VM Flow** ⚡ IMMEDIATE
**Branch Name:** `test-spot-vm-deployment`
**Estimated Effort:** 2-4 hours
**Status:** READY TO TEST

#### Why This is Critical:
- GitHub Actions deployment is configured and ready
- Cloud Storage deployment package system is built
- Spot VM creation logic is implemented
- **BUT**: Never tested end-to-end with actual RAM > 85% trigger
- Need to verify VMs actually pull from Cloud Storage correctly
- Need to confirm cleanup works when JARVIS stops

#### What to Do:
1. **Trigger RAM threshold naturally:**
   ```bash
   python start_system.py
   # Open multiple Chrome tabs, apps to push RAM > 85%
   # Watch logs for: "🚀 Creating GCP Spot VM..."
   ```

2. **Manual RAM trigger test:**
   - Temporarily lower threshold in `backend/core/workload_router.py:48`
   - Change `if ram_percent > 85:` to `if ram_percent > 75:` (easier to hit)
   - Test VM creation, startup script, Cloud Storage pull

3. **Validate:**
   - ✅ VM creates successfully
   - ✅ VM pulls code from gs://jarvis-473803-deployments/
   - ✅ VM starts backend on port 8010
   - ✅ Requests route to GCP when needed
   - ✅ VM deletes when you stop JARVIS (Ctrl+C)
   - ✅ No orphaned VMs left behind

4. **Fix any issues found**

#### Success Criteria:
- [ ] Successfully created Spot VM via RAM trigger
- [ ] VM pulled latest deployment from Cloud Storage
- [ ] Backend started and responded to health checks
- [ ] Requests successfully routed to GCP
- [ ] VM auto-deleted on JARVIS shutdown
- [ ] No manual cleanup needed

---

### Priority 2: **Cost Monitoring & Alerts** 💰 HIGH
**Branch Name:** `cost-monitoring-alerts`
**Estimated Effort:** 1-2 days
**Depends On:** Priority 1 completion

#### Why This is Critical:
- Spot VMs are cheap BUT can still cost money if left running
- You have 3-hour max runtime, but what if cleanup fails?
- Need visibility into actual costs vs projected $11-15/month
- Should know immediately if costs spike unexpectedly

#### What to Do:
1. **GCP Budget Alerts:**
   ```bash
   # Set up budget alerts in GCP Console
   # Alert thresholds: $20, $50, $100/month
   # Email notifications to your account
   ```

2. **Cost Tracking Dashboard:**
   - Add `/hybrid/cost` endpoint to backend
   - Track: VM creation count, runtime hours, estimated cost
   - Store in Cloud SQL learning database
   - Display in frontend dashboard

3. **Orphaned VM Monitoring:**
   - Enhance `scripts/cleanup_orphaned_vms.sh`
   - Run as cron job: `0 */6 * * *` (every 6 hours)
   - Log orphaned VMs to monitoring dashboard
   - Send notification if orphaned VMs found

4. **Cost Optimization Metrics:**
   - Track: Local vs GCP request routing ratio
   - Monitor: Average VM lifetime per session
   - Alert if: Average VM lifetime > 2 hours (approaching 3hr limit)

#### Success Criteria:
- [ ] GCP budget alerts configured
- [ ] Cost tracking endpoint implemented
- [ ] Orphaned VM cron job running
- [ ] Cost dashboard visible in frontend
- [ ] Email alerts working

#### Files to Create/Modify:
- `backend/core/cost_tracker.py` (new)
- `backend/routers/hybrid.py` (add /cost endpoint)
- `.github/workflows/setup-cost-monitoring.yml` (new)
- `scripts/setup_cost_monitoring.sh` (new)

---

### Priority 3: **Multi-Monitor Performance Optimization** 🚀 HIGH
**Branch Name:** `multi-monitor-performance`
**Estimated Effort:** 3-5 days
**Current Issue:** Multi-Space Vision is memory-heavy

#### Why This is Important:
- Multi-Space Desktop Monitoring uses ~1.2GB memory budget
- With 16GB Mac, this triggers Spot VM creation more frequently
- More VM creation = higher costs (even if minimal)
- Optimize local performance = less GCP usage = lower costs

#### What to Do:
1. **Memory Profiling:**
   ```bash
   # Profile Multi-Space Vision during heavy use
   # Identify memory hotspots
   # Target: Reduce 1.2GB → 800MB (33% reduction)
   ```

2. **Optimize Image Processing Pipeline:**
   - Current: 9-stage pipeline captures all spaces
   - Optimize: Lazy capture (only active spaces by default)
   - Add: Configurable "monitored spaces" list
   - Implement: Aggressive image compression for inactive spaces

3. **Cache Optimization:**
   - Bloom Filter: Already efficient
   - Semantic Cache: Review TTL settings
   - Predictive Engine: Reduce model size if possible
   - VSMS: Implement cache eviction for old spaces

4. **Benchmarking:**
   - Before: Measure RAM usage during typical workflow
   - After: Re-measure with optimizations
   - Target: Keep under 80% RAM to avoid Spot VM trigger

#### Success Criteria:
- [ ] Multi-Space Vision memory reduced by 25-33%
- [ ] No degradation in accuracy/functionality
- [ ] Spot VM triggers less frequently
- [ ] Documented performance improvements

#### Files to Modify:
- `backend/vision/multi_space_vision.py`
- `backend/vision/pipeline_orchestrator.py`
- `backend/cache/semantic_cache.py`

---

### Priority 4: **Production Readiness Hardening** 🛡️ MEDIUM
**Branch Name:** `production-hardening`
**Estimated Effort:** 2-3 days
**Recommended Before:** Heavy production use

#### Why This Matters:
- Current system is stable but not battle-tested
- Need better error recovery for GCP failures
- Need comprehensive logging for debugging
- Need monitoring for proactive issue detection

#### What to Do:
1. **Error Recovery:**
   - Handle: GCP quota exceeded errors
   - Handle: Cloud Storage access denied
   - Handle: Spot VM preemption (should already work, but test)
   - Handle: Network partition between local/GCP
   - Implement: Graceful degradation (fall back to local only)

2. **Comprehensive Logging:**
   - Structured logging with correlation IDs
   - Separate log files: local.log, gcp.log, hybrid.log
   - Log retention: 7 days local, 30 days Cloud Logging
   - Searchable logs in GCP Cloud Logging

3. **Health Monitoring:**
   - Enhanced `/health` endpoint with component status
   - Monitoring dashboard showing:
     - Local system health
     - GCP connection status
     - Cloud SQL connection status
     - Active VM status
     - Request routing stats
   - Alert on: Repeated failures, high error rates

4. **Deployment Safety:**
   - Blue/Green deployment for backend updates
   - Automated rollback on health check failures
   - Canary deployments for risky changes

#### Success Criteria:
- [ ] All failure scenarios handled gracefully
- [ ] Comprehensive logging implemented
- [ ] Health monitoring dashboard complete
- [ ] Automated rollback tested and working

---

### Priority 5: **Cloud SQL Learning Database Migration** 🧠 MEDIUM
**Branch Name:** `cloud-sql-learning-migration`
**Estimated Effort:** 2-3 days
**Current State:** Cloud SQL configured but not fully utilized

#### Why This is Valuable:
- Currently using local SQLite for learning data
- Cloud SQL enables cross-device learning
- Learning patterns from Mac can help GCP instances
- Persistent learning survives local machine resets

#### What to Do:
1. **Schema Migration:**
   - Design unified schema for learning data
   - Create migration scripts: SQLite → Cloud SQL
   - Test: Backup SQLite, migrate, verify data integrity

2. **Hybrid Learning Strategy:**
   - Local cache: Recent learning data (last 7 days)
   - Cloud SQL: Complete learning history
   - Sync: Background sync every 30 minutes
   - Conflict resolution: Latest timestamp wins

3. **Learning Data Types to Migrate:**
   - Goal Inference patterns
   - Display connection preferences
   - Voice command patterns
   - Error recovery strategies
   - User workflow patterns

4. **Performance Optimization:**
   - Connection pooling for Cloud SQL
   - Read replicas for read-heavy queries
   - Caching for frequently accessed patterns

#### Success Criteria:
- [ ] All learning data migrated to Cloud SQL
- [ ] Local cache + Cloud sync working
- [ ] No performance degradation
- [ ] Learning persists across sessions/devices

#### Files to Create/Modify:
- `backend/database/cloud_sql_manager.py` (new)
- `backend/database/learning_sync.py` (new)
- `scripts/migrate_learning_data.sh` (new)

---

### Priority 6: **Advanced Hybrid Routing Logic** 🎯 LOW
**Branch Name:** `advanced-hybrid-routing`
**Estimated Effort:** 3-5 days
**Current State:** Basic routing by RAM threshold

#### Why This Could Be Better:
- Current: Route to GCP when RAM > 85%
- Better: Route by request complexity, not just RAM
- Smarter: Learn which requests are better on GCP vs local

#### What to Do:
1. **Request Complexity Analysis:**
   - Classify requests: Simple, Medium, Complex
   - Simple: "What time is it?" → Always local
   - Medium: "Summarize this page" → Local if RAM available
   - Complex: "Analyze all spaces and create report" → Prefer GCP

2. **ML-Based Routing:**
   - Train model on: Request type → Best execution location
   - Features: Request length, history, current RAM, time of day
   - Output: Probability(local vs GCP)

3. **Cost-Aware Routing:**
   - Track: Cost per request type (local vs GCP)
   - Optimize: Route to minimize cost while maintaining performance
   - Learn: User tolerance for latency vs cost

4. **Predictive Scaling:**
   - Predict: "User likely to need GCP in next 10 minutes"
   - Pre-warm: Create Spot VM before RAM hits 85%
   - Result: Faster response times, no wait for VM creation

#### Success Criteria:
- [ ] Request classification implemented
- [ ] ML-based routing model trained
- [ ] Cost-aware routing working
- [ ] Predictive scaling reduces latency

---

## 📊 Priority Matrix

| Priority | Branch Name | Effort | Impact | Urgency | Dependencies |
|----------|------------|--------|--------|---------|--------------|
| **1** | `test-spot-vm-deployment` | 2-4h | 🔴 Critical | ⚡ Immediate | None |
| **2** | `cost-monitoring-alerts` | 1-2d | 🔴 High | 🟡 High | Priority 1 |
| **3** | `multi-monitor-performance` | 3-5d | 🟡 High | 🟢 Medium | None |
| **4** | `production-hardening` | 2-3d | 🟡 Medium | 🟢 Medium | Priority 1 |
| **5** | `cloud-sql-learning-migration` | 2-3d | 🟢 Medium | 🟢 Low | None |
| **6** | `advanced-hybrid-routing` | 3-5d | 🟢 Low | 🟢 Low | Priority 1 |

---

## 🎯 Recommended Workflow

### This Week (Priority 1 & 2):
```bash
# Day 1-2: Test Spot VM Flow
git checkout -b test-spot-vm-deployment
# Test, fix, validate, document
git checkout main && git merge test-spot-vm-deployment
git push origin main

# Day 3-4: Cost Monitoring
git checkout -b cost-monitoring-alerts
# Implement, test, deploy
git checkout main && git merge cost-monitoring-alerts
git push origin main
```

### Next Week (Priority 3 or 4):
Choose based on your goals:
- **Reduce costs long-term?** → Priority 3 (Performance)
- **Increase reliability?** → Priority 4 (Hardening)

### Future (Priority 5 & 6):
Nice-to-haves that can wait until core system is solid.

---

## 🚨 Critical Issues to Watch

### Issue 1: Orphaned VMs
**Status:** Mitigated (3hr max runtime + cleanup script)
**Action:** Monitor first week of usage
**Branch:** `test-spot-vm-deployment` (validation)

### Issue 2: Cloud Storage Deployment Untested
**Status:** ⚠️ NOT TESTED
**Action:** **MUST TEST** in Priority 1
**Branch:** `test-spot-vm-deployment`

### Issue 3: No Cost Visibility
**Status:** ⚠️ BLIND SPOT
**Action:** Implement Priority 2 ASAP
**Branch:** `cost-monitoring-alerts`

---

## 📈 Success Metrics

### Week 1 (After Priority 1 & 2):
- ✅ Spot VM creation tested and working
- ✅ Actual cost < $20/month
- ✅ Zero orphaned VMs
- ✅ Cost dashboard showing real data

### Month 1 (After Priority 3 or 4):
- ✅ Average RAM usage < 80% (fewer VM triggers)
- ✅ All GCP failures handled gracefully
- ✅ Comprehensive logs for debugging
- ✅ Cost stabilized at $11-15/month

### Month 2+ (After Priority 5 & 6):
- ✅ Learning data persisting across sessions
- ✅ Intelligent routing reducing GCP usage
- ✅ System self-optimizes over time

---

## 🎓 Why This Order?

1. **Priority 1 (Test)**: Can't optimize what you haven't validated
2. **Priority 2 (Cost)**: Need visibility before it becomes a problem
3. **Priority 3 (Performance)**: Reduce costs by reducing GCP usage
4. **Priority 4 (Hardening)**: Make it production-ready
5. **Priority 5 (Cloud SQL)**: Enable advanced features
6. **Priority 6 (Advanced Routing)**: Polish and optimize

**Start with Priority 1 TODAY.** Everything else builds on it.

---

## 🎯 Your Next Command

```bash
# Create the test branch
git checkout -b test-spot-vm-deployment

# Start JARVIS and monitor
python start_system.py

# Open Activity Monitor
# Watch RAM usage
# Test VM creation when RAM > 85%
# Or temporarily lower threshold for faster testing
```

**Let me know when Priority 1 is complete, and we'll tackle Priority 2 together!** 🚀
