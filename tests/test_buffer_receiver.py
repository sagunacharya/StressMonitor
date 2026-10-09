import json
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tests"))
import test_support  # test-only import shim; no serial device is opened

import buffer
import config
from receiver import SerialReceiver


def sample(seq, *, t_ms=None):
    row = {"seq": seq, "ir": 100000 + seq % 13, "red": 80000,
           "accMagnitude": 1.0, "eda": 300, "t_recv": seq / config.SAMPLE_RATE_HZ}
    if t_ms is not None:
        row["t_device_ms"] = t_ms
    return row


class SlidingWindowTests(unittest.TestCase):
    def fill(self, buf, start, count):
        for i in range(start, start + count):
            buf.add_sample(sample(i))

    def test_emits_unique_windows_and_advances_exact_step(self):
        buf = buffer.SlidingWindowBuffer()
        self.fill(buf, 0, config.WINDOW_SIZE_SAMPLES)
        first = buf.pop_ready_window()
        self.assertIsNotNone(first)
        self.assertEqual(first["sample_seq_start"], 0)
        self.assertEqual(first["sample_seq_end"], config.WINDOW_SIZE_SAMPLES - 1)
        self.assertEqual(len(first["samples"]), config.WINDOW_SIZE_SAMPLES)
        # Regression for the original defect: repeatedly popping used to
        # return the same full window when no new samples had arrived.
        self.assertIsNone(buf.pop_ready_window())
        self.fill(buf, config.WINDOW_SIZE_SAMPLES, config.WINDOW_STEP_SAMPLES)
        second = buf.pop_ready_window()
        self.assertEqual(second["sample_seq_start"], config.WINDOW_STEP_SAMPLES)
        self.assertEqual(second["sample_seq_end"], config.WINDOW_STEP_SAMPLES + config.WINDOW_SIZE_SAMPLES - 1)
        self.assertIsNone(buf.pop_ready_window())

    def test_reset_discards_stale_samples_and_changes_generation(self):
        buf = buffer.SlidingWindowBuffer()
        self.fill(buf, 0, config.WINDOW_SIZE_SAMPLES)
        generation = buf.generation
        buf.reset()
        self.assertGreater(buf.generation, generation)
        self.assertEqual(buf.buffered_samples, 0)
        self.assertIsNone(buf.pop_ready_window())
        self.fill(buf, 5000, config.WINDOW_SIZE_SAMPLES)
        window = buf.pop_ready_window()
        self.assertEqual(window["sample_seq_start"], 5000)

    def test_memory_is_bounded_during_long_pause(self):
        buf = buffer.SlidingWindowBuffer()
        for i in range(config.WINDOW_SIZE_SAMPLES * 10):
            buf.add_sample(sample(i))
            self.assertLessEqual(buf.buffered_samples, config.WINDOW_SIZE_SAMPLES + config.WINDOW_STEP_SAMPLES)
        self.assertIsNotNone(buf.pop_ready_window())

    def test_concurrent_ingestion_and_extraction_are_safe(self):
        buf = buffer.SlidingWindowBuffer()
        errors = []
        windows = []
        start = threading.Barrier(3)

        def writer(offset):
            try:
                start.wait()
                for i in range(offset, offset + 5000):
                    buf.add_sample(sample(i))
            except Exception as exc:  # surfaced in the main test thread
                errors.append(exc)

        def reader():
            try:
                start.wait()
                for _ in range(3000):
                    item = buf.pop_ready_window()
                    if item:
                        windows.append(item)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(0,)),
                   threading.Thread(target=writer, args=(10000,)),
                   threading.Thread(target=reader)]
        # The barrier has three parties: two writers and one reader.
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertLessEqual(buf.buffered_samples, config.WINDOW_SIZE_SAMPLES + config.WINDOW_STEP_SAMPLES)
        for item in windows:
            self.assertEqual(len(item["samples"]), config.WINDOW_SIZE_SAMPLES)


class SerialParsingTests(unittest.TestCase):
    def test_valid_packet_and_timing_fields(self):
        packet = {"seq": 17, "t_ms": 265, "ir": 100000, "red": 90000,
                  "accMagnitude": 1.02, "eda": 250}
        parsed = SerialReceiver._parse_line(json.dumps(packet).encode())
        self.assertEqual(parsed["seq"], 17)
        self.assertEqual(parsed["t_device_ms"], 265.0)
        self.assertTrue(parsed["t_recv"] > 0)

    def test_malformed_nonfinite_missing_out_of_range_and_status_packets_rejected(self):
        cases = [b"not json", b"", b'{"status":"rate","hz":64}',
                 b'{"ir":1,"red":1,"accMagnitude":1}',
                 b'{"ir":NaN,"red":1,"accMagnitude":1,"eda":1}',
                 b'{"ir":0,"red":1,"accMagnitude":1,"eda":1}',
                 b'{"ir":1,"red":1,"accMagnitude":99,"eda":1}',
                 b'{"seq":true,"ir":1,"red":1,"accMagnitude":1,"eda":1}']
        for packet in cases:
            with self.subTest(packet=packet):
                self.assertIsNone(SerialReceiver._parse_line(packet))

    def test_negative_or_nonmonotonic_timestamp_is_parsed_then_gap_is_diagnosable(self):
        receiver = SerialReceiver(lambda _sample: None)
        first = SerialReceiver._parse_line(b'{"seq":1,"t_ms":20,"ir":1,"red":1,"accMagnitude":1,"eda":1}')
        second = SerialReceiver._parse_line(b'{"seq":3,"t_ms":90,"ir":1,"red":1,"accMagnitude":1,"eda":1}')
        annotated = receiver._annotate_timing(first)
        annotated_next = receiver._annotate_timing(second)
        self.assertFalse(annotated["gap_before"])
        self.assertTrue(annotated_next["gap_before"])

    def test_repeated_ppg_pairs_are_counted_and_eventually_marked_stale(self):
        statuses = []
        receiver = SerialReceiver(lambda _sample: None, on_status=statuses.append)
        packet = b'{"seq":1,"t_ms":10,"ir":100000,"red":90000,"accMagnitude":1,"eda":200}'
        rows = [SerialReceiver._parse_line(packet)]
        for seq in (2, 3, 4):
            rows.append({**rows[0], "seq": seq, "t_device_ms": float(seq * 10)})
        annotated = [receiver._annotate_timing(dict(row)) for row in rows]
        self.assertEqual(receiver.diagnostics()["repeated_ppg_values"], 3)
        self.assertTrue(annotated[-1]["gap_before"])
        self.assertTrue(any(item.get("state") == "stale_ppg_data" for item in statuses))


if __name__ == "__main__":
    unittest.main()
