"""
Race telemetry simulator — full 22-car simulation (positions, compounds, DRS, ERS, lap trends).
"""
import asyncio
import json
import math
import random
import websockets

SERVER_URL = "ws://127.0.0.1:8000/ws/telemetry"
RECONNECT_DELAY = 3

CAR_DEFAULTS = {
  "fuel": 16.2,
  "engineTemp": 110,
  "tireTemps": {"FL": 85, "FR": 85, "RL": 85, "RR": 85},
  "damage": 0,
}

UPSHIFT_SPEED = [0, 0, 32, 134, 167, 195, 236, 270, 304]
GEAR_RPM = [
  (3500, 13500), (4000, 13100), (7000, 12800), (8500, 12600),
  (9000, 12600), (9500, 11600), (9500, 11900), (9800, 11900), (9700, 12500),
]

SPEED_PROFILE = [
  275, 285, 290, 292, 290, 285, 270, 250, 225, 195,
  165, 135, 120, 118, 120, 125, 135, 150, 170, 195,
  220, 245, 270, 290, 308, 318, 324, 325, 323, 318,
  312, 306, 298, 290, 281, 275, 270, 268, 270, 275,
]

COMPOUNDS = ["C0", "C1", "C2", "C3", "C4", "C5"]
COMPOUND_WEIGHTS = [0.05, 0.10, 0.25, 0.30, 0.20, 0.10]  # mid compounds more likely

DRIVERS = [
  (1, "VER"), (11, "PER"), (16, "LEC"), (55, "SAI"),
  (44, "HAM"), (63, "RUS"), (4, "NOR"), (81, "PIA"),
  (14, "ALO"), (18, "STR"), (10, "GAS"), (31, "OCO"),
  (77, "BOT"), (24, "ZHO"), (22, "TSU"), (3, "RIC"),
  (20, "MAG"), (27, "HUL"), (2, "SAR"), (23, "ALB"),
  (5, "BEA"), (97, "VES"),
]

TRACK_LENGTH = 2830  # meters


def gear_from_speed(speed):
  if speed < 2:
    return 0
  for g in range(8, 0, -1):
    if speed >= UPSHIFT_SPEED[g]:
      return g
  return 1


def rpm_from_speed(speed, gear):
  if gear == 0:
    return 3500 + random.uniform(-100, 100)
  low = UPSHIFT_SPEED[gear]
  high = UPSHIFT_SPEED[gear + 1] if gear < 8 else UPSHIFT_SPEED[8] + 40
  rl, rh = GEAR_RPM[gear]
  if high <= low:
    return (rl + rh) / 2
  t = (speed - low) / (high - low)
  return rl + t * (rh - rl)


def get_speed(tick):
  p = tick % 40
  return max(0, SPEED_PROFILE[p] + random.uniform(-2, 3))


def throttle_brake(speed, next_speed):
  delta = next_speed - speed
  if delta > 0.5:
    return round(min(100, 50 + delta * 3 + random.uniform(-3, 0)), 1), 0
  elif delta < -0.5:
    return round(max(0, min(30, (speed * 0.1) - (delta * -0.5))), 1), round(max(0, min(100, -delta * 1.8 + random.uniform(-2, 2))), 1)
  else:
    return round(max(30, min(100, speed * 0.25)), 1), 0


def random_compound():
  r = random.random()
  cum = 0
  for i, w in enumerate(COMPOUND_WEIGHTS):
    cum += w
    if r < cum:
      return COMPOUNDS[i]
  return "C3"


def compound_index(c):
  return COMPOUNDS.index(c) if c in COMPOUNDS else 3


def trend_from_history(history):
  if len(history) < 3:
    return "→"
  recent = history[-3:]
  if recent[2] > recent[0] + 1:
    return "↑"
  if recent[2] < recent[0] - 1:
    return "↓"
  return "→"


_car_states = None


def build_all_cars(tick):
  global _car_states

  if tick == 0:
    _car_states = []
    for i in range(22):
      compound = random_compound()
      base = dict(CAR_DEFAULTS)
      base.update({
        "compound": compound,
        "ersBattery": 100.0,
        "stintLaps": 0,
        "cumulDist": compound_index(compound) * 50 + i * 5,
        "speedHistory": [],
        "lapCount": 0,
        "lapTime": 0,
        "bestLapTime": 0,
        "drs": False,
        "tireWear": {"FL": 0.5, "FR": 0.4, "RL": 0.0, "RR": 0.0},
      })
      _car_states.append(base)

  dt_s = 2  # 2 seconds per tick

  cars = []
  for i in range(22):
    state = _car_states[i]
    offset = i / 22
    car_tick = (tick + int(offset * 40)) % 40

    spd = max(0, get_speed(car_tick))
    gear = gear_from_speed(spd)
    rpm = round(rpm_from_speed(spd, gear))
    next_spd = get_speed(car_tick + 1)
    t, b = throttle_brake(spd, next_spd)

    # cumulative distance
    dist_delta = spd / 3.6 * dt_s
    state["cumulDist"] += dist_delta

    # lap detection
    prev_laps = int((state["cumulDist"] - dist_delta) / TRACK_LENGTH)
    cur_laps = int(state["cumulDist"] / TRACK_LENGTH)
    if cur_laps > prev_laps:
      state["lapCount"] = cur_laps
      state["stintLaps"] += 1
      # lap time from historical average speed
      avg_spd = sum(state["speedHistory"][-20:]) / max(len(state["speedHistory"][-20:]), 1)
      lt = round(TRACK_LENGTH / (avg_spd / 3.6), 1) if avg_spd > 0 else 0
      state["lapTime"] = lt
      if state["bestLapTime"] == 0 or lt < state["bestLapTime"]:
        state["bestLapTime"] = lt

    # speed history (for trend)
    history = state["speedHistory"]
    history.append(spd)
    if len(history) > 10:
      history.pop(0)

    trend = trend_from_history(history)

    # fuel
    state["fuel"] = round(max(0, state["fuel"] - t / 100 * 0.06 - 0.015), 2)

    # tire temps
    load = t / 100 + (1 - spd / 330) * 0.4
    for k in ("FL", "FR", "RL", "RR"):
      curr = state["tireTemps"][k]
      if curr < 105:
        curr += load * random.uniform(0.4, 1.0)
      else:
        curr += random.uniform(-0.4, 0.3)
      state["tireTemps"][k] = round(max(80, min(135, curr)), 1)

    # engine temp
    target_et = 110 + t * 0.1 + spd * 0.015
    state["engineTemp"] = round(state["engineTemp"] + (target_et - state["engineTemp"]) * 0.06 + random.uniform(-0.3, 0.3), 1)

    # tire wear growth
    for k in ("FL", "FR", "RL", "RR"):
      state["tireWear"][k] = round(min(100, state["tireWear"][k] + load * random.uniform(0.1, 0.3)), 1)

    # ERS battery
    if t > 80:
      state["ersBattery"] = round(max(0, state["ersBattery"] - random.uniform(0.3, 0.8)), 1)
    elif b > 20:
      state["ersBattery"] = round(min(100, state["ersBattery"] + random.uniform(0.5, 1.2)), 1)

    # DRS
    state["drs"] = spd > 280 and random.random() < 0.4

    # coordinates
    cx, cz, radius = 0, 0, 450
    angle = (state["cumulDist"] / TRACK_LENGTH) * 2 * math.pi

    cars.append({
      "idx": i,
      "raceNumber": DRIVERS[i][0],
      "name": DRIVERS[i][1],
      "x": round(cx + radius * math.cos(angle), 1),
      "z": round(cz + radius * math.sin(angle), 1),
      "heading": round(math.degrees(angle) + 90, 1),
      "speed": round(spd, 1),
      "rpm": rpm,
      "gear": gear,
      "throttle": t,
      "brake": b,
      "fuel": state["fuel"],
      "engineTemp": state["engineTemp"],
      "tireTemps": dict(state["tireTemps"]),
      "tireWear": dict(state["tireWear"]),
      "damage": state["damage"],
      "compound": state["compound"],
      "drs": state["drs"],
      "ersBattery": round(state["ersBattery"], 1),
      "lapTime": state["lapTime"],
      "bestLapTime": state["bestLapTime"],
      "stintLaps": state["stintLaps"],
      "trend": trend,
      "cumulDist": round(state["cumulDist"], 1),
      "lapDist": round(state["cumulDist"] % TRACK_LENGTH, 1),
    })

  # positions & gaps
  sorted_idx = sorted(range(len(cars)), key=lambda i: cars[i]["cumulDist"], reverse=True)
  leader_dist = cars[sorted_idx[0]]["cumulDist"]
  for pos, idx in enumerate(sorted_idx):
    cars[idx]["position"] = pos + 1
    dist_to_leader = leader_dist - cars[idx]["cumulDist"]
    speed_ms = cars[idx]["speed"] / 3.6
    cars[idx]["gapToLeader"] = round(dist_to_leader / max(speed_ms, 1), 1)
    if pos > 0:
      ahead_idx = sorted_idx[pos - 1]
      dist_ahead = cars[ahead_idx]["cumulDist"] - cars[idx]["cumulDist"]
      cars[idx]["gapToAhead"] = round(dist_ahead / max(speed_ms, 1), 1)
    else:
      cars[idx]["gapToAhead"] = 0
    if pos + 1 < len(sorted_idx):
      next_idx = sorted_idx[pos + 1]
      dist_to_next = cars[idx]["cumulDist"] - cars[next_idx]["cumulDist"]
      behind = round(dist_to_next / max(speed_ms, 1), 1)
      cars[idx]["gapToBehind"] = behind
      cars[idx]["gapToNext"] = behind
    else:
      cars[idx]["gapToBehind"] = 0
      cars[idx]["gapToNext"] = 0
    cars[idx]["tyreAge"] = cars[idx].get("stintLaps", 0)

  return cars


async def connect_and_send():
  lap = 0
  tick = 0

  while True:
    try:
      async with websockets.connect(
        SERVER_URL,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=10
      ) as ws:
        print("Simulator connected to Server", flush=True)
        while True:
          if tick >= 40 and tick % 40 == 0:
            lap += 1

          cars = build_all_cars(tick)
          tick += 1

          payload = {
            "cars": cars,
            "track": {
              "id": 6,
              "name": "Silverstone",
              "weather": "Dry",
              "trackTemp": 32,
              "airTemp": 28,
            },
            "player_idx": 19,
            "lap": lap,
          }

          await ws.send(json.dumps(payload, ensure_ascii=False))

          pc = cars[19]
          print(
            f"L{lap} P{pc.get('position','?')} | {pc['speed']:>3.0f}km/h G{pc['gear']} "
            f"T{pc['throttle']:>3.0f}% B{pc['brake']:>3.0f}% "
            f"T{pc['tireTemps']['FL']:.0f}C "
            f"F{pc['fuel']:.1f}L "
            f"E{pc['engineTemp']:.0f}C "
            f"D{pc['damage']} "
            f"{pc.get('compound','?')} "
            f"ERS{pc.get('ersBattery',0):.0f}% "
            f"DRS={'Y' if pc.get('drs') else 'N'}",
            flush=True,
          )

          await asyncio.sleep(2)

    except (
      websockets.exceptions.ConnectionClosedError,
      websockets.exceptions.ConnectionClosedOK,
      ConnectionRefusedError,
      OSError
    ) as e:
      print(f"Connection lost or failed: {e}", flush=True)
      print(f"Reconnecting in {RECONNECT_DELAY}s...", flush=True)
      await asyncio.sleep(RECONNECT_DELAY)

    except Exception as e:
      print(f"Unexpected error: {e}", flush=True)
      print(f"Reconnecting in {RECONNECT_DELAY}s...", flush=True)
      await asyncio.sleep(RECONNECT_DELAY)


async def dry_run():
  lap = 0
  tick = 0
  print(f"{'tick':>4} {'Lap':>3} {'Pos':>3} {'Speed':>5} {'Gear':>2} {'Comp':>3} {'ERS':>4} {'DRS':>3} {'Fuel':>5} {'WearFL':>5} {'TFL':>4} {'Laptime':>7}", flush=True)
  print("-" * 75, flush=True)
  for _ in range(100):
    if tick >= 40 and tick % 40 == 0:
      lap += 1
    cars = build_all_cars(tick)
    pc = cars[19]
    wear = pc.get("tireWear", {})
    print(
      f"{tick:>4} {lap:>3} P{pc.get('position','?'):>2} {pc['speed']:>5.0f} {pc['gear']:>2} "
      f"{pc.get('compound','?'):>3} {pc.get('ersBattery',0):>4.0f} "
      f"{'Y' if pc.get('drs') else 'N':>3} "
      f"{pc['fuel']:>5.1f} {wear.get('FL',0):>5.1f} "
      f"{pc['tireTemps']['FL']:>4.0f} "
      f"{pc.get('lapTime',0):>6.1f}s",
      flush=True,
    )
    tick += 1


if __name__ == "__main__":
  import sys
  if "--dry" in sys.argv:
    asyncio.run(dry_run())
  else:
    print("Starting race telemetry simulator...", flush=True)
    try:
      asyncio.run(connect_and_send())
    except KeyboardInterrupt:
      print("\nSimulator stopped by user", flush=True)
