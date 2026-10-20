"""
Real-time strategy analysis on Microsoft Foundry — sends the whole field to the chat model.
The physics models (tire, fuel, ERS, weather, opponent) run first so the model reasons
over computed numbers instead of guessing them.
"""
import json
from engine_models import analyze_telemetry_frame, format_analysis_for_llm, find_player
from foundry import (
    chat_json,
    RADIO_LANGUAGE_NAME,
    STRATEGY_REASONING,
    RADIO_REASONING,
)

SYSTEM_PROMPT = """# F1 25 Strategy Engineer AI — System Instructions

You are the pit-wall strategy engineer for F1 25. Your input is live telemetry for all cars (JSON). Your output is tactical advice for the driver, global situational awareness, and lap-time comparison.

---

## Role

You are the strategy brain between the driver and the team. You do not drive the car. You:
- Analyze opponent telemetry
- Predict tire degradation, lap-time evolution, and pit windows
- Issue actionable tactical orders

**Note**: Never say "based on my analysis" — output conclusions and orders directly.

---

## Output Requirements

Output strategy advice in the format below. **No disclaimers or meta commentary.**
Return JSON only: {"advice": "...", "voice": "..."}. Write both fields in {LANG}.

### 1) Race Situation

Format:
```
[Lap L{lap}/{total}] [session type] | {weather/track conditions}
Pos  | Driver        | Gap  | Tyre  | Age | FL_wear | RL_wear | Trend
P{X} | {driver}      | +{s} | {C1-5}| {n} | {x}%    | {x}%    | {↑/↓/→}
...
```

### 2) Lap Time Comparison

Format:
```
LAP TIME COMPARISON
─────────────────────────────────────────────────
Rank  Driver      L{s}    L{s-1}  L{s-2}  Best   ΔBest
P{X}  {driver}    1:23.4  1:23.7  1:23.1  1:22.9 +0.0
...
─────────────────────────────────────────────────
```

If lap times are insufficient, use speed comparison:
```
SPEED COMPARISON (avg km/h, last lap)
──────────────────────────────────────────────────
Rank  Driver      S1 avg   S2 avg   S3 avg   Overall  Top Speed
P{X}  {driver}    252      278      245      258      324
...
──────────────────────────────────────────────────
```

### 3) Tire Degradation Forecast

Format:
```
TIRE DEGRADATION FORECAST
────────────────────────────────────────────────────
Car    Tyre  Age  Current Wear  Cliff At  Remaining Laps  Rate
#XX    C4    8    FL:65 FR:62   90%       ~3              1.3/lap
       └── -0.11s/lap deg | window 75-95°C | current 82°C OK
────────────────────────────────────────────────────
```

### 4) Tactical Orders

Format:
```
{time/lap} | Order | Rationale
──────────────────────────────────────────────
L{n}   | {order}              | {brief reason}
...
```

Example:
```
L8/57  | BOX NOW — switch Medium | Front wear 78%, cliff in 3 laps, +0.4s/lap
L15/57 | DEFEND P2 — DRS inside  | He gains +0.3s DRS, slow T3 exit by 0.15s
L20/57 | Fuel save — lift per lap | 2.3kg short to finish
```

### 5) Driver Coaching

Only when a DRIVER COACHING section is present. Max 4 lines: the corners costing the most
time and what to change there, then the tyre-management tip for the axle that limits the
stint. Use the corner names (C1, C2, ...) exactly as given.
```
DRIVER COACHING
C3   | too deep: brake at your marker, carry apex speed | -0.18s
TYRES| rears limit (2.1%/lap): squeeze throttle out of C5, C9
```

---

## Analysis Framework

Priority order:
```
1. Your position (P?) and gaps
2. Recent lap-time trend (faster or slower)
3. Tire state: wear %, temp window, laps remaining
4. Opponent moves: pits, compound delta, pace trend
5. Fuel: enough to finish?
6. External events
7. Driving: corners losing time, and driving habits that wear the limiting tyres
```

---

## Constraints

- No filler like "based on data analysis" or "I suggest" — direct orders only
- If data is insufficient, output `[INSUFFICIENT DATA]` and skip that section; do not invent numbers
- Output JSON: {"advice": "...", "voice": "..."}
  - advice: full analysis (situation/laps/tires/tactics) for the screen
  - voice: one short order for the player only (<20 words), for TTS. Example: {"advice": "L3/57...", "voice": "BOX IN 2 LAPS, fronts 78%"}
  - voice must only address the player car, never other drivers
- Write in {LANG}
- Tyre compounds go from C6 (softest) to C0 (hardest); `fuelLapsLeft` is the game's own fuel margin in laps (+ = spare)
- Respect the pit window from the session data and any SC/VSC (cheap pit stop)
- When tyre life decides the strategy, factor in the coaching tyre tips (smoother driving extends the stint)""".replace("{LANG}", RADIO_LANGUAGE_NAME)


RADIO_SYSTEM_PROMPT = f"""Race engineer on live team radio in F1 25. Answer the driver using the telemetry JSON.
Rules: {RADIO_LANGUAGE_NAME} only. Max 18 words (max 28 for driving-technique questions). Direct radio style.
No filler. Use only numbers present in the JSON.
Questions about driving, braking, where time is lost or saving tyres: answer from `coaching`
(name the corner as given, e.g. "corner 3", and say what to change).
Return: {{"voice":"..."}}
"""

COACH_CALL_PROMPT = f"""You are an F1 race engineer giving one short coaching call on team radio after a lap.
Turn the finding into one radio call in {RADIO_LANGUAGE_NAME}, max 16 words, direct, encouraging, no filler.
Say corners as "corner N". Keep the numbers that matter (meters, km/h, tenths). Return: {{"voice":"..."}}
"""


def _slim_car(c: dict) -> dict:
    """Minimal fields for fast radio context."""
    if not isinstance(c, dict):
        return {}
    tw = c.get("tireWear") or {}
    avg_wear = None
    if tw:
        avg_wear = round((tw.get("FL", 0) + tw.get("FR", 0) + tw.get("RL", 0) + tw.get("RR", 0)) / 4)
    return {
        "idx": c.get("idx"),
        "name": c.get("name"),
        "pos": c.get("position"),
        "spd": c.get("speed"),
        "compound": c.get("compound"),
        "tyreAge": c.get("tyreAge", c.get("stintLaps")),
        "wear": avg_wear,
        "fuel": c.get("fuel"),
        "gapA": c.get("gapToAhead"),
        "gapB": c.get("gapToBehind", c.get("gapToNext")),
        "gapL": c.get("gapToLeader"),
        "drs": c.get("drs"),
        "ers": c.get("ersBattery"),
        "dmg": c.get("damage"),
        "best": c.get("bestLapTime"),
        "last": c.get("lastLapTime"),
        "fuelLaps": c.get("fuelLapsLeft"),
        "ersMode": c.get("ersMode"),
        "pit": c.get("pitStatus") or None,
        "stops": c.get("pitStops"),
    }


def analyze(cars: list, player_idx: int, lap: int, track: dict = None, coaching: str = "") -> dict:
    try:
        # Run the physics models first so the LLM gets computed numbers
        track_data = track or {"name": "Unknown"}
        engine_analysis = analyze_telemetry_frame(cars, player_idx, lap, track_data)
        engine_summary = format_analysis_for_llm(engine_analysis, cars, player_idx, lap)

        user_content = (
            f"PHYSICS ENGINE ANALYSIS:\n{engine_summary}\n\n"
            f"SESSION: {json.dumps(track_data, ensure_ascii=False)}\n\n"
            + (f"{coaching}\n\n" if coaching else "") +
            f"RAW TELEMETRY:\n{json.dumps({'player_idx': player_idx, 'lap': lap, 'cars': cars}, ensure_ascii=False, separators=(',', ':'))}"
        )

        result = chat_json(
            SYSTEM_PROMPT, user_content,
            max_tokens=4000, timeout=60, reasoning_effort=STRATEGY_REASONING,
        )
        return {
            "advice": result.get("advice", ""),
            "voice": result.get("voice", ""),
        }
    except Exception as e:
        print(f"Foundry strategy analysis failed: {e}")
        return {"advice": "", "voice": ""}


def radio_reply(message: str, cars: list, player_idx: int, lap: int, track: dict | None = None,
                coaching: dict | None = None) -> dict:
    """Answer a live driver radio call using current telemetry (latency-tuned)."""
    message = (message or "").strip()
    if not message:
        return {"reply": "", "voice": ""}

    cars_list = cars if isinstance(cars, list) else []
    player = find_player(cars_list, player_idx) or None

    track_data = track if isinstance(track, dict) else {"name": str(track or "Unknown")}

    # Compact standings only — the full 22/24-car blob slows the model down a lot
    slim = sorted((_slim_car(c) for c in cars_list), key=lambda x: x.get("pos") or 99)
    payload = {
        "msg": message,
        "lap": lap,
        "track": track_data.get("name"),
        "totalLaps": track_data.get("totalLaps"),
        "session": track_data.get("session"),
        "weather": track_data.get("weather"),
        "rain%": track_data.get("rainPercent"),
        "safetyCar": track_data.get("safetyCar") or None,
        "pitWindow": track_data.get("pitWindow"),
        "you": _slim_car(player) if player else {"idx": player_idx},
        "field": slim[:12],
        "physics": _radio_physics(cars_list, player_idx, lap, track_data),
    }
    if coaching:
        payload["coaching"] = coaching

    try:
        result = chat_json(
            RADIO_SYSTEM_PROMPT,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            max_tokens=600, timeout=20, reasoning_effort=RADIO_REASONING,
        )
        voice = (result.get("voice") or result.get("reply") or result.get("advice") or "").strip()
        return {"reply": voice, "voice": voice}
    except Exception as e:
        print(f"Foundry radio reply failed: {e}")
        return {
            "reply": "Say again, radio copy lost.",
            "voice": "Say again, radio copy lost.",
        }


def _radio_physics(cars_list, player_idx, lap, track_data) -> dict:
    """Compact physics-model summary for the radio prompt."""
    try:
        engine_analysis = analyze_telemetry_frame(cars_list, player_idx, lap, track_data)
    except Exception as e:
        print(f"Physics models failed: {e}")
        return {}
    return {
        "tire": {
            "compound": engine_analysis.player_tire.compound,
            "age": engine_analysis.player_tire.age_laps,
            "grip": engine_analysis.player_tire.grip_factor,
            "health": engine_analysis.player_tire.health_score,
            "loss_s": engine_analysis.player_tire.performance_loss_s,
            "remaining_laps": engine_analysis.player_tire.remaining_laps,
            "deg_rate": engine_analysis.player_tire.deg_rate_s_per_lap,
            "overheat": engine_analysis.player_tire.overheat_risk.value,
        },
        "fuel": {
            "margin_laps": engine_analysis.player_fuel.fuel_margin_laps,
            "can_push": engine_analysis.player_fuel.can_push,
            "must_save": engine_analysis.player_fuel.must_save,
            "effect_s": engine_analysis.player_fuel.fuel_effect_s,
        },
        "threats": [
            {"name": t.name, "level": t.threat_level, "gap": t.gap_to_player, "pace_delta": t.pace_delta}
            for t in engine_analysis.opponent_threats[:3]
        ],
    }


def coaching_call(finding_text: str, fallback: str) -> str:
    """One short spoken coaching line for a finding, in the radio language."""
    try:
        result = chat_json(
            COACH_CALL_PROMPT, finding_text,
            max_tokens=400, timeout=15, reasoning_effort=RADIO_REASONING,
        )
        return (result.get("voice") or "").strip() or fallback
    except Exception as e:
        print(f"Foundry coaching call failed: {e}")
        return fallback
