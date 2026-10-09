"""
buffer.py
=========
Sample-count-based sliding-window buffer. Accumulates incoming samples and
yields a complete WINDOW_SIZE_SAMPLES-sample window every WINDOW_STEP_SAMPLES
samples (1920/3840 = 50% overlap at the fixed 64 Hz system rate). Windowing
is driven purely by sample count, never by elapsed wall-clock time.
"""

from collections import deque
from typing import Optional

import config


class SlidingWindowBuffer:
    """
    Not thread-safe by design: intended to be driven from a single reader
    loop (see main.py), which calls add_sample() and periodically checks
    for a ready window via pop_ready_window().
    """

    def __init__(self):
        self._samples: deque = deque()
        # Count of samples already consumed (shifted past) by prior windows.
        # The next window starts at this offset into the conceptual
        # (infinite) sample stream.
        self._consumed_count: int = 0

    def add_sample(self, sample: dict) -> None:
        """Append one validated sample: {"ir","red","accMagnitude","eda","t_recv"}."""
        self._samples.append(sample)
        self._drop_stale_samples()

    def _drop_stale_samples(self) -> None:
        """
        Discard samples that fall entirely before the next window's start,
        i.e. samples already shifted past by pop_ready_window(). Keeps
        memory bounded to roughly one window's worth of lookback.
        """
        while len(self._samples) > 0 and self._consumed_count > 0:
            # _consumed_count tracks how many leading samples in the
            # current deque are stale (already shifted past). Drop them
            # one-for-one; this stays O(1) amortized across calls.
            self._samples.popleft()
            self._consumed_count -= 1

    def pop_ready_window(self) -> Optional[dict]:
        """
        Returns a window dict {"window_start": t, "samples": [...]} once
        WINDOW_SIZE_SAMPLES samples are available, else None. Advances the
        internal cursor by WINDOW_STEP_SAMPLES samples after each yield.
        window_start is the wall-clock t_recv of the window's first sample
        (kept only for CSV/reporting timestamps - not used in any windowing
        or fs-dependent computation).
        """
        if len(self._samples) < config.WINDOW_SIZE_SAMPLES:
            return None

        window_samples = list(self._samples)[: config.WINDOW_SIZE_SAMPLES]
        window_start = window_samples[0]["t_recv"]

        # Shift the cursor by WINDOW_STEP_SAMPLES samples for the next call.
        self._consumed_count += config.WINDOW_STEP_SAMPLES

        if len(window_samples) < config.MIN_SAMPLES_PER_WINDOW:
            # Unreachable given the size check above (WINDOW_SIZE_SAMPLES >
            # MIN_SAMPLES_PER_WINDOW always), kept as a defensive guard to
            # preserve the empty-window contract main.py already relies on.
            return {"window_start": window_start, "samples": []}

        return {"window_start": window_start, "samples": window_samples}

    def samples_collected_this_cycle(self) -> int:
        """
        Read-only progress counter for the frontend's "Collecting data:
        X / 3840 samples" state. Does NOT affect windowing: purely a
        len(self._samples) read, capped at WINDOW_SIZE_SAMPLES for
        display (the deque briefly holds up to one extra sample beyond
        that before pop_ready_window() is next called, which would
        otherwise flash "3841 / 3840" for one sample's worth of time).
        Added for the web UI; add_sample()/pop_ready_window()/
        _drop_stale_samples() above are the original, unmodified
        windowing logic and do not call this method.
        """
        return min(len(self._samples), config.WINDOW_SIZE_SAMPLES)
