"""Cross-check f1_parser against the independent f1-packets definitions of F1 25.

Builds real binary F1 25 packets with f1-packets and checks what the parser reads back.
"""
import ctypes
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import f1_parser as P  # noqa: E402

pk = pytest.importorskip("f1.packets")
FMT = 2025
N = 22


def _hdr(pkt, pid, player=3):
  h = pkt.header
  h.packet_format = FMT
  h.game_year = 25
  h.packet_id = pid
  h.session_uid = 42
  h.player_car_index = player


@pytest.mark.parametrize("ours,theirs", [
  ("motion", "CarMotionData"), ("participant", "ParticipantData"),
  ("telemetry", "CarTelemetryData"), ("status", "CarStatusData"),
  ("damage", "CarDamageData"), ("lap", "LapData"),
])
def test_struct_sizes_match_spec(ours, theirs):
  assert ctypes.sizeof(P.layouts(FMT)[ours]) == ctypes.sizeof(getattr(pk, theirs))


@pytest.fixture
def frame():
  acc = P.F1Accumulator()

  s = pk.PacketSessionData(); _hdr(s, 1)
  s.weather = 3; s.track_temperature = 31; s.air_temperature = 22; s.total_laps = 57
  s.session_type = 15; s.track_id = 26; s.safety_car_status = 2
  s.pit_stop_window_ideal_lap = 18; s.pit_stop_window_latest_lap = 24
  s.num_weather_forecast_samples = 1
  s.weather_forecast_samples[0].session_type = 15
  s.weather_forecast_samples[0].rain_percentage = 40
  acc.feed(bytes(s))

  p = pk.PacketParticipantsData(); _hdr(p, 4); p.num_active_cars = N
  for i in range(N):
    p.participants[i].race_number = 10 + i
    p.participants[i].name = f"DRIVER{i}".encode()
  acc.feed(bytes(p))

  lap = pk.PacketLapData(); _hdr(lap, 2)
  for i in range(N):
    d = lap.lap_data[i]
    d.car_position = i + 1; d.current_lap_num = 12; d.last_lap_time_in_ms = 81234 + i
    d.delta_to_car_in_front_ms_part = 800 if i else 0
    d.delta_to_race_leader_ms_part = 800 * i
    d.pit_status = 1 if i == 5 else 0; d.result_status = 2
  acc.feed(bytes(lap))

  t = pk.PacketCarTelemetryData(); _hdr(t, 6)
  for i in range(N):
    c = t.car_telemetry_data[i]
    c.speed = 300 + i; c.gear = 7; c.engine_rpm = 11000; c.drs = 1; c.engine_temperature = 105
    for j, v in enumerate((101, 102, 91, 92)):  # RL RR FL FR
      c.tyres_surface_temperature[j] = v
      c.tyres_inner_temperature[j] = v - 5
  acc.feed(bytes(t))

  st = pk.PacketCarStatusData(); _hdr(st, 7)
  for i in range(N):
    c = st.car_status_data[i]
    c.fuel_in_tank = 30.5; c.fuel_remaining_laps = 1.25
    c.actual_tyre_compound = 22 if i == 3 else 17; c.visual_tyre_compound = 16
    c.tyres_age_laps = 9; c.ers_store_energy = 2_000_000; c.ers_deploy_mode = 3
  acc.feed(bytes(st))

  dm = pk.PacketCarDamageData(); _hdr(dm, 10)
  for i in range(N):
    c = dm.car_damage_data[i]
    for j, v in enumerate((10.0, 20.0, 30.0, 40.0)):  # RL RR FL FR
      c.tyres_wear[j] = v
    c.tyre_blisters[2] = 7
    c.front_left_wing_damage = 12; c.front_right_wing_damage = 30
  return acc.feed(bytes(dm))


def _me(frame):
  return next(c for c in frame["cars"] if c["idx"] == 3)


def test_session(frame):
  tr = frame["track"]
  assert (tr["name"], tr["totalLaps"], tr["weather"], tr["rainPercent"]) == ("Zandvoort", 57, "LightRain", 40)
  assert (tr["session"], tr["safetyCar"], tr["pitWindow"]) == ("Race", "VSC", [18, 24])
  assert frame["game"] == "F1 25"
  assert (frame["player_idx"], frame["lap"]) == (3, 12)


def test_all_cars_and_names(frame):
  assert len(frame["cars"]) == N
  assert (_me(frame)["name"], _me(frame)["raceNumber"]) == ("DRIVER3", 13)


def test_timing(frame):
  me = _me(frame)
  assert (me["position"], me["gapToAhead"], me["gapToLeader"], me["gapToBehind"]) == (4, 0.8, 2.4, 0.8)
  assert me["bestLapTime"] == 81.237
  assert frame["cars"][5]["pitStatus"] == "PITTING"


def test_wheels_are_mapped_rl_rr_fl_fr(frame):
  me = _me(frame)
  assert me["tireTemps"] == {"RL": 101, "RR": 102, "FL": 91, "FR": 92}
  assert me["tireWear"] == {"RL": 10.0, "RR": 20.0, "FL": 30.0, "FR": 40.0}
  assert me["tireBlisters"]["FL"] == 7


def test_status_and_damage(frame):
  me = _me(frame)
  assert (me["compound"], me["visualCompound"], me["stintLaps"]) == ("C6", "SOFT", 9)
  assert (me["fuel"], me["fuelLapsLeft"]) == (30.5, 1.25)
  assert (me["ersBattery"], me["ersMode"]) == (50.0, "OVERTAKE")
  assert me["damage"] == 30
  assert (me["speed"], me["gear"], me["rpm"], me["drs"], me["engineTemp"]) == (303, 7, 11000, True, 105)


def test_2026_season_pack_layout_has_24_cars():
  lay = P.layouts(2026)
  assert lay["num_cars"] == 24
  assert ctypes.sizeof(lay["telemetry"]) == 59  # engine temperature is uint8
  assert ctypes.sizeof(lay["status"]) == 59     # + ERS harvest limit
  assert ctypes.sizeof(lay["participant"]) == 60  # 16-bit driver/network/team ids
