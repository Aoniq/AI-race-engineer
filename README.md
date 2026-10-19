# Box-Box for F1 25 on Microsoft Foundry

A fork of [Box-Box](https://github.com/ploverbyHB/box-box), the AI race engineer for home sim racing, ported to **F1 25** and to **Microsoft Foundry** for every AI step:

| Job | Upstream | This fork |
| --- | --- | --- |
| Strategy + radio replies | DeepSeek | Foundry chat deployment (e.g. `gpt-5.4-mini`) |
| Engineer voice (TTS) | ElevenLabs | Foundry `gpt-4o-mini-tts` |
| Driver voice (STT) | Browser Web Speech API | Foundry `gpt-4o-mini-transcribe` |

One Foundry resource, one endpoint and one key cover all three.

## What it does

- Listens for live **F1 25** UDP telemetry (packet format 2025, and the 24-car 2026 Season Pack format). F1 24 still works. **Gran Turismo 7** JSON telemetry is accepted too.
- Models tires (C6–C0, blisters), fuel (using the game's own fuel margin), ERS, weather and nearby opponents before the strategy call.
- Asks the Foundry chat model for pit-wall advice on a timed interval and speaks it with Foundry TTS.
- Lets the driver talk back on pit radio: the browser records, Foundry transcribes, the engineer answers in text and voice.
- Shows a live dashboard: standings, track map, selected-car data, and the radio log.
- Falls back to a 22-car simulator when a game is not connected.

## 1. Set up Microsoft Foundry

1. Create a Foundry resource in the Azure portal or at [ai.azure.com](https://ai.azure.com). Pick a region where all three models are offered — the audio models are the limiting factor (at the time of writing, Global Standard `gpt-4o-mini-tts` is listed for **East US 2**).
2. Deploy three models and note the deployment names:
   - a chat model, e.g. `gpt-5.4-mini` (fast, cheap, good at structured output). `gpt-4.1-mini` also works.
   - `gpt-4o-mini-tts`
   - `gpt-4o-mini-transcribe` (or `gpt-4o-transcribe` for higher accuracy)
3. Copy the resource endpoint (`https://<name>.openai.azure.com/` or `https://<name>.cognitiveservices.azure.com/`) and an API key.

Prefer no keys? Leave `FOUNDRY_API_KEY` empty, run `az login`, and give your account the *Cognitive Services OpenAI User* role on the resource.

## 2. Run it

Python 3.10 or newer.

```bat
python -m venv venv
venv\Scripts\pip install -r requirements.txt
copy .env.example .env
```

Fill in `FOUNDRY_ENDPOINT`, `FOUNDRY_API_KEY` and your three deployment names in `.env`, then:

```bat
start.bat
```

On PowerShell, use `.\start.ps1`. The dashboard opens at http://127.0.0.1:8000. The server log prints `Foundry: chat=… tts=… stt=… (ready)` when the configuration is picked up.

To run the pieces yourself:

```bat
venv\Scripts\python.exe server.py
venv\Scripts\python.exe simulator.py
```

## 3. Connect F1 25

In the game, open **Settings → Telemetry Settings**:

- UDP Telemetry: **On**
- UDP Broadcast Mode: **Off**
- UDP IP Address: `127.0.0.1` if F1 25 runs on the same PC, otherwise this PC's LAN address (console on the same network)
- UDP Port: `20777`
- UDP Send Rate: 20Hz or higher
- UDP Format: **2025** (the 2026 Season Pack format also works)

Enter a session on track; the menus only send event packets. Gran Turismo 7 telemetry is accepted as JSON on UDP port `33740`.

## Pit radio

Click **MIC** or press Space to start talking, press again to send. Ask about tires, the pit window, gaps or fuel. The first time, the browser asks for microphone access. Set `RADIO_LANGUAGE=nl` (or another ISO code) to talk to — and hear back from — your engineer in another language.

## Configuration

| Variable | Purpose |
| --- | --- |
| `FOUNDRY_ENDPOINT` | Foundry / Azure OpenAI resource endpoint |
| `FOUNDRY_API_KEY` | API key. Empty = Microsoft Entra ID sign-in |
| `FOUNDRY_API_VERSION` | Data-plane API version. Defaults to `2025-04-01-preview` |
| `FOUNDRY_CHAT_DEPLOYMENT` | Chat deployment for strategy and radio replies |
| `FOUNDRY_TTS_DEPLOYMENT` | Text-to-speech deployment |
| `FOUNDRY_STT_DEPLOYMENT` | Speech-to-text deployment |
| `FOUNDRY_REASONING_EFFORT` / `FOUNDRY_RADIO_REASONING_EFFORT` | Reasoning effort for gpt-5 family models. Ignored by models that don't support it |
| `FOUNDRY_TTS_VOICE` | Engineer voice. Defaults to `onyx` |
| `FOUNDRY_TTS_SPEED` | Speech speed. Defaults to `1.1` |
| `FOUNDRY_TTS_INSTRUCTIONS` | Speaking-style instructions for `gpt-4o-mini-tts` |
| `RADIO_LANGUAGE` | Radio language, ISO-639-1. Defaults to `en` |
| `F1_PORT` | F1 UDP port. Defaults to `20777` |
| `GT7_PORT` | GT7 UDP port. Defaults to `33740` |
| `ANALYSIS_INTERVAL` | Seconds between strategy updates. Defaults to `15` |

## Layout

| File | Role |
| --- | --- |
| `server.py` | FastAPI app, UDP listeners, dashboard, and radio API (`/api/radio`, `/api/radio/audio`) |
| `f1_parser.py` | F1 25 UDP parser (formats 2024, 2025, 2026) for all 22/24 cars |
| `engine_models.py` | Tire, fuel, weather, and opponent models |
| `foundry.py` | Microsoft Foundry client: chat, TTS and STT |
| `analyzer.py` | Strategy and radio prompts |
| `tts_bridge.py` | Engineer voice files |
| `simulator.py` | WebSocket telemetry source for local runs |
| `static/index.html` | Live dashboard |
| `tests/` | Parser and Foundry-wiring tests (`pip install -r requirements-dev.txt`, then `python -m pytest`) |

## Changes from upstream besides Foundry

- Parser rebuilt from the F1 25 spec: 57-byte participants, 46-byte car damage with tyre blisters, C6 compound, 24-car 2026 Season Pack format.
- Wheel order fixed: the game sends every per-wheel array as RL, RR, FL, FR.
- Track IDs corrected (e.g. 15 = Austin, 17 = Red Bull Ring, 26 = Zandvoort).
- Total laps, session type, safety car, pit window and rain chance are now read from the session packet.
- The weather model no longer crashes on live F1 data, so strategy calls run during real sessions.
- Opponent gaps are measured to the player instead of to each opponent's own car ahead.
