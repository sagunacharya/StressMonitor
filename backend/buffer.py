"""Thread-safe bounded sliding-window buffer with explicit overrun handling."""
from collections import deque
import threading
from typing import Optional
import config


class SlidingWindowBuffer:
    """A FIFO which emits a full window and advances by exactly the step."""
    def __init__(self):
        self._samples: deque[dict] = deque()
        self._lock = threading.RLock()
        self._generation = 0
        self._total_added = 0
        self._total_emitted = 0
        self._capacity = config.WINDOW_SIZE_SAMPLES + config.WINDOW_STEP_SAMPLES

    def add_sample(self, sample: dict) -> bool:
        """Append a sample. Returns True if overrun forced a safe reset."""
        with self._lock:
            overflow = len(self._samples) >= self._capacity
            if overflow:
                # Never silently discard the oldest values and then pretend
                # the remaining samples formed a contiguous uniform window.
                self._samples.clear()
                self._generation += 1
                self._total_added = 0
                self._total_emitted = 0
            self._samples.append(dict(sample))
            self._total_added += 1
            return overflow

    def pop_ready_window(self) -> Optional[dict]:
        with self._lock:
            if len(self._samples) < config.WINDOW_SIZE_SAMPLES:
                return None
            samples = list(self._samples)[:config.WINDOW_SIZE_SAMPLES]
            result = {
                "window_start": samples[0]["t_recv"],
                "samples": samples,
                "sample_seq_start": samples[0].get("seq"),
                "sample_seq_end": samples[-1].get("seq"),
                "generation": self._generation,
            }
            for _ in range(config.WINDOW_STEP_SAMPLES):
                self._samples.popleft()
            self._total_emitted += 1
            return result

    def samples_collected_this_cycle(self) -> int:
        with self._lock:
            return min(len(self._samples), config.WINDOW_SIZE_SAMPLES)

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self._total_added = 0
            self._total_emitted = 0
            self._generation += 1

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def buffered_samples(self) -> int:
        with self._lock:
            return len(self._samples)

    @property
    def total_added(self) -> int:
        with self._lock:
            return self._total_added

    @property
    def total_emitted(self) -> int:
        with self._lock:
            return self._total_emitted
