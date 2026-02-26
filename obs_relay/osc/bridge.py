"""
osc/bridge.py — TouchOSC / OSC UDP bridge.

Listens for OSC messages on a UDP port and maps them to OBS actions.
Also sends feedback (current state) back to OSC clients on reply port.

TouchOSC address map (configurable via osc_map in config.yaml):
  /obs/scene/{name}           → activate preset
  /obs/scene/next             → not applicable (scenes)
  /obs/playlist/next          → next track
  /obs/playlist/prev          → previous track
  /obs/playlist/activate/{n}  → activate named playlist
  /obs/stream/start           → start streaming
  /obs/stream/stop            → stop streaming
  /obs/volume/{source}  [0.0–1.0] → set volume
  /obs/mute/{source}    [0/1]     → mute/unmute

Feedback messages sent back:
  /obs/state/scene            → current scene name
  /obs/state/stream           → 0 or 1
  /obs/state/playlist         → current playlist name
  /obs/state/track            → current track title
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

try:
    from pythonosc.dispatcher import Dispatcher
    from pythonosc.osc_server import AsyncIOOSCUDPServer
    from pythonosc.udp_client import SimpleUDPClient
    OSC_AVAILABLE = True
except ImportError:
    OSC_AVAILABLE = False
    log.warning("python-osc not installed. OSC bridge disabled.")


class OSCBridge:
    """
    UDP OSC server that translates TouchOSC / Open Sound Control messages
    into obs-relay actions, and sends feedback back to clients.
    """

    def __init__(
        self,
        listen_host: str = "0.0.0.0",
        listen_port: int = 9000,
        reply_port: int = 9001,
        client_host: str = "255.255.255.255",
        obs_client: Optional[Any] = None,
        preset_manager: Optional[Any] = None,
        playlist_manager: Optional[Any] = None,
    ):
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.reply_port = reply_port
        self.client_host = client_host

        self._obs = obs_client
        self._presets = preset_manager
        self._playlists = playlist_manager

        self._server: Optional[Any] = None
        self._transport: Optional[Any] = None
        self._reply_client: Optional[Any] = None
        self._running = False
        self._custom_handlers: dict[str, Callable] = {}

    def register_handler(self, address: str, handler: Callable) -> None:
        """Register a custom OSC address handler."""
        self._custom_handlers[address] = handler

    # ──────────────────────────────────────────────────────────────────
    # Server lifecycle
    # ──────────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if not OSC_AVAILABLE:
            log.error("python-osc not installed. Cannot start OSC bridge.")
            return

        dispatcher = Dispatcher()
        self._setup_dispatcher(dispatcher)

        self._server = AsyncIOOSCUDPServer(
            (self.listen_host, self.listen_port),
            dispatcher,
            asyncio.get_running_loop(),
        )
        self._transport, _ = await self._server.create_serve_endpoint()
        self._reply_client = SimpleUDPClient(self.client_host, self.reply_port)
        self._running = True
        log.info(f"OSC bridge listening on {self.listen_host}:{self.listen_port} → reply to {self.client_host}:{self.reply_port}")

    async def stop(self) -> None:
        if self._transport:
            self._transport.close()
        self._running = False
        log.info("OSC bridge stopped.")

    def is_running(self) -> bool:
        return self._running

    # ──────────────────────────────────────────────────────────────────
    # Dispatcher setup
    # ──────────────────────────────────────────────────────────────────

    def _setup_dispatcher(self, dispatcher: Any) -> None:
        # Scene presets
        dispatcher.map("/obs/scene/*", self._handle_scene)

        # Playlist control
        dispatcher.map("/obs/playlist/next", self._handle_playlist_next)
        dispatcher.map("/obs/playlist/prev", self._handle_playlist_prev)
        dispatcher.map("/obs/playlist/activate/*", self._handle_playlist_activate)
        dispatcher.map("/obs/playlist/seek", self._handle_playlist_seek)

        # Streaming
        dispatcher.map("/obs/stream/start", self._handle_stream_start)
        dispatcher.map("/obs/stream/stop", self._handle_stream_stop)

        # Volume / mute
        dispatcher.map("/obs/volume/*", self._handle_volume)
        dispatcher.map("/obs/mute/*", self._handle_mute)

        # State query
        dispatcher.map("/obs/state/query", self._handle_state_query)

        # Custom handlers
        for addr, handler in self._custom_handlers.items():
            dispatcher.map(addr, handler)

        # Default fallback
        dispatcher.set_default_handler(self._handle_unknown)

    # ──────────────────────────────────────────────────────────────────
    # OSC message handlers
    # ──────────────────────────────────────────────────────────────────

    def _run(self, coro) -> None:
        """Schedule a coroutine on the running event loop."""
        asyncio.get_event_loop().create_task(coro)

    def _handle_scene(self, address: str, *args) -> None:
        # /obs/scene/{preset_name}
        parts = address.strip("/").split("/")
        if len(parts) >= 3:
            preset_name = parts[2]
            log.info(f"[OSC] Scene preset: {preset_name}")
            self._run(self._activate_preset(preset_name))

    def _handle_playlist_next(self, address: str, *args) -> None:
        log.info("[OSC] Playlist: next")
        self._run(self._playlist_next())

    def _handle_playlist_prev(self, address: str, *args) -> None:
        log.info("[OSC] Playlist: previous")
        self._run(self._playlist_prev())

    def _handle_playlist_activate(self, address: str, *args) -> None:
        parts = address.strip("/").split("/")
        if len(parts) >= 4:
            pl_name = parts[3]
            log.info(f"[OSC] Activate playlist: {pl_name}")
            self._run(self._activate_playlist(pl_name))

    def _handle_playlist_seek(self, address: str, *args) -> None:
        if args:
            try:
                pos = int(args[0])
                self._run(self._playlist_seek(pos))
            except (ValueError, IndexError):
                pass

    def _handle_stream_start(self, address: str, *args) -> None:
        log.info("[OSC] Stream: start")
        self._run(self._stream_start())

    def _handle_stream_stop(self, address: str, *args) -> None:
        log.info("[OSC] Stream: stop")
        self._run(self._stream_stop())

    def _handle_volume(self, address: str, *args) -> None:
        # /obs/volume/{source_name}  value=0.0-1.0 → mapped to -60dB to 0dB
        parts = address.strip("/").split("/")
        if len(parts) >= 3 and args:
            source_name = parts[2]
            try:
                val = float(args[0])  # 0.0 to 1.0
                db = (val * 60) - 60  # map to -60 to 0 dB
                self._run(self._set_volume(source_name, db))
            except (ValueError, IndexError):
                pass

    def _handle_mute(self, address: str, *args) -> None:
        parts = address.strip("/").split("/")
        if len(parts) >= 3 and args:
            source_name = parts[2]
            muted = bool(int(args[0]))
            self._run(self._set_mute(source_name, muted))

    def _handle_state_query(self, address: str, *args) -> None:
        self._run(self._send_state())

    def _handle_unknown(self, address: str, *args) -> None:
        log.debug(f"[OSC] Unhandled: {address} {args}")

    # ──────────────────────────────────────────────────────────────────
    # Async action implementations
    # ──────────────────────────────────────────────────────────────────

    async def _activate_preset(self, name: str) -> None:
        if self._presets:
            try:
                await self._presets.activate(name)
                await self._send_state()
            except Exception as e:
                log.error(f"OSC preset activation error: {e}")

    async def _playlist_next(self) -> None:
        if self._playlists:
            item = await self._playlists.next()
            if item:
                self._send_osc("/obs/state/track", item.title)

    async def _playlist_prev(self) -> None:
        if self._playlists:
            item = await self._playlists.previous()
            if item:
                self._send_osc("/obs/state/track", item.title)

    async def _activate_playlist(self, name: str) -> None:
        if self._playlists:
            item = await self._playlists.activate(name)
            if item:
                self._send_osc("/obs/state/playlist", name)
                self._send_osc("/obs/state/track", item.title)

    async def _playlist_seek(self, pos: int) -> None:
        if self._playlists:
            await self._playlists.seek(pos)

    async def _stream_start(self) -> None:
        if self._obs and self._obs.is_connected():
            await self._obs.start_stream()
            self._send_osc("/obs/state/stream", 1)

    async def _stream_stop(self) -> None:
        if self._obs and self._obs.is_connected():
            await self._obs.stop_stream()
            self._send_osc("/obs/state/stream", 0)

    async def _set_volume(self, source: str, db: float) -> None:
        if self._obs and self._obs.is_connected():
            await self._obs.set_volume(source, db)

    async def _set_mute(self, source: str, muted: bool) -> None:
        if self._obs and self._obs.is_connected():
            await self._obs.set_mute(source, muted)

    async def _send_state(self) -> None:
        """Broadcast current state back to OSC clients."""
        if not self._reply_client:
            return
        if self._obs and self._obs.is_connected():
            try:
                scene = await self._obs.get_current_scene()
                self._send_osc("/obs/state/scene", scene)
                status = await self._obs.get_stream_status()
                self._send_osc("/obs/state/stream", 1 if status["active"] else 0)
            except Exception as e:
                log.debug(f"State send error: {e}")

        if self._playlists:
            pl_status = self._playlists.get_status()
            if pl_status["active_playlist"]:
                self._send_osc("/obs/state/playlist", pl_status["active_playlist"])
            if pl_status["current_item"]:
                self._send_osc("/obs/state/track", pl_status["current_item"]["title"])

    def _send_osc(self, address: str, value: Any) -> None:
        if self._reply_client:
            try:
                self._reply_client.send_message(address, value)
            except Exception as e:
                log.debug(f"OSC send error: {e}")

    def send_feedback(self, address: str, value: Any) -> None:
        """Public method to send arbitrary OSC feedback."""
        self._send_osc(address, value)
