# Changelog

All notable changes to obs-relay are documented here.

---

## [1.1.0] — 2026-02-24

### Added
- **Auto-advance playlists** — OBS `MediaInputPlaybackEnded` event now automatically advances to the next track when a video finishes. No more manual `/playlists/next` calls for unattended playback.
- **State persistence** — active playlist and position saved to `playlist_state.json` on every track change. Server restores position automatically after a restart or crash.
- **Preflight validation** — `GET /playlists/validate` and `python run.py validate-playlists` check all files exist before going live.
- **Recording endpoints** — `POST /obs/record/start`, `/stop`, `/pause`, `/resume` and `GET /obs/record/status` (were previously dead code).
- **Transition control** — `GET/POST /obs/transition` to set type (Fade, Cut, Stinger, etc.) and duration in milliseconds from the API.
- **WebSocket auth** — `?token=` query param on `/ws` endpoint. Required when `api.api_key` is set.
- **OBS scene-change passthrough** — `CurrentProgramSceneChanged` event forwarded to all WebSocket clients and OSC as `scene_changed_external`. Keeps control panels in sync when scene is changed directly in OBS UI.
- **Media source status** — `GET /obs/source/{name}/media` returns playback state, cursor, and duration.
- **`/healthz` endpoint** — returns HTTP 503 when OBS is disconnected. Designed for uptime monitoring tools.
- **Auto-advance toggle** — `POST /playlists/auto-advance` to enable/disable at runtime.
- **EXTVLCOPT trim support** — M3U parser now reads `#EXTVLCOPT:start-time` and `stop-time` directives.
- **`validate-playlists` CLI command** — preflight check from the command line.
- **`playlist_prev` and `playlist_seek` WebSocket commands**.
- `requirements.txt`, `LICENSE`, `.gitignore`, `config.yaml.example`, `CHANGELOG.md`.

### Fixed
- `set_media_source` now sets `looping: false` by default — obs-relay handles advancement via the event bus instead of OBS internal looping.
- WebSocket endpoint sends initial state on connect.
- Recording `stop_recording` now returns `output_path` from OBS.

### Changed
- Version bumped to `1.1.0`.
- `obs_relay/__init__.py` version string updated.

---

## [1.0.0] — 2026-02-23

### Initial release
- FastAPI REST API with full OBS control surface
- WebSocket relay with command routing and broadcast events
- M3U playlist parser and manager
- Scene preset system (live, brb, standby, intermission, end_card)
- TouchOSC / OSC UDP bridge
- Auto-reconnect to OBS on disconnect
- Pydantic settings with YAML + env var override
- PyInstaller standalone build support
- Typer CLI (start, init-config, check, list-presets, build-standalone)
