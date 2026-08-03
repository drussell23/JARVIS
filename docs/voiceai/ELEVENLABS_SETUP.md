# ElevenLabs TTS Integration Setup Guide

This guide explains how to integrate ElevenLabs TTS for African American, African, and Asian accent voices in JARVIS voice security testing.

## Overview

JARVIS now supports **hybrid TTS** using multiple providers:
- **Google Cloud TTS**: 60 voices (US, British, Australian, Indian, Hispanic, European accents)
- **ElevenLabs TTS**: 16+ voices (African American, African, Asian accents)

**Total**: 76+ unique voices for comprehensive security testing

## Features

✅ **Hybrid caching** - Generate voices once, reuse forever (FREE tier optimization)
✅ **Async API** - Fast parallel voice generation
✅ **Dynamic routing** - Automatically selects best provider for each accent
✅ **Zero hardcoding** - Fully configurable voice profiles
✅ **Backward compatible** - Existing GCP TTS code continues to work

## Setup Steps

### 1. Get ElevenLabs API Key

1. Go to [elevenlabs.io](https://elevenlabs.io)
2. Sign up for a free account
3. Navigate to Settings → API Keys
4. Copy your API key

### 2. Set Environment Variable

Add to your `~/.zshrc` or `~/.bashrc`:

```bash
export ELEVENLABS_API_KEY='your-api-key-here'
```

Then reload:
```bash
source ~/.zshrc  # or source ~/.bashrc
```

### 3. Discover Available Voices

Run the discovery tool to see all available ElevenLabs voices:

```bash
python3 setup_elevenlabs_voices.py --discover
```

This will:
- Fetch all voices from ElevenLabs API
- Save them to `~/.jarvis/tts_cache/elevenlabs/all_voices.json`
- Show you available voices for configuration

### 4. Configure Curated Voices

1. Open `all_voices.json` and find voices that match desired accents:
   - African American English
   - Nigerian/Kenyan/South African English
   - Chinese/Japanese/Korean-accented English

2. Edit `setup_elevenlabs_voices.py` and update `CURATED_VOICES`:
   - Replace `PLACEHOLDER_ID_1`, `PLACEHOLDER_ID_2`, etc. with actual voice IDs
   - Update names, descriptions, and settings as needed

3. Save the configuration:
```bash
python3 setup_elevenlabs_voices.py --configure
```

This creates `~/.jarvis/tts_cache/elevenlabs/curated_voices.json`

### 5. Test Voice Generation

Generate sample audio to verify voices work:

```bash
python3 setup_elevenlabs_voices.py --test
```

This will:
- Generate 3 sample voices saying "unlock my screen"
- Save to `/tmp/elevenlabs_test_samples/`
- Let you verify voice quality

Listen to samples:
```bash
afplay /tmp/elevenlabs_test_samples/*.mp3
# or
open /tmp/elevenlabs_test_samples
```

### 6. Run Voice Security Test

Run the full security test with both providers:

```bash
python3 backend/voice_unlock/voice_security_tester.py
```

The system will:
- Use GCP TTS for 60 voices (US, British, Australian, etc.)
- Use ElevenLabs for 16 voices (African American, African, Asian)
- Cache all voices for future reuse (stays within free tier)
- Generate comprehensive security report

## Configuration Files

### Cache Directory Structure

```
~/.jarvis/tts_cache/
├── gcp/
│   └── *.mp3                    # GCP TTS cached audio
└── elevenlabs/
    ├── all_voices.json          # All discovered voices
    ├── curated_voices.json      # Your configured voices
    └── *.mp3                    # ElevenLabs cached audio
```

### Curated Voice Configuration Format

```json
{
  "voice_name": {
    "voice_id": "actual_voice_id_from_api",
    "name": "DisplayName",
    "accent": "african_american|african|asian",
    "gender": "male|female|neutral",
    "description": "Voice description",
    "language_code": "en",
    "stability": 0.5,
    "similarity_boost": 0.75,
    "style": 0.0,
    "use_speaker_boost": true
  }
}
```

## Free Tier Optimization

### ElevenLabs Free Tier
- **10,000 characters/month**
- Phrase "unlock my screen" = 17 characters
- 16 voices × 17 chars = **272 characters** (one-time generation)
- **After caching**: 0 API calls, unlimited testing

### Cost Breakdown
| Provider | Monthly Cost | Voices | Free Tier |
|----------|-------------|--------|-----------|
| Google Cloud TTS | $0 | 60 | First 4M chars free |
| ElevenLabs | $0 | 16 | 10k chars free |
| **Total** | **$0** | **76** | ✅ **Fully free** |

## Usage Examples

### Basic Security Test
```bash
python3 backend/voice_unlock/voice_security_tester.py
```

### With Audio Playback
```python
from backend.voice_unlock.voice_security_tester import VoiceSecurityTester, PlaybackConfig

config = {
    'authorized_user': 'Sir',
    'test_phrase': 'unlock my screen',
    'test_mode': 'full'  # Test all 76 voices
}

playback_config = PlaybackConfig(enabled=True)

tester = VoiceSecurityTester(config=config, playback_config=playback_config)
results = await tester.run_security_tests()
```

### Check Cache Stats
```python
from backend.audio.tts_provider_manager import TTSProviderManager

manager = TTSProviderManager()
stats = manager.get_cache_stats()

print(f"GCP cached files: {stats['providers']['gcp']['file_count']}")
print(f"ElevenLabs cached files: {stats['providers']['elevenlabs']['file_count']}")
```

## Troubleshooting

### API Key Not Found
```
⚠️  No ElevenLabs API key found. Set ELEVENLABS_API_KEY environment variable.
```

**Solution**: Export the environment variable and restart terminal

### Voice IDs Still Placeholders
```
⚠️  WARNING: 16 voices still have PLACEHOLDER_ID
```

**Solution**: Run `--discover`, then update `CURATED_VOICES` with real voice IDs

### No Voices Configured
```
❌ No curated voices configured!
```

**Solution**: Run `python3 setup_elevenlabs_voices.py --configure`

### API Rate Limits
```
❌ API error 429: Rate limit exceeded
```

**Solution**: Wait a moment and retry. Free tier has rate limits.

## Architecture

### Provider Selection Logic

```
Voice Request
    ↓
Is it African American/African/Asian accent?
    ├─ YES → Use ElevenLabs TTS
    └─ NO  → Use Google Cloud TTS
    ↓
Check cache first
    ├─ Cache HIT  → Return cached audio (FREE)
    └─ Cache MISS → Generate via API → Save to cache
```

### File Structure

```
backend/audio/
├── gcp_tts_service.py           # GCP TTS (existing)
├── elevenlabs_tts_service.py    # NEW: ElevenLabs TTS
└── tts_provider_manager.py      # NEW: Multi-provider manager

backend/voice_unlock/
└── voice_security_tester.py     # UPDATED: Uses multi-provider

setup_elevenlabs_voices.py       # NEW: Setup tool
ELEVENLABS_SETUP.md             # This file
```

## Next Steps

1. ✅ Set `ELEVENLABS_API_KEY` environment variable
2. ✅ Run `python3 setup_elevenlabs_voices.py --discover`
3. ✅ Review `all_voices.json` and find desired accents
4. ✅ Update `CURATED_VOICES` in `setup_elevenlabs_voices.py`
5. ✅ Run `python3 setup_elevenlabs_voices.py --configure`
6. ✅ Test with `python3 setup_elevenlabs_voices.py --test`
7. ✅ Run full security test

## Support

For issues or questions:
- Check the troubleshooting section above
- Review `~/.jarvis/tts_cache/elevenlabs/all_voices.json` for available voices
- Verify API key is set correctly: `echo $ELEVENLABS_API_KEY`
