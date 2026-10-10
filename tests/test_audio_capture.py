"""Process isolation: freeze real IPC, kill stuck children, record again."""

import os
import pathlib
import subprocess
import sys
import tempfile
import threading
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


class TestStandby(unittest.TestCase):
    """A child started between recordings, so the key press only opens the mic."""

    def make_stream(self, mode):
        data, errors = [], []
        stream = audio_capture.InputStream(
            device=0, samplerate=16_000, channels=1, dtype="float32",
            callback=lambda chunk, *_: data.append(chunk), on_error=errors.append,
            command=[sys.executable, str(pathlib.Path(__file__).with_name("audio_child_fixture.py")), mode],
        )
        self.addCleanup(stream.close)
        return stream, data, errors

    def test_a_prepared_child_opens_the_microphone_only_when_started(self):
        # An idle standby must not hold the mic, or macOS shows its indicator
        marker = pathlib.Path(tempfile.mkdtemp()) / "opened"
        with mock.patch.dict(os.environ, {"KAHO_TEST_OPENED": str(marker)}):
            stream, chunks, _ = self.make_stream("native_standby")
            stream.prepare()
            self.assertTrue(stream.ready.wait(5), "child never reported ready")
            time.sleep(0.2)
            self.assertFalse(marker.exists(), "the microphone was opened before start()")
            self.assertTrue(stream.usable())
            stream.start()
        self.assertTrue(marker.exists())
        stream.freeze()
        self.assertEqual(sum(len(c) for c in chunks), 1600)
        self.assertFalse(stream.usable(), "a started stream cannot be reused as a standby")

    def test_a_discarded_standby_exits_without_opening_the_microphone(self):
        marker = pathlib.Path(tempfile.mkdtemp()) / "opened"
        with mock.patch.dict(os.environ, {"KAHO_TEST_OPENED": str(marker)}):
            stream, _, errors = self.make_stream("native_standby")
            stream.prepare()
            self.assertTrue(stream.ready.wait(5))
            stream.close()
        self.assertIsNotNone(stream.process.returncode)
        self.assertFalse(marker.exists())
        self.assertEqual(errors, [], "discarding a standby is not a microphone failure")

    def test_a_standby_that_died_is_not_usable(self):
        stream, _, _ = self.make_stream("start_error")
        stream.prepare()
        stream.reader.join(5)
        self.assertFalse(stream.usable())
        with self.assertRaises(audio_capture.CaptureError):
            stream.start()

    def test_open_and_stop_arriving_together_are_both_obeyed(self):
        # A press shorter than one pipe read: "open\nstop\n" lands as one chunk
        stream, chunks, _ = self.make_stream("native_standby")
        stream.prepare()
        self.assertTrue(stream.ready.wait(5))
        stream.process.stdin.write(b"open\nstop\n")
        stream.reader.join(5)
        self.assertTrue(stream.frozen.is_set())
        self.assertEqual(sum(len(c) for c in chunks), 1600)

    def test_idle_standby_does_not_time_out_before_the_first_audio_callback(self):
        stream, chunks, errors = self.make_stream("native_delayed_first_audio")
        received = threading.Event()

        def receive(chunk, *_):
            chunks.append(chunk)
            received.set()

        stream.callback = receive
        stream.prepare()
        self.assertTrue(stream.ready.wait(5))
        # Longer than the two-second no-audio watchdog, but the mic is closed.
        time.sleep(2.1)
        stream.start()
        self.assertTrue(received.wait(1), f"first audio was lost: {errors}")
        stream.freeze()
        self.assertEqual(sum(len(c) for c in chunks), 1600)
        self.assertEqual(errors, [])

    def test_a_started_microphone_without_audio_still_times_out(self):
        stream, chunks, errors = self.make_stream("native_no_audio")
        stream.prepare()
        self.assertTrue(stream.ready.wait(5))
        stream.start()
        self.assertFalse(stream.frozen.wait(0.2), "watchdog fired before its grace period")
        stream.reader.join(3)
        self.assertFalse(stream.reader.is_alive(), "missing microphone audio was not detected")
        self.assertEqual(chunks, [])
        self.assertEqual(errors, ["microphone stopped delivering audio"])
