"""
MySQL-backed session and reading storage, so past sessions can be pulled up
later. Connection settings come from environment variables (see .env.example)
so your real password never has to be hardcoded or committed anywhere.
"""

import os
import re
import time
import uuid
from contextlib import contextmanager

import mysql.connector
from dotenv import load_dotenv

load_dotenv()  # reads a .env file in this folder, if present

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "stress_dashboard")
if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", DB_NAME):
    raise ValueError("DB_NAME must be a SQL identifier containing only letters, digits, and underscores")
if not (1 <= DB_PORT <= 65535):
    raise ValueError("DB_PORT must be between 1 and 65535")


def _connect(with_db: bool = True):
    return mysql.connector.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME if with_db else None,
    )


@contextmanager
def get_conn():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Creates the database (if missing) and both tables. Safe to call every startup."""
    conn = _connect(with_db=False)
    cur = conn.cursor()
    cur.execute(f"CREATE DATABASE IF NOT EXISTS {DB_NAME}")
    conn.commit()
    cur.close()
    conn.close()

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(64) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                created_at DOUBLE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id VARCHAR(36) PRIMARY KEY,
                user_id INT,
                subject_id VARCHAR(100),
                start_time DOUBLE,
                end_time DOUBLE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
            )
        """)
        try:  # migrate DBs created before multi-login existed
            cur.execute("ALTER TABLE sessions ADD COLUMN user_id INT")
        except mysql.connector.Error:
            pass
        cur.execute("""
            CREATE TABLE IF NOT EXISTS readings (
                id INT AUTO_INCREMENT PRIMARY KEY,
                session_id VARCHAR(36),
                timestamp DOUBLE,
                hr DOUBLE, sdnn DOUBLE, rmssd DOUBLE,
                eda_mean DOUBLE, eda_std DOUBLE, eda_min DOUBLE, eda_max DOUBLE,
                eda_range DOUBLE, eda_slope DOUBLE,
                stress_label INT, stress_prob DOUBLE,
                INDEX idx_session (session_id),
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
            )
        """)
        cur.close()


def create_user(username: str, password_hash: str) -> None:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (%s, %s, %s)",
            (username, password_hash, time.time()),
        )
        cur.close()


def get_user_by_username(username: str):
    with get_conn() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM users WHERE username = %s", (username,))
        row = cur.fetchone()
        cur.close()
        return row


def get_user_by_id(user_id: int):
    with get_conn() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT id, username, created_at FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        cur.close()
        return row


def start_session(subject_id: str = "SUBJECT-01", user_id: int | None = None) -> dict:
    session_id = str(uuid.uuid4())
    now = time.time()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO sessions (id, user_id, subject_id, start_time, end_time) VALUES (%s, %s, %s, %s, NULL)",
            (session_id, user_id, subject_id, now),
        )
        cur.close()
    return {"id": session_id, "user_id": user_id, "subject_id": subject_id, "start_time": now, "end_time": None}


def get_session(session_id: str, user_id: int):
    """Return a session only when it is owned by the given authenticated user."""
    with get_conn() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT id, user_id, subject_id, start_time, end_time FROM sessions WHERE id = %s AND user_id = %s",
            (session_id, user_id),
        )
        row = cur.fetchone()
        cur.close()
        return row


def stop_session(session_id: str, user_id: int) -> bool:
    """Stop only an owned, still-active session; return whether one row changed."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE sessions SET end_time = %s WHERE id = %s AND user_id = %s AND end_time IS NULL",
            (time.time(), session_id, user_id),
        )
        changed = cur.rowcount == 1
        cur.close()
        return changed


def insert_reading(session_id: str, timestamp: float, features: dict, label: int, prob: float):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO readings
               (session_id, timestamp, hr, sdnn, rmssd, eda_mean, eda_std,
                eda_min, eda_max, eda_range, eda_slope, stress_label, stress_prob)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                session_id, timestamp, features["hr"], features["sdnn"], features["rmssd"],
                features["eda_mean"], features["eda_std"], features["eda_min"],
                features["eda_max"], features["eda_range"], features["eda_slope"],
                label, prob,
            ),
        )
        cur.close()


def list_sessions(user_id: int, limit: int = 200) -> list[dict]:
    with get_conn() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT s.id, s.user_id, s.subject_id, s.start_time, s.end_time,
                   COUNT(r.id) AS num_readings,
                   CAST(COALESCE(SUM(r.stress_label), 0) AS UNSIGNED) AS stressed_count
            FROM sessions s
            LEFT JOIN readings r ON r.session_id = s.id
            WHERE s.user_id = %s
            GROUP BY s.id, s.user_id, s.subject_id, s.start_time, s.end_time
            ORDER BY s.start_time DESC LIMIT %s
        """, (user_id, max(1, min(int(limit), 500))))
        rows = cur.fetchall()
        cur.close()
        return rows


def get_session_readings(session_id: str, user_id: int, limit: int = 300) -> list[dict]:
    """Fetch bounded readings only for a session owned by this user."""
    with get_conn() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """SELECT r.id, r.session_id, r.timestamp, r.hr, r.sdnn, r.rmssd,
                      r.eda_mean, r.eda_std, r.eda_min, r.eda_max, r.eda_range,
                      r.eda_slope, r.stress_label, r.stress_prob
               FROM readings r JOIN sessions s ON s.id = r.session_id
               WHERE r.session_id = %s AND s.user_id = %s
               ORDER BY r.timestamp DESC LIMIT %s""",
            (session_id, user_id, max(1, min(int(limit), 1000))),
        )
        rows = cur.fetchall()
        cur.close()
        rows.reverse()
        return rows
