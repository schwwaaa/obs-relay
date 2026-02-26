#!/usr/bin/env python3
"""
run.py — Launch obs-relay without installing.

Usage (from the obs-relay directory):
    python run.py start
    python run.py start --obs-password mypassword
    python run.py init-config
    python run.py check
    python run.py list-presets
"""
import sys
from pathlib import Path

# Ensure the project root is on the path
sys.path.insert(0, str(Path(__file__).parent))

from obs_relay.main import app

if __name__ == "__main__":
    app()
