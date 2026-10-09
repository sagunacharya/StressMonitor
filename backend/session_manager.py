"""Coordinates one physical serial stream with one authenticated live session."""
from __future__ import annotations
import logging
import queue
import threading
import time
from typing import Callable, Optional

import buffer
import config
import feature_extraction
import inference
import preprocessing
from receiver import SerialReceiver

logger = logging.getLogger("session_manager")
OnResult = Callable[[dict], None]
OnStatus = Callable[[dict], None]


class SessionManager:
    def __init__(self, on_result: OnResult, on_status: Optional[OnStatus] = None):
        self._on_result = on_result
        self._on_status = on_status or (lambda _status: None)
        self._model_bundle = inference.get_bundle()
        self._lock = threading.RLock()
        # Serial ingestion uses _lock only. Long database/broadcast callbacks
        # serialize against stop/start via a separate lock so acquisition is
        # not blocked by persistence latency.
        self._dispatch_lock = threading.RLock()
        self._status_queue: queue.Queue[dict] = queue.Queue(maxsize=256)
        self._last_gap_status_at = 0.0
        self._active_buffer: Optional[buffer.SlidingWindowBuffer] = None
        self._active_inference: Optional[inference.SessionInferenceState] = None
        self.active_session_id: Optional[str] = None
        self.active_user_id: Optional[int] = None
        self._last_progress_at = 0.0
        self._receiver = SerialReceiver(on_sample=self._handle_sample, on_status=self._on_status_update)
        self._receiver_thread = threading.Thread(target=self._receiver.run_forever, daemon=True, name="serial-receiver")
        self._processor_thread = threading.Thread(target=self._process_loop, daemon=True, name="window-processor")
        self._status_thread = threading.Thread(target=self._status_loop, daemon=True, name="status-dispatcher")
        self._started = False

    @property
    def esp32_connected(self) -> bool:
        return self._receiver.connected

    def serial_diagnostics(self) -> dict:
        return self._receiver.diagnostics()

    def _queue_status(self, status: dict) -> None:
        """Bound status memory and never block the serial acquisition callback."""
        try:
            self._status_queue.put_nowait(status)
        except queue.Full:
            # Preserve newer state transitions by evicting the oldest queued
            # item. The queue only carries UI status, never sensor samples.
            try:
                self._status_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._status_queue.put_nowait(status)
            except queue.Full:
                logger.warning("Status queue full; dropping a UI status event")

    def _status_loop(self) -> None:
        while True:
            status = self._status_queue.get()
            try:
                self._on_status(status)
            except Exception:
                logger.exception("Status dispatch failed")
            finally:
                self._status_queue.task_done()

    def _on_status_update(self, status: dict) -> None:
        # A serial disconnect invalidates any partial window. The receiver resets
        # sequence tracking on reconnect, so the first post-reconnect packet may
        # not carry a gap marker by itself.
        if status.get("type") == "esp32_status" and status.get("connected") is False:
            with self._lock:
                active = self._active_buffer
                session_id = self.active_session_id
                user_id = self.active_user_id
                if active is not None:
                    active.reset()
            if active is not None and session_id is not None and user_id is not None:
                self._queue_status({"type": "sampling_gap", "session_id": session_id,
                                    "user_id": user_id, "reason": "serial_disconnected"})

        # Connection health is global. Data-quality diagnostics are associated
        # with the active session owner and are not broadcast to other users.
        if status.get("type") == "sensor_quality" and not status.get("user_id"):
            with self._lock:
                if self.active_session_id is None or self.active_user_id is None:
                    return
                status = {**status, "session_id": self.active_session_id,
                          "user_id": self.active_user_id}
        self._queue_status(status)

    def start_background_threads(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._receiver_thread.start()
            self._processor_thread.start()
            self._status_thread.start()
        logger.info("Serial receiver and window processor started")

    def start_session(self, session_id: str, user_id: int) -> None:
        with self._dispatch_lock:
            with self._lock:
                if self.active_session_id is not None:
                    raise RuntimeError("A physical sensor session is already active")
                self._active_buffer = buffer.SlidingWindowBuffer()
                self._active_inference = inference.SessionInferenceState(self._model_bundle)
                self.active_session_id = session_id
                self.active_user_id = int(user_id)
                self._last_progress_at = 0.0
            self._queue_status({"type": "session_status", "session_id": session_id,
                                "user_id": int(user_id), "state": "collecting",
                                "samples_needed": config.WINDOW_SIZE_SAMPLES})

    def dispatch_guard(self):
        """Return the re-entrant result-dispatch lock for atomic lifecycle work.

        Callers may hold this around a database stop/commit, then invoke
        stop_session() re-entrantly. Existing callbacks finish before the DB
        stop; callbacks waiting behind the guard are rejected after state clear.
        """
        return self._dispatch_lock

    def stop_session(self, expected_session_id: Optional[str] = None) -> Optional[str]:
        with self._dispatch_lock:
            with self._lock:
                if expected_session_id and self.active_session_id != expected_session_id:
                    return None
                stopped_id, user_id = self.active_session_id, self.active_user_id
                self._active_buffer = None
                self._active_inference = None
                self.active_session_id = None
                self.active_user_id = None
                self._last_progress_at = 0.0
            if stopped_id:
                self._queue_status({"type": "session_status", "session_id": stopped_id,
                                    "user_id": user_id, "state": "ready"})
            return stopped_id

    def current_session_info(self) -> Optional[dict]:
        with self._lock:
            if self.active_session_id is None:
                return None
            return {"session_id": self.active_session_id, "user_id": self.active_user_id}

    def status_snapshot(self, user_id: int) -> Optional[dict]:
        """Return live acquisition/calibration phase for the authorized owner."""
        with self._lock:
            if (self.active_session_id is None or self.active_user_id != int(user_id)
                    or self._active_buffer is None or self._active_inference is None):
                return None
            session_id = self.active_session_id
            buffered = self._active_buffer.samples_collected_this_cycle()
            inference_state = self._active_inference.snapshot()
        if inference_state["calibration_failed"]:
            phase = "calibration_failed"
        elif inference_state["calibration_ready"]:
            phase = "prediction_available"
        elif inference_state["windows_seen"] > 0:
            phase = "calibrating"
        else:
            phase = "collecting"
        return {
            "type": "session_status", "session_id": session_id,
            "state": phase, "samples_collected": buffered,
            "samples_needed": config.WINDOW_SIZE_SAMPLES,
            "windows_seen": inference_state["windows_seen"],
            "windows_needed": inference_state["windows_needed"],
        }

    def _handle_sample(self, sample: dict) -> None:
        with self._lock:
            active = self._active_buffer
            session_id = self.active_session_id
            user_id = self.active_user_id
            if active is None or session_id is None:
                return
            gap_reason = None
            if sample.get("gap_before"):
                active.reset()
                gap_reason = "sample_gap"
            overflow = active.add_sample(sample)
            if overflow:
                gap_reason = "processor_overrun"
            now = time.monotonic()
            if gap_reason and now - self._last_gap_status_at >= 1.0:
                self._last_gap_status_at = now
                self._queue_status({"type": "sampling_gap", "session_id": session_id,
                                    "user_id": user_id, "reason": gap_reason})
            if now - self._last_progress_at >= 0.5:
                self._last_progress_at = now
                collected = active.samples_collected_this_cycle()
                self._queue_status({"type": "collecting_progress", "session_id": session_id,
                                    "user_id": user_id, "samples_collected": collected,
                                    "samples_needed": config.WINDOW_SIZE_SAMPLES})

    def _emit_for_session(self, payload: dict, active_buf, active_inf, session_id, generation: int) -> None:
        # Stop/start hold the dispatch lock while changing session state. The
        # callback may perform a DB write, but the serial sample path never
        # waits on this lock and continues draining USB packets.
        with self._dispatch_lock:
            with self._lock:
                if (self._active_buffer is not active_buf or self._active_inference is not active_inf
                        or self.active_session_id != session_id or active_buf.generation != generation):
                    return
                payload["session_id"] = session_id
                payload["user_id"] = self.active_user_id
            self._on_result(payload)

    def _process_loop(self) -> None:
        while True:
            with self._lock:
                active_buf = self._active_buffer
                active_inf = self._active_inference
                session_id = self.active_session_id
            if active_buf is not None and active_inf is not None and session_id is not None:
                window = active_buf.pop_ready_window()
                while window is not None:
                    self._process_one_window(window, active_buf, active_inf, session_id)
                    with self._lock:
                        if self._active_buffer is not active_buf or self.active_session_id != session_id:
                            break
                    window = active_buf.pop_ready_window()
            time.sleep(0.1)

    def _process_one_window(self, window: dict, active_buf, active_inf, session_id: str) -> None:
        generation = window["generation"]
        def emit(payload):
            self._emit_for_session(payload, active_buf, active_inf, session_id, generation)

        arrays = preprocessing.window_to_arrays(window)
        if arrays is None:
            emit({"type": "window_rejected", "reason": "insufficient_or_invalid_data"})
            return
        if not preprocessing.passes_quality_gate(arrays):
            emit({"type": "window_rejected", "reason": "excessive_motion"})
            return
        try:
            sensor_row = feature_extraction.extract_window_features(arrays)
        except Exception:
            logger.exception("Feature extraction failed")
            emit({"type": "window_error", "reason": "feature_extraction_failed"})
            return
        if sensor_row is None:
            emit({"type": "window_rejected", "reason": "signal_quality"})
            return
        if config.ENABLE_FEATURE_CSV_LOG:
            try:
                feature_extraction.append_to_csv(sensor_row)
            except OSError:
                # Optional diagnostic logging must never suppress a valid live
                # model inference, and failure does not change feature values.
                logger.exception("Optional feature CSV logging failed")
        try:
            result = active_inf.predict(sensor_row)
        except Exception:
            logger.exception("Inference failed")
            emit({"type": "window_error", "reason": "inference_failed"})
            return
        result_type = {
            "ok": "prediction", "warming_up": "warming_up",
            "calibration_failed": "calibration_failed",
        }.get(result.get("status"), "window_error")
        result["type"] = result_type
        result["window_start"] = arrays["window_start"]
        emit(result)
