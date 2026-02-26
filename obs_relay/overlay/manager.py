"""
overlay/manager.py — Timed text overlay system driven by playlist track changes.

How it works:
  1. An OBS Text (GDI+) or Text (FreeType 2) source named "TitleOverlay" (configurable)
     lives in your scene, hidden by default (unchecked in OBS sources panel).
  2. When a track changes (auto-advance OR manual), OverlayManager:
       a. Sets the text content to the track's title (from EXTINF in the .m3u)
       b. Shows the source (via SetSceneItemEnabled)
       c. Waits `hold_sec` seconds
       d. Hides the source again
  3. Per-track overrides are supported via custom M3U tags:
       #EXTOVERLAY:text=Now Playing: My Custom Title
       #EXTOVERLAY:hold=10
       #EXTOVERLAY:delay=2
       #EXTOVERLAY:skip=1
  4. An optional `delay_sec` lets you wait before showing (e.g. show after 2s into clip).
  5. The "next up" mode shows upcoming track title instead of current.
  6. Thread-safe: if a new track fires before the timer expires, the old timer is cancelled.

OBS Setup Required:
  - Add a Text (GDI+) source to your scene named "TitleOverlay" (or config name)
  - Set it hidden (eye icon off) by default
  - Style it however you want — obs-relay only sets text + visibility
  - For fade effects: add a Color Correction filter with opacity, or use a Luma Key filter
    (OBS does not expose per-source CSS transitions, so opacity fades require OBS filters)

M3U Example:
  #EXTM3U

  #EXTINF:3600,Episode 01 — The Beginning
  #EXTOVERLAY:hold=12
  /media/ep01.mp4

  #EXTINF:120,Station ID Bumper
  #EXTOVERLAY:skip=1
  /media/bumper.mp4

  #EXTINF:3600,Episode 02 — Return
  #EXTOVERLAY:text=Now Playing: Episode Two
  #EXTOVERLAY:delay=3
  /media/ep02.mp4
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Overlay config
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class OverlayConfig:
    """Global overlay defaults — all overridable per-track via #EXTOVERLAY: tags."""
    enabled: bool         = True
    source_name: str      = "TitleOverlay"    # OBS source name (Text GDI+)
    scene_name: str       = ""                # Scene that owns the source. Empty = current scene.
    hold_sec: float       = 8.0              # How long the overlay stays visible
    delay_sec: float      = 1.0              # Seconds after track start before showing
    fade_in_ms: int       = 500              # Visual reference — actual fade requires OBS filter
    fade_out_ms: int      = 500
    prefix: str           = ""               # Prepended to title: e.g. "Now Playing: "
    suffix: str           = ""               # Appended to title
    mode: str             = "current"        # "current" | "next_up"
    auto_trigger: bool    = True             # Fire automatically on every track change
    next_up_prefix: str   = "Up Next: "


@dataclass
class OverlayStatus:
    active: bool        = False
    current_text: str   = ""
    track_title: str    = ""
    playlist: str       = ""
    position: int       = 0
    timer_remaining: float = 0.0
    config: dict        = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────────────
# OverlayManager
# ──────────────────────────────────────────────────────────────────────────────

class OverlayManager:
    """
    Manages timed text overlays in OBS driven by playlist track changes.

    Lifecycle per track:
      delay_sec → [show source + set text] → hold_sec → [hide source]

    The timer is cancelled and restarted cleanly if a new track fires mid-sequence.
    """

    def __init__(self, obs_client: Any, config: Optional[OverlayConfig] = None):
        self._obs = obs_client
        self.config = config or OverlayConfig()
        self._status = OverlayStatus()
        self._timer_task: Optional[asyncio.Task] = None
        self._track_callbacks: list[Callable] = []

    # ──────────────────────────────────────────────────────────────────
    # Registration
    # ──────────────────────────────────────────────────────────────────

    def register_playlist_listener(self, playlist_manager: Any) -> None:
        """
        Hook into PlaylistManager so overlays fire automatically on every track change.
        Call this after both managers are initialized.
        """
        playlist_manager.add_track_listener(self._on_track_changed)
        log.info(f"Overlay auto-trigger registered → source: '{self.config.source_name}'")

    def add_track_listener(self, callback: Callable) -> None:
        """Register an external callback fired when overlay triggers (for WS broadcast etc)."""
        self._track_callbacks.append(callback)

    # ──────────────────────────────────────────────────────────────────
    # Track change handler
    # ──────────────────────────────────────────────────────────────────

    async def _on_track_changed(self, item: Any, playlist_name: str, position: int) -> None:
        """
        Called by PlaylistManager whenever a track changes.
        item: PlaylistItem with .title, .metadata["overlay_*"] from M3U tags.
        """
        if not self.config.enabled or not self.config.auto_trigger:
            return

        # Read per-track overrides from M3U #EXTOVERLAY: tags
        meta = item.metadata or {}
        if meta.get("overlay_skip"):
            log.info(f"Overlay skipped for track: {item.title}")
            return

        hold   = float(meta.get("overlay_hold",  self.config.hold_sec))
        delay  = float(meta.get("overlay_delay", self.config.delay_sec))
        custom = meta.get("overlay_text", "")

        # Build display text
        if custom:
            text = custom
        elif self.config.mode == "next_up":
            # For next_up, we'd need to peek ahead — use title of incoming track
            text = self.config.next_up_prefix + item.title
        else:
            text = self.config.prefix + item.title + self.config.suffix

        self._status.track_title  = item.title
        self._status.playlist     = playlist_name
        self._status.position     = position

        await self.trigger(text, hold_sec=hold, delay_sec=delay)

    # ──────────────────────────────────────────────────────────────────
    # Core trigger — can be called manually from API too
    # ──────────────────────────────────────────────────────────────────

    async def trigger(
        self,
        text: str,
        hold_sec: Optional[float] = None,
        delay_sec: Optional[float] = None,
    ) -> dict:
        """
        Show the overlay with `text` for `hold_sec` seconds.
        Any in-progress overlay is cancelled and replaced immediately.

        Args:
            text:      Text to display in OBS source
            hold_sec:  How long to keep visible (default: config.hold_sec)
            delay_sec: Seconds to wait before showing (default: config.delay_sec)
        """
        hold  = hold_sec  if hold_sec  is not None else self.config.hold_sec
        delay = delay_sec if delay_sec is not None else self.config.delay_sec

        # Cancel any running timer
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
            # Ensure source is hidden before restarting
            try:
                await self._hide_source()
            except Exception:
                pass

        self._status.current_text = text
        self._status.active       = True

        self._timer_task = asyncio.create_task(
            self._run_sequence(text, hold, delay)
        )
        log.info(f"Overlay triggered: '{text}' hold={hold}s delay={delay}s")

        # Notify external listeners
        for cb in self._track_callbacks:
            try:
                await cb({"text": text, "hold_sec": hold, "delay_sec": delay, "event": "triggered"})
            except Exception as e:
                log.error(f"Overlay track callback error: {e}")

        return {
            "status": "triggered",
            "text": text,
            "hold_sec": hold,
            "delay_sec": delay,
            "source": self.config.source_name,
        }

    async def _run_sequence(self, text: str, hold: float, delay: float) -> None:
        """Delay → show → hold → hide sequence."""
        try:
            if delay > 0:
                log.debug(f"Overlay delay {delay}s...")
                await asyncio.sleep(delay)

            await self._show_source(text)
            self._status.active = True

            log.debug(f"Overlay holding {hold}s: '{text}'")
            # Count down so status.timer_remaining is accurate
            elapsed = 0.0
            tick = 0.25
            while elapsed < hold:
                self._status.timer_remaining = round(hold - elapsed, 1)
                await asyncio.sleep(tick)
                elapsed += tick

            await self._hide_source()

        except asyncio.CancelledError:
            log.debug("Overlay timer cancelled")
        except Exception as e:
            log.error(f"Overlay sequence error: {e}")
        finally:
            self._status.active = False
            self._status.timer_remaining = 0.0
            self._status.current_text = ""

    # ──────────────────────────────────────────────────────────────────
    # OBS operations
    # ──────────────────────────────────────────────────────────────────

    async def _show_source(self, text: str) -> None:
        """Set text content and make the source visible."""
        await self._obs.set_text_source(self.config.source_name, text)
        scene = self.config.scene_name or await self._obs.get_current_scene()
        await self._obs.set_scene_item_enabled(scene, self.config.source_name, True)
        log.debug(f"Overlay shown in scene '{scene}': {text}")

    async def _hide_source(self) -> None:
        """Hide the overlay source."""
        scene = self.config.scene_name or await self._obs.get_current_scene()
        await self._obs.set_scene_item_enabled(scene, self.config.source_name, False)
        log.debug("Overlay hidden")

    # ──────────────────────────────────────────────────────────────────
    # Manual hide
    # ──────────────────────────────────────────────────────────────────

    async def hide(self) -> dict:
        """Immediately cancel and hide the overlay."""
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
        try:
            await self._hide_source()
        except Exception as e:
            log.warning(f"Overlay hide error: {e}")
        self._status.active = False
        self._status.timer_remaining = 0.0
        self._status.current_text = ""
        return {"status": "hidden", "source": self.config.source_name}

    # ──────────────────────────────────────────────────────────────────
    # Config + status
    # ──────────────────────────────────────────────────────────────────

    def update_config(self, **kwargs) -> dict:
        """Update overlay config at runtime. Returns new config dict."""
        for key, val in kwargs.items():
            if hasattr(self.config, key):
                setattr(self.config, key, val)
                log.info(f"Overlay config: {key} = {val!r}")
        return self.get_config()

    def get_config(self) -> dict:
        c = self.config
        return {
            "enabled":        c.enabled,
            "source_name":    c.source_name,
            "scene_name":     c.scene_name,
            "hold_sec":       c.hold_sec,
            "delay_sec":      c.delay_sec,
            "fade_in_ms":     c.fade_in_ms,
            "fade_out_ms":    c.fade_out_ms,
            "prefix":         c.prefix,
            "suffix":         c.suffix,
            "mode":           c.mode,
            "auto_trigger":   c.auto_trigger,
            "next_up_prefix": c.next_up_prefix,
        }

    def get_status(self) -> dict:
        return {
            "active":           self._status.active,
            "current_text":     self._status.current_text,
            "track_title":      self._status.track_title,
            "playlist":         self._status.playlist,
            "position":         self._status.position,
            "timer_remaining":  self._status.timer_remaining,
            "config":           self.get_config(),
        }
