"""
Telemetry → DeepSeek analysis → TTS → broadcast to frontend
Supports: UDP (PS5) + WebSocket (simulator fallback)
"""
import asyncio, json, time, os, webbrowser, threading, urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from analyzer import analyze, radio_reply
from tts_bridge import synthesize
from f1_parser import F1Accumulator

load_dotenv()

UDP_HOST = os.getenv("UDP_HOST", "0.0.0.0")
GT7_PORT = int(os.getenv("GT7_PORT", "33740"))
F1_PORT = int(os.getenv("F1_PORT", "20777"))
UDP_TIMEOUT = 5.0
UDP_DEBUG = os.getenv("UDP_DEBUG", "0") == "1"
UDP_DUMP = os.getenv("UDP_DUMP", "0") == "1"
ANALYSIS_INTERVAL = float(os.getenv("ANALYSIS_INTERVAL", "15"))

pit_radios = []
_last_analysis_time = 0.0
_last_advice = ""
_last_udp_time = 0.0
_fallback = False
_last_cars = []
_last_player_idx = -1
_last_lap = 0
_last_track = {"id": -1, "name": ""}

BASE_DIR = Path(__file__).parent
dump_dir = BASE_DIR / "udp_dump"
_dump_count = 0

f1_acc = F1Accumulator()


class Gt7Protocol(asyncio.DatagramProtocol):
  def datagram_received(self, data, addr):
    global _dump_count
    try:
      text = data.decode("utf-8")
      if UDP_DEBUG:
        print(f"[GT7 RAW] {text[:200]}")
      if UDP_DUMP:
        _dump_count += 1
        fname = dump_dir / f"gt7_{_dump_count:06d}.json"
        fname.write_text(text, encoding="utf-8")
      frame = json.loads(text)
      asyncio.create_task(process_frame(frame, "udp"))
    except Exception as e:
      print(f"GT7 UDP parse error: {e}")


class F1Protocol(asyncio.DatagramProtocol):
  def __init__(self):
    self._pkt_counts = {}
    self._last_summary = 0.0
    self._frames = 0

  def datagram_received(self, data, addr):
    global _dump_count
    try:
      packet_id = data[6] if len(data) > 6 else -1
      self._pkt_counts[packet_id] = self._pkt_counts.get(packet_id, 0) + 1
      if UDP_DEBUG:
        print(f"[F1 RAW] from={addr[0]} len={len(data)} packetId={packet_id}")
      if UDP_DUMP:
        dump_dir.mkdir(exist_ok=True)
        _dump_count += 1
        fname = dump_dir / f"f1_{_dump_count:06d}.bin"
        fname.write_bytes(data)
      result = f1_acc.feed(data)
      if result and result.get("cars"):
        self._frames += 1
        asyncio.create_task(process_frame(result, "udp"))
      now = time.time()
      if now - self._last_summary >= 5.0:
        self._last_summary = now
        counts = ", ".join(f"id{k}:{v}" for k, v in sorted(self._pkt_counts.items()))
        print(f"[F1 UDP] from={addr[0]} 5s packets=[{counts}] ui_frames={self._frames}")
        self._pkt_counts.clear()
        self._frames = 0
    except Exception as e:
      print(f"F1 UDP parse error: {e}")


@asynccontextmanager
async def lifespan(_app):
  loop = asyncio.get_running_loop()
  print("=== F1 UDP setup ===")
  print(f"Listening F1 telemetry on UDP {UDP_HOST}:{F1_PORT}")
  print("On PS5 F1 24 Settings → Telemetry:")
  print("  UDP Telemetry = On")
  print("  UDP Broadcast / IP = this PC's LAN IP (same Wi‑Fi/LAN as PS5)")
  print(f"  UDP Port = {F1_PORT}")
  print("  UDP Send Rate = 20Hz or higher")
  print("  Then enter a session ON TRACK (menu only sends Event packets).")
  print("====================")
  for name, port, proto in [
    ("GT7", GT7_PORT, Gt7Protocol),
    ("F1", F1_PORT, F1Protocol),
  ]:
    try:
      await loop.create_datagram_endpoint(proto, local_addr=(UDP_HOST, port))
      print(f"UDP listening ({name}) on {UDP_HOST}:{port}")
    except Exception as e:
      print(f"UDP {name} bind failed (port {port}): {e}")
  yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.mount("/audio", StaticFiles(directory=BASE_DIR / "static/audio"), name="audio")


async def _broadcast(payload: dict):
  dead = []
  for client in pit_radios.copy():
    try:
      await client.send_json(payload)
    except Exception:
      dead.append(client)
  for client in dead:
    try:
      pit_radios.remove(client)
    except ValueError:
      pass


async def _run_analysis(cars: list, player_idx: int, lap: int, source: str, track: dict = None):
  global _last_advice
  try:
    result = await asyncio.to_thread(analyze, cars, player_idx, lap, track)
    advice = result.get("advice", "")
    voice = result.get("voice", "")
    _last_advice = advice
  except Exception as e:
    print(f"Analysis error: {e}")
    return

  audio_url = await synthesize(voice) if voice else None
  print(f"[{source.upper()}] Analysis ({len(cars)} cars, L{lap})")
  if advice:
    print(advice[:200])

  await _broadcast({
    "advice": advice,
    "voice": voice,
    "audio_url": audio_url,
  })


async def process_frame(data: dict, source: str):
  global _last_analysis_time, _last_advice, _last_udp_time, _fallback
  global _last_cars, _last_player_idx, _last_lap, _last_track

  cars = data.get("cars", [])
  player_idx = data.get("player_idx", -1)
  lap = data.get("lap", 0)
  track = data.get("track", {"id": -1, "name": ""})

  if source == "udp":
    _last_udp_time = time.time()
    if _fallback:
      _fallback = False
      print("Switched to UDP (live data)")
  else:
    if time.time() - _last_udp_time < UDP_TIMEOUT:
      return

  _last_cars = cars
  _last_player_idx = player_idx
  _last_lap = lap
  _last_track = track

  await _broadcast({
    "advice": _last_advice,
    "voice": "",
    "audio_url": None,
    "cars": cars,
    "track": track,
    "player_idx": player_idx,
    "lap": lap,
  })

  now = time.time()
  if now - _last_analysis_time >= ANALYSIS_INTERVAL:
    _last_analysis_time = now
    asyncio.create_task(_run_analysis(cars, player_idx, lap, source, track))


@app.get("/favicon.ico")
async def favicon():
  return Response(status_code=204)


@app.get("/")
async def get_index():
  return HTMLResponse((BASE_DIR / "static/index.html").read_text(encoding="utf-8"))


class RadioRequest(BaseModel):
  text: str


@app.post("/api/radio")
async def api_radio(req: RadioRequest):
  """Driver speaks to the race engineer; reply is spoken back via TTS."""
  text = (req.text or "").strip()
  if not text:
    return {"ok": False, "error": "empty message"}

  result = await asyncio.to_thread(
    radio_reply, text, _last_cars, _last_player_idx, _last_lap, _last_track
  )
  reply = result.get("reply", "")
  voice = result.get("voice", "") or reply
  audio_url = await synthesize(voice) if voice else None

  print(f"[RADIO] Driver: {text}")
  print(f"[RADIO] Engineer: {voice}")

  await _broadcast({
    "type": "radio",
    "driver": text,
    "advice": reply,
    "voice": voice,
    "audio_url": audio_url,
  })

  return {
    "ok": True,
    "driver": text,
    "reply": reply,
    "voice": voice,
    "audio_url": audio_url,
  }


@app.websocket("/ws/telemetry")
async def telemetry_ws(websocket: WebSocket):
  global _fallback
  await websocket.accept()
  print("Simulator connected (fallback mode)")
  try:
    async for raw in websocket.iter_text():
      frame = json.loads(raw)
      await process_frame(frame, "simulator")
  except Exception as e:
    print(f"Simulator WS error: {e}")


@app.websocket("/ws/pitradio")
async def pitradio_ws(websocket: WebSocket):
  global pit_radios
  await websocket.accept()
  pit_radios.append(websocket)
  print(f"Frontend connected ({len(pit_radios)} online)")
  try:
    await websocket.receive_text()
  except Exception:
    pass
  finally:
    if websocket in pit_radios:
      pit_radios.remove(websocket)


def _open_browser():
  try:
    urllib.request.urlopen("http://127.0.0.1:8000", timeout=1)
    webbrowser.open("http://127.0.0.1:8000")
  except Exception:
    threading.Timer(2, _open_browser).start()


if __name__ == "__main__":
  threading.Timer(1, _open_browser).start()
  import uvicorn
  uvicorn.run("server:app", host="127.0.0.1", port=8000)
