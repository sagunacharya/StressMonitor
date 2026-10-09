"""Flask API/UI server for the single physical ESP32 sensor stream."""
from __future__ import annotations
import json
import logging
import os
import re
import threading
import time
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory, session, redirect
from flask_sock import Sock
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv(Path(__file__).parent / ".env")
import db
import inference
from session_manager import SessionManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("app")
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

app = Flask(__name__, static_folder=str(FRONTEND_DIR), static_url_path="")
app.secret_key = os.getenv("DASH_SECRET_KEY")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "0").strip().lower() in {"1", "true", "yes"},
    PERMANENT_SESSION_LIFETIME=60 * 60 * 8,
)
sock = Sock(app)

SESSION_SECRET_PLACEHOLDERS = {
    "dev-only-change-me-in-.env", "change_this_to_a_long_random_string",
    "replace_with_a_unique_random_secret_before_running_123456789",
    "changeme", "secret", "password"
}
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
SUBJECT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_lifecycle_lock = threading.RLock()
_clients_lock = threading.Lock()
clients: list[dict] = []
current_session: dict | None = None
_login_failures: dict[str, tuple[int, float]] = {}
_login_lock = threading.Lock()


def validate_startup_config() -> None:
    secret = os.getenv("DASH_SECRET_KEY", "").strip()
    if len(secret) < 32 or secret.lower() in SESSION_SECRET_PLACEHOLDERS:
        raise RuntimeError("Set DASH_SECRET_KEY to a random secret of at least 32 characters in backend/.env")
    if not db.DB_NAME:
        raise RuntimeError("DB_NAME must not be empty")


def _json_error(message: str, status: int):
    return jsonify({"error": message}), status


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not isinstance(session.get("user_id"), int):
            return _json_error("Authentication required", 401)
        return view(*args, **kwargs)
    return wrapped


@app.before_request
def validate_same_origin_for_state_changes():
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    # Modern browsers send Origin on fetch() POST requests. If supplied, it
    # must match the current host/scheme; SameSite=Lax is an additional layer.
    origin = request.headers.get("Origin")
    referrer = request.headers.get("Referer")
    candidate = origin or referrer
    if not candidate:
        return _json_error("Origin or Referer header is required for state-changing requests", 403)
    parsed = urlparse(candidate)
    expected = urlparse(request.host_url)
    if (parsed.scheme, parsed.netloc) != (expected.scheme, expected.netloc):
        return _json_error("Cross-origin state-changing request rejected", 403)
    return None


def _login_limited(ip: str) -> tuple[bool, int]:
    now = time.monotonic()
    with _login_lock:
        failures, until = _login_failures.get(ip, (0, 0.0))
        if until > now:
            return True, max(1, int(until - now))
        if until and until <= now:
            _login_failures.pop(ip, None)
    return False, 0


def _record_login_failure(ip: str) -> None:
    now = time.monotonic()
    with _login_lock:
        failures, until = _login_failures.get(ip, (0, 0.0))
        failures += 1
        # 5 failed attempts -> 15-minute cooldown. This is process-local,
        # intentionally lightweight; a multi-worker deployment needs shared storage.
        _login_failures[ip] = (failures, now + 900 if failures >= 5 else until)


def _clear_login_failures(ip: str) -> None:
    with _login_lock:
        _login_failures.pop(ip, None)


def _normalize_reading(row: dict) -> dict:
    """Normalize SQL history and live inference to one dashboard wire shape."""
    label = row.get("stress_label")
    try:
        label = int(label) if label is not None else None
    except (TypeError, ValueError):
        label = None
    status = row.get("stress_status")
    if status not in {"Stressed", "Not Stressed"}:
        status = "Stressed" if label == 1 else "Not Stressed" if label == 0 else None
    result = {
        "type": "reading",
        "session_id": row.get("session_id"),
        "timestamp": row.get("timestamp", row.get("window_start")),
        "stress_label": label,
        "stress_status": status,
        "stress_prob": row.get("stress_prob"),
        "decision_threshold": row.get("decision_threshold", row.get("threshold")),
    }
    keys = ("hr", "sdnn", "rmssd", "eda_mean", "eda_std", "eda_min", "eda_max", "eda_range", "eda_slope")
    for key in keys:
        value = row.get(key)
        if value is None:
            # Live feature extraction uses title-cased EDA keys.
            aliases = {"eda_mean": "EDA_Mean", "eda_std": "EDA_Std", "eda_min": "EDA_Min",
                       "eda_max": "EDA_Max", "eda_range": "EDA_Range", "eda_slope": "EDA_Slope"}
            value = row.get(aliases.get(key, key.upper()))
        try:
            result[key] = float(value) if value is not None else None
        except (TypeError, ValueError):
            result[key] = None
    return result


def _broadcast(payload: dict, audience_user_id: int | None = None) -> None:
    # Session-associated payloads are private by default. A missing owner is
    # a programming error, not permission to fan out to every websocket client.
    if (payload.get("session_id") is not None or payload.get("type") in {
            "reading", "warming_up", "calibration_failed", "window_rejected",
            "window_error", "collecting_progress", "session_status", "sampling_gap"}) \
            and audience_user_id is None:
        logger.error("Dropping session-scoped websocket event without an audience")
        return
    wire_payload = {k: v for k, v in payload.items() if k != "user_id"}
    data = json.dumps(wire_payload, allow_nan=False, default=str)
    with _clients_lock:
        recipients = list(clients)
    dead = []
    for item in recipients:
        if audience_user_id is not None and item["user_id"] != audience_user_id:
            continue
        try:
            item["ws"].send(data)
        except Exception:
            dead.append(item)
    if dead:
        with _clients_lock:
            clients[:] = [item for item in clients if item not in dead]


def _on_prediction_result(result: dict) -> None:
    user_id = result.get("user_id")
    if result.get("type") == "prediction":
        # The originating session ID is mandatory; never substitute a global
        # current_session, because a late result may belong to an old session.
        session_id = result.get("session_id")
        if not session_id:
            logger.error("Dropping prediction without originating session ID")
            return
        try:
            db.insert_reading(
                session_id, result["window_start"],
                {"hr": result["HR"], "sdnn": result["SDNN"], "rmssd": result["RMSSD"],
                 "eda_mean": result["EDA_Mean"], "eda_std": result["EDA_Std"],
                 "eda_min": result["EDA_Min"], "eda_max": result["EDA_Max"],
                 "eda_range": result["EDA_Range"], "eda_slope": result["EDA_Slope"]},
                result["stress_label"], result["stress_prob"],
            )
        except Exception:
            logger.exception("Could not persist a session reading")
    if result.get("type") == "prediction":
        payload = _normalize_reading({
            "session_id": result.get("session_id"), "timestamp": result.get("window_start"),
            "HR": result.get("HR"), "SDNN": result.get("SDNN"), "RMSSD": result.get("RMSSD"),
            "EDA_Mean": result.get("EDA_Mean"), "EDA_Std": result.get("EDA_Std"),
            "EDA_Min": result.get("EDA_Min"), "EDA_Max": result.get("EDA_Max"),
            "EDA_Range": result.get("EDA_Range"), "EDA_Slope": result.get("EDA_Slope"),
            "stress_label": result.get("stress_label"), "stress_status": result.get("stress_status"),
            "stress_prob": result.get("stress_prob"), "threshold": result.get("threshold"),
        })
    else:
        payload = {"type": result.get("type", "window_error"), "session_id": result.get("session_id"),
                   "timestamp": result.get("window_start"), "reason": result.get("reason"),
                   "windows_seen": result.get("windows_seen"), "windows_needed": result.get("windows_needed"),
                   "calibration_ready": result.get("calibration_ready")}
    _broadcast(payload, audience_user_id=user_id)


def _on_status_update(status: dict) -> None:
    _broadcast(status, audience_user_id=status.get("user_id"))


# Fail early on unsafe defaults before touching version-sensitive pickle files.
# The actual model artifacts are then verified as one bundle; no fallback exists.
validate_startup_config()
inference.get_bundle()
session_manager = SessionManager(on_result=_on_prediction_result, on_status=_on_status_update)


@sock.route("/ws/stream")
def stream(ws):
    origin = request.headers.get("Origin")
    expected = urlparse(request.host_url)
    parsed_origin = urlparse(origin) if origin else None
    if not parsed_origin or (parsed_origin.scheme, parsed_origin.netloc) != (expected.scheme, expected.netloc):
        ws.close()
        return
    user_id = session.get("user_id")
    if not isinstance(user_id, int):
        ws.close()
        return
    with _lifecycle_lock:
        own_session = dict(current_session) if current_session and current_session.get("user_id") == user_id else None
    with _clients_lock:
        clients.append({"ws": ws, "user_id": user_id})
    try:
        history = []
        public_session = ({k: v for k, v in own_session.items() if k != "user_id"}
                          if own_session else None)
        if own_session:
            try:
                history = [_normalize_reading(r) for r in db.get_session_readings(own_session["id"], user_id, limit=300)]
            except Exception:
                logger.exception("Could not restore authorized session history")
        ws.send(json.dumps({"type": "history", "data": history, "session": public_session}, default=str))
        ws.send(json.dumps({"type": "esp32_status", "connected": session_manager.esp32_connected}))
        snapshot = session_manager.status_snapshot(user_id)
        if snapshot:
            ws.send(json.dumps(snapshot, default=str))
        while True:
            if ws.receive() is None:
                break
    except Exception:
        pass
    finally:
        with _clients_lock:
            clients[:] = [item for item in clients if item["ws"] is not ws]


@app.route("/")
def index():
    if "user_id" not in session:
        return redirect("/login")
    return send_from_directory(app.static_folder, "index.html")


@app.route("/login")
def login_page():
    return send_from_directory(app.static_folder, "login.html")


@app.post("/api/auth/register")
def api_register():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username") or "").strip()
    password = data.get("password") or ""
    if not USERNAME_RE.fullmatch(username):
        return _json_error("Username must be 3–32 characters (letters, digits, dot, underscore or hyphen)", 400)
    categories = sum(bool(re.search(pattern, password)) for pattern in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]")) if isinstance(password, str) else 0
    if not isinstance(password, str) or len(password) < 12 or categories < 2:
        return _json_error("Password must be at least 12 characters and use at least two character types", 400)
    try:
        if db.get_user_by_username(username):
            return _json_error("Username already taken", 409)
        db.create_user(username, generate_password_hash(password))
    except Exception:
        logger.exception("Registration failed")
        return _json_error("Could not create account; check database setup", 503)
    return jsonify({"registered": username}), 201


@app.post("/api/auth/login")
def api_login():
    ip = request.remote_addr or "unknown"
    limited, retry_after = _login_limited(ip)
    if limited:
        response = jsonify({"error": "Too many failed login attempts; try again later"})
        response.status_code = 429
        response.headers["Retry-After"] = str(retry_after)
        return response
    data = request.get_json(silent=True) or {}
    username = str(data.get("username") or "").strip()
    password = data.get("password") or ""
    try:
        user = db.get_user_by_username(username)
    except Exception:
        logger.exception("Login lookup failed")
        return _json_error("Authentication service temporarily unavailable", 503)
    if not user or not isinstance(password, str) or not check_password_hash(user["password_hash"], password):
        _record_login_failure(ip)
        return _json_error("Invalid username or password", 401)
    _clear_login_failures(ip)
    session.clear()
    session["user_id"] = int(user["id"])
    session["username"] = user["username"]
    session.permanent = True
    return jsonify({"id": user["id"], "username": user["username"]})


@app.post("/api/auth/logout")
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/auth/me")
def api_me():
    if not isinstance(session.get("user_id"), int):
        return jsonify(None)
    return jsonify({"id": session["user_id"], "username": session.get("username")})


@app.post("/api/sessions/start")
@login_required
def api_start_session():
    global current_session
    data = request.get_json(silent=True) or {}
    subject_id = str(data.get("subject_id") or "SUBJECT-01").strip()
    if not SUBJECT_RE.fullmatch(subject_id):
        return _json_error("Subject ID must be 1–64 safe characters (letters, digits, dot, underscore or hyphen)", 400)
    if data.get("calibration_acknowledged") is not True:
        return _json_error("Confirm the subject is seated/resting for baseline calibration before starting", 400)
    user_id = session["user_id"]
    with _lifecycle_lock:
        if current_session is not None or session_manager.active_session_id is not None:
            return _json_error("The physical sensor is already in use by an active session", 409)
        new_session = None
        try:
            new_session = db.start_session(subject_id, user_id=user_id)
            session_manager.start_session(new_session["id"], user_id=user_id)
            current_session = new_session
        except RuntimeError:
            if new_session:
                db.stop_session(new_session["id"], user_id)
            return _json_error("The physical sensor is already in use by an active session", 409)
        except Exception:
            logger.exception("Could not start session")
            if new_session:
                try:
                    db.stop_session(new_session["id"], user_id)
                except Exception:
                    logger.exception("Could not roll back failed session start")
            return _json_error("Could not start session; check database and sensor backend", 503)
    return jsonify(new_session), 201


@app.post("/api/sessions/<session_id>/stop")
@login_required
def api_stop_session(session_id):
    global current_session
    user_id = session["user_id"]
    with _lifecycle_lock:
        try:
            owned = db.get_session(session_id, user_id)
        except Exception:
            logger.exception("Session ownership lookup failed")
            return _json_error("Could not verify session ownership", 503)
        if not owned:
            return _json_error("Session not found", 404)
        if owned.get("end_time") is not None:
            return _json_error("Session is already stopped", 409)
        current_matches = bool(current_session and current_session.get("id") == session_id)
        manager_matches = session_manager.active_session_id == session_id
        # Serialize DB stop with prediction persistence. Results already inside
        # dispatch finish before end_time is written; waiting stale callbacks
        # re-check active session state and are discarded after this block.
        if current_matches or manager_matches:
            with session_manager.dispatch_guard():
                try:
                    if not db.stop_session(session_id, user_id):
                        return _json_error("Session was already stopped or changed", 409)
                except Exception:
                    logger.exception("Could not stop session")
                    return _json_error("Could not stop session; database unavailable", 503)
                if manager_matches:
                    session_manager.stop_session(expected_session_id=session_id)
                if current_matches:
                    current_session = None
        else:
            try:
                if not db.stop_session(session_id, user_id):
                    return _json_error("Session was already stopped or changed", 409)
            except Exception:
                logger.exception("Could not stop session")
                return _json_error("Could not stop session; database unavailable", 503)
    return jsonify({"stopped": True})


@app.get("/api/sessions")
@login_required
def api_list_sessions():
    try:
        limit = min(500, max(1, int(request.args.get("limit", "200"))))
        return jsonify(db.list_sessions(user_id=session["user_id"], limit=limit))
    except (TypeError, ValueError):
        return _json_error("limit must be an integer", 400)
    except Exception:
        logger.exception("Session history query failed")
        return _json_error("Could not load session history", 503)


@app.get("/api/sessions/<session_id>/readings")
@login_required
def api_session_readings(session_id):
    user_id = session["user_id"]
    try:
        if not db.get_session(session_id, user_id):
            return _json_error("Session not found", 404)
        limit = min(1000, max(1, int(request.args.get("limit", "300"))))
        return jsonify([_normalize_reading(r) for r in db.get_session_readings(session_id, user_id, limit)])
    except (TypeError, ValueError):
        return _json_error("limit must be an integer", 400)
    except Exception:
        logger.exception("Reading history query failed")
        return _json_error("Could not load session readings", 503)


@app.get("/api/sessions/current")
@login_required
def api_current_session():
    user_id = session["user_id"]
    with _lifecycle_lock:
        own = ({k: v for k, v in current_session.items() if k != "user_id"}
               if current_session and current_session.get("user_id") == user_id else None)
    if own:
        own.update({k: v for k, v in (session_manager.status_snapshot(user_id) or {}).items()
                    if k not in {"type", "session_id"}})
    return jsonify(own)


@app.get("/health")
def health():
    return jsonify({"status": "ok", "sensor_connected": session_manager.esp32_connected})


if __name__ == "__main__":
    validate_startup_config()
    db.init_db()
    session_manager.start_background_threads()
    # Loopback-only by default; configure DASH_HOST=0.0.0.0 explicitly for LAN use.
    app.run(host=os.getenv("DASH_HOST", "127.0.0.1"), port=int(os.getenv("DASH_PORT", "5000")), threaded=True)
