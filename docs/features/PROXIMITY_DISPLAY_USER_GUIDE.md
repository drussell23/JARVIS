# 🖥️ Proximity-Aware Display Connection System - User Guide

## 🎉 **Welcome to Spatially Intelligent JARVIS!**

Your MacBook Pro M1 can now automatically detect when you're near TVs and monitors using Bluetooth proximity from your Apple Watch or iPhone. JARVIS becomes environmentally intelligent - understanding your physical location and suggesting display connections contextually.

---

## 🚀 **Quick Start**

### **Step 1: Restart JARVIS Backend**

```bash
cd /Users/derekjrussell/Documents/repos/JARVIS-AI-Agent
python3 start_system.py
```

Look for this in the logs:
```
✅ Proximity-Aware Display API configured
```

### **Step 2: Check Proximity Status**

```bash
curl http://localhost:8000/api/proximity-display/status
```

**Response:**
```json
{
  "user_proximity": {
    "device_name": "Derek's Apple Watch",
    "distance": 2.3,
    "proximity_zone": "near"
  },
  "nearest_display": {
    "display_id": 1,
    "name": "MacBook Pro Built-in Display"
  },
  "proximity_scores": {
    "1": 0.95
  }
}
```

---

## 📍 **Configure Your Displays**

### **Method 1: API (Recommended)**

Register your Living Room TV:

```bash
curl -X POST http://localhost:8000/api/proximity-display/register \
  -H "Content-Type: application/json" \
  -d '{
    "display_id": 23,
    "location_name": "Living Room TV",
    "zone": "living_room",
    "min_distance": 2.0,
    "max_distance": 8.0,
    "auto_connect_enabled": true,
    "connection_priority": 0.8,
    "tags": ["tv", "entertainment", "4k"]
  }'
```

Register your Office Monitor:

```bash
curl -X POST http://localhost:8000/api/proximity-display/register \
  -H "Content-Type: application/json" \
  -d '{
    "display_id": 2,
    "location_name": "Office 4K Monitor",
    "zone": "office",
    "min_distance": 1.0,
    "max_distance": 5.0,
    "auto_connect_enabled": true,
    "connection_priority": 0.9,
    "tags": ["monitor", "work", "primary"]
  }'
```

### **Method 2: JSON File**

Edit `backend/config/display_locations.json`:

```json
{
  "display_locations": {
    "1": {
      "display_id": 1,
      "location_name": "MacBook Pro Built-in Display",
      "zone": "mobile",
      "expected_proximity_range": [0.0, 2.0],
      "auto_connect_enabled": false,
      "connection_priority": 1.0,
      "tags": ["builtin", "primary"]
    },
    "23": {
      "display_id": 23,
      "location_name": "Living Room TV",
      "zone": "living_room",
      "expected_proximity_range": [2.0, 8.0],
      "auto_connect_enabled": true,
      "connection_priority": 0.8,
      "tags": ["tv", "entertainment"]
    }
  }
}
```

---

## 🎯 **How It Works**

### **1. Bluetooth Proximity Detection**

JARVIS scans for your Apple Watch or iPhone via Bluetooth:

- **Strong Signal (-40 dBm):** ~0.5-1m away
- **Medium Signal (-60 dBm):** ~2-3m away
- **Weak Signal (-80 dBm):** ~8-10m away

### **2. Distance Estimation**

Uses path loss model:
```
distance = 10^((RSSI_0 - RSSI) / (10 * n))

RSSI_0 = -59 dBm  (reference at 1 meter)
n = 2.5           (environmental factor)
```

**Accuracy:** ±2-3 meters (typical for Bluetooth)

### **3. Proximity Zones**

- **IMMEDIATE:** 0-1m (touch distance)
- **NEAR:** 1-3m (conversational distance)
- **ROOM:** 3-8m (same room)
- **FAR:** 8-15m (adjacent room)
- **OUT_OF_RANGE:** >15m (too far)

### **4. Proximity Scoring**

For each display:
```
proximity_score = 0.4 * config_priority + 0.6 * distance_score

distance_score = 1.0 - (distance / 15.0)  # Exponential decay

If zone == IMMEDIATE: score *= 1.2
If zone == NEAR: score *= 1.1
```

**Result:** 0.0 (far) to 1.0 (very close)

### **5. Connection Decision**

| Distance | Confidence | Action |
|----------|------------|--------|
| < 1.5m | > 0.8 | AUTO_CONNECT (future) |
| 1.5-5m | > 0.5 | PROMPT_USER |
| > 5m | any | IGNORE |

---

## 📊 **API Reference**

### **GET /api/proximity-display/status**
Get current proximity and display status

```bash
curl http://localhost:8000/api/proximity-display/status
```

### **GET /api/proximity-display/context**
Get full proximity-display context (comprehensive)

```bash
curl http://localhost:8000/api/proximity-display/context
```

### **POST /api/proximity-display/register**
Register a display location

```bash
curl -X POST http://localhost:8000/api/proximity-display/register \
  -H "Content-Type: application/json" \
  -d '{...}'
```

### **POST /api/proximity-display/decision**
Get intelligent connection recommendation

```bash
curl -X POST http://localhost:8000/api/proximity-display/decision
```

### **POST /api/proximity-display/scan**
Trigger immediate Bluetooth scan

```bash
curl -X POST http://localhost:8000/api/proximity-display/scan
```

### **GET /api/proximity-display/stats**
Get service statistics

```bash
curl http://localhost:8000/api/proximity-display/stats
```

### **GET /api/proximity-display/displays**
Get all displays with proximity scores

```bash
curl http://localhost:8000/api/proximity-display/displays
```

### **GET /api/proximity-display/health**
Health check

```bash
curl http://localhost:8000/api/proximity-display/health
```

---

## 🎓 **Real-World Examples**

### **Example 1: Working in Office**

```
You: Sitting at desk with MacBook (0.5m from built-in display)
Apple Watch: RSSI = -45 dBm
Distance: 0.8m
Zone: IMMEDIATE
Proximity Score (Built-in): 0.95

JARVIS: "Primary display (Built-in) is your nearest screen"
```

### **Example 2: Walking to Living Room**

```
You: Walk within 3m of Living Room TV
Apple Watch: RSSI = -55 dBm
Distance: 2.3m
Zone: NEAR
Proximity Score (TV): 0.85

JARVIS: "I see you're near the Living Room TV (2.3m away). 
         Would you like to connect?"
```

### **Example 3: Moving Between Rooms**

```
BEFORE (Office):
  - Office Monitor: score 0.92 (1.2m away)
  - Living Room TV: score 0.15 (12m away)
  
You: Walk to living room

AFTER (Living Room):
  - Living Room TV: score 0.85 (2.5m away)
  - Office Monitor: score 0.18 (10m away)

JARVIS: "Nearest display changed from Office Monitor to Living Room TV"
```

---

## ⚙️ **Configuration Parameters**

### **Display Location**

| Parameter | Description | Example |
|-----------|-------------|---------|
| `display_id` | CoreGraphics display ID | 1, 23, 2 |
| `location_name` | Human-readable name | "Living Room TV" |
| `zone` | Location zone | "living_room", "office" |
| `min_distance` | Minimum expected distance (m) | 2.0 |
| `max_distance` | Maximum expected distance (m) | 8.0 |
| `auto_connect_enabled` | Allow auto-connection | true/false |
| `connection_priority` | Base priority (0.0-1.0) | 0.8 |
| `tags` | Custom tags | ["tv", "4k"] |

### **Proximity Thresholds**

| Threshold | Default | Description |
|-----------|---------|-------------|
| `immediate_distance` | 1.0m | Immediate proximity zone |
| `near_distance` | 3.0m | Near proximity zone |
| `room_distance` | 8.0m | Same room zone |
| `far_distance` | 15.0m | Far zone |
| `auto_connect_distance` | 1.5m | Auto-connect threshold |
| `auto_connect_confidence` | 0.8 | Min confidence for auto-connect |
| `prompt_user_confidence` | 0.5 | Min confidence to prompt |

---

## 🐛 **Troubleshooting**

### **"No proximity data"**

**Problem:** JARVIS can't detect your Apple Watch/iPhone

**Solutions:**
1. Make sure your Apple Watch or iPhone is paired and nearby
2. Check Bluetooth is enabled on your Mac
3. Ensure JARVIS has Bluetooth permissions:
   - System Settings → Privacy & Security → Bluetooth
4. Try manual scan:
   ```bash
   curl -X POST http://localhost:8000/api/proximity-display/scan
   ```

### **"Inaccurate distance"**

**Problem:** Distance estimates seem off

**Causes:**
- Bluetooth signal interference (walls, furniture)
- RSSI variance (±2-3m is normal)
- Environmental factors (path loss exponent)

**Solutions:**
- Use Kalman filtering (already enabled)
- Adjust path loss exponent (default: 2.5)
- Increase smoothing window (default: 5 samples)

### **"Display not detected"**

**Problem:** Your TV/monitor isn't showing up

**Solutions:**
1. Make sure display is powered on
2. Check display is connected (cable/wireless)
3. Run display detection:
   ```bash
   curl http://localhost:8000/api/proximity-display/displays
   ```
4. Check display ID matches your configuration

### **"Configuration not persisting"**

**Problem:** Display locations reset after restart

**Solutions:**
1. Check config file exists:
   ```bash
   ls backend/config/display_locations.json
   ```
2. Check file permissions (should be writable)
3. Verify JSON syntax is valid

---

## 📈 **Performance Tips**

### **Battery Life**

Bluetooth scanning uses power. To optimize:
- ✅ Already optimized: Adaptive scan intervals
- ✅ Kalman filtering reduces redundant scans
- ✅ Cache display detection results (5s TTL)

**Battery Impact:** ~2-5% per hour (typical)

### **Accuracy**

To improve distance accuracy:
1. Calibrate RSSI reference:
   - Stand exactly 1m from display
   - Note RSSI value
   - Update `rssi_at_1m` in config
2. Adjust path loss exponent:
   - Open office: n = 2.0
   - Home/walls: n = 2.5
   - Dense obstacles: n = 3.0-4.0

### **Responsiveness**

Adjust scan frequency:
```python
# In bluetooth_proximity_service.py
scan_interval = 1.0  # seconds (default)
```

Lower = more responsive, higher battery use  
Higher = less responsive, better battery life

---

## 🎊 **What's Next?**

### **Phase 1C: Command Routing (Coming Soon)**
- Voice commands: "Show me X on the nearest display"
- JARVIS acknowledges: "I see you're near the Living Room TV"
- Auto-route display output based on proximity

### **Phase 1D: Auto-Connection (Future)**
- Automatic display mirroring/extending
- "When you walk to the living room, display extends automatically"

### **Phase 2.0: ML Learning (Future)**
- JARVIS learns your preferences
- "I notice you usually connect to the TV at 7pm"
- Predictive connection suggestions

---

## ✅ **Quick Reference**

### **Get Status:**
```bash
curl http://localhost:8000/api/proximity-display/status
```

### **Register Display:**
```bash
curl -X POST http://localhost:8000/api/proximity-display/register \
  -H "Content-Type: application/json" \
  -d '{"display_id": 23, "location_name": "Living Room TV", ...}'
```

### **Get Decision:**
```bash
curl -X POST http://localhost:8000/api/proximity-display/decision
```

### **Check Health:**
```bash
curl http://localhost:8000/api/proximity-display/health
```

---

**JARVIS is now spatially intelligent! Enjoy your proximity-aware display system!** 🎉

*User Guide Version: 1.0*  
*Date: 2025-10-14*  
*For: MacBook Pro M1 + Apple Watch/iPhone*
