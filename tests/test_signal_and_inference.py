import json
import platform
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import joblib
import pandas as pd
import sklearn
import xgboost as xgb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import config
import eda
import feature_extraction
import inference
import ppg
from inference import RAW_FEATURE_ORDER, SENSOR_KEY_TO_RAW_FEATURE, ModelBundle, SessionInferenceState


class SignalProcessingTests(unittest.TestCase):
    def test_ppg_features_from_deterministic_pulse_train(self):
        fs = config.SAMPLE_RATE_HZ
        t = np.arange(config.WINDOW_SIZE_SAMPLES) / fs
        # Deterministic, clean synthetic signal used only in this unit test.
        signal = 100000 + 5000 * np.sin(2 * np.pi * 1.2 * t) + 500 * np.sin(2 * np.pi * 2.4 * t)
        result = ppg.extract_ppg_features(signal, fs)
        self.assertIsNotNone(result)
        self.assertTrue(40 <= result["HR"] <= 180)
        self.assertTrue(np.isfinite(result["SDNN"]))
        self.assertTrue(np.isfinite(result["RMSSD"]))

    def test_flat_ppg_and_invalid_eda_are_rejected(self):
        self.assertIsNone(ppg.extract_ppg_features(np.ones(400), config.SAMPLE_RATE_HZ))
        self.assertIsNone(eda.preprocess_eda(np.full(300, 3000.0), config.SAMPLE_RATE_HZ))
        with self.assertRaises(ValueError):
            eda.adc_to_skin_conductance(np.array([np.nan, 100.0]))

    def test_full_feature_extraction_returns_nine_features_for_valid_test_window(self):
        n = config.WINDOW_SIZE_SAMPLES
        fs = config.SAMPLE_RATE_HZ
        t = np.arange(n) / fs
        samples = []
        for i in range(n):
            samples.append({"ir": 100000 + 5000 * np.sin(2 * np.pi * 1.2 * t[i]),
                            "accMagnitude": 1.0, "eda": 250 + 20 * np.sin(2 * np.pi * 0.08 * t[i]),
                            "seq": i, "t_recv": t[i]})
        arrays = {"window_start": 1.0, "samples": samples}
        import preprocessing
        window = preprocessing.window_to_arrays(arrays)
        self.assertIsNotNone(window)
        features = feature_extraction.extract_window_features(window)
        self.assertIsNotNone(features)
        expected = {"HR", "SDNN", "RMSSD", "EDA_Mean", "EDA_Std", "EDA_Min", "EDA_Max", "EDA_Range", "EDA_Slope"}
        self.assertEqual(expected.issubset(features), True)
        self.assertTrue(all(np.isfinite(float(features[name])) for name in expected))


class FakeScaler:
    def __init__(self):
        self.input_columns = None
        self.last_values = None

    def transform(self, frame):
        self.input_columns = list(frame.columns)
        self.last_values = frame.to_numpy(dtype=float).copy()
        return self.last_values * 2.0


class FakeBundle:
    raw_feature_order = RAW_FEATURE_ORDER
    transformed_feature_order = RAW_FEATURE_ORDER
    threshold = 0.5

    def __init__(self):
        self.scaler = FakeScaler()
        self.inputs = []

    def predict_proba(self, values):
        self.inputs.append(np.asarray(values).copy())
        return float(1 / (1 + np.exp(-values[0])))


def sensor_row(base):
    row = {}
    for i, feature in enumerate(RAW_FEATURE_ORDER):
        sensor_key = next(sensor for sensor, raw in SENSOR_KEY_TO_RAW_FEATURE.items() if raw == feature)
        row[sensor_key] = float(base + i * 10)
    return row


class CalibrationTests(unittest.TestCase):
    def test_static_artifact_metadata_feature_order_and_threshold_agree(self):
        model_dir = ROOT / "backend" / "model"
        with open(model_dir / "feature_names.json", encoding="utf-8") as f:
            feature_meta = json.load(f)
        with open(model_dir / "decision_threshold.json", encoding="utf-8") as f:
            threshold_meta = json.load(f)
        with open(model_dir / "deployment_metadata.json", encoding="utf-8") as f:
            deploy = json.load(f)
        self.assertEqual(feature_meta["raw_feature_order"], RAW_FEATURE_ORDER)
        self.assertEqual(feature_meta["standard_scaler_columns"], RAW_FEATURE_ORDER)
        self.assertEqual(feature_meta["transformed_feature_order"], RAW_FEATURE_ORDER)
        self.assertEqual(deploy["feature_list_raw"], RAW_FEATURE_ORDER)
        self.assertEqual(deploy["feature_list_transformed"], RAW_FEATURE_ORDER)
        self.assertEqual(deploy["preprocessing"]["standard_scaler_columns"], RAW_FEATURE_ORDER)
        self.assertAlmostEqual(float(threshold_meta["threshold"]), float(deploy["decision_threshold"]))
        for artifact in ("final_model.pkl", "scaler.pkl", "feature_names.json",
                         "decision_threshold.json", "deployment_metadata.json"):
            self.assertGreater((model_dir / artifact).stat().st_size, 0, artifact)

    def test_calibration_windows_are_never_predictions_and_postcal_values_reach_model(self):
        bundle = FakeBundle()
        state = SessionInferenceState(bundle)
        for base in (10.0, 11.0):
            self.assertEqual(state.predict(sensor_row(base))["status"], "warming_up")
        self.assertEqual(bundle.inputs, [])
        # This is the first window that makes calibration ready, and remains calibration-only.
        self.assertEqual(state.predict(sensor_row(12.0))["status"], "warming_up")
        self.assertEqual(bundle.inputs, [])
        first = state.predict(sensor_row(13.0))
        self.assertEqual(first["status"], "ok")
        self.assertEqual(bundle.scaler.input_columns, RAW_FEATURE_ORDER)
        first_model_input = bundle.inputs[-1].copy()
        second = state.predict(sensor_row(14.0))
        self.assertEqual(second["status"], "ok")
        self.assertFalse(np.allclose(first_model_input, bundle.inputs[-1]))

    def test_degenerate_calibration_fails_without_arbitrary_scale(self):
        state = SessionInferenceState(FakeBundle())
        constant = sensor_row(20.0)
        for _ in range(state.CALIBRATION_WINDOWS_MAX):
            result = state.predict(constant)
        self.assertEqual(result["status"], "calibration_failed")
        self.assertEqual(state._baseline_sigma, None)

    def test_real_model_artifact_bundle_matches_metadata_or_refuses_mismatched_runtime(self):
        model_dir = ROOT / "backend" / "model"
        with open(model_dir / "deployment_metadata.json", encoding="utf-8") as f:
            metadata = json.load(f)
        recorded = metadata["software_versions"]
        installed = {
            "python": platform.python_version(), "xgboost": xgb.__version__,
            "scikit_learn": sklearn.__version__, "numpy": np.__version__,
            "pandas": pd.__version__, "joblib": joblib.__version__,
        }
        mismatches = {name: (recorded.get(name), value) for name, value in installed.items()
                      if recorded.get(name) != value}
        if mismatches:
            # Safety regression: never unpickle with an unverified stack.
            with self.assertRaisesRegex(RuntimeError, "Refusing to unpickle"):
                ModelBundle(model_dir)
            return

        # Under the exact recorded runtime, either validate the full bundle or
        # prove that an objective mismatch is rejected explicitly. This archive
        # currently contains regression-objective strings despite binary metadata;
        # treating that refusal as a passing safety test does NOT mean live
        # prediction is deployable (README/MODEL_CARD call it a release blocker).
        try:
            bundle = ModelBundle(model_dir)
        except ValueError as exc:
            message = str(exc)
            if "objective" not in message or "reg:squarederror" not in message or "binary:logistic" not in message:
                raise
            self.assertIn("will not interpret regression scores as stress probabilities", message)
            return

        with open(model_dir / "feature_names.json", encoding="utf-8") as f:
            names = json.load(f)
        with open(model_dir / "decision_threshold.json", encoding="utf-8") as f:
            threshold = json.load(f)
        self.assertEqual(bundle.raw_feature_order, names["raw_feature_order"])
        self.assertEqual(bundle.transformed_feature_order, names["transformed_feature_order"])
        self.assertAlmostEqual(bundle.threshold, float(threshold["threshold"]))
        self.assertEqual(bundle.booster.num_features(), len(bundle.transformed_feature_order))
        self.assertIsNotNone(bundle.metadata)


if __name__ == "__main__":
    unittest.main()
