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
    if not samples:
        return None

    ir = np.array([s["ir"] for s in samples], dtype=float)
    acc_magnitude = np.array([s["accMagnitude"] for s in samples], dtype=float)
    eda_adc = np.array([s["eda"] for s in samples], dtype=float)

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
