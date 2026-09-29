"""Unit tests for the parts of Sotto that do not need a Mac.

sotto.py imports AppKit, Quartz, sounddevice, mlx_whisper, huggingface_hub and
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
    """Stand in for the Mac-only and ML dependencies before sotto is imported.

    AppKit needs real classes for NSObject and NSView because sotto subclasses
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
    # Real exception class: sotto catches it, and `except <MagicMock>` is a
    # TypeError at raise time
    sounddevice.PortAudioError = type("PortAudioError", (Exception,), {})

    pyobjctools = types.ModuleType("PyObjCTools")
    pyobjctools.AppHelper = _Stub("PyObjCTools.AppHelper")

    # sotto loads Carbon at import time for IsSecureEventInputEnabled. There is
    # no such framework off a Mac and ctypes.CDLL raises rather than degrading,
    # so the loader itself is stubbed for the life of the test process.
    ctypes.CDLL = mock.MagicMock(name="ctypes.CDLL")

    for name, module in (
        ("AppKit", appkit),
        ("Quartz", _Stub("Quartz")),
        ("sounddevice", sounddevice),
        ("mlx_whisper", _Stub("mlx_whisper")),
        ("huggingface_hub", _Stub("huggingface_hub")),
        ("numpy", REAL_NUMPY or _Stub("numpy")),
        ("PyObjCTools", pyobjctools),
        ("PyObjCTools.AppHelper", pyobjctools.AppHelper),
    ):
        sys.modules.setdefault(name, module)


_install_stubs()
# The stubs have to be in place before this import, not at the top of the file
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import sotto


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


class SottoTestCase(unittest.TestCase):
    """Resets the module globals and redirects every path into a tmpdir."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = pathlib.Path(self.tmp.name)

        for name, value in (
            ("LOG_PATH", str(tmp / "Sotto.log")),
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
            ("history_version", 0),
            ("rewriter", None),
        ):
            self.enterContext(mock.patch.object(sotto, name, value))
        self.enterContext(mock.patch.object(sotto, "settings", {"hotkey": "right_option", "rewrite": "off"}))
        self.enterContext(mock.patch.object(sotto, "overlay", mock.MagicMock()))

        # Keep the log out of stdout, and let tests read what was logged
        self.logged = []
        self.enterContext(mock.patch.object(sotto, "log", self.logged.append))

        sotto.history.clear()
        while not sotto.audio_ops.empty():
            sotto.audio_ops.get()
        while not sotto.jobs.empty():
            sotto.jobs.get()

        self.clock = Clock()
        self.enterContext(mock.patch("sotto.time.monotonic", self.clock))

        # A fresh stream object per open, so the tests can tell them apart
        sotto.sd.InputStream.reset_mock()
        sotto.sd.InputStream.side_effect = lambda **_: mock.MagicMock(name="stream")
        sotto.AppKit.NSTimer.reset_mock()
        sotto.AppKit.NSPasteboard.reset_mock()
        sotto.AppHelper.callAfter.reset_mock()

    def run_audio_ops(self):
        """Drain the queue the way the audio thread would, in order."""
        while not sotto.audio_ops.empty():
            sotto.audio_ops.get()()

    def down(self):
        keycode, mask, _ = sotto.HOTKEYS[sotto.settings["hotkey"]]
        sotto.handle_flags_changed(FakeEvent(keycode, mask))

    def up(self):
        keycode, _, _ = sotto.HOTKEYS[sotto.settings["hotkey"]]
        sotto.handle_flags_changed(FakeEvent(keycode, 0))

    def deferred_stop(self):
        """The block schedule_deferred_stop handed to NSTimer."""
        calls = sotto.AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_.call_args_list
        self.assertTrue(calls, "no deferred stop was scheduled")
        interval, repeats, block = calls[-1][0]
        self.assertEqual(interval, sotto.DOUBLE_TAP_SECONDS)
        self.assertFalse(repeats)
        return block


class TestTapStateMachine(SottoTestCase):
    def test_hold_then_release_records_and_stops(self):
        self.down()
        self.assertEqual(sotto.state, "recording")
        self.clock.advance(2.0)  # a real hold, well past TAP_MAX_SECONDS
        self.up()
        self.assertEqual(sotto.state, "ready")
        sotto.overlay.setPhase_.assert_called_with("transcribing")

    def test_other_keys_are_ignored(self):
        other = sotto.HOTKEYS["right_shift"][0]
        sotto.handle_flags_changed(FakeEvent(other, sotto.HOTKEYS["right_shift"][1]))
        self.assertEqual(sotto.state, "ready")

    def test_a_tap_defers_the_stop_instead_of_running_it(self):
        # Stopping immediately is what forced the reopen that recorded silence
        self.down()
        self.clock.advance(0.1)
        self.up()
        self.assertEqual(sotto.state, "recording")
        self.deferred_stop()

    def test_the_deferred_stop_fires_when_no_second_tap_arrives(self):
        self.down()
        self.clock.advance(0.1)
        self.up()
        block = self.deferred_stop()
        self.clock.advance(sotto.DOUBLE_TAP_SECONDS)
        block(None)
        self.assertEqual(sotto.state, "ready")

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
        self.assertTrue(sotto.locked)
        self.assertEqual(sotto.state, "recording")
        # The first tap's pending stop must not tear the live stream down
        block(None)
        self.assertEqual(sotto.state, "recording")

    def test_a_double_tap_never_reopens_the_audio_stream(self):
        """The 1.7.5 bug: hands-free recorded a silent room.

        Stopping on the first tap and reopening ~60 ms later handed back a
        stream that captured silence, because PortAudio had not finished
        releasing the device — and Whisper hallucinated fluent paragraphs from
        that noise floor. One hold, one stream: the double-tap must run start
        to lock without a stop in between.
        """
        opened = []
        sotto.sd.InputStream.side_effect = lambda **_: opened.append(mock.MagicMock()) or opened[-1]
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
        self.assertFalse(sotto.locked)
        self.assertEqual(sotto.state, "recording")

    def test_a_tap_inside_the_lock_grace_is_the_tail_of_the_double_tap(self):
        self._lock_hands_free()
        self.clock.advance(sotto.LOCK_GRACE_SECONDS / 2)
        self.down()
        self.assertTrue(sotto.locked)
        self.assertEqual(sotto.state, "recording")

    def test_a_tap_after_the_lock_grace_stops_the_recording(self):
        self._lock_hands_free()
        self.clock.advance(sotto.LOCK_GRACE_SECONDS + 0.1)
        self.down()
        self.assertFalse(sotto.locked)
        self.assertEqual(sotto.state, "ready")

    def test_releasing_the_key_while_locked_keeps_recording(self):
        self._lock_hands_free()
        self.clock.advance(0.1)
        self.up()
        self.assertTrue(sotto.locked)
        self.assertEqual(sotto.state, "recording")

    def _lock_hands_free(self):
        self.down()
        self.clock.advance(0.1)
        self.up()
        self.clock.advance(0.7)
        self.down()
        self.clock.advance(0.1)
        self.up()
        self.assertTrue(sotto.locked, "setup failed to lock hands-free")


class TestRecordingHandoff(SottoTestCase):
    """The stream must end up owned by exactly one side of the handoff.

    These pin the two orderings that are reachable from a test: the open lands
    before the release, and the open is still queued when the release arrives.
    The interleaving that caused the leak — a release between _open_stream's
    check and its publish — is two adjacent statements apart and cannot be
    staged from outside the function, which is exactly why the fix is a lock
    around the handoff rather than a test asserting it never happens.
    """

    def test_a_completed_recording_closes_its_stream_once(self):
        sotto.start_recording()
        buf = sotto.record_buf
        self.run_audio_ops()  # the open lands while the key is still held
        opened = sotto.stream
        self.assertIsNotNone(opened)

        sotto.stop_recording()
        self.assertIsNone(sotto.stream)
        self.run_audio_ops()
        opened.stop.assert_called_once()
        opened.close.assert_called_once()
        self.assertEqual(sotto.record_buf, None)
        self.assertIsNot(buf, None)

    def test_a_release_before_the_open_lands_leaves_no_stream_behind(self):
        opened = []
        sotto.sd.InputStream.side_effect = lambda **_: opened.append(mock.MagicMock()) or opened[-1]
        sotto.start_recording()
        sotto.stop_recording()  # key released while the open is still queued
        self.run_audio_ops()
        self.assertEqual(len(opened), 1, "expected exactly one stream to be opened")
        opened[0].stop.assert_called_once()
        opened[0].close.assert_called_once()
        self.assertIsNone(sotto.stream)

    def test_a_failed_open_returns_to_ready(self):
        sotto.sd.InputStream.side_effect = sotto.sd.PortAudioError("no device")
        sotto.start_recording()
        self.assertEqual(sotto.state, "recording")
        self.run_audio_ops()
        self.assertEqual(sotto.state, "ready")
        self.assertFalse(sotto.locked)

    def test_a_wedged_audio_device_skips_the_recording(self):
        sotto.audio_op_started = self.clock.now - 10
        sotto.start_recording()
        self.assertEqual(sotto.state, "ready")
        self.assertTrue(any("not responding" in m for m in self.logged))


class TestSettings(SottoTestCase):
    def test_a_missing_file_keeps_the_defaults(self):
        sotto.load_settings()
        self.assertEqual(sotto.settings, {"hotkey": "right_option", "rewrite": "off"})

    def test_malformed_json_keeps_the_defaults(self):
        pathlib.Path(sotto.SETTINGS_PATH).write_text("{not json")
        sotto.load_settings()
        self.assertEqual(sotto.settings["rewrite"], "off")

    def test_the_bullets_rename_still_loads(self):
        # 1.6.0 renamed the mode; without the migration it reverts to Off
        pathlib.Path(sotto.SETTINGS_PATH).write_text(json.dumps({"rewrite": "bullets"}))
        sotto.load_settings()
        self.assertEqual(sotto.settings["rewrite"], "structured")

    def test_unknown_values_are_ignored(self):
        pathlib.Path(sotto.SETTINGS_PATH).write_text(
            json.dumps({"hotkey": "left_option", "rewrite": "shakespeare"})
        )
        sotto.load_settings()
        self.assertEqual(sotto.settings, {"hotkey": "right_option", "rewrite": "off"})

    def test_saved_settings_round_trip_and_stay_private(self):
        sotto.settings["rewrite"] = "caveman"
        sotto.save_settings()
        self.assertEqual(os.stat(sotto.SETTINGS_PATH).st_mode & 0o777, 0o600)
        sotto.settings["rewrite"] = "off"
        sotto.load_settings()
        self.assertEqual(sotto.settings["rewrite"], "caveman")


class TestDictionary(SottoTestCase):
    def test_a_missing_file_yields_no_terms(self):
        self.assertEqual(sotto.read_dictionary(), [])

    def test_comments_and_blank_lines_are_skipped(self):
        pathlib.Path(sotto.DICTIONARY_PATH).write_text(
            "# a comment\n\n  Duckterm  \nKubernetes\n"
        )
        self.assertEqual(sotto.read_dictionary(), ["Duckterm", "Kubernetes"])

    def test_the_term_cap_protects_whispers_prompt_window(self):
        terms = [f"term{i}" for i in range(sotto.DICTIONARY_MAX_TERMS + 5)]
        pathlib.Path(sotto.DICTIONARY_PATH).write_text("\n".join(terms))
        read = sotto.read_dictionary()
        self.assertEqual(len(read), sotto.DICTIONARY_MAX_TERMS)
        self.assertTrue(any("224 tokens" in m for m in self.logged))

    def test_the_prompt_reads_as_a_sentence(self):
        # A bare list of nouns biases Whisper toward transcribing lists
        prompt = sotto.dictionary_prompt(["Sotto", "Duckterm"])
        self.assertTrue(prompt.endswith("."))
        self.assertIn("Sotto, Duckterm", prompt)

    def test_the_template_is_created_once_and_never_overwritten(self):
        sotto.ensure_dictionary_file()
        path = pathlib.Path(sotto.DICTIONARY_PATH)
        self.assertIn("one term per line", path.read_text())
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        path.write_text("Mine\n")
        sotto.ensure_dictionary_file()
        self.assertEqual(path.read_text(), "Mine\n")


class TestHistory(SottoTestCase):
    def test_a_missing_file_yields_no_entries(self):
        self.assertEqual(sotto.read_history_file(), [])

    def test_a_damaged_line_never_takes_the_app_down(self):
        pathlib.Path(sotto.HISTORY_PATH).write_text(
            json.dumps({"t": 1.0, "text": "first"}) + "\n"
            + "{truncated\n"
            + json.dumps({"t": 2.0, "text": "second"}) + "\n"
        )
        self.assertEqual(sotto.read_history_file(), [(1.0, "first"), (2.0, "second")])
        self.assertTrue(any("malformed" in m for m in self.logged))

    def test_appending_persists_the_entry_privately_and_newest_first(self):
        sotto.append_history("older")
        sotto.append_history("newer")
        self.assertEqual([text for _, text in sotto.history], ["newer", "older"])
        self.assertEqual(sotto.history_version, 2)
        self.assertEqual(os.stat(sotto.HISTORY_PATH).st_mode & 0o777, 0o600)
        self.assertEqual([text for _, text in sotto.read_history_file()], ["older", "newer"])

    def test_startup_loads_the_last_entries_newest_first(self):
        lines = [
            json.dumps({"t": float(i), "text": f"entry {i}"}) + "\n"
            for i in range(sotto.HISTORY_SIZE + 3)
        ]
        pathlib.Path(sotto.HISTORY_PATH).write_text("".join(lines))
        sotto.load_history()
        texts = [text for _, text in sotto.history]
        self.assertEqual(len(texts), sotto.HISTORY_SIZE)
        self.assertEqual(texts[0], f"entry {sotto.HISTORY_SIZE + 2}")
        self.assertEqual(texts[-1], "entry 3")


@unittest.skipUnless(HAVE_NUMPY, "the audio drop rules need real numpy arrays")
class TestCleanSkip(SottoTestCase):
    """Clean up skips generation when the model is sure it would change nothing."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(sotto, "rewriter", (mock.MagicMock(), mock.MagicMock())))
        self.mlx_lm = self.enterContext(mock.patch.object(sotto, "mlx_lm", mock.MagicMock()))
        self.mlx_lm.generate.return_value = "Rewritten."

    def check_returns(self, value):
        return self.enterContext(mock.patch.object(sotto, "clean_as_is_probability", return_value=value))

    def test_a_confident_check_pastes_the_transcript_untouched(self):
        self.check_returns(0.99)
        self.assertIsNone(sotto.rewrite("Thanks, that works for me.", "clean"))
        self.mlx_lm.generate.assert_not_called()

    def test_an_unsure_check_still_rewrites(self):
        self.check_returns(sotto.SKIP_REWRITE_CONFIDENCE - 0.01)
        self.assertEqual(sotto.rewrite("um so yeah", "clean"), "Rewritten.")

    def test_a_failed_check_rewrites_instead_of_skipping(self):
        self.enterContext(
            mock.patch.object(sotto, "clean_as_is_probability", side_effect=RuntimeError("metal"))
        )
        self.assertEqual(sotto.rewrite("um so yeah", "clean"), "Rewritten.")

    def test_other_modes_never_run_the_check(self):
        check = self.check_returns(1.0)
        for mode in ("structured", "caveman"):
            self.assertEqual(sotto.rewrite("Thanks, that works for me.", mode), "Rewritten.")
        check.assert_not_called()


class TestAudioDropRules(SottoTestCase):
    """What reaches Whisper, and what is dropped before it can hallucinate."""

    def frames(self, seconds, amplitude):
        np = REAL_NUMPY
        n = int(sotto.SAMPLE_RATE * seconds)
        # A tone rather than a constant: peak is what the rules look at, but a
        # constant would also make rms meaningless if these ever grow
        wave = amplitude * np.sin(np.linspace(0, 40 * np.pi, n, dtype="float32"))
        return [wave.reshape(-1, 1)]

    def test_no_frames_at_all(self):
        audio, message = sotto._audio_or_drop_reason([])
        self.assertIsNone(audio)
        self.assertEqual(message, "dropped: no audio captured")

    def test_a_hold_too_short_to_be_speech(self):
        audio, message = sotto._audio_or_drop_reason(self.frames(0.1, 0.5))
        self.assertIsNone(audio)
        self.assertIn(f"under the {sotto.MIN_SECONDS}s minimum", message)

    def test_pure_silence_names_the_permission(self):
        audio, message = sotto._audio_or_drop_reason(self.frames(1.0, 0.0))
        self.assertIsNone(audio)
        self.assertIn("macOS delivered no mic signal", message)
        self.assertIn("Microphone", message)

    def test_audio_under_the_speech_floor(self):
        audio, message = sotto._audio_or_drop_reason(self.frames(1.0, sotto.MIN_PEAK / 2))
        self.assertIsNone(audio)
        self.assertIn("too quiet to be speech", message)

    def test_a_real_dictation_gets_through(self):
        audio, message = sotto._audio_or_drop_reason(self.frames(2.0, 0.2))
        self.assertIsNotNone(audio)
        self.assertEqual(len(audio), 2 * sotto.SAMPLE_RATE)
        self.assertIn("recorded 2.0s", message)
        self.assertIn("transcribing", message)

    def test_finish_recording_hides_the_pill_on_every_drop(self):
        for buf in ([], self.frames(0.1, 0.5), self.frames(1.0, 0.0),
                    self.frames(1.0, sotto.MIN_PEAK / 2)):
            sotto.AppHelper.callAfter.reset_mock()
            sotto._finish_recording(None, buf)
            sotto.AppHelper.callAfter.assert_called_once_with(sotto.overlay.hide)
            self.assertTrue(sotto.jobs.empty(), "dropped audio must not reach the worker")

    def test_finish_recording_queues_usable_audio(self):
        sotto._finish_recording(None, self.frames(2.0, 0.2))
        self.assertFalse(sotto.jobs.empty())
        sotto.AppHelper.callAfter.assert_not_called()


class TestMenuWiring(unittest.TestCase):
    """The status menu and the app menu are built from one table."""

    def test_every_menu_action_has_a_handler(self):
        # A typo in a selector is silent at runtime: the item just does nothing
        for title, action, _ in sotto.MENU_ACTIONS:
            self.assertTrue(
                hasattr(sotto.StatusItem, action.replace(":", "_")),
                f"{title!r} points at {action!r}, which StatusItem does not implement",
            )

    def test_the_app_menu_drops_open_log_and_quits_through_nsapp(self):
        sotto.AppKit.NSMenuItem.reset_mock()
        with mock.patch.object(sotto, "status_item", mock.MagicMock()):
            sotto.install_app_menu()
        built = [
            call[0]
            for call in sotto.AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_.call_args_list
        ]
        self.assertNotIn("openLog:", [action for _, action, _ in built])
        self.assertIn(("Quit Sotto", "terminate:", "q"), built)
        self.assertIn(("Settings…", "showSettings:", ","), built)
        # The separator that sits above Quit
        sotto.AppKit.NSMenuItem.separatorItem.assert_called_once()


class TestSettingsWiring(SottoTestCase):
    """A change on either surface has to reach the other."""

    def setUp(self):
        super().setUp()
        self.status = mock.MagicMock()
        self.window = mock.MagicMock()
        self.enterContext(mock.patch.object(sotto, "status_item", self.status))
        self.enterContext(mock.patch.object(sotto, "settings_win", self.window))

    def test_applying_a_hotkey_saves_it_and_refreshes_both_surfaces(self):
        sotto.apply_hotkey("right_shift")
        self.assertEqual(sotto.settings["hotkey"], "right_shift")
        self.assertIn('"hotkey": "right_shift"', pathlib.Path(sotto.SETTINGS_PATH).read_text())
        self.status.rebuildMenu.assert_called_once()
        self.window.syncControls.assert_called_once()
        self.assertTrue(any("Right Shift" in m for m in self.logged))

    def test_applying_a_rewrite_mode_loads_the_model_only_when_on(self):
        with mock.patch.object(sotto, "ensure_rewriter") as ensure:
            sotto.apply_rewrite("caveman")
            ensure.assert_called_once()
            ensure.reset_mock()
            sotto.apply_rewrite("off")
            ensure.assert_not_called()
        self.assertEqual(sotto.settings["rewrite"], "off")

    def test_opening_the_dictionary_creates_the_template_first(self):
        with mock.patch.object(sotto, "subprocess") as sp:
            sotto.open_dictionary()
        self.assertTrue(pathlib.Path(sotto.DICTIONARY_PATH).exists())
        sp.run.assert_called_once_with(["open", "-t", sotto.DICTIONARY_PATH], check=False)


class TestRewriterLoading(SottoTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(sotto, "rewriter_thread", None))

    def test_repeated_calls_start_one_loader(self):
        with mock.patch.object(sotto, "threading") as threading_stub:
            started = threading_stub.Thread.return_value
            started.is_alive.return_value = True
            sotto.ensure_rewriter()
            sotto.ensure_rewriter()
            sotto.ensure_rewriter()
        threading_stub.Thread.assert_called_once_with(target=sotto._load_rewriter, daemon=True)
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

        def factory(target=None, daemon=None):
            handle = mock.MagicMock()
            handle.is_alive.return_value = True
            created.append(handle)
            if len(created) == 1:
                other = real_thread(target=sotto.ensure_rewriter, daemon=True)
                other.start()
                other.join(timeout=0.3)
                # Still alive = still waiting on the lock, which is the point
                was_blocked.append(other.is_alive())
            return handle

        with mock.patch("sotto.threading.Thread", factory):
            sotto.ensure_rewriter()

        self.assertEqual(was_blocked, [True], "the second caller was not held at the lock")
        self.assertEqual(len(created), 1, "two loaders were started for one model")

    def test_nothing_starts_once_the_model_is_loaded(self):
        self.enterContext(mock.patch.object(sotto, "rewriter", ("model", "tokenizer")))
        self.enterContext(mock.patch.object(sotto, "rewriter_thread", None))
        with mock.patch.object(sotto, "threading") as threading_stub:
            sotto.ensure_rewriter()
        threading_stub.Thread.assert_not_called()


class TestHallucinationFilter(unittest.TestCase):
    """Whisper's repetition loops must not reach the user's editor."""

    def test_short_transcripts_are_always_kept(self):
        self.assertFalse(sotto.looks_hallucinated("fix the header and update the changelog"))

    def test_a_repetition_loop_is_caught(self):
        self.assertTrue(sotto.looks_hallucinated("videos " * 400))

    def test_punctuation_does_not_disguise_a_loop(self):
        self.assertTrue(sotto.looks_hallucinated("Videos, videos. videos! " * 100))

    def test_a_genuine_long_dictation_survives(self):
        text = (
            "the release is blocked on three things, the header is broken on "
            "mobile, the changelog still says version one point six, and we "
            "never emailed the beta testers about any of it"
        )
        self.assertGreaterEqual(len(text.split()), 20)
        self.assertFalse(sotto.looks_hallucinated(text))


class TestPasteClipboardHandling(SottoTestCase):
    """The clipboard is the user's, borrowed for one keystroke."""

    def setUp(self):
        super().setUp()
        self.pb = sotto.AppKit.NSPasteboard.generalPasteboard.return_value
        self.pb.reset_mock()
        self.pb.stringForType_.return_value = "what the user had copied"
        self.pb.changeCount.return_value = 7

    def scheduled_restore(self):
        """The restore block, dug out of callAfter -> NSTimer."""
        (scheduler,), _ = sotto.AppHelper.callAfter.call_args
        scheduler()
        args = sotto.AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_.call_args[0]
        interval, repeats, block = args
        self.assertEqual(interval, sotto.CLIPBOARD_RESTORE_SECONDS)
        self.assertFalse(repeats)
        return block

    def test_the_transcript_is_marked_transient_for_clipboard_managers(self):
        # Maccy, Paste and Raycast archive everything on the pasteboard; a
        # dictated transcript is transient and often private
        sotto.paste("a private transcript")
        declared = self.pb.declareTypes_owner_.call_args[0][0]
        for transient in sotto.TRANSIENT_TYPES:
            self.assertIn(transient, declared)
        self.pb.setString_forType_.assert_called_with(
            "a private transcript", sotto.AppKit.NSPasteboardTypeString
        )

    def test_the_previous_clipboard_comes_back_when_untouched(self):
        sotto.paste("a transcript")
        self.scheduled_restore()(None)
        self.pb.setString_forType_.assert_called_with(
            "what the user had copied", sotto.AppKit.NSPasteboardTypeString
        )

    def test_a_clipboard_the_user_changed_is_never_clobbered(self):
        sotto.paste("a transcript")
        restore = self.scheduled_restore()
        self.pb.changeCount.return_value = 9  # the user copied something else
        restore(None)
        self.pb.setString_forType_.assert_called_with(
            "a transcript", sotto.AppKit.NSPasteboardTypeString
        )

    def test_an_empty_clipboard_schedules_no_restore(self):
        self.pb.stringForType_.return_value = None
        sotto.paste("a transcript")
        sotto.AppHelper.callAfter.assert_not_called()


class TestConstantsAgree(unittest.TestCase):
    """Cheap guards for the tables that drift apart when a mode is renamed."""

    def test_every_rewrite_mode_has_a_hint(self):
        self.assertEqual(set(sotto.REWRITE_MODES), set(sotto.REWRITE_HINTS))

    def test_every_rewriting_mode_has_a_prompt_expecting_the_transcript(self):
        self.assertEqual(set(sotto.REWRITE_PROMPTS), set(sotto.REWRITE_MODES) - {"off"})
        for mode, prompt in sotto.REWRITE_PROMPTS.items():
            self.assertIn("{text}", prompt, mode)

    def test_every_state_has_a_menu_bar_glyph(self):
        self.assertEqual(set(sotto.TITLES), {"loading", "ready", "recording", "error"})

    def test_every_post_release_phase_has_a_label(self):
        self.assertEqual(
            set(sotto.PHASE_LABELS), {"transcribing", "rewriting", "done", "blocked"}
        )

    def test_hotkeys_are_distinct_and_labelled(self):
        keycodes = [k for k, _, _ in sotto.HOTKEYS.values()]
        masks = [m for _, m, _ in sotto.HOTKEYS.values()]
        labels = [label for _, _, label in sotto.HOTKEYS.values()]
        self.assertEqual(len(set(keycodes)), len(keycodes))
        self.assertEqual(len(set(masks)), len(masks))
        self.assertEqual(len(set(labels)), len(labels))


if __name__ == "__main__":
    unittest.main()
