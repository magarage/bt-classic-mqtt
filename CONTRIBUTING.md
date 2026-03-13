# Contributing

Contributions are welcome — bug reports, fixes, and improvements alike.

## Getting Started

```bash
git clone https://github.com/magarage/yas207-mqtt-bridge.git
cd yas207-mqtt-bridge

# Install dependencies (requires uv)
uv sync --dev
```

## Running Tests

```bash
uv run pytest tests/ -v
```

Tests are pure unit tests — no soundbar or MQTT broker required.

## Project Structure

```
src/yas207/
├── bt/
│   ├── commands.py    # All BT command/state enums — start here
│   ├── protocol.py    # Packet encode/decode (pure functions, easy to test)
│   └── connection.py  # RFCOMM socket management
├── mqtt/
│   ├── client.py      # paho-mqtt wrapper
│   └── ha_discovery.py
└── controller.py      # Wires everything together
```

## Adding Commands

1. Add the hex payload to `Command` in `commands.py`
2. If the command changes input/surround/volume/subwoofer, set the `.followup`
3. Wire it up in `controller.py` `_on_mqtt_command()` if needed

## Protocol Notes

The YAS-207 SPP protocol was reverse-engineered by Michal Jirku:
https://wejn.org/2021/11/making-yamaha-yas-207-do-what-i-want/

The Ruby reference implementation is at:
https://github.com/wejn/yamaha-yas-207

## Pull Requests

- Keep PRs focused — one feature or fix per PR
- Add or update tests for protocol changes
- Run `uv run pytest` before submitting
