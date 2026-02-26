"""
config/settings.py — Central configuration via env vars + YAML override.

Priority: ENV > config.yaml > defaults
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class OBSSettings(BaseSettings):
    host: str = Field("localhost", description="OBS WebSocket host")
    port: int = Field(4455, description="OBS WebSocket port")
    password: str = Field("", description="OBS WebSocket password")
    reconnect_interval: float = Field(5.0, description="Seconds between reconnect attempts")
    max_reconnect_attempts: int = Field(10, description="Max reconnect attempts (0=infinite)")

    model_config = SettingsConfigDict(env_prefix="OBS_")


class APISettings(BaseSettings):
    host: str = Field("0.0.0.0", description="API server bind host")
    port: int = Field(8080, description="API server port")
    api_key: Optional[str] = Field(None, description="Bearer token for API auth (optional)")
    cors_origins: list[str] = Field(["*"], description="CORS allowed origins")
    log_level: str = Field("info", description="Log level")

    model_config = SettingsConfigDict(env_prefix="API_")


class OSCSettings(BaseSettings):
    enabled: bool = Field(True, description="Enable TouchOSC/OSC listener")
    listen_host: str = Field("0.0.0.0", description="OSC UDP listen host")
    listen_port: int = Field(9000, description="OSC UDP listen port")
    reply_port: int = Field(9001, description="OSC UDP reply/feedback port")
    client_host: str = Field("255.255.255.255", description="OSC broadcast/client host")

    model_config = SettingsConfigDict(env_prefix="OSC_")


class PlaylistSettings(BaseSettings):
    directory: Path = Field(Path("playlists"), description="Directory for .m3u playlist files")
    default_playlist: Optional[str] = Field(None, description="Auto-load playlist on startup")
    loop: bool = Field(True, description="Loop playlists by default")
    source_name: str = Field("MediaSource", description="OBS media source name for playlist items")

    model_config = SettingsConfigDict(env_prefix="PLAYLIST_")


class Settings(BaseSettings):
    obs: OBSSettings = Field(default_factory=OBSSettings)
    api: APISettings = Field(default_factory=APISettings)
    osc: OSCSettings = Field(default_factory=OSCSettings)
    playlist: PlaylistSettings = Field(default_factory=PlaylistSettings)
    config_file: Path = Field(Path("config.yaml"), description="Path to YAML config file")

    model_config = SettingsConfigDict(env_prefix="RELAY_")

    @classmethod
    def load(cls, config_path: Optional[Path] = None) -> "Settings":
        """Load settings, merging YAML file if present."""
        path = config_path or Path(os.environ.get("RELAY_CONFIG_FILE", "config.yaml"))
        yaml_data: dict = {}

        if path.exists():
            with open(path) as f:
                yaml_data = yaml.safe_load(f) or {}

        # Build sub-settings from YAML + env (env takes priority via pydantic-settings)
        obs = OBSSettings(**yaml_data.get("obs", {}))
        api = APISettings(**yaml_data.get("api", {}))
        osc = OSCSettings(**yaml_data.get("osc", {}))
        playlist = PlaylistSettings(**yaml_data.get("playlist", {}))

        return cls(obs=obs, api=api, osc=osc, playlist=playlist)

    def to_yaml(self, path: Path) -> None:
        """Save current settings to YAML."""
        data = {
            "obs": self.obs.model_dump(),
            "api": self.api.model_dump(),
            "osc": self.osc.model_dump(),
            "playlist": {
                **self.playlist.model_dump(),
                "directory": str(self.playlist.directory),
            },
        }
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)


# Singleton accessor — call get_settings() anywhere in the app
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.load()
    return _settings


def reload_settings(config_path: Optional[Path] = None) -> Settings:
    global _settings
    _settings = Settings.load(config_path)
    return _settings
