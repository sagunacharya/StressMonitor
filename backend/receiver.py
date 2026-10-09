"""
receiver.py
===========
Owns the serial connection to the ESP32-S3. Reads newline-delimited JSON,
validates each packet, and hands clean samples to the caller via a callback.
Handles disconnects with an automatic reconnect loop.
"""

import json
import time
from typing import Callable, Optional

import serial

import config


class SerialReceiver:
    """
    Wraps a pyserial connection with auto-reconnect and safe JSON parsing.

    Usage:
        receiver = SerialReceiver(on_sample=buffer.add_sample)
        receiver.run_forever()  # blocks; call from main loop or a thread
    """

    def __init__(self, on_sample: Callable[[dict], None]):
        self._on_sample = on_sample
        self._ser: Optional[serial.Serial] = None
        self._connected = False

    # -- connection management -------------------------------------------------

    def _open(self) -> bool:
        """Attempt to open the serial port. Returns True on success."""
        try:
            self._ser = serial.Serial(
                port=config.SERIAL_PORT,
                baudrate=config.SERIAL_BAUDRATE,
                timeout=config.SERIAL_TIMEOUT_SEC,
            )
            self._connected = True
            print(f"[Serial] Connected on {config.SERIAL_PORT} "
                  f"@ {config.SERIAL_BAUDRATE} baud.")
            return True
        except serial.SerialException as exc:
            self._connected = False
            print(f"[Serial] Connection failed: {exc}. "
                  f"Retrying in {config.RECONNECT_DELAY_SEC:.1f}s...")
            return False

    def _close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except serial.SerialException:
                pass
        if self._connected:
            print("[Serial] Disconnected.")
        self._connected = False

    # -- parsing -----------------------------------------------------------

    @staticmethod
    def _parse_line(raw_line: bytes) -> Optional[dict]:
        """
        Decode + validate one line as a sample dict. Returns None (and
        silently swallows the error) if the line is malformed in any way.
        This is intentional: a single garbled packet must never crash the
        pipeline or spam the terminal.
        """
        try:
            text = raw_line.decode("utf-8", errors="strict").strip()
            if not text:
                return None
            data = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

        if not isinstance(data, dict):
            return None

        if not all(key in data for key in config.REQUIRED_KEYS):
            return None

        try:
            sample = {
                "ir": float(data["ir"]),
                "red": float(data["red"]),
                "accMagnitude": float(data["accMagnitude"]),
                "eda": float(data["eda"]),
                "t_recv": time.time(),  # host-side arrival timestamp
            }
        except (TypeError, ValueError):
            return None

        return sample

    # -- main loop -----------------------------------------------------------

    def run_forever(self) -> None:
        """
        Blocking loop: connect, read lines, reconnect on failure, repeat.
        Intended to run for the lifetime of the program.
        """
        while True:
            if not self._connected:
                if not self._open():
                    time.sleep(config.RECONNECT_DELAY_SEC)
                    continue

            try:
                raw_line = self._ser.readline()
                if not raw_line:
                    # timeout with no data; loop again (allows Ctrl+C checks)
                    continue

                sample = self._parse_line(raw_line)
                if sample is not None:
                    self._on_sample(sample)
                # malformed lines are silently ignored, per spec

            except (serial.SerialException, OSError) as exc:
                print(f"[Serial] Read error: {exc}")
                self._close()
                time.sleep(config.RECONNECT_DELAY_SEC)
