# obs-relay

> Remote OBS Studio control API explorer
> REST API · WebSocket relay · TouchOSC bridge · M3U playlist scheduler

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://python.org)
[![OBS WebSocket 5.x](https://img.shields.io/badge/OBS%20WebSocket-5.x-purple.svg)](https://github.com/obsproject/obs-websocket)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

---

## What it does

obs-relay sits between your control surfaces and OBS Studio. It exposes a REST API and WebSocket endpoint that any device on your network can call to control OBS — switch scenes, manage video playlists, start/stop streams, control audio, and more.

Think of it as a **Pluto.tv-style channel controller**: define playlists of video files, name each broadcast state (Live, BRB, Standby, Intermission), and switch between them from a phone, tablet, custom controller, or automation script.

```
                   ┌─────────────────────┐
  REST clients ───▶│                     │
  WebSocket   ───▶ │    obs-relay        │──▶ OBS Studio (WebSocket 5.x)
  TouchOSC    ───▶ │  (FastAPI + asyncio)│       ↑ events flow back too
  (UDP OSC)        └─────────────────────┘
                           │
                    Playlist Manager
                   (M3U auto-scheduler)
```

---

## Features

| Feature | Details |
|---|---|
| **Scene control** | Switch scenes, presets, studio mode, transitions |
| **Playlist scheduler** | M3U playlists with **auto-advance** when a video ends |
| **Hot-swap channels** | Switch between playlists mid-show (Pluto.tv style) |
| **WebSocket relay** | Real-time bidirectional control + push events |
| **TouchOSC / OSC** | UDP bridge — build a custom hardware controller |
| **Recording** | Start, stop, pause, resume, status |
| **Audio** | Volume (dB) and mute per source |
| **Transition control** | Set type (Fade/Cut/etc) and duration from API |
| **Preflight validation** | Check all playlist files exist before going live |
| **State persistence** | Survives restarts — resumes playlist position |
| **Auth** | Optional Bearer token for REST + `?token=` for WebSocket |
| **Standalone build** | Single executable via PyInstaller |

---

## Requirements

- **Python 3.10 or higher**
- **OBS Studio 28+** (WebSocket 5.x is built in — no plugin needed)
- macOS, Windows, or Linux

---

## Installation

### 1. Clone the repo

```bash
git clone https://github.com/YOUR_ORG/obs-relay.git
cd obs-relay
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

Or manually:

```bash
pip install fastapi "uvicorn[standard]" websockets obs-websocket-py python-osc \
            pydantic pydantic-settings aiofiles httpx rich typer \
            python-multipart pyyaml apscheduler
```

> **Recommended — use a virtual environment:**
> ```bash
> python -m venv .venv
> source .venv/bin/activate      # macOS/Linux
> .venv\Scripts\activate         # Windows
> pip install -r requirements.txt
> ```

### 3. (Optional) Install the package for the `obs-relay` command

```bash
pip install -e .
```

> **If `obs-relay` command is not found on macOS/zsh** — use `python run.py` instead.
> It always works without any PATH setup. Or add pip's script dir to your shell:
> ```bash
> echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc
> ```

---

## OBS Setup

1. Open OBS Studio
2. Go to **Tools → WebSocket Server Settings**
3. **Enable** the WebSocket server
4. Set port: **4455**
5. Set a password (recommended)
6. Click OK

> **Important for playlists:** Create a **Media Source** input in your scene named
> `MediaSource` (or whatever you set in `config.yaml` under `playlist.source_name`).
> obs-relay controls playlist playback by swapping files into this source.

---

## Quick Start

### 1. Generate your config

```bash
python run.py init-config
```

Edit `config.yaml` — at minimum set your OBS password:

```yaml
obs:
  password: "your_obs_password_here"
```

### 2. Test the connection

```bash
python run.py check --password your_obs_password_here
```

You should see your OBS version, scene list, and current transition.

### 3. Start the relay

```bash
python run.py start
```

Or with inline flags (no config file needed):

```bash
python run.py start --obs-password mypassword --port 8080
```

### 4. Verify

```bash
curl http://localhost:8080/health
```

```json
{"status": "ok", "obs_connected": true, "ws_clients": 0, "osc_active": true, "version": "1.1.0"}
```

Interactive API docs: **http://localhost:8080/docs**

---

## Playlists — How They Work

A playlist is a `.m3u` file containing a list of video paths. When you activate a playlist, obs-relay loads the **first file** into your OBS Media Source. When that video finishes, OBS fires a `MediaInputPlaybackEnded` event — obs-relay catches it and automatically loads the next file. No polling, no timers.

Drop `.m3u` files into the `playlists/` folder. They load automatically on startup.

### M3U format

```m3u
#EXTM3U

#EXTINF:15,Intro Bumper
/absolute/path/to/intro.mp4

#EXTINF:3600,Main Content Block
/absolute/path/to/episode-01.mp4

#EXTINF:-1,Outro (unknown duration)
/absolute/path/to/outro.mp4
```

> **Always use absolute paths.** OBS resolves paths from its own working directory.

### Trim in/out points (optional)

```m3u
#EXTINF:30,Bumper (trimmed from a longer file)
#EXTVLCOPT:start-time=120.0
#EXTVLCOPT:stop-time=150.0
/media/long-video.mp4
```

### Before going live — preflight check

```bash
python run.py validate-playlists
```

```
✓ main:         6/6 files OK
✗ friday-show:  1 missing
    → /media/episode-03.mp4
```

---

## Common Commands

```bash
# Check what scenes OBS has
curl http://localhost:8080/obs/scenes

# Switch to a scene immediately
curl -X POST http://localhost:8080/obs/scene/Live

# Activate a preset (scene switch + side effects)
curl -X POST http://localhost:8080/presets/brb/activate

# Load and start a playlist
curl -X POST http://localhost:8080/playlists/main/activate

# Advance to next track manually
curl -X POST http://localhost:8080/playlists/next

# Set transition to a 500ms fade
curl -X POST http://localhost:8080/obs/transition \
  -H "Content-Type: application/json" \
  -d '{"name": "Fade", "duration_ms": 500}'

# Start/stop streaming
curl -X POST http://localhost:8080/obs/stream/start
curl -X POST http://localhost:8080/obs/stream/stop

# Start/stop recording
curl -X POST http://localhost:8080/obs/record/start
curl -X POST http://localhost:8080/obs/record/stop

# Mute/unmute a source
curl -X POST http://localhost:8080/obs/source/Mic/mute \
  -H "Content-Type: application/json" \
  -d '{"muted": true}'

# Check recording status
curl http://localhost:8080/obs/record/status

# Validate all playlists
curl http://localhost:8080/playlists/validate
```

---

## WebSocket

Connect to `ws://localhost:8080/ws` (add `?token=YOUR_KEY` if auth is enabled).

### Send commands

```json
{"cmd": "activate_preset",   "params": {"name": "brb"}}
{"cmd": "switch_scene",      "params": {"scene_name": "Live"}}
{"cmd": "playlist_activate", "params": {"name": "main"}}
{"cmd": "playlist_next"}
{"cmd": "playlist_prev"}
{"cmd": "playlist_seek",     "params": {"position": 3}}
{"cmd": "stream_start"}
{"cmd": "stream_stop"}
{"cmd": "record_start"}
{"cmd": "record_stop"}
{"cmd": "set_transition",    "params": {"name": "Fade", "duration_ms": 300}}
{"cmd": "get_status"}
```

### Events pushed to all clients

```json
{"event": "scene_switched",         "data": {"scene": "BRB"}}
{"event": "scene_changed_external", "data": {"scene": "Live"}}
{"event": "preset_activated",       "data": {"preset": "brb", "actions": [...]}}
{"event": "playlist_activated",     "data": {"playlist": "main", "track": "Intro"}}
{"event": "track_changed",          "data": {"track": "Content Block 2", "status": "advanced"}}
{"event": "stream_started",         "data": {"status": "streaming_started"}}
{"event": "recording_started",      "data": {"status": "recording_started"}}
```

`scene_changed_external` fires whenever the scene is changed from the OBS UI or another client — keeps all connected panels in sync.

---

## Scene Presets

Presets are named broadcast states. Built-in defaults:

| Preset | OBS Scene | Side effects |
|---|---|---|
| `live` | Live | — |
| `brb` | BRB | Auto-mutes Mic |
| `standby` | Standby | — |
| `intermission` | Intermission | Activates intermission playlist |
| `end_card` | EndCard | — |

Configure scene names in `config.yaml` to match your OBS setup. Activate:

```bash
curl -X POST http://localhost:8080/presets/live/activate
```

---

## TouchOSC / OSC

obs-relay listens on UDP port 9000 and sends feedback on port 9001.

**TouchOSC device setup:**
1. TouchOSC → Connections → OSC
2. Host: your server's IP
3. Send port: `9000` / Receive port: `9001`
4. In `config.yaml`: set `osc.client_host` to your device's IP for direct feedback

See **[docs/touchosc-layout.md](docs/touchosc-layout.md)** for the full OSC address map.

---

## Authentication

```yaml
# config.yaml
api:
  api_key: "your-secret-token"
```

```bash
# REST requests
curl -H "Authorization: Bearer your-secret-token" http://localhost:8080/obs/scenes

# WebSocket
wscat -c "ws://localhost:8080/ws?token=your-secret-token"
```

Leave `api_key` blank for open access (fine for LAN-only use).

---

## Configuration Reference

```yaml
obs:
  host: localhost
  port: 4455
  password: ""
  reconnect_interval: 5.0
  max_reconnect_attempts: 0     # 0 = infinite

api:
  host: "0.0.0.0"
  port: 8080
  api_key: ""
  cors_origins: ["*"]
  log_level: info

osc:
  enabled: true
  listen_host: "0.0.0.0"
  listen_port: 9000
  reply_port: 9001
  client_host: "255.255.255.255"  # or specific device IP

playlist:
  directory: playlists
  default_playlist: ""            # e.g. "main" to auto-load
  loop: true
  source_name: MediaSource        # must match your OBS source name
```

All values can be set as environment variables: `OBS_PASSWORD=x API_PORT=8080 python run.py start`

---

## CLI Reference

```
python run.py start                  Start the server
python run.py init-config            Create config.yaml
python run.py check                  Test OBS connection
python run.py list-presets           Show scene presets
python run.py validate-playlists     Check all playlist files exist
python run.py build-standalone       Build standalone exe (needs pyinstaller)
```

---

## Troubleshooting

**`obs-relay: command not found`**  
Use `python run.py start` — it always works. See [Installation](#installation) for the PATH fix.

**OBS keeps reconnecting**  
- WebSocket must be enabled in OBS: Tools → WebSocket Server Settings  
- Check the password is exact (case-sensitive)  
- Run `python run.py check --password yourpassword` to isolate the issue  

**Playlist not auto-advancing**  
- The OBS Media Source must be named exactly as `playlist.source_name` (default: `MediaSource`)  
- Confirm with `GET /playlists/status` → `auto_advance` should be `true`  
- Check `GET /obs/source/MediaSource/media` to see if OBS is playing the file  

**Videos not loading / black screen**  
- Use **absolute paths** in `.m3u` files  
- Run `python run.py validate-playlists` to find missing files  
- Confirm the file format is supported by OBS (mp4, mov, mkv, etc.)  

**WebSocket drops immediately with auth enabled**  
- Connect as: `ws://host:8080/ws?token=YOUR_API_KEY`  

**TouchOSC not receiving feedback**  
- Set `osc.client_host` to your specific device IP (not broadcast)  
- Send `/obs/state/query` with value `1.0` to trigger a state broadcast  

---

## Roadmap

- [ ] Scheduled cue queue / show rundown
- [ ] HTTPS/TLS for internet-facing deployments  
- [ ] Web control panel UI  
- [ ] Multi-OBS support (backup failover)  
- [ ] Log rotation to file  

---

## Project Structure

```
obs-relay/
├── run.py                   Direct launcher (no install needed)
├── config.yaml              Default configuration
├── requirements.txt         Dependencies
├── pyproject.toml           Package metadata
├── playlists/               Drop .m3u files here
├── docs/
│   ├── touchosc-layout.md   OSC address map + TouchOSC guide
│   └── api-reference.html   Interactive API reference
├── obs_relay/
│   ├── main.py              CLI + app assembly
│   ├── config/              Settings (pydantic + YAML + env)
│   ├── core/                OBS WebSocket client
│   ├── api/                 FastAPI REST + WebSocket
│   ├── osc/                 TouchOSC / OSC bridge
│   ├── playlist/            M3U parser + auto-advance
│   └── scenes/              Scene presets
├── scripts/
│   └── build.py             PyInstaller helper
└── tests/
    └── test_core.py
```

---

## Contributing

1. Fork the repo  
2. Create a branch: `git checkout -b feature/my-feature`  
3. Run tests: `pytest tests/ -v`  
4. Open a pull request  

---

## License

MIT — see [LICENSE](LICENSE).

Built on [obs-websocket-py](https://github.com/obsproject/obs-websocket) and [FastAPI](https://fastapi.tiangolo.com/).  
Part of the **drop-zone-ops** broadcast toolchain.
