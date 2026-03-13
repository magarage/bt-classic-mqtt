"""
DeviceModel — abstract interface that every device definition must implement.

The core engine (controller, connection, MQTT) only depends on this interface.
Device-specific logic lives entirely in devices/<config>.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


# Partial state update — only the fields present in a given packet
PartialState = dict[str, Any]


@dataclass
class DeviceState:
    """Accumulated state of a BT Classic device. All fields start as unknown."""

    _fields: dict[str, Any] = field(default_factory=dict, repr=False)

    def merge(self, partial: PartialState) -> bool:
        """Apply a partial update. Returns True if anything changed."""
        changed = False
        for k, v in partial.items():
            if self._fields.get(k) != v:
                self._fields[k] = v
                changed = True
        return changed

    def get(self, key: str, default: Any = None) -> Any:
        return self._fields.get(key, default)

    def is_complete(self) -> bool:
        """True once we have received at least one full-state packet."""
        return bool(self._fields)

    def as_dict(self) -> dict[str, Any]:
        return dict(self._fields)


class DeviceModel(ABC):
    """
    Abstract base class for all BT Classic device definitions.

    Implementations can be:
      - Pure YAML  — via YamlDeviceModel (no Python needed for most devices)
      - YAML + Python override — add protocol.py next to model.yaml
      - Pure Python — subclass DeviceModel directly (full control)
    """

    @property
    @abstractmethod
    def device_id(self) -> str:
        """Unique device identifier, used as MQTT topic prefix by default."""

    @property
    def mqtt_topic_prefix(self) -> str:
        """MQTT topic prefix. Defaults to device_id."""
        return self.device_id

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    @abstractmethod
    def init_sequence(self) -> list[bytes]:
        """Packets to send on initial BT connect (handshake)."""

    # ------------------------------------------------------------------
    # Packet framing
    # ------------------------------------------------------------------

    @abstractmethod
    def encode(self, payload: bytes) -> bytes:
        """Wrap raw payload bytes in a framed packet ready to send."""

    @abstractmethod
    def decode_stream(self, data: bytes) -> tuple[list[bytes], bytes]:
        """
        Parse raw bytes from the BT stream.

        Returns:
          (list of complete payloads, remaining buffer bytes)
        """

    # ------------------------------------------------------------------
    # State parsing
    # ------------------------------------------------------------------

    @abstractmethod
    def parse_packet(self, payload: bytes) -> PartialState | None:
        """Parse a single decoded payload into a PartialState. None if unrecognised."""

    def new_state(self) -> DeviceState:
        """Return a fresh empty state object."""
        return DeviceState()

    # ------------------------------------------------------------------
    # MQTT command handling
    # ------------------------------------------------------------------

    @abstractmethod
    def mqtt_payload_to_packets(self, payload: dict[str, Any]) -> list[bytes]:
        """
        Translate an incoming MQTT command payload to a list of framed packets.

        e.g. {"source": "Bluetooth", "sound_mode": "Music"} -> [pkt1, pkt2, pkt3]
        """

    @abstractmethod
    def state_to_mqtt(self, state: DeviceState) -> dict[str, Any]:
        """Convert internal DeviceState to the dict published on {topic_prefix}/state."""

    # ------------------------------------------------------------------
    # Home Assistant Discovery
    # ------------------------------------------------------------------

    @abstractmethod
    def ha_discovery_payloads(
        self, topic_availability: str, topic_state: str, topic_command: str
    ) -> list[tuple[str, dict]]:
        """
        Return list of (discovery_topic, payload_dict) to publish for HA auto-discovery.
        """
