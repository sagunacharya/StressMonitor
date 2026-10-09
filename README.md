# ESP32 Physiological Signal Monitor

An embedded-sensing and machine-learning prototype that streams physiological sensor readings from an ESP32-S3 to a Python backend, extracts PPG/EDA features, applies a saved XGBoost model, and displays the resulting estimates in a browser dashboard.

> **Research prototype, not a medical device.** The model produces an experimental estimate from physiological patterns; it does not diagnose stress or replace professional assessment. Validate the signal pipeline and model independently before relying on its output.

## What it includes

- **Firmware:** ESP32-S3 code for MAX30102 red/infrared readings, MPU6500 acceleration magnitude, and Grove GSR analog readings.
- **Signal processing:** serial reception, window buffering, motion-quality gating, PPG/EDA processing, and feature extraction.
- **Inference:** saved model, scaler, ordered feature names, threshold, and deployment metadata.
- **Backend:** Flask REST endpoints, authenticated sessions, WebSocket updates, and MySQL persistence.
- **Frontend:** HTML/CSS/JavaScript login and monitoring dashboard.

The ESP32 sends JSON records over USB serial at 115200 baud. Python performs signal processing and inference; the model does **not** run on the ESP32 itself.

## Repository layout

```text
.
├── backend/                  # Python API, signal pipeline, inference and DB
│   ├── model/                # Required inference artifacts (small; keep together)
│   ├── requirements.txt
│   └── .env.example
├── firmware/
│   └── esp32_stress_monitor/
│       └── esp32_stress_monitor.ino
├── frontend/                 # Browser dashboard
├── Start Stress Monitor.bat  # Windows setup/start launcher
├── Stop Stress Monitor.bat   # Windows stop helper
├── .gitignore
└── README.md
```

## Hardware

- ESP32-S3 development board
- MAX30102 PPG sensor
- MPU6500 accelerometer/gyroscope module
- Grove GSR v2.0 sensor
- USB cable and suitable jumper wires

The firmware configures I²C SDA on GPIO 8 and SCL on GPIO 9, reads the Grove GSR analog signal on GPIO 13, and emits newline-delimited JSON at 115200 baud. MAX30102 and MPU6500 share the I²C bus. Confirm your exact ESP32 board pinout and each module's voltage requirements before wiring; do not connect a signal voltage that exceeds the board's input limits.

### Arduino setup

1. Open `firmware/esp32_stress_monitor/esp32_stress_monitor.ino` in Arduino IDE.
2. Select the correct ESP32-S3 board and port.
3. Install compatible Arduino libraries providing `MAX30105.h`, `MPU6500_WE.h`, and `ArduinoJson.h`.
4. Upload the firmware and open Serial Monitor at **115200 baud** to verify that valid JSON records are being emitted.

## Software requirements

- Python (the supplied environment metadata records Python 3.13)
- MySQL Server and a MySQL account allowed to create/use the configured database and tables
- USB serial access to the ESP32 when running live mode

The project deliberately does not include a Python virtual environment. It is generated locally from `backend/requirements.txt` so platform-specific binaries, caches and installed packages do not bloat the repository.

## Configure and run on Windows

1. Install Python and MySQL Server.
2. Copy `backend\.env.example` to `backend\.env`.
3. Edit `backend\.env` with your MySQL credentials, a long random `DASH_SECRET_KEY`, and the ESP32 serial port (for example, `COM3`).
4. Upload the firmware and connect the board.
5. Double-click **Start Stress Monitor.bat**. On first run it creates `backend\.venv`, installs the requirements and starts the server.
6. Open [http://127.0.0.1:5000](http://127.0.0.1:5000). Register an account, sign in, and use the dashboard controls.
7. Use **Stop Stress Monitor.bat** to stop the server when finished.

Never commit `backend\.env`. Only the placeholder template `backend\.env.example` belongs in version control. Do not reuse a real password or secret in a public repository.

## Run manually (Windows, macOS or Linux)

From the repository root, create and activate an environment:

```bash
python -m venv backend/.venv
```

Windows PowerShell:

```powershell
backend\.venv\Scripts\Activate.ps1
python -m pip install -r backend/requirements.txt
Copy-Item backend/.env.example backend/.env
Set-Location backend
python app.py
```

macOS/Linux:

```bash
source backend/.venv/bin/activate
python -m pip install -r backend/requirements.txt
cp backend/.env.example backend/.env
cd backend
python app.py
```

Before running, edit `.env` with the correct credentials and serial port. MySQL must be running; on startup the app creates the configured database and required tables, so the database user needs the appropriate privileges. Then visit `http://127.0.0.1:5000`.

## Model artifacts: keep these files together

The following files are intentionally included because live inference depends on them:

- `backend/model/final_model.pkl`
- `backend/model/scaler.pkl`
- `backend/model/feature_names.json`
- `backend/model/decision_threshold.json`
- `backend/model/deployment_metadata.json`

They are small compared with the bundled virtual environment. Do not remove, regenerate, or independently replace one artifact without checking compatibility with the others. Pickle-based artifacts should only be loaded from a trusted source. The metadata records model-development details and explicitly notes that training on the development subjects does not itself provide an unbiased estimate of generalization performance.

## Lightweight repository policy

The repository excludes local virtual environments, Python caches, runtime logs, PID files, the real `.env`, and the generated `extracted_features.csv`. These are recreated locally and are not required source files. The firmware, backend algorithms, frontend, dependency manifest, and model artifacts are retained.

## Current limitations to address before claiming reliable real-time performance

This lightweight package is a repository cleanup, not a claim that the algorithm has been revalidated. Before relying on predictions or presenting scientific performance figures, test and resolve the sliding-window cadence/duplicate-window behavior, verify the baseline-normalization procedure against the training pipeline, validate actual sensor sampling/timestamp behavior, and enforce ownership checks for every session/readings endpoint. Run held-out-subject evaluation and publish measured metrics rather than relying on tuning/cross-validation metadata alone.

No clinical validation is provided. Use the output for development/research only.
