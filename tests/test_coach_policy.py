"""When the engineer speaks coaching after a lap (server policy)."""
import importlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)


def _report(lap, top=None, tips=None, limiting="rear", pit=False):
  return {"lap": lap, "pit": pit, "top": top,
          "tyre": {"tips": tips or [], "limiting_axle": limiting}}


DEEP = {"kind": "too_deep", "loss_s": 0.3, "tyre": None, "text": "C3: too deep", "voice": "Corner 3, too deep."}
SMALL = {"kind": "slower", "loss_s": 0.05, "tyre": None, "text": "C2: 0.05s slower", "voice": "Corner 2."}
TIP = [{"axle": "rear", "corners": ["C5"], "text": "Wheelspin out of C5 is eating the rears."}]


def load(monkeypatch, mode="auto"):
  monkeypatch.setenv("COACH_VOICE", mode)
  monkeypatch.setenv("FOUNDRY_ENDPOINT", "")
  import server
  return importlib.reload(server)


def test_practice_speaks_driving_tips(monkeypatch):
  srv = load(monkeypatch)
  srv.coach.session = {"session": "P1", "session_uid": 1}
  assert srv._pick_coaching_line(_report(3, DEEP)) == ("C3: too deep", "Corner 3, too deep.")
  assert srv._pick_coaching_line(_report(4, SMALL)) is None  # too small to bother the driver
  assert srv._pick_coaching_line(_report(5, DEEP, pit=True)) is None


def test_race_speaks_only_tyre_tips_every_few_laps(monkeypatch):
  srv = load(monkeypatch)
  srv.coach.session = {"session": "Race", "session_uid": 7}
  assert srv._pick_coaching_line(_report(4, DEEP)) is None          # no driving tips in a race
  assert srv._pick_coaching_line(_report(5, DEEP, TIP))[0].startswith("Wheelspin")
  assert srv._pick_coaching_line(_report(6, DEEP, TIP)) is None      # not every lap
  assert srv._pick_coaching_line(_report(8, DEEP, TIP)) is not None  # after COACH_RACE_EVERY_LAPS
  srv.coach.session = {"session": "Race", "session_uid": 8}         # new session resets spacing
  assert srv._pick_coaching_line(_report(2, DEEP, TIP)) is not None


def test_off_and_always(monkeypatch):
  srv = load(monkeypatch, "off")
  srv.coach.session = {"session": "P1"}
  assert srv._pick_coaching_line(_report(3, DEEP)) is None
  srv = load(monkeypatch, "always")
  srv.coach.session = {"session": "Race", "session_uid": 1}
  assert srv._pick_coaching_line(_report(3, DEEP)) is not None
