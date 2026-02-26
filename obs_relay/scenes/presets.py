"""
scenes/presets.py — Scene preset system for broadcast workflows.

Presets are named configurations that map to OBS scene names and optional
side effects (e.g., mute sources, update media). They're designed for the
Pluto.tv-style workflow: standby → content → BRB → switchover → etc.

Presets are loaded from config.yaml under `scenes:` or defined in code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, Any

log = logging.getLogger(__name__)


@dataclass
class SceneAction:
    """A single action to perform when a preset activates."""
    type: str  # "switch_scene" | "set_volume" | "set_mute" | "media_play" | "media_pause" | "media_restart"
    params: dict = field(default_factory=dict)


@dataclass
class ScenePreset:
    """
    A named broadcast state combining a scene switch with optional side effects.

    Example presets:
      - "live"        → Main content scene, unmute everything
      - "brb"         → "Be Right Back" overlay scene
      - "standby"     → Static holding screen
      - "intermission"→ Loop intermission playlist
      - "end_card"    → Final slate with social links
    """
    name: str
    scene_name: str
    description: str = ""
    actions: list[SceneAction] = field(default_factory=list)
    playlist: Optional[str] = None  # playlist to activate when this preset fires
    hotkey: Optional[str] = None    # OSC address or keyboard shortcut hint

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "scene_name": self.scene_name,
            "description": self.description,
            "playlist": self.playlist,
            "hotkey": self.hotkey,
            "actions": [{"type": a.type, "params": a.params} for a in self.actions],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ScenePreset":
        actions = [SceneAction(type=a["type"], params=a.get("params", {})) for a in data.get("actions", [])]
        return cls(
            name=data["name"],
            scene_name=data["scene_name"],
            description=data.get("description", ""),
            actions=actions,
            playlist=data.get("playlist"),
            hotkey=data.get("hotkey"),
        )


# ── Built-in defaults ─────────────────────────────────────────────────────────

DEFAULT_PRESETS: list[ScenePreset] = [
    ScenePreset(
        name="live",
        scene_name="Live",
        description="Main live broadcast scene",
        hotkey="/obs/scene/live",
    ),
    ScenePreset(
        name="brb",
        scene_name="BRB",
        description="Be Right Back screen",
        actions=[SceneAction(type="set_mute", params={"source_name": "Mic", "muted": True})],
        hotkey="/obs/scene/brb",
    ),
    ScenePreset(
        name="standby",
        scene_name="Standby",
        description="Holding / pre-show screen",
        hotkey="/obs/scene/standby",
    ),
    ScenePreset(
        name="intermission",
        scene_name="Intermission",
        description="Intermission loop with playlist",
        playlist="intermission",
        hotkey="/obs/scene/intermission",
    ),
    ScenePreset(
        name="end_card",
        scene_name="EndCard",
        description="Post-show slate",
        hotkey="/obs/scene/end_card",
    ),
]


class ScenePresetManager:
    """
    Registry and executor for scene presets.

    Usage:
        mgr = ScenePresetManager(obs_client, playlist_manager)
        mgr.register_defaults()
        await mgr.activate("brb")
    """

    def __init__(self, obs_client: Any, playlist_manager: Any = None):
        self._obs = obs_client
        self._playlists = playlist_manager
        self._presets: dict[str, ScenePreset] = {}
        self._active_preset: Optional[str] = None
        self._source_name: str = "MediaSource"

    def register(self, preset: ScenePreset) -> None:
        self._presets[preset.name] = preset
        log.debug(f"Registered scene preset: {preset.name} → {preset.scene_name}")

    def register_defaults(self) -> None:
        for preset in DEFAULT_PRESETS:
            self._presets.setdefault(preset.name, preset)

    def register_from_list(self, presets: list[dict]) -> None:
        for data in presets:
            self.register(ScenePreset.from_dict(data))

    def get(self, name: str) -> Optional[ScenePreset]:
        return self._presets.get(name)

    def list_presets(self) -> list[dict]:
        return [p.to_dict() for p in self._presets.values()]

    @property
    def active_preset(self) -> Optional[str]:
        return self._active_preset

    async def activate(self, name: str, source_name: Optional[str] = None) -> dict:
        """Activate a preset: switch scene + run side effects."""
        preset = self._presets.get(name)
        if not preset:
            raise ValueError(f"Preset '{name}' not found. Available: {list(self._presets.keys())}")

        src = source_name or self._source_name
        results = []

        # 1. Switch scene
        if not self._obs.is_connected():
            log.warning("OBS not connected — scene switch skipped.")
            results.append({"action": "switch_scene", "status": "skipped (not connected)"})
        else:
            r = await self._obs.switch_scene(preset.scene_name)
            results.append({"action": "switch_scene", **r})

        # 2. Run side-effect actions
        for action in preset.actions:
            try:
                result = await self._run_action(action)
                results.append({"action": action.type, **result})
            except Exception as e:
                log.error(f"Action '{action.type}' failed: {e}")
                results.append({"action": action.type, "error": str(e)})

        # 3. Activate linked playlist
        if preset.playlist and self._playlists:
            try:
                item = await self._playlists.activate(preset.playlist, source_name=src)
                results.append({
                    "action": "activate_playlist",
                    "playlist": preset.playlist,
                    "track": item.title if item else None,
                })
            except Exception as e:
                log.error(f"Playlist activation failed: {e}")

        self._active_preset = name
        log.info(f"Activated preset: {name}")
        return {"preset": name, "actions": results}

    async def _run_action(self, action: SceneAction) -> dict:
        p = action.params
        match action.type:
            case "set_volume":
                return await self._obs.set_volume(p["source_name"], p["volume_db"])
            case "set_mute":
                return await self._obs.set_mute(p["source_name"], p["muted"])
            case "media_play":
                return await self._obs.play_pause_media(p["source_name"], pause=False)
            case "media_pause":
                return await self._obs.play_pause_media(p["source_name"], pause=True)
            case "media_restart":
                return await self._obs.restart_media(p["source_name"])
            case _:
                return {"status": f"unknown action type: {action.type}"}
