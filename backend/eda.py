
from typing import Optional

import numpy as np

import config
import utils


# ============================================================
# EDA CALIBRATION
# ============================================================

# Your previous:
#     conductance_uS = conductance_uS / 25.0
#
# is equivalent to:
#     conductance_uS * 0.04
#
# Keep this as a FIXED hardware calibration factor.
# Do NOT recalculate it for every 60-second window.
EDA_CALIBRATION_FACTOR = 0.04


def adc_to_skin_conductance(adc: np.ndarray) -> np.ndarray:
    """
    Convert ESP32 12-bit ADC values to skin conductance (uS).

    Processing:
        ESP32 ADC (0-4095)
            ↓
        Grove 10-bit equivalent (0-1023)
            ↓
        Grove GSR resistance formula
            ↓
        Conductance in uS
            ↓
        Fixed hardware calibration
    """

    # --------------------------------------------------------
    # Convert to floating point
    # --------------------------------------------------------
    adc = adc.astype(float).copy()

    # --------------------------------------------------------
    # ESP32 12-bit ADC (0-4095)
    # -> Grove 10-bit ADC (0-1023)
    # --------------------------------------------------------
    adc_10bit = adc * 1023.0 / 4095.0

    # --------------------------------------------------------
    # Prevent division by zero and invalid resistance
    #
    # Grove formula contains:
    #       512 - ADC
    #
    # Therefore ADC must remain below 512.
    # --------------------------------------------------------
    adc_10bit = np.clip(
        adc_10bit,
        0.0,
        511.0
    )

    # --------------------------------------------------------
    # Calculate skin resistance
    #
    # R = ((1024 + 2*ADC) * 10000)
    #     --------------------------------
    #          (512 - ADC)
    #
    # Resistance is in ohms.
    # --------------------------------------------------------
    resistance = (
        (1024.0 + 2.0 * adc_10bit) * 10000.0
    ) / (
        512.0 - adc_10bit
    )

    # --------------------------------------------------------
    # Resistance -> conductance
    #
    # Conductance (uS) = 1,000,000 / Resistance
    # --------------------------------------------------------
    conductance_uS = 1000000.0 / resistance

    # --------------------------------------------------------
    # FIXED HARDWARE CALIBRATION
    #
    # Previous:
    #     conductance_uS = conductance_uS / 25.0
    #
    # Now:
    #     conductance_uS = conductance_uS * 0.04
    #
    # This is deliberately fixed so that genuine EDA
    # changes are NOT normalized away from one window
    # to another.
    # --------------------------------------------------------
    conductance_uS *= EDA_CALIBRATION_FACTOR

    return conductance_uS


# ============================================================
# EDA PREPROCESSING
# ============================================================

def preprocess_eda(
    adc_values: np.ndarray,
    fs: float
) -> Optional[np.ndarray]:
    """
    Complete EDA preprocessing pipeline.

    Steps:
        1. ADC -> skin conductance
        2. Outlier removal
        3. Low-pass filtering
        4. Validation
    """

    # Need enough samples for processing
    if adc_values.size < 4:
        return None

    # --------------------------------------------------------
    # 1. ADC -> Skin Conductance
    # --------------------------------------------------------
    sc = adc_to_skin_conductance(adc_values)

    # --------------------------------------------------------
    # 2. Remove outliers
    # --------------------------------------------------------
    sc = utils.zscore_clip_outliers(
        sc,
        config.EDA_OUTLIER_ZSCORE_THRESH
    )

    # --------------------------------------------------------
    # 3. Low-pass filter
    # --------------------------------------------------------
    try:

        sc_filtered = utils.butter_lowpass_filter(
            sc,
            cutoff_hz=config.EDA_LOWPASS_CUTOFF_HZ,
            fs=fs,
            order=config.EDA_FILTER_ORDER,
        )

    except ValueError:
        return None

    # --------------------------------------------------------
    # 4. Check for invalid values
    # --------------------------------------------------------
    if not np.all(np.isfinite(sc_filtered)):
        return None

    return sc_filtered


# ============================================================
# EDA SLOPE
# ============================================================

def compute_eda_slope(
    signal: np.ndarray,
    fs: float
) -> float:
    """
    Calculate the linear slope of the EDA signal.

    Unit:
        uS / second
    """

    if signal.size < 2:
        return 0.0

    # Time axis in seconds
    t = np.arange(signal.size) / fs

    # Linear regression
    slope, _ = np.polyfit(
        t,
        signal,
        1
    )

    return float(slope)


# ============================================================
# EDA FEATURE EXTRACTION
# ============================================================

def extract_eda_features(
    adc_values: np.ndarray,
    fs: float
) -> Optional[dict]:
    """
    Extract EDA features from ADC samples.

    Features:
        EDA_Mean
        EDA_Std
        EDA_Min
        EDA_Max
        EDA_Range
        EDA_Slope
    """

    # --------------------------------------------------------
    # Preprocess EDA
    # --------------------------------------------------------
    eda = preprocess_eda(
        adc_values,
        fs
    )

    if eda is None:
        return None

    # --------------------------------------------------------
    # Extract features
    # --------------------------------------------------------
    return {
        "EDA_Mean": float(
            np.mean(eda)
        ),

        "EDA_Std": (
            float(
                np.std(
                    eda,
                    ddof=1
                )
            )
            if eda.size >= 2
            else 0.0
        ),

        "EDA_Min": float(
            np.min(eda)
        ),

        "EDA_Max": float(
            np.max(eda)
        ),

        "EDA_Range": float(
            np.max(eda) - np.min(eda)
        ),

        "EDA_Slope": compute_eda_slope(
            eda,
            fs
        ),
    }
