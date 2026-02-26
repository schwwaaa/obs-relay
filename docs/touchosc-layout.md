# TouchOSC Layout Guide for obs-relay

## OSC Address Map

### Scene Presets (send any value, e.g. 1.0 on press)
| OSC Address              | Action                        |
|--------------------------|-------------------------------|
| `/obs/scene/live`        | Switch to Live scene          |
| `/obs/scene/brb`         | Switch to BRB scene           |
| `/obs/scene/standby`     | Switch to Standby scene       |
| `/obs/scene/intermission`| Switch to Intermission scene  |
| `/obs/scene/end_card`    | Switch to End Card scene      |

### Playlist Control
| OSC Address                      | Action                        |
|----------------------------------|-------------------------------|
| `/obs/playlist/next`             | Next track                    |
| `/obs/playlist/prev`             | Previous track                |
| `/obs/playlist/activate/main`    | Activate "main" playlist      |
| `/obs/playlist/activate/intermission` | Activate intermission playlist |
| `/obs/playlist/seek`             | Seek to track number (int)    |

### Stream Control
| OSC Address         | Action         |
|---------------------|----------------|
| `/obs/stream/start` | Start stream   |
| `/obs/stream/stop`  | Stop stream    |

### Audio Control
| OSC Address               | Value    | Action                          |
|---------------------------|----------|---------------------------------|
| `/obs/volume/Mic`         | 0.0–1.0  | Set Mic volume (maps to -60–0dB)|
| `/obs/volume/Desktop`     | 0.0–1.0  | Set Desktop audio volume        |
| `/obs/mute/Mic`           | 0 or 1   | Mute/unmute Mic                 |

### State Feedback (received from obs-relay)
| OSC Address            | Value Type | Content               |
|------------------------|------------|-----------------------|
| `/obs/state/scene`     | string     | Current scene name    |
| `/obs/state/stream`    | int        | 1=streaming, 0=not    |
| `/obs/state/playlist`  | string     | Active playlist name  |
| `/obs/state/track`     | string     | Current track title   |

### Query
| OSC Address         | Action                         |
|---------------------|--------------------------------|
| `/obs/state/query`  | Request full state broadcast   |

---

## TouchOSC Setup

1. Open TouchOSC on your device
2. Go to **Connections → OSC**
3. Set:
   - **Host**: IP address of machine running obs-relay
   - **Port (send)**: `9000` (matches `osc.listen_port` in config.yaml)
   - **Port (receive)**: `9001` (matches `osc.reply_port`)
4. In obs-relay's `config.yaml`, set `osc.client_host` to your device's IP for direct feedback
   (or keep as `255.255.255.255` for broadcast)

## Recommended TouchOSC Layout

### Page 1: Scene Control
- 5 push buttons labeled: LIVE, BRB, STANDBY, INTERMISSION, END CARD
- Each sends to `/obs/scene/{preset_name}`

### Page 2: Playlist Control
- PREV / NEXT buttons
- Playlist selector (radio buttons or labels)
- Track display label (receives `/obs/state/track`)

### Page 3: Stream & Audio
- STREAM START / STOP toggle
- Volume faders for Mic and Desktop
- Mute buttons

### Page 4: Status Monitor
- Labels receiving:
  - Current Scene: `/obs/state/scene`
  - Stream: `/obs/state/stream`
  - Playlist: `/obs/state/playlist`
  - Track: `/obs/state/track`

---

## WebSocket Control (alternative to REST API)

Connect to `ws://HOST:8080/ws` and send JSON commands:

```json
{"cmd": "activate_preset", "params": {"name": "brb"}}
{"cmd": "playlist_next"}
{"cmd": "playlist_activate", "params": {"name": "main"}}
{"cmd": "switch_scene", "params": {"scene_name": "Live"}}
{"cmd": "stream_start"}
{"cmd": "get_status"}
```

Responses and broadcast events arrive as:
```json
{"event": "preset_activated", "data": {"preset": "brb", ...}}
{"event": "track_changed", "data": {"track": "Intermission Loop A"}}
{"event": "scene_switched", "data": {"scene": "BRB"}}
```
