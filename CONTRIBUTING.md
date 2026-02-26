# Contributing to obs-relay

Thanks for wanting to contribute. Here's how to get set up and what to know before submitting a PR.

## Setup

```bash
git clone https://github.com/YOUR_ORG/obs-relay.git
cd obs-relay
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest pytest-asyncio black ruff
pip install -e .
```

## Running tests

```bash
pytest tests/ -v
```

Tests don't require a live OBS instance — the OBS client is mocked.

## Code style

```bash
black obs_relay/        # format
ruff check obs_relay/   # lint
```

## Project layout

The codebase is intentionally split into focused modules. When adding features, keep them in the right place:

- **`core/obs_client.py`** — OBS WebSocket client only. No business logic.
- **`playlist/manager.py`** — M3U parsing and playback state. No API concerns.
- **`scenes/presets.py`** — Preset definitions and executor. No direct OBS calls outside of `obs_client`.
- **`api/server.py`** — HTTP/WS layer only. Thin wrappers that delegate to managers.
- **`osc/bridge.py`** — OSC address mapping. Delegates to the same managers as the API.

The goal is that `api/`, `osc/`, and `scenes/` are all just different control surfaces for the same underlying `core/` and `playlist/` logic.

## Adding a new endpoint

1. Add the method to `obs_client.py` if it requires an OBS call
2. Add the FastAPI route in `api/server.py`
3. If relevant, add an OSC address in `osc/bridge.py`
4. If relevant, add a WebSocket command in `_handle_ws_command`
5. Add a test in `tests/`
6. Update `CHANGELOG.md`

## Submitting a PR

- Open an issue first for significant changes
- One feature/fix per PR
- Update the CHANGELOG
- All tests must pass
