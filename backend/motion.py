"""
motion.py
=========
Motion-artifact quality gate. Uses accMagnitude ONLY, per spec: no motion
features are computed or saved - this module's sole job is accept/reject.
"""

import numpy as np

import config


def is_window_motion_clean(acc_magnitude: np.ndarray) -> bool:
    """
    Returns True if the window passes the motion-quality check, False if it
    should be rejected for excessive movement.

    A sample is "bad" if it deviates from 1.0 g by more than
    config.MOTION_DEVIATION_G. The window is rejected if the fraction of
    bad samples exceeds config.MOTION_MAX_BAD_FRACTION.
    """
    if acc_magnitude.size == 0:
        return False

    deviation = np.abs(acc_magnitude - 1.0)
    bad_fraction = np.mean(deviation > config.MOTION_DEVIATION_G)

    return bool(bad_fraction <= config.MOTION_MAX_BAD_FRACTION)
