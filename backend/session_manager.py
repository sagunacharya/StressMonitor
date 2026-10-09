"""
session_manager.py
===================
Bridges the ESP32 serial pipeline (receiver.py -> buffer.py ->
preprocessing.py -> motion.py -> ppg.py/eda.py -> feature_extraction.py)
to the web backend's START SESSION / STOP SESSION lifecycle.

Design:
  - The serial receiver thread is started ONCE at process startup and
    runs for the process's lifetime -- this is what "64 Hz acquisition
    is already fixed, do not break it" and "do not reintroduce blocking
    delays in ESP32" require: the ESP32 keeps streaming at full rate
    regardless of session state, exactly as buffer.py/receiver.py were
    already built to do.
  - What START/STOP actually toggles is whether incoming samples are fed
    into a *live* SlidingWindowBuffer + SessionInferenceState pair.
    Before START and after STOP, incoming samples are received (keeping
    the serial link alive and drained) but simply discarded -- never
    buffered, never windowed, never used for a prediction. This is what
    "do not use samples collected before START SESSION" and "on STOP
    SESSION, stop predictions and clear/reset the session" require.
  - On START SESSION: a brand-new buffer.SlidingWindowBuffer() and a
    brand-new inference.SessionInferenceState() are created. Since
    buffer.SlidingWindowBuffer starts empty by construction, this alone
    guarantees the fresh-buffer requirement -- there is no "clear()"
    method to call or forget to call; the old buffer object is simply
    dropped and a new one takes its place.
  - A background thread polls the live buffer for ready windows (same
    poll-loop pattern as the original main.py) and, for each one, runs
    the full existing quality-gate + feature-extraction chain untouched,
    then calls SessionInferenceState.predict(). Results are pushed out
    via the on_result callback (app.py wires this to the WebSocket
    broadcast + MySQL insert).
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
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

        self._model_bundle = inference.get_bundle()  # fail fast at startup

        self._lock = threading.Lock()
        self._active_buffer: Optional[buffer.SlidingWindowBuffer] = None
        self._active_inference: Optional[inference.SessionInferenceState] = None
        self.active_session_id: Optional[str] = None

        self._receiver = SerialReceiver(on_sample=self._handle_sample)
        self._receiver_thread = threading.Thread(
            target=self._run_receiver_with_status, daemon=True
        )
        self._processor_thread = threading.Thread(
            target=self._process_loop, daemon=True
        )
        self._started = False

    # -- lifecycle: process-level (once) -----------------------------------

    def start_background_threads(self) -> None:
        """Call once at Flask process startup. Starts the ESP32 serial
        link immediately (independent of any session) and the window-
        processing poll loop. Idempotent."""
        if self._started:
            return
        self._started = True
        self._receiver_thread.start()
        self._processor_thread.start()
        logger.info(
            "Serial receiver + window processor started "
            "(process-level, independent of session state)."
        )

    def _run_receiver_with_status(self) -> None:
        # receiver.run_forever() already logs connect/disconnect/reconnect
        # to stdout (per-line, per receiver.py's own design); we don't
        # duplicate that here, but we do want the frontend to know serial
        # health, so we track it via a lightweight polling flag instead
        # of threading it through receiver.py's internals.
        self._receiver.run_forever()

    @property
    def esp32_connected(self) -> bool:
        return bool(getattr(self._receiver, "_connected", False))

    # -- lifecycle: per-session (START/STOP) --------------------------------

    def start_session(self, session_id: str) -> None:
        """Creates a fresh buffer + fresh inference state and makes them
        the active target for incoming samples. Any samples that arrive
        between now and the first call to _handle_sample race only with
        this lock, never with stale state -- the old buffer/inference
        objects (if any) are simply replaced, not mutated."""
        with self._lock:
            self._active_buffer = buffer.SlidingWindowBuffer()
            self._active_inference = inference.SessionInferenceState(
                self._model_bundle
            )
            self.active_session_id = session_id
        logger.info(f"[Session] Started {session_id}: buffer + baseline reset.")
        self._on_status(
            {
                "type": "session_status",
                "session_id": session_id,
                "state": "collecting",
                "samples_needed": config.WINDOW_SIZE_SAMPLES,
            }
        )

    def stop_session(self) -> None:
        with self._lock:
            stopped_id = self.active_session_id
            self._active_buffer = None
            self._active_inference = None
            self.active_session_id = None
        if stopped_id:
            logger.info(f"[Session] Stopped {stopped_id}: buffer + state cleared.")
        self._on_status({"type": "session_status", "state": "ready"})

    # -- sample ingestion (always running, gated by active session) --------

    def _handle_sample(self, sample: dict) -> None:
        with self._lock:
            active = self._active_buffer
        if active is None:
            return  # no session running: sample intentionally discarded
        active.add_sample(sample)
        # Lightweight progress ping for the "Collecting: X / 3840 samples"
        # UI state. Cheap (just a counter read) so it's fine every sample
        # at 64 Hz; the frontend only needs it while collecting.
        collected = active.samples_collected_this_cycle()
        if collected is not None:
            self._on_status(
                {
                    "type": "collecting_progress",
                    "session_id": self.active_session_id,
                    "samples_collected": collected,
                    "samples_needed": config.WINDOW_SIZE_SAMPLES,
                }
            )

    # -- window processing loop ---------------------------------------------

    def _process_loop(self) -> None:
        while True:
            with self._lock:
                active_buf = self._active_buffer
                active_inf = self._active_inference
                session_id = self.active_session_id

            if active_buf is not None:
                window = active_buf.pop_ready_window()
                while window is not None:
                    self._process_one_window(window, active_inf, session_id)
                    # Re-check under lock in case STOP happened mid-drain.
                    with self._lock:
                        if self._active_buffer is not active_buf:
                            break
                        window = active_buf.pop_ready_window()

            time.sleep(1.0)  # matches original main.py poll cadence

    def _process_one_window(
        self,
        window: dict,
        active_inference: Optional[inference.SessionInferenceState],
        session_id: Optional[str],
    ) -> None:
        if active_inference is None:
            return  # session was stopped between pop and processing

        window_arrays = preprocessing.window_to_arrays(window)
        if window_arrays is None:
            return  # empty window, nothing to report (startup/reconnect gap)

        if not preprocessing.passes_quality_gate(window_arrays):
            logger.info("[Window] Rejected due to excessive motion.")
            self._on_result(
                {
                    "type": "window_rejected",
                    "session_id": session_id,
                    "reason": "excessive_motion",
                }
            )
            return

        try:
            sensor_row = feature_extraction.extract_window_features(window_arrays)
        except Exception as exc:  # noqa: BLE001 - never let one bad window
            logger.exception(f"[Error] Feature extraction failed: {exc}")
            self._on_result(
                {
                    "type": "window_error",
                    "session_id": session_id,
                    "reason": "feature_extraction_failed",
                    "detail": str(exc),
                }
            )
            return

        if sensor_row is None:
            logger.info(
                "[Window] Rejected: PPG/EDA signal quality unusable "
                "(insufficient peaks or degenerate signal)."
            )
            self._on_result(
                {
                    "type": "window_rejected",
                    "session_id": session_id,
                    "reason": "signal_quality",
                }
            )
            return

        try:
            feature_extraction.append_to_csv(sensor_row)
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[Error] Failed to write features to CSV: {exc}")
            # Non-fatal for the live pipeline: still proceed to predict
            # and broadcast, just log the CSV failure.

        try:
            prediction = active_inference.predict(sensor_row)
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[Error] Model inference failed: {exc}")
            self._on_result(
                {
                    "type": "window_error",
                    "session_id": session_id,
                    "reason": "inference_failed",
                    "detail": str(exc),
                }
            )
            return

        prediction["type"] = (
            "prediction" if prediction["status"] == "ok" else "warming_up"
        )
        prediction["session_id"] = session_id
        prediction["window_start"] = window_arrays["window_start"]
        self._on_result(prediction)
