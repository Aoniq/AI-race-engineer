"""
Driving coach — turns the player's high-rate telemetry into concrete driving advice.

How it works
  1. Every Car Telemetry packet gives one sample of the player's car (pedals, steering,
     speed, slip per wheel from Motion Ex, surface under each wheel, tyre temperatures),
     placed on the lap by lap distance.
  2. From the first complete flying lap the coach learns the corners of the track
     (every braking/lifting zone with a real speed drop) and fixes timing gates around
     each one, so corner times are comparable lap to lap.
  3. Each completed lap is measured corner by corner (brake point, apex speed, throttle
     pick-up, time between the gates, lock-ups, wheelspin, sliding, off-track, coasting)
     and compared with the driver's own best time through that corner.
  4. Findings are ranked by time lost and tyre cost. Tyre wear per wheel over the last
     laps decides which tyre-saving advice matters.

Corners are numbered C1, C2, ... in the order the driver meets them from the start
line. These follow the braking zones, not the official turn numbers.

Everything here is deterministic: the language model only phrases what this finds.
Thresholds live in CoachConfig and can be tuned per driver.
"""
from __future__ import annotations

import bisect
import statistics
from collections import Counter, deque
from dataclasses import dataclass, field

from engine_models import _get_compound

WHEELS = ("RL", "RR", "FL", "FR")  # spec order of every per-wheel array
FRONT = (2, 3)
REAR = (0, 1)
WHEEL_NAMES = {"RL": "rear-left", "RR": "rear-right", "FL": "front-left", "FR": "front-right"}
OFF_TRACK_SURFACES = {3, 4, 5, 6, 7, 8}  # rock, gravel, mud, sand, grass, water


@dataclass
class CoachConfig:
  # pedal thresholds (0..1)
  brake_on: float = 0.10
  throttle_lift: float = 0.90       # below this counts as "off full throttle"
  throttle_pickup: float = 0.50
  throttle_full: float = 0.95
  coast_pedal: float = 0.05
  # corner detection
  min_corner_drop_kmh: float = 15.0
  gate_before_m: float = 40.0
  gate_after_m: float = 40.0
  brake_search_m: float = 150.0
  # tyre events (slip ratio: -1 = locked, + = spinning; slip angle in radians)
  lockup_slip: float = -0.25
  spin_slip: float = 0.20
  spin_min_throttle: float = 0.20
  understeer_front_rad: float = 0.12
  understeer_margin_rad: float = 0.05
  oversteer_rear_rad: float = 0.10
  oversteer_margin_rad: float = 0.04
  slide_min_steer: float = 0.08
  slide_min_speed: float = 60.0
  event_min_s: float = 0.10
  coast_min_s: float = 0.40
  coast_min_speed: float = 120.0
  # comparison with personal best per corner
  min_loss_s: float = 0.05
  deep_brake_m: float = 8.0
  deep_apex_kmh: float = 4.0
  deep_pickup_m: float = 12.0
  early_brake_m: float = 12.0
  late_pickup_m: float = 15.0
  slow_apex_kmh: float = 6.0
  # laps
  min_lap_coverage: float = 0.90
  tyre_window_laps: int = 5


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _interp(samples, dists, d, key):
  """Value of samples[key] at lap distance d (linear interpolation)."""
  if not samples:
    return None
  i = bisect.bisect_left(dists, d)
  if i <= 0:
    return samples[0][key]
  if i >= len(samples):
    return samples[-1][key]
  a, b = samples[i - 1], samples[i]
  span = b["dist"] - a["dist"]
  if span <= 0:
    return b[key]
  f = (d - a["dist"]) / span
  return a[key] + (b[key] - a[key]) * f


def _runs(samples, pred, min_s):
  """Contiguous runs where pred(sample) is truthy, at least min_s long.

  pred returns a falsy value or a set of wheel names; returns
  [(start_dist, duration_s, Counter(wheel -> samples))].
  """
  out, start, wheels = [], None, Counter()

  def close(end_idx):
    if start is None:
      return
    dur = (samples[end_idx]["lap_ms"] - samples[start]["lap_ms"]) / 1000.0
    if dur >= min_s:
      out.append((samples[start]["dist"], round(dur, 2), wheels.copy()))

  for i, s in enumerate(samples):
    hit = pred(s)
    if hit:
      if start is None:
        start, wheels = i, Counter()
      wheels.update(hit if isinstance(hit, (set, list, tuple)) else ())
    elif start is not None:
      close(i - 1 if i > start else i)
      start = None
  if start is not None:
    close(len(samples) - 1)
  return out


def _main_wheel(counter: Counter) -> str | None:
  return counter.most_common(1)[0][0] if counter else None


def _wheel_label(counter: Counter) -> str:
  """'front-left', or 'fronts'/'rears' when both wheels of an axle are involved."""
  top = counter.most_common(2)
  if not top:
    return "a wheel"
  if len(top) == 2 and top[1][1] >= 0.7 * top[0][1]:
    pair = {top[0][0], top[1][0]}
    if pair == {"FL", "FR"}:
      return "fronts"
    if pair == {"RL", "RR"}:
      return "rears"
  return WHEEL_NAMES.get(top[0][0], top[0][0])


def fmt_lap(sec: float | None) -> str:
  if not sec:
    return "-"
  m, s = divmod(sec, 60)
  return f"{int(m)}:{s:06.3f}"


# ---------------------------------------------------------------------------
# data classes
# ---------------------------------------------------------------------------

@dataclass
class Corner:
  num: int
  entry: float      # timing gate before the braking zone (m)
  apex: float       # reference apex distance (m)
  exit: float       # timing gate after full throttle (m)
  x: float | None = None
  z: float | None = None

  @property
  def name(self) -> str:
    return f"C{self.num}"


@dataclass
class CornerRun:
  corner: int
  time: float | None
  brake_start: float | None
  lift_start: float | None
  apex_dist: float | None
  min_speed: float | None
  pickup: float | None
  full_throttle: float | None
  exit_speed: float | None
  lockups: list = field(default_factory=list)
  wheelspin: list = field(default_factory=list)
  understeer: list = field(default_factory=list)
  oversteer: list = field(default_factory=list)
  off_track: list = field(default_factory=list)
  coast_s: float = 0.0
  max_temp: dict = field(default_factory=dict)

  @property
  def clean(self) -> bool:
    return not self.off_track


@dataclass
class Finding:
  corner: int | None
  kind: str
  text: str          # full sentence for the screen / LLM
  voice: str         # short radio phrase (English)
  loss_s: float = 0.0
  tyre: str | None = None  # wheel name (FL/FR/RL/RR) or "front"/"rear"

  @property
  def score(self) -> float:
    return self.loss_s * 10 + (1.5 if self.tyre else 0.0)

  def to_dict(self) -> dict:
    return {
      "corner": f"C{self.corner}" if self.corner else None, "kind": self.kind,
      "text": self.text, "voice": self.voice, "loss_s": round(self.loss_s, 3),
      "tyre": self.tyre,
    }


# ---------------------------------------------------------------------------
# the coach
# ---------------------------------------------------------------------------

class DrivingCoach:
  def __init__(self, config: CoachConfig | None = None):
    self.cfg = config or CoachConfig()
    self.reset()

  # -- lifecycle --------------------------------------------------------------

  def reset(self, session: dict | None = None):
    self.session = session or {}
    self.track_length = float((session or {}).get("track_length") or 0)
    self.corners: list[Corner] = []
    self.best_runs: dict[int, CornerRun] = {}
    self.best_lap: float | None = None
    self.lap_no: int | None = None
    self.samples: list[dict] = []
    self.lap_flags = {"pit": False, "invalid": False}
    self.lap_start_wear = None
    self.lap_start_compound = None
    self.history: deque = deque(maxlen=self.cfg.tyre_window_laps)  # recent lap analyses
    self.report: dict | None = None
    self.laps_done = 0
    self._listeners = []

  def on_lap_report(self, fn):
    """Register fn(report: dict), called after every completed lap."""
    self._listeners.append(fn)

  def on_session(self, info: dict):
    listeners = self._listeners
    self.reset(info)
    self._listeners = listeners

  # -- sample stream ----------------------------------------------------------

  def on_sample(self, s: dict):
    if s.get("lap") is None or s.get("dist") is None:
      return
    if self.lap_no is None:
      self._start_lap(s)
    elif s["lap"] != self.lap_no:
      if s["lap"] == self.lap_no + 1:
        self._finish_lap(s.get("last_lap_ms") or 0)
      self._start_lap(s)
    if s["dist"] < 0:
      return  # before the line (formation, pit exit)

    if self.samples and s["dist"] < self.samples[-1]["dist"] - 30:
      # flashback / rewind: drop everything after the rewind point
      d = s["dist"]
      while self.samples and self.samples[-1]["dist"] >= d:
        self.samples.pop()
    elif self.samples and s["dist"] <= self.samples[-1]["dist"]:
      return  # duplicate or stationary
    self.samples.append(s)
    self.lap_flags["pit"] |= bool(s.get("pit"))
    self.lap_flags["invalid"] |= bool(s.get("invalid"))

  def _start_lap(self, s):
    self.lap_no = s["lap"]
    self.samples = []
    self.lap_flags = {"pit": bool(s.get("pit")), "invalid": False}
    self.lap_start_wear = dict(s["wear"]) if s.get("wear") else None
    self.lap_start_compound = s.get("compound")

  # -- lap analysis -----------------------------------------------------------

  def _coverage_ok(self, samples) -> bool:
    if len(samples) < 50:
      return False
    max_d = samples[-1]["dist"]
    if self.track_length:
      return max_d >= self.cfg.min_lap_coverage * self.track_length and samples[0]["dist"] < 0.1 * self.track_length
    return max_d > 1500

  def _finish_lap(self, lap_ms: int):
    samples, lap_no = self.samples, self.lap_no
    flags = dict(self.lap_flags)
    end_wear = samples[-1].get("wear") if samples else None
    compound = (samples[-1].get("compound") if samples else None) or self.lap_start_compound
    if not self._coverage_ok(samples):
      return
    lap_time = lap_ms / 1000.0 if lap_ms else None
    if not self.track_length:
      self.track_length = samples[-1]["dist"]
    if not self.corners and not flags["pit"]:
      self.corners = self.detect_corners(samples)
    if not self.corners:
      return
    try:
      report = self.analyze_lap(lap_no, samples, lap_time, flags, self.lap_start_wear, end_wear, compound)
    except Exception as e:  # coaching must never take the engineer down
      print(f"[coach] lap analysis failed: {e}")
      return
    self.report = report
    self.laps_done += 1
    for fn in self._listeners:
      try:
        fn(report)
      except Exception as e:
        print(f"[coach] listener error: {e}")

  def detect_corners(self, samples) -> list[Corner]:
    """Braking/lifting zones with a real speed drop become corners with timing gates."""
    cfg = self.cfg
    zones, i, n = [], 0, len(samples)
    while i < n:
      s = samples[i]
      if s["brake"] >= cfg.brake_on or s["throttle"] < 0.3:
        start, j, min_i, braked = i, i, i, False
        while j < n and samples[j]["throttle"] < cfg.throttle_lift:
          braked |= samples[j]["brake"] >= cfg.brake_on
          if samples[j]["speed"] < samples[min_i]["speed"]:
            min_i = j
          j += 1
        top = max(x["speed"] for x in samples[max(0, start - 5):start + 1])
        if top - samples[min_i]["speed"] >= cfg.min_corner_drop_kmh:
          zones.append((samples[start]["dist"], samples[min_i], samples[min(j, n - 1)]["dist"]))
        i = j + 1
      else:
        i += 1

    corners, last_exit = [], samples[0]["dist"]
    end = samples[-1]["dist"]
    for k, (start_d, apex_s, full_d) in enumerate(zones):
      entry = max(start_d - cfg.gate_before_m, last_exit + 1, samples[0]["dist"] + 1)
      nxt = zones[k + 1][0] - cfg.gate_before_m if k + 1 < len(zones) else end - 1
      exit_ = min(full_d + cfg.gate_after_m, nxt - 1, end - 1)
      if exit_ <= apex_s["dist"] or entry >= apex_s["dist"]:
        continue
      corners.append(Corner(len(corners) + 1, round(entry, 1), round(apex_s["dist"], 1),
                            round(exit_, 1), apex_s.get("x"), apex_s.get("z")))
      last_exit = exit_
    return corners

  def measure_corner(self, c: Corner, samples, dists, prev_apex: float | None = None,
                     seg_end: float | None = None) -> CornerRun:
    """Measure one corner. Its time runs from its entry gate to the next corner's entry
    gate (a mini-sector), so a poor exit that costs time down the straight counts too."""
    cfg = self.cfg
    start_d = c.entry - cfg.brake_search_m
    if prev_apex is not None:
      start_d = max(start_d, prev_apex + 1)  # never reach back into the previous corner
    lo = bisect.bisect_left(dists, start_d)
    hi = bisect.bisect_right(dists, c.exit)
    win = samples[lo:hi]
    if len(win) < 3:
      return CornerRun(c.num, None, None, None, None, None, None, None, None)

    # apex = slowest point between the gates
    inner = [s for s in win if c.entry <= s["dist"] <= c.exit] or win
    apex = min(inner, key=lambda s: s["speed"])
    ai = win.index(apex)

    # walk back from the apex through the deceleration phase
    k = ai
    while k > 0 and (win[k - 1]["throttle"] < cfg.throttle_lift or win[k - 1]["brake"] >= cfg.brake_on):
      k -= 1
    decel = win[k:ai + 1]
    brake_s = next((s for s in decel if s["brake"] >= cfg.brake_on), None)
    pickup = next((s for s in win[ai:] if s["throttle"] >= cfg.throttle_pickup), None)
    full = next((s for s in win[ai:] if s["throttle"] >= cfg.throttle_full), None)

    t_in = _interp(samples, dists, c.entry, "lap_ms")
    t_out = _interp(samples, dists, seg_end if seg_end else c.exit, "lap_ms")
    corner_time = (t_out - t_in) / 1000.0 if t_in is not None and t_out is not None and t_out > t_in else None

    zone = [s for s in win if s["dist"] >= decel[0]["dist"]]
    has_ex = any(s.get("slip_ratio") for s in zone)

    def lockup(s):
      if not has_ex or not s.get("slip_ratio") or s["brake"] < cfg.brake_on:
        return None
      return {WHEELS[w] for w in range(4) if s["slip_ratio"][w] <= cfg.lockup_slip}

    def spin(s):
      if not has_ex or not s.get("slip_ratio") or s["throttle"] < cfg.spin_min_throttle:
        return None
      return {WHEELS[w] for w in REAR if s["slip_ratio"][w] >= cfg.spin_slip}

    def angles(s):
      sa = s.get("slip_angle")
      if not sa or abs(s["steer"]) < cfg.slide_min_steer or s["speed"] < cfg.slide_min_speed:
        return None
      return max(abs(sa[2]), abs(sa[3])), max(abs(sa[0]), abs(sa[1]))

    def under(s):
      a = angles(s)
      if a and a[0] >= cfg.understeer_front_rad and a[0] - a[1] >= cfg.understeer_margin_rad:
        return {"FL", "FR"}
      return None

    def over(s):
      a = angles(s)
      if a and a[1] >= cfg.oversteer_rear_rad and a[1] - a[0] >= cfg.oversteer_margin_rad:
        return {"RL", "RR"}
      return None

    def off(s):
      wheels = {WHEELS[w] for w in range(4) if s["surface"][w] in OFF_TRACK_SURFACES}
      return wheels if len(wheels) >= 2 else None

    def coast(s):
      return (s["throttle"] < cfg.coast_pedal and s["brake"] < cfg.coast_pedal
              and s["speed"] >= cfg.coast_min_speed) and {"car"}

    gated = [s for s in win if s["dist"] >= min(c.entry, decel[0]["dist"])]
    max_temp = {}
    for s in gated:
      for w in range(4):
        max_temp[WHEELS[w]] = max(max_temp.get(WHEELS[w], 0), s["tyre_surface"][w])

    return CornerRun(
      corner=c.num,
      time=round(corner_time, 3) if corner_time else None,
      brake_start=brake_s["dist"] if brake_s else None,
      lift_start=decel[0]["dist"],
      apex_dist=apex["dist"],
      min_speed=apex["speed"],
      pickup=pickup["dist"] if pickup else None,
      full_throttle=full["dist"] if full else None,
      exit_speed=_interp(samples, dists, c.exit, "speed"),
      lockups=_runs(gated, lockup, cfg.event_min_s),
      wheelspin=_runs(gated, spin, cfg.event_min_s),
      understeer=_runs(gated, under, cfg.event_min_s),
      oversteer=_runs(gated, over, cfg.event_min_s),
      off_track=_runs(gated, off, 0.0),
      coast_s=round(sum(r[1] for r in _runs(gated, coast, cfg.coast_min_s)), 2),
      max_temp=max_temp,
    )

  def _compare(self, c: Corner, run: CornerRun, ref: CornerRun | None) -> list[Finding]:
    cfg, out, n = self.cfg, [], c.num
    loss = (run.time - ref.time) if (ref and run.time and ref.time) else 0.0

    if ref and ref is not run and loss >= cfg.min_loss_s:
      db = run.brake_start - ref.brake_start if run.brake_start is not None and ref.brake_start is not None else None
      dv = run.min_speed - ref.min_speed if run.min_speed is not None and ref.min_speed is not None else 0
      dp = run.pickup - ref.pickup if run.pickup is not None and ref.pickup is not None else None
      if db is not None and db >= cfg.deep_brake_m and (dv <= -cfg.deep_apex_kmh or (dp or 0) >= cfg.deep_pickup_m):
        out.append(Finding(n, "too_deep",
          f"C{n}: braking too deep — {db:.0f} m later than your best, apex {dv:+.0f} km/h, "
          f"back on power {max(dp or 0, 0):.0f} m later; lost {loss:.2f}s. Brake at your best marker "
          f"and carry speed to the apex instead.",
          f"Corner {n}, you're braking too deep. Brake at your marker, carry the apex speed.", loss))
      elif db is not None and db <= -cfg.early_brake_m:
        out.append(Finding(n, "early_brake",
          f"C{n}: braking {-db:.0f} m earlier than your best (apex {dv:+.0f} km/h); lost {loss:.2f}s.",
          f"Corner {n}, you can brake {round(-db / 5) * 5:.0f} meters later.", loss))
      elif dv <= -cfg.slow_apex_kmh:
        out.append(Finding(n, "slow_apex",
          f"C{n}: apex speed {dv:+.0f} km/h below your best — over-slowing mid-corner; lost {loss:.2f}s.",
          f"Corner {n}, carry more speed to the apex.", loss))
      elif dp is not None and dp >= cfg.late_pickup_m:
        out.append(Finding(n, "late_throttle",
          f"C{n}: back on throttle {dp:.0f} m later than your best; lost {loss:.2f}s.",
          f"Corner {n}, pick up the throttle earlier.", loss))
      else:
        out.append(Finding(n, "slower",
          f"C{n}: {loss:.2f}s slower than your best through this corner.",
          f"Corner {n}, {loss:.1f} down on your best.", loss))

    for _, dur, wheels in run.lockups:
      w, label = _main_wheel(wheels), _wheel_label(wheels)
      out.append(Finding(n, "lockup",
        f"C{n}: {label} locked under braking ({dur:.1f}s) — flat-spot risk, front wear.",
        f"Lock-up, {label}, into corner {n}. Ease the brake pressure.", 0.0, w))
    for _, dur, wheels in run.wheelspin:
      w, label = _main_wheel(wheels), _wheel_label(wheels)
      out.append(Finding(n, "wheelspin",
        f"C{n}: wheelspin on exit ({label}, {dur:.1f}s) — burns the rears.",
        f"Wheelspin out of corner {n}. Squeeze the throttle.", 0.0, w))
    if sum(r[1] for r in run.understeer) >= 0.3:
      out.append(Finding(n, "understeer",
        f"C{n}: understeer for {sum(r[1] for r in run.understeer):.1f}s — the fronts are sliding.",
        f"Understeer in corner {n}. Slower entry, wait for the front.", 0.0, "front"))
    if run.oversteer:
      out.append(Finding(n, "oversteer",
        f"C{n}: rear sliding for {sum(r[1] for r in run.oversteer):.1f}s.",
        f"Rear stepping out in corner {n}. Smoother on the throttle.", 0.0, "rear"))
    if run.off_track:
      out.append(Finding(n, "off_track",
        f"C{n}: off the track ({len(run.off_track)}x) — time and tyre damage risk.",
        f"Off track in corner {n}. Tidy it up.", max(loss, 0.0)))
    if run.coast_s >= self.cfg.coast_min_s:
      out.append(Finding(n, "coasting",
        f"C{n}: coasting {run.coast_s:.1f}s without throttle or brake before the corner.",
        f"Coasting into corner {n}. Brake later or stay flat longer.", 0.0))
    return out

  def analyze_lap(self, lap_no, samples, lap_time, flags, start_wear, end_wear, compound) -> dict:
    dists = [s["dist"] for s in samples]
    last = min(samples[-1]["dist"], self.track_length - 5) if self.track_length else samples[-1]["dist"]
    runs = {
      c.num: self.measure_corner(
        c, samples, dists,
        prev_apex=self.corners[k - 1].apex if k else None,
        seg_end=self.corners[k + 1].entry if k + 1 < len(self.corners) else last,
      )
      for k, c in enumerate(self.corners)
    }

    findings, table = [], []
    for c in self.corners:
      run, ref = runs[c.num], self.best_runs.get(c.num)
      findings += self._compare(c, run, ref)
      delta = round(run.time - ref.time, 3) if (ref and run.time and ref.time) else None
      table.append({
        "corner": c.name, "num": c.num, "dist": c.apex, "x": c.x, "z": c.z,
        "time": run.time, "delta": delta,
        "brake_m": round(run.brake_start - ref.brake_start) if (ref and run.brake_start is not None and ref.brake_start is not None) else None,
        "apex_kmh": round(run.min_speed - ref.min_speed) if (ref and run.min_speed is not None and ref.min_speed is not None) else None,
        "min_speed": run.min_speed,
        "flags": sorted({f.kind for f in findings if f.corner == c.num and f.kind not in ("slower",)}),
      })

    # personal bests per corner: only from laps the game counts and without pit visits
    usable = not flags["pit"] and not flags["invalid"]
    for c in self.corners:
      run = runs[c.num]
      ref = self.best_runs.get(c.num)
      if usable and run.time and run.clean and (ref is None or run.time < ref.time):
        self.best_runs[c.num] = run
    if usable and lap_time and (self.best_lap is None or lap_time < self.best_lap):
      self.best_lap = lap_time

    wear_delta = None
    if start_wear and end_wear:
      wear_delta = {w: round(end_wear.get(w, 0) - start_wear.get(w, 0), 2) for w in WHEELS}
      if any(v < 0 for v in wear_delta.values()):
        wear_delta = None  # tyres changed during the lap

    params = _get_compound(compound or "MEDIUM")
    hot_corners = Counter()
    for num, run in runs.items():
      for w, temp in run.max_temp.items():
        if temp >= params.t_hot:
          hot_corners[(w, num)] += 1

    self.history.append({"lap": lap_no, "runs": runs, "wear": wear_delta,
                         "hot": hot_corners, "pit": flags["pit"]})
    tyre = self._tyre_report(compound)

    findings.sort(key=lambda f: f.score, reverse=True)
    lost = sum(max(r["delta"] or 0, 0) for r in table)
    focus = self._focus_corners()
    return {
      "lap": lap_no, "lap_time": lap_time, "lap_time_str": fmt_lap(lap_time),
      "best_lap": self.best_lap, "best_lap_str": fmt_lap(self.best_lap),
      "valid": not flags["invalid"], "pit": flags["pit"], "compound": compound,
      "corners_known": len(self.corners),
      "time_lost_in_corners": round(lost, 3),
      "findings": [f.to_dict() for f in findings[:8]],
      "top": findings[0].to_dict() if findings else None,
      "corner_table": table,
      "focus": focus,
      "tyre": tyre,
      "has_slip_data": any(s.get("slip_ratio") for s in samples[:200]),
    }

  def _focus_corners(self) -> list[dict]:
    """Corners where time is lost lap after lap (average over recent laps)."""
    losses = {}
    for lap in self.history:
      for num, run in lap["runs"].items():
        ref = self.best_runs.get(num)
        if ref and run.time and ref.time and run is not ref:
          losses.setdefault(num, []).append(max(run.time - ref.time, 0))
    # median, so one big mistake doesn't look like a habit
    typical = [(statistics.median(v), num, len(v)) for num, v in losses.items() if len(v) >= 3]
    typical.sort(reverse=True)
    return [{"corner": f"C{num}", "avg_loss_s": round(a, 3), "laps": n} for a, num, n in typical[:3] if a >= 0.03]

  def _tyre_report(self, compound) -> dict:
    """Which tyre wears fastest, why, and what to change (over recent laps)."""
    laps = [h for h in self.history if not h["pit"]]
    wear_laps = [h["wear"] for h in laps if h["wear"]]
    per_lap = None
    if wear_laps:
      per_lap = {w: round(sum(l[w] for l in wear_laps) / len(wear_laps), 2) for w in WHEELS}

    lock, spin, under, over = Counter(), Counter(), Counter(), Counter()
    hot = Counter()
    for h in laps:
      for num, run in h["runs"].items():
        lock[num] += len(run.lockups)
        spin[num] += len(run.wheelspin)
        under[num] += 1 if sum(r[1] for r in run.understeer) >= 0.3 else 0
        over[num] += len(run.oversteer)
      hot.update(h["hot"])

    def worst(counter, k=3):
      return [f"C{n}" for n, v in counter.most_common(k) if v > 0]

    limiting = None
    if per_lap:
      front = (per_lap["FL"] + per_lap["FR"]) / 2
      rear = (per_lap["RL"] + per_lap["RR"]) / 2
      limiting = "rear" if rear > front * 1.05 else "front" if front > rear * 1.05 else "even"
      worst_wheel = max(per_lap, key=per_lap.get)
    else:
      worst_wheel = None

    tips = []
    rear_issue = sum(spin.values()) + sum(over.values())
    front_issue = sum(lock.values()) + sum(under.values())

    def needed(axle):  # the axle that limits the stint gets advice from the first event
      return 1 if limiting in (axle, None) else 2

    if sum(spin.values()) >= needed("rear"):
      tips.append({"axle": "rear", "corners": worst(spin),
                   "text": f"Wheelspin out of {', '.join(worst(spin))} is eating the rears: squeeze the throttle "
                           f"in, short-shift or take one gear higher on exit."})
    if sum(over.values()) >= needed("rear"):
      tips.append({"axle": "rear", "corners": worst(over),
                   "text": f"Rear slides in {', '.join(worst(over))}: wait until the car is straighter "
                           f"before full throttle."})
    if sum(lock.values()) >= 1:  # flat-spot risk: always worth a word
      tips.append({"axle": "front", "corners": worst(lock),
                   "text": f"Lock-ups into {', '.join(worst(lock))}: hit the brake hard, then release "
                           f"progressively as speed drops; brake a touch earlier if it keeps happening."})
    if sum(under.values()) >= needed("front"):
      tips.append({"axle": "front", "corners": worst(under),
                   "text": f"Understeer in {', '.join(worst(under))} scrubs the fronts: slower entry, "
                           f"less steering lock, let the front bite before adding throttle."})
    hot_wheels = Counter()
    for (w, num), v in hot.items():
      hot_wheels[w] += v
    if hot_wheels:
      w, _ = hot_wheels.most_common(1)[0]
      corners = [f"C{num}" for (ww, num), v in hot.most_common() if ww == w][:3]
      tips.append({"axle": "front" if w in ("FL", "FR") else "rear", "corners": corners,
                   "text": f"{WHEEL_NAMES[w].capitalize()} runs above its window in {', '.join(corners)}"
                           + (": lift-and-coast into the heavy braking zones to cool the fronts."
                              if w in ("FL", "FR") else ": calmer throttle on exit to cool the rears.")})
    if limiting in ("rear", "front") and not any(t["axle"] == limiting for t in tips):
      tips.append({"axle": limiting, "corners": [],
                   "text": f"The {limiting}s wear fastest; no single corner stands out — "
                           f"{'smooth throttle on exits and fewer kerbs' if limiting == 'rear' else 'gentler turn-in and trail braking'} "
                           f"protects them."})
    # tips for the limiting axle first
    tips.sort(key=lambda t: 0 if t["axle"] == limiting else 1)
    return {
      "wear_per_lap": per_lap, "limiting_axle": limiting, "worst_wheel": worst_wheel,
      "laps_analysed": len(laps), "front_events": front_issue, "rear_events": rear_issue,
      "tips": tips[:3],
    }

  # -- outputs ---------------------------------------------------------------

  def summary_for_llm(self) -> str:
    r = self.report
    if not r:
      if self.corners:
        return "DRIVER COACHING: corners learned, waiting for the next completed lap."
      return "DRIVER COACHING: learning the track (needs one full flying lap)."
    lines = [
      f"DRIVER COACHING (player telemetry, lap {r['lap']}: {r['lap_time_str']}, best {r['best_lap_str']}, "
      f"{'valid' if r['valid'] else 'INVALID'}{', pit lap' if r['pit'] else ''}; corners C1..C{r['corners_known']} "
      f"are braking zones in order from the start line, not official turn numbers):",
    ]
    for f in r["findings"][:5]:
      lines.append(f"  - {f['text']}")
    if not r["findings"]:
      lines.append("  - No issues found on this lap.")
    if r["focus"]:
      lines.append("  Consistent losses: " + ", ".join(f"{x['corner']} {x['avg_loss_s']:.2f}s/lap" for x in r["focus"]))
    t = r["tyre"]
    if t["wear_per_lap"]:
      w = t["wear_per_lap"]
      lines.append(f"TYRE MANAGEMENT: wear per lap FL {w['FL']}% FR {w['FR']}% RL {w['RL']}% RR {w['RR']}% "
                   f"(limiting: {t['limiting_axle']}).")
    for tip in t["tips"]:
      lines.append(f"  - {tip['text']}")
    return "\n".join(lines)

  def radio_context(self) -> dict | None:
    r = self.report
    if not r:
      return {"status": "learning the track"} if not self.corners else {"status": "waiting for a full lap"}
    return {
      "lap": r["lap"], "lapTime": r["lap_time_str"], "best": r["best_lap_str"],
      "issues": [f["text"] for f in r["findings"][:3]],
      "consistentLosses": r["focus"],
      "tyre": {"wearPerLap": r["tyre"]["wear_per_lap"], "limiting": r["tyre"]["limiting_axle"],
               "tips": [t["text"] for t in r["tyre"]["tips"]]},
      "note": "C-numbers are braking zones in order from the start line",
    }

  def corners_for_map(self) -> list[dict]:
    return [{"corner": c.name, "x": c.x, "z": c.z, "dist": c.apex} for c in self.corners if c.x is not None]
