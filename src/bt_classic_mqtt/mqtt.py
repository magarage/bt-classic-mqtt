"""
MQTT client wrapper (paho-mqtt 2.x).

Topic names are driven by the model's mqtt_topic_prefix.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)


class MQTTClient:
    def __init__(
        self,
        host: str,
        port: int,
        topic_prefix: str,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self._host   = host
        self._port   = port
        self._prefix = topic_prefix

        self.topic_availability = f"{topic_prefix}/availability"
        self.topic_state        = f"{topic_prefix}/state"
        self.topic_command      = f"{topic_prefix}/command"

        self._command_cb: Callable[[str, dict], None] | None = None

        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if username:
            self._client.username_pw_set(username, password)
        self._client.will_set(self.topic_availability, "offline", retain=True)
        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message    = self._on_message

    def set_command_callback(self, cb: Callable[[str, dict], None]) -> None:
        self._command_cb = cb

    def connect(self) -> None:
        logger.info("Connecting to MQTT broker %s:%s …", self._host, self._port)
        self._client.connect(self._host, self._port)
        self._client.loop_start()

    def disconnect(self) -> None:
        self.publish_availability(online=False)
        self._client.loop_stop()
        self._client.disconnect()

    def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        self._client.publish(topic, payload, retain=retain)

    def publish_availability(self, online: bool) -> None:
        status = "online" if online else "offline"
        logger.debug("Availability: %s", status)
        self._client.publish(self.topic_availability, status, retain=True)

    def publish_state(self, state: dict[str, Any]) -> None:
        payload = json.dumps(state)
        logger.debug("State published: %s", payload)
        self._client.publish(self.topic_state, payload, retain=True)

    # ------------------------------------------------------------------
    # paho callbacks
    # ------------------------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code == 0:
            logger.info("MQTT connected")
            client.subscribe(self.topic_command)
            self.publish_availability(online=True)
        else:
            logger.error("MQTT connect failed: %s", reason_code)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        logger.warning("MQTT disconnected (rc=%s) — paho will auto-reconnect", reason_code)

    def _on_message(self, client, userdata, msg) -> None:
        try:
            payload = json.loads(msg.payload)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON on %s: %s", msg.topic, msg.payload)
            return
        logger.debug("MQTT command received: %s", payload)
        if self._command_cb:
            self._command_cb(msg.topic, payload)
