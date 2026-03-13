"""
bt-classic-mqtt — entry point.

Configuration via environment variables:
  BT_MAC          Bluetooth MAC address of the device (required)
  CONFIG          Path to the device model.yaml file (required)
                  e.g. devices/yamaha-yas-207/model.yaml
  MQTT_HOST       MQTT broker host (required)
  MQTT_PORT       MQTT broker port (default: 1883)
  MQTT_USERNAME   MQTT username (optional)
  MQTT_PASSWORD   MQTT password (optional)
  LOG_LEVEL       Logging level (default: INFO)
"""

from __future__ import annotations

import logging
import os
import signal
import sys
from pathlib import Path

from bt_classic_mqtt.bluetooth import BTConnection
from bt_classic_mqtt.controller import Controller
from bt_classic_mqtt.mqtt import MQTTClient
from bt_classic_mqtt.yaml_model import YamlDeviceModel


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"ERROR: environment variable {name} is required", file=sys.stderr)
        sys.exit(1)
    return value


def main() -> None:
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    bt_mac      = _require("BT_MAC")
    config_path = _require("CONFIG")
    mqtt_host   = _require("MQTT_HOST")
    mqtt_port   = int(os.environ.get("MQTT_PORT", "1883"))
    mqtt_user   = os.environ.get("MQTT_USERNAME") or None
    mqtt_pass   = os.environ.get("MQTT_PASSWORD") or None

    try:
        model = YamlDeviceModel.from_config(Path(config_path))
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    mqtt = MQTTClient(
        host=mqtt_host,
        port=mqtt_port,
        topic_prefix=model.mqtt_topic_prefix,
        username=mqtt_user,
        password=mqtt_pass,
    )
    bt         = BTConnection(mac=bt_mac, model=model)
    controller = Controller(bt=bt, mqtt=mqtt, model=model)

    def _shutdown(signum, frame):
        logging.getLogger(__name__).info("Signal %s received — shutting down", signum)
        controller.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    controller.start()
    signal.pause()


if __name__ == "__main__":
    main()
