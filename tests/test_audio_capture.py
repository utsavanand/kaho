"""Process isolation: freeze real IPC, kill stuck children, record again."""

import pathlib
import subprocess
import sys
import time
import unittest
from unittest import mock

import audio_capture


class TestAudioCapture(unittest.TestCase):
    def make_stream(self, mode="normal"):
        data, logs, errors = [], [], []
        stream = audio_capture.InputStream(
            device=0, samplerate=16_000, channels=1, dtype="float32",
            callback=lambda chunk, *_: data.append(chunk),
            on_error=errors.append, log=logs.append,
            command=[sys.executable, str(pathlib.Path(__file__).with_name("audio_child_fixture.py")), mode],
        )
        self.addCleanup(stream.close)
        return stream, data, logs, errors

    def test_freeze_receives_final_frames_and_returns_before_native_close(self):
        stream, chunks, logs, _ = self.make_stream("close_hang")
        stream.start()
        begin = time.monotonic()
        stream.freeze()
        self.assertLess(time.monotonic() - begin, 0.3)
        self.assertEqual(sum(len(c) for c in chunks), 3200)
        self.assertEqual(float(chunks[0][0, 0]), 0.25)
        self.assertEqual(float(chunks[-1][-1, 0]), 0.5)
        self.assertIsNone(stream.process.poll(), "fixture must still be stuck during handoff")
        stream.stop()
        stream.close()
        self.assertIsNotNone(stream.process.returncode)
        self.assertTrue(any("terminated" in line for line in logs))
        self.assertFalse(stream.reader.is_alive())
        # The next capture actually starts and returns audio after the forced kill.
        next_stream, next_chunks, _, _ = self.make_stream()
        next_stream.start()
        next_stream.stop()
        self.assertEqual(sum(len(c) for c in next_chunks), 3200)

    def test_stuck_freeze_preserves_received_audio_and_reaps_child(self):
        stream, chunks, _, _ = self.make_stream("freeze_hang")
        stream.start()
        with mock.patch.object(audio_capture, "FREEZE_TIMEOUT", 0.15):
            stream.freeze()
        self.assertEqual(sum(len(c) for c in chunks), 1600)
        self.assertIsNotNone(stream.process.returncode)
        self.assertTrue(stream.frozen.is_set())

    def test_crash_preserves_partial_audio_and_reports_interruption(self):
        stream, chunks, _, errors = self.make_stream("crash")
        stream.start()
        stream.reader.join(1)
        self.assertEqual(sum(len(c) for c in chunks), 1600)
        self.assertTrue(errors)
        stream.stop()
        self.assertIsNotNone(stream.process.returncode)

    def test_startup_error_and_timeout_close_every_resource(self):
        for mode in ("start_error", "start_hang"):
            with self.subTest(mode=mode):
                stream, _, _, _ = self.make_stream(mode)
                with (mock.patch.object(audio_capture, "START_TIMEOUT", 0.2),
                      self.assertRaises(audio_capture.CaptureError)):
                    stream.start()
                self.assertIsNotNone(stream.process.returncode)
                self.assertTrue(stream.storage.closed)
                self.assertTrue(stream.mapping.closed)
                self.assertFalse(stream.reader.is_alive())

    def test_bad_metadata_cannot_read_outside_shared_audio(self):
        stream, chunks, logs, _ = self.make_stream("bad_frames")
        stream.start()
        stream.stop()
        self.assertEqual(sum(len(c) for c in chunks), 1600)
        self.assertTrue(any("invalid microphone frame count" in line for line in logs))

    def test_cancel_close_discards_child_and_unmaps_private_audio(self):
        stream, _, _, _ = self.make_stream()
        stream.start()
        stream.close()
        self.assertTrue(stream.storage.closed)
        self.assertTrue(stream.mapping.closed)
        self.assertIsNotNone(stream.process.returncode)
        stream.close()  # Idempotent even after a forced shutdown.

    def test_spawn_failure_closes_temporary_storage(self):
        stream, _, _, _ = self.make_stream()
        stream.command = ["/nonexistent/kaho-audio-helper"]
        with self.assertRaises(FileNotFoundError):
            stream.start()
        self.assertTrue(stream.storage.closed)
        self.assertTrue(stream.mapping.closed)

    def test_actual_child_protocol_survives_native_stop_hang(self):
        stream, chunks, logs, _ = self.make_stream("native_stop_hang")
        stream.start()
        stream.freeze()
        self.assertEqual(sum(len(c) for c in chunks), 1600)
        stream.stop()
        self.assertIsNotNone(stream.process.returncode)
        self.assertTrue(any("terminated" in line for line in logs))

    def test_actual_child_protocol_survives_native_open_hang(self):
        stream, _, _, _ = self.make_stream("native_open_hang")
        with (mock.patch.object(audio_capture, "START_TIMEOUT", 0.3),
              self.assertRaises(audio_capture.CaptureError)):
            stream.start()
        self.assertIsNotNone(stream.process.returncode)

    def test_packaged_entrypoint_dispatches_before_gui_and_model_imports(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        result = subprocess.run([sys.executable, str(root / "kaho.py"), "--kaho-audio-helper", "--help"],
                                capture_output=True, text=True, timeout=3, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--audio-fd", result.stdout)
