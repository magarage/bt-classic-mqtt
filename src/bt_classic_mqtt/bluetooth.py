"""
Bluetooth RFCOMM connection manager.

Responsibilities:
  - Open / close the raw RFCOMM socket
  - Perform the model-specific handshake via init_sequence()
  - Provide thread-safe send() / recv()

Everything here is I/O only — no business logic.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bt_classic_mqtt.model import DeviceModel

logger = logging.getLogger(__name__)

_RFCOMM_CHANNEL = 1
_RECV_TIMEOUT   = 5.0
_HANDSHAKE_WAIT = 0.5


class BTConnectionError(OSError):
    pass


class BTConnection:
    """Thread-safe Bluetooth RFCOMM connection."""

    def __init__(self, mac: str, model: "DeviceModel") -> None:
        self._mac   = mac
        self._model = model
        self._sock: socket.socket | None = None
        self._lock  = threading.Lock()

    @property
    def is_connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> None:
        if self._sock:
            return
        logger.info("Connecting to %s …", self._mac)
        try:
            sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
            sock.settimeout(_RECV_TIMEOUT)
            sock.connect((self._mac, _RFCOMM_CHANNEL))
        except OSError as exc:
            raise BTConnectionError(f"Failed to connect to {self._mac}: {exc}") from exc
        self._sock = sock
        logger.info("Connected to %s", self._mac)
        self._handshake()

    def _handshake(self) -> None:
        logger.debug("Starting handshake …")
        for pkt in self._model.init_sequence():
            self._sock.send(pkt)
        time.sleep(_HANDSHAKE_WAIT)
        logger.debug("Handshake complete")

    def close(self) -> None:
        with self._lock:
            if self._sock:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None
                logger.info("Disconnected from %s", self._mac)

    def send(self, data: bytes) -> None:
        with self._lock:
            if not self._sock:
                raise OSError("Not connected")
            self._sock.send(data)

    def recv(self, bufsize: int = 256) -> bytes:
        if not self._sock:
            raise OSError("Not connected")
        return self._sock.recv(bufsize)
