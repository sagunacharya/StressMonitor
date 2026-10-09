"""Serial acquisition with validation, reconnects, and sampling diagnostics."""
import json
import math
import threading
import time
from typing import Callable, Optional

import serial
import config


class SerialReceiver:
    def __init__(self, on_sample: Callable[[dict], None], on_status: Optional[Callable[[dict], None]] = None):
        self._on_sample = on_sample
        self._on_status = on_status or (lambda _status: None)
        self._ser: Optional[serial.Serial] = None
        self._connected = False
        self._lock = threading.Lock()
        self._last_seq: Optional[int] = None
        self._last_device_ms: Optional[float] = None
        self._last_sample_monotonic: Optional[float] = None
        self._last_ppg_pair: Optional[tuple[float, float]] = None
        self._ppg_repeat_streak = 0
        self._last_quality_status: dict[str, float] = {}
        self._stats = {"connect_attempts": 0, "invalid_packets": 0, "status_packets": 0,
                       "samples_received": 0, "sequence_gaps": 0, "timing_gaps": 0,
                       "repeated_ppg_values": 0, "last_sample_rate_hz": None}

    @property
    def connected(self) -> bool:
        return self._connected

    def diagnostics(self) -> dict:
        with self._lock:
            return {**self._stats, "connected": self._connected}

    def _set_connected(self, value: bool) -> None:
        with self._lock:
            changed = self._connected != value
            self._connected = value
        if changed:
            self._on_status({"type": "esp32_status", "connected": value,
                             "state": "connected" if value else "disconnected"})

    def _emit_quality_status(self, reason: str, state: str, interval_sec: float = 2.0) -> None:
        now = time.monotonic()
        if now - self._last_quality_status.get(reason, 0.0) >= interval_sec:
            self._last_quality_status[reason] = now
            self._on_status({"type": "sensor_quality", "state": state, "reason": reason})

    def _open(self) -> bool:
        with self._lock:
            self._stats["connect_attempts"] += 1
        try:
            self._ser = serial.Serial(port=config.SERIAL_PORT, baudrate=config.SERIAL_BAUDRATE,
                                      timeout=config.SERIAL_TIMEOUT_SEC)
            self._last_seq = None
            self._last_device_ms = None
            self._last_sample_monotonic = None
            self._last_ppg_pair = None
            self._ppg_repeat_streak = 0
            self._set_connected(True)
            print(f"[Serial] Connected on {config.SERIAL_PORT} @ {config.SERIAL_BAUDRATE} baud.")
            return True
        except (serial.SerialException, OSError) as exc:
            self._set_connected(False)
            print(f"[Serial] Connection failed ({type(exc).__name__}); retrying.")
            return False

    def _close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except (serial.SerialException, OSError):
                pass
        self._ser = None
        self._set_connected(False)

    @staticmethod
    def _parse_line(raw_line: bytes) -> Optional[dict]:
        """Parse legacy or timestamped sample packets; reject malformed input."""
        try:
            text = raw_line.decode("utf-8", errors="strict").strip()
            if not text:
                return None
            data = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or data.get("status"):
            return None
        if not all(key in data for key in config.REQUIRED_KEYS):
            return None
        try:
            values = {key: float(data[key]) for key in config.REQUIRED_KEYS}
        except (TypeError, ValueError, OverflowError):
            return None
        if not all(math.isfinite(v) for v in values.values()):
            return None
        if not (0 <= values["ir"] <= config.MAX_PPG_RAW and 0 <= values["red"] <= config.MAX_PPG_RAW):
            return None
        # IR is the PPG channel used by feature extraction. Zero means no
        # usable PPG input; max code values indicate likely ADC saturation.
        if values["ir"] <= 0 or values["ir"] >= config.MAX_PPG_RAW or values["red"] >= config.MAX_PPG_RAW:
            return None
        if not (0 <= values["accMagnitude"] <= config.MAX_ACCEL_MAG_G):
            return None
        if not (0 <= values["eda"] <= config.EDA_ADC_MAX):
            return None
        sample = {**values, "t_recv": time.time()}
        if "seq" in data:
            if isinstance(data["seq"], bool) or not isinstance(data["seq"], int) or data["seq"] < 0:
                return None
            sample["seq"] = data["seq"]
        if "t_ms" in data:
            try:
                device_ms = float(data["t_ms"])
            except (TypeError, ValueError, OverflowError):
                return None
            if not math.isfinite(device_ms) or device_ms < 0:
                return None
            sample["t_device_ms"] = device_ms
        return sample

    def _annotate_timing(self, sample: dict) -> dict:
        """Mark sequence/timestamp gaps so downstream code resets partial windows."""
        gap = False
        if "seq" in sample and self._last_seq is not None:
            if sample["seq"] != self._last_seq + 1:
                gap = True
                with self._lock:
                    self._stats["sequence_gaps"] += max(1, sample["seq"] - self._last_seq - 1)
        if "t_device_ms" in sample and self._last_device_ms is not None:
            delta = sample["t_device_ms"] - self._last_device_ms
            if delta <= 0 or delta > config.MAX_DEVICE_GAP_MS:
                gap = True
                with self._lock:
                    self._stats["timing_gaps"] += 1
        ppg_pair = (float(sample["ir"]), float(sample["red"]))
        if self._last_ppg_pair is not None and ppg_pair == self._last_ppg_pair:
            self._ppg_repeat_streak += 1
            with self._lock:
                self._stats["repeated_ppg_values"] += 1
            if self._ppg_repeat_streak >= 3:
                gap = True
                self._emit_quality_status("repeated_ppg_values", "stale_ppg_data")
        else:
            self._ppg_repeat_streak = 0
        self._last_ppg_pair = ppg_pair

        now_mono = time.monotonic()
        if self._last_sample_monotonic is not None:
            delta = now_mono - self._last_sample_monotonic
            if delta > config.MAX_DEVICE_GAP_MS / 1000.0:
                # Host scheduling/USB buffering can be jittery, so this is a
                # diagnostic only unless firmware timestamps are present.
                if "t_device_ms" in sample:
                    gap = True
        self._last_sample_monotonic = now_mono
        self._last_seq = sample.get("seq", self._last_seq)
        self._last_device_ms = sample.get("t_device_ms", self._last_device_ms)
        sample["gap_before"] = gap
        with self._lock:
            self._stats["samples_received"] += 1
        return sample

    def run_forever(self) -> None:
        while True:
            if not self._connected:
                if not self._open():
                    time.sleep(config.RECONNECT_DELAY_SEC)
                    continue
            try:
                raw_line = self._ser.readline() if self._ser else b""
                if not raw_line:
                    continue
                # Device-side rate diagnostics are JSON but are not samples.
                try:
                    decoded = json.loads(raw_line.decode("utf-8", errors="strict").strip())
                except (UnicodeDecodeError, json.JSONDecodeError):
                    decoded = None
                if isinstance(decoded, dict) and decoded.get("status"):
                    with self._lock:
                        self._stats["status_packets"] += 1
                        if decoded.get("status") == "rate":
                            try:
                                rate = float(decoded.get("hz"))
                                self._stats["last_sample_rate_hz"] = rate if math.isfinite(rate) else None
                            except (TypeError, ValueError):
                                pass
                    continue
                sample = self._parse_line(raw_line)
                if sample is None:
                    with self._lock:
                        self._stats["invalid_packets"] += 1
                    if isinstance(decoded, dict):
                        try:
                            ir_value = float(decoded.get("ir"))
                            red_value = float(decoded.get("red"))
                            if math.isfinite(ir_value) and ir_value <= 0:
                                self._emit_quality_status("missing_ppg_data", "missing_ppg_data")
                            elif ((math.isfinite(ir_value) and ir_value >= config.MAX_PPG_RAW) or
                                  (math.isfinite(red_value) and red_value >= config.MAX_PPG_RAW)):
                                self._emit_quality_status("ppg_saturated", "ppg_saturated")
                        except (TypeError, ValueError, OverflowError):
                            pass
                    continue
                self._on_sample(self._annotate_timing(sample))
            except (serial.SerialException, OSError) as exc:
                print(f"[Serial] Read error ({type(exc).__name__}); reconnecting.")
                self._close()
                time.sleep(config.RECONNECT_DELAY_SEC)
