"""
YamlDeviceModel — a fully config-driven DeviceModel implementation.

Reads a model.yaml file and implements all DeviceModel methods generically.
Devices that need custom packet framing or parsing can add a protocol.py
alongside model.yaml and define MODEL_CLASS there.

Built-in checksum algorithms (model.yaml: packet.checksum):
  negate_sum   (-sum([length, *payload])) & 0xFF
  sum          sum([length, *payload]) & 0xFF
  xor          XOR of all payload bytes
  crc8         CRC-8 (poly 0x07)
  none         no checksum byte appended

Supported field types in state_packets:
  bool        — true if byte value == true_value
  int         — raw byte value
  map         — byte value looked up in a named map
  map_word_be — 2-byte big-endian word looked up in a named map
  bitmask     — true if (byte & mask) != 0
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from bt_classic_mqtt.model import DeviceModel, DeviceState, PartialState

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Built-in checksum algorithms
# name → fn(length: int, payload: bytes) -> int
# ------------------------------------------------------------------

def _csum_negate_sum(length: int, payload: bytes) -> int:
    """(-sum([length, *payload])) & 0xFF"""
    return (-sum([length, *payload])) & 0xFF

def _csum_sum(length: int, payload: bytes) -> int:
    """sum([length, *payload]) & 0xFF"""
    return sum([length, *payload]) & 0xFF

def _csum_xor(length: int, payload: bytes) -> int:
    """XOR of all payload bytes (length not included)."""
    result = 0
    for b in payload:
        result ^= b
    return result

def _csum_crc8(length: int, payload: bytes) -> int:
    """CRC-8 (poly 0x07) over payload bytes."""
    crc = 0
    for b in payload:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc

def _csum_none(length: int, payload: bytes) -> int:
    """No checksum — returns 0 (caller must not append it)."""
    return 0


_CHECKSUM_REGISTRY: dict[str, Any] = {
    "negate_sum": _csum_negate_sum,
    "sum":        _csum_sum,
    "xor":        _csum_xor,
    "crc8":       _csum_crc8,
    "none":       _csum_none,
}


class YamlDeviceModel(DeviceModel):
    """
    Config-driven DeviceModel.

    Load via:
      YamlDeviceModel.from_config(Path("devices/my-device/model.yaml"))
    """

    def __init__(self, config_path: Path) -> None:
        with open(config_path) as f:
            self._cfg = yaml.safe_load(f)

        self._device_id: str    = self._cfg["device_id"]
        self._topic_prefix: str = self._cfg.get("mqtt_topic_prefix", self._device_id)

        # Packet framing
        pkt = self._cfg["packet"]
        self._sync: bytes = bytes.fromhex(pkt["sync"])
        csum_name: str    = pkt.get("checksum", "none")
        if csum_name not in _CHECKSUM_REGISTRY:
            raise ValueError(
                f"Unknown checksum '{csum_name}'. "
                f"Available: {list(_CHECKSUM_REGISTRY)}"
            )
        self._checksum_fn  = _CHECKSUM_REGISTRY[csum_name]
        self._has_checksum = csum_name != "none"

        # Streaming buffer
        self._buf = bytearray()

        # Pre-build init sequence
        self._init_seq = [
            self._wrap(bytes.fromhex(h))
            for h in self._cfg.get("connection", {}).get("init_sequence", [])
        ]

        # Maps (name → {int_key: str_value})
        self._maps: dict[str, dict[int, str]] = {
            name: {int(k, 16) if isinstance(k, str) else k: v
                   for k, v in entries.items()}
            for name, entries in self._cfg.get("maps", {}).items()
        }
        # Reverse maps (name → {str_value_lower: int_key})
        self._reverse_maps: dict[str, dict[str, int]] = {
            name: {v.lower(): k for k, v in m.items()}
            for name, m in self._maps.items()
        }

        # Commands & followups
        self._commands:  dict[str, dict]  = self._cfg.get("commands", {})
        self._followups: dict[str, bytes] = {
            name: bytes.fromhex(h)
            for name, h in self._cfg.get("followups", {}).items()
        }

        # State packet definitions
        self._state_packets: dict[int, dict] = {
            (int(k, 16) if isinstance(k, str) else k): v
            for k, v in self._cfg.get("state_packets", {}).items()
        }

    @classmethod
    def from_config(cls, config_path: Path) -> "YamlDeviceModel":
        """
        Load a device from a model.yaml path.

        If a protocol.py exists in the same directory and defines MODEL_CLASS,
        that class is used instead (custom packet framing support).
        """
        import importlib.util

        config_path = Path(config_path)
        if not config_path.exists():
            raise FileNotFoundError(f"Config not found: {config_path}")

        # Optional: load MODEL_CLASS from protocol.py for custom framing
        protocol_path = config_path.parent / "protocol.py"
        if protocol_path.exists():
            spec = importlib.util.spec_from_file_location("protocol", protocol_path)
            mod  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if hasattr(mod, "MODEL_CLASS"):
                return mod.MODEL_CLASS(config_path)

        return cls(config_path)

    # ------------------------------------------------------------------
    # DeviceModel interface
    # ------------------------------------------------------------------

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def mqtt_topic_prefix(self) -> str:
        return self._topic_prefix

    def init_sequence(self) -> list[bytes]:
        return list(self._init_seq)

    def encode(self, payload: bytes) -> bytes:
        return self._wrap(payload)

    def decode_stream(self, data: bytes) -> tuple[list[bytes], bytes]:
        self._buf.extend(data)
        buf = self._buf
        packets = []

        while True:
            idx = buf.find(self._sync)
            if idx == -1:
                buf.clear()
                break
            if idx > 0:
                logger.debug("Discarding %d bytes before sync", idx)
                del buf[:idx]

            # sync + length_byte + (optional checksum)
            min_frame = len(self._sync) + 1 + (1 if self._has_checksum else 0)
            if len(buf) < min_frame:
                break

            pkt_len = buf[len(self._sync)]
            total   = len(self._sync) + 1 + pkt_len + (1 if self._has_checksum else 0)

            if len(buf) < total:
                break

            raw = bytes(buf[len(self._sync) + 1: len(self._sync) + 1 + pkt_len])

            if self._has_checksum:
                csum_recv = buf[len(self._sync) + 1 + pkt_len]
                csum_exp  = self._checksum_fn(pkt_len, raw)
                if csum_recv != csum_exp:
                    logger.warning(
                        "Checksum mismatch (got 0x%02X, expected 0x%02X) — skipping byte",
                        csum_recv, csum_exp,
                    )
                    del buf[0]
                    continue

            packets.append(raw)
            del buf[:total]

        self._buf = buf
        return packets, bytes(self._buf)

    def parse_packet(self, payload: bytes) -> PartialState | None:
        if not payload:
            return None

        ptype = payload[0]
        spec  = self._state_packets.get(ptype)
        if spec is None:
            return None

        min_len = spec.get("min_length", 1)
        if len(payload) < min_len:
            logger.warning("Packet 0x%02X too short (%d < %d)", ptype, len(payload), min_len)
            return None

        result: PartialState = {}
        for field_name, fdef in spec["fields"].items():
            value = self._parse_field(payload, fdef)
            if value is not None:
                result[field_name] = value

        return result if result else None

    def mqtt_payload_to_packets(self, payload: dict[str, Any]) -> list[bytes]:
        packets: list[bytes] = []
        mqtt_cmd_cfg = self._cfg.get("mqtt_commands", {})

        for key, value in payload.items():
            if key not in mqtt_cmd_cfg:
                logger.warning("Unknown MQTT command key: %s", key)
                continue

            spec = mqtt_cmd_cfg[key]

            if spec == "direct":
                cmd_name = str(value).upper()
                pkts = self._resolve_command(cmd_name)
                if pkts:
                    packets.extend(pkts)
                else:
                    logger.warning("Unknown direct command: %s", cmd_name)

            elif isinstance(spec, dict) and "map" in spec:
                map_name = spec["map"]
                prefix   = spec.get("prefix", "")
                rev      = self._reverse_maps.get(map_name, {})
                key_val  = rev.get(str(value).lower())
                if key_val is None:
                    logger.warning("Unknown value '%s' for key '%s'", value, key)
                    continue
                cmd_name = self._find_command_for_map_value(map_name, prefix, key_val)
                if cmd_name:
                    pkts = self._resolve_command(cmd_name)
                    if pkts:
                        packets.extend(pkts)
                else:
                    logger.warning("No command found for %s=%s", key, value)

            elif isinstance(spec, dict):
                # pyyaml parses on/off/true/false as Python bools
                if isinstance(value, bool):
                    cmd_name = spec.get(value)
                else:
                    str_val  = str(value).lower()
                    cmd_name = spec.get(str_val)
                    if cmd_name is None:
                        if str_val in ("true", "on"):
                            cmd_name = spec.get(True)
                        elif str_val in ("false", "off"):
                            cmd_name = spec.get(False)
                if cmd_name:
                    pkts = self._resolve_command(cmd_name)
                    if pkts:
                        packets.extend(pkts)
                else:
                    logger.warning("No command mapping for %s=%s", key, value)

        return packets

    def state_to_mqtt(self, state: DeviceState) -> dict[str, Any]:
        d = state.as_dict()

        result: dict[str, Any] = {}

        field_map = {
            "power":      lambda v: v,
            "input":      lambda v: v,
            "muted":      lambda v: v,
            "volume":     lambda v: round(v * 2),   # 0–50 → 0–100 (%)
            "subwoofer":  lambda v: v,
            "surround":   "sound_mode",              # key rename only
            "bass_ext":   lambda v: v,
            "clearvoice": lambda v: v,
        }

        for internal_key, transform in field_map.items():
            if internal_key not in d:
                continue
            if isinstance(transform, str):
                # key rename, value unchanged
                result[transform] = d[internal_key]
            else:
                result[internal_key] = transform(d[internal_key])

        for k, v in d.items():
            if k not in field_map:
                result[k] = v

        return result

    def ha_discovery_payloads(
        self,
        topic_availability: str,
        topic_state: str,
        topic_command: str,
    ) -> list[tuple[str, dict]]:
        import json

        ha_cfg = self._cfg.get("ha_discovery", {})
        did    = self._device_id
        payloads: list[tuple[str, dict]] = []

        device = {
            "identifiers":  [did],
            "name":         self._cfg.get("device_name",         did.upper()),
            "model":        self._cfg.get("device_model",        did.upper()),
            "manufacturer": self._cfg.get("device_manufacturer", "Unknown"),
        }
        base = {"availability_topic": topic_availability, "device": device}

        def pub(topic: str, payload: dict) -> None:
            payloads.append((topic, {**base, **payload}))

        # Switches
        for sw in ha_cfg.get("switches", []):
            uid = f"{did}_{sw['field']}"
            pub(f"homeassistant/switch/{uid}/config", {
                "name":           sw["name"],
                "unique_id":      uid,
                "state_topic":    topic_state,
                "value_template": f"{{{{ 'ON' if value_json.{sw['field']} else 'OFF' }}}}",
                "state_on":       "ON",
                "state_off":      "OFF",
                "command_topic":  topic_command,
                "payload_on":     json.dumps({sw.get("cmd_key", "command"): sw["on_cmd"]}),
                "payload_off":    json.dumps({sw.get("cmd_key", "command"): sw["off_cmd"]}),
                "icon":           sw.get("icon", "mdi:toggle-switch"),
            })

        # Selects
        for sel in ha_cfg.get("selects", []):
            uid          = f"{did}_{sel['field']}"
            options      = list(self._maps.get(sel["options_from"], {}).values())
            mqtt_key     = {"surround": "sound_mode"}.get(sel["field"], sel["field"])
            pub(f"homeassistant/select/{uid}/config", {
                "name":             sel["name"],
                "unique_id":        uid,
                "state_topic":      topic_state,
                "value_template":   f"{{{{ value_json.{mqtt_key} }}}}",
                "command_topic":    topic_command,
                "command_template": '{{"{}": "{{{{ value }}}}"}}'.format(mqtt_key),
                "options":          options,
                "icon":             sel.get("icon", "mdi:tune"),
            })

        # Buttons
        for btn in ha_cfg.get("buttons", []):
            uid = f"{did}_{btn['cmd'].lower()}"
            pub(f"homeassistant/button/{uid}/config", {
                "name":          btn["name"],
                "unique_id":     uid,
                "command_topic": topic_command,
                "payload_press": json.dumps({"command": btn["cmd"]}),
                "icon":          btn.get("icon", "mdi:button-pointer"),
            })

        # Sensors
        for sen in ha_cfg.get("sensors", []):
            uid     = f"{did}_{sen['field']}"
            payload = {
                "name":           sen["name"],
                "unique_id":      uid,
                "state_topic":    topic_state,
                "value_template": f"{{{{ value_json.{sen['field']} }}}}",
                "icon":           sen.get("icon", "mdi:information"),
            }
            if "unit" in sen:
                payload["unit_of_measurement"] = sen["unit"]
            pub(f"homeassistant/sensor/{uid}/config", payload)

        return payloads

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _wrap(self, payload: bytes) -> bytes:
        """Frame: sync + length + payload [+ checksum]."""
        length = len(payload)
        frame  = self._sync + bytes([length]) + payload
        if self._has_checksum:
            frame += bytes([self._checksum_fn(length, payload)])
        return frame

    def _parse_field(self, payload: bytes, fdef: dict) -> Any:
        offset: int = fdef["offset"]
        ftype:  str = fdef["type"]

        if offset >= len(payload):
            return None

        byte = payload[offset]

        if ftype == "bool":
            return byte == fdef.get("true_value", 0x01)
        elif ftype == "int":
            return byte
        elif ftype == "map":
            m   = self._maps.get(fdef["map"], {})
            val = m.get(byte)
            return val if val is not None else f"unknown(0x{byte:02X})"
        elif ftype == "map_word_be":
            if offset + 1 >= len(payload):
                return None
            word = (payload[offset] << 8) | payload[offset + 1]
            m    = self._maps.get(fdef["map"], {})
            val  = m.get(word)
            return val if val is not None else f"unknown(0x{word:04X})"
        elif ftype == "bitmask":
            mask = fdef["mask"]
            if isinstance(mask, str):
                mask = int(mask, 16)
            return bool(byte & mask)

        logger.warning("Unknown field type: %s", ftype)
        return None

    def _resolve_command(self, cmd_name: str) -> list[bytes]:
        cmd_def = self._commands.get(cmd_name)
        if cmd_def is None:
            return []
        packets = [self._wrap(bytes.fromhex(cmd_def["payload"]))]
        followup_key = cmd_def.get("followup")
        if followup_key:
            fp = self._followups.get(followup_key)
            if fp:
                packets.append(self._wrap(fp))
        return packets

    def _find_command_for_map_value(
        self, map_name: str, prefix: str, map_key: int
    ) -> str | None:
        m        = self._maps.get(map_name, {})
        display  = m.get(map_key, "").upper().replace(" ", "")
        for name in [f"{prefix}{display}", f"{prefix}{display.replace('_', '')}"]:
            if name in self._commands:
                return name
        for name in self._commands:
            if name.startswith(prefix) and display in name:
                return name
        return None