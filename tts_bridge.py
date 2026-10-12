"""
TTS bridge (ElevenLabs) — flash model for low-latency pit radio.
"""
import asyncio, uuid, os, time
from pathlib import Path
from dotenv import load_dotenv
from elevenlabs import ElevenLabs
from elevenlabs.types.voice_settings import VoiceSettings

load_dotenv()

AUDIO_DIR = Path("static/audio")
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "fxwDjR24F8nuQhDtF5eD")
API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
# Flash is built for low latency (~75ms vs multilingual_v2 quality model)
MODEL_ID = os.getenv("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5")
OUTPUT_FORMAT = os.getenv("ELEVENLABS_OUTPUT_FORMAT", "mp3_22050_32")
# 0–4; higher = lower latency, slight quality tradeoff
LATENCY_OPT = int(os.getenv("ELEVENLABS_LATENCY_OPT", "4"))
CLEANUP_MAX_AGE = int(os.getenv("TTS_CLEANUP_MAX_AGE", "3600"))

_client = ElevenLabs(api_key=API_KEY) if API_KEY else None
_VOICE_SETTINGS = VoiceSettings(
    stability=0.4,
    similarity_boost=0.75,
    style=0.0,
    use_speaker_boost=False,
)

def cleanup_old_audio():
    now = time.time()
    for f in AUDIO_DIR.iterdir():
        if f.suffix.lower() == ".mp3" and (now - f.stat().st_mtime) > CLEANUP_MAX_AGE:
            f.unlink(missing_ok=True)

async def synthesize(text: str) -> str | None:
    text = (text or "").strip()
    if not text:
        return None
    # Keep spoken lines short — faster synth + faster playback start
    if len(text) > 180:
        text = text[:177].rsplit(" ", 1)[0] + "…"

    fname = f"{uuid.uuid4().hex}.mp3"
    fpath = AUDIO_DIR / fname
    loop = asyncio.get_running_loop()
    try:
        audio = await loop.run_in_executor(None, _sync_synthesize, text)
    except Exception as e:
        print(f"TTS failed: {e}")
        return None
    fpath.write_bytes(audio)
    cleanup_old_audio()
    return f"/audio/{fname}"

def _sync_synthesize(text: str) -> bytes:
    if _client is None:
        raise RuntimeError("ELEVENLABS_API_KEY is not set")
    chunks = []
    for chunk in _client.text_to_speech.convert(
        text=text,
        voice_id=VOICE_ID,
        model_id=MODEL_ID,
        output_format=OUTPUT_FORMAT,
        optimize_streaming_latency=LATENCY_OPT,
        voice_settings=_VOICE_SETTINGS,
    ):
        chunks.append(chunk)
    return b"".join(chunks)
