# Model Card — Stress Monitor XGBoost Deployment

## Summary

The backend loads the existing `final_model.pkl` as an `xgboost.Booster` for binary non-stress/stress classification. It loads the paired `scaler.pkl`, `feature_names.json`, `decision_threshold.json`, and `deployment_metadata.json`. The implementation preserves these artifacts and does not retrain or substitute a model.

## Intended use and exclusions

Intended use: engineering/research exploration of a PPG + GSR feature pipeline and a local browser monitor. It is not a medical device, diagnostic tool, mental-health assessment, or suitable basis for employment, insurance, safety, or other consequential decisions. Predictions are experimental labels, not a direct measurement of a person's psychological state.

## Inputs and target

The nine feature names/order required by the artifact metadata are:

1. `HR` (beats per minute)
2. `SDNN` (milliseconds)
3. `RMSSD` (milliseconds)
4. `eda_mean` (nominal µS)
5. `eda_std` (nominal µS)
6. `eda_min` (nominal µS)
7. `eda_max` (nominal µS)
8. `eda_range` (nominal µS)
9. `eda_slope` (nominal µS/s)

The EDA physical units depend on an unverified circuit conversion/calibration factor. Motion is a quality-rejection signal, not one of the nine model input features. The dashboard's binary display is `0 = Not Stressed`, `1 = Stressed` according to the existing thresholded output.

## Dataset and development provenance

`deployment_metadata.json` reports a WESAD-derived development set with 15 subjects and 9,654 rows before SMOTE and 11,347 after SMOTE. It records subject-specific baseline normalization and a saved scaler, and describes a final model trained on the full development dataset using a separately tuned parameter set. The original training scripts, raw dataset, split outputs, report metrics, and full provenance/licensing evidence are not included in the supplied archive; figures above are metadata claims, not independently reproduced here. The raw WESAD dataset is not packaged in this repository.

## Evaluation

The metadata says separate subject-wise/LOSO evaluation outputs were generated at `outputs/reports/overall_metrics.json` and `per_subject_metrics.csv`, but those files are absent from the archive. This audit therefore does not reproduce or claim any offline performance number. The included final model was trained on all available development subjects and has **no independent held-out test score of its own**, as the metadata itself cautions. No physical-hardware performance measurements were supplied or collected in this code-only environment.

## Preprocessing and known parity issue

Metadata says training used subject-specific baseline normalization from each subject's baseline-condition rows before scaler transformation. The original training scripts and exact window generation are unavailable, so live parity cannot be established. The runtime uses an explicitly acknowledged quiet/resting setup and at least three quality-approved 60-second windows to estimate a session baseline, with a maximum of eight baseline windows when scale is near-degenerate. Calibration windows never also produce predictions, and degenerate calibration fails rather than falling back to arbitrary constants. This is an operational approximation, not a claim of training equivalence.

The backend window setting is 3,840 samples at nominal 64 Hz and a 1,920-sample step. The feature pipeline filters IR PPG at 0.5–5 Hz and EDA conductance at 1 Hz. The legacy EDA factor `0.04` is retained only to avoid silently changing the deployed feature distribution; it has not been verified with the physical GSR circuit. MAX30102 hardware FIFO/output cadence likewise still needs measurement.

## Deployment artifacts

- `backend/model/final_model.pkl`
- `backend/model/scaler.pkl`
- `backend/model/feature_names.json`
- `backend/model/decision_threshold.json` — threshold `0.3562862862862863`
- `backend/model/deployment_metadata.json`

These files are a single compatibility bundle. Artifacts are pickle/joblib-based and must only be loaded from a trusted origin. The runtime validates feature order, artifact presence, metadata agreement, feature dimensionality, scaler interface, booster type, runtime versions, and threshold range. **Audit blocker:** the serialized `final_model.pkl` bytes contain `reg:squarederror` objective strings, conflicting with the metadata declaration `xgboost.Booster (binary:logistic)`. A deserialization attempted under a different XGBoost/scikit-learn stack also reported a regression objective and serialization warnings, so it is not a definitive compatible-runtime validation. This repo intentionally preserves the artifact and refuses inference unless the pinned-runtime objective is verified as binary logistic. Until the artifact owner resolves that discrepancy from the original export/training provenance, the deployed classifier must be treated as not validated for inference.

## Risks and limitations

- Subject-specific normalization from the original training pipeline cannot be reproduced exactly from this archive.
- A human's initial resting state is not guaranteed; acknowledgement is not physiological proof.
- Motion and signal-quality rejection can create missing predictions and may behave differently under real-world movement.
- EDA conversion depends on the precise sensor circuit and ESP32 ADC characteristics; physical calibration is outstanding.
- Firmware requests a 400 Hz MAX30102 sample rate with four-sample averaging while emitting at a nominal 64 Hz scheduler rate. Actual FIFO/effective delivered timing must be measured on the assembled board.
- WESAD population, setup, and label limitations may not represent everyday use, populations, devices, or stress contexts.
- No independent real-world accuracy, calibration, fairness, clinical, or hardware validation is demonstrated by the repository.

## Safe interpretation

Show prediction availability and data-quality/calibration states clearly. Do not present a missing, rejected, or calibration-only window as a normal stress prediction. Treat outputs as experimental and require separate subject-wise evaluation, circuit calibration, and hardware validation before drawing scientific conclusions.
