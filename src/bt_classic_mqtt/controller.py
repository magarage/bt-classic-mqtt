"""
Controller — wires BT connection, speaker model, and MQTT together.

Two daemon threads:
  1. recv_loop    — reads BT stream, parses packets via model, publishes state
  2. command_loop — drains the command queue; reconnects BT on-demand

Connection policy:
  - MQTT availability reflects whether the BRIDGE is alive, not the BT connection.
    The bridge is always "online" as long as the process is running — this allows
    HA to send commands (e.g. power on) even when the device is off.
  - BT disconnect → power field set to False in state, published to MQTT.
  - Command arrives while BT is offline → reconnect, then send.
  - No keepalive — lets the device's idle timer turn it off naturally.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

from bt_classic_mqtt.bluetooth import BTConnection, BTConnectionError
from bt_classic_mqtt.model import DeviceModel, DeviceState
from bt_classic_mqtt.mqtt import MQTTClient

logger = logging.getLogger(__name__)

_COMMAND_TIMEOUT  = 0.2
_BT_RETRY_COUNT   = 5    # reconnect attempts before dropping command
_BT_RETRY_DELAY   = 3.0  # seconds between reconnect attempts


class Controller:
    def __init__(self, bt: BTConnection, mqtt: MQTTClient, model: DeviceModel) -> None:
        self._bt    = bt
        self._mqtt  = mqtt
        self._model = model

        self._cmd_queue: queue.Queue[list[bytes]] = queue.Queue()
        self._running = False
        self._threads: list[threading.Thread] = []
        self._state: DeviceState = model.new_state()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._mqtt.set_command_callback(self._on_mqtt_command)
        self._mqtt.connect()

        # Publish HA discovery after MQTT is connected so entities appear in HA
        self._publish_discovery()

        # Bridge is online as soon as MQTT connects — regardless of BT state
        # (availability tracks the bridge process, not the BT connection)
        try:
            self._bt.connect()
        except BTConnectionError as exc:
            logger.warning("Initial BT connect failed: %s — waiting for command to retry", exc)
            # Publish power=off so HA reflects the device is currently off
            self._publish_device_off()

        self._running = True

        for target, name in [
            (self._recv_loop,    "bt-recv"),
            (self._command_loop, "bt-cmd"),
        ]:
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

        logger.info("Controller started (model=%s)", self._model.device_id)

    def stop(self) -> None:
        logger.info("Controller stopping …")
        self._running = False
        self._bt.close()
        self._mqtt.disconnect()
        for t in self._threads:
            t.join(timeout=5)
        logger.info("Controller stopped")

    # ------------------------------------------------------------------
    # Thread: receive loop
    # ------------------------------------------------------------------

    def _recv_loop(self) -> None:
        while self._running:
            if not self._bt.is_connected:
                time.sleep(1.0)
                continue
            try:
                data = self._bt.recv(256)
            except TimeoutError:
                # No data within timeout — connection still alive, keep waiting
                continue
            except OSError as exc:
                if not self._running:
                    break
                logger.warning("BT recv error: %s — device went offline", exc)
                self._on_bt_disconnect()
                continue

            if not data:
                continue

            packets, _ = self._model.decode_stream(data)
            for payload in packets:
                self._handle_packet(payload)

    def _handle_packet(self, payload: bytes) -> None:
        partial = self._model.parse_packet(payload)
        if partial is None:
            logger.debug("Ignored packet (type=0x%02X, len=%d)", payload[0], len(payload))
            return

        changed = self._state.merge(partial)
        if not changed:
            return

        if not self._state.is_complete():
            logger.debug("Partial state (waiting for full state): %s", partial)
            return

        logger.debug("State update: %s", self._state.as_dict())
        self._mqtt.publish_state(self._model.state_to_mqtt(self._state))

    # ------------------------------------------------------------------
    # Thread: command loop
    # ------------------------------------------------------------------

    def _command_loop(self) -> None:
        while self._running:
            try:
                packets = self._cmd_queue.get(timeout=_COMMAND_TIMEOUT)
            except queue.Empty:
                continue

            if not self._bt.is_connected:
                logger.info("BT disconnected — reconnecting for queued command")
                connected = False
                for attempt in range(1, _BT_RETRY_COUNT + 1):
                    try:
                        self._bt.connect()
                        logger.info("BT reconnected (attempt %d)", attempt)
                        connected = True
                        break
                    except BTConnectionError as exc:
                        logger.warning(
                            "Reconnect attempt %d/%d failed: %s",
                            attempt, _BT_RETRY_COUNT, exc,
                        )
                        if attempt < _BT_RETRY_COUNT:
                            time.sleep(_BT_RETRY_DELAY)
                if not connected:
                    logger.error("BT reconnect failed after %d attempts — dropping command", _BT_RETRY_COUNT)
                    continue

            try:
                for pkt in packets:
                    self._bt.send(pkt)
                logger.debug("Sent %d packet(s)", len(packets))
            except OSError as exc:
                logger.error("Command send failed: %s", exc)
                self._on_bt_disconnect()

    # ------------------------------------------------------------------
    # MQTT command handler
    # ------------------------------------------------------------------

    def _on_mqtt_command(self, topic: str, payload: dict[str, Any]) -> None:
        packets = self._model.mqtt_payload_to_packets(payload)
        if packets:
            self._cmd_queue.put(packets)
        else:
            logger.warning("No packets generated for MQTT payload: %s", payload)

    # ------------------------------------------------------------------
    # BT disconnect handler
    # ------------------------------------------------------------------

    def _on_bt_disconnect(self) -> None:
        """
        Called when BT connection drops (device turned off, out of range, etc.).

        - Close the socket
        - Reset internal state
        - Publish power=off to MQTT so HA reflects device is off
        - Keep MQTT availability=online so HA can still send commands
          (e.g. user presses Power On → triggers BT reconnect)
        """
        self._bt.close()
        self._state = self._model.new_state()
        self._publish_device_off()
        logger.info("BT offline — MQTT still online, reconnect on next command")

    def _publish_device_off(self) -> None:
        """Publish a minimal state with power=off."""
        off_state = DeviceState()
        off_state.merge({"power": False})
        self._mqtt.publish_state(self._model.state_to_mqtt(off_state))

    # ------------------------------------------------------------------
    # HA Discovery
    # ------------------------------------------------------------------

    def _publish_discovery(self) -> None:
        import json
        for topic, payload in self._model.ha_discovery_payloads(
            self._mqtt.topic_availability,
            self._mqtt.topic_state,
            self._mqtt.topic_command,
        ):
            self._mqtt.publish(topic, json.dumps(payload), retain=True)
        logger.debug("HA discovery published for model=%s", self._model.device_id)