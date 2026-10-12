"""
F1 24 UDP parser — all 22 cars (official PacketHeader / payload layout).
"""
import math
import struct

# F1 23/24 header: format, year, maj, min, packetVer, packetId, sessionUID, ...
HEADER_FMT = struct.Struct("<HBBBBBQfIIBB")
HEADER_SIZE = HEADER_FMT.size  # 29

MOTION_CAR_FMT = struct.Struct("<3f3f6h6f")  # 60 bytes
MOTION_CAR_SIZE = MOTION_CAR_FMT.size

CAR_TELE_FMT = struct.Struct("<HfffBbHBBH4H4B4BH4f4B")  # 60 bytes
CAR_TELE_SIZE = CAR_TELE_FMT.size

CAR_STATUS_CAR_SIZE = 55
CAR_STATUS_FUEL_OFFSET = 5
CAR_STATUS_COMPOUND_OFFSET = 25  # m_actualTyreCompound
CAR_STATUS_ERS_OFFSET = 37  # m_ersStoreEnergy (Joules)
ERS_MAX_J = 4_000_000.0

CAR_DMG_CAR_SIZE = 42  # float[4] wear + uint8 fields (packed)
CAR_DMG_WEAR_OFFSET = 0  # 4 × float percentage
CAR_DMG_FRONT_WING_OFFSET = 24  # after wear(16)+tyreDmg(4)+brakeDmg(4)

# F1 24 LapData is 57 bytes (lastLap/currentLap are uint32 ms)
LAP_DATA_CAR_SIZE = 57

_wheel_map = ["FL", "FR", "RL", "RR"]

# F1 24 actual compound ids
COMPOUND_NAMES = {
  7: "INTER", 8: "WET",
  9: "DRY", 10: "WET",
  11: "SS", 12: "S", 13: "M", 14: "H", 15: "WET",
  16: "C5", 17: "C4", 18: "C3", 19: "C2", 20: "C1", 21: "C0",
  0: "C0", 1: "C1", 2: "C2", 3: "C3", 4: "C4", 5: "C5",
  6: "SOFT",
}

TRACK_NAMES = {
  0: "Melbourne", 1: "Paul Ricard", 2: "Shanghai", 3: "Bahrain",
  4: "Catalunya", 5: "Monaco", 6: "Montreal", 7: "Silverstone",
  8: "Hockenheim", 9: "Hungaroring", 10: "Spa", 11: "Monza",
  12: "Singapore", 13: "Suzuka", 14: "Abu Dhabi", 15: "Red Bull Ring",
  16: "Austin", 17: "Interlagos", 18: "Baku", 19: "Miami",
  20: "Imola", 21: "Mexico City", 22: "Jeddah", 23: "Las Vegas",
  24: "Losail", 25: "Zandvoort",
}

# Packet IDs that update on-track car state enough to refresh the UI
_UI_PACKET_IDS = {0, 2, 4, 6, 7, 10}


class F1Accumulator:
  def __init__(self):
    self._reset()

  def _reset(self):
    self.cars = {}
    self.player_idx = 0
    self.track_id = -1
    self.lap = 0
    self.session_type = 0
    self.weather = 0
    self.track_temp = 0
    self.air_temp = 0

  def feed(self, data: bytes, player_idx: int | None = None):
    if len(data) < HEADER_SIZE:
      return None

    header = HEADER_FMT.unpack_from(data, 0)
    packet_format = header[0]
    packet_id = header[5]
    if player_idx is None:
      player_idx = header[10]
    self.player_idx = player_idx

    if packet_format not in (2023, 2024, 2025):
      # Still try — some builds may differ; offsets assume 23+ header
      pass

    if packet_id == 0:
      self._parse_motion(data)
    elif packet_id == 1:
      self._parse_session(data)
    elif packet_id == 2:
      self._parse_lap_data(data)
    elif packet_id == 4:
      self._parse_participants(data)
    elif packet_id == 6:
      self._parse_car_telemetry(data)
    elif packet_id == 7:
      self._parse_car_status(data)
    elif packet_id == 10:
      self._parse_car_damage(data)
    else:
      # Event / lobby / history etc. — no UI frame
      return None

    if packet_id not in _UI_PACKET_IDS:
      return None
    if not self.cars:
      return None

    return self._build_frame()

  def _parse_motion(self, data):
    for i in range(22):
      offset = HEADER_SIZE + i * MOTION_CAR_SIZE
      if offset + MOTION_CAR_SIZE > len(data):
        break
      raw = MOTION_CAR_FMT.unpack_from(data, offset)
      car = self.cars.setdefault(i, {})
      car["x"] = round(raw[0], 1)
      car["z"] = round(raw[2], 1)
      fx, fz = raw[6] / 32767.0, raw[8] / 32767.0
      car["heading"] = round((math.degrees(math.atan2(fx, fz)) + 360) % 360, 1)

  def _parse_session(self, data):
    if len(data) < HEADER_SIZE + 8:
      return
    self.weather = data[HEADER_SIZE]
    self.track_temp = data[HEADER_SIZE + 1]
    self.air_temp = data[HEADER_SIZE + 2]
    # F1 24: weather, trackTemp, airTemp, totalLaps, trackLength(u16), sessionType, trackId
    self.track_id = struct.unpack_from("<b", data, HEADER_SIZE + 7)[0]

  def _parse_lap_data(self, data):
    for i in range(22):
      offset = HEADER_SIZE + i * LAP_DATA_CAR_SIZE
      if offset + LAP_DATA_CAR_SIZE > len(data):
        break
      car = self.cars.setdefault(i, {})
      last_ms, cur_ms = struct.unpack_from("<II", data, offset)
      # deltas: uint16 ms part + uint8 minutes part
      ahead_ms, ahead_min = struct.unpack_from("<HB", data, offset + 14)
      lead_ms, lead_min = struct.unpack_from("<HB", data, offset + 17)
      lap_dist = struct.unpack_from("<f", data, offset + 20)[0]
      total_dist = struct.unpack_from("<f", data, offset + 24)[0]
      position = data[offset + 32]
      current_lap = data[offset + 33]
      car["lapTime"] = round(cur_ms / 1000.0, 3) if cur_ms else 0
      car["lapDist"] = round(lap_dist, 1)
      if last_ms:
        last_s = round(last_ms / 1000.0, 3)
        prev_best = car.get("bestLapTime") or 0
        if prev_best <= 0 or last_s < prev_best:
          car["bestLapTime"] = last_s
      car["position"] = position
      car["cumulDist"] = round(total_dist, 1)
      car["gapToAhead"] = round((ahead_min * 60000 + ahead_ms) / 1000.0, 3)
      car["gapToLeader"] = round((lead_min * 60000 + lead_ms) / 1000.0, 3)
      if i == self.player_idx:
        self.lap = current_lap

    # gap to car behind = ahead gap of the car one place behind
    by_pos = {}
    for idx, c in self.cars.items():
      pos = c.get("position") or 0
      if pos:
        by_pos[pos] = idx
    for idx, c in self.cars.items():
      pos = c.get("position") or 0
      behind_idx = by_pos.get(pos + 1)
      if behind_idx is not None:
        c["gapToBehind"] = self.cars[behind_idx].get("gapToAhead", 0)
      else:
        c["gapToBehind"] = 0
      # keep old key for UI compatibility
      c["gapToNext"] = c.get("gapToBehind", 0)

  def _parse_participants(self, data):
    if len(data) < HEADER_SIZE + 1:
      return
    for i in range(22):
      offset = HEADER_SIZE + 1 + i * 60
      if offset + 60 > len(data):
        break
      race_number = data[offset + 5]
      raw_name = data[offset + 7: offset + 7 + 48]
      name = raw_name.decode("utf-8", errors="ignore").strip("\x00").strip()
      car = self.cars.setdefault(i, {})
      car["raceNumber"] = race_number
      if name:
        car["name"] = name

  def _parse_car_telemetry(self, data):
    for i in range(22):
      offset = HEADER_SIZE + i * CAR_TELE_SIZE
      if offset + CAR_TELE_SIZE > len(data):
        break
      raw = CAR_TELE_FMT.unpack_from(data, offset)
      car = self.cars.setdefault(i, {})
      surface_temps = raw[14:18]
      inner_temps = raw[18:22]
      car.update({
        "speed": raw[0],
        "throttle": round(raw[1] * 100, 1),
        "brake": round(raw[3] * 100, 1),
        "gear": raw[5],
        "rpm": raw[6],
        "drs": bool(raw[7]),
        "engineTemp": raw[22],
        "tireTemps": {
          _wheel_map[j]: max(surface_temps[j], inner_temps[j]) for j in range(4)
        },
      })

  def _parse_car_status(self, data):
    for i in range(22):
      offset = HEADER_SIZE + i * CAR_STATUS_CAR_SIZE
      if offset + CAR_STATUS_CAR_SIZE > len(data):
        continue
      car = self.cars.setdefault(i, {})
      fuel = struct.unpack_from("<f", data, offset + CAR_STATUS_FUEL_OFFSET)[0]
      car["fuel"] = round(fuel, 2)
      compound_id = data[offset + CAR_STATUS_COMPOUND_OFFSET]
      car["compound"] = COMPOUND_NAMES.get(compound_id, f"T{compound_id}")
      car["stintLaps"] = data[offset + 27]  # m_tyresAgeLaps
      ers_j = struct.unpack_from("<f", data, offset + CAR_STATUS_ERS_OFFSET)[0]
      car["ersBattery"] = round(max(0.0, min(100.0, (ers_j / ERS_MAX_J) * 100.0)), 1)

  def _parse_car_damage(self, data):
    for i in range(22):
      offset = HEADER_SIZE + i * CAR_DMG_CAR_SIZE
      if offset + CAR_DMG_CAR_SIZE > len(data):
        # F1 24 size is 42; accept slightly shorter if trailing fields missing
        if offset + 26 > len(data):
          continue
      car = self.cars.setdefault(i, {})
      if offset + 16 <= len(data):
        wears = struct.unpack_from("<4f", data, offset + CAR_DMG_WEAR_OFFSET)
        car["tireWear"] = {
          _wheel_map[j]: round(wears[j], 1) for j in range(4)
        }
      if offset + CAR_DMG_FRONT_WING_OFFSET + 2 <= len(data):
        fl = data[offset + CAR_DMG_FRONT_WING_OFFSET]
        fr = data[offset + CAR_DMG_FRONT_WING_OFFSET + 1]
        car["damage"] = max(fl, fr)

  def _build_frame(self):
    all_cars = []
    for idx in sorted(self.cars.keys()):
      c = self.cars[idx]
      all_cars.append({
        "idx": idx,
        "raceNumber": c.get("raceNumber", idx),
        "name": c.get("name", f"#{idx}"),
        "x": c.get("x", 0),
        "z": c.get("z", 0),
        "heading": c.get("heading", 0),
        "speed": c.get("speed", 0),
        "rpm": c.get("rpm", 0),
        "gear": c.get("gear", 0),
        "throttle": c.get("throttle", 0),
        "brake": c.get("brake", 0),
        "fuel": c.get("fuel", 0),
        "engineTemp": c.get("engineTemp", 0),
        "tireTemps": c.get("tireTemps", {"FL": 0, "FR": 0, "RL": 0, "RR": 0}),
        "tireWear": c.get("tireWear", {"FL": 0, "FR": 0, "RL": 0, "RR": 0}),
        "damage": c.get("damage", 0),
        "compound": c.get("compound", "C3"),
        "drs": c.get("drs", False),
        "ersBattery": c.get("ersBattery", 0),
        "lapTime": c.get("lapTime", 0),
        "bestLapTime": c.get("bestLapTime", 0),
        "stintLaps": c.get("stintLaps", 0),
        "tyreAge": c.get("stintLaps", 0),
        "position": c.get("position", 0),
        "gapToLeader": c.get("gapToLeader", 0),
        "gapToAhead": c.get("gapToAhead", 0),
        "gapToBehind": c.get("gapToBehind", c.get("gapToNext", 0)),
        "gapToNext": c.get("gapToNext", c.get("gapToBehind", 0)),
        "cumulDist": c.get("cumulDist", 0),
        "lapDist": c.get("lapDist", -1),
        "trend": c.get("trend", "→"),
      })

    track_name = TRACK_NAMES.get(self.track_id, f"Track-{self.track_id}")
    weather_map = {
      0: "Clear", 1: "LightCloud", 2: "Overcast",
      3: "LightRain", 4: "HeavyRain", 5: "Storm",
    }

    return {
      "cars": all_cars,
      "track": {
        "id": self.track_id,
        "name": track_name,
        "weather": weather_map.get(self.weather, "Clear"),
        "trackTemp": self.track_temp,
        "airTemp": self.air_temp,
      },
      "player_idx": self.player_idx,
      "lap": self.lap,
    }
