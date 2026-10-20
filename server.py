"""
Telemetry → Foundry strategy analysis → Foundry TTS → broadcast to frontend.
Driver pit radio: browser mic → Foundry STT → Foundry chat → Foundry TTS.
Supports: F1 25 UDP (PC/PS5/Xbox), GT7 JSON over UDP, WebSocket simulator fallback.

Front-ends on /ws/pitradio:
  dashboard  (/)        full telemetry + radio + coaching
  overlay    (/overlay) team-radio overlay on top of the game or in OBS; no telemetry frames.
             When an overlay plays the radio audio (?audio=1), dashboards stay silent.
"""
import asyncio, json, time, os, webbrowser, threading, urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from dotenv import load_dotenv
from dataclasses import dataclass, field

from fastapi import FastAPI, File, UploadFile, WebSocket
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from analyzer import analyze, radio_reply, coaching_call
from coach import DrivingCoach
from tts_bridge import synthesize
from foundry import transcribe, CHAT_DEPLOYMENT, TTS_DEPLOYMENT, STT_DEPLOYMENT, client as foundry_client
from f1_parser import F1Accumulator

load_dotenv()

UDP_HOST = os.getenv("UDP_HOST", "0.0.0.0")
GT7_PORT = int(os.getenv("GT7_PORT", "33740"))
F1_PORT = int(os.getenv("F1_PORT", "20777"))
UDP_TIMEOUT = 5.0
UDP_DEBUG = os.getenv("UDP_DEBUG", "0") == "1"
UDP_DUMP = os.getenv("UDP_DUMP", "0") == "1"
ANALYSIS_INTERVAL = float(os.getenv("ANALYSIS_INTERVAL", "15"))
# 127.0.0.1 = this PC only; 0.0.0.0 = also reachable from a phone/tablet/second PC on your network
HTTP_HOST = os.getenv("HOST", "127.0.0.1").strip() or "127.0.0.1"
HTTP_PORT = int(os.getenv("PORT", "8000"))
MAX_RADIO_AUDIO_BYTES = 10 * 1024 * 1024
# Spoken coaching after a lap: auto = driving tips in practice/qualifying/time trial,
# only tyre-saving tips in races; always = every session; off = screen and radio only
COACH_VOICE = os.getenv("COACH_VOICE", "auto").strip().lower()
COACH_RACE_EVERY_LAPS = max(1, int(os.getenv("COACH_RACE_EVERY_LAPS", "3")))
COACH_MIN_LOSS_S = float(os.getenv("COACH_MIN_LOSS_S", "0.08"))

@dataclass
class Frontend:
  role: str = "dashboard"          # dashboard | overlay
  plays_audio: bool = True
  lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # one send at a time per client


pit_radios: dict = {}  # websocket -> Frontend
_player_info: dict = {}
UI_MAX_FPS = float(os.getenv("UI_MAX_FPS", "15"))
_last_ui_push = 0.0
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

coach = DrivingCoach()
f1_acc = F1Accumulator(on_player_sample=coach.on_sample, on_session=coach.on_session)
_last_coach_voice = (None, -99)  # (session uid, lap) of the last spoken race tip


def _on_lap_report(report: dict):
  """Called by the coach (inside the UDP handler) after every completed lap."""
  try:
    asyncio.get_running_loop().create_task(_publish_coaching(report))
  except RuntimeError:
    pass


coach.on_lap_report(_on_lap_report)


def _pick_coaching_line(report: dict) -> tuple[str, str] | None:
  """(finding text, English fallback voice) worth saying after this lap, or None."""
  global _last_coach_voice
  if COACH_VOICE == "off" or report.get("pit"):
    return None
  session = (coach.session or {}).get("session", "")
  racing = session.startswith("Race")
  if COACH_VOICE == "auto" and racing:
    # races: only tyre management, and not every lap
    uid = (coach.session or {}).get("session_uid")
    if _last_coach_voice[0] == uid and report["lap"] - _last_coach_voice[1] < COACH_RACE_EVERY_LAPS:
      return None
    tips = report["tyre"]["tips"]
    if not tips or report["tyre"]["limiting_axle"] not in ("front", "rear"):
      return None
    _last_coach_voice = (uid, report["lap"])
    return tips[0]["text"], tips[0]["text"]
  top = report.get("top")
  if not top:
    return None
  if top["loss_s"] < COACH_MIN_LOSS_S and not top["tyre"] and top["kind"] not in ("off_track",):
    return None
  return top["text"], top["voice"]


async def _publish_coaching(report: dict):
  payload = {"type": "coaching", "report": report, "corners": coach.corners_for_map()}
  line = _pick_coaching_line(report)
  if line:
    voice = await asyncio.to_thread(coaching_call, *line)
    payload["voice"] = voice
    payload["audio_url"] = await synthesize(voice)
    print(f"[COACH] L{report['lap']}: {voice}")
  await _broadcast(payload)


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
        fmt = f1_acc.packet_format or "?"
        print(f"[F1 UDP] from={addr[0]} format={fmt} 5s packets=[{counts}] ui_frames={self._frames}")
        self._pkt_counts.clear()
        self._frames = 0
    except Exception as e:
      print(f"F1 UDP parse error: {e}")


@asynccontextmanager
async def lifespan(_app):
  loop = asyncio.get_running_loop()
  print("=== F1 UDP setup ===")
  print(f"Listening F1 telemetry on UDP {UDP_HOST}:{F1_PORT}")
  print("In F1 25: Settings → Telemetry Settings:")
  print("  UDP Telemetry = On")
  print("  UDP Broadcast Mode = Off, UDP IP Address = this PC's LAN IP (127.0.0.1 if F1 25 runs on this PC)")
  print(f"  UDP Port = {F1_PORT}")
  print("  UDP Send Rate = 20Hz or higher, UDP Format = 2025 (or the 2026 Season Pack format)")
  print("  Then enter a session ON TRACK (menu only sends Event packets).")
  print("====================")
  print(f"Foundry: chat={CHAT_DEPLOYMENT} tts={TTS_DEPLOYMENT} stt={STT_DEPLOYMENT} "
        f"({'ready' if foundry_client else 'NOT CONFIGURED'})")
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


async def _send(ws, fe: "Frontend", payload: dict) -> bool:
  try:
    async with fe.lock:
      await ws.send_json(payload)
    return True
  except Exception as e:
    print(f"{fe.role.capitalize()} disconnected ({type(e).__name__})")
    pit_radios.pop(ws, None)
    return False


async def _broadcast(payload: dict, droppable: bool = False):
  """Send to every front-end. Telemetry frames are `droppable`: skipped for a client that is
  still busy receiving the previous message (and never sent to overlays), so advice,
  coaching and radio always get through."""
  for ws, fe in list(pit_radios.items()):
    if droppable and (fe.role == "overlay" or fe.lock.locked()):
      continue
    await _send(ws, fe, payload)


def _overlay_has_audio() -> bool:
  return any(fe.role == "overlay" and fe.plays_audio for fe in pit_radios.values())


async def _announce_audio_route():
  """Tell dashboards whether an overlay is playing the radio, so audio never doubles up."""
  await _broadcast({"type": "audio_route", "overlay": _overlay_has_audio()})


async def _update_player_info(cars: list, player_idx: int):
  global _player_info
  me = next((c for c in cars if c.get("idx") == player_idx), None)
  if not me:
    return
  info = {"type": "player", "name": me.get("name") or "", "teamId": me.get("teamId"),
          "raceNumber": me.get("raceNumber")}
  if info != _player_info:
    _player_info = info
    for ws, fe in list(pit_radios.items()):
      if fe.role == "overlay":
        await _send(ws, fe, info)


async def _run_analysis(cars: list, player_idx: int, lap: int, source: str, track: dict = None):
  global _last_advice
  try:
    coaching = coach.summary_for_llm() if source == "udp" else ""
    result = await asyncio.to_thread(analyze, cars, player_idx, lap, track, coaching)
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
    "type": "strategy",
    "advice": advice,
    "voice": voice,
    "audio_url": audio_url,
  })


async def process_frame(data: dict, source: str):
  global _last_analysis_time, _last_advice, _last_udp_time, _fallback
  global _last_cars, _last_player_idx, _last_lap, _last_track, _last_ui_push

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
  await _update_player_info(cars, player_idx)

  now = time.time()
  if now - _last_ui_push >= 1.0 / UI_MAX_FPS:
    _last_ui_push = now
    await _broadcast({
      "advice": _last_advice,
      "voice": "",
      "audio_url": None,
      "cars": cars,
      "track": track,
      "player_idx": player_idx,
      "lap": lap,
      "game": data.get("game") or ("Simulator" if source == "simulator" else "Live"),
    }, droppable=True)

  now = time.time()
  if now - _last_analysis_time >= ANALYSIS_INTERVAL:
    _last_analysis_time = now
    asyncio.create_task(_run_analysis(cars, player_idx, lap, source, track))


@app.get("/favicon.ico")
async def favicon():
  return Response(status_code=204)


@app.get("/api/coaching")
async def api_coaching():
  """Latest coaching report and learned corners (dashboard loads this on connect)."""
  return {"report": coach.report, "corners": coach.corners_for_map(),
          "session": coach.session, "learning": not coach.corners}


@app.get("/")
async def get_index():
  return HTMLResponse((BASE_DIR / "static/index.html").read_text(encoding="utf-8"))


@app.get("/overlay")
async def get_overlay():
  """Team-radio overlay: transparent page for the desktop overlay app or an OBS browser source."""
  return HTMLResponse((BASE_DIR / "static/overlay.html").read_text(encoding="utf-8"))


TEST_LINES = [
  "Radio check. Loud and clear, good luck out there.",
  "Box this lap, box this lap. Mediums ready.",
  "Gap behind is one point two. He has DRS, defend into turn one.",
]
_test_idx = 0


@app.post("/api/overlay/test")
async def api_overlay_test():
  """Send a test radio call (with voice, if Foundry is set up) to every overlay and dashboard."""
  global _test_idx
  text = TEST_LINES[_test_idx % len(TEST_LINES)]
  _test_idx += 1
  audio_url = await synthesize(text)
  await _broadcast({"type": "radio_test", "voice": text, "audio_url": audio_url})
  return {"ok": True, "voice": text, "audio_url": audio_url}


class RadioRequest(BaseModel):
  text: str


@app.post("/api/radio")
async def api_radio(req: RadioRequest):
  """Driver types to the race engineer; reply is spoken back via TTS."""
  return await _answer_driver((req.text or "").strip())


@app.post("/api/radio/audio")
async def api_radio_audio(audio: UploadFile = File(...)):
  """Driver's recorded pit-radio call → Foundry STT → engineer reply (text + voice)."""
  data = await audio.read()
  if not data:
    return {"ok": False, "error": "empty recording"}
  if len(data) > MAX_RADIO_AUDIO_BYTES:
    return {"ok": False, "error": "recording too long"}
  try:
    text = await asyncio.to_thread(
      transcribe, data, audio.filename or "radio.webm", audio.content_type or ""
    )
  except Exception as e:
    print(f"[RADIO] Transcription failed: {e}")
    return {"ok": False, "error": "transcription failed"}
  if not text:
    return {"ok": False, "error": "no speech heard"}
  # Show the driver's words straight away (overlay), while the engineer thinks
  await _broadcast({"type": "radio_driver", "driver": text})
  return await _answer_driver(text)


async def _answer_driver(text: str) -> dict:
  if not text:
    return {"ok": False, "error": "empty message"}

  result = await asyncio.to_thread(
    radio_reply, text, _last_cars, _last_player_idx, _last_lap, _last_track,
    coach.radio_context() if coach.corners or coach.samples else None,
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
  role = "overlay" if websocket.query_params.get("role") == "overlay" else "dashboard"
  plays_audio = websocket.query_params.get("audio", "1") != "0"
  await websocket.accept()
  fe = Frontend(role=role, plays_audio=plays_audio)
  pit_radios[websocket] = fe
  print(f"{role.capitalize()} connected ({len(pit_radios)} online"
        f"{', plays radio audio' if role == 'overlay' and plays_audio else ''})")
  await _send(websocket, fe, {"type": "audio_route", "overlay": _overlay_has_audio()})
  if role == "overlay":
    if _player_info:
      await _send(websocket, fe, _player_info)
    await _announce_audio_route()
  try:
    while True:
      await websocket.receive_text()
  except Exception:
    pass
  finally:
    pit_radios.pop(websocket, None)
    if role == "overlay":
      await _announce_audio_route()


def _open_browser():
  try:
    urllib.request.urlopen(f"http://127.0.0.1:{HTTP_PORT}", timeout=1)
    webbrowser.open(f"http://127.0.0.1:{HTTP_PORT}")
  except Exception:
    threading.Timer(2, _open_browser).start()


if __name__ == "__main__":
  if os.getenv("OPEN_BROWSER", "1") != "0":
    threading.Timer(1, _open_browser).start()
  import uvicorn
  uvicorn.run("server:app", host=HTTP_HOST, port=HTTP_PORT)
