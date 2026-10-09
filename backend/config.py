"""
config.py
=========
Single source of truth for every tunable parameter in the pipeline.
No other module should hardcode a magic number that belongs here.
"""

# ---------------------------------------------------------------------------
# Serial connection
# ---------------------------------------------------------------------------
# Overridable via environment so the same code runs unmodified on Windows
# (COM3, COM4, ...) and Linux/macOS (/dev/ttyUSB0, /dev/cu.usbserial-*,
# ...) -- set SERIAL_PORT in backend/.env. Falls back to the original
# Windows default if unset.
import os

SERIAL_PORT = os.getenv("SERIAL_PORT", "COM3")
SERIAL_BAUDRATE = int(os.getenv("SERIAL_BAUDRATE", "115200"))
SERIAL_TIMEOUT_SEC = 1.0          # read() timeout, keeps reconnect loop responsive
RECONNECT_DELAY_SEC = 2.0         # wait between reconnect attempts

# ---------------------------------------------------------------------------
# Required JSON keys from the ESP32-S3 packet
# ---------------------------------------------------------------------------
REQUIRED_KEYS = ("ir", "red", "accMagnitude", "eda")

# ---------------------------------------------------------------------------
# Sampling / windowing
# ---------------------------------------------------------------------------
# Fixed system sampling rate. Not estimated from serial arrival timestamps -
# every fs-dependent computation (filters, peak-distance, slope) uses this
# constant directly.
SAMPLE_RATE_HZ = 64.0

# Sample-count based sliding window (not time-based): 3840 samples = 60 s at
# 64 Hz, 1920-sample step = 30 s shift / 50% overlap at 64 Hz.
WINDOW_SIZE_SAMPLES = 3840
WINDOW_STEP_SAMPLES = 128

# Minimum samples required in a window before attempting processing at all
# (guards against a near-empty window right after connect/reconnect).
MIN_SAMPLES_PER_WINDOW = int(WINDOW_SIZE_SAMPLES * 0.5)

# ---------------------------------------------------------------------------
# PPG preprocessing
# ---------------------------------------------------------------------------
PPG_BANDPASS_LOW_HZ = 0.5
PPG_BANDPASS_HIGH_HZ = 5.0
PPG_FILTER_ORDER = 3

# scipy.signal.find_peaks parameters for BVP peak (heartbeat) detection.
# distance is expressed in seconds and converted to samples at runtime using
# SAMPLE_RATE_HZ (max plausible HR ~180 bpm -> min 0.333 s between beats;
# we use a slightly relaxed 0.4 s floor).
PPG_PEAK_MIN_DISTANCE_SEC = 0.4
PPG_PEAK_PROMINENCE = 0.15  # on normalized (unit-variance-ish) BVP signal

# Physiological plausibility bounds for HR / IBI, used to sanity-filter peaks
HR_MIN_BPM = 40.0
HR_MAX_BPM = 180.0

# ---------------------------------------------------------------------------
# EDA preprocessing (Grove GSR v2.0 official conversion)
# ---------------------------------------------------------------------------
EDA_ADC_MAX = 4095  # 12-bit ADC as used in the official Grove GSR formula

# Outlier removal on raw ADC counts (simple z-score clip within a window)
EDA_OUTLIER_ZSCORE_THRESH = 3.0

# Low-pass Butterworth filter for the converted skin-conductance (µS) signal
EDA_LOWPASS_CUTOFF_HZ = 1.0
EDA_FILTER_ORDER = 3

# Baseline correction: subtract a slow-moving baseline estimated as the
# median of the filtered signal (robust to transient SCRs) so the extracted
# statistics reflect deviations from tonic level.
EDA_BASELINE_METHOD = "median"  # kept explicit/configurable per project rule

# ---------------------------------------------------------------------------
# Motion artifact rejection (MPU accMagnitude only)
# ---------------------------------------------------------------------------
# accMagnitude is expected around 1.0 g at rest. A window is rejected if the
# fraction of samples deviating from 1.0 g by more than MOTION_DEVIATION_G
# exceeds MOTION_MAX_BAD_FRACTION.
MOTION_DEVIATION_G = 0.20
MOTION_MAX_BAD_FRACTION = 0.15

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
OUTPUT_CSV_PATH = "extracted_features.csv"
CSV_COLUMNS = (
    "window_start",
    "HR",
    "SDNN",
    "RMSSD",
    "EDA_Mean",
    "EDA_Min",
    "EDA_Max",
    "EDA_Range",
    "EDA_Std",
    "EDA_Slope",
)
