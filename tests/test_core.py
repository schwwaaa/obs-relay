"""
tests/ — Basic test coverage for obs-relay modules.
Run with: pytest tests/ -v
"""

import pytest
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


# ─── Playlist Manager ─────────────────────────────────────────────────────────

from obs_relay.playlist import PlaylistManager, M3UParser, Playlist, PlaylistItem


def test_m3u_parse(tmp_path):
    m3u = tmp_path / "test.m3u"
    m3u.write_text(
        "#EXTM3U\n\n"
        "#EXTINF:120,Test Track 1\n/media/track1.mp4\n\n"
        "#EXTINF:240,Test Track 2\n/media/track2.mp4\n"
    )
    pl = M3UParser.parse(m3u)
    assert pl.name == "test"
    assert len(pl.items) == 2
    assert pl.items[0].title == "Test Track 1"
    assert pl.items[0].duration == 120
    assert pl.items[1].path == "/media/track2.mp4"


def test_m3u_write_roundtrip(tmp_path):
    pl = Playlist(
        name="roundtrip",
        items=[
            PlaylistItem("/a.mp4", "Track A", 60),
            PlaylistItem("/b.mp4", "Track B", 90),
        ],
    )
    out = tmp_path / "roundtrip.m3u"
    M3UParser.write(pl, out)
    parsed = M3UParser.parse(out)
    assert len(parsed.items) == 2
    assert parsed.items[0].title == "Track A"


@pytest.mark.asyncio
async def test_playlist_manager_basic(tmp_path):
    updates = []

    async def fake_update(source, path):
        updates.append((source, path))

    mgr = PlaylistManager(playlist_dir=tmp_path, obs_update_callback=fake_update)
    pl = mgr.create_playlist("test", ["/a.mp4", "/b.mp4", "/c.mp4"])
    assert len(pl.items) == 3

    item = await mgr.activate("test", source_name="MediaSource")
    assert item.path == "/a.mp4"
    assert len(updates) == 1

    item = await mgr.next(source_name="MediaSource")
    assert item.path == "/b.mp4"

    item = await mgr.next(source_name="MediaSource")
    assert item.path == "/c.mp4"

    # Loop back
    item = await mgr.next(source_name="MediaSource")
    assert item.path == "/a.mp4"


@pytest.mark.asyncio
async def test_playlist_seek(tmp_path):
    mgr = PlaylistManager(tmp_path)
    mgr.create_playlist("seek_test", [f"/track{i}.mp4" for i in range(5)])
    await mgr.activate("seek_test")
    item = await mgr.seek(3)
    assert item.path == "/track3.mp4"


# ─── Scene Presets ─────────────────────────────────────────────────────────────

from obs_relay.scenes import ScenePreset, ScenePresetManager


@pytest.mark.asyncio
async def test_preset_manager_activate():
    mock_obs = AsyncMock()
    mock_obs.is_connected.return_value = True
    mock_obs.switch_scene = AsyncMock(return_value={"scene": "BRB", "status": "ok"})

    mgr = ScenePresetManager(mock_obs)
    mgr.register_defaults()

    result = await mgr.activate("brb")
    assert result["preset"] == "brb"
    mock_obs.switch_scene.assert_called_once_with("BRB")


@pytest.mark.asyncio
async def test_preset_not_found():
    mock_obs = AsyncMock()
    mock_obs.is_connected.return_value = True
    mgr = ScenePresetManager(mock_obs)
    mgr.register_defaults()

    with pytest.raises(ValueError, match="not found"):
        await mgr.activate("nonexistent")


def test_preset_from_dict():
    data = {
        "name": "custom",
        "scene_name": "CustomScene",
        "description": "A custom preset",
        "actions": [{"type": "set_mute", "params": {"source_name": "Mic", "muted": True}}],
        "hotkey": "/obs/scene/custom",
    }
    preset = ScenePreset.from_dict(data)
    assert preset.name == "custom"
    assert len(preset.actions) == 1
    assert preset.actions[0].type == "set_mute"


# ─── Config ───────────────────────────────────────────────────────────────────

from obs_relay.config import Settings


def test_settings_defaults():
    s = Settings()
    assert s.obs.port == 4455
    assert s.api.port == 8080
    assert s.osc.listen_port == 9000


def test_settings_yaml_load(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "obs:\n  host: 192.168.1.100\n  port: 4455\n  password: secret\n"
        "api:\n  port: 9090\n"
    )
    s = Settings.load(config)
    assert s.obs.host == "192.168.1.100"
    assert s.obs.password == "secret"
    assert s.api.port == 9090
