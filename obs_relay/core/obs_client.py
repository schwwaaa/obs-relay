"""
core/obs_client.py — Async OBS WebSocket 5.x client with reconnect & event bus.

Key additions in v1.1:
  - on_media_ended()    → subscribe to MediaInputPlaybackEnded (drives auto-advance)
  - on_scene_changed()  → subscribe to CurrentProgramSceneChanged (OBS UI passthrough)
  - set_transition()    → change transition type
  - set_transition_duration() → change transition duration
  - get_recording_status() / pause_recording() / resume_recording()
  - get_media_status()  → cursor + duration for a media source
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Callable, Coroutine, Optional

log = logging.getLogger(__name__)

try:
    from obswebsocket import obsws, requests as obs_requests, events as obs_events  # type: ignore
    OBS_LIB = "obswebsocket"
except ImportError:
    OBS_LIB = None

EventCallback = Callable[[Any], Coroutine[Any, Any, None]]


class OBSConnectionError(Exception):
    pass


class OBSClient:
    def __init__(
        self,
        host: str = "localhost",
        port: int = 4455,
        password: str = "",
        reconnect_interval: float = 5.0,
        max_reconnect_attempts: int = 0,
    ):
        self.host = host
        self.port = port
        self.password = password
        self.reconnect_interval = reconnect_interval
        self.max_reconnect_attempts = max_reconnect_attempts

        self._ws: Optional[Any] = None
        self._connected = False
        self._reconnecting = False
        self._reconnect_task: Optional[asyncio.Task] = None
        self._listeners: dict[str, list[EventCallback]] = defaultdict(list)
        self._connection_listeners: list[EventCallback] = []
        self._disconnect_listeners: list[EventCallback] = []
        self._media_ended_listeners: list[Callable] = []
        self._scene_changed_listeners: list[Callable] = []

    # ── Connection ────────────────────────────────────────────────────

    async def connect(self) -> bool:
        if OBS_LIB is None:
            raise OBSConnectionError("obs-websocket-py not installed.")
        try:
            self._ws = obsws(self.host, self.port, self.password)
            self._ws.register(self._on_disconnect, obs_events.Exiting)
            self._ws.register(self._on_media_ended, obs_events.MediaInputPlaybackEnded)
            self._ws.register(self._on_scene_changed, obs_events.CurrentProgramSceneChanged)
            self._ws.connect()
            self._connected = True
            log.info(f"Connected to OBS at {self.host}:{self.port}")
            await self._emit_connection_event()
            return True
        except Exception as e:
            log.warning(f"OBS connection failed: {e}")
            self._connected = False
            return False

    async def disconnect(self) -> None:
        if self._reconnect_task:
            self._reconnect_task.cancel()
        if self._ws and self._connected:
            try:
                self._ws.disconnect()
            except Exception:
                pass
        self._connected = False

    async def ensure_connected(self) -> bool:
        if not self._connected:
            return await self.connect()
        return True

    def is_connected(self) -> bool:
        return self._connected

    async def start_reconnect_loop(self) -> None:
        if self._reconnecting:
            return
        self._reconnecting = True
        attempts = 0
        while True:
            if self.max_reconnect_attempts and attempts >= self.max_reconnect_attempts:
                log.error("Max OBS reconnect attempts reached.")
                break
            log.info(f"Reconnect attempt {attempts + 1}...")
            if await self.connect():
                break
            attempts += 1
            await asyncio.sleep(self.reconnect_interval)
        self._reconnecting = False

    def _on_disconnect(self, _event: Any = None) -> None:
        if self._connected:
            self._connected = False
            log.warning("OBS disconnected. Scheduling reconnect...")
            loop = asyncio.get_event_loop()
            self._reconnect_task = loop.create_task(self.start_reconnect_loop())

    # ── OBS event handlers ────────────────────────────────────────────

    def _on_media_ended(self, event: Any) -> None:
        """Fired by OBS when a media source finishes playing."""
        try:
            source_name = event.datain.get("inputName", "")
            log.debug(f"MediaInputPlaybackEnded: {source_name}")
            loop = asyncio.get_event_loop()
            for cb in self._media_ended_listeners:
                loop.create_task(cb(source_name))
        except Exception as e:
            log.error(f"_on_media_ended error: {e}")

    def _on_scene_changed(self, event: Any) -> None:
        """Fired when program scene changes from ANY source (OBS UI, hotkeys, other clients)."""
        try:
            scene_name = event.datain.get("sceneName", "")
            log.debug(f"CurrentProgramSceneChanged: {scene_name}")
            loop = asyncio.get_event_loop()
            for cb in self._scene_changed_listeners:
                loop.create_task(cb(scene_name))
        except Exception as e:
            log.error(f"_on_scene_changed error: {e}")

    # ── Event subscriptions ───────────────────────────────────────────

    def on_media_ended(self, callback: Callable) -> None:
        """
        Subscribe to media playback ending. Callback receives source_name: str.
        This is what drives playlist auto-advance.

        Example:
            async def handle(source_name: str):
                if source_name == "MediaSource":
                    await playlist_manager.next()
            client.on_media_ended(handle)
        """
        self._media_ended_listeners.append(callback)

    def on_scene_changed(self, callback: Callable) -> None:
        """
        Subscribe to program scene changes from any source.
        Callback receives scene_name: str.
        Use this to keep OSC/WS clients in sync when OBS UI is used directly.
        """
        self._scene_changed_listeners.append(callback)

    def on(self, event_type: str, callback: EventCallback) -> None:
        self._listeners[event_type].append(callback)

    def on_connect(self, callback: EventCallback) -> None:
        self._connection_listeners.append(callback)

    async def _emit_connection_event(self) -> None:
        for cb in self._connection_listeners:
            try:
                await cb(None)
            except Exception as e:
                log.error(f"Connection listener error: {e}")

    # ── Core request helper ───────────────────────────────────────────

    def _call(self, request: Any) -> Any:
        if not self._connected or not self._ws:
            raise OBSConnectionError("Not connected to OBS")
        return self._ws.call(request)

    async def call_async(self, request: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._call, request)

    # ── Scenes ───────────────────────────────────────────────────────

    async def switch_scene(self, scene_name: str) -> dict:
        await self.call_async(obs_requests.SetCurrentProgramScene(sceneName=scene_name))
        log.info(f"Switched to scene: {scene_name}")
        return {"scene": scene_name, "status": "ok"}

    async def get_scenes(self) -> list[dict]:
        result = await self.call_async(obs_requests.GetSceneList())
        return [{"name": s["sceneName"], "index": s["sceneIndex"]} for s in result.datain.get("scenes", [])]

    async def get_current_scene(self) -> str:
        result = await self.call_async(obs_requests.GetCurrentProgramScene())
        return result.datain.get("currentProgramSceneName", "")

    # ── Transitions ───────────────────────────────────────────────────

    async def set_transition(self, transition_name: str) -> dict:
        """Set active transition type (e.g. 'Fade', 'Cut', 'Stinger')."""
        await self.call_async(obs_requests.SetCurrentSceneTransition(transitionName=transition_name))
        return {"transition": transition_name, "status": "ok"}

    async def set_transition_duration(self, duration_ms: int) -> dict:
        """Set transition duration in milliseconds."""
        await self.call_async(obs_requests.SetCurrentSceneTransitionDuration(transitionDuration=duration_ms))
        return {"duration_ms": duration_ms, "status": "ok"}

    async def get_transition(self) -> dict:
        result = await self.call_async(obs_requests.GetCurrentSceneTransition())
        d = result.datain
        return {"name": d.get("transitionName", ""), "duration_ms": d.get("transitionDuration", 0)}

    # ── Media sources ─────────────────────────────────────────────────

    async def set_media_source(self, source_name: str, file_path: str, loop: bool = False) -> dict:
        """
        Load a file into a Media Source input.
        loop=False by default — obs-relay handles advancing via on_media_ended.
        """
        await self.call_async(
            obs_requests.SetInputSettings(
                inputName=source_name,
                inputSettings={"local_file": file_path, "looping": loop, "is_local_file": True},
            )
        )
        log.info(f"Media source '{source_name}' → {file_path}")
        return {"source": source_name, "file": file_path, "status": "ok"}

    async def play_pause_media(self, source_name: str, pause: bool = False) -> dict:
        action = "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PAUSE" if pause else "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PLAY"
        await self.call_async(obs_requests.TriggerMediaInputAction(inputName=source_name, mediaAction=action))
        return {"source": source_name, "paused": pause, "status": "ok"}

    async def restart_media(self, source_name: str) -> dict:
        await self.call_async(obs_requests.TriggerMediaInputAction(
            inputName=source_name, mediaAction="OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART"
        ))
        return {"source": source_name, "status": "restarted"}

    async def get_media_status(self, source_name: str) -> dict:
        """Get playback state + cursor/duration for a media source."""
        result = await self.call_async(obs_requests.GetMediaInputStatus(inputName=source_name))
        d = result.datain
        return {
            "source": source_name,
            "state": d.get("mediaState", ""),
            "duration_ms": d.get("mediaDuration", 0),
            "cursor_ms": d.get("mediaCursor", 0),
        }

    # ── Streaming ─────────────────────────────────────────────────────

    async def start_stream(self) -> dict:
        await self.call_async(obs_requests.StartStream())
        return {"status": "streaming_started"}

    async def stop_stream(self) -> dict:
        await self.call_async(obs_requests.StopStream())
        return {"status": "streaming_stopped"}

    async def get_stream_status(self) -> dict:
        result = await self.call_async(obs_requests.GetStreamStatus())
        d = result.datain
        return {
            "active": d.get("outputActive", False),
            "reconnecting": d.get("outputReconnecting", False),
            "timecode": d.get("outputTimecode", ""),
            "bytes": d.get("outputBytes", 0),
        }

    # ── Recording ─────────────────────────────────────────────────────

    async def start_recording(self) -> dict:
        await self.call_async(obs_requests.StartRecord())
        return {"status": "recording_started"}

    async def stop_recording(self) -> dict:
        result = await self.call_async(obs_requests.StopRecord())
        d = result.datain if result and hasattr(result, "datain") else {}
        return {"status": "recording_stopped", "output_path": d.get("outputPath", "")}

    async def get_recording_status(self) -> dict:
        result = await self.call_async(obs_requests.GetRecordStatus())
        d = result.datain
        return {
            "active": d.get("outputActive", False),
            "paused": d.get("outputPaused", False),
            "timecode": d.get("outputTimecode", ""),
            "bytes": d.get("outputBytes", 0),
        }

    async def pause_recording(self) -> dict:
        await self.call_async(obs_requests.PauseRecord())
        return {"status": "recording_paused"}

    async def resume_recording(self) -> dict:
        await self.call_async(obs_requests.ResumeRecord())
        return {"status": "recording_resumed"}

    # ── Studio mode ───────────────────────────────────────────────────

    async def enable_studio_mode(self, enabled: bool = True) -> dict:
        await self.call_async(obs_requests.SetStudioModeEnabled(studioModeEnabled=enabled))
        return {"studio_mode": enabled, "status": "ok"}

    async def get_preview_scene(self) -> str:
        result = await self.call_async(obs_requests.GetCurrentPreviewScene())
        return result.datain.get("currentPreviewSceneName", "")

    async def transition_to_program(self) -> dict:
        await self.call_async(obs_requests.TriggerStudioModeTransition())
        return {"status": "transitioned"}

    # ── Audio ─────────────────────────────────────────────────────────

    async def set_volume(self, source_name: str, volume_db: float) -> dict:
        await self.call_async(obs_requests.SetInputVolume(inputName=source_name, inputVolumeDb=volume_db))
        return {"source": source_name, "volume_db": volume_db, "status": "ok"}

    async def set_mute(self, source_name: str, muted: bool) -> dict:
        await self.call_async(obs_requests.SetInputMute(inputName=source_name, inputMuted=muted))
        return {"source": source_name, "muted": muted, "status": "ok"}

    # ── Text sources & scene item visibility ──────────────────────────

    async def set_text_source(self, source_name: str, text: str) -> dict:
        """
        Update a Text (GDI+) or Text (FreeType 2) source's content.
        The source must already exist in OBS — obs-relay only sets the text value.
        """
        await self.call_async(
            obs_requests.SetInputSettings(
                inputName=source_name,
                inputSettings={"text": text},
            )
        )
        log.info(f"Text source '{source_name}' → {text!r}")
        return {"source": source_name, "text": text, "status": "ok"}

    async def get_text_source(self, source_name: str) -> dict:
        """Read the current text content of a text source."""
        result = await self.call_async(
            obs_requests.GetInputSettings(inputName=source_name)
        )
        settings = result.datain.get("inputSettings", {})
        return {"source": source_name, "text": settings.get("text", "")}

    async def get_scene_item_id(self, scene_name: str, source_name: str) -> int:
        """
        Look up the sceneItemId for a source within a scene.
        Required by SetSceneItemEnabled — OBS identifies items by numeric ID, not name.
        Returns -1 if not found.
        """
        try:
            result = await self.call_async(
                obs_requests.GetSceneItemId(sceneName=scene_name, sourceName=source_name)
            )
            return result.datain.get("sceneItemId", -1)
        except Exception:
            return -1

    async def set_scene_item_enabled(
        self, scene_name: str, source_name: str, enabled: bool
    ) -> dict:
        """
        Show or hide a source within a scene by toggling its visibility.
        This is the equivalent of clicking the eye icon in the OBS Sources panel.

        Args:
            scene_name:  Name of the OBS scene containing the source
            source_name: Name of the source to show/hide
            enabled:     True = visible, False = hidden
        """
        item_id = await self.get_scene_item_id(scene_name, source_name)
        if item_id == -1:
            raise ValueError(
                f"Source '{source_name}' not found in scene '{scene_name}'. "
                "Ensure the source exists and the scene name matches exactly."
            )
        await self.call_async(
            obs_requests.SetSceneItemEnabled(
                sceneName=scene_name,
                sceneItemId=item_id,
                sceneItemEnabled=enabled,
            )
        )
        log.debug(f"Scene item '{source_name}' in '{scene_name}' → {'visible' if enabled else 'hidden'}")
        return {
            "scene": scene_name,
            "source": source_name,
            "enabled": enabled,
            "scene_item_id": item_id,
            "status": "ok",
        }

    async def get_scene_item_enabled(self, scene_name: str, source_name: str) -> dict:
        """Check whether a source is currently visible in a scene."""
        item_id = await self.get_scene_item_id(scene_name, source_name)
        if item_id == -1:
            return {"source": source_name, "enabled": False, "scene_item_id": -1}
        result = await self.call_async(
            obs_requests.GetSceneItemEnabled(sceneName=scene_name, sceneItemId=item_id)
        )
        return {
            "scene": scene_name,
            "source": source_name,
            "enabled": result.datain.get("sceneItemEnabled", False),
            "scene_item_id": item_id,
        }

    # ── System ────────────────────────────────────────────────────────

    async def get_version(self) -> dict:
        result = await self.call_async(obs_requests.GetVersion())
        d = result.datain
        return {
            "obs_version": d.get("obsVersion", ""),
            "obs_web_socket_version": d.get("obsWebSocketVersion", ""),
            "platform": d.get("platform", ""),
        }
