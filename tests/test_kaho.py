"""Unit tests for the parts of Kaho that do not need a Mac.

kaho.py imports AppKit, Quartz, sounddevice, mlx_audio, huggingface_hub and
numpy at module scope, so importing it normally requires an Apple Silicon Mac
with the app's venv installed. Stubbing those six in sys.modules first makes the
pure logic testable anywhere, including CI — which is the point: the tap state
machine, the settings migration and the recording handoff have each shipped a
regression that only a human dictating into the real app could catch.

Deliberately not covered: anything that draws, records, or runs a model.

Run with:  python -m unittest discover -s tests -t .
"""

import ctypes
import json
import os
import pathlib
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock


def _real_numpy():
    """The real numpy if it is installed, else None.

    The audio drop rules need real arrays; nothing else here does. CI installs
    numpy for exactly that reason — without it those cases skip rather than
    testing a MagicMock's opinion of `<`. This has to be decided BEFORE the
    stubs are installed, or `import numpy` finds the stub and answers yes.
    """
    try:
        import numpy
    except ImportError:
        return None
    return numpy


REAL_NUMPY = _real_numpy()
HAVE_NUMPY = REAL_NUMPY is not None


def _install_stubs():
    """Stand in for the Mac-only and ML dependencies before kaho is imported.

    AppKit needs real classes for NSObject and NSView because kaho subclasses
    them at import time; everything else can be a MagicMock, cached on the
    module so a test can assert against the same object the code called.
    """
    class _Stub(types.ModuleType):
        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            value = mock.MagicMock(name=f"{self.__name__}.{name}")
            setattr(self, name, value)
            return value

    appkit = _Stub("AppKit")
    appkit.NSObject = type("NSObject", (), {})
    appkit.NSView = type("NSView", (), {})

    sounddevice = _Stub("sounddevice")
    # Real exception class: kaho catches it, and `except <MagicMock>` is a
    # TypeError at raise time
    sounddevice.PortAudioError = type("PortAudioError", (Exception,), {})

    pyobjctools = types.ModuleType("PyObjCTools")
    pyobjctools.AppHelper = _Stub("PyObjCTools.AppHelper")

    # kaho loads Carbon at import time for IsSecureEventInputEnabled. There is
    # no such framework off a Mac and ctypes.CDLL raises rather than degrading,
    # so the loader itself is stubbed for the life of the test process.
    ctypes.CDLL = mock.MagicMock(name="ctypes.CDLL")

    for name, module in (
        ("AppKit", appkit),
        ("Quartz", _Stub("Quartz")),
        ("sounddevice", sounddevice),
        ("mlx_audio", _Stub("mlx_audio")),
        ("mlx_audio.stt", _Stub("mlx_audio.stt")),
        ("mlx_audio.stt.utils", _Stub("mlx_audio.stt.utils")),
        ("huggingface_hub", _Stub("huggingface_hub")),
        ("numpy", REAL_NUMPY or _Stub("numpy")),
        ("PyObjCTools", pyobjctools),
        ("PyObjCTools.AppHelper", pyobjctools.AppHelper),
    ):
        sys.modules.setdefault(name, module)


_install_stubs()
# The stubs have to be in place before this import, not at the top of the file
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import kaho


class Clock:
    """Monotonic clock the tests drive by hand, so timing windows are exact."""

    def __init__(self, now=1_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


class FakeEvent:
    """The two NSEvent methods handle_flags_changed calls."""

    def __init__(self, keycode, flags):
        self._keycode, self._flags = keycode, flags

    def keyCode(self):
        return self._keycode

    def modifierFlags(self):
        return self._flags


class KahoTestCase(unittest.TestCase):
    """Resets the module globals and redirects every path into a tmpdir."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = pathlib.Path(self.tmp.name)

        for name, value in (
            ("LOG_PATH", str(tmp / "Kaho.log")),
            ("SUPPORT_DIR", str(tmp)),
            ("HISTORY_PATH", str(tmp / "history.jsonl")),
            ("SETTINGS_PATH", str(tmp / "settings.json")),
            ("DICTIONARY_PATH", str(tmp / "dictionary.txt")),
            ("state", "ready"),
            ("locked", False),
            ("press_time", 0.0),
            ("last_tap", 0.0),
            ("lock_time", 0.0),
            ("stream", None),
            ("record_buf", None),
            ("audio_op_started", None),
            ("recording", None),
            
            ("history_version", 0),
            ("rewriter", None),
        ):
            self.enterContext(mock.patch.object(kaho, name, value))
        self.enterContext(mock.patch.object(
            kaho, "settings", {"hotkey": "right_option", "rewrite": "off", "language": "auto",
             "trigger": "hold", "rewrite_backend": "local",
             "api_url": "", "api_model": ""}
        ))
        self.enterContext(mock.patch.object(kaho, "overlay", mock.MagicMock()))

        # Keep the log out of stdout, and let tests read what was logged
        self.logged = []
        self.enterContext(mock.patch.object(kaho, "log", self.logged.append))

        kaho.history.clear()
        while not kaho.audio_ops.empty():
            kaho.audio_ops.get()
        while not kaho.jobs.empty():
            kaho.jobs.get()

        self.clock = Clock()
        self.enterContext(mock.patch("kaho.time.monotonic", self.clock))

        # A fresh stream object per open, so the tests can tell them apart
        kaho.sd.InputStream.reset_mock()
        kaho.sd.InputStream.side_effect = lambda **_: mock.MagicMock(name="stream")
        kaho.AppKit.NSTimer.reset_mock()
        kaho.AppKit.NSPasteboard.reset_mock()
        kaho.AppHelper.callAfter.reset_mock()

    def run_audio_ops(self):
        """Drain the queue the way the audio thread would, in order."""
        while not kaho.audio_ops.empty():
            kaho.audio_ops.get()()

    def down(self):
        keycode, mask, _ = kaho.HOTKEYS[kaho.settings["hotkey"]]
        kaho.handle_flags_changed(FakeEvent(keycode, mask))

    def up(self):
        keycode, _, _ = kaho.HOTKEYS[kaho.settings["hotkey"]]
        kaho.handle_flags_changed(FakeEvent(keycode, 0))

    def deferred_stop(self):
        """The block schedule_deferred_stop handed to NSTimer."""
        calls = kaho.AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_.call_args_list
        self.assertTrue(calls, "no deferred stop was scheduled")
        interval, repeats, block = calls[-1][0]
        self.assertEqual(interval, kaho.DOUBLE_TAP_SECONDS)
        self.assertFalse(repeats)
        return block


class TestTapStateMachine(KahoTestCase):
    def test_hold_then_release_records_and_stops(self):
        self.down()
        self.assertEqual(kaho.state, "recording")
        self.clock.advance(2.0)  # a real hold, well past TAP_MAX_SECONDS
        self.up()
        self.assertEqual(kaho.state, "ready")
        kaho.overlay.setPhase_.assert_called_with("transcribing")

    def test_other_keys_are_ignored(self):
        other = kaho.HOTKEYS["right_shift"][0]
        kaho.handle_flags_changed(FakeEvent(other, kaho.HOTKEYS["right_shift"][1]))
        self.assertEqual(kaho.state, "ready")

    def test_a_tap_defers_the_stop_instead_of_running_it(self):
        # Stopping immediately is what forced the reopen that recorded silence
        self.down()
        self.clock.advance(0.1)
        self.up()
        self.assertEqual(kaho.state, "recording")
        self.deferred_stop()

    def test_the_deferred_stop_fires_when_no_second_tap_arrives(self):
        self.down()
        self.clock.advance(0.1)
        self.up()
        block = self.deferred_stop()
        self.clock.advance(kaho.DOUBLE_TAP_SECONDS)
        block(None)
        self.assertEqual(kaho.state, "ready")

    def test_a_natural_double_tap_locks_hands_free(self):
        # 0.7 s apart on purpose: DOUBLE_TAP_SECONDS was 0.5 s in 1.7.4, which
        # was tighter than a real double-tap, and attempts logged at 0.6-0.8 s
        # silently did nothing
        self.down()
        self.clock.advance(0.1)
        self.up()
        block = self.deferred_stop()
        self.clock.advance(0.7)
        self.down()
        self.clock.advance(0.1)
        self.up()
        self.assertTrue(kaho.locked)
        self.assertEqual(kaho.state, "recording")
        # The first tap's pending stop must not tear the live stream down
        block(None)
        self.assertEqual(kaho.state, "recording")

    def test_a_double_tap_never_reopens_the_audio_stream(self):
        """The 1.7.5 bug: hands-free recorded a silent room.

        Stopping on the first tap and reopening ~60 ms later handed back a
        stream that captured silence, because PortAudio had not finished
        releasing the device — and Whisper hallucinated fluent paragraphs from
        that noise floor. One hold, one stream: the double-tap must run start
        to lock without a stop in between.
        """
        opened = []
        kaho.sd.InputStream.side_effect = lambda **_: opened.append(mock.MagicMock()) or opened[-1]
        self._lock_hands_free()
        self.run_audio_ops()
        self.assertEqual(len(opened), 1, "the double-tap reopened the stream")
        opened[0].stop.assert_not_called()
        opened[0].close.assert_not_called()

    def test_the_first_tap_after_launch_never_locks_on_its_own(self):
        # Regression (1.7.5): last_tap starts at 0.0 while monotonic() counts
        # from boot, so `now - last_tap` was small enough to match a pair on a
        # launch soon after boot
        self.clock.now = 0.2
        self.down()
        self.clock.advance(0.1)
        self.up()
        self.assertFalse(kaho.locked)
        self.assertEqual(kaho.state, "recording")

    def test_a_tap_inside_the_lock_grace_is_the_tail_of_the_double_tap(self):
        self._lock_hands_free()
        self.clock.advance(kaho.LOCK_GRACE_SECONDS / 2)
        self.down()
        self.assertTrue(kaho.locked)
        self.assertEqual(kaho.state, "recording")

    def test_a_tap_after_the_lock_grace_stops_the_recording(self):
        self._lock_hands_free()
        self.clock.advance(kaho.LOCK_GRACE_SECONDS + 0.1)
        self.down()
        self.assertFalse(kaho.locked)
        self.assertEqual(kaho.state, "ready")

    def test_releasing_the_key_while_locked_keeps_recording(self):
        self._lock_hands_free()
        self.clock.advance(0.1)
        self.up()
        self.assertTrue(kaho.locked)
        self.assertEqual(kaho.state, "recording")

    def _lock_hands_free(self):
        self.down()
        self.clock.advance(0.1)
        self.up()
        self.clock.advance(0.7)
        self.down()
        self.clock.advance(0.1)
        self.up()
        self.assertTrue(kaho.locked, "setup failed to lock hands-free")


class TestRecordingHandoff(KahoTestCase):
    """The stream must end up owned by exactly one side of the handoff.

    These pin the two orderings that are reachable from a test: the open lands
    before the release, and the open is still queued when the release arrives.
    The interleaving that caused the leak — a release between _open_stream's
    check and its publish — is two adjacent statements apart and cannot be
    staged from outside the function, which is exactly why the fix is a lock
    around the handoff rather than a test asserting it never happens.
    """

    def test_a_completed_recording_closes_its_stream_once(self):
        kaho.start_recording()
        buf = kaho.record_buf
        self.run_audio_ops()  # the open lands while the key is still held
        opened = kaho.stream
        self.assertIsNotNone(opened)

        kaho.stop_recording()
        self.assertIsNone(kaho.stream)
        self.run_audio_ops()
        opened.stop.assert_called_once()
        opened.close.assert_called_once()
        self.assertEqual(kaho.record_buf, None)
        self.assertIsNot(buf, None)

    def test_a_release_before_the_open_lands_leaves_no_stream_behind(self):
        opened = []
        kaho.sd.InputStream.side_effect = lambda **_: opened.append(mock.MagicMock()) or opened[-1]
        kaho.start_recording()
        kaho.stop_recording()  # key released while the open is still queued
        self.run_audio_ops()
        self.assertEqual(len(opened), 1, "expected exactly one stream to be opened")
        opened[0].stop.assert_called_once()
        opened[0].close.assert_called_once()
        self.assertIsNone(kaho.stream)

    def test_a_stream_that_fails_to_start_is_closed(self):
        """QA finding: an unclosed stream keeps the device claimed, so one
        bad open makes every later open fail too."""
        opened = mock.MagicMock()
        opened.start.side_effect = kaho.sd.PortAudioError("device busy")
        kaho.sd.InputStream.side_effect = lambda **_: opened

        kaho.start_recording()
        self.run_audio_ops()

        opened.close.assert_called_once()
        self.assertEqual(kaho.state, "ready")

    def test_a_failed_open_returns_to_ready(self):
        kaho.sd.InputStream.side_effect = kaho.sd.PortAudioError("no device")
        kaho.start_recording()
        self.assertEqual(kaho.state, "recording")
        self.run_audio_ops()
        self.assertEqual(kaho.state, "ready")
        self.assertFalse(kaho.locked)

    def test_a_wedged_audio_device_skips_the_recording(self):
        kaho.audio_op_started = self.clock.now - (kaho.WEDGE_SECONDS + 5)
        kaho.start_recording()
        self.assertEqual(kaho.state, "ready")
        self.assertTrue(any("not responding" in m for m in self.logged))

    def test_a_wedged_audio_device_says_so_on_screen(self):
        # It used to only reach the log, so the symptom was dictation
        # silently doing nothing at all
        kaho.audio_op_started = self.clock.now - (kaho.WEDGE_SECONDS + 5)
        kaho.start_recording()
        kaho.overlay.show_wedged.assert_called_once()

    def test_an_op_that_is_merely_slow_is_not_treated_as_wedged(self):
        kaho.audio_op_started = self.clock.now - (kaho.WEDGE_SECONDS - 1)
        kaho.start_recording()
        self.assertEqual(kaho.state, "recording")
        kaho.overlay.show_wedged.assert_not_called()


class TestSettings(KahoTestCase):
    def test_a_missing_file_keeps_the_defaults(self):
        kaho.load_settings()
        self.assertEqual(
            kaho.settings,
            {"hotkey": "right_option", "rewrite": "off", "language": "auto",
             "trigger": "hold", "rewrite_backend": "local",
             "api_url": "", "api_model": ""},
        )

    def test_json_that_is_not_an_object_keeps_the_defaults(self):
        """QA finding: valid JSON of the wrong shape crashed startup.

        This function's whole job is that a bad settings file cannot brick
        the app, and a list or a bare null got past the JSON guard.
        """
        for payload in ([], None, "a string", 42):
            pathlib.Path(kaho.SETTINGS_PATH).write_text(json.dumps(payload))
            kaho.load_settings()
            self.assertEqual(kaho.settings["hotkey"], "right_option")

    def test_values_of_the_wrong_type_are_ignored(self):
        # Unhashable values raise on `in`, rather than simply not matching
        pathlib.Path(kaho.SETTINGS_PATH).write_text(
            json.dumps({"hotkey": [], "rewrite": {"a": 1}, "language": 7})
        )
        kaho.load_settings()
        self.assertEqual(kaho.settings["hotkey"], "right_option")
        self.assertEqual(kaho.settings["rewrite"], "off")
        self.assertEqual(kaho.settings["language"], "auto")

    def test_malformed_json_keeps_the_defaults(self):
        pathlib.Path(kaho.SETTINGS_PATH).write_text("{not json")
        kaho.load_settings()
        self.assertEqual(kaho.settings["rewrite"], "off")

    def test_the_bullets_rename_still_loads(self):
        # 1.6.0 renamed the mode; without the migration it reverts to Off
        pathlib.Path(kaho.SETTINGS_PATH).write_text(json.dumps({"rewrite": "bullets"}))
        kaho.load_settings()
        self.assertEqual(kaho.settings["rewrite"], "structured")

    def test_unknown_values_are_ignored(self):
        pathlib.Path(kaho.SETTINGS_PATH).write_text(
            json.dumps({"hotkey": "left_option", "rewrite": "shakespeare"})
        )
        kaho.load_settings()
        self.assertEqual(
            kaho.settings,
            {"hotkey": "right_option", "rewrite": "off", "language": "auto",
             "trigger": "hold", "rewrite_backend": "local",
             "api_url": "", "api_model": ""},
        )

    def test_saved_settings_round_trip_and_stay_private(self):
        kaho.settings["rewrite"] = "caveman"
        kaho.save_settings()
        self.assertEqual(os.stat(kaho.SETTINGS_PATH).st_mode & 0o777, 0o600)
        kaho.settings["rewrite"] = "off"
        kaho.load_settings()
        self.assertEqual(kaho.settings["rewrite"], "caveman")


class TestDictionary(KahoTestCase):
    def test_a_missing_file_yields_no_terms(self):
        self.assertEqual(kaho.read_dictionary(), [])

    def test_comments_and_blank_lines_are_skipped(self):
        pathlib.Path(kaho.DICTIONARY_PATH).write_text(
            "# a comment\n\n  Duckterm  \nKubernetes\n"
        )
        self.assertEqual(kaho.read_dictionary(), ["Duckterm", "Kubernetes"])

    def test_the_term_cap_bounds_the_prompt(self):
        terms = [f"term{i}" for i in range(kaho.DICTIONARY_MAX_TERMS + 5)]
        pathlib.Path(kaho.DICTIONARY_PATH).write_text("\n".join(terms))
        read = kaho.read_dictionary()
        self.assertEqual(len(read), kaho.DICTIONARY_MAX_TERMS)
        self.assertTrue(any("using the first" in m for m in self.logged))

    def test_the_template_is_created_once_and_never_overwritten(self):
        kaho.ensure_dictionary_file()
        path = pathlib.Path(kaho.DICTIONARY_PATH)
        self.assertIn("one term per line", path.read_text())
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        path.write_text("Mine\n")
        kaho.ensure_dictionary_file()
        self.assertEqual(path.read_text(), "Mine\n")


class TestRespell(KahoTestCase):
    """Near-misses from the benchmark, and the real words that must survive."""

    TERMS = ("Sotto", "Siobhan", "PostgreSQL", "Postman", "Swift")

    def setUp(self):
        super().setUp()
        # CI's Linux runner has no /usr/share/dict/words; use a known list
        # shaped like the macOS one — base forms, no "postmen" or "motions"
        words = pathlib.Path(self.tmp.name) / "words"
        words.write_text("postman\nswift\nmotto\nmotion\nreview\nthe\n")
        self.enterContext(mock.patch.object(kaho, "ENGLISH_WORDS_PATH", str(words)))
        self.enterContext(mock.patch.object(kaho, "_english_words", None))

    def respell(self, text):
        return kaho.respell(text, self.TERMS)

    def test_a_near_miss_takes_the_dictionary_spelling(self):
        self.assertEqual(
            self.respell("Soto runs on the GPU through MLX."), "Sotto runs on the GPU through MLX."
        )

    def test_a_possessive_keeps_its_suffix(self):
        self.assertEqual(self.respell("Soto's pill is back."), "Sotto's pill is back.")

    def test_an_exact_match_takes_the_dictionary_casing(self):
        self.assertEqual(self.respell("the PostgresQL pool"), "the PostgreSQL pool")

    def test_two_words_that_join_into_a_term_are_merged(self):
        self.assertEqual(self.respell("move it into Postgre SQL now"), "move it into PostgreSQL now")

    def test_a_mishearing_too_far_from_the_term_is_left_alone(self):
        # "Savan" for "Siobhan" was a real benchmark miss — too different to fix safely
        self.assertEqual(self.respell("Can you ask Savan?"), "Can you ask Savan?")

    def test_a_lowercase_word_is_not_claimed_by_a_capitalized_term(self):
        # "postmen" scores 0.86 against "Postman" and is missing from the word
        # list; that the model did not capitalize it is what keeps it
        self.assertEqual(self.respell("the postmen arrived"), "the postmen arrived")

    def test_the_word_list_covers_regular_inflections(self):
        self.assertTrue(kaho.is_english("motions"))
        self.assertTrue(kaho.is_english("Reviewed"))
        self.assertFalse(kaho.is_english("Soto"))

    def test_a_real_word_that_matches_a_term_keeps_its_case(self):
        self.assertEqual(self.respell("a swift reply"), "a swift reply")

    def test_no_dictionary_means_no_changes(self):
        self.assertEqual(kaho.respell("Soto runs", []), "Soto runs")


class TestHistory(KahoTestCase):
    def test_a_missing_file_yields_no_entries(self):
        self.assertEqual(kaho.read_history_file(), [])

    def test_a_damaged_line_never_takes_the_app_down(self):
        pathlib.Path(kaho.HISTORY_PATH).write_text(
            json.dumps({"t": 1.0, "text": "first"}) + "\n"
            + "{truncated\n"
            + json.dumps({"t": 2.0, "text": "second"}) + "\n"
        )
        self.assertEqual(kaho.read_history_file(), [(1.0, "first"), (2.0, "second")])
        self.assertTrue(any("malformed" in m for m in self.logged))

    def test_appending_persists_the_entry_privately_and_newest_first(self):
        kaho.append_history("older")
        kaho.append_history("newer")
        self.assertEqual([text for _, text in kaho.history], ["newer", "older"])
        self.assertEqual(kaho.history_version, 2)
        self.assertEqual(os.stat(kaho.HISTORY_PATH).st_mode & 0o777, 0o600)
        self.assertEqual([text for _, text in kaho.read_history_file()], ["older", "newer"])

    def test_startup_loads_the_last_entries_newest_first(self):
        lines = [
            json.dumps({"t": float(i), "text": f"entry {i}"}) + "\n"
            for i in range(kaho.HISTORY_SIZE + 3)
        ]
        pathlib.Path(kaho.HISTORY_PATH).write_text("".join(lines))
        kaho.load_history()
        texts = [text for _, text in kaho.history]
        self.assertEqual(len(texts), kaho.HISTORY_SIZE)
        self.assertEqual(texts[0], f"entry {kaho.HISTORY_SIZE + 2}")
        self.assertEqual(texts[-1], "entry 3")


@unittest.skipUnless(HAVE_NUMPY, "the audio drop rules need real numpy arrays")
class TestCleanSkip(KahoTestCase):
    """Clean up skips generation when the model is sure it would change nothing."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(kaho, "rewriter", (mock.MagicMock(), mock.MagicMock())))
        self.mlx_lm = self.enterContext(mock.patch.object(kaho, "mlx_lm", mock.MagicMock()))
        self.mlx_lm.generate.return_value = "Rewritten."

    def check_returns(self, value):
        return self.enterContext(mock.patch.object(kaho, "clean_as_is_probability", return_value=value))

    def test_a_confident_check_pastes_the_transcript_untouched(self):
        self.check_returns(0.99)
        self.assertIsNone(kaho.rewrite("Thanks, that works for me.", "clean"))
        self.mlx_lm.generate.assert_not_called()

    def test_an_unsure_check_still_rewrites(self):
        self.check_returns(kaho.SKIP_REWRITE_CONFIDENCE - 0.01)
        self.assertEqual(kaho.rewrite("um so yeah", "clean"), "Rewritten.")

    def test_a_failed_check_rewrites_instead_of_skipping(self):
        self.enterContext(
            mock.patch.object(kaho, "clean_as_is_probability", side_effect=RuntimeError("metal"))
        )
        self.assertEqual(kaho.rewrite("um so yeah", "clean"), "Rewritten.")

    def test_other_modes_never_run_the_check(self):
        check = self.check_returns(1.0)
        for mode in ("structured", "caveman"):
            self.assertEqual(kaho.rewrite("Thanks, that works for me.", mode), "Rewritten.")
        check.assert_not_called()


class TestAudioDropRules(KahoTestCase):
    """What reaches Whisper, and what is dropped before it can hallucinate."""

    def frames(self, seconds, amplitude):
        np = REAL_NUMPY
        n = int(kaho.SAMPLE_RATE * seconds)
        # A tone rather than a constant: peak is what the rules look at, but a
        # constant would also make rms meaningless if these ever grow
        wave = amplitude * np.sin(np.linspace(0, 40 * np.pi, n, dtype="float32"))
        return [wave.reshape(-1, 1)]

    def test_no_frames_at_all(self):
        audio, message, pill = kaho._audio_or_drop_reason([])
        self.assertIsNone(audio)
        self.assertEqual(message, "dropped: no audio captured")
        self.assertIn("No audio", pill)

    def test_a_hold_too_short_to_be_speech(self):
        audio, message, pill = kaho._audio_or_drop_reason(self.frames(0.1, 0.5))
        self.assertIsNone(audio)
        self.assertIn(f"under the {kaho.MIN_SECONDS}s minimum", message)
        self.assertIsNone(pill, "a fumbled key is not worth reporting")

    def test_pure_silence_names_the_permission(self):
        audio, message, pill = kaho._audio_or_drop_reason(self.frames(1.0, 0.0))
        self.assertIsNone(audio)
        self.assertIn("macOS delivered no mic signal", message)
        self.assertIn("Microphone", message)
        self.assertIn("mic signal", pill)

    def test_audio_under_the_speech_floor(self):
        audio, message, pill = kaho._audio_or_drop_reason(self.frames(1.0, kaho.MIN_PEAK / 2))
        self.assertIsNone(audio)
        self.assertIn("too quiet to be speech", message)
        self.assertIn("Too quiet", pill)

    def test_a_real_dictation_gets_through(self):
        audio, message, pill = kaho._audio_or_drop_reason(self.frames(2.0, 0.2))
        self.assertIsNotNone(audio)
        self.assertIsNone(pill, "a good recording has nothing to report")
        self.assertEqual(len(audio), 2 * kaho.SAMPLE_RATE)
        self.assertIn("recorded 2.0s", message)
        self.assertIn("transcribing", message)

    def test_escape_still_cancels_a_job_the_pill_is_no_longer_showing(self):
        """A timeout must not silently disarm cancellation.

        Cancellability used to be read off panel visibility, so the watchdog
        hiding the pill made Escape stop reaching a job that was still
        running: no indication, and no way out.
        """
        # The real method, with a panel that reports itself hidden — exactly
        # the state the watchdog leaves behind
        panel = mock.MagicMock()
        panel.isVisible.return_value = False
        overlay = mock.MagicMock(panel=panel)

        with mock.patch.object(kaho, "job_outstanding", True):
            self.assertTrue(kaho.Overlay.is_working(overlay),
                            "a hidden pill made a running job uncancellable")
        with mock.patch.object(kaho, "job_outstanding", False):
            self.assertFalse(kaho.Overlay.is_working(overlay))

    def test_an_old_completion_leaves_a_live_recording_alone(self):
        """QA finding: a slow job finishing could hide a newer pill.

        Completions are posted from the worker, so one can land after the
        user has started dictating again — taking down the level meter and
        leaving them with no sign anything is being captured.
        """
        overlay = mock.MagicMock()
        with mock.patch.object(kaho, "state", "recording"):
            kaho.Overlay.finishStale_(overlay)
            kaho.Overlay.hideStale_(overlay)
        overlay.finish.assert_not_called()
        overlay.hide.assert_not_called()

        with mock.patch.object(kaho, "state", "ready"):
            kaho.Overlay.finishStale_(overlay)
            kaho.Overlay.hideStale_(overlay)
        overlay.finish.assert_called_once()
        overlay.hide.assert_called_once()

    def test_an_old_deferred_stop_cannot_end_a_newer_recording(self):
        """QA finding: a timer left over from an abandoned tap fired later.

        The guard compared last_tap, which a cancel resets to 0.0 — so the
        stale timer could match again and stop a recording the user had
        only just started.
        """
        self.down()
        self.run_audio_ops()
        self.clock.advance(0.1)
        self.up()                        # a tap: schedules a deferred stop
        stale = self.deferred_stop()

        kaho.overlay.is_working.return_value = False
        kaho.cancel_pending_job()        # abandon it; last_tap resets
        self.run_audio_ops()

        self.clock.advance(kaho.DOUBLE_TAP_SECONDS + 1.0)
        self.down()                      # a brand new recording
        self.run_audio_ops()
        self.assertEqual(kaho.state, "recording")

        stale(None)                      # the old timer finally fires
        self.assertEqual(kaho.state, "recording",
                         "a stale timer stopped a newer recording")

    def test_releasing_the_key_stamps_the_recording(self):
        """Without this stamp the timing silently falls back to the old,
        misleading measurement rather than failing visibly."""
        self.down()
        self.run_audio_ops()
        session = kaho.recording
        self.clock.advance(2.0)
        self.up()
        self.assertEqual(session.released_at, self.clock.now,
                         "the release moment was never recorded")

    def test_the_reported_time_includes_the_wait_before_the_worker(self):
        """QA finding 2: a four-second stall used to log as 0.00s.

        The timer began after jobs.get(), so microphone shutdown and queue
        waiting — the parts the user actually feels — were not measured.
        """
        released = self.clock.now
        self.clock.advance(4.0)          # a slow device release
        picked_up = self.clock.now
        self.clock.advance(0.5)          # then the real work

        self.assertEqual(kaho.job_timing(released, picked_up),
                         "4.50s, 4.00s of it waiting")

    def test_a_timing_with_no_release_stamp_still_reports(self):
        # Warmup jobs and direct calls carry no session
        self.clock.advance(1.0)
        self.assertEqual(kaho.job_timing(None, self.clock.now - 0.75), "0.75s")

    def test_a_negligible_queue_wait_is_not_mentioned(self):
        released = self.clock.now
        self.clock.advance(0.01)
        picked_up = self.clock.now
        self.clock.advance(0.3)
        self.assertEqual(kaho.job_timing(released, picked_up), "0.31s")

    def test_a_hung_device_stop_still_delivers_the_transcript(self):
        """The worst failure in this app is losing words already spoken.

        CoreAudio's stop can block forever on a HAL mutex. Shutting the
        device down before enqueueing meant that hang discarded a finished
        recording, so the order is now enqueue first, release second.
        """
        stream = mock.MagicMock()
        stopped = threading.Event()

        def hang():
            stopped.set()
            raise AssertionError("the test must not actually block here")

        stream.stop.side_effect = hang
        # The job has to be on the queue BEFORE stop() is ever reached
        with self.assertRaises(AssertionError):
            kaho._finish_recording(stream, self.frames(2.0, 0.2))
        self.assertTrue(stopped.is_set(), "stop() was never attempted")
        self.assertFalse(kaho.jobs.empty(),
                         "the transcript was lost to a hung device stop")

    def test_a_drop_the_user_can_fix_is_shown_on_the_pill(self):
        """Silence and quiet audio are actionable, so they must be visible.

        These used to reach the log only, so a mic turned down looked like
        the app being dead rather than a setting being wrong.
        """
        for buf, expect in ((self.frames(1.0, 0.0), "mic signal"),
                            (self.frames(1.0, kaho.MIN_PEAK / 2), "Too quiet"),
                            ([], "No audio")):
            kaho.AppHelper.callAfter.reset_mock()
            kaho._finish_recording(None, buf)
            call = kaho.AppHelper.callAfter.call_args
            self.assertEqual(call.args[0], kaho.overlay.showProblem_)
            self.assertIn(expect, call.args[1])
            self.assertTrue(kaho.jobs.empty(), "dropped audio must not reach the worker")

    def test_a_stray_keypress_is_dropped_silently(self):
        """Too short to be speech is a fumbled key, not a problem to report."""
        kaho.AppHelper.callAfter.reset_mock()
        kaho._finish_recording(None, self.frames(0.1, 0.5))
        kaho.AppHelper.callAfter.assert_called_once_with(kaho.overlay.hide)

    def test_finish_recording_queues_usable_audio(self):
        kaho._finish_recording(None, self.frames(2.0, 0.2))
        self.assertFalse(kaho.jobs.empty())
        kaho.AppHelper.callAfter.assert_not_called()


class TestMenuWiring(unittest.TestCase):
    """The status menu and the app menu are built from one table."""

    def test_every_menu_action_has_a_handler(self):
        # A typo in a selector is silent at runtime: the item just does nothing
        for title, action, _ in kaho.MENU_ACTIONS:
            self.assertTrue(
                hasattr(kaho.StatusItem, action.replace(":", "_")),
                f"{title!r} points at {action!r}, which StatusItem does not implement",
            )

    def test_the_app_menu_drops_open_log_and_quits_through_nsapp(self):
        kaho.AppKit.NSMenuItem.reset_mock()
        with mock.patch.object(kaho, "status_item", mock.MagicMock()):
            kaho.install_app_menu()
        built = [
            call[0]
            for call in kaho.AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_.call_args_list
        ]
        self.assertNotIn("openLog:", [action for _, action, _ in built])
        self.assertIn(("Quit Kaho", "terminate:", "q"), built)
        self.assertIn(("Settings…", "showSettings:", ","), built)
        # One above Quit, one inside the Edit menu
        self.assertEqual(kaho.AppKit.NSMenuItem.separatorItem.call_count, 2)

    def test_the_edit_menu_carries_the_standard_shortcuts(self):
        """⌘C and friends are routed by the menu bar, not by the focused view.

        Without this menu they did nothing anywhere in the app: copying a
        transcript out of History needed a right-click.
        """
        kaho.AppKit.NSMenuItem.reset_mock()
        with mock.patch.object(kaho, "status_item", mock.MagicMock()):
            kaho.install_app_menu()
        built = [
            call[0]
            for call in kaho.AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_.call_args_list
        ]
        for entry in (
            ("Copy", "copy:", "c"),
            ("Paste", "paste:", "v"),
            ("Cut", "cut:", "x"),
            ("Select All", "selectAll:", "a"),
        ):
            self.assertIn(entry, built)
        # No setTarget_ for these: a nil target is what sends them down the
        # responder chain to the view that has focus
        self.assertTrue(
            any("Edit" in str(c) for c in kaho.AppKit.NSMenu.alloc().initWithTitle_.call_args_list)
        )


class TestSettingsWiring(KahoTestCase):
    """A change on either surface has to reach the other."""

    def setUp(self):
        super().setUp()
        self.status = mock.MagicMock()
        self.window = mock.MagicMock()
        self.enterContext(mock.patch.object(kaho, "status_item", self.status))
        self.enterContext(mock.patch.object(kaho, "settings_win", self.window))

    def test_applying_a_hotkey_saves_it_and_refreshes_both_surfaces(self):
        kaho.apply_hotkey("right_shift")
        self.assertEqual(kaho.settings["hotkey"], "right_shift")
        self.assertIn('"hotkey": "right_shift"', pathlib.Path(kaho.SETTINGS_PATH).read_text())
        self.status.rebuildMenu.assert_called_once()
        self.window.syncControls.assert_called_once()
        self.assertTrue(any("Right Shift" in m for m in self.logged))

    def test_applying_a_rewrite_mode_loads_the_model_only_when_on(self):
        with mock.patch.object(kaho, "ensure_rewriter") as ensure:
            kaho.apply_rewrite("caveman")
            ensure.assert_called_once()
            ensure.reset_mock()
            kaho.apply_rewrite("off")
            ensure.assert_not_called()
        self.assertEqual(kaho.settings["rewrite"], "off")

    def test_opening_the_dictionary_creates_the_template_first(self):
        with mock.patch.object(kaho, "subprocess") as sp:
            kaho.open_dictionary()
        self.assertTrue(pathlib.Path(kaho.DICTIONARY_PATH).exists())
        sp.run.assert_called_once_with(["open", "-t", kaho.DICTIONARY_PATH], check=False)


class TestRewriterLoading(KahoTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(kaho, "rewriter_thread", None))

    def test_repeated_calls_start_one_loader(self):
        with mock.patch.object(kaho, "threading") as threading_stub:
            started = threading_stub.Thread.return_value
            started.is_alive.return_value = True
            kaho.ensure_rewriter()
            kaho.ensure_rewriter()
            kaho.ensure_rewriter()
        threading_stub.Thread.assert_called_once_with(target=kaho._load_rewriter, daemon=True)
        started.start.assert_called_once()

    def test_a_second_caller_arriving_mid_start_is_held_at_the_lock(self):
        """The race itself, staged rather than argued about.

        ensure_rewriter is called from the backend thread at startup and from
        the menu and settings window on the main thread. Without the lock, a
        caller landing between the "already loading?" check and the thread
        start passes the check too, and downloads and loads its own copy of a
        2.3 GB model.

        The thread factory is where we interrupt: the first call is inside the
        critical section when the second arrives.
        """
        real_thread = threading.Thread
        created, was_blocked = [], []
        contender = []

        def factory(target=None, daemon=None):
            handle = mock.MagicMock()
            handle.is_alive.return_value = True
            created.append(handle)
            if len(created) == 1:
                other = real_thread(target=kaho.ensure_rewriter, daemon=True)
                other.start()
                contender.append(other)
                other.join(timeout=0.3)
                # Still alive = still waiting on the lock, which is the point
                was_blocked.append(other.is_alive())
            return handle

        with mock.patch("kaho.threading.Thread", factory):
            kaho.ensure_rewriter()
            # Joined inside the patch, so when the contender finally takes the
            # lock it gets the mock factory too. Letting it escape to the real
            # threading.Thread starts a live _load_rewriter that imports
            # mlx_lm against the stubbed huggingface_hub, which prints a
            # traceback into the test output and races the next test.
            for t in contender:
                t.join(timeout=2.0)
                self.assertFalse(t.is_alive(), "the second caller never finished")

        self.assertEqual(was_blocked, [True], "the second caller was not held at the lock")
        # Still one: once it has the lock the contender sees a live loader
        # and declines, which is the behaviour being proved
        self.assertEqual(len(created), 1, "two loaders were started for one model")

    def test_nothing_starts_once_the_model_is_loaded(self):
        self.enterContext(mock.patch.object(kaho, "rewriter", ("model", "tokenizer")))
        self.enterContext(mock.patch.object(kaho, "rewriter_thread", None))
        with mock.patch.object(kaho, "threading") as threading_stub:
            kaho.ensure_rewriter()
        threading_stub.Thread.assert_not_called()


class TestHallucinationFilter(unittest.TestCase):
    """Whisper's repetition loops must not reach the user's editor."""

    def test_short_transcripts_are_always_kept(self):
        self.assertFalse(kaho.looks_hallucinated("fix the header and update the changelog"))

    def test_a_repetition_loop_is_caught(self):
        self.assertTrue(kaho.looks_hallucinated("videos " * 400))

    def test_punctuation_does_not_disguise_a_loop(self):
        self.assertTrue(kaho.looks_hallucinated("Videos, videos. videos! " * 100))

    def test_a_genuine_long_dictation_survives(self):
        text = (
            "the release is blocked on three things, the header is broken on "
            "mobile, the changelog still says version one point six, and we "
            "never emailed the beta testers about any of it"
        )
        self.assertGreaterEqual(len(text.split()), 20)
        self.assertFalse(kaho.looks_hallucinated(text))


class TestPasteClipboardHandling(KahoTestCase):
    """The clipboard is the user's, borrowed for one keystroke."""

    def setUp(self):
        super().setUp()
        self.pb = kaho.AppKit.NSPasteboard.generalPasteboard.return_value
        self.pb.reset_mock()
        self.pb.stringForType_.return_value = "what the user had copied"
        self.pb.changeCount.return_value = 7

    def scheduled_restore(self):
        """The restore block, dug out of callAfter -> NSTimer."""
        (scheduler,), _ = kaho.AppHelper.callAfter.call_args
        scheduler()
        args = kaho.AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_.call_args[0]
        interval, repeats, block = args
        self.assertEqual(interval, kaho.CLIPBOARD_RESTORE_SECONDS)
        self.assertFalse(repeats)
        return block

    def test_the_transcript_is_marked_transient_for_clipboard_managers(self):
        # Maccy, Paste and Raycast archive everything on the pasteboard; a
        # dictated transcript is transient and often private
        kaho.paste("a private transcript")
        declared = self.pb.declareTypes_owner_.call_args[0][0]
        for transient in kaho.TRANSIENT_TYPES:
            self.assertIn(transient, declared)
        self.pb.setString_forType_.assert_called_with(
            "a private transcript", kaho.AppKit.NSPasteboardTypeString
        )

    def test_the_previous_clipboard_comes_back_when_untouched(self):
        kaho.paste("a transcript")
        self.scheduled_restore()(None)
        self.pb.setString_forType_.assert_called_with(
            "what the user had copied", kaho.AppKit.NSPasteboardTypeString
        )

    def test_a_clipboard_the_user_changed_is_never_clobbered(self):
        kaho.paste("a transcript")
        restore = self.scheduled_restore()
        self.pb.changeCount.return_value = 9  # the user copied something else
        restore(None)
        self.pb.setString_forType_.assert_called_with(
            "a transcript", kaho.AppKit.NSPasteboardTypeString
        )

    def test_an_empty_clipboard_schedules_no_restore(self):
        self.pb.stringForType_.return_value = None
        kaho.paste("a transcript")
        kaho.AppHelper.callAfter.assert_not_called()


class TestConstantsAgree(unittest.TestCase):
    """Cheap guards for the tables that drift apart when a mode is renamed."""

    def test_every_rewrite_mode_has_a_hint(self):
        self.assertEqual(set(kaho.REWRITE_MODES), set(kaho.REWRITE_HINTS))

    def test_every_rewriting_mode_has_a_prompt_expecting_the_transcript(self):
        self.assertEqual(set(kaho.REWRITE_PROMPTS), set(kaho.REWRITE_MODES) - {"off"})
        for mode, prompt in kaho.REWRITE_PROMPTS.items():
            self.assertIn("{text}", prompt, mode)

    def test_every_state_has_a_menu_bar_glyph(self):
        self.assertEqual(set(kaho.TITLES), {"loading", "ready", "recording", "error"})

    def test_every_post_release_phase_has_a_label(self):
        self.assertEqual(
            set(kaho.PHASE_LABELS),
            {"transcribing", "rewriting", "done", "blocked", "wedged",
             "instructing"},
        )

    def test_hotkeys_are_distinct_and_labelled(self):
        keycodes = [k for k, _, _ in kaho.HOTKEYS.values()]
        masks = [m for _, m, _ in kaho.HOTKEYS.values()]
        labels = [label for _, _, label in kaho.HOTKEYS.values()]
        self.assertEqual(len(set(keycodes)), len(keycodes))
        self.assertEqual(len(set(masks)), len(masks))
        self.assertEqual(len(set(labels)), len(labels))


class TestLanguage(KahoTestCase):
    """Detection can guess wrong on short audio; pinning the language is the cure."""

    def transcribe_kwargs(self):
        asr = self.enterContext(mock.patch.object(kaho, "asr", mock.MagicMock()))
        asr.generate.return_value.text = ""
        kaho.transcribe(mock.MagicMock(), use_dictionary=False)
        return asr.generate.call_args.kwargs

    def test_auto_lets_the_model_detect(self):
        kaho.settings["language"] = "auto"
        self.assertIsNone(self.transcribe_kwargs()["language"])

    def test_a_chosen_language_is_passed_by_name(self):
        # Qwen3-ASR matches language names, not codes: "en" would be ignored
        kaho.settings["language"] = "en"
        self.assertEqual(self.transcribe_kwargs()["language"], "English")

    def test_it_is_saved_and_reloaded(self):
        kaho.apply_language("hi")
        kaho.settings["language"] = "auto"
        kaho.load_settings()
        self.assertEqual(kaho.settings["language"], "hi")

    def test_an_unknown_language_falls_back_to_auto(self):
        # A settings file from a newer version must not break this one
        pathlib.Path(kaho.SETTINGS_PATH).write_text(json.dumps({"language": "klingon"}))
        kaho.load_settings()
        self.assertEqual(kaho.settings["language"], "auto")


@unittest.skipUnless(HAVE_NUMPY, "cancel tests need real audio buffers")
class TestToggleTrigger(KahoTestCase):
    """Tap to start, tap to stop — for anyone who cannot hold a key down."""

    def setUp(self):
        super().setUp()
        kaho.settings["trigger"] = "toggle"

    def test_one_tap_starts_and_the_recording_outlives_the_key(self):
        self.down()
        self.up()
        self.assertEqual(kaho.state, "recording")
        self.assertTrue(kaho.locked, "the recording must survive the key release")

    def test_the_next_tap_stops_it(self):
        self.down()
        self.up()
        self.clock.advance(5.0)
        self.down()
        self.assertEqual(kaho.state, "ready")
        self.assertFalse(kaho.locked)

    def test_a_long_hold_is_still_one_tap_not_a_stop(self):
        """Key-up is ignored, so holding the key does not end the recording."""
        self.down()
        self.clock.advance(3.0)
        self.up()
        self.assertEqual(kaho.state, "recording")

    def test_a_wedged_device_does_not_latch_the_lock(self):
        # start_recording declines; a stale lock would make the next tap try
        # to stop a recording that never began
        kaho.audio_op_started = self.clock.now - (kaho.WEDGE_SECONDS + 5)
        self.down()
        self.assertEqual(kaho.state, "ready")
        self.assertFalse(kaho.locked)

    def test_hold_mode_is_unaffected(self):
        kaho.settings["trigger"] = "hold"
        self.down()
        self.clock.advance(2.0)
        self.up()
        self.assertEqual(kaho.state, "ready")
        self.assertFalse(kaho.locked)

    def test_switching_mode_mid_recording_ends_it(self):
        self.down()
        self.up()
        self.assertEqual(kaho.state, "recording")
        kaho.apply_trigger("hold")
        self.assertEqual(kaho.state, "ready")
        self.assertFalse(kaho.locked)


@unittest.skipUnless(HAVE_NUMPY, "the split needs real audio buffers")
class TestSpokenInstruction(KahoTestCase):
    """Press a second modifier mid-dictation to say how it should be written."""

    def setUp(self):
        super().setUp()
        kaho.overlay.is_working.return_value = False

    def hold_instruction(self):
        code, mask = kaho.instruction_key()
        kaho.handle_flags_changed(FakeEvent(code, mask))

    def release_instruction(self):
        code, _ = kaho.instruction_key()
        kaho.handle_flags_changed(FakeEvent(code, 0))

    def speak(self, seconds=1.0):
        loud = REAL_NUMPY.full((int(kaho.SAMPLE_RATE * seconds), 1), 0.5, dtype="float32")
        kaho.record_buf.append(loud)

    def test_holding_the_key_opens_a_span_and_releasing_closes_it(self):
        self.down()
        self.run_audio_ops()
        self.speak(1.0)
        self.hold_instruction()
        self.speak(1.0)
        self.release_instruction()
        self.assertEqual(kaho.recording.spans,
                         [[kaho.SAMPLE_RATE, 2 * kaho.SAMPLE_RATE]])

    def test_it_can_be_toggled_more_than_once(self):
        """Releasing goes back to dictating — the whole point of holding."""
        self.down()
        self.run_audio_ops()
        self.speak(1.0)
        self.hold_instruction()
        self.speak(1.0)
        self.release_instruction()
        self.speak(1.0)
        self.hold_instruction()
        self.speak(1.0)
        self.release_instruction()
        self.assertEqual(
            kaho.recording.spans,
            [[kaho.SAMPLE_RATE, 2 * kaho.SAMPLE_RATE],
             [3 * kaho.SAMPLE_RATE, 4 * kaho.SAMPLE_RATE]],
        )

    def test_the_pill_goes_back_to_recording_on_release(self):
        self.down()
        self.run_audio_ops()
        self.hold_instruction()
        kaho.overlay.setPhase_.assert_called_with("instructing")
        self.release_instruction()
        kaho.overlay.setPhase_.assert_called_with("recording")

    def test_the_key_does_nothing_when_not_recording(self):
        self.hold_instruction()
        self.assertEqual((kaho.recording.spans if kaho.recording else []), [])

    def test_spans_are_cleared_for_the_next_dictation(self):
        self.down()
        self.run_audio_ops()
        self.speak(1.0)
        self.hold_instruction()
        self.speak(1.0)
        self.clock.advance(2.0)
        self.up()
        self.run_audio_ops()

        self.clock.advance(kaho.DOUBLE_TAP_SECONDS + 1.0)
        self.down()
        self.assertEqual((kaho.recording.spans if kaho.recording else []), [],
                         "a stale span would split the next dictation")

    def test_a_span_left_open_is_closed_at_the_end(self):
        """The hotkey can be released while the instruction key is still down."""
        self.down()
        self.run_audio_ops()
        self.speak(1.0)
        self.hold_instruction()
        self.speak(1.0)
        self.clock.advance(2.0)
        self.up()
        self.run_audio_ops()

        _, _, spans, _ = kaho.jobs.get()
        self.assertEqual(spans, [[kaho.SAMPLE_RATE, 2 * kaho.SAMPLE_RATE]])

    def test_the_halves_are_gathered_from_every_piece(self):
        audio = REAL_NUMPY.arange(100, dtype="float32")
        message, instruction = kaho.split_audio(audio, [[20, 30], [60, 70]])
        self.assertEqual(len(message), 80)
        self.assertEqual(len(instruction), 20)
        # Order preserved: message is 0-20, 30-60, 70-100
        self.assertEqual(message[0], 0)
        self.assertEqual(message[20], 30)
        self.assertEqual(instruction[0], 20)
        self.assertEqual(instruction[10], 60)

    def test_the_instruction_rewrites_the_message(self):
        # Instruction half is transcribed first, then the message
        with mock.patch.object(kaho, "transcribe",
                               side_effect=["make it formal", "cant make the offsite"]), \
             mock.patch.object(kaho, "rewrite_with_instruction",
                               return_value="I am unable to attend.") as rw, \
             mock.patch.object(kaho, "paste") as paste, \
             mock.patch.object(kaho, "append_history"), \
             mock.patch.object(kaho, "paste_blocked_reason", return_value=None), \
             mock.patch.object(kaho, "rewriter", (mock.MagicMock(), mock.MagicMock())):
            audio = REAL_NUMPY.zeros(kaho.SAMPLE_RATE * 2, dtype="float32")
            kaho.run_job(audio, kaho.job_generation, [[kaho.SAMPLE_RATE, len(audio)]], None, self.clock.now)

        rw.assert_called_once_with("cant make the offsite", "make it formal")
        paste.assert_called_once_with("I am unable to attend.")

    def test_a_spoken_instruction_overrides_the_configured_mode(self):
        kaho.settings["rewrite"] = "caveman"
        with mock.patch.object(kaho, "transcribe",
                               side_effect=["make it formal", "cant make it"]), \
             mock.patch.object(kaho, "rewrite_with_instruction",
                               return_value="I cannot attend."), \
             mock.patch.object(kaho, "rewrite") as plain, \
             mock.patch.object(kaho, "paste"), \
             mock.patch.object(kaho, "append_history"), \
             mock.patch.object(kaho, "paste_blocked_reason", return_value=None), \
             mock.patch.object(kaho, "rewriter", (mock.MagicMock(), mock.MagicMock())):
            audio = REAL_NUMPY.zeros(kaho.SAMPLE_RATE * 2, dtype="float32")
            kaho.run_job(audio, kaho.job_generation, [[kaho.SAMPLE_RATE, len(audio)]], None, self.clock.now)

        plain.assert_not_called()

    def test_an_instruction_too_short_to_be_speech_is_ignored(self):
        with mock.patch.object(kaho, "transcribe", return_value="the whole thing"), \
             mock.patch.object(kaho, "rewrite_with_instruction") as rw, \
             mock.patch.object(kaho, "paste") as paste, \
             mock.patch.object(kaho, "append_history"), \
             mock.patch.object(kaho, "paste_blocked_reason", return_value=None):
            audio = REAL_NUMPY.zeros(kaho.SAMPLE_RATE, dtype="float32")
            # Split 0.1 s from the end: below MIN_SECONDS
            kaho.run_job(audio, kaho.job_generation,
                         [[int(kaho.SAMPLE_RATE * 0.9), len(audio)]], None, self.clock.now)

        rw.assert_not_called()
        paste.assert_called_once_with("the whole thing")

    def test_a_broken_chat_template_keeps_the_transcript(self):
        """Speech already recognised must survive a rewrite that cannot run."""
        tokenizer = mock.MagicMock()
        tokenizer.apply_chat_template.side_effect = ValueError("bad template")
        with mock.patch.object(kaho, "rewriter", (mock.MagicMock(), tokenizer)):
            self.assertIsNone(kaho.rewrite("the words I said", "structured"))
            self.assertIsNone(
                kaho.rewrite_with_instruction("the words I said", "make it formal"))

    def test_a_too_short_instruction_is_still_kept_out_of_the_message(self):
        """Marked as instruction means never transcribed as message.

        The branch that gives up on a brief instruction used to leave the
        full recording in place, so Kaho spoke the instruction back at the
        user inside their own text.
        """
        audio = REAL_NUMPY.arange(kaho.SAMPLE_RATE, dtype="float32")
        with mock.patch.object(kaho, "transcribe", return_value="msg") as tr, \
             mock.patch.object(kaho, "paste"), \
             mock.patch.object(kaho, "append_history"), \
             mock.patch.object(kaho, "paste_blocked_reason", return_value=None):
            # Final 0.1 s marked as instruction: under MIN_SECONDS
            kaho.run_job(audio, kaho.job_generation,
                         [[int(kaho.SAMPLE_RATE * 0.9), kaho.SAMPLE_RATE]], None,
                         self.clock.now)

        sent = tr.call_args.args[0]
        self.assertEqual(len(sent), int(kaho.SAMPLE_RATE * 0.9),
                         "the instruction audio was transcribed into the message")

    def test_cancelling_a_second_dictation_leaves_the_first_alone(self):
        """QA finding 3b: cancellation belonged to whichever recording was last.

        Stop A, start B, cancel B before A's finalizer runs. A used to
        consume the global cancel flag and be discarded, while B pasted.
        """
        self.down()
        self.run_audio_ops()
        self.speak(1.0)
        session_a, buf_a = kaho.recording, kaho.record_buf
        self.clock.advance(2.0)
        self.up()                      # A stops; its finalizer is queued

        self.clock.advance(kaho.DOUBLE_TAP_SECONDS + 1.0)
        self.down()                    # B starts
        self.run_audio_ops()
        self.speak(1.0)
        kaho.overlay.is_working.return_value = False
        kaho.cancel_pending_job()      # cancel B

        self.assertFalse(session_a.cancelled, "cancelling B marked A cancelled")
        self.assertTrue(kaho.recording.cancelled, "B was not cancelled")

        before = kaho.jobs.qsize()
        kaho._finish_recording(None, buf_a, session_a)
        self.assertEqual(kaho.jobs.qsize(), before + 1,
                         "A was discarded by a cancel that belonged to B")

    def test_a_new_dictation_does_not_erase_the_previous_instructions(self):
        """QA finding 3c: B used to clear A's instruction ranges."""
        self.down()
        self.run_audio_ops()
        self.speak(1.0)
        self.hold_instruction()
        self.speak(1.0)
        self.release_instruction()
        session_a = kaho.recording
        spans_a = list(session_a.spans)
        self.clock.advance(2.0)
        self.up()

        self.clock.advance(kaho.DOUBLE_TAP_SECONDS + 1.0)
        self.down()                    # B starts before A finalizes
        self.assertEqual(session_a.spans, spans_a,
                         "starting B erased A's instruction ranges")
        self.assertEqual(kaho.recording.spans, [], "B inherited A's ranges")

    def test_the_instruction_key_is_never_the_hotkey(self):
        for name in kaho.HOTKEYS:
            kaho.settings["hotkey"] = name
            self.assertNotEqual(kaho.instruction_key()[0], kaho.HOTKEYS[name][0], name)


class TestBringYourOwnKey(KahoTestCase):
    """A key only ever adds quality; nothing may depend on it."""

    def setUp(self):
        super().setUp()
        kaho.settings["rewrite_backend"] = "openai"
        self.enterContext(mock.patch.object(kaho, "get_api_key", return_value="sk-test"))

    def test_the_transcript_is_sent_and_the_reply_used(self):
        reply = {"choices": [{"message": {"content": "Polished."}}]}
        with mock.patch.object(kaho.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = \
                json.dumps(reply).encode()
            out = kaho.call_rewrite_api("openai", "do the thing")
        self.assertEqual(out, "Polished.")
        request = urlopen.call_args.args[0]
        self.assertIn("do the thing", request.data.decode())
        self.assertEqual(request.headers["Authorization"], "Bearer sk-test")

    def test_anthropic_uses_its_own_shape(self):
        reply = {"content": [{"text": "Polished."}]}
        kaho.settings["rewrite_backend"] = "anthropic"
        with mock.patch.object(kaho.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = \
                json.dumps(reply).encode()
            out = kaho.call_rewrite_api("anthropic", "do the thing")
        self.assertEqual(out, "Polished.")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.headers["X-api-key"], "sk-test")

    def test_a_dead_network_falls_back_to_the_local_model(self):
        with mock.patch.object(kaho.urllib.request, "urlopen",
                               side_effect=OSError("no route to host")), \
             mock.patch.object(kaho, "rewriter", (mock.MagicMock(), mock.MagicMock())), \
             mock.patch.object(kaho, "mlx_lm") as mlx:
            mlx.generate.return_value = "Local rewrite."
            out = kaho.rewrite("um so yeah", "structured")
        self.assertEqual(out, "Local rewrite.", "a dead network must not lose the words")
        self.assertTrue(any("using the on-device model" in m for m in self.logged))

    def test_a_missing_key_falls_back_without_a_request(self):
        with mock.patch.object(kaho, "get_api_key", return_value=""), \
             mock.patch.object(kaho.urllib.request, "urlopen") as urlopen:
            out = kaho.call_rewrite_api("openai", "do the thing")
        self.assertIsNone(out)
        urlopen.assert_not_called()

    def test_an_unexpected_response_shape_falls_back(self):
        with mock.patch.object(kaho.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b'{"oops": 1}'
            self.assertIsNone(kaho.call_rewrite_api("openai", "x"))

    def test_the_key_is_never_written_to_the_log(self):
        with mock.patch.object(kaho.urllib.request, "urlopen",
                               side_effect=OSError("401 Bearer sk-test rejected")):
            kaho.call_rewrite_api("openai", "x")
        self.assertFalse(any("sk-test" in m for m in self.logged),
                         f"the key leaked into the log: {self.logged}")

    def test_a_malformed_endpoint_falls_back_instead_of_raising(self):
        """A user typing a custom URL can get this wrong; it must not crash."""
        kaho.settings.update(api_url="not-a-url", api_model="qa")
        self.assertIsNone(kaho.call_rewrite_api("custom", "text"))

    def test_a_null_content_field_falls_back(self):
        reply = b'{"choices":[{"message":{"content":null}}]}'
        with mock.patch.object(kaho.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = reply
            self.assertIsNone(kaho.call_rewrite_api("openai", "text"))

    def test_local_is_the_default_and_makes_no_request(self):
        kaho.settings["rewrite_backend"] = "local"
        with mock.patch.object(kaho.urllib.request, "urlopen") as urlopen, \
             mock.patch.object(kaho, "rewriter", (mock.MagicMock(), mock.MagicMock())), \
             mock.patch.object(kaho, "mlx_lm") as mlx:
            mlx.generate.return_value = "Local."
            kaho.rewrite("text", "structured")
        urlopen.assert_not_called()


class TestCancel(KahoTestCase):
    """Escape has to stop an unwanted dictation from landing in the document."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(kaho, "job_generation", 0))

    def escape(self):
        return kaho.handle_key_down(FakeEvent(kaho.ESCAPE_KEYCODE, 0))

    def test_escape_while_recording_stops_it_and_discards_the_audio(self):
        kaho.overlay.is_working.return_value = False
        self.down()
        self.run_audio_ops()
        self.assertEqual(kaho.state, "recording")
        self.speak()

        self.assertTrue(self.escape())

        self.assertEqual(kaho.state, "ready")
        # The audio is dropped outright rather than versioned: _finish_recording
        # runs later on the audio thread and would read the post-bump counter
        self.assertTrue(kaho.recording.cancelled)
        self.run_audio_ops()
        self.assertEqual(self.queued_audio(), [], "cancelled audio was queued anyway")

    def test_a_cancelled_job_is_not_pasted(self):
        """The worker drops a result whose generation no longer matches.

        MLX inference cannot be interrupted, so this is what cancelling
        actually buys: the text is computed and then thrown away.
        """
        kaho.jobs.put((mock.MagicMock(), 0, [], None))
        kaho.job_generation = 1
        self.enterContext(mock.patch.object(kaho, "asr", mock.MagicMock()))
        kaho.asr.generate.return_value.text = "unwanted text"

        with mock.patch.object(kaho, "paste") as paste, \
             mock.patch.object(kaho, "append_history") as history:
            self.run_one_job()

        paste.assert_not_called()
        history.assert_not_called()
        self.assertTrue(any("cancelled" in m for m in self.logged))

    def test_an_uncancelled_job_still_pastes(self):
        kaho.jobs.put((mock.MagicMock(), 0, [], None))
        self.enterContext(mock.patch.object(kaho, "asr", mock.MagicMock()))
        kaho.asr.generate.return_value.text = "wanted text"

        with mock.patch.object(kaho, "paste") as paste, \
             mock.patch.object(kaho, "append_history"), \
             mock.patch.object(kaho, "paste_blocked_reason", return_value=None):
            self.run_one_job()

        paste.assert_called_once_with("wanted text")

    def test_the_next_recording_is_not_dropped_by_a_stale_cancel(self):
        kaho.overlay.is_working.return_value = False
        self.down()
        self.escape()
        self.run_audio_ops()

        # Well clear of the double-tap window, so this is a fresh dictation
        # rather than the second half of a double-tap
        self.clock.advance(kaho.DOUBLE_TAP_SECONDS + 1.0)
        self.down()
        self.run_audio_ops()
        self.assertFalse(kaho.recording.cancelled)
        self.speak()
        self.clock.advance(2.0)
        self.up()
        self.run_audio_ops()
        self.assertEqual(
            len(self.queued_audio()), 1, "the recording after a cancel was dropped"
        )

    def test_escape_does_nothing_when_idle(self):
        kaho.overlay.is_working.return_value = False
        self.assertFalse(self.escape())
        self.assertEqual(kaho.job_generation, 0)

    def test_other_keys_are_passed_through(self):
        kaho.overlay.is_working.return_value = True
        self.assertFalse(kaho.handle_key_down(FakeEvent(kaho.ESCAPE_KEYCODE + 1, 0)))
        self.assertEqual(kaho.job_generation, 0)

    def speak(self):
        """Put audible samples in the live buffer.

        Without this every recording is dropped as "no audio captured", and a
        test cannot tell a cancelled recording from an empty one — which let
        two mutations of the cancel logic pass.
        """
        loud = REAL_NUMPY.full((kaho.SAMPLE_RATE, 1), 0.5, dtype="float32")
        kaho.record_buf.append(loud)

    def queued_audio(self):
        """Dictation jobs on the queue, ignoring WARMUP.

        start_recording() queues a warmup sentinel when the models have gone
        idle, so `jobs.empty()` is not the question — whether the *recording*
        made it through is.
        """
        items = []
        while not kaho.jobs.empty():
            item = kaho.jobs.get()
            if item is not kaho.WARMUP:
                items.append(item)
        return items

    def run_one_job(self):
        """The real worker body, minus its `while True`."""
        audio, generation, spans, released = kaho.jobs.get()
        kaho.run_job(audio, generation, spans, released, self.clock.now)


class TestRelaunch(KahoTestCase):
    """Recovering from a wedged audio device is the only cure for it."""

    def setUp(self):
        super().setUp()
        kaho.AppKit.NSApp.terminate_.reset_mock()

    def test_it_reopens_the_app_bundle_then_quits(self):
        with mock.patch.object(kaho, "__file__", "/Applications/Kaho.app/Contents/Resources/kaho.py"), \
             mock.patch.object(kaho.subprocess, "Popen") as popen:
            kaho.relaunch()

        command = popen.call_args.args[0][-1]
        self.assertIn("/Applications/Kaho.app", command)
        # The bundle, not the file inside it, and not Contents/Resources
        self.assertNotIn("Resources", command)
        # `open -n`, or macOS activates the instance on its way out instead
        # of launching a fresh one
        self.assertIn("open -n", command)
        # Detached, or the replacement dies with the process that spawned it
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        kaho.AppKit.NSApp.terminate_.assert_called_once()

    def test_the_replacement_waits_for_this_copy_to_exit(self):
        with mock.patch.object(kaho, "__file__", "/Applications/Kaho.app/Contents/Resources/kaho.py"), \
             mock.patch.object(kaho.subprocess, "Popen") as popen:
            kaho.relaunch()
        self.assertIn("sleep", popen.call_args.args[0][-1])

    def test_run_from_source_it_quits_rather_than_reopening_nothing(self):
        """Relaunching only works from a bundle.

        The first version of this used NSBundle.mainBundle(), which returns
        Homebrew's Python.app because that is the running executable — so it
        reopened the Python framework, the app quit, and nothing came back.
        """
        with mock.patch.object(kaho, "__file__", "/Users/me/src/kaho/kaho.py"), \
             mock.patch.object(kaho.subprocess, "Popen") as popen:
            kaho.relaunch()

        popen.assert_not_called()
        kaho.AppKit.NSApp.terminate_.assert_called_once()
        self.assertTrue(any("by hand" in m for m in self.logged))


class TestSottoMigration(KahoTestCase):
    """2.0 renamed the app; the user's data has to survive that."""

    def setUp(self):
        super().setUp()
        root = pathlib.Path(self.tmp.name)
        self.old = root / "Sotto"
        self.new = root / "Kaho"
        self.enterContext(mock.patch.object(kaho, "SUPPORT_DIR", str(self.new)))
        self.enterContext(
            mock.patch.object(kaho.os.path, "expanduser", lambda p: str(self.old))
        )

    def test_history_and_dictionary_move_to_the_new_name(self):
        self.old.mkdir()
        (self.old / "history.jsonl").write_text('{"text": "hello"}\n')
        (self.old / "dictionary.txt").write_text("Kaho\nmlx\n")

        self.assertTrue(kaho.migrate_sotto_support_dir())

        self.assertFalse(self.old.exists())
        self.assertEqual((self.new / "history.jsonl").read_text(), '{"text": "hello"}\n')
        self.assertEqual((self.new / "dictionary.txt").read_text(), "Kaho\nmlx\n")

    def test_the_stale_venv_is_dropped(self):
        # Its scripts and pyvenv.cfg hardcode the old path, so it cannot run
        self.old.mkdir()
        (self.old / "venv" / "bin").mkdir(parents=True)
        (self.old / "venv" / "bin" / "python").write_text("#!/bin/sh\n")
        (self.old / "settings.json").write_text('{"rewrite": "caveman"}')

        kaho.migrate_sotto_support_dir()

        self.assertFalse((self.new / "venv").exists())
        self.assertEqual((self.new / "settings.json").read_text(), '{"rewrite": "caveman"}')

    def test_existing_data_under_the_new_name_is_never_overwritten(self):
        self.old.mkdir()
        (self.old / "history.jsonl").write_text("stale\n")
        self.new.mkdir()
        (self.new / "history.jsonl").write_text("current\n")

        with mock.patch.object(kaho.os, "rename") as rename:
            self.assertFalse(kaho.migrate_sotto_support_dir())

        # os.rename onto a non-empty directory happens to raise, so assert the
        # guard declined rather than trusting the filesystem to refuse for us
        rename.assert_not_called()
        self.assertEqual((self.new / "history.jsonl").read_text(), "current\n")
        self.assertTrue(self.old.exists())

    def test_a_fresh_install_has_nothing_to_migrate(self):
        self.assertFalse(kaho.migrate_sotto_support_dir())
        self.assertFalse(self.new.exists())


if __name__ == "__main__":
    unittest.main()
