import importlib.util
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tests"))
import test_support  # allows session-manager unit tests without hardware serial

os.environ.setdefault("DASH_SECRET_KEY", "test-only-random-secret-which-is-long-enough-123456789")
_missing = [name for name in ("flask", "flask_sock", "mysql", "dotenv") if importlib.util.find_spec(name) is None]
webapp = None
APP_IMPORT_ERROR = None
if not _missing:
    # API tests exercise authorization with an injected in-memory bundle; the
    # separate artifact test validates the actual version-sensitive pickles.
    # Once runtime dependencies exist, an application import failure is a real
    # test-discovery failure and must not be converted into a skipped suite.
    import inference as _inference
    with patch.object(_inference, "get_bundle", return_value=object()):
        import app as webapp
else:
    APP_IMPORT_ERROR = "missing runtime dependencies: " + ", ".join(_missing)


@unittest.skipIf(APP_IMPORT_ERROR is not None, APP_IMPORT_ERROR or "application dependencies unavailable")
class APIOwnershipTests(unittest.TestCase):
    def setUp(self):
        webapp.app.config.update(TESTING=True, SECRET_KEY="unit-test-secret-key-long-enough-123456")
        webapp.current_session = None
        webapp.session_manager.active_session_id = None
        webapp.session_manager.active_user_id = None
        self.owner_sessions = {"owner-session": {"id": "owner-session", "user_id": 1,
                                                 "subject_id": "S1", "start_time": 1.0, "end_time": None}}
        self._patchers = [
            patch.object(webapp.db, "get_session", side_effect=self.fake_get_session),
            patch.object(webapp.db, "get_session_readings", side_effect=self.fake_get_readings),
            patch.object(webapp.db, "stop_session", side_effect=self.fake_stop_session),
            patch.object(webapp.db, "start_session", side_effect=self.fake_start_session),
            patch.object(webapp.db, "insert_reading"),
            patch.object(webapp, "_broadcast"),
        ]
        self.mocks = [p.start() for p in self._patchers]
        (self.mock_get_session, self.mock_readings, self.mock_db_stop,
         self.mock_db_start, self.mock_insert, self.mock_broadcast) = self.mocks
        self._db_lock = threading.Lock()
        self._start_counter = 0

    def tearDown(self):
        for patcher in reversed(self._patchers):
            patcher.stop()
        webapp.current_session = None
        webapp.session_manager.active_session_id = None
        webapp.session_manager.active_user_id = None

    def fake_get_session(self, session_id, user_id):
        row = self.owner_sessions.get(session_id)
        return dict(row) if row and row["user_id"] == user_id else None

    def fake_get_readings(self, session_id, user_id, limit=300):
        if not self.fake_get_session(session_id, user_id):
            return []
        return [{"session_id": session_id, "timestamp": 1.0, "stress_label": 1,
                 "stress_prob": 0.8, "hr": 80}]

    def fake_stop_session(self, session_id, user_id):
        row = self.fake_get_session(session_id, user_id)
        if not row or row.get("end_time") is not None:
            return False
        self.owner_sessions[session_id]["end_time"] = 2.0
        return True

    def fake_start_session(self, subject_id, user_id=None):
        with self._db_lock:
            self._start_counter += 1
            session_id = f"session-{self._start_counter}"
            row = {"id": session_id, "user_id": user_id, "subject_id": subject_id,
                   "start_time": 1.0, "end_time": None}
            self.owner_sessions[session_id] = row
            return dict(row)

    @staticmethod
    def client_for(user_id):
        client = webapp.app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = user_id
            sess["username"] = f"user{user_id}"
        return client

    def test_cross_user_reading_access_is_denied(self):
        response = self.client_for(2).get("/api/sessions/owner-session/readings")
        self.assertEqual(response.status_code, 404)
        self.assertIn("error", response.get_json())
        self.mock_readings.assert_not_called()

    def test_unauthorized_stop_does_not_call_stop_or_change_active_session(self):
        webapp.current_session = dict(self.owner_sessions["owner-session"])
        webapp.session_manager.active_session_id = "owner-session"
        with patch.object(webapp.session_manager, "stop_session") as stop_manager:
            response = self.client_for(2).post("/api/sessions/owner-session/stop")
            self.assertEqual(response.status_code, 404)
            stop_manager.assert_not_called()
        self.mock_db_stop.assert_not_called()
        self.assertEqual(webapp.session_manager.active_session_id, "owner-session")

    def test_authorized_stop_serializes_persistence_before_clearing_session(self):
        webapp.current_session = dict(self.owner_sessions["owner-session"])
        webapp.session_manager.active_session_id = "owner-session"
        webapp.session_manager.active_user_id = 1

        def stop_manager(session_id):
            self.assertTrue(self.mock_db_stop.called, "database end_time must commit before acquisition is cleared")
            return session_id

        with patch.object(webapp.session_manager, "stop_session", side_effect=stop_manager):
            response = self.client_for(1).post("/api/sessions/owner-session/stop",
                                               headers={"Origin": "http://localhost"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(webapp.current_session)
        self.mock_db_stop.assert_called_once_with("owner-session", 1)

    def test_current_session_is_hidden_from_other_user(self):
        webapp.current_session = dict(self.owner_sessions["owner-session"])
        response = self.client_for(2).get("/api/sessions/current")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get_json())

    def test_two_simultaneous_session_starts_yield_one_winner_and_one_conflict(self):
        responses = []
        barrier = threading.Barrier(2)

        def fake_manager_start(session_id, user_id):
            self.assertIsNone(webapp.session_manager.active_session_id)
            webapp.session_manager.active_session_id = session_id
            webapp.session_manager.active_user_id = user_id

        def run(user_id):
            client = self.client_for(user_id)
            barrier.wait()
            response = client.post("/api/sessions/start", headers={"Origin": "http://localhost"}, json={
                "subject_id": f"SUBJECT-{user_id}", "calibration_acknowledged": True,
            })
            responses.append(response.status_code)

        with patch.object(webapp.session_manager, "start_session", side_effect=fake_manager_start):
            threads = [threading.Thread(target=run, args=(1,)), threading.Thread(target=run, args=(2,))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertCountEqual(responses, [201, 409])
        self.assertIsNotNone(webapp.current_session)
        self.assertIn(webapp.current_session["user_id"], {1, 2})

    def test_prediction_persists_to_originating_session_not_global_current(self):
        webapp.current_session = {"id": "new-session", "user_id": 2}
        result = {"type": "prediction", "session_id": "old-session", "user_id": 1,
                  "window_start": 123.0, "HR": 70, "SDNN": 20, "RMSSD": 15,
                  "EDA_Mean": 1, "EDA_Std": .1, "EDA_Min": .8, "EDA_Max": 1.2,
                  "EDA_Range": .4, "EDA_Slope": .02, "stress_label": 1,
                  "stress_status": "Stressed", "stress_prob": .8, "threshold": .356}
        webapp._on_prediction_result(result)
        self.mock_insert.assert_called_once()
        self.assertEqual(self.mock_insert.call_args.args[0], "old-session")
        self.mock_broadcast.assert_called_once()
        self.assertEqual(self.mock_broadcast.call_args.kwargs["audience_user_id"], 1)

    def test_live_and_stored_reading_status_normalize_consistently(self):
        live = webapp._normalize_reading({"session_id": "s", "window_start": 10,
                                          "stress_label": 1, "EDA_Mean": 2.0})
        stored = webapp._normalize_reading({"session_id": "s", "timestamp": 10,
                                            "stress_label": 1, "eda_mean": 2.0})
        self.assertEqual(live["stress_status"], "Stressed")
        self.assertEqual(stored["stress_status"], "Stressed")
        self.assertEqual(live["eda_mean"], stored["eda_mean"])

    def test_start_requires_calibration_acknowledgement_and_safe_subject_id(self):
        client = self.client_for(1)
        missing_ack = client.post("/api/sessions/start", headers={"Origin": "http://localhost"}, json={"subject_id": "S1"})
        bad_subject = client.post("/api/sessions/start", headers={"Origin": "http://localhost"}, json={"subject_id": "<script>", "calibration_acknowledged": True})
        self.assertEqual(missing_ack.status_code, 400)
        self.assertEqual(bad_subject.status_code, 400)
        self.mock_db_start.assert_not_called()


@unittest.skipIf(APP_IMPORT_ERROR is not None, APP_IMPORT_ERROR or "application dependencies unavailable")
class StartupSecurityTests(unittest.TestCase):
    def test_missing_and_placeholder_secret_fail(self):
        with patch.dict(os.environ, {"DASH_SECRET_KEY": ""}):
            with self.assertRaises(RuntimeError):
                webapp.validate_startup_config()
        for placeholder in ("dev-only-change-me-in-.env", "replace_with_a_unique_random_secret_before_running_123456789"):
            with patch.dict(os.environ, {"DASH_SECRET_KEY": placeholder}):
                with self.assertRaises(RuntimeError):
                    webapp.validate_startup_config()

    def test_strong_secret_passes_configuration_validation(self):
        with patch.dict(os.environ, {"DASH_SECRET_KEY": "a-test-secret-with-more-than-thirty-two-characters-123"}):
            webapp.validate_startup_config()


if __name__ == "__main__":
    unittest.main()
