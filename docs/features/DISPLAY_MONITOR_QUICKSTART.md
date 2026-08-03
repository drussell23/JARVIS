# JARVIS Display Monitor - Quick Start

**Get up and running in 2 minutes!**

---

## 🚀 Installation

No installation needed! Uses built-in Python modules.

---

## ⚡ Quick Start (3 Steps)

### Step 1: Add Your Display

```bash
python3 start_tv_monitoring.py --add-display
```

Follow the prompts:
```
Display ID: living_room_tv
Display Name: Living Room TV
Display Type: airplay
Aliases: Living Room,LG TV,TV
Auto-connect: no
Auto-prompt: yes
Connection mode: extend
Priority: 1

✅ Added Living Room TV to monitoring
```

### Step 2: Test Voice (Optional)

```bash
python3 start_tv_monitoring.py --test-voice
```

You should hear: "JARVIS display monitoring is online, sir."

### Step 3: Start Monitoring!

```bash
python3 start_tv_monitoring.py
```

Output:
```
🖥️  JARVIS Advanced Display Monitor
================================================================================

📋 Configuration:
   • Monitoring: ✅ Enabled
   • Displays: 1 monitored
   • Voice: ✅ Enabled
   • Detection: applescript, coregraphics
   • Caching: ✅ Enabled

📺 Monitored Displays:
   ✅ Living Room TV - 📢 Prompt

🚀 Starting display monitoring...
   Press Ctrl+C to stop
```

**That's it!** JARVIS will now:
1. Detect when your TV becomes available
2. Speak: "Sir, I see your Living Room TV is now available..."
3. Prompt you to connect

---

## 🎯 What Happens Next?

When your display is detected:

1. **Voice Prompt** (if enabled):
   - "Sir, I see your Living Room TV is now available. Would you like to extend your display to it?"

2. **Terminal Notification**:
   ```
   ✨ Detected: Living Room TV (Living Room TV)
   ```

3. **Connection** (if auto-connect enabled):
   ```
   ✅ Connected: Living Room TV
   [Voice]: "Connected to Living Room TV, sir."
   ```

---

## 🔧 Common Commands

```bash
# List all monitored displays
python3 start_tv_monitoring.py --list-displays

# Check monitor status
python3 start_tv_monitoring.py --status

# Run tests
python3 test_advanced_display_monitor.py --quick

# Use simple legacy mode
python3 start_tv_monitoring.py --simple
```

---

## ⚙️ Quick Configuration

Edit `backend/config/display_monitor_config.json`:

```json
{
  "display_monitoring": {
    "check_interval_seconds": 10.0  // How often to check
  },
  "voice_integration": {
    "enabled": true,                // Enable voice
    "speak_on_detection": true      // Speak when detected
  }
}
```

---

## 🔒 macOS Permissions

First time you run, macOS will ask for permissions:
1. **Accessibility** - Click "Open System Settings"
2. Enable "Terminal" or "Python"
3. Restart the monitor

---

## 🐛 Troubleshooting

### Display not detected?

```bash
# Check what's available
osascript -e 'tell application "System Events" to tell process "ControlCenter" to get name of menu items of menu 1 of menu bar item "Screen Mirroring" of menu bar 1'
```

Make sure your display name matches exactly!

### Voice not working?

```bash
# Test macOS say command
say "test"

# List available voices
say -v ?
```

### Need help?

```bash
# Run full tests
python3 test_advanced_display_monitor.py

# Check logs
python3 start_tv_monitoring.py --verbose
```

---

## 📚 Full Documentation

- **Usage Guide:** `DISPLAY_MONITOR_USAGE.md`
- **Edge Cases:** `VISION_MULTISPACE_EDGE_CASES.md`
- **Implementation:** `DISPLAY_MONITOR_IMPLEMENTATION_SUMMARY.md`

---

## ✨ Advanced Features

### Auto-Connect

Set `auto_connect: true` in config to automatically connect when detected.

### Custom Voice Messages

Edit `voice_integration.prompt_template` in config:
```json
{
  "prompt_template": "Your custom message for {display_name}"
}
```

### Multiple Displays

Add as many displays as you want:
```bash
python3 start_tv_monitoring.py --add-display
```

### Presets

```python
from display.display_config_manager import get_config_manager

config = get_config_manager()
config.apply_preset('performance')  # or 'minimal', 'voice_focused'
```

---

## 🎉 That's It!

You're now running JARVIS's advanced display monitoring system!

**Next Steps:**
1. ✅ Add your displays
2. ✅ Configure voice settings (optional)
3. ✅ Start monitoring
4. ✅ Enjoy automatic display detection!

---

**Questions? Check the full documentation:**
- `DISPLAY_MONITOR_USAGE.md` - Complete guide
- `VISION_MULTISPACE_EDGE_CASES.md` - Edge cases
- `DISPLAY_MONITOR_IMPLEMENTATION_SUMMARY.md` - Technical details
