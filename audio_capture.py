"""Recoverable microphone capture. No AppKit, MLX, or models in the child.

Audio is exchanged through an unlinked, bounded shared file. The pipe carries
only published frame counts; samples are never logged or left in named files.
Only the child enters PortAudio. A stuck stop/close cannot strand the next open.
"""

import json
import mmap
import os
import select
import subprocess
import sys
import tempfile
import threading
import time

SAMPLE_RATE = 16_000
MAX_SECONDS = 600
MAX_BYTES = SAMPLE_RATE * MAX_SECONDS * 4
START_TIMEOUT = 5.0
FREEZE_TIMEOUT = 1.0
CLOSE_TIMEOUT = 0.35


class CaptureError(RuntimeError):
    pass


class InputStream:
    """The subset of sounddevice.InputStream Kaho uses, plus freeze().

    Construct/start/freeze/stop/close on the audio executor. The callback runs
    on a reader thread, not a native audio thread. freeze() completes that
    callback before the caller snapshots its recording buffer.
    """

    def __init__(self, *, device, samplerate, channels, dtype, callback,
                 on_error=None, log=None, command=None):
        if (samplerate, channels, dtype) != (SAMPLE_RATE, 1, "float32"):
            raise ValueError("capture requires 16 kHz mono float32")
        self.device = device
        self.callback = callback
        self.on_error = on_error or (lambda _message: None)
        self.log = log or (lambda _message: None)
        self.command = command
        self.process = None
        self.reader = None
        self.storage = None
        self.mapping = None
        self.ready = threading.Event()
        self.started = threading.Event()
        self.frozen = threading.Event()
        self.stopping = threading.Event()
        self.error = None
        self.frames = 0
        self._closed = False

    def prepare(self):
        """Start the child without opening the microphone.

        The child imports sounddevice and then waits for "open". Done between
        recordings, this takes process startup (about 200 ms, over a second on
        a cold launch) out of the time between the key press and the first
        captured audio. No microphone is opened, so macOS shows no indicator.
        """
        if self.process is not None or self._closed:
            raise CaptureError("capture already started or closed")
        command = self.command
        if command is None:
            command = ([sys.executable, "--kaho-audio-helper"] if getattr(sys, "frozen", False)
                       else [sys.executable, os.path.abspath(__file__)])
        try:
            self.storage = tempfile.TemporaryFile()  # noqa: SIM115 - owned until close()
            self.storage.truncate(MAX_BYTES)
            self.mapping = mmap.mmap(self.storage.fileno(), MAX_BYTES, access=mmap.ACCESS_READ)
            self.process = subprocess.Popen(
                [*command, "--audio-fd", str(self.storage.fileno()), "--device", json.dumps(self.device)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                pass_fds=(self.storage.fileno(),), bufsize=0,
            )
            self.reader = threading.Thread(target=self._read, name="Kaho audio reader", daemon=True)
            self.reader.start()
        except BaseException:
            self.close()
            raise

    def usable(self):
        """Whether a prepared child can still be opened."""
        return (self.process is not None and not self._closed and self.process.poll() is None
                and not self.error and not self.started.is_set())

    def start(self):
        if self._closed or self.started.is_set():
            raise CaptureError("capture already started or closed")
        if self.process is None:
            self.prepare()
        try:
            try:
                self.process.stdin.write(b"open\n")
            except OSError as error:
                raise CaptureError("microphone process exited before opening") from error
            if not self.started.wait(START_TIMEOUT):
                raise CaptureError("microphone startup timed out")
            if self.error:
                raise CaptureError(self.error)
        except BaseException:
            self.close()
            raise

    def _read(self):
        try:
            import numpy as np

            while True:
                line = self.process.stdout.readline(4097)
                if not line:
                    raise CaptureError("microphone process exited unexpectedly")
                if len(line) > 4096 or not line.endswith(b"\n"):
                    raise CaptureError("invalid microphone message size")
                event = json.loads(line)
                kind = event["event"]
                if kind == "ready":
                    self.ready.set()
                elif kind == "started":
                    self.started.set()
                elif kind == "frames":
                    total = event["frames"]
                    if type(total) is not int or not self.frames <= total <= MAX_BYTES // 4:
                        raise CaptureError("invalid microphone frame count")
                    if total > self.frames:
                        # The child only appends. Published ranges are immutable.
                        data = np.frombuffer(self.mapping, dtype="<f4", count=total - self.frames,
                                             offset=self.frames * 4).copy().reshape(-1, 1)
                        self.frames = total
                        self.callback(data, len(data), None, None)
                elif kind == "frozen":
                    reason = event.get("reason", "released")
                    if reason != "released":
                        self.error = reason
                    return
                elif kind == "error":
                    raise CaptureError(str(event.get("message", "microphone failure"))[:1024])
                else:
                    raise CaptureError("unknown microphone event")
        except Exception as error:  # noqa: BLE001
            self.error = f"{type(error).__name__}: {error}"
        finally:
            self.started.set()  # Release a failed startup immediately.
            self.frozen.set()  # No callback can run after this point.
            if self.error:
                self.log(f"microphone capture interrupted: {self.error}")
                if not self.stopping.is_set():
                    self.on_error(self.error)

    def freeze(self):
        if self.process is None or self._closed:
            return
        if not self.stopping.is_set():
            self.stopping.set()
            try:
                self.process.stdin.write(b"stop\n")
            except (BrokenPipeError, OSError):
                pass
        if not self.frozen.wait(FREEZE_TIMEOUT):
            self.log("microphone capture timed out — recovering; using audio already received")
            self._reap(0)
            if not self.frozen.wait(1):
                raise CaptureError("microphone reader did not stop")

    def _reap(self, grace):
        if self.process is None:
            return
        try:
            self.process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self.process.kill()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired as error:
                raise CaptureError("microphone process could not be terminated") from error
            self.log("microphone process was stuck — terminated it; next recording can start normally")

    def stop(self):
        self.freeze()
        self._reap(CLOSE_TIMEOUT)

    def close(self):
        if self._closed:
            return
        self.stopping.set()
        self._reap(0)
        if self.reader:
            self.reader.join(1)
            if self.reader.is_alive():
                # Do not unmap storage under a reader still accessing it.
                raise CaptureError("microphone reader did not terminate")
        if self.process:
            self.process.stdin.close()
            self.process.stdout.close()
        if self.mapping:
            self.mapping.close()
        if self.storage:
            self.storage.close()
        self._closed = True


def child_main():
    """Audio-only process entrypoint, including the PyInstaller dispatch path."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--kaho-audio-helper", action="store_true")
    parser.add_argument("--audio-fd", required=True, type=int)
    parser.add_argument("--device", required=True)
    args = parser.parse_args()

    parent = os.getppid()

    def watch_parent():
        # Still runs if the main child thread is blocked inside CoreAudio.
        # Neither the microphone nor its anonymous storage should outlive Kaho.
        while True:
            time.sleep(0.5)
            if os.getppid() != parent:
                os._exit(0)

    threading.Thread(target=watch_parent, daemon=True).start()

    def emit(event, **values):
        packet = json.dumps({"event": event, **values}).encode() + b"\n"
        # os.write also works in a windowed PyInstaller executable, whose
        # sys.stdout may be None even though Popen supplied an output pipe.
        while packet:
            packet = packet[os.write(1, packet):]

    try:
        import sounddevice as sd

        if os.fstat(args.audio_fd).st_size != MAX_BYTES:
            raise CaptureError("invalid shared capture size")
        storage = mmap.mmap(args.audio_fd, MAX_BYTES, access=mmap.ACCESS_WRITE)
        lock = threading.Lock()
        active, frames, reason = True, 0, "released"
        last_audio = time.monotonic()

        def receive(data, _frames, _timing, status):
            nonlocal active, frames, reason, last_audio
            try:
                with lock:
                    if not active:
                        return
                    if status:
                        # Preserve preceding speech and make a discontinuity
                        # visible instead of silently transcribing broken audio.
                        reason, active = f"microphone reported {status}", False
                        return
                    raw = data.tobytes()
                    length = min(len(raw), MAX_BYTES - frames * 4)
                    storage[frames * 4:frames * 4 + length] = raw[:length]
                    frames += length // 4
                    last_audio = time.monotonic()
                    if frames * 4 == MAX_BYTES:
                        reason, active = "ten-minute recording limit reached", False
            except Exception as error:  # noqa: BLE001
                reason, active = f"audio callback failed: {error!r}", False

        def read_line(pending):
            while b"\n" not in pending:
                chunk = os.read(0, 64)
                if not chunk:
                    os._exit(0)  # Parent vanished; do not retain its mic.
                pending += chunk
                if len(pending) > 64:
                    raise CaptureError("invalid microphone command")
            line, _, rest = pending.partition(b"\n")
            return line, rest

        emit("ready")
        command, pending = read_line(b"")
        if command != b"open":
            os._exit(0)  # A discarded standby: the microphone was never opened.
        # A standby may have waited minutes since sounddevice enumerated the
        # devices. Refreshing costs 1-2 ms and picks up a headset plugged in
        # meanwhile, or a new system default.
        sd._terminate()
        sd._initialize()
        stream = sd.InputStream(device=json.loads(args.device), samplerate=SAMPLE_RATE,
                                channels=1, dtype="float32", callback=receive)
        stream.start()
        with lock:
            # Standby may have waited minutes with the mic closed. Start the
            # no-audio grace period when capture opens, not when the child did.
            last_audio = time.monotonic()
        emit("started")
        published = 0
        while True:
            if pending or select.select([0], [], [], 0.02)[0]:
                command, pending = read_line(pending)
                if command != b"stop":
                    raise CaptureError("invalid microphone command")
                with lock:
                    active = False
            with lock:
                total, capturing = frames, active
                if capturing and time.monotonic() - last_audio > 2:
                    reason, active, capturing = "microphone stopped delivering audio", False, False
            if total != published:
                emit("frames", frames=total)
                published = total
            if not capturing:
                break
        # No native shutdown before the parent has the final immutable range.
        emit("frozen", reason=reason)
        stream.stop()
        stream.close()
        os._exit(0)
    except Exception as error:  # noqa: BLE001
        try:
            emit("error", message=f"{type(error).__name__}: {error}")
        finally:
            # sounddevice's atexit teardown can itself enter stuck CoreAudio.
            os._exit(1)


if __name__ == "__main__":
    child_main()
