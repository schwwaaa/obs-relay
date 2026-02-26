"""
main.py — obs-relay application entrypoint.

Bootstraps:
  1. Config loading
  2. OBS WebSocket client
  3. Playlist manager (with auto-advance wiring)
  4. Scene preset manager
  5. TouchOSC/OSC bridge
  6. FastAPI server (uvicorn)

CLI:
  python run.py start              start the relay server
  python run.py init-config        create a default config.yaml
  python run.py list-presets       print available presets
  python run.py check              test OBS connectivity
  python run.py validate-playlists check all playlist files exist
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Optional

import typer
import uvicorn
from rich.console import Console
from rich.table import Table
from rich.logging import RichHandler

from obs_relay.config import get_settings, reload_settings
from obs_relay.core import init_obs_client, get_obs_client
from obs_relay.playlist import PlaylistManager
from obs_relay.scenes import ScenePresetManager
from obs_relay.osc import OSCBridge
from obs_relay.overlay import OverlayManager, OverlayConfig
from obs_relay.api import create_app, set_managers

console = Console()
app = typer.Typer(name="obs-relay", help="Remote OBS control relay — drop-zone-ops ecosystem")


def setup_logging(level: str = "info") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )


async def build_and_run(config_path: Optional[Path] = None) -> None:
    settings = reload_settings(config_path)
    setup_logging(settings.api.log_level)
    log = logging.getLogger("obs_relay")

    console.rule("[bold blue]obs-relay v1.1.0[/bold blue]")

    # 1. OBS client
    obs_client = init_obs_client(
        host=settings.obs.host,
        port=settings.obs.port,
        password=settings.obs.password,
        reconnect_interval=settings.obs.reconnect_interval,
        max_reconnect_attempts=settings.obs.max_reconnect_attempts,
    )

    # 2. Initial OBS connection (non-fatal)
    connected = await obs_client.connect()
    if not connected:
        console.print(f"[yellow]⚠ OBS not reachable at {settings.obs.host}:{settings.obs.port} — will retry in background[/yellow]")
        asyncio.create_task(obs_client.start_reconnect_loop())

    # 3. Playlist manager
    async def obs_media_update(source_name: str, file_path: str):
        if obs_client.is_connected():
            await obs_client.set_media_source(source_name, file_path)

    playlist_mgr = PlaylistManager(
        playlist_dir=settings.playlist.directory,
        obs_update_callback=obs_media_update,
        state_dir=Path("."),
    )
    playlist_mgr.load_all()

    # Wire auto-advance: OBS MediaInputPlaybackEnded → next track
    playlist_mgr.register_auto_advance(obs_client, source_name=settings.playlist.source_name)

    # Restore previous playback state (crash recovery)
    restored = await playlist_mgr.restore_state()
    if restored:
        console.print("[green]✓ Playlist state restored from previous session[/green]")
    elif settings.playlist.default_playlist:
        await playlist_mgr.activate(
            settings.playlist.default_playlist,
            source_name=settings.playlist.source_name,
        )

    # 4. Scene preset manager
    preset_mgr = ScenePresetManager(obs_client, playlist_mgr)
    preset_mgr.register_defaults()

    # 4b. Overlay manager
    overlay_config = OverlayConfig(
        source_name=settings.overlay.source_name if hasattr(settings, 'overlay') else 'TitleOverlay',
        hold_sec=settings.overlay.hold_sec if hasattr(settings, 'overlay') else 8.0,
        delay_sec=settings.overlay.delay_sec if hasattr(settings, 'overlay') else 1.0,
        prefix=settings.overlay.prefix if hasattr(settings, 'overlay') else '',
        mode=settings.overlay.mode if hasattr(settings, 'overlay') else 'current',
        auto_trigger=settings.overlay.auto_trigger if hasattr(settings, 'overlay') else True,
    )
    overlay_mgr = OverlayManager(obs_client, overlay_config)
    overlay_mgr.register_playlist_listener(playlist_mgr)

    # 5. OSC bridge
    osc_bridge: Optional[OSCBridge] = None
    if settings.osc.enabled:
        osc_bridge = OSCBridge(
            listen_host=settings.osc.listen_host,
            listen_port=settings.osc.listen_port,
            reply_port=settings.osc.reply_port,
            client_host=settings.osc.client_host,
            obs_client=obs_client,
            preset_manager=preset_mgr,
            playlist_manager=playlist_mgr,
        )
        await osc_bridge.start()

    # 6. Wire managers into API
    set_managers(preset_mgr, playlist_mgr, osc_bridge, overlay_mgr)
    fast_app = create_app()

    # 7. Startup summary
    console.print(f"\n[green]✓ OBS[/green]       {settings.obs.host}:{settings.obs.port} ({'connected' if connected else 'pending reconnect'})")
    console.print(f"[green]✓ API[/green]       http://{settings.api.host}:{settings.api.port}")
    console.print(f"[green]✓ WS[/green]        ws://{settings.api.host}:{settings.api.port}/ws")
    if osc_bridge:
        console.print(f"[green]✓ OSC[/green]       UDP :{settings.osc.listen_port} → reply :{settings.osc.reply_port}")
    if settings.api.api_key:
        console.print(f"[green]✓ Auth[/green]      API key set — Bearer token required")
        console.print(f"[dim]            WS auth: ws://host:{settings.api.port}/ws?token=YOUR_KEY[/dim]")
    else:
        console.print(f"[yellow]⚠ Auth[/yellow]      No API key set — open access (fine for LAN, not internet)")
    console.print(f"[green]✓ Docs[/green]      http://{settings.api.host}:{settings.api.port}/docs\n")

    # 8. uvicorn
    config = uvicorn.Config(
        fast_app,
        host=settings.api.host,
        port=settings.api.port,
        log_level=settings.api.log_level,
        loop="asyncio",
    )
    server = uvicorn.Server(config)

    loop = asyncio.get_running_loop()

    def shutdown():
        log.info("Shutdown signal received.")
        server.should_exit = True
        if osc_bridge:
            loop.create_task(osc_bridge.stop())
        loop.create_task(obs_client.disconnect())

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shutdown)
        except NotImplementedError:
            pass  # Windows

    await server.serve()


# ──────────────────────────────────────────────────────────────────────────────
# CLI commands
# ──────────────────────────────────────────────────────────────────────────────

@app.command()
def start(
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Path to config.yaml"),
    host: Optional[str] = typer.Option(None, "--host", help="API bind host"),
    port: Optional[int] = typer.Option(None, "--port", "-p", help="API port"),
    obs_host: Optional[str] = typer.Option(None, "--obs-host", help="OBS WebSocket host"),
    obs_port: Optional[int] = typer.Option(None, "--obs-port", help="OBS WebSocket port"),
    obs_password: Optional[str] = typer.Option(None, "--obs-password", help="OBS WebSocket password"),
):
    """Start the obs-relay server."""
    import os
    if host:
        os.environ["API_HOST"] = host
    if port:
        os.environ["API_PORT"] = str(port)
    if obs_host:
        os.environ["OBS_HOST"] = obs_host
    if obs_port:
        os.environ["OBS_PORT"] = str(obs_port)
    if obs_password:
        os.environ["OBS_PASSWORD"] = obs_password
    asyncio.run(build_and_run(config))


@app.command("init-config")
def init_config(
    output: Path = typer.Option(Path("config.yaml"), "--output", "-o"),
):
    """Generate a default config.yaml."""
    from obs_relay.config import Settings
    s = Settings.load()
    s.to_yaml(output)
    console.print(f"[green]✓[/green] Config written to [bold]{output}[/bold]")


@app.command("list-presets")
def list_presets_cmd():
    """Print the built-in scene presets."""
    from obs_relay.scenes import DEFAULT_PRESETS
    table = Table(title="Built-in Scene Presets", show_header=True)
    table.add_column("Name", style="cyan")
    table.add_column("OBS Scene", style="green")
    table.add_column("Description")
    table.add_column("OSC Address", style="yellow")
    for p in DEFAULT_PRESETS:
        table.add_row(p.name, p.scene_name, p.description, p.hotkey or "-")
    console.print(table)


@app.command("check")
def check_obs(
    host: str = typer.Option("localhost", "--host"),
    port: int = typer.Option(4455, "--port"),
    password: str = typer.Option("", "--password"),
):
    """Test OBS WebSocket connectivity."""
    async def _check():
        client = init_obs_client(host, port, password)
        ok = await client.connect()
        if ok:
            version = await client.get_version()
            console.print(f"[green]✓ Connected to OBS[/green]")
            console.print(f"  OBS version:       {version.get('obs_version')}")
            console.print(f"  WebSocket version: {version.get('obs_web_socket_version')}")
            console.print(f"  Platform:          {version.get('platform')}")
            scenes = await client.get_scenes()
            console.print(f"  Scenes ({len(scenes)}): {', '.join(s['name'] for s in scenes)}")
            transition = await client.get_transition()
            console.print(f"  Transition: {transition['name']} ({transition['duration_ms']}ms)")
            await client.disconnect()
        else:
            console.print(f"[red]✗ Could not connect to OBS at {host}:{port}[/red]")
            sys.exit(1)
    asyncio.run(_check())


@app.command("validate-playlists")
def validate_playlists_cmd(
    playlist_dir: Path = typer.Option(Path("playlists"), "--dir", "-d"),
):
    """Preflight check — verify all files in all playlists exist on disk."""
    from obs_relay.playlist import PlaylistManager
    mgr = PlaylistManager(playlist_dir)
    mgr.load_all()
    results = mgr.validate_all()

    if results["all_valid"]:
        console.print("[green]✓ All playlists valid — no missing files[/green]")
    else:
        console.print("[red]✗ Missing files found:[/red]")

    for name, r in results["playlists"].items():
        if r["valid"]:
            console.print(f"  [green]✓[/green] {name}: {r['ok']}/{r['total']} files OK")
        else:
            console.print(f"  [red]✗[/red] {name}: {r['missing_count']} missing")
            for f in r["missing"]:
                console.print(f"      [dim]→ {f}[/dim]")

    if not results["all_valid"]:
        sys.exit(1)


@app.command("build-standalone")
def build_standalone(
    name: str = typer.Option("obs-relay", "--name"),
    onefile: bool = typer.Option(True, "--onefile/--onedir"),
):
    """Build a standalone executable using PyInstaller."""
    import subprocess
    entry = Path(__file__).parent / "_entry.py"
    entry.write_text("from obs_relay.main import app\nif __name__ == '__main__':\n    app()\n")
    cmd = [sys.executable, "-m", "PyInstaller", "--name", name, "--clean", "--noconfirm"]
    if onefile:
        cmd.append("--onefile")
    cmd.append(str(entry))
    result = subprocess.run(cmd)
    entry.unlink(missing_ok=True)
    if result.returncode == 0:
        console.print(f"\n[green]✓ Build complete.[/green] Executable: dist/{name}")
    else:
        console.print("[red]Build failed.[/red]")
        sys.exit(1)


if __name__ == "__main__":
    app()
