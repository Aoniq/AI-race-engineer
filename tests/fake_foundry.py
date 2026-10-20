"""A tiny stand-in for a Microsoft Foundry (Azure OpenAI) endpoint, for tests and offline demos.

Run standalone:  python tests/fake_foundry.py 8765
then point FOUNDRY_ENDPOINT at http://127.0.0.1:8765/ with any FOUNDRY_API_KEY.
"""
import json
import socket
import sys
import threading
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

REQUESTS: list[dict] = []
# Deployments that reject reasoning_effort, like non-reasoning chat models
NO_REASONING = {"gpt-4.1-mini", "old-chat"}

app = FastAPI()


def _record(request: Request, deployment: str, body):
  REQUESTS.append({
    "path": request.url.path,
    "api_version": request.query_params.get("api-version"),
    "api_key": request.headers.get("api-key"),
    "deployment": deployment,
    "body": body,
  })


@app.post("/openai/deployments/{deployment}/chat/completions")
async def chat(deployment: str, request: Request):
  body = await request.json()
  _record(request, deployment, body)
  if "reasoning_effort" in body and deployment in NO_REASONING:
    return JSONResponse(status_code=400, content={"error": {
      "message": "Unrecognized request argument supplied: reasoning_effort",
      "type": "invalid_request_error", "param": "reasoning_effort", "code": None}})
  system = body["messages"][0]["content"]
  if "coaching call" in system:
    content = {"voice": "Corner 3, brake at your marker and carry the apex speed."}
  elif "team radio" in system:
    content = {"voice": "Tyres are fine, 9 laps old, box window opens lap 18."}
  else:
    content = {"advice": "[L12/57] Race | LightRain\nL12 | STAY OUT | fronts 30%", "voice": "Stay out, fronts at 30 percent."}
  return {
    "id": "chatcmpl-fake", "object": "chat.completion", "created": int(time.time()),
    "model": deployment,
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": json.dumps(content)}}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
  }


@app.post("/openai/deployments/{deployment}/audio/speech")
async def speech(deployment: str, request: Request):
  body = await request.json()
  _record(request, deployment, body)
  return Response(content=_speech_wav(body.get("input", "")), media_type="audio/mpeg")


def _speech_wav(text: str) -> bytes:
  """A playable clip roughly as long as the sentence, with a speech-like loudness rhythm."""
  import io, math, struct, wave
  rate, secs = 16000, max(0.8, min(6.0, 0.06 * len(text)))
  buf = io.BytesIO()
  with wave.open(buf, "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
    frames = bytearray()
    for i in range(int(rate * secs)):
      t = i / rate
      env = max(0.0, math.sin(2 * math.pi * 3.1 * t)) * (0.6 + 0.4 * math.sin(2 * math.pi * 0.7 * t))
      v = env * (math.sin(2 * math.pi * 180 * t) + 0.5 * math.sin(2 * math.pi * 360 * t))
      frames += struct.pack("<h", int(max(-1, min(1, v * 0.6)) * 32000))
    w.writeframes(bytes(frames))
  return buf.getvalue()


@app.post("/openai/deployments/{deployment}/audio/transcriptions")
async def transcriptions(deployment: str, request: Request):
  form = await request.form()
  upload = form.get("file")
  audio = await upload.read() if upload is not None else b""
  _record(request, deployment, {
    "model": form.get("model"), "language": form.get("language"),
    "prompt": form.get("prompt"), "filename": getattr(upload, "filename", None),
    "bytes": len(audio),
  })
  return {"text": "How are my tyres?"}


def free_port() -> int:
  with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    return s.getsockname()[1]


def start_in_thread(port: int) -> uvicorn.Server:
  server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
  threading.Thread(target=server.run, daemon=True).start()
  for _ in range(100):
    if server.started:
      break
    time.sleep(0.05)
  return server


if __name__ == "__main__":
  uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
