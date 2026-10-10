"""Deterministic audio subprocess faults, without microphone/model access."""

import argparse
import json
import mmap
import os
import pathlib
import struct
import sys
import time
import types

parser = argparse.ArgumentParser()
parser.add_argument("mode")
parser.add_argument("--audio-fd", type=int)
parser.add_argument("--device")
args = parser.parse_args()

if args.mode.startswith("native_"):
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import audio_capture

    class NativeStream:
        def __init__(self, *, callback, **_):
            if args.mode == "native_open_hang":
                time.sleep(30)
            if os.environ.get("KAHO_TEST_OPENED"):
                pathlib.Path(os.environ["KAHO_TEST_OPENED"]).write_text("opened")
            self.callback = callback

        def start(self):
            data = types.SimpleNamespace(tobytes=lambda: struct.pack("<1600f", *([0.25] * 1600)))
            self.callback(data, 1600, None, None)

        def stop(self):
            time.sleep(30)

        def close(self):
            pass

    sys.modules["sounddevice"] = types.SimpleNamespace(
        InputStream=NativeStream, _terminate=lambda: None, _initialize=lambda: None)
    sys.argv = [sys.argv[0], *sys.argv[2:]]
    audio_capture.child_main()


def emit(event, **values):
    print(json.dumps({"event": event, **values}), flush=True)


if args.mode == "start_hang":
    time.sleep(30)
if args.mode == "start_error":
    emit("error", message="device unavailable")
    os._exit(1)
storage = mmap.mmap(args.audio_fd, os.fstat(args.audio_fd).st_size)
# Same handshake as the real child: ready, then nothing until "open"
emit("ready")
if os.read(0, 64) != b"open\n":
    os._exit(0)
emit("started")
storage[:6400] = struct.pack("<1600f", *([0.25] * 1600))
emit("frames", frames=1600)
if args.mode == "crash":
    os._exit(2)
command = os.read(0, 64)
if args.mode == "freeze_hang":
    time.sleep(30)
if args.mode == "bad_frames":
    emit("frames", frames=100_000_000)
    time.sleep(30)
# The final frame range is only announced after the release command.
storage[6400:12800] = struct.pack("<1600f", *([0.5] * 1600))
emit("frames", frames=3200)
emit("frozen", reason="released")
if args.mode == "close_hang":
    time.sleep(30)
