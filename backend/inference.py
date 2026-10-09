"""
inference.py
============
Loads the deployment artifacts (feature_names.json, scaler.pkl,
final_model.pkl, decision_threshold.json) exactly once at startup and
exposes predict_from_features() for the live pipeline.

--------------------------------------------------------------------------
Model metadata indicates subject-specific baseline normalization followed by a
saved sklearn transformer and an XGBoost Booster. The original training code
and training rows are not present here, so live calibration is an explicit
approximation and parity is not claimed. Calibration requires an operator
acknowledgement of a quiet/resting setup, collects only quality-approved
windows, and refuses inference when its baseline scale is degenerate.
--------------------------------------------------------------------------
"""

from __future__ import annotations

import json
import logging
import platform
import threading
from pathlib import Path
from typing import Optional

import joblib
import numpy as np
import pandas as pd
import sklearn
import xgboost as xgb

logger = logging.getLogger("inference")

MODEL_DIR = Path(__file__).parent / "model"

# At least three accepted windows are required for session baseline calibration.
# Calibration feature windows are never also emitted as stress predictions.
RAW_FEATURE_ORDER = [
    "HR", "SDNN", "RMSSD",
    "eda_mean", "eda_std", "eda_min", "eda_max", "eda_range", "eda_slope",
]

# Maps the sensor pipeline's CSV_COLUMNS casing (features.extract_window_
# features -> EDA_Mean, EDA_Min, ...) to the training-time lowercase eda_*
# names in feature_names.json / scaler.pkl / final_model.pkl. Values are
# never reordered or renamed silently anywhere else -- this is the single
# place that mapping happens, so it can be audited in one spot.
SENSOR_KEY_TO_RAW_FEATURE = {
    "HR": "HR", "SDNN": "SDNN", "RMSSD": "RMSSD",
    "EDA_Mean": "eda_mean", "EDA_Std": "eda_std", "EDA_Min": "eda_min",
    "EDA_Max": "eda_max", "EDA_Range": "eda_range", "EDA_Slope": "eda_slope",
}


class ModelBundle:
    """Loads and validates the complete deployment artifact bundle.

    joblib/pickle compatibility is version-sensitive. Check recorded training
    runtime versions before unpickling anything, and refuse to predict when
    the installed XGBoost/scikit-learn/numeric stack does not match metadata.
    """

    def __init__(self, model_dir: Path = MODEL_DIR):
        feature_names_path = model_dir / "feature_names.json"
        scaler_path = model_dir / "scaler.pkl"
        model_path = model_dir / "final_model.pkl"
        threshold_path = model_dir / "decision_threshold.json"
        metadata_path = model_dir / "deployment_metadata.json"
        paths = (feature_names_path, scaler_path, model_path, threshold_path, metadata_path)
        for path in paths:
            if not path.is_file():
                raise FileNotFoundError(
                    f"Required deployment artifact missing: {path}. All five files "
                    "(final_model.pkl, scaler.pkl, feature_names.json, "
                    "decision_threshold.json, deployment_metadata.json) must stay together."
                )

        self.metadata = self._read_json(metadata_path)
        self._validate_runtime_versions(self.metadata)
        feature_meta = self._read_json(feature_names_path)
        threshold_meta = self._read_json(threshold_path)

        raw_order = feature_meta.get("raw_feature_order")
        transformed_order = feature_meta.get("transformed_feature_order")
        if not isinstance(raw_order, list) or not all(isinstance(v, str) for v in raw_order):
            raise ValueError("feature_names.json must contain a string raw_feature_order list")
        if not isinstance(transformed_order, list) or not all(isinstance(v, str) for v in transformed_order):
            raise ValueError("feature_names.json must contain a string transformed_feature_order list")
        self.raw_feature_order: list[str] = raw_order
        self.transformed_feature_order: list[str] = transformed_order
        if self.raw_feature_order != RAW_FEATURE_ORDER:
            raise ValueError(
                f"feature_names.json raw_feature_order {self.raw_feature_order} does not match "
                f"the implemented feature contract {RAW_FEATURE_ORDER}; refusing silent feature reordering."
            )
        if feature_meta.get("standard_scaler_columns") != self.raw_feature_order:
            raise ValueError("feature_names.json standard_scaler_columns disagrees with raw_feature_order")

        metadata_features = self.metadata.get("feature_list_raw")
        metadata_transformed = self.metadata.get("feature_list_transformed")
        if metadata_features != self.raw_feature_order:
            raise ValueError("deployment_metadata.json raw feature order disagrees with feature_names.json")
        if metadata_transformed != self.transformed_feature_order:
            raise ValueError("deployment_metadata.json transformed feature order disagrees with feature_names.json")
        preprocessing_meta = self.metadata.get("preprocessing", {})
        if preprocessing_meta.get("standard_scaler_columns") != self.raw_feature_order:
            raise ValueError("deployment_metadata.json preprocessing.standard_scaler_columns disagrees with raw feature order")
        if preprocessing_meta.get("minmax_scaler_columns") != []:
            raise ValueError("deployment_metadata.json declares unexpected min-max scaler columns")
        if self.metadata.get("model_type") != "xgboost.Booster (binary:logistic)":
            raise ValueError("deployment_metadata.json does not declare the expected binary:logistic Booster")

        artifact_paths = self.metadata.get("artifact_paths", {})
        expected_paths = {
            "final_model": "final_model.pkl",
            "feature_names": "feature_names.json",
            "decision_threshold": "decision_threshold.json",
            "scaler": "scaler.pkl",
        }
        for key, expected_name in expected_paths.items():
            if artifact_paths.get(key) != expected_name:
                raise ValueError(f"deployment_metadata.json artifact_paths.{key} must be {expected_name!r}")

        try:
            self.threshold = float(threshold_meta["threshold"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("decision_threshold.json must contain a numeric 'threshold'") from exc
        if not np.isfinite(self.threshold) or not 0.0 <= self.threshold <= 1.0:
            raise ValueError("decision_threshold.json threshold must be finite and in [0, 1]")
        metadata_threshold = self.metadata.get("decision_threshold")
        try:
            threshold_agrees = metadata_threshold is not None and np.isclose(
                float(metadata_threshold), self.threshold, rtol=0, atol=1e-12
            )
        except (TypeError, ValueError, OverflowError):
            threshold_agrees = False
        if not threshold_agrees:
            raise ValueError("deployment_metadata.json threshold disagrees with decision_threshold.json")

        # Only trusted artifacts from the version-matched bundle are unpickled.
        try:
            self.scaler = joblib.load(scaler_path)
            self.booster = joblib.load(model_path)
        except Exception as exc:
            raise RuntimeError(f"Could not load model/scaler artifacts from {model_dir}: {type(exc).__name__}") from exc
        if not isinstance(self.booster, xgb.Booster):
            raise TypeError(
                f"final_model.pkl loaded as {type(self.booster)}, expected xgboost.core.Booster; "
                "refusing a silent model-type substitution."
            )
        if not callable(getattr(self.scaler, "transform", None)):
            raise TypeError("scaler.pkl does not expose transform()")
        if hasattr(self.scaler, "n_features_in_") and int(self.scaler.n_features_in_) != len(self.raw_feature_order):
            raise ValueError("scaler.pkl input feature count is not nine")
        scaler_columns = getattr(self.scaler, "feature_names_in_", None)
        if scaler_columns is not None and list(scaler_columns) != self.raw_feature_order:
            raise ValueError("scaler.pkl feature_names_in_ disagrees with feature_names.json raw_feature_order")
        if self.booster.num_features() != len(self.transformed_feature_order):
            raise ValueError("final_model.pkl feature count disagrees with feature_names.json")

        try:
            booster_config = json.loads(self.booster.save_config())
            objective = booster_config["learner"]["objective"]["name"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Could not verify the saved Booster objective") from exc
        if objective != "binary:logistic":
            raise ValueError(
                f"Saved Booster objective is {objective!r}, expected 'binary:logistic'. "
                "The app will not interpret regression scores as stress probabilities. "
                "Verify the original artifact under the exact recorded XGBoost runtime; do not retrain or replace it silently."
            )
        expected_rounds = self.metadata.get("num_boost_round")
        if expected_rounds is not None and len(self.booster.get_dump()) != int(expected_rounds):
            raise ValueError("final_model.pkl tree count disagrees with deployment_metadata.json num_boost_round")

        logger.info("Loaded validated binary XGBoost deployment bundle with %d features", len(self.raw_feature_order))

    @staticmethod
    def _read_json(path: Path) -> dict:
        try:
            with path.open(encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Could not read valid JSON deployment metadata at {path}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"Deployment JSON artifact must contain an object: {path}")
        return value

    @staticmethod
    def _validate_runtime_versions(metadata: dict) -> None:
        recorded = metadata.get("software_versions")
        if not isinstance(recorded, dict):
            raise ValueError("deployment_metadata.json software_versions is missing or malformed")
        current = {
            "python": platform.python_version(),
            "xgboost": xgb.__version__,
            "scikit_learn": sklearn.__version__,
            "numpy": np.__version__,
            "pandas": pd.__version__,
            "joblib": joblib.__version__,
        }
        mismatches = []
        for package, installed in current.items():
            expected = recorded.get(package)
            if not isinstance(expected, str) or expected != installed:
                mismatches.append(f"{package}: expected {expected!r}, installed {installed!r}")
        if mismatches:
            raise RuntimeError(
                "Refusing to unpickle deployment artifacts with an unverified runtime. "
                "Install the pinned versions in backend/requirements.txt. Mismatches: "
                + "; ".join(mismatches)
            )

    def predict_proba(self, transformed_row: np.ndarray) -> float:
        """transformed_row: 1D array, already baseline-normalized AND
        scaler-transformed, in self.transformed_feature_order order."""
        dmatrix = xgb.DMatrix(
            transformed_row.reshape(1, -1),
            feature_names=self.transformed_feature_order,
        )
        prob = self.booster.predict(dmatrix)
        return float(prob[0])


class SessionInferenceState:
    """Session-specific, explicit baseline calibration followed by inference.

    The first three accepted windows are calibration-only. Because the model
    metadata says training data were normalized relative to subject baseline
    rows, the operator must explicitly acknowledge the quiet/resting setup at
    session start. The backend cannot physiologically verify that condition.
    Baseline scale is estimated from valid calibration windows with sample
    standard deviation. Near-constant features delay calibration; they never
    receive an arbitrary epsilon/fallback and are never sent to the model.
    """
    CALIBRATION_WINDOWS_MIN = 3
    CALIBRATION_WINDOWS_MAX = 8
    MIN_BASELINE_STD = 1e-6

    def __init__(self, bundle: ModelBundle):
        self._bundle = bundle
        self._baseline_rows: list[np.ndarray] = []
        self._baseline_mu: Optional[np.ndarray] = None
        self._baseline_sigma: Optional[np.ndarray] = None
        self.windows_seen = 0
        self.calibration_failed = False
        self._lock = threading.RLock()

    def snapshot(self) -> dict:
        """Thread-safe calibration summary for WebSocket/API reconnection."""
        with self._lock:
            return {
                "windows_seen": self.windows_seen,
                "windows_needed": self.CALIBRATION_WINDOWS_MIN,
                "calibration_ready": self._baseline_mu is not None,
                "calibration_failed": self.calibration_failed,
            }

    def _raw_vector_from_sensor_row(self, sensor_row: dict) -> np.ndarray:
        values = []
        reverse_map = {raw: sensor for sensor, raw in SENSOR_KEY_TO_RAW_FEATURE.items()}
        for raw_name in RAW_FEATURE_ORDER:
            if raw_name not in reverse_map or reverse_map[raw_name] not in sensor_row:
                raise ValueError(f"Missing required feature: {raw_name}")
            values.append(float(sensor_row[reverse_map[raw_name]]))
        raw = np.asarray(values, dtype=np.float64)
        if raw.shape != (len(RAW_FEATURE_ORDER),) or not np.all(np.isfinite(raw)):
            raise ValueError("Feature vector must contain nine finite values")
        return raw

    def _try_finalize_baseline(self) -> bool:
        if len(self._baseline_rows) < self.CALIBRATION_WINDOWS_MIN:
            return False
        rows = np.vstack(self._baseline_rows)
        mu = rows.mean(axis=0)
        sigma = rows.std(axis=0, ddof=1)
        if not np.all(np.isfinite(mu)) or not np.all(np.isfinite(sigma)):
            return False
        if np.any(sigma < self.MIN_BASELINE_STD):
            if len(self._baseline_rows) >= self.CALIBRATION_WINDOWS_MAX:
                self.calibration_failed = True
            return False
        self._baseline_mu = mu
        self._baseline_sigma = sigma
        logger.info("Session baseline finalized from %d valid windows", len(rows))
        return True

    @property
    def calibration_ready(self) -> bool:
        return self._baseline_mu is not None

    def predict(self, sensor_row: dict) -> dict:
        with self._lock:
            return self._predict_locked(sensor_row)

    def _predict_locked(self, sensor_row: dict) -> dict:
        raw_vec = self._raw_vector_from_sensor_row(sensor_row)
        if self._baseline_mu is None:
            if len(self._baseline_rows) < self.CALIBRATION_WINDOWS_MAX:
                self._baseline_rows.append(raw_vec.copy())
            self.windows_seen += 1
            self._try_finalize_baseline()
            if self._baseline_mu is None:
                if self.calibration_failed:
                    return {
                        "status": "calibration_failed",
                        "windows_seen": self.windows_seen,
                        "windows_needed": self.CALIBRATION_WINDOWS_MIN,
                        "reason": "baseline_variation_too_small",
                    }
                return {
                    "status": "warming_up",
                    "windows_seen": self.windows_seen,
                    "windows_needed": self.CALIBRATION_WINDOWS_MIN,
                }
            # The window which finalized calibration is also calibration-only.
            return {
                "status": "warming_up",
                "windows_seen": self.windows_seen,
                "windows_needed": self.CALIBRATION_WINDOWS_MIN,
                "calibration_ready": True,
            }

        baseline_normalized = (raw_vec - self._baseline_mu) / self._baseline_sigma
        if not np.all(np.isfinite(baseline_normalized)):
            raise ValueError("Baseline-normalized feature vector is non-finite")
        input_df = pd.DataFrame(baseline_normalized.reshape(1, -1), columns=self._bundle.raw_feature_order)
        scaled = np.asarray(self._bundle.scaler.transform(input_df), dtype=np.float64)
        if scaled.shape != (1, len(self._bundle.transformed_feature_order)) or not np.all(np.isfinite(scaled)):
            raise ValueError("Scaler returned malformed or non-finite model input")
        prob = float(self._bundle.predict_proba(scaled[0]))
        if not np.isfinite(prob) or not 0 <= prob <= 1:
            raise ValueError("Model returned an invalid probability")
        label = int(prob >= self._bundle.threshold)
        result = {
            "status": "ok", "stress_label": label,
            "stress_status": "Stressed" if label == 1 else "Not Stressed",
            "stress_prob": round(prob, 4), "threshold": self._bundle.threshold,
        }
        result.update(sensor_row)
        return result


_bundle: Optional[ModelBundle] = None


def get_bundle() -> ModelBundle:
    """Lazily loads and caches the single shared ModelBundle. Raises on
    first call if artifacts are missing/inconsistent -- callers (app.py
    startup) should let that exception crash startup loudly rather than
    silently serving unpredictable output."""
    global _bundle
    if _bundle is None:
        _bundle = ModelBundle()
    return _bundle
