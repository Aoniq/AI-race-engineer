"""Driving coach on synthetic laps with known, injected mistakes."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools"))
import sim_track as T  # noqa: E402
from coach import DrivingCoach  # noqa: E402


def run(laps, hz=60, session="Practice 1", transform=None):
  c = DrivingCoach()
  c.on_session({"track_length": T.TRACK_LEN, "session": session})
  reports = []
  c.on_lap_report(reports.append)
  samples = T.session(laps, hz)
  if transform:
    samples = transform(samples)
  for s in samples:
    c.on_sample(s)
  return c, reports


def kinds(report, corner=None):
  return {(f["corner"], f["kind"]) for f in report["findings"] if corner is None or f["corner"] == corner}


@pytest.fixture(scope="module")
def session_run():
  return run([{}, {}, {"deep": 3, "lockup": 1, "spin": 5}, {"early": 2, "under": 4, "hot": 4}, {}])


def test_learns_all_corners(session_run):
  c, reports = session_run
  assert len(c.corners) == len(T.CORNERS)
  for corner, (apex, _) in zip(c.corners, T.CORNERS):
    assert abs(corner.apex - apex) < 15
    assert corner.entry < corner.apex < corner.exit
  assert len(reports) == 5


def test_identical_lap_is_clean(session_run):
  _, reports = session_run
  assert reports[1]["findings"] == []
  assert reports[4]["findings"] == []


def test_too_deep_is_found_and_explains_the_lap_time(session_run):
  _, reports = session_run
  r = reports[2]
  deep = [f for f in r["findings"] if f["kind"] == "too_deep"]
  assert [f["corner"] for f in deep] == ["C3"]
  lap_loss = r["lap_time"] - r["best_lap"]
  assert deep[0]["loss_s"] == pytest.approx(lap_loss, abs=0.03)
  assert r["top"]["kind"] == "too_deep"


def test_lockup_and_wheelspin(session_run):
  _, reports = session_run
  r = reports[2]
  lock = next(f for f in r["findings"] if f["kind"] == "lockup")
  assert lock["corner"] == "C1" and lock["tyre"] == "FL"
  spin = next(f for f in r["findings"] if f["kind"] == "wheelspin")
  assert spin["corner"] == "C5" and "rears" in spin["text"]


def test_early_braking_understeer_and_hot_tyre(session_run):
  _, reports = session_run
  r = reports[3]
  assert ("C2", "early_brake") in kinds(r)
  assert ("C4", "understeer") in kinds(r)
  early = next(f for f in r["findings"] if f["kind"] == "early_brake")
  assert early["loss_s"] > 0.1
  tips = " ".join(t["text"] for t in r["tyre"]["tips"])
  assert "Front-left runs above its window in C4" in tips


def test_bad_lap_never_becomes_the_reference(session_run):
  c, _ = session_run
  # C3 best must come from a clean lap, not the too-deep lap
  assert c.best_runs[3].min_speed == pytest.approx(T.CORNERS[2][1], abs=2)


def test_tyre_report_rear_limited_with_reasons(session_run):
  _, reports = session_run
  t = reports[-1]["tyre"]
  assert t["limiting_axle"] == "rear"
  assert t["wear_per_lap"]["RR"] == pytest.approx(2.1, abs=0.05)
  assert t["tips"][0]["axle"] == "rear"


def test_one_off_mistake_is_not_a_habit():
  _, reports = run([{}, {}, {"deep": 3}, {}, {}])
  assert reports[-1]["focus"] == []


def test_repeated_mistake_is_a_habit():
  _, reports = run([{}, {"deep": 3}, {"deep": 3}, {"deep": 3}])
  assert reports[-1]["focus"][0]["corner"] == "C3"


def test_works_at_20hz():
  _, reports = run([{}, {}, {"deep": 3, "lockup": 1}], hz=20)
  assert ("C3", "too_deep") in kinds(reports[2])
  assert ("C1", "lockup") in kinds(reports[2])


def test_flashback_rewind_is_handled():
  def rewind(samples):
    # in lap 3, rewind 400 m after reaching 2600 m, then drive on normally
    out, done = [], False
    for s in samples:
      out.append(s)
      if not done and s["lap"] == 3 and s["dist"] >= 2600:
        done = True
        out += [dict(x) for x in samples if x["lap"] == 3 and 2200 <= x["dist"] < 2600]
    return out
  _, reports = run([{}, {}, {}], transform=rewind)
  assert len(reports) == 3
  assert reports[2]["findings"] == []


def test_pit_laps_are_not_references():
  def pit_lap_two(samples):
    for s in samples:
      if s["lap"] == 2:
        s["pit"] = True
    return samples
  c, reports = run([{}, {"early": 2}], transform=pit_lap_two)
  assert reports[1]["pit"] is True
  assert c.best_runs[2].brake_start is not None
  # the early-braking pit lap didn't overwrite the clean reference
  ref = c.best_runs[2]
  assert ref.time == min(r["corner_table"][1]["time"] for r in reports if not r["pit"])


def test_summary_and_radio_context(session_run):
  c, _ = session_run
  text = c.summary_for_llm()
  assert "DRIVER COACHING" in text and "TYRE MANAGEMENT" in text
  ctx = c.radio_context()
  assert ctx["tyre"]["limiting"] == "rear"
  assert len(c.corners_for_map()) == len(T.CORNERS)
