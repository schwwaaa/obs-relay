"""
api/server.py — FastAPI REST API + WebSocket relay bridge.

v1.1 additions:
  - WebSocket auth (Bearer token on connect via ?token= query param)
  - Recording endpoints (start/stop/status/pause/resume)
  - Transition control (type + duration)
  - Preflight playlist validation endpoint
  - Auto-advance toggle endpoint
  - OBS scene_changed passthrough to all WS clients
  - /healthz endpoint for uptime monitoring (503 when OBS disconnected)
  - Media source status endpoint
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Header, Query, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from obs_relay.config import get_settings
from obs_relay.core import get_obs_client, OBSConnectionError
# Overlay manager imported lazily via set_managers to avoid circular imports

log = logging.getLogger(__name__)

_preset_manager = None
_playlist_manager = None
_osc_bridge = None
_overlay_manager = None


def set_managers(presets, playlists, osc, overlay=None):
    global _preset_manager, _playlist_manager, _osc_bridge, _overlay_manager
    _preset_manager = presets
    _playlist_manager = playlists
    _osc_bridge = osc
    _overlay_manager = overlay


# ──────────────────────────────────────────────────────────────────────────────
# WebSocket connection pool
# ──────────────────────────────────────────────────────────────────────────────

class WSConnectionPool:
    def __init__(self):
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.append(ws)
        log.info(f"WS client connected. Total: {len(self._connections)}")

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self._connections:
            self._connections.remove(ws)
        log.info(f"WS client disconnected. Total: {len(self._connections)}")

    async def broadcast(self, message: dict) -> None:
        if not self._connections:
            return
        data = json.dumps(message)
        dead = []
        for ws in self._connections:
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    def count(self) -> int:
        return len(self._connections)


ws_pool = WSConnectionPool()


# ──────────────────────────────────────────────────────────────────────────────
# App factory
# ──────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    log.info(f"obs-relay API starting on {settings.api.host}:{settings.api.port}")

    # Wire OBS scene-change passthrough to WS broadcast
    try:
        obs_client = get_obs_client()
        async def on_scene_changed(scene_name: str):
            await ws_pool.broadcast({"event": "scene_changed_external", "data": {"scene": scene_name}})
            if _osc_bridge:
                _osc_bridge.send_feedback("/obs/state/scene", scene_name)
        obs_client.on_scene_changed(on_scene_changed)
        log.info("OBS scene-change passthrough registered")
    except RuntimeError:
        pass  # OBS client not yet initialized — reconnect loop will handle it

    yield
    log.info("obs-relay API shutting down.")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="obs-relay",
        description="Remote OBS control relay — drop-zone-ops ecosystem",
        version="1.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.api.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── REST auth dependency ──────────────────────────────────────────

    async def verify_api_key(authorization: Optional[str] = Header(None)):
        if settings.api.api_key:
            if not authorization or not authorization.startswith("Bearer "):
                raise HTTPException(status_code=401, detail="Missing Bearer token")
            token = authorization.removeprefix("Bearer ").strip()
            if token != settings.api.api_key:
                raise HTTPException(status_code=403, detail="Invalid API key")

    auth = Depends(verify_api_key)

    def obs():
        try:
            return get_obs_client()
        except RuntimeError:
            raise HTTPException(status_code=503, detail="OBS client not initialized")

    # ─────────────────────────────────────────────────────────────────
    # Health
    # ─────────────────────────────────────────────────────────────────

    @app.get("/health", tags=["System"])
    async def health():
        client = obs()
        return {
            "status": "ok",
            "obs_connected": client.is_connected(),
            "ws_clients": ws_pool.count(),
            "osc_active": _osc_bridge.is_running() if _osc_bridge else False,
            "version": "1.1.0",
        }

    @app.get("/healthz", tags=["System"])
    async def healthz():
        """Machine-readable health check. Returns 503 when OBS is disconnected."""
        client = obs()
        connected = client.is_connected()
        if not connected:
            raise HTTPException(
                status_code=503,
                detail={"status": "degraded", "reason": "OBS not connected"}
            )
        return {"status": "ok"}

    # ─────────────────────────────────────────────────────────────────
    # OBS — Scenes
    # ─────────────────────────────────────────────────────────────────

    @app.get("/obs/version", tags=["OBS"], dependencies=[auth])
    async def obs_version():
        return await obs().get_version()

    @app.get("/obs/scenes", tags=["OBS"], dependencies=[auth])
    async def list_scenes():
        return await obs().get_scenes()

    @app.get("/obs/scene/current", tags=["OBS"], dependencies=[auth])
    async def current_scene():
        return {"scene": await obs().get_current_scene()}

    @app.post("/obs/scene/{scene_name}", tags=["OBS"], dependencies=[auth])
    async def switch_scene(scene_name: str):
        result = await obs().switch_scene(scene_name)
        await ws_pool.broadcast({"event": "scene_switched", "data": result})
        if _osc_bridge:
            _osc_bridge.send_feedback("/obs/state/scene", scene_name)
        return result

    # ─────────────────────────────────────────────────────────────────
    # OBS — Transitions  ← NEW
    # ─────────────────────────────────────────────────────────────────

    @app.get("/obs/transition", tags=["OBS"], dependencies=[auth])
    async def get_transition():
        """Get current transition name and duration."""
        return await obs().get_transition()

    class TransitionBody(BaseModel):
        name: Optional[str] = None
        duration_ms: Optional[int] = None

    @app.post("/obs/transition", tags=["OBS"], dependencies=[auth])
    async def set_transition(body: TransitionBody):
        """Set transition type and/or duration. Pass one or both fields."""
        results = {}
        if body.name:
            results["transition"] = await obs().set_transition(body.name)
        if body.duration_ms is not None:
            results["duration"] = await obs().set_transition_duration(body.duration_ms)
        return results or {"status": "nothing to set"}

    # ─────────────────────────────────────────────────────────────────
    # OBS — Studio mode
    # ─────────────────────────────────────────────────────────────────

    @app.post("/obs/studio/enable", tags=["OBS"], dependencies=[auth])
    async def studio_mode(enabled: bool = True):
        return await obs().enable_studio_mode(enabled)

    @app.post("/obs/studio/transition", tags=["OBS"], dependencies=[auth])
    async def studio_transition():
        return await obs().transition_to_program()

    # ─────────────────────────────────────────────────────────────────
    # OBS — Audio
    # ─────────────────────────────────────────────────────────────────

    class VolumeBody(BaseModel):
        volume_db: float

    @app.post("/obs/source/{source_name}/volume", tags=["OBS"], dependencies=[auth])
    async def set_volume(source_name: str, body: VolumeBody):
        return await obs().set_volume(source_name, body.volume_db)

    class MuteBody(BaseModel):
        muted: bool

    @app.post("/obs/source/{source_name}/mute", tags=["OBS"], dependencies=[auth])
    async def set_mute(source_name: str, body: MuteBody):
        return await obs().set_mute(source_name, body.muted)

    # ─────────────────────────────────────────────────────────────────
    # OBS — Media source status  ← NEW
    # ─────────────────────────────────────────────────────────────────

    @app.get("/obs/source/{source_name}/media", tags=["OBS"], dependencies=[auth])
    async def get_media_status(source_name: str):
        """Get playback state, cursor, and duration of a media source."""
        return await obs().get_media_status(source_name)

    # ─────────────────────────────────────────────────────────────────
    # OBS — Streaming
    # ─────────────────────────────────────────────────────────────────

    @app.get("/obs/stream/status", tags=["OBS"], dependencies=[auth])
    async def stream_status():
        return await obs().get_stream_status()

    @app.post("/obs/stream/start", tags=["OBS"], dependencies=[auth])
    async def start_stream():
        result = await obs().start_stream()
        await ws_pool.broadcast({"event": "stream_started", "data": result})
        if _osc_bridge:
            _osc_bridge.send_feedback("/obs/state/stream", 1)
        return result

    @app.post("/obs/stream/stop", tags=["OBS"], dependencies=[auth])
    async def stop_stream():
        result = await obs().stop_stream()
        await ws_pool.broadcast({"event": "stream_stopped", "data": result})
        if _osc_bridge:
            _osc_bridge.send_feedback("/obs/state/stream", 0)
        return result

    # ─────────────────────────────────────────────────────────────────
    # OBS — Recording  ← NEW (was dead code in v1.0)
    # ─────────────────────────────────────────────────────────────────

    @app.get("/obs/record/status", tags=["OBS"], dependencies=[auth])
    async def record_status():
        """Current recording state — active, paused, timecode, bytes."""
        return await obs().get_recording_status()

    @app.post("/obs/record/start", tags=["OBS"], dependencies=[auth])
    async def start_record():
        result = await obs().start_recording()
        await ws_pool.broadcast({"event": "recording_started", "data": result})
        return result

    @app.post("/obs/record/stop", tags=["OBS"], dependencies=[auth])
    async def stop_record():
        result = await obs().stop_recording()
        await ws_pool.broadcast({"event": "recording_stopped", "data": result})
        return result

    @app.post("/obs/record/pause", tags=["OBS"], dependencies=[auth])
    async def pause_record():
        result = await obs().pause_recording()
        await ws_pool.broadcast({"event": "recording_paused", "data": result})
        return result

    @app.post("/obs/record/resume", tags=["OBS"], dependencies=[auth])
    async def resume_record():
        result = await obs().resume_recording()
        await ws_pool.broadcast({"event": "recording_resumed", "data": result})
        return result

    # ─────────────────────────────────────────────────────────────────
    # Scene Presets
    # ─────────────────────────────────────────────────────────────────

    @app.get("/presets", tags=["Presets"], dependencies=[auth])
    async def list_presets():
        if not _preset_manager:
            raise HTTPException(status_code=503, detail="Preset manager not initialized")
        return _preset_manager.list_presets()

    @app.post("/presets/{preset_name}/activate", tags=["Presets"], dependencies=[auth])
    async def activate_preset(preset_name: str):
        if not _preset_manager:
            raise HTTPException(status_code=503, detail="Preset manager not initialized")
        try:
            result = await _preset_manager.activate(preset_name)
            await ws_pool.broadcast({"event": "preset_activated", "data": result})
            return result
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    # ─────────────────────────────────────────────────────────────────
    # Playlists
    # ─────────────────────────────────────────────────────────────────

    def _require_playlists():
        if not _playlist_manager:
            raise HTTPException(status_code=503, detail="Playlist manager not initialized")
        return _playlist_manager

    @app.get("/playlists", tags=["Playlists"], dependencies=[auth])
    async def list_playlists():
        mgr = _require_playlists()
        pls = mgr.get_playlists()
        return {
            name: {
                "name": pl.name,
                "item_count": len(pl.items),
                "loop": pl.loop,
                "shuffle": pl.shuffle,
                "items": [
                    {"path": i.path, "title": i.title, "duration": i.duration,
                     "start_time": i.start_time, "stop_time": i.stop_time}
                    for i in pl.items
                ],
            }
            for name, pl in pls.items()
        }

    @app.get("/playlists/status", tags=["Playlists"], dependencies=[auth])
    async def playlist_status():
        return _require_playlists().get_status()

    @app.get("/playlists/validate", tags=["Playlists"], dependencies=[auth])
    async def validate_all_playlists():
        """Preflight check — verify all files in all playlists exist on disk."""
        return _require_playlists().validate_all()

    @app.get("/playlists/{playlist_name}/validate", tags=["Playlists"], dependencies=[auth])
    async def validate_playlist(playlist_name: str):
        """Preflight check for a specific playlist."""
        return _require_playlists().validate_playlist(playlist_name)

    class AutoAdvanceBody(BaseModel):
        enabled: bool

    @app.post("/playlists/auto-advance", tags=["Playlists"], dependencies=[auth])
    async def set_auto_advance(body: AutoAdvanceBody):
        """Enable or disable automatic track advancement when a video ends."""
        _require_playlists().set_auto_advance(body.enabled)
        return {"auto_advance": body.enabled, "status": "ok"}

    class CreatePlaylistBody(BaseModel):
        name: str
        items: list[str]
        loop: bool = True
        save: bool = False

    @app.post("/playlists/create", tags=["Playlists"], dependencies=[auth])
    async def create_playlist(body: CreatePlaylistBody):
        mgr = _require_playlists()
        pl = mgr.create_playlist(body.name, body.items, body.loop)
        if body.save:
            mgr.save_playlist(body.name)
        return {"name": pl.name, "item_count": len(pl.items), "status": "created"}

    @app.post("/playlists/upload", tags=["Playlists"], dependencies=[auth])
    async def upload_playlist(file: UploadFile = File(...)):
        mgr = _require_playlists()
        dest = mgr.playlist_dir / file.filename
        content = await file.read()
        dest.write_bytes(content)
        pl = mgr.load_file(dest)
        return {"name": pl.name, "item_count": len(pl.items), "status": "uploaded"}

    @app.delete("/playlists/{playlist_name}", tags=["Playlists"], dependencies=[auth])
    async def delete_playlist(playlist_name: str):
        mgr = _require_playlists()
        if not mgr.delete_playlist(playlist_name):
            raise HTTPException(status_code=404, detail=f"Playlist '{playlist_name}' not found")
        return {"status": "deleted", "name": playlist_name}

    @app.post("/playlists/{playlist_name}/activate", tags=["Playlists"], dependencies=[auth])
    async def activate_playlist(playlist_name: str, position: int = 0):
        mgr = _require_playlists()
        item = await mgr.activate(playlist_name, position)
        if item is None:
            raise HTTPException(status_code=404, detail=f"Playlist '{playlist_name}' not found")
        result = {"playlist": playlist_name, "position": position, "track": item.title}
        await ws_pool.broadcast({"event": "playlist_activated", "data": result})
        return result

    @app.post("/playlists/next", tags=["Playlists"], dependencies=[auth])
    async def next_track():
        mgr = _require_playlists()
        item = await mgr.next()
        result = {"track": item.title if item else None, "status": "advanced" if item else "end"}
        await ws_pool.broadcast({"event": "track_changed", "data": result})
        return result

    @app.post("/playlists/prev", tags=["Playlists"], dependencies=[auth])
    async def prev_track():
        mgr = _require_playlists()
        item = await mgr.previous()
        return {"track": item.title if item else None}

    @app.post("/playlists/seek/{position}", tags=["Playlists"], dependencies=[auth])
    async def seek_track(position: int):
        mgr = _require_playlists()
        item = await mgr.seek(position)
        return {"position": position, "track": item.title if item else None}

    # ─────────────────────────────────────────────────────────────────
    # Text Overlays  ← NEW
    # ─────────────────────────────────────────────────────────────────

    def _require_overlay():
        if not _overlay_manager:
            raise HTTPException(status_code=503, detail="Overlay manager not initialized")
        return _overlay_manager

    @app.get("/overlay/status", tags=["Overlay"], dependencies=[auth])
    async def overlay_status():
        """Current overlay state — active, text, timer remaining, config."""
        return _require_overlay().get_status()

    @app.get("/overlay/config", tags=["Overlay"], dependencies=[auth])
    async def overlay_config():
        """Current overlay configuration."""
        return _require_overlay().get_config()

    class OverlayConfigBody(BaseModel):
        enabled: Optional[bool] = None
        source_name: Optional[str] = None
        scene_name: Optional[str] = None
        hold_sec: Optional[float] = None
        delay_sec: Optional[float] = None
        fade_in_ms: Optional[int] = None
        fade_out_ms: Optional[int] = None
        prefix: Optional[str] = None
        suffix: Optional[str] = None
        mode: Optional[str] = None
        auto_trigger: Optional[bool] = None
        next_up_prefix: Optional[str] = None

    @app.post("/overlay/config", tags=["Overlay"], dependencies=[auth])
    async def set_overlay_config(body: OverlayConfigBody):
        """Update overlay configuration at runtime. Only pass fields you want to change."""
        mgr = _require_overlay()
        updates = {k: v for k, v in body.model_dump().items() if v is not None}
        return mgr.update_config(**updates)

    class OverlayTriggerBody(BaseModel):
        text: str
        hold_sec: Optional[float] = None
        delay_sec: Optional[float] = None

    @app.post("/overlay/trigger", tags=["Overlay"], dependencies=[auth])
    async def overlay_trigger(body: OverlayTriggerBody):
        """
        Manually trigger an overlay with any text for a configurable duration.
        Useful for pop-up announcements, ad banners, or 'Up Next' cards.
        Overrides and cancels any active overlay immediately.
        """
        mgr = _require_overlay()
        result = await mgr.trigger(
            text=body.text,
            hold_sec=body.hold_sec,
            delay_sec=body.delay_sec,
        )
        await ws_pool.broadcast({"event": "overlay_triggered", "data": result})
        return result

    @app.post("/overlay/hide", tags=["Overlay"], dependencies=[auth])
    async def overlay_hide():
        """Immediately cancel and hide the active overlay."""
        mgr = _require_overlay()
        result = await mgr.hide()
        await ws_pool.broadcast({"event": "overlay_hidden", "data": result})
        return result

    @app.post("/overlay/trigger-current", tags=["Overlay"], dependencies=[auth])
    async def overlay_trigger_current():
        """
        Re-trigger the overlay for the currently playing track.
        Uses the title from the M3U '#EXTINF' tag and applies configured prefix/suffix.
        Safe to call mid-playlist — does not lose position.
        """
        if not _playlist_manager:
            raise HTTPException(status_code=503, detail="Playlist manager not initialized")
        mgr = _require_overlay()
        status = _playlist_manager.get_status()
        item = _playlist_manager.current_item
        if not item:
            raise HTTPException(status_code=404, detail="No active track")
        meta = item.metadata or {}
        custom = meta.get("overlay_text", "")
        hold   = float(meta.get("overlay_hold",  mgr.config.hold_sec))
        delay  = float(meta.get("overlay_delay", 0))  # no delay for manual re-trigger
        text = custom or (mgr.config.prefix + item.title + mgr.config.suffix)
        result = await mgr.trigger(text=text, hold_sec=hold, delay_sec=delay)
        await ws_pool.broadcast({"event": "overlay_triggered", "data": result})
        return result

    # ─────────────────────────────────────────────────────────────────
    # OSC Status
    # ─────────────────────────────────────────────────────────────────

    @app.get("/osc/status", tags=["OSC"], dependencies=[auth])
    async def osc_status():
        settings = get_settings()
        return {
            "enabled": settings.osc.enabled,
            "running": _osc_bridge.is_running() if _osc_bridge else False,
            "listen_port": settings.osc.listen_port,
            "reply_port": settings.osc.reply_port,
        }

    # ─────────────────────────────────────────────────────────────────
    # WebSocket relay — with auth
    # ─────────────────────────────────────────────────────────────────

    @app.websocket("/ws")
    async def websocket_endpoint(
        websocket: WebSocket,
        token: Optional[str] = Query(None),
    ):
        settings = get_settings()

        # Auth check: if API key is set, require it as ?token= query param
        if settings.api.api_key:
            if not token or token != settings.api.api_key:
                await websocket.close(code=4001, reason="Unauthorized")
                return

        await ws_pool.connect(websocket)
        # Send initial state on connect
        try:
            await websocket.send_text(json.dumps({
                "event": "connected",
                "data": {
                    "obs_connected": get_obs_client().is_connected(),
                    "version": "1.1.0",
                }
            }))
        except Exception:
            pass

        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                    response = await _handle_ws_command(msg)
                    await websocket.send_text(json.dumps(response))
                except json.JSONDecodeError:
                    await websocket.send_text(json.dumps({"error": "Invalid JSON"}))
                except Exception as e:
                    await websocket.send_text(json.dumps({"error": str(e)}))
        except WebSocketDisconnect:
            ws_pool.disconnect(websocket)

    async def _handle_ws_command(msg: dict) -> dict:
        cmd = msg.get("cmd", "")
        params = msg.get("params", {})

        def client():
            try:
                return get_obs_client()
            except RuntimeError:
                raise ValueError("OBS not connected")

        match cmd:
            case "switch_scene":
                return await client().switch_scene(params["scene_name"])
            case "activate_preset":
                if _preset_manager:
                    return await _preset_manager.activate(params["name"])
                return {"error": "Preset manager not available"}
            case "playlist_next":
                if _playlist_manager:
                    item = await _playlist_manager.next()
                    return {"track": item.title if item else None}
                return {"error": "Playlist manager not available"}
            case "playlist_prev":
                if _playlist_manager:
                    item = await _playlist_manager.previous()
                    return {"track": item.title if item else None}
                return {"error": "Playlist manager not available"}
            case "playlist_activate":
                if _playlist_manager:
                    item = await _playlist_manager.activate(params["name"])
                    return {"playlist": params["name"], "track": item.title if item else None}
                return {"error": "Playlist manager not available"}
            case "playlist_seek":
                if _playlist_manager:
                    item = await _playlist_manager.seek(int(params.get("position", 0)))
                    return {"track": item.title if item else None}
                return {"error": "Playlist manager not available"}
            case "stream_start":
                return await client().start_stream()
            case "stream_stop":
                return await client().stop_stream()
            case "record_start":
                return await client().start_recording()
            case "record_stop":
                return await client().stop_recording()
            case "set_transition":
                results = {}
                if "name" in params:
                    results["transition"] = await client().set_transition(params["name"])
                if "duration_ms" in params:
                    results["duration"] = await client().set_transition_duration(params["duration_ms"])
                return results
            case "overlay_trigger":
                if _overlay_manager:
                    r = await _overlay_manager.trigger(
                        text=params.get("text", ""),
                        hold_sec=params.get("hold_sec"),
                        delay_sec=params.get("delay_sec"),
                    )
                    return r
                return {"error": "Overlay manager not available"}
            case "overlay_hide":
                if _overlay_manager:
                    return await _overlay_manager.hide()
                return {"error": "Overlay manager not available"}
            case "overlay_trigger_current":
                if _overlay_manager and _playlist_manager:
                    item = _playlist_manager.current_item
                    if not item:
                        return {"error": "No active track"}
                    meta = item.metadata or {}
                    text = meta.get("overlay_text") or (_overlay_manager.config.prefix + item.title + _overlay_manager.config.suffix)
                    return await _overlay_manager.trigger(text=text)
                return {"error": "Overlay or playlist manager not available"}
            case "get_status":
                c = client()
                return {
                    "obs_connected": c.is_connected(),
                    "scene": await c.get_current_scene() if c.is_connected() else None,
                    "playlist": _playlist_manager.get_status() if _playlist_manager else None,
                    "overlay": _overlay_manager.get_status() if _overlay_manager else None,
                }
            case _:
                return {"error": f"Unknown command: {cmd}"}

    return app
