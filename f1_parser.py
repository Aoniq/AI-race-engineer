"""
F1 25 UDP parser (also accepts F1 24 and the F1 25 "2026 Season Pack" UDP mode).

Struct layouts follow EA's "Data Output from F1 25" specification and are
selected per packet by the header's m_packetFormat:

  2024  F1 24 UDP                    22 cars, 48-byte names, no tyre blisters
  2025  F1 25 UDP                    22 cars, 32-byte names, tyre blisters
  2026  F1 25 2026 Season Pack UDP   24 cars, 16-bit team/driver ids, u8 engine temp,
                                     int16 g-forces, ERS harvest limit in car status

All per-wheel arrays in the spec are ordered RL, RR, FL, FR.
"""
from __future__ import annotations

import ctypes
import math
from functools import lru_cache

WHEELS = ("RL", "RR", "FL", "FR")  # spec order for every [4] array
SUPPORTED_FORMATS = (2024, 2025, 2026)
DEFAULT_FORMAT = 2025

# m_actualTyreCompound
COMPOUND_NAMES = {
  16: "C5", 17: "C4", 18: "C3", 19: "C2", 20: "C1", 21: "C0", 22: "C6",
  7: "INTER", 8: "WET",
  9: "DRY", 10: "WET",                           # classic cars
  11: "SOFT", 12: "SOFT", 13: "MEDIUM", 14: "HARD", 15: "WET",  # F2 (11 = super soft)
}
# m_visualTyreCompound (what the driver sees on the sidewall)
VISUAL_COMPOUND_NAMES = {
  16: "SOFT", 17: "MEDIUM", 18: "HARD", 7: "INTER", 8: "WET",
  15: "WET", 19: "SOFT", 20: "SOFT", 21: "MEDIUM", 22: "HARD",
}

TRACK_NAMES = {
  0: "Melbourne", 1: "Paul Ricard", 2: "Shanghai", 3: "Bahrain",
  4: "Catalunya", 5: "Monaco", 6: "Montreal", 7: "Silverstone",
  8: "Hockenheim", 9: "Hungaroring", 10: "Spa", 11: "Monza",
  12: "Singapore", 13: "Suzuka", 14: "Abu Dhabi", 15: "Austin",
  16: "Interlagos", 17: "Red Bull Ring", 18: "Sochi", 19: "Mexico City",
  20: "Baku", 21: "Bahrain Short", 22: "Silverstone Short", 23: "Austin Short",
  24: "Suzuka Short", 25: "Hanoi", 26: "Zandvoort", 27: "Imola",
  28: "Portimao", 29: "Jeddah", 30: "Miami", 31: "Las Vegas", 32: "Losail",
  39: "Silverstone Reverse", 40: "Red Bull Ring Reverse", 41: "Zandvoort Reverse",
}

WEATHER_NAMES = {
  0: "Clear", 1: "LightCloud", 2: "Overcast",
  3: "LightRain", 4: "HeavyRain", 5: "Storm",
}

SESSION_TYPES = {
  0: "Unknown", 1: "P1", 2: "P2", 3: "P3", 4: "Short Practice",
  5: "Q1", 6: "Q2", 7: "Q3", 8: "Short Qualifying", 9: "One-Shot Qualifying",
  10: "Sprint Shootout 1", 11: "Sprint Shootout 2", 12: "Sprint Shootout 3",
  13: "Short Sprint Shootout", 14: "One-Shot Sprint Shootout",
  15: "Race", 16: "Race 2", 17: "Race 3", 18: "Time Trial",
}

ERS_MODES = {0: "NONE", 1: "MEDIUM", 2: "HOTLAP", 3: "OVERTAKE"}
PIT_STATUS = {0: "", 1: "PITTING", 2: "IN PIT AREA"}
ERS_MAX_J = 4_000_000.0

# Packets that change on-track state enough to refresh the dashboard
_UI_PACKET_IDS = {0, 2, 6, 7, 10}

u8, i8, u16, i16, u32, u64, f32 = (
  ctypes.c_uint8, ctypes.c_int8, ctypes.c_uint16, ctypes.c_int16,
  ctypes.c_uint32, ctypes.c_uint64, ctypes.c_float,
)


class _S(ctypes.LittleEndianStructure):
  _pack_ = 1


class PacketHeader(_S):
  _fields_ = [
    ("packet_format", u16), ("game_year", u8), ("game_major_version", u8),
    ("game_minor_version", u8), ("packet_version", u8), ("packet_id", u8),
    ("session_uid", u64), ("session_time", f32), ("frame_identifier", u32),
    ("overall_frame_identifier", u32), ("player_car_index", u8),
    ("secondary_player_car_index", u8),
  ]


HEADER_SIZE = ctypes.sizeof(PacketHeader)  # 29


class MarshalZone(_S):
  _fields_ = [("zone_start", f32), ("zone_flag", i8)]


class WeatherForecastSample(_S):
  _fields_ = [
    ("session_type", u8), ("time_offset", u8), ("weather", u8),
    ("track_temperature", i8), ("track_temperature_change", i8),
    ("air_temperature", i8), ("air_temperature_change", i8),
    ("rain_percentage", u8),
  ]


class SessionPrefix(_S):
  """Leading part of PacketSessionData; identical in 2024/2025/2026."""
  _fields_ = [
    ("weather", u8), ("track_temperature", i8), ("air_temperature", i8),
    ("total_laps", u8), ("track_length", u16), ("session_type", u8),
    ("track_id", i8), ("formula", u8), ("session_time_left", u16),
    ("session_duration", u16), ("pit_speed_limit", u8), ("game_paused", u8),
    ("is_spectating", u8), ("spectator_car_index", u8),
    ("sli_pro_native_support", u8), ("num_marshal_zones", u8),
    ("marshal_zones", MarshalZone * 21), ("safety_car_status", u8),
    ("network_game", u8), ("num_weather_forecast_samples", u8),
    ("weather_forecast_samples", WeatherForecastSample * 64),
    ("forecast_accuracy", u8), ("ai_difficulty", u8),
    ("season_link_identifier", u32), ("weekend_link_identifier", u32),
    ("session_link_identifier", u32), ("pit_stop_window_ideal_lap", u8),
    ("pit_stop_window_latest_lap", u8), ("pit_stop_rejoin_position", u8),
  ]


class LapData(_S):
  _fields_ = [
    ("last_lap_time_in_ms", u32), ("current_lap_time_in_ms", u32),
    ("sector1_time_ms_part", u16), ("sector1_time_minutes_part", u8),
    ("sector2_time_ms_part", u16), ("sector2_time_minutes_part", u8),
    ("delta_to_car_in_front_ms_part", u16), ("delta_to_car_in_front_minutes_part", u8),
    ("delta_to_race_leader_ms_part", u16), ("delta_to_race_leader_minutes_part", u8),
    ("lap_distance", f32), ("total_distance", f32), ("safety_car_delta", f32),
    ("car_position", u8), ("current_lap_num", u8), ("pit_status", u8),
    ("num_pit_stops", u8), ("sector", u8), ("current_lap_invalid", u8),
    ("penalties", u8), ("total_warnings", u8), ("corner_cutting_warnings", u8),
    ("num_unserved_drive_through_pens", u8), ("num_unserved_stop_go_pens", u8),
    ("grid_position", u8), ("driver_status", u8), ("result_status", u8),
    ("pit_lane_timer_active", u8), ("pit_lane_time_in_lane_in_ms", u16),
    ("pit_stop_timer_in_ms", u16), ("pit_stop_should_serve_pen", u8),
    ("speed_trap_fastest_speed", f32), ("speed_trap_fastest_lap", u8),
  ]


class CarDamageData2024(_S):
  _fields_ = [
    ("tyres_wear", f32 * 4), ("tyres_damage", u8 * 4), ("brakes_damage", u8 * 4),
    ("front_left_wing_damage", u8), ("front_right_wing_damage", u8),
    ("rear_wing_damage", u8), ("floor_damage", u8), ("diffuser_damage", u8),
    ("sidepod_damage", u8), ("drs_fault", u8), ("ers_fault", u8),
    ("gear_box_damage", u8), ("engine_damage", u8), ("engine_mguh_wear", u8),
    ("engine_es_wear", u8), ("engine_ce_wear", u8), ("engine_ice_wear", u8),
    ("engine_mguk_wear", u8), ("engine_tc_wear", u8), ("engine_blown", u8),
    ("engine_seized", u8),
  ]


class CarDamageData2025(_S):
  _fields_ = (
    CarDamageData2024._fields_[:3]
    + [("tyre_blisters", u8 * 4)]
    + CarDamageData2024._fields_[3:]
  )


def _motion_fields(gforce_type):
  return [
    ("world_position_x", f32), ("world_position_y", f32), ("world_position_z", f32),
    ("world_velocity_x", f32), ("world_velocity_y", f32), ("world_velocity_z", f32),
    ("world_forward_dir_x", i16), ("world_forward_dir_y", i16), ("world_forward_dir_z", i16),
    ("world_right_dir_x", i16), ("world_right_dir_y", i16), ("world_right_dir_z", i16),
    ("g_force_lateral", gforce_type), ("g_force_longitudinal", gforce_type),
    ("g_force_vertical", gforce_type), ("yaw", f32), ("pitch", f32), ("roll", f32),
  ]


def _participant_fields(fmt):
  id_t = u16 if fmt >= 2026 else u8
  fields = [
    ("ai_controlled", u8), ("driver_id", id_t), ("network_id", id_t),
    ("team_id", id_t), ("my_team", u8), ("race_number", u8), ("nationality", u8),
    ("name", ctypes.c_char * (48 if fmt <= 2024 else 32)),
    ("your_telemetry", u8), ("show_online_names", u8), ("tech_level", u16),
    ("platform", u8),
  ]
  if fmt >= 2025:
    fields += [("num_colours", u8), ("livery_colours", (u8 * 3) * 4)]
  return fields


def _telemetry_fields(fmt):
  return [
    ("speed", u16), ("throttle", f32), ("steer", f32), ("brake", f32),
    ("clutch", u8), ("gear", i8), ("engine_rpm", u16), ("drs", u8),
    ("rev_lights_percent", u8), ("rev_lights_bit_value", u16),
    ("brakes_temperature", u16 * 4), ("tyres_surface_temperature", u8 * 4),
    ("tyres_inner_temperature", u8 * 4),
    ("engine_temperature", u8 if fmt >= 2026 else u16),
    ("tyres_pressure", f32 * 4), ("surface_type", u8 * 4),
  ]


def _status_fields(fmt):
  fields = [
    ("traction_control", u8), ("anti_lock_brakes", u8), ("fuel_mix", u8),
    ("front_brake_bias", u8), ("pit_limiter_status", u8), ("fuel_in_tank", f32),
    ("fuel_capacity", f32), ("fuel_remaining_laps", f32), ("max_rpm", u16),
    ("idle_rpm", u16), ("max_gears", u8), ("drs_allowed", u8),
    ("drs_activation_distance", u16), ("actual_tyre_compound", u8),
    ("visual_tyre_compound", u8), ("tyres_age_laps", u8), ("vehicle_fia_flags", i8),
    ("engine_power_ice", f32), ("engine_power_mguk", f32), ("ers_store_energy", f32),
    ("ers_deploy_mode", u8), ("ers_harvested_this_lap_mguk", f32),
    ("ers_harvested_this_lap_mguh", f32), ("ers_deployed_this_lap", f32),
  ]
  if fmt >= 2026:
    fields.append(("ers_harvest_limit_per_lap", f32))
  fields.append(("network_paused", u8))
  return fields


def _struct(name, fields):
  return type(name, (_S,), {"_fields_": fields})


@lru_cache(maxsize=None)
def layouts(fmt: int) -> dict:
  """Per-car struct classes and car count for a packet format."""
  if fmt not in SUPPORTED_FORMATS:
    fmt = 2026 if fmt > 2026 else DEFAULT_FORMAT
  return {
    "format": fmt,
    "num_cars": 24 if fmt >= 2026 else 22,
    "motion": _struct(f"CarMotion{fmt}", _motion_fields(i16 if fmt >= 2026 else f32)),
    "lap": LapData,
    "participant": _struct(f"Participant{fmt}", _participant_fields(fmt)),
    "telemetry": _struct(f"CarTelemetry{fmt}", _telemetry_fields(fmt)),
    "status": _struct(f"CarStatus{fmt}", _status_fields(fmt)),
    "damage": CarDamageData2024 if fmt <= 2024 else CarDamageData2025,
  }


def _wheels(arr, fn=lambda v: v):
  return {WHEELS[j]: fn(arr[j]) for j in range(4)}


def _ms_min(ms, minutes):
  return round((minutes * 60000 + ms) / 1000.0, 3)


class F1Accumulator:
  """Collects packets into one dashboard/analysis frame for all cars."""

  def __init__(self):
    self._reset()

  def _reset(self):
    self.cars: dict[int, dict] = {}
    self.session_uid = None
    self.packet_format = None
    self.num_cars = 22
    self.num_active = None
    self.player_idx = 0
    self.track_id = -1
    self.lap = 0
    self.total_laps = 0
    self.session_type = 0
    self.weather = 0
    self.track_temp = 0
    self.air_temp = 0
    self.rain_percent = 0
    self.safety_car = 0
    self.pit_window = (0, 0)

  def feed(self, data: bytes, player_idx: int | None = None):
    if len(data) < HEADER_SIZE:
      return None
    header = PacketHeader.from_buffer_copy(data, 0)

    if self.session_uid is not None and header.session_uid != self.session_uid:
      self._reset()  # new session: forget stale cars
    self.session_uid = header.session_uid

    lay = layouts(header.packet_format)
    self.packet_format = lay["format"]
    self.num_cars = lay["num_cars"]
    self.player_idx = header.player_car_index if player_idx is None else player_idx

    handler = {
      0: self._parse_motion, 1: self._parse_session, 2: self._parse_lap_data,
      4: self._parse_participants, 6: self._parse_car_telemetry,
      7: self._parse_car_status, 10: self._parse_car_damage,
    }.get(header.packet_id)
    if handler is None:
      return None  # events, setups, history, lobby, etc.
    handler(data, lay)

    if header.packet_id not in _UI_PACKET_IDS or not self.cars:
      return None
    return self._build_frame()

  # -- helpers ---------------------------------------------------------------

  def _each_car(self, data, cls, base_offset=HEADER_SIZE):
    size = ctypes.sizeof(cls)
    for i in range(self.num_cars):
      off = base_offset + i * size
      if off + size > len(data):
        break
      yield i, cls.from_buffer_copy(data, off)

  # -- packet handlers -------------------------------------------------------

  def _parse_motion(self, data, lay):
    for i, m in self._each_car(data, lay["motion"]):
      car = self.cars.setdefault(i, {})
      car["x"] = round(m.world_position_x, 1)
      car["z"] = round(m.world_position_z, 1)
      fx, fz = m.world_forward_dir_x / 32767.0, m.world_forward_dir_z / 32767.0
      car["heading"] = round((math.degrees(math.atan2(fx, fz)) + 360) % 360, 1)

  def _parse_session(self, data, lay):
    if len(data) < HEADER_SIZE + ctypes.sizeof(SessionPrefix):
      return
    s = SessionPrefix.from_buffer_copy(data, HEADER_SIZE)
    self.weather = s.weather
    self.track_temp = s.track_temperature
    self.air_temp = s.air_temperature
    self.total_laps = s.total_laps
    self.session_type = s.session_type
    self.track_id = s.track_id
    self.safety_car = s.safety_car_status
    self.pit_window = (s.pit_stop_window_ideal_lap, s.pit_stop_window_latest_lap)
    # Rain chance "now": first forecast sample for this session
    rain = 0
    for k in range(min(s.num_weather_forecast_samples, 64)):
      w = s.weather_forecast_samples[k]
      if w.session_type == s.session_type:
        rain = w.rain_percentage
        break
    self.rain_percent = rain

  def _parse_lap_data(self, data, lay):
    for i, ld in self._each_car(data, lay["lap"]):
      car = self.cars.setdefault(i, {})
      car["lapTime"] = round(ld.current_lap_time_in_ms / 1000.0, 3) if ld.current_lap_time_in_ms else 0
      if ld.last_lap_time_in_ms:
        last_s = round(ld.last_lap_time_in_ms / 1000.0, 3)
        car["lastLapTime"] = last_s
        best = car.get("bestLapTime") or 0
        if best <= 0 or last_s < best:
          car["bestLapTime"] = last_s
      car["lapDist"] = round(ld.lap_distance, 1)
      car["cumulDist"] = round(ld.total_distance, 1)
      car["position"] = ld.car_position
      car["currentLap"] = ld.current_lap_num
      car["gapToAhead"] = _ms_min(ld.delta_to_car_in_front_ms_part, ld.delta_to_car_in_front_minutes_part)
      car["gapToLeader"] = _ms_min(ld.delta_to_race_leader_ms_part, ld.delta_to_race_leader_minutes_part)
      car["pitStatus"] = PIT_STATUS.get(ld.pit_status, "")
      car["pitStops"] = ld.num_pit_stops
      car["penalties"] = ld.penalties
      car["resultStatus"] = ld.result_status
      if i == self.player_idx:
        self.lap = ld.current_lap_num

    by_pos = {c.get("position"): idx for idx, c in self.cars.items() if c.get("position")}
    for c in self.cars.values():
      behind = by_pos.get((c.get("position") or 0) + 1)
      c["gapToBehind"] = self.cars[behind].get("gapToAhead", 0) if behind is not None else 0

  def _parse_participants(self, data, lay):
    if len(data) < HEADER_SIZE + 1:
      return
    self.num_active = data[HEADER_SIZE]
    for i, p in self._each_car(data, lay["participant"], HEADER_SIZE + 1):
      car = self.cars.setdefault(i, {})
      car["raceNumber"] = p.race_number
      car["teamId"] = p.team_id
      name = p.name.decode("utf-8", errors="ignore").strip()
      if name:
        car["name"] = name

  def _parse_car_telemetry(self, data, lay):
    for i, t in self._each_car(data, lay["telemetry"]):
      car = self.cars.setdefault(i, {})
      car.update({
        "speed": t.speed,
        "throttle": round(t.throttle * 100, 1),
        "brake": round(t.brake * 100, 1),
        "gear": t.gear,
        "rpm": t.engine_rpm,
        "drs": bool(t.drs),
        "engineTemp": t.engine_temperature,
        "tireTemps": {
          WHEELS[j]: max(t.tyres_surface_temperature[j], t.tyres_inner_temperature[j])
          for j in range(4)
        },
        "tirePressures": _wheels(t.tyres_pressure, lambda v: round(v, 1)),
      })

  def _parse_car_status(self, data, lay):
    for i, s in self._each_car(data, lay["status"]):
      car = self.cars.setdefault(i, {})
      car["fuel"] = round(s.fuel_in_tank, 2)
      car["fuelLapsLeft"] = round(s.fuel_remaining_laps, 2)  # game's own +/- laps estimate
      car["compound"] = COMPOUND_NAMES.get(s.actual_tyre_compound, f"T{s.actual_tyre_compound}")
      car["visualCompound"] = VISUAL_COMPOUND_NAMES.get(s.visual_tyre_compound, "")
      car["stintLaps"] = s.tyres_age_laps
      car["ersBattery"] = round(max(0.0, min(100.0, s.ers_store_energy / ERS_MAX_J * 100.0)), 1)
      car["ersMode"] = ERS_MODES.get(s.ers_deploy_mode, "NONE")
      car["drsAllowed"] = bool(s.drs_allowed)

  def _parse_car_damage(self, data, lay):
    for i, d in self._each_car(data, lay["damage"]):
      car = self.cars.setdefault(i, {})
      car["tireWear"] = _wheels(d.tyres_wear, lambda v: round(v, 1))
      car["tireDamage"] = _wheels(d.tyres_damage)
      if hasattr(d, "tyre_blisters"):
        car["tireBlisters"] = _wheels(d.tyre_blisters)
      car["damage"] = max(d.front_left_wing_damage, d.front_right_wing_damage)
      car["rearWingDamage"] = d.rear_wing_damage
      car["floorDamage"] = d.floor_damage

  # -- output ----------------------------------------------------------------

  def _is_active(self, idx, c):
    if self.num_active is not None and idx >= self.num_active and idx != self.player_idx:
      return False
    # result status 0 = invalid, 1 = inactive; position 0 = empty slot
    if c.get("resultStatus") in (0, 1) and idx != self.player_idx:
      return False
    return bool(c.get("position")) or idx == self.player_idx

  def _build_frame(self):
    zero4 = {w: 0 for w in ("FL", "FR", "RL", "RR")}
    all_cars = []
    for idx in sorted(self.cars):
      c = self.cars[idx]
      if "position" in c and not self._is_active(idx, c):
        continue
      all_cars.append({
        "idx": idx,
        "raceNumber": c.get("raceNumber", idx),
        "name": c.get("name", f"#{idx}"),
        "x": c.get("x", 0), "z": c.get("z", 0), "heading": c.get("heading", 0),
        "speed": c.get("speed", 0), "rpm": c.get("rpm", 0), "gear": c.get("gear", 0),
        "throttle": c.get("throttle", 0), "brake": c.get("brake", 0),
        "fuel": c.get("fuel", 0), "fuelLapsLeft": c.get("fuelLapsLeft"),
        "engineTemp": c.get("engineTemp", 0),
        "tireTemps": c.get("tireTemps", dict(zero4)),
        "tireWear": c.get("tireWear", dict(zero4)),
        "tireBlisters": c.get("tireBlisters", dict(zero4)),
        "damage": c.get("damage", 0),
        "compound": c.get("compound", "C3"),
        "visualCompound": c.get("visualCompound", ""),
        "drs": c.get("drs", False),
        "ersBattery": c.get("ersBattery", 0),
        "ersMode": c.get("ersMode", "NONE"),
        "lapTime": c.get("lapTime", 0),
        "lastLapTime": c.get("lastLapTime", 0),
        "bestLapTime": c.get("bestLapTime", 0),
        "stintLaps": c.get("stintLaps", 0),
        "tyreAge": c.get("stintLaps", 0),
        "position": c.get("position", 0),
        "gapToLeader": c.get("gapToLeader", 0),
        "gapToAhead": c.get("gapToAhead", 0),
        "gapToBehind": c.get("gapToBehind", 0),
        "gapToNext": c.get("gapToBehind", 0),
        "pitStatus": c.get("pitStatus", ""),
        "pitStops": c.get("pitStops", 0),
        "penalties": c.get("penalties", 0),
        "cumulDist": c.get("cumulDist", 0),
        "lapDist": c.get("lapDist", -1),
        "trend": c.get("trend", "→"),
      })

    return {
      "cars": all_cars,
      "track": {
        "id": self.track_id,
        "name": TRACK_NAMES.get(self.track_id, f"Track-{self.track_id}"),
        "weather": WEATHER_NAMES.get(self.weather, "Clear"),
        "rainPercent": self.rain_percent,
        "trackTemp": self.track_temp,
        "airTemp": self.air_temp,
        "totalLaps": self.total_laps,
        "session": SESSION_TYPES.get(self.session_type, "Unknown"),
        "safetyCar": {0: "", 1: "SC", 2: "VSC", 3: "FORMATION"}.get(self.safety_car, ""),
        "pitWindow": list(self.pit_window),
      },
      "player_idx": self.player_idx,
      "lap": self.lap,
      "game": {2024: "F1 24", 2025: "F1 25", 2026: "F1 25 (2026 pack)"}.get(self.packet_format, "F1"),
    }
