# 🚀 JARVIS TTS Setup - Quick Start

## One-Command Setup

Run the interactive CLI wizard:

```bash
python3 setup_tts_voices.py
```

The wizard will:
1. ✅ Check/set your ElevenLabs API key (FREE tier)
2. ✅ Discover all available voices
3. ✅ Auto-select diverse accent voices
4. ✅ Save configuration
5. ✅ Test voice generation (optional)

**Total time**: ~2-3 minutes

---

## What You Need

### ElevenLabs FREE Account
- **Cost**: $0 (no credit card required)
- **Sign up**: https://elevenlabs.io/app/settings/api-keys
- **Free tier**: 10,000 characters/month
- **Our usage**: ~200 characters (one-time generation, then cached forever)

---

## What You Get

### Voice Coverage

| Provider | Voices | Accents | Cost |
|----------|--------|---------|------|
| GCP TTS | 60 | US, British, Australian, Indian, Hispanic, European | FREE |
| ElevenLabs | 16+ | African American, African, Asian | FREE |
| **Total** | **76+** | **Complete diversity** | **$0** |

### Cache Optimization
- Generate voices **once** using API
- Cache permanently in `~/.jarvis/tts_cache/`
- Reuse cached audio for all future tests
- **Result**: Zero API calls after initial setup

---

## Step-by-Step Walkthrough

### 1. Run Setup Wizard

```bash
python3 setup_tts_voices.py
```

### 2. Follow Prompts

```
[Step 1/5] ElevenLabs API Key Setup
   Enter your API key: sk_xxxxxxxxxxxxx
   Save to ~/.zshrc? [Y/n]: y

[Step 2/5] Discovering Available Voices
   ✅ Discovered 120 available voices

[Step 3/5] Auto-Selecting Diverse Voices
   AFRICAN_AMERICAN:
      ✅ JamesEarl (male)
      ✅ MayaAngelou (female)
   AFRICAN:
      ✅ NigerianEnglish (male)
      ✅ KenyanEnglish (female)
   ASIAN:
      ✅ ChineseAccent (male)
      ✅ JapaneseAccent (female)
   ✅ Auto-selected 16 diverse voices

[Step 4/5] Saving Voice Configuration
   ✅ Saved 16 voices to curated_voices.json

[Step 5/5] Testing Voice Generation
   Generate sample audio? [Y/n]: y
   🔊 Generating: JamesEarl (african_american)
   ✅ Saved: /tmp/jarvis_voice_samples/JamesEarl.mp3
```

### 3. Done!

Your setup is complete. The wizard auto-configured everything.

---

## Verify Setup

### Check Configuration

```bash
# View configured voices
cat ~/.jarvis/tts_cache/elevenlabs/curated_voices.json

# Check cache stats
ls -lh ~/.jarvis/tts_cache/elevenlabs/*.mp3
```

### Test Voice Generation

```bash
# Listen to sample voices
afplay /tmp/jarvis_voice_samples/*.mp3

# Or open in Finder
open /tmp/jarvis_voice_samples
```

### Run Security Test

```bash
# Test with all 76 voices
python3 backend/voice_unlock/voice_security_tester.py
```

---

## Advanced Options

### Manual Configuration

If you prefer manual setup:

```bash
# 1. Set API key
export ELEVENLABS_API_KEY='your-key-here'

# 2. Discover voices
python3 setup_elevenlabs_voices.py --discover

# 3. Review voices
cat ~/.jarvis/tts_cache/elevenlabs/all_voices.json

# 4. Edit configuration
nano setup_elevenlabs_voices.py  # Update CURATED_VOICES

# 5. Save config
python3 setup_elevenlabs_voices.py --configure

# 6. Test
python3 setup_elevenlabs_voices.py --test
```

### View Usage Stats

```python
from backend.audio.tts_provider_manager import TTSProviderManager
import asyncio

async def check_stats():
    manager = TTSProviderManager()
    stats = manager.get_usage_stats()
    print(f"Total requests: {stats['total_requests']}")
    print(f"Total characters: {stats['total_characters']}")

asyncio.run(check_stats())
```

---

## Troubleshooting

### API Key Issues

**Problem**: `⚠️  No ElevenLabs API key found`

**Solution**:
```bash
export ELEVENLABS_API_KEY='your-key-here'
source ~/.zshrc
```

### No Voices Found

**Problem**: `Auto-selected 0 voices`

**Solution**: Run wizard again, or manually configure voices in `setup_elevenlabs_voices.py`

### API Rate Limit

**Problem**: `❌ API error 429: Rate limit exceeded`

**Solution**: Wait 1 minute and retry. Free tier has rate limits.

---

## Free Tier Details

### ElevenLabs FREE Tier

- **Monthly limit**: 10,000 characters
- **Our usage**: ~200 characters (one-time)
- **Cached**: Yes (reuse forever)
- **Cost after setup**: $0

### Cost Breakdown

```
Setup (one-time):
  16 voices × "unlock my screen" (17 chars) = 272 characters

Ongoing usage:
  Cached voices: 0 API calls
  Total cost: $0/month
```

---

## What Happens Next?

After setup, your voice security tests will automatically use:

1. **GCP TTS** for 60 voices (US, British, Australian, etc.)
2. **ElevenLabs** for 16+ voices (African American, African, Asian)
3. **Total**: 76+ unique voices for comprehensive testing

All voices are **cached**, so zero API cost after initial generation.

---

## Support

Need help?

1. Check this guide
2. Review `ELEVENLABS_SETUP.md` for detailed docs
3. Verify API key: `echo $ELEVENLABS_API_KEY`
4. Check cache: `ls ~/.jarvis/tts_cache/elevenlabs/`

---

## Summary

```bash
# One command to rule them all
python3 setup_tts_voices.py

# Follow the prompts
# Takes 2-3 minutes
# FREE forever with caching
# 76+ diverse voices
# Zero maintenance
```

**That's it!** 🎉
