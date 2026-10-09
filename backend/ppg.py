"""
ppg.py
======
PPG preprocessing and HRV feature extraction.

Pipeline: remove DC -> detrend -> 3rd-order Butterworth bandpass (0.5-5 Hz)
-> normalize -> BVP -> find_peaks -> HR / SDNN / RMSSD.

The ESP32 sends raw "ir" and "red" channels; "ir" is used as the PPG signal
(standard choice - IR has better perfusion signal quality than red for most
reflective PPG sensors).
"""

from typing import Optional

import numpy as np
from scipy.signal import detrend, find_peaks

import config
import utils


def _remove_dc(signal: np.ndarray) -> np.ndarray:
    """Subtract the mean to remove the DC offset."""
    return signal - np.mean(signal)


def _normalize(signal: np.ndarray) -> np.ndarray:
    """Zero-mean, unit-variance normalization. Guards against zero std."""
    std = np.std(signal)
    if std == 0 or not np.isfinite(std):
        return signal - np.mean(signal)
    return (signal - np.mean(signal)) / std


def compute_bvp(ir_signal: np.ndarray, fs: float) -> Optional[np.ndarray]:
    """
    Runs the full PPG cleaning chain and returns the normalized BVP signal.
    Returns None if the signal is degenerate (too short/flat to filter).
    """
    if ir_signal.size < 4:
        return None

    dc_removed = _remove_dc(ir_signal)
    detrended = detrend(dc_removed, type="linear")

    try:
        bandpassed = utils.butter_bandpass_filter(
            detrended,
            low_hz=config.PPG_BANDPASS_LOW_HZ,
            high_hz=config.PPG_BANDPASS_HIGH_HZ,
            fs=fs,
            order=config.PPG_FILTER_ORDER,
        )
    except ValueError:
        # e.g. fs too low for the requested band -> unusable window
        return None

    bvp = _normalize(bandpassed)

    if not np.all(np.isfinite(bvp)):
        return None

    return bvp


def detect_peaks(bvp: np.ndarray, fs: float) -> np.ndarray:
    """Detect heartbeat peaks in the BVP signal via scipy.signal.find_peaks."""
    min_distance_samples = max(
        1, int(round(config.PPG_PEAK_MIN_DISTANCE_SEC * fs))
    )
    peaks, _ = find_peaks(
        bvp,
        distance=min_distance_samples,
        prominence=config.PPG_PEAK_PROMINENCE,
    )
    return peaks


def compute_hrv_features(peaks: np.ndarray, fs: float) -> Optional[dict]:
    """
    Computes HR (bpm), SDNN (ms), RMSSD (ms) from detected peak indices.
    Returns None if too few peaks exist for a meaningful IBI series, or if
    the resulting HR falls outside physiological bounds.
    """
    if peaks.size < 3:
        return None  # need at least 2 IBIs for SDNN/RMSSD

    ibi_sec = np.diff(peaks) / fs
    ibi_ms = ibi_sec * 1000.0

    # Filter IBIs to a physiologically plausible range before computing
    # summary stats, to avoid a single spurious peak wrecking the window.
    min_ibi_ms = 60000.0 / config.HR_MAX_BPM
    max_ibi_ms = 60000.0 / config.HR_MIN_BPM
    valid_mask = (ibi_ms >= min_ibi_ms) & (ibi_ms <= max_ibi_ms)
    ibi_ms = ibi_ms[valid_mask]

    if ibi_ms.size < 2:
        return None

    mean_ibi_ms = np.mean(ibi_ms)
    hr_bpm = 60000.0 / mean_ibi_ms

    if not (config.HR_MIN_BPM <= hr_bpm <= config.HR_MAX_BPM):
        return None

    sdnn = float(np.std(ibi_ms, ddof=1)) if ibi_ms.size >= 2 else 0.0

    successive_diffs = np.diff(ibi_ms)
    rmssd = (
        float(np.sqrt(np.mean(successive_diffs ** 2)))
        if successive_diffs.size >= 1
        else 0.0
    )

    return {"HR": float(hr_bpm), "SDNN": sdnn, "RMSSD": rmssd}


def extract_ppg_features(ir_values: np.ndarray, fs: float) -> Optional[dict]:
    """
    End-to-end PPG feature extraction for one window. Returns a dict with
    HR / SDNN / RMSSD, or None if the window's PPG quality was unusable.
    """
    bvp = compute_bvp(ir_values, fs)
    if bvp is None:
        return None

    peaks = detect_peaks(bvp, fs)
    return compute_hrv_features(peaks, fs)
