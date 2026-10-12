"""
DeepSeek real-time strategy analysis — sends all 22 cars to the AI.
Integrates physics engine models (tire, fuel, ERS, weather, opponent) for precise data.
"""
import os, json
from openai import OpenAI
from dotenv import load_dotenv
from engine_models import (
    analyze_telemetry_frame,
    format_analysis_for_llm,
    TireResult,
    FuelResult,
    OpponentThreat,
)

load_dotenv()
client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url=os.getenv("DEEPSEEK_BASE_URL"),
)

SYSTEM_PROMPT = """# F1 24 Strategy Engineer AI — System Instructions

You are the pit-wall strategy engineer for F1 24. Your input is live telemetry for all cars (JSON). Your output is tactical advice for the driver, global situational awareness, and lap-time comparison.

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
Return JSON only: {"advice": "...", "voice": "..."}. Use English in advice.

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
```

---

## Constraints

- No filler like "based on data analysis" or "I suggest" — direct orders only
- If data is insufficient, output `[INSUFFICIENT DATA]` and skip that section; do not invent numbers
- Output JSON: {"advice": "...", "voice": "..."}
  - advice: full analysis (situation/laps/tires/tactics) for the screen
  - voice: one short order for the player only (<20 words), for TTS. Example: {"advice": "L3/57...", "voice": "BOX IN 2 LAPS, fronts 78%"}
  - voice must only address the player car, never other drivers
- Use English"""


RADIO_SYSTEM_PROMPT = """Race engineer on live team radio. Answer the driver using the telemetry JSON.
Rules: English only. Max 18 words. Direct radio style. No filler. No JSON keys except voice.
Return: {"voice":"..."}
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
    }


def analyze(cars: list, player_idx: int, lap: int, track: dict = None) -> dict:
    try:
        # 先运行物理模型，获取精确数据
        track_data = track or {"weather": {}, "name": "Unknown"}
        engine_analysis = analyze_telemetry_frame(cars, player_idx, lap, track_data)
        engine_summary = format_analysis_for_llm(engine_analysis, cars, player_idx, lap)

        # 将物理模型结果附加到用户输入
        user_content = (
            f"PHYSICS ENGINE ANALYSIS:\n{engine_summary}\n\n"
            f"RAW TELEMETRY:\n{json.dumps({'player_idx': player_idx, 'lap': lap, 'cars': cars}, ensure_ascii=False)}"
        )

        resp = client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content}
            ],
            response_format={"type": "json_object"},
            timeout=60
        )
        result = json.loads(resp.choices[0].message.content)
        return {
            "advice": result.get("advice", ""),
            "voice": result.get("voice", ""),
        }
    except Exception as e:
        print(f"DeepSeek analysis failed: {e}")
        return {"advice": "", "voice": ""}


def radio_reply(message: str, cars: list, player_idx: int, lap: int, track: dict | None = None) -> dict:
    """Answer a live driver radio call using current telemetry (latency-tuned)."""
    message = (message or "").strip()
    if not message:
        return {"reply": "", "voice": ""}

    cars_list = cars if isinstance(cars, list) else []
    player = None
    if 0 <= player_idx < len(cars_list):
        player = cars_list[player_idx]
    else:
        for c in cars_list:
            if c.get("idx") == player_idx:
                player = c
                break

    # 运行物理模型获取精确数据
    track_data = track or {"weather": {}, "name": "Unknown"}
    if isinstance(track_data, dict) and "name" in track_data:
        track_data = {"weather": {}, **track_data}
    engine_analysis = analyze_telemetry_frame(cars_list, player_idx, lap, track_data)

    # Compact standings only — full 22-car blobs slow the LLM a lot
    slim = sorted((_slim_car(c) for c in cars_list), key=lambda x: x.get("pos") or 99)
    payload = {
        "msg": message,
        "lap": lap,
        "track": (track or {}).get("name") if isinstance(track, dict) else track,
        "you": _slim_car(player) if player else {"idx": player_idx},
        "field": slim[:12],
        # 物理模型精确数据
        "physics": {
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
        },
    }

    try:
        resp = client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[
                {"role": "system", "content": RADIO_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
            ],
            response_format={"type": "json_object"},
            max_tokens=60,
            temperature=0.3,
            timeout=20,
        )
        result = json.loads(resp.choices[0].message.content)
        voice = (result.get("voice") or result.get("reply") or result.get("advice") or "").strip()
        return {"reply": voice, "voice": voice}
    except Exception as e:
        print(f"DeepSeek radio reply failed: {e}")
        return {
            "reply": "Say again, radio copy lost.",
            "voice": "Say again, radio copy lost.",
        }
