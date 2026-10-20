"""
TTS bridge (Microsoft Foundry) — engineer voice via the gpt-4o-mini-tts deployment.
"""
import asyncio
import os
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

from foundry import speak

load_dotenv()

AUDIO_DIR = Path(__file__).parent / "static" / "audio"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

# gpt-4o-mini-tts voices: alloy, ash, ballad, coral, echo, fable, nova, onyx, sage, shimmer, verse
VOICE = os.getenv("FOUNDRY_TTS_VOICE", "onyx")
SPEED = float(os.getenv("FOUNDRY_TTS_SPEED", "1.1") or 0) or None
INSTRUCTIONS = os.getenv(
  "FOUNDRY_TTS_INSTRUCTIONS",
  "You are a calm, focused Formula 1 race engineer on team radio. "
  "Speak clearly and briskly, short pauses, no drama.",
)
CLEANUP_MAX_AGE = int(os.getenv("TTS_CLEANUP_MAX_AGE", "3600"))


def cleanup_old_audio():
  now = time.time()
  for f in AUDIO_DIR.iterdir():
    if f.suffix.lower() == ".mp3" and (now - f.stat().st_mtime) > CLEANUP_MAX_AGE:
      f.unlink(missing_ok=True)


async def synthesize(text: str) -> str | None:
  """Speak `text`; returns the /audio/... URL of the MP3, or None on failure."""
  text = (text or "").strip()
  if not text:
    return None
  # Keep spoken lines short — faster synth + faster playback start
  if len(text) > 180:
    text = text[:177].rsplit(" ", 1)[0] + "…"

  try:
    audio = await asyncio.to_thread(speak, text, VOICE, INSTRUCTIONS, SPEED)
  except Exception as e:
    print(f"TTS failed: {e}")
    return None

  fname = f"{uuid.uuid4().hex}.mp3"
  (AUDIO_DIR / fname).write_bytes(audio)
  cleanup_old_audio()
  return f"/audio/{fname}"
