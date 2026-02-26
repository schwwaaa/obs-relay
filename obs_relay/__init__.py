"""
obs-relay — Remote OBS control relay for drop-zone-ops ecosystem.

Modules:
  core/     — OBS WebSocket client & connection manager
  api/      — FastAPI REST + WebSocket bridge
  osc/      — TouchOSC UDP listener/sender
  playlist/ — M3U playlist parser & scheduler
  scenes/   — Scene preset definitions & switcher logic
  config/   — Settings, env loading, YAML config
"""

__version__ = "1.1.0"
__author__ = "drop-zone-ops"
