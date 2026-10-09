"""
preprocessing.py
=================
Window-level orchestration layer, sitting between buffer.py (raw samples)
and the per-signal modules (ppg.py, eda.py, motion.py). Converts a raw
window (list of sample dicts) into aligned NumPy arrays at the fixed
system sampling rate, then runs the motion-quality gate before any
expensive signal processing happens.

RED channel is parsed on ingest (receiver.py) but is not carried past this
point: PPG preprocessing and HR/HRV feature extraction use IR only.
"""

from typing import Optional

import numpy as np

import config
import motion


def window_to_arrays(window: dict) -> Optional[dict]:
    """
    Converts a raw window {"window_start", "samples": [...]} into a dict of
    NumPy arrays at config.SAMPLE_RATE_HZ. Returns None if the window has
    no samples at all.
    """
    samples = window["samples"]
    if len(samples) != config.WINDOW_SIZE_SAMPLES:
        return None
    try:
        ir = np.array([s["ir"] for s in samples], dtype=float)
        acc_magnitude = np.array([s["accMagnitude"] for s in samples], dtype=float)
        eda_adc = np.array([s["eda"] for s in samples], dtype=float)
    except (KeyError, TypeError, ValueError):
        return None
    if not (np.all(np.isfinite(ir)) and np.all(np.isfinite(acc_magnitude)) and np.all(np.isfinite(eda_adc))):
        return None
    if np.any(eda_adc < 0) or np.any(eda_adc > config.EDA_ADC_MAX):
        return None
    if np.any(acc_magnitude < 0) or np.any(acc_magnitude > config.MAX_ACCEL_MAG_G):
        return None


    return {
        "window_start": window["window_start"],
        "ir": ir,
        "acc_magnitude": acc_magnitude,
        "eda_adc": eda_adc,
        "fs": config.SAMPLE_RATE_HZ,
    }


def passes_quality_gate(window_arrays: dict) -> bool:
    """
    Runs the motion-only quality check (accMagnitude). Also rejects windows
    with too few samples to process meaningfully, since a sparse window
    would produce unreliable HRV/EDA statistics regardless of motion.
    """
    if window_arrays["ir"].size < config.MIN_SAMPLES_PER_WINDOW:
        return False

    return motion.is_window_motion_clean(window_arrays["acc_magnitude"])
