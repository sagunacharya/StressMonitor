import shutil
import subprocess
import sys
import queue
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tests"))
import test_support  # test-only import shim; no serial device is opened
from session_manager import SessionManager
import buffer


class CallbackTests(unittest.TestCase):
    def test_serial_disconnect_resets_partial_window(self):
        manager = SessionManager.__new__(SessionManager)
        manager._lock = threading.RLock()
        manager._status_queue = queue.Queue(maxsize=16)
        manager.active_session_id = "session-1"
        manager.active_user_id = 7
        manager._active_buffer = buffer.SlidingWindowBuffer()
        manager._active_inference = object()
        for i in range(12):
            manager._active_buffer.add_sample({"ir": i, "t_recv": float(i)})
        generation = manager._active_buffer.generation
        manager._on_status_update({"type": "esp32_status", "connected": False})
        self.assertEqual(manager._active_buffer.buffered_samples, 0)
        self.assertGreater(manager._active_buffer.generation, generation)
        statuses = []
        while not manager._status_queue.empty():
            statuses.append(manager._status_queue.get_nowait())
        self.assertTrue(any(item.get("reason") == "serial_disconnected" for item in statuses))

    def test_old_or_reset_generation_callbacks_are_discarded(self):
        manager = SessionManager.__new__(SessionManager)
        manager._lock = threading.RLock()
        manager._dispatch_lock = threading.RLock()
        manager._on_result = Mock()
        class Buffer:
            generation = 4
        active_buffer = Buffer()
        active_inference = object()
        manager._active_buffer = active_buffer
        manager._active_inference = active_inference
        manager.active_session_id = "session-new"
        manager.active_user_id = 2
        manager._emit_for_session({"type": "prediction"}, active_buffer, active_inference, "session-old", 4)
        manager._emit_for_session({"type": "prediction"}, active_buffer, active_inference, "session-new", 3)
        manager._on_result.assert_not_called()
        payload = {"type": "warming_up"}
        manager._emit_for_session(payload, active_buffer, active_inference, "session-new", 4)
        manager._on_result.assert_called_once_with({"type": "warming_up", "session_id": "session-new", "user_id": 2})


class FrontendContractTests(unittest.TestCase):
    def test_javascript_syntax_when_node_is_available(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not installed")
        for relative in ("frontend/app.js", "frontend/login.js"):
            result = subprocess.run([node, "--check", str(ROOT / relative)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, msg=f"{relative}: {result.stderr}")

    def test_expected_api_and_calibration_contract_is_present(self):
        source = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
        for contract in ("/api/sessions/start", "/api/sessions/${encodeURIComponent(state.session.id)}/stop",
                         "calibration_acknowledged", "type === 'history'", "calibration_failed"):
            self.assertIn(contract, source)


if __name__ == "__main__":
    unittest.main()
