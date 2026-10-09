"""
utils.py
========
Small shared helpers used by more than one signal-processing module.
Keeping these here avoids duplicating filter-design code between ppg.py
and eda.py.
"""

import numpy as np
from scipy.signal import butter, filtfilt


def butter_bandpass_filter(
    signal: np.ndarray,
    low_hz: float,
    high_hz: float,
    fs: float,
    order: int,
) -> np.ndarray:
    """Zero-phase Butterworth bandpass filter via filtfilt."""
    signal = np.asarray(signal, dtype=float)
    if signal.ndim != 1 or signal.size < 4 or not np.all(np.isfinite(signal)):
        raise ValueError("Filter input must be a finite one-dimensional signal with at least 4 samples")
    if not (0 < low_hz < high_hz < fs / 2):
        raise ValueError("Bandpass cutoffs must satisfy 0 < low < high < Nyquist")
    nyq = 0.5 * fs
    low = low_hz / nyq
    high = high_hz / nyq
    b, a = butter(order, [low, high], btype="band")
    return filtfilt(b, a, signal)


def butter_lowpass_filter(
    signal: np.ndarray,
    cutoff_hz: float,
    fs: float,
    order: int,
) -> np.ndarray:
    """Zero-phase Butterworth low-pass filter via filtfilt."""
    signal = np.asarray(signal, dtype=float)
    if signal.ndim != 1 or signal.size < 4 or not np.all(np.isfinite(signal)):
        raise ValueError("Filter input must be a finite one-dimensional signal with at least 4 samples")
    if not (0 < cutoff_hz < fs / 2):
        raise ValueError("Low-pass cutoff must be below Nyquist and above zero")
    nyq = 0.5 * fs
    normal_cutoff = cutoff_hz / nyq
    b, a = butter(order, normal_cutoff, btype="low")
    return filtfilt(b, a, signal)


def zscore_clip_outliers(values: np.ndarray, z_thresh: float) -> np.ndarray:
    """
    Replace samples whose |z-score| exceeds z_thresh with the nearest
    in-bound neighbor's value (simple hold-last-valid clipping). Returns a
    new array; does not mutate the input.
    """
    values = values.astype(float).copy()
    std = values.std()
    if std == 0 or not np.isfinite(std):
        return values

    mean = values.mean()
    z = (values - mean) / std
    bad = np.abs(z) > z_thresh

    if not bad.any():
        return values

    good_idx = np.where(~bad)[0]
    if good_idx.size == 0:
        return values  # everything flagged - leave untouched, caller decides

    for i in np.where(bad)[0]:
        nearest = good_idx[np.argmin(np.abs(good_idx - i))]
        values[i] = values[nearest]

    return values
