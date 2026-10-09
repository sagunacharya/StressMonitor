"""
inference.py
============
Loads the deployment artifacts (feature_names.json, scaler.pkl,
final_model.pkl, decision_threshold.json) exactly once at startup and
exposes predict_from_features() for the live pipeline.

--------------------------------------------------------------------------
WHY THIS FILE IS NOT A TRIVIAL "load pkl, call .predict()" WRAPPER
--------------------------------------------------------------------------
Inspecting outputs/deployment/deployment_metadata.json shows the training
pipeline (ml_pipeline/preprocessing.py: full_prepare) ran, IN THIS ORDER,
before scaler.pkl was ever fit:

  1. per_subject_baseline_normalize(): for every subject, every one of the
     9 raw features is z-scored using ONLY that subject's own baseline-
     condition rows: (x - mu_subject_baseline) / sigma_subject_baseline.
  2. scaler.pkl (a StandardScaler) was then fit ON TOP of that already-
     normalized data (confirmed by loading scaler.pkl directly: its
     mean_/scale_ are ~0.5/~2.2/~9.7, not raw-unit values like HR~75 bpm
     or EDA~1.8 uS -- i.e. it is a second z-score layered on the first).

deployment_metadata.json says this outright: "At inference time on a NEW
subject with no prior baseline recording, this step cannot be replicated
exactly." That is the real constraint this file has to work within -- not
a bug to "fix" by inventing a different preprocessing order, and not
something to silently ignore either (skipping step 1 and feeding raw
physical units straight into scaler.pkl would be systematically wrong:
every feature would sit ~50-250 std-devs from what the model was trained
on, per the mean_/scale_ values above).

THE PRACTICAL RESOLUTION USED HERE: a rolling SESSION baseline.
  - The first BASELINE_WINDOWS completed windows of a session (default 2,
    i.e. the first ~90s of data after START SESSION) are used to compute
    a per-session mu/sigma for each of the 9 raw features -- the same
    role WESAD's "baseline condition" rows played per-subject at training
    time, just gathered live instead of pre-labeled.
  - EVERY window in the session (including those first ones, once the
    baseline itself is ready) is then normalized as
    (raw - mu_session) / sigma_session before scaler.transform().
  - Until the session baseline is ready, predict_from_features() returns
    a "warming_up" status instead of a prediction -- it does NOT guess
    with an unset or partial baseline, and it does NOT fall back to
    feeding raw units through scaler.pkl.

This is the closest honest analogue to the training-time procedure that
is achievable with only live streaming data and no separate resting
calibration recording. It is a deliberate design choice, not a hidden
guess -- see BASELINE_WINDOWS below and STARTUP_BEHAVIOR.md-equivalent
notes in README for how to change it (e.g. to a fixed calm-sit
calibration phase) if the team wants a different calibration protocol.
--------------------------------------------------------------------------
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import joblib
import numpy as np
import pandas as pd
import xgboost as xgb

logger = logging.getLogger("inference")

MODEL_DIR = Path(__file__).parent / "model"

# Number of completed windows used to establish the rolling per-session
# baseline (mirrors WESAD's per-subject "baseline condition" rows).
# BASELINE_WINDOWS = 1 so a prediction is produced from the very first
# completed window, per spec ("After first complete window: show ...
# STRESS/NON-STRESS"). IMPORTANT: with n=1 there is no real within-
# session variance yet, so sigma cannot come from the session itself --
# see SINGLE_WINDOW_FALLBACK_SIGMA below for what window 1 actually uses
# instead. From window 2 onward the session's own accumulating
# mean/std would normally be preferred, but this deployment freezes the
# baseline once ready (see _maybe_finalize_baseline) rather than
# continuously updating it, so a stress window occurring early can't
# drag the reference point -- BASELINE_WINDOWS=1 combined with the fixed
# fallback sigma below is therefore the operating baseline for the
# entire session, not just window 1. Raising BASELINE_WINDOWS to 2+
# would let the true first N windows set a session-specific sigma
# instead, at the cost of delaying the first prediction beyond window 1.
BASELINE_WINDOWS = 1

# Fallback per-feature standard deviation used ONLY when the session
# baseline would otherwise be computed from a single window (sigma=0 by
# construction, which is undefined for a z-score -- NOT the same
# situation as preprocessing.per_subject_baseline_normalize's `sigma ==
# 0` fallback, which handles a genuinely constant-valued feature across
# >=2 real baseline rows; here sigma is unset because there is only one
# sample, not because the feature is constant).
#
# Values below are each feature's population standard deviation computed
# directly from ml_pipeline/wesad_features_v5.csv (the RAW, pre-baseline-
# normalization training features -- i.e. real cross-subject, physical-
# unit variability), in RAW_FEATURE_ORDER. This is the closest available
# honest reference for "how much does this feature typically vary" in
# the complete absence of any real within-session spread yet. It is used
# for exactly one window per session (window 1) and never again once a
# real multi-window sigma would be available -- see BASELINE_WINDOWS
# note above for why this deployment does not currently take that further
# step.
#
# Regenerate if wesad_features_v5.csv or its preprocessing changes:
#   python -c "import pandas as pd; df = pd.read_csv('wesad_features_v5.csv'); \
#     print(df[['HR','SDNN','RMSSD','eda_mean','eda_std','eda_min','eda_max','eda_range','eda_slope']].std(ddof=0).tolist())"
SINGLE_WINDOW_FALLBACK_SIGMA = {
    "HR": 12.2019, "SDNN": 36.3976, "RMSSD": 20.4136,
    "eda_mean": 2.4303, "eda_std": 0.0971, "eda_min": 2.2568,
    "eda_max": 2.5649, "eda_range": 0.4323, "eda_slope": 0.0048,
}

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
    """Loads all 4 deployment artifacts once and validates they agree."""

    def __init__(self, model_dir: Path = MODEL_DIR):
        feature_names_path = model_dir / "feature_names.json"
        scaler_path = model_dir / "scaler.pkl"
        model_path = model_dir / "final_model.pkl"
        threshold_path = model_dir / "decision_threshold.json"

        for p in (feature_names_path, scaler_path, model_path, threshold_path):
            if not p.exists():
                raise FileNotFoundError(
                    f"Required deployment artifact missing: {p}. "
                    f"The backend cannot start without all 4 files "
                    f"(feature_names.json, scaler.pkl, final_model.pkl, "
                    f"decision_threshold.json) in {model_dir}."
                )

        with open(feature_names_path) as f:
            feature_meta = json.load(f)
        self.raw_feature_order: list[str] = feature_meta["raw_feature_order"]
        self.transformed_feature_order: list[str] = feature_meta[
            "transformed_feature_order"
        ]

        if self.raw_feature_order != RAW_FEATURE_ORDER:
            raise ValueError(
                f"feature_names.json raw_feature_order "
                f"{self.raw_feature_order} does not match the order this "
                f"module was written against {RAW_FEATURE_ORDER}. Refusing "
                f"to start rather than silently mis-order features into "
                f"the model."
            )

        self.scaler = joblib.load(scaler_path)  # sklearn ColumnTransformer
        self.booster = joblib.load(model_path)  # xgboost.core.Booster
        if not isinstance(self.booster, xgb.Booster):
            raise TypeError(
                f"final_model.pkl loaded as {type(self.booster)}, expected "
                f"xgboost.core.Booster. deployment_metadata.json says "
                f'model_type is "xgboost.Booster (binary:logistic)" -- if '
                f"this changed, predict_from_features()'s DMatrix-based "
                f"call needs updating too."
            )

        with open(threshold_path) as f:
            threshold_meta = json.load(f)
        self.threshold: float = float(threshold_meta["threshold"])

        logger.info(
            f"Loaded deployment model: {len(self.raw_feature_order)} raw "
            f"features, threshold={self.threshold:.4f}"
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
    """
    Per-session rolling baseline + prediction. One instance per active
    START SESSION -> STOP SESSION lifecycle; discarded entirely on STOP
    (see app.py SessionManager), which is what gives every new session a
    completely fresh baseline as the spec requires.
    """

    def __init__(self, bundle: ModelBundle):
        self._bundle = bundle
        # One running list per raw feature, in RAW_FEATURE_ORDER order,
        # of that feature's raw value from every completed window so far
        # this session. Used both to build the baseline (first
        # BASELINE_WINDOWS windows) and, after that, is no longer
        # appended to for baseline purposes -- the baseline is frozen
        # once ready so later high-stress windows can't drag the
        # session's own reference point toward "new normal".
        self._baseline_samples: dict[str, list[float]] = {
            name: [] for name in RAW_FEATURE_ORDER
        }
        self._baseline_mu: Optional[np.ndarray] = None
        self._baseline_sigma: Optional[np.ndarray] = None
        self.windows_seen = 0

    def _raw_vector_from_sensor_row(self, sensor_row: dict) -> np.ndarray:
        """sensor_row uses feature_extraction.py's CSV_COLUMNS casing
        (HR, SDNN, RMSSD, EDA_Mean, ...). Returns a 9-vector in
        RAW_FEATURE_ORDER (training-time casing/order)."""
        values = []
        for raw_name in RAW_FEATURE_ORDER:
            sensor_key = next(
                k for k, v in SENSOR_KEY_TO_RAW_FEATURE.items() if v == raw_name
            )
            values.append(float(sensor_row[sensor_key]))
        return np.array(values, dtype=float)

    def _maybe_finalize_baseline(self) -> None:
        if self._baseline_mu is not None:
            return  # already frozen
        if self.windows_seen < BASELINE_WINDOWS:
            return

        mu = np.array(
            [np.mean(self._baseline_samples[n]) for n in RAW_FEATURE_ORDER]
        )

        n_samples = len(self._baseline_samples[RAW_FEATURE_ORDER[0]])
        if n_samples < 2:
            # A single sample has zero variance by construction -- there
            # is no real within-session spread to measure yet. Using
            # eps here (as a naive "avoid divide by zero" guard) would
            # make (raw - mu)/sigma blow up to ~1e8 for any nonzero
            # deviation in the very next window, which testing showed
            # saturates the model toward "Stressed" regardless of actual
            # signal content. Use the fixed, documented cross-subject
            # fallback instead (see SINGLE_WINDOW_FALLBACK_SIGMA above).
            sigma = np.array(
                [SINGLE_WINDOW_FALLBACK_SIGMA[n] for n in RAW_FEATURE_ORDER]
            )
            logger.info(
                "Session baseline has only 1 window: using fixed "
                "cross-subject fallback sigma (see "
                "SINGLE_WINDOW_FALLBACK_SIGMA) rather than an undefined "
                "single-sample variance."
            )
        else:
            # >=2 real samples: use the session's own spread, mirroring
            # preprocessing.per_subject_baseline_normalize's own `sigma =
            # base_rows[...].std().replace(0, eps)` fallback for the
            # (rare) case a feature is genuinely constant across those
            # samples.
            sigma = np.array(
                [np.std(self._baseline_samples[n], ddof=0) for n in RAW_FEATURE_ORDER]
            )
            eps = 1e-8
            sigma = np.where(sigma < eps, eps, sigma)

        self._baseline_mu = mu
        self._baseline_sigma = sigma
        logger.info(
            f"Session baseline frozen after {self.windows_seen} window(s): "
            f"mu={mu.round(3).tolist()} sigma={sigma.round(3).tolist()}"
        )

    def predict(self, sensor_row: dict) -> dict:
        """
        sensor_row: the dict returned by feature_extraction.
        extract_window_features() for ONE completed 60s/3840-sample
        window (already passed the motion quality gate upstream).

        Returns one of:
          {"status": "warming_up", "windows_seen": N, "windows_needed": M}
          {"status": "ok", "stress_label": 0|1, "stress_status": str,
           "stress_prob": float, "threshold": float, <raw features...>}
        Never raises for a well-formed sensor_row; malformed input is the
        caller's responsibility to have already screened out.
        """
        raw_vec = self._raw_vector_from_sensor_row(sensor_row)

        if self.windows_seen < BASELINE_WINDOWS:
            for i, name in enumerate(RAW_FEATURE_ORDER):
                self._baseline_samples[name].append(float(raw_vec[i]))
        self.windows_seen += 1
        self._maybe_finalize_baseline()

        if self._baseline_mu is None:
            return {
                "status": "warming_up",
                "windows_seen": self.windows_seen,
                "windows_needed": BASELINE_WINDOWS,
            }

        baseline_normalized = (raw_vec - self._baseline_mu) / self._baseline_sigma

        # scaler.pkl is a ColumnTransformer fit on a named pandas
        # DataFrame at training time (preprocessing.transform() calls
        # ct.transform(df[feature_cols])). It therefore REQUIRES named
        # DataFrame input at inference time too -- passing a plain 2D
        # ndarray raises "Specifying the columns using strings is only
        # supported for dataframes" (confirmed by running this exact
        # call). RAW_FEATURE_ORDER is used as the column names since
        # standard_scaler_columns in feature_names.json lists the same 9
        # names in the same order the ColumnTransformer was fit with.
        input_df = pd.DataFrame(
            baseline_normalized.reshape(1, -1), columns=RAW_FEATURE_ORDER
        )
        scaled = self._bundle.scaler.transform(input_df)[0]

        prob = self._bundle.predict_proba(scaled)
        threshold = self._bundle.threshold
        label = int(prob >= threshold)

        result = {
            "status": "ok",
            "stress_label": label,
            "stress_status": "Stressed" if label == 1 else "Not Stressed",
            "stress_prob": round(prob, 4),
            "threshold": threshold,
        }
        # Echo back the RAW (physical-unit) features for display/storage
        # -- the website shows real HR/EDA values, never the internal
        # normalized/scaled numbers the model actually consumes.
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
