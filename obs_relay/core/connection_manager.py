"""
core/connection_manager.py — Global OBS client singleton for dependency injection.
"""

from __future__ import annotations

from typing import Optional
from .obs_client import OBSClient

_obs_client: Optional[OBSClient] = None


def init_obs_client(
    host: str,
    port: int,
    password: str,
    reconnect_interval: float = 5.0,
    max_reconnect_attempts: int = 0,
) -> OBSClient:
    global _obs_client
    _obs_client = OBSClient(
        host=host,
        port=port,
        password=password,
        reconnect_interval=reconnect_interval,
        max_reconnect_attempts=max_reconnect_attempts,
    )
    return _obs_client


def get_obs_client() -> OBSClient:
    if _obs_client is None:
        raise RuntimeError("OBS client not initialized. Call init_obs_client() first.")
    return _obs_client
