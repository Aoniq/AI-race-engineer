"""
Microsoft Foundry client — one Foundry (Azure OpenAI) resource serves all three jobs:

  chat  -> FOUNDRY_CHAT_DEPLOYMENT  (e.g. gpt-5.4-mini)       strategy + radio replies
  tts   -> FOUNDRY_TTS_DEPLOYMENT   (e.g. gpt-4o-mini-tts)    engineer voice
  stt   -> FOUNDRY_STT_DEPLOYMENT   (e.g. gpt-4o-mini-transcribe)  driver's pit-radio call

Authentication: FOUNDRY_API_KEY, or leave it empty to sign in with Microsoft Entra ID
(`az login` locally; needs `pip install azure-identity` and the
"Cognitive Services OpenAI User" role on the resource).
"""
from __future__ import annotations

import json
import os
import re
from urllib.parse import urlparse

import openai
from dotenv import load_dotenv
from openai import AzureOpenAI

load_dotenv()

ENDPOINT = os.getenv("FOUNDRY_ENDPOINT", "").strip()
API_KEY = os.getenv("FOUNDRY_API_KEY", "").strip()
API_VERSION = os.getenv("FOUNDRY_API_VERSION", "2025-04-01-preview").strip()

CHAT_DEPLOYMENT = os.getenv("FOUNDRY_CHAT_DEPLOYMENT", "gpt-5.4-mini").strip()
TTS_DEPLOYMENT = os.getenv("FOUNDRY_TTS_DEPLOYMENT", "gpt-4o-mini-tts").strip()
STT_DEPLOYMENT = os.getenv("FOUNDRY_STT_DEPLOYMENT", "gpt-4o-mini-transcribe").strip()

# Reasoning models (gpt-5 family) only. Ignored automatically if the model rejects it.
STRATEGY_REASONING = os.getenv("FOUNDRY_REASONING_EFFORT", "low").strip()
RADIO_REASONING = os.getenv("FOUNDRY_RADIO_REASONING_EFFORT", "low").strip()

# Language the driver speaks and the engineer answers in (ISO-639-1)
RADIO_LANGUAGE = os.getenv("RADIO_LANGUAGE", "en").strip().lower() or "en"
_LANGUAGE_NAMES = {
  "en": "English", "nl": "Dutch", "de": "German", "fr": "French", "es": "Spanish",
  "it": "Italian", "pt": "Portuguese", "pl": "Polish", "ja": "Japanese", "zh": "Chinese",
}
RADIO_LANGUAGE_NAME = _LANGUAGE_NAMES.get(RADIO_LANGUAGE, RADIO_LANGUAGE)


def _base_endpoint(url: str) -> str:
  """Accept a resource or project URL; the SDK needs https://<host>/ only."""
  if not url:
    return ""
  if "://" not in url:
    url = "https://" + url
  p = urlparse(url)
  return f"{p.scheme}://{p.netloc}/"


def _make_client() -> AzureOpenAI | None:
  endpoint = _base_endpoint(ENDPOINT)
  if not endpoint:
    print("[Foundry] FOUNDRY_ENDPOINT is not set — AI strategy, voice and radio are off.")
    return None
  common = dict(azure_endpoint=endpoint, api_version=API_VERSION, max_retries=1)
  if API_KEY:
    return AzureOpenAI(api_key=API_KEY, **common)
  try:
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
  except ImportError:
    print("[Foundry] No FOUNDRY_API_KEY and azure-identity is not installed. "
          "Set the key or run: pip install azure-identity")
    return None
  token_provider = get_bearer_token_provider(
    DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
  )
  print("[Foundry] Using Microsoft Entra ID sign-in (no API key set).")
  return AzureOpenAI(azure_ad_token_provider=token_provider, **common)


client = _make_client()

# Parameters a deployment turned out not to support; learned once, then skipped.
_unsupported: set[str] = set()


_DROPPABLE = ("reasoning_effort", "max_completion_tokens", "response_format",
              "temperature", "instructions", "speed", "prompt", "language")


def _rejected_param(err: openai.BadRequestError, sent: dict) -> str | None:
  """Name of the optional parameter the service rejected, if that is what failed."""
  param = (getattr(err, "param", None) or "").strip()
  if param in _DROPPABLE and param in sent:
    return param
  msg = str(err).lower()
  if "content_filter" in msg or "filtered" in msg:
    return None
  for name in _DROPPABLE:
    if name in sent and re.search(rf"['\"`]{name}['\"`]", msg):
      return name
  return None


def _call_with_fallback(fn, kwargs: dict):
  """Call an SDK method, dropping parameters the deployment rejects (max 4 retries)."""
  for _ in range(5):
    sent = {k: v for k, v in kwargs.items() if k not in _unsupported}
    try:
      return fn(**sent)
    except openai.BadRequestError as e:
      bad = _rejected_param(e, sent)
      if not bad:
        raise
      print(f"[Foundry] Deployment does not accept '{bad}' — retrying without it.")
      _unsupported.add(bad)
      if bad == "max_completion_tokens" and "max_tokens" not in kwargs:
        kwargs = {**kwargs, "max_tokens": kwargs["max_completion_tokens"]}
  raise RuntimeError("Foundry request kept failing on unsupported parameters")


def _parse_json(text: str) -> dict:
  text = (text or "").strip()
  try:
    return json.loads(text)
  except json.JSONDecodeError:
    m = re.search(r"\{.*\}", text, re.S)
    if m:
      try:
        return json.loads(m.group(0))
      except json.JSONDecodeError:
        pass
  return {"voice": text}


def chat_json(system: str, user: str, *, max_tokens: int, timeout: float,
              reasoning_effort: str = "") -> dict:
  """One JSON-mode chat completion on the chat deployment."""
  if client is None:
    raise RuntimeError("Foundry is not configured")
  kwargs = dict(
    model=CHAT_DEPLOYMENT,
    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    response_format={"type": "json_object"},
    max_completion_tokens=max_tokens,
    timeout=timeout,
  )
  if reasoning_effort:
    kwargs["reasoning_effort"] = reasoning_effort
  resp = _call_with_fallback(client.chat.completions.create, kwargs)
  return _parse_json(resp.choices[0].message.content)


def speak(text: str, voice: str, instructions: str = "", speed: float | None = None) -> bytes:
  """Text-to-speech on the TTS deployment; returns MP3 bytes."""
  if client is None:
    raise RuntimeError("Foundry is not configured")
  kwargs = dict(model=TTS_DEPLOYMENT, voice=voice, input=text, response_format="mp3", timeout=30)
  if instructions:
    kwargs["instructions"] = instructions
  if speed:
    kwargs["speed"] = speed
  return _call_with_fallback(client.audio.speech.create, kwargs).content


STT_PROMPT = os.getenv(
  "STT_PROMPT",
  "Formula 1 team radio. Driver talking to race engineer about box, pit window, "
  "undercut, overcut, DRS, ERS, overtake mode, softs, mediums, hards, inters, wets, "
  "deg, blisters, fuel, gap, lap times, safety car, VSC.",
)


def transcribe(audio: bytes, filename: str, content_type: str) -> str:
  """Speech-to-text on the STT deployment."""
  if client is None:
    raise RuntimeError("Foundry is not configured")
  kwargs = dict(
    model=STT_DEPLOYMENT,
    file=(filename, audio, content_type or "application/octet-stream"),
    language=RADIO_LANGUAGE,
    prompt=STT_PROMPT,
    response_format="json",
    timeout=30,
  )
  result = _call_with_fallback(client.audio.transcriptions.create, kwargs)
  return (getattr(result, "text", "") or "").strip()
