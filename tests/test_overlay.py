"""Radio overlay: server routing (who plays audio, what overlays receive) and hotkey parsing."""
import importlib
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client(monkeypatch):
  monkeypatch.setenv("FOUNDRY_ENDPOINT", "")
  import server
  server = importlib.reload(server)

  async def no_tts(text):
    return "/audio/test.mp3"
  monkeypatch.setattr(server, "synthesize", no_tts)
  return TestClient(server.app), server


def recv_type(ws, wanted, limit=10):
  for _ in range(limit):
    msg = ws.receive_json()
    if msg.get("type") == wanted:
      return msg
  raise AssertionError(f"no {wanted} message")


FRAME = {
  "cars": [{"idx": 0, "name": "Max VERSTAPPEN", "teamId": 2, "raceNumber": 1, "position": 1},
           {"idx": 1, "name": "Lando NORRIS", "teamId": 8, "raceNumber": 4, "position": 2}],
  "player_idx": 0, "lap": 3, "track": {"name": "Zandvoort"}, "game": "Simulator",
}


def test_overlay_page_is_served(client):
  c, _ = client
  r = c.get("/overlay")
  assert r.status_code == 200 and "window.boxbox" in r.text


def test_dashboard_goes_quiet_while_an_overlay_plays_audio(client):
  c, _ = client
  with c.websocket_connect("/ws/pitradio") as dash:
    assert dash.receive_json() == {"type": "audio_route", "overlay": False}
    with c.websocket_connect("/ws/pitradio?role=overlay&audio=1") as ov:
      assert ov.receive_json()["type"] == "audio_route"
      assert recv_type(dash, "audio_route") == {"type": "audio_route", "overlay": True}
    assert recv_type(dash, "audio_route") == {"type": "audio_route", "overlay": False}


def test_text_only_overlay_leaves_audio_to_the_dashboard(client):
  c, _ = client
  with c.websocket_connect("/ws/pitradio") as dash:
    dash.receive_json()
    with c.websocket_connect("/ws/pitradio?role=overlay&audio=0") as ov:
      ov.receive_json()
      assert recv_type(dash, "audio_route")["overlay"] is False


def test_overlay_gets_player_and_radio_but_not_telemetry(client):
  c, srv = client
  srv._last_ui_push = 0
  srv._last_analysis_time = 0
  srv.analyze = lambda *a, **k: {"advice": "L3 | STAY OUT", "voice": "Stay out."}
  with c.websocket_connect("/ws/pitradio?role=overlay") as ov, \
       c.websocket_connect("/ws/pitradio") as dash, \
       c.websocket_connect("/ws/telemetry") as sim:
    ov.receive_json(); recv_type(ov, "audio_route")
    sim.send_text(json.dumps(FRAME))
    player = recv_type(ov, "player")
    assert player == {"type": "player", "name": "Max VERSTAPPEN", "teamId": 2, "raceNumber": 1}
    # the dashboard does get the telemetry frame
    for _ in range(6):
      m = dash.receive_json()
      if m.get("cars"):
        break
    assert m["cars"][0]["teamId"] == 2
    r = c.post("/api/overlay/test").json()
    seen = {}
    for _ in range(6):
      msg = ov.receive_json()
      assert "cars" not in msg, "telemetry frames must not go to overlays"
      seen[msg["type"]] = msg
      if {"radio_test", "strategy"} <= seen.keys():
        break
    assert seen["radio_test"]["voice"] == r["voice"]
    assert seen["strategy"]["voice"] == "Stay out."  # strategy calls are typed for the overlay


def test_new_overlay_gets_current_player(client):
  c, srv = client
  srv._player_info = {"type": "player", "name": "Aoniq", "teamId": 8, "raceNumber": 27}
  with c.websocket_connect("/ws/pitradio?role=overlay") as ov:
    ov.receive_json()  # audio_route
    assert ov.receive_json()["name"] == "Aoniq"


@pytest.mark.parametrize("spec,expected", [
  ("F9", (0, 0x78)),
  ("ctrl+shift+F9", (0x0002 | 0x0004, 0x78)),
  ("numpad0", (0, 0x60)),
  ("Alt+T", (0x0001, ord("T"))),
  ("pause", (0, 0x13)),
])
def test_hotkey_parsing(spec, expected):
  import overlay
  assert overlay.parse_hotkey(spec) == expected


@pytest.mark.parametrize("spec", ["ctrl", "F99", "hyper+K"])
def test_hotkey_parsing_rejects_nonsense(spec):
  import overlay
  with pytest.raises(ValueError):
    overlay.parse_hotkey(spec)


def test_overlay_url_from_env(monkeypatch):
  import overlay
  monkeypatch.setenv("OVERLAY_POSITION", "top-right")
  monkeypatch.setenv("OVERLAY_SCALE", "1.3")
  monkeypatch.setenv("OVERLAY_DRIVER_NAME", "Aoniq")
  monkeypatch.setenv("OVERLAY_ACCENT", "#ff8000")
  monkeypatch.setenv("OVERLAY_AUDIO", "0")
  url = overlay.overlay_url()
  assert url.startswith(overlay.BASE_URL + "/overlay?")
  for part in ("pos=top-right", "scale=1.3", "name=Aoniq", "accent=ff8000", "audio=0"):
    assert part in url
