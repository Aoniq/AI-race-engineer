"""Check the Foundry wiring (URLs, auth header, parameters, fallbacks) against a local fake endpoint."""
import importlib
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(__file__))
import fake_foundry  # noqa: E402


@pytest.fixture(scope="module")
def endpoint():
  port = fake_foundry.free_port()
  server = fake_foundry.start_in_thread(port)
  yield f"http://127.0.0.1:{port}/api/projects/my-project"  # a project URL is accepted too
  server.should_exit = True


@pytest.fixture
def foundry(endpoint, monkeypatch):
  monkeypatch.setenv("FOUNDRY_ENDPOINT", endpoint)
  monkeypatch.setenv("FOUNDRY_API_KEY", "test-key")
  monkeypatch.setenv("FOUNDRY_CHAT_DEPLOYMENT", "gpt-5.4-mini")
  monkeypatch.setenv("FOUNDRY_TTS_DEPLOYMENT", "my-tts")
  monkeypatch.setenv("FOUNDRY_STT_DEPLOYMENT", "my-stt")
  monkeypatch.setenv("RADIO_LANGUAGE", "nl")
  fake_foundry.REQUESTS.clear()
  import foundry as f
  f = importlib.reload(f)
  f._unsupported.clear()
  return f


def test_chat_goes_to_the_chat_deployment(foundry):
  out = foundry.chat_json("strategy", "data", max_tokens=100, timeout=5, reasoning_effort="low")
  assert out["voice"].startswith("Stay out")
  req = fake_foundry.REQUESTS[-1]
  assert req["path"] == "/openai/deployments/gpt-5.4-mini/chat/completions"
  assert req["api_version"] == "2025-04-01-preview"
  assert req["api_key"] == "test-key"
  assert req["body"]["reasoning_effort"] == "low"
  assert req["body"]["max_completion_tokens"] == 100
  assert req["body"]["response_format"] == {"type": "json_object"}


def test_reasoning_effort_dropped_for_models_without_it(foundry, monkeypatch):
  monkeypatch.setattr(foundry, "CHAT_DEPLOYMENT", "gpt-4.1-mini")
  out = foundry.chat_json("strategy", "data", max_tokens=100, timeout=5, reasoning_effort="low")
  assert "advice" in out
  first, second = fake_foundry.REQUESTS[-2:]
  assert "reasoning_effort" in first["body"] and "reasoning_effort" not in second["body"]
  # learned once: next call doesn't send it again
  foundry.chat_json("strategy", "data", max_tokens=100, timeout=5, reasoning_effort="low")
  assert "reasoning_effort" not in fake_foundry.REQUESTS[-1]["body"]


def test_tts(foundry):
  audio = foundry.speak("Box box", "onyx", "calm engineer", 1.1)
  assert audio.startswith(b"ID3")
  req = fake_foundry.REQUESTS[-1]
  assert req["path"] == "/openai/deployments/my-tts/audio/speech"
  assert req["body"]["voice"] == "onyx" and req["body"]["input"] == "Box box"
  assert req["body"]["response_format"] == "mp3"


def test_stt(foundry):
  text = foundry.transcribe(b"\x1aE\xdf\xa3" + b"0" * 2000, "radio.webm", "audio/webm")
  assert text == "How are my tyres?"
  req = fake_foundry.REQUESTS[-1]
  assert req["path"] == "/openai/deployments/my-stt/audio/transcriptions"
  assert req["body"]["model"] == "my-stt"
  assert req["body"]["language"] == "nl"
  assert req["body"]["filename"] == "radio.webm" and req["body"]["bytes"] == 2004
  assert "undercut" in req["body"]["prompt"]


def test_radio_language_reaches_prompts(foundry):
  import analyzer
  analyzer = importlib.reload(analyzer)
  assert "Dutch only" in analyzer.RADIO_SYSTEM_PROMPT
  assert "{LANG}" not in analyzer.SYSTEM_PROMPT and "Dutch" in analyzer.SYSTEM_PROMPT


def test_coaching_reaches_strategy_and_radio_prompts(foundry):
  import json as _json
  import analyzer
  analyzer = importlib.reload(analyzer)
  cars = [{"idx": 0, "name": "YOU", "position": 1, "compound": "C3", "tyreAge": 5}]
  analyzer.analyze(cars, 0, 5, {"name": "Zandvoort"}, coaching="DRIVER COACHING (lap 4):\n  - C3: braking too deep")
  strategy = fake_foundry.REQUESTS[-1]["body"]["messages"][1]["content"]
  assert "DRIVER COACHING" in strategy and "C3: braking too deep" in strategy

  out = analyzer.radio_reply("where am I losing time?", cars, 0, 5, {"name": "Zandvoort"},
                             coaching={"issues": ["C3: braking too deep"]})
  payload = _json.loads(fake_foundry.REQUESTS[-1]["body"]["messages"][1]["content"])
  assert payload["coaching"]["issues"] == ["C3: braking too deep"]
  assert out["voice"]

  line = analyzer.coaching_call("C3: braking too deep", "fallback")
  assert line.startswith("Corner 3")
