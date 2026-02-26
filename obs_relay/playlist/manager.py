"""
playlist/manager.py — M3U playlist parser, loader, and media scheduler.

v1.1 additions:
  - Auto-advance via OBS MediaInputPlaybackEnded event (register_auto_advance)
  - State persistence: saves/restores active playlist + position to state.json
  - Preflight validation: validate_playlist() checks all files exist on disk
  - M3U EXTVLCOPT start-time / stop-time parsing (trim points)
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class PlaylistItem:
    path: str
    title: str = ""
    duration: int = -1          # seconds, -1 = unknown
    start_time: float = 0.0     # EXTVLCOPT trim in
    stop_time: float = 0.0      # EXTVLCOPT trim out (0 = no trim)
    metadata: dict = field(default_factory=dict)

    @property
    def is_url(self) -> bool:
        return self.path.startswith(("http://", "https://", "rtmp://", "rtsp://"))

    @property
    def exists_on_disk(self) -> bool:
        if self.is_url:
            return True  # Can't check remote URLs
        return Path(self.path).exists()


@dataclass
class Playlist:
    name: str
    items: list[PlaylistItem] = field(default_factory=list)
    loop: bool = True
    shuffle: bool = False
    source_path: Optional[Path] = None

    def __len__(self) -> int:
        return len(self.items)


class M3UParser:
    """Parse .m3u and .m3u8 files into Playlist objects."""

    @staticmethod
    def parse(path: Path, name: Optional[str] = None) -> Playlist:
        playlist = Playlist(name=name or path.stem, source_path=path)
        items: list[PlaylistItem] = []
        current_meta: dict = {}

        with open(path, encoding="utf-8", errors="replace") as f:
            lines = [l.rstrip("\n\r") for l in f.readlines()]

        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line or line == "#EXTM3U":
                i += 1
                continue

            if line.startswith("#EXTINF:"):
                rest = line[8:]
                if "," in rest:
                    dur_str, title = rest.split(",", 1)
                else:
                    dur_str, title = rest, ""
                try:
                    current_meta["duration"] = int(float(dur_str))
                except ValueError:
                    current_meta["duration"] = -1
                current_meta["title"] = title.strip()
                i += 1
                continue

            # Parse EXTVLCOPT trim directives
            if line.startswith("#EXTVLCOPT:start-time="):
                try:
                    current_meta["start_time"] = float(line.split("=", 1)[1])
                except ValueError:
                    pass
                i += 1
                continue

            if line.startswith("#EXTVLCOPT:stop-time="):
                try:
                    current_meta["stop_time"] = float(line.split("=", 1)[1])
                except ValueError:
                    pass
                i += 1
                continue

            # Parse overlay directives — #EXTOVERLAY:key=value
            # Supported keys:
            #   text=Custom overlay text (overrides title)
            #   hold=12         (seconds visible, overrides global default)
            #   delay=3         (seconds after track start before showing)
            #   skip=1          (suppress overlay for this track)
            if line.startswith("#EXTOVERLAY:"):
                rest = line[12:]
                if "=" in rest:
                    key, val = rest.split("=", 1)
                    key = key.strip().lower()
                    val = val.strip()
                    if key == "skip":
                        current_meta["overlay_skip"] = val not in ("0", "false", "no", "")
                    elif key == "hold":
                        try:
                            current_meta["overlay_hold"] = float(val)
                        except ValueError:
                            pass
                    elif key == "delay":
                        try:
                            current_meta["overlay_delay"] = float(val)
                        except ValueError:
                            pass
                    elif key == "text":
                        current_meta["overlay_text"] = val
                i += 1
                continue

            if line.startswith("#"):
                i += 1
                continue

            items.append(PlaylistItem(
                path=line,
                title=current_meta.get("title", Path(line).stem if not line.startswith("http") else line),
                duration=current_meta.get("duration", -1),
                start_time=current_meta.get("start_time", 0.0),
                stop_time=current_meta.get("stop_time", 0.0),
                metadata={k: v for k, v in current_meta.items() if k not in ("title", "duration", "start_time", "stop_time")},
            ))
            current_meta = {}
            i += 1

        playlist.items = items
        log.info(f"Parsed playlist '{playlist.name}' — {len(items)} items from {path}")
        return playlist

    @staticmethod
    def write(playlist: Playlist, path: Path) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n\n")
            for item in playlist.items:
                f.write(f"#EXTINF:{item.duration},{item.title}\n")
                if item.start_time:
                    f.write(f"#EXTVLCOPT:start-time={item.start_time}\n")
                if item.stop_time:
                    f.write(f"#EXTVLCOPT:stop-time={item.stop_time}\n")
                f.write(f"{item.path}\n\n")
        log.info(f"Wrote playlist '{playlist.name}' to {path}")


class PlaylistManager:
    """
    Manages multiple named playlists and drives OBS media source updates.

    v1.1: Auto-advance, state persistence, preflight validation.
    """

    STATE_FILE = "playlist_state.json"

    def __init__(self, playlist_dir: Path, obs_update_callback=None, state_dir: Optional[Path] = None):
        self.playlist_dir = playlist_dir
        self.playlist_dir.mkdir(parents=True, exist_ok=True)
        self._obs_update = obs_update_callback
        self._state_dir = state_dir or playlist_dir

        self._playlists: dict[str, Playlist] = {}
        self._active_playlist: Optional[str] = None
        self._position: int = 0
        self._shuffled_order: list[int] = []
        self._source_name: str = "MediaSource"
        self._auto_advance_enabled: bool = True
        self._track_listeners: list = []  # (item, playlist_name, position) callbacks

    # ──────────────────────────────────────────────────────────────────
    # Auto-advance  ← NEW
    # ──────────────────────────────────────────────────────────────────

    def register_auto_advance(self, obs_client: "OBSClient", source_name: str = "MediaSource") -> None:
        """
        Wire up automatic playlist advancement when a media source finishes.
        Call this after both obs_client and playlist_manager are initialized.

        This subscribes to the OBS MediaInputPlaybackEnded event and advances
        the playlist whenever the named source finishes playing.
        """
        self._source_name = source_name

        async def on_ended(ended_source_name: str) -> None:
            if not self._auto_advance_enabled:
                return
            # Only advance if it's our managed source
            if ended_source_name == source_name:
                log.info(f"Auto-advance: '{source_name}' finished → next track")
                await self.next(source_name=source_name)

        obs_client.on_media_ended(on_ended)
        log.info(f"Auto-advance registered for source: '{source_name}'")

    def set_auto_advance(self, enabled: bool) -> None:
        """Enable or disable automatic playlist advancement."""
        self._auto_advance_enabled = enabled
        log.info(f"Auto-advance {'enabled' if enabled else 'disabled'}")

    def add_track_listener(self, callback) -> None:
        """
        Register a callback fired whenever a track changes (activate, next, prev, seek).
        Callback signature: async def cb(item: PlaylistItem, playlist_name: str, position: int)
        Used by OverlayManager and any other system that needs to react to track changes.
        """
        self._track_listeners.append(callback)
        log.info(f"Track listener registered: {callback.__qualname__ if hasattr(callback, '__qualname__') else callback}")

    async def _fire_track_listeners(self, item, playlist_name: str, position: int) -> None:
        """Fire all registered track-change listeners."""
        for cb in self._track_listeners:
            try:
                await cb(item, playlist_name, position)
            except Exception as e:
                log.error(f"Track listener error: {e}")

    # ──────────────────────────────────────────────────────────────────
    # State persistence  ← NEW
    # ──────────────────────────────────────────────────────────────────

    def save_state(self) -> None:
        """Persist current playback position to disk for crash recovery."""
        state = {
            "active_playlist": self._active_playlist,
            "position": self._position,
            "source_name": self._source_name,
            "auto_advance": self._auto_advance_enabled,
        }
        state_file = self._state_dir / self.STATE_FILE
        try:
            with open(state_file, "w") as f:
                json.dump(state, f, indent=2)
            log.debug(f"Playlist state saved → {state_file}")
        except Exception as e:
            log.warning(f"Could not save playlist state: {e}")

    def load_state(self) -> Optional[dict]:
        """Load persisted playback state. Returns None if no state file exists."""
        state_file = self._state_dir / self.STATE_FILE
        if not state_file.exists():
            return None
        try:
            with open(state_file) as f:
                state = json.load(f)
            log.info(f"Loaded playlist state: playlist='{state.get('active_playlist')}' pos={state.get('position')}")
            return state
        except Exception as e:
            log.warning(f"Could not load playlist state: {e}")
            return None

    async def restore_state(self) -> bool:
        """
        Attempt to restore previous playback state after startup.
        Returns True if state was restored, False otherwise.
        """
        state = self.load_state()
        if not state or not state.get("active_playlist"):
            return False
        pl_name = state["active_playlist"]
        position = state.get("position", 0)
        if pl_name not in self._playlists:
            log.warning(f"State restore: playlist '{pl_name}' not found")
            return False
        self._auto_advance_enabled = state.get("auto_advance", True)
        await self.activate(pl_name, position, source_name=state.get("source_name", "MediaSource"))
        log.info(f"State restored: '{pl_name}' at position {position}")
        return True

    # ──────────────────────────────────────────────────────────────────
    # Preflight validation  ← NEW
    # ──────────────────────────────────────────────────────────────────

    def validate_playlist(self, name: str) -> dict:
        """
        Check all items in a playlist exist on disk before going live.
        Returns a report with any missing files.

        Example:
            result = playlist_mgr.validate_playlist("main")
            if not result["valid"]:
                print("Missing files:", result["missing"])
        """
        pl = self._playlists.get(name)
        if not pl:
            return {"valid": False, "error": f"Playlist '{name}' not found"}

        missing = []
        ok = []
        for item in pl.items:
            if item.exists_on_disk:
                ok.append(item.path)
            else:
                missing.append(item.path)

        return {
            "playlist": name,
            "valid": len(missing) == 0,
            "total": len(pl.items),
            "ok": len(ok),
            "missing_count": len(missing),
            "missing": missing,
        }

    def validate_all(self) -> dict:
        """Run preflight validation on all loaded playlists."""
        results = {}
        all_valid = True
        for name in self._playlists:
            r = self.validate_playlist(name)
            results[name] = r
            if not r["valid"]:
                all_valid = False
        return {"all_valid": all_valid, "playlists": results}

    # ──────────────────────────────────────────────────────────────────
    # Playlist CRUD
    # ──────────────────────────────────────────────────────────────────

    def load_all(self) -> dict[str, Playlist]:
        for m3u_path in sorted(self.playlist_dir.glob("*.m3u")):
            pl = M3UParser.parse(m3u_path)
            self._playlists[pl.name] = pl
        for m3u_path in sorted(self.playlist_dir.glob("*.m3u8")):
            pl = M3UParser.parse(m3u_path)
            self._playlists[pl.name] = pl
        log.info(f"Loaded {len(self._playlists)} playlists from {self.playlist_dir}")
        return self._playlists

    def load_file(self, path: Path) -> Playlist:
        pl = M3UParser.parse(path)
        self._playlists[pl.name] = pl
        return pl

    def save_playlist(self, name: str) -> Optional[Path]:
        pl = self._playlists.get(name)
        if not pl:
            return None
        out_path = self.playlist_dir / f"{name}.m3u"
        M3UParser.write(pl, out_path)
        return out_path

    def create_playlist(self, name: str, items: list[str], loop: bool = True) -> Playlist:
        pl = Playlist(
            name=name,
            items=[PlaylistItem(path=p, title=Path(p).stem if not p.startswith("http") else p) for p in items],
            loop=loop,
        )
        self._playlists[name] = pl
        return pl

    def get_playlists(self) -> dict[str, Playlist]:
        return dict(self._playlists)

    def get_playlist(self, name: str) -> Optional[Playlist]:
        return self._playlists.get(name)

    def delete_playlist(self, name: str) -> bool:
        if name not in self._playlists:
            return False
        del self._playlists[name]
        p = self.playlist_dir / f"{name}.m3u"
        if p.exists():
            p.unlink()
        return True

    # ──────────────────────────────────────────────────────────────────
    # Playback control
    # ──────────────────────────────────────────────────────────────────

    @property
    def active_playlist(self) -> Optional[Playlist]:
        return self._playlists.get(self._active_playlist) if self._active_playlist else None

    @property
    def current_item(self) -> Optional[PlaylistItem]:
        pl = self.active_playlist
        if not pl or not pl.items:
            return None
        idx = self._resolve_index()
        return pl.items[idx] if 0 <= idx < len(pl.items) else None

    def _resolve_index(self) -> int:
        pl = self.active_playlist
        if not pl:
            return 0
        if pl.shuffle and self._shuffled_order:
            return self._shuffled_order[self._position % len(self._shuffled_order)]
        return self._position % len(pl.items) if pl.items else 0

    async def activate(self, name: str, position: int = 0, source_name: Optional[str] = None) -> Optional[PlaylistItem]:
        if name not in self._playlists:
            log.warning(f"Playlist '{name}' not found.")
            return None
        src = source_name or self._source_name
        self._active_playlist = name
        self._position = position
        pl = self._playlists[name]
        if pl.shuffle:
            self._shuffled_order = list(range(len(pl.items)))
            random.shuffle(self._shuffled_order)
        else:
            self._shuffled_order = []
        item = self.current_item
        if item and self._obs_update:
            await self._obs_update(src, item.path)
        self.save_state()
        if item:
            await self._fire_track_listeners(item, name, self._position)
        log.info(f"Activated playlist '{name}' at position {position}")
        return item

    async def next(self, source_name: Optional[str] = None) -> Optional[PlaylistItem]:
        pl = self.active_playlist
        if not pl or not pl.items:
            return None
        src = source_name or self._source_name
        self._position += 1
        if self._position >= len(pl.items):
            if pl.loop:
                self._position = 0
                if pl.shuffle:
                    random.shuffle(self._shuffled_order)
            else:
                self._position = len(pl.items) - 1
                log.info("Playlist ended (no loop).")
                self.save_state()
                return None
        item = self.current_item
        if item and self._obs_update:
            await self._obs_update(src, item.path)
        self.save_state()
        if item:
            await self._fire_track_listeners(item, self._active_playlist or "", self._position)
        log.info(f"Advanced to track {self._position}: {item.title if item else 'N/A'}")
        return item

    async def previous(self, source_name: Optional[str] = None) -> Optional[PlaylistItem]:
        pl = self.active_playlist
        if not pl or not pl.items:
            return None
        src = source_name or self._source_name
        self._position = max(0, self._position - 1)
        item = self.current_item
        if item and self._obs_update:
            await self._obs_update(src, item.path)
        self.save_state()
        if item:
            await self._fire_track_listeners(item, self._active_playlist or "", self._position)
        return item

    async def seek(self, position: int, source_name: Optional[str] = None) -> Optional[PlaylistItem]:
        pl = self.active_playlist
        if not pl:
            return None
        src = source_name or self._source_name
        self._position = max(0, min(position, len(pl.items) - 1))
        item = self.current_item
        if item and self._obs_update:
            await self._obs_update(src, item.path)
        self.save_state()
        if item:
            await self._fire_track_listeners(item, self._active_playlist or "", self._position)
        return item

    def get_status(self) -> dict:
        pl = self.active_playlist
        item = self.current_item
        return {
            "active_playlist": self._active_playlist,
            "position": self._position,
            "total_items": len(pl.items) if pl else 0,
            "loop": pl.loop if pl else False,
            "shuffle": pl.shuffle if pl else False,
            "auto_advance": self._auto_advance_enabled,
            "current_item": {
                "path": item.path,
                "title": item.title,
                "duration": item.duration,
                "start_time": item.start_time,
                "stop_time": item.stop_time,
            } if item else None,
        }
