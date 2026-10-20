"""Synthetic laps for testing the driving coach.

A 4.2 km track with six corners. Speed follows physics-like braking and acceleration
curves around each apex, so brake points, apex speeds and throttle pick-up behave the
way they do in the game. `mistakes` injects driving errors per corner (1-based):

  {"deep": 3}       brake 3 late, overshoot: lower apex speed, later throttle
  {"early": 2}      brake much earlier than needed
  {"lockup": 1}     front-left locks under braking
  {"spin": 5}       rear wheelspin on exit
  {"under": 4}      front slip angles high mid-corner (understeer)
  {"off": 6}        two wheels on grass at the exit
  {"hot": 4}        front-left surface temperature above window through the corner
"""
import math

TRACK_LEN = 4200.0
# (apex distance m, apex speed km/h)
CORNERS = [(450, 95), (1200, 160), (1850, 75), (2500, 120), (3100, 85), (3800, 180)]
VMAX = 315.0
A_BRAKE = 38.0   # m/s^2 (~3.9 g)
A_ACCEL = 9.0    # m/s^2


def _profile(corners):
  def v_at(d):
    v = VMAX / 3.6
    for apex, va, a_b, a_a in corners:
      va_ms = va / 3.6
      if d <= apex:
        lim = math.sqrt(va_ms ** 2 + 2 * a_b * (apex - d))
      else:
        lim = math.sqrt(va_ms ** 2 + 2 * a_a * (d - apex))
      v = min(v, lim)
    return v * 3.6
  return v_at


def lap_samples(lap, mistakes=None, hz=60, wear_start=None, wear_rate=None, lap_start_t=0.0):
  """Samples (same shape as F1Accumulator emits) for one full lap starting at dist 0."""
  m = mistakes or {}
  corners = []
  for i, (apex, va) in enumerate(CORNERS, start=1):
    a_b, a_a = A_BRAKE, A_ACCEL
    if m.get("deep") == i:
      apex, va, a_b, a_a = apex + 12, va - 9, A_BRAKE * 1.35, A_ACCEL * 0.8
    if m.get("early") == i:
      a_b = A_BRAKE * 0.6
    corners.append((apex, va, a_b, a_a))
  v_at = _profile(corners)
  wear_start = wear_start or {"RL": 0.0, "RR": 0.0, "FL": 0.0, "FR": 0.0}
  wear_rate = wear_rate or {"RL": 2.0, "RR": 2.1, "FL": 1.5, "FR": 1.4}

  out, d, t, dt = [], 0.0, 0.0, 1.0 / hz
  prev_v = v_at(0)
  while d < TRACK_LEN:
    v = v_at(d)
    nxt = v_at(d + v / 3.6 * dt)
    decel = nxt < v - 0.05
    near = min(CORNERS, key=lambda c: abs(c[0] - d))
    ci = CORNERS.index(near) + 1
    rel = d - corners[ci - 1][0]
    in_corner = abs(rel) < 90

    brake = 1.0 if decel and v > 60 else 0.0
    throttle = 0.0 if decel else (1.0 if v >= VMAX - 1 or rel > 25 or not in_corner else 0.6)
    steer = 0.35 if in_corner else 0.0
    slip_ratio = [0.03, 0.03, -0.02, -0.02]
    slip_angle = [0.04, 0.04, 0.05, 0.05] if in_corner else [0.0, 0.0, 0.0, 0.0]
    surface = [0, 0, 0, 0]
    temps = [95, 95, 92, 92]

    if m.get("lockup") == ci and brake and -45 < rel < -15:
      slip_ratio[2] = -0.6
    if m.get("spin") == ci and throttle >= 0.5 and 10 < rel < 60:
      slip_ratio[0] = slip_ratio[1] = 0.35
    if m.get("under") == ci and -20 < rel < 30:
      slip_angle = [0.05, 0.05, 0.17, 0.16]
    if m.get("off") == ci and 50 < rel < 70:
      surface = [7, 0, 7, 0]
    if m.get("hot") == ci and abs(rel) < 60:
      temps[2] = 112

    frac = d / TRACK_LEN
    wear = {w: round(wear_start[w] + wear_rate[w] * frac, 2) for w in wear_start}
    out.append({
      "t": lap_start_t + t, "lap": lap, "dist": d, "lap_ms": int(t * 1000), "last_lap_ms": 0,
      "invalid": False, "pit": False, "speed": round(v), "throttle": throttle, "brake": brake,
      "steer": steer, "gear": 7, "surface": surface, "tyre_surface": temps,
      "x": 1000 * math.cos(2 * math.pi * frac), "z": 600 * math.sin(2 * math.pi * frac),
      "wear": wear, "compound": "C3", "slip_ratio": slip_ratio, "slip_angle": slip_angle,
    })
    d += v / 3.6 * dt
    t += dt
    prev_v = v
  return out, t


def session(laps_mistakes, hz=60):
  """Samples for several consecutive laps; each lap's first sample carries last_lap_ms."""
  all_s, wear, last_ms, t0 = [], {"RL": 0.0, "RR": 0.0, "FL": 0.0, "FR": 0.0}, 0, 0.0
  rate = {"RL": 2.0, "RR": 2.1, "FL": 1.5, "FR": 1.4}
  for n, mistakes in enumerate(laps_mistakes, start=1):
    s, lap_t = lap_samples(n, mistakes, hz, wear, rate, t0)
    for x in s:
      x["last_lap_ms"] = last_ms
    all_s += s
    last_ms = int(lap_t * 1000)
    wear = {w: wear[w] + rate[w] for w in wear}
    t0 += lap_t
  # one sample of the next lap closes the last one
  closing = dict(all_s[-1], lap=len(laps_mistakes) + 1, dist=0.5, lap_ms=0, last_lap_ms=last_ms)
  return all_s + [closing]
