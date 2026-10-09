"""
Stress monitoring backend (Flask edition) -- REAL sensor pipeline.

Runs as ONE process on ONE port: serves the plain HTML/CSS/JS frontend,
the REST API, and the live WebSocket stream together. Just:

    python app.py

Pipeline (see session_manager.py + inference.py for the full chain):
  ESP32 (64 Hz JSON over serial)
    -> receiver.SerialReceiver           [unchanged from sensor_pipeline]
    -> buffer.SlidingWindowBuffer         [unchanged, 3840/1920 samples]
    -> preprocessing.window_to_arrays     [unchanged]
    -> preprocessing.passes_quality_gate  [unchanged, motion gate]
    -> feature_extraction.extract_window_features  [unchanged PPG/EDA]
    -> inference.SessionInferenceState.predict      [NEW: real model]
    -> WebSocket broadcast + MySQL insert_reading

This file intentionally contains ZERO synthetic/simulated data generation
-- see session_manager.py's docstring for how START/STOP gates which
samples ever reach a buffer, and inference.py's docstring for exactly
which preprocessing steps run before the real final_model.pkl sees a
feature vector.
"""

import json
import logging
import os
import threading
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, session, redirect
from flask_cors import CORS
from flask_sock import Sock
from werkzeug.security import generate_password_hash, check_password_hash

import db
import inference
from session_manager import SessionManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("app")

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

app = Flask(__name__, static_folder=str(FRONTEND_DIR), static_url_path="")
app.secret_key = os.getenv("DASH_SECRET_KEY", "dev-only-change-me-in-.env")
CORS(app, supports_credentials=True)
sock = Sock(app)


def login_required(view):
    """Every API route wrapped with this needs a logged-in session (multi-user via MySQL)."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "auth required"}), 401
        return view(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------------------
# Real sensor + model pipeline
# ---------------------------------------------------------------------------
# Fail loudly and immediately if the deployment artifacts are missing or
# inconsistent -- per spec, this system must never silently fall back to
# fake predictions. If this raises, `python app.py` exits before binding
# any port at all, rather than serving a broken /health as 200 OK.
try:
    inference.get_bundle()
except Exception:
    logger.exception(
        "FATAL: could not load model deployment artifacts from "
        "backend/model/ (feature_names.json, scaler.pkl, final_model.pkl, "
        "decision_threshold.json). Server will not start."
    )
    raise

current_session: dict | None = None  # mirrors db.start_session()'s return shape
clients: list = []
clients_lock = threading.Lock()


def _broadcast(payload: dict) -> None:
    data = json.dumps(payload)
    with clients_lock:
        dead = []
        for ws in clients:
            try:
                ws.send(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            clients.remove(ws)


def _on_prediction_result(result: dict) -> None:
    """Called from session_manager's background processing thread for
    every completed window: a real prediction, a rejected window, a
    warm-up status, or an error -- never fabricated data."""
    global current_session

    if result.get("type") == "prediction" and current_session:
        try:
            db.insert_reading(
                current_session["id"],
                result["window_start"],
                {
                    "hr": result["HR"], "sdnn": result["SDNN"],
                    "rmssd": result["RMSSD"], "eda_mean": result["EDA_Mean"],
                    "eda_std": result["EDA_Std"], "eda_min": result["EDA_Min"],
                    "eda_max": result["EDA_Max"], "eda_range": result["EDA_Range"],
                    "eda_slope": result["EDA_Slope"],
                },
                result["stress_label"],
                result["stress_prob"],
            )
        except Exception:
            logger.exception("Failed to persist reading to MySQL (non-fatal).")

    # Reshape to the flat, lowercase-key wire format the existing
    # frontend (app.js METRICS / handleReading) already expects, so the
    # UI's rendering code does not need to change.
    payload = {
        "type": "reading" if result.get("type") == "prediction" else result.get("type"),
        "timestamp": result.get("window_start"),
        "session_id": result.get("session_id"),
    }
    if result.get("type") == "prediction":
        payload.update(
            {
                "hr": result["HR"], "sdnn": result["SDNN"], "rmssd": result["RMSSD"],
                "eda_mean": result["EDA_Mean"], "eda_std": result["EDA_Std"],
                "eda_min": result["EDA_Min"], "eda_max": result["EDA_Max"],
                "eda_range": result["EDA_Range"], "eda_slope": result["EDA_Slope"],
                "stress_label": result["stress_label"],
                "stress_prob": result["stress_prob"],
                "stress_status": result["stress_status"],
                "decision_threshold": result["threshold"],
            }
        )
    elif result.get("type") == "warming_up":
        payload.update(
            {
                "windows_seen": result.get("windows_seen"),
                "windows_needed": result.get("windows_needed"),
            }
        )
    else:  # window_rejected / window_error
        payload.update(
            {"reason": result.get("reason"), "detail": result.get("detail")}
        )

    _broadcast(payload)


def _on_status_update(status: dict) -> None:
    _broadcast(status)


session_manager = SessionManager(
    on_result=_on_prediction_result, on_status=_on_status_update
)


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------
@sock.route("/ws/stream")
def stream(ws):
    if "user_id" not in session:
        ws.close()
        return
    if current_session:
        recent = db.get_session_readings(current_session["id"])[-300:]
        ws.send(json.dumps({"type": "history", "data": recent, "session": current_session}))
    ws.send(json.dumps({
        "type": "esp32_status",
        "connected": session_manager.esp32_connected,
    }))
    with clients_lock:
        clients.append(ws)
    try:
        while True:
            msg = ws.receive()  # blocks; None/exception means the client disconnected
            if msg is None:
                break
    except Exception:
        pass
    finally:
        with clients_lock:
            if ws in clients:
                clients.remove(ws)


# ---------------------------------------------------------------------------
# Frontend (served from the same process/port -- no separate dev server)
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    if "user_id" not in session:
        return redirect("/login")
    return send_from_directory(app.static_folder, "index.html")


@app.route("/login")
def login_page():
    return send_from_directory(app.static_folder, "login.html")


# ---------------------------------------------------------------------------
# Auth (multi-login, MySQL-backed -- each account only sees its own sessions)
# ---------------------------------------------------------------------------
@app.post("/api/auth/register")
def api_register():
    data = request.get_json(force=True, silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if len(username) < 3 or len(password) < 6:
        return jsonify({"error": "username needs 3+ chars, password needs 6+ chars"}), 400
    if db.get_user_by_username(username):
        return jsonify({"error": "username already taken"}), 409
    db.create_user(username, generate_password_hash(password))
    return jsonify({"registered": username})


@app.post("/api/auth/login")
def api_login():
    data = request.get_json(force=True, silent=True) or {}
    user = db.get_user_by_username((data.get("username") or "").strip())
    if not user or not check_password_hash(user["password_hash"], data.get("password") or ""):
        return jsonify({"error": "invalid username or password"}), 401
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    return jsonify({"id": user["id"], "username": user["username"]})


@app.post("/api/auth/logout")
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/auth/me")
def api_me():
    if "user_id" not in session:
        return jsonify(None)
    return jsonify({"id": session["user_id"], "username": session["username"]})


# ---------------------------------------------------------------------------
# Session management -- START/STOP now drives the REAL sensor pipeline
# ---------------------------------------------------------------------------
@app.post("/api/sessions/start")
@login_required
def api_start_session():
    global current_session
    subject_id = request.args.get("subject_id", session["username"])
    if current_session:
        db.stop_session(current_session["id"])
        session_manager.stop_session()
    current_session = db.start_session(subject_id, user_id=session["user_id"])
    session_manager.start_session(current_session["id"])
    return jsonify(current_session)


@app.post("/api/sessions/<session_id>/stop")
@login_required
def api_stop_session(session_id):
    db.stop_session(session_id)
    global current_session
    if current_session and current_session["id"] == session_id:
        current_session = None
    if session_manager.active_session_id == session_id:
        session_manager.stop_session()
    return jsonify({"stopped": session_id})


@app.get("/api/sessions")
@login_required
def api_list_sessions():
    return jsonify(db.list_sessions(user_id=session["user_id"]))


@app.get("/api/sessions/<session_id>/readings")
@login_required
def api_session_readings(session_id):
    return jsonify(db.get_session_readings(session_id))


@app.get("/api/sessions/current")
@login_required
def api_current_session():
    return jsonify(current_session)


@app.get("/health")
def health():
    bundle = inference.get_bundle()
    return jsonify({
        "status": "ok",
        "clients_connected": len(clients),
        "esp32_connected": session_manager.esp32_connected,
        "active_session": session_manager.active_session_id,
        "feature_order": bundle.raw_feature_order,
        "decision_threshold": bundle.threshold,
    })


if __name__ == "__main__":
    db.init_db()  # creates DB + users/sessions/readings tables if missing
    session_manager.start_background_threads()  # starts ESP32 serial link
    app.run(host="0.0.0.0", port=5000, threaded=True)
