"""
features.py
============
Combines PPG and EDA feature extraction into the final 9-feature vector,
and handles writing rows to the single output CSV.
"""

import os
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

import config
import eda
import ppg


def extract_window_features(window_arrays: dict) -> Optional[dict]:
    """
    Runs PPG and EDA feature extraction for one (already motion-approved)
    window. Returns the full feature row dict matching config.CSV_COLUMNS,
    or None if either signal was too degenerate to yield valid features.
    """
    fs = window_arrays["fs"]

    ppg_features = ppg.extract_ppg_features(window_arrays["ir"], fs)
    if ppg_features is None:
        return None

    eda_features = eda.extract_eda_features(window_arrays["eda_adc"], fs)
    if eda_features is None:
        return None

    window_start_iso = datetime.fromtimestamp(
        window_arrays["window_start"], tz=timezone.utc
    ).isoformat()

    row = {"window_start": window_start_iso}
    row.update(ppg_features)
    row.update(eda_features)

    return row


def append_to_csv(row: dict) -> None:
    """
    Appends one feature row to config.OUTPUT_CSV_PATH, writing the header
    only if the file does not yet exist. Column order is fixed by
    config.CSV_COLUMNS regardless of dict insertion order.
    """
    df_row = pd.DataFrame([row], columns=config.CSV_COLUMNS)

    write_header = not os.path.exists(config.OUTPUT_CSV_PATH)
    df_row.to_csv(
        config.OUTPUT_CSV_PATH,
        mode="a",
        header=write_header,
        index=False,
    )
