#!/usr/bin/env python3
"""
scripts/build.py — Build a standalone obs-relay executable.

Usage:
    python scripts/build.py [--onefile] [--name obs-relay]

Requires: pip install pyinstaller
"""

import subprocess
import sys
from pathlib import Path


def build(name: str = "obs-relay", onefile: bool = True, platform_hint: str = ""):
    root = Path(__file__).parent.parent
    entry = root / "obs_relay" / "_entry.py"

    # Write entry script
    entry.write_text(
        "import sys\n"
        "from obs_relay.main import app\n"
        "if __name__ == '__main__':\n"
        "    app()\n"
    )

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", name,
        "--clean",
        "--noconfirm",
        "--add-data", f"{root / 'config.yaml'}:.",
        "--add-data", f"{root / 'playlists'}:playlists",
        "--hidden-import", "obs_relay.config.settings",
        "--hidden-import", "obs_relay.core.obs_client",
        "--hidden-import", "obs_relay.api.server",
        "--hidden-import", "obs_relay.osc.bridge",
        "--hidden-import", "obs_relay.playlist.manager",
        "--hidden-import", "obs_relay.scenes.presets",
        "--hidden-import", "uvicorn.lifespan.on",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.logging",
    ]

    if onefile:
        cmd.append("--onefile")

    cmd.append(str(entry))

    print(f"Building: {' '.join(cmd)}\n")
    result = subprocess.run(cmd, cwd=root)

    # Cleanup temp entry
    entry.unlink(missing_ok=True)

    if result.returncode == 0:
        output = root / "dist" / (name if not onefile else name)
        print(f"\n✓ Build successful: {output}")
    else:
        print("\n✗ Build failed.")
        sys.exit(1)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="obs-relay")
    parser.add_argument("--onefile", action="store_true", default=True)
    parser.add_argument("--onedir", dest="onefile", action="store_false")
    args = parser.parse_args()
    build(name=args.name, onefile=args.onefile)
