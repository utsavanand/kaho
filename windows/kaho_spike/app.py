"""kaho-spike: hold Right Ctrl, talk, release — the text is pasted at the cursor.

    kaho-spike.exe                      dictate (default engine: qwen3)
    kaho-spike.exe --engine parakeet    try another engine
    kaho-spike.exe bench                run the engine benchmark, print a table
"""

import argparse
import logging
import os
import pathlib
import sys
import threading
import time

MIN_SECONDS = 0.3  # shorter holds are slips, not speech
DATA = pathlib.Path(os.environ.get("LOCALAPPDATA", pathlib.Path.home())) / "KahoSpike"


def parse(argv):
    p = argparse.ArgumentParser(prog="kaho-spike", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("command", nargs="?", default="run", choices=["run", "bench"])
    p.add_argument("--engine", default="qwen3", choices=["qwen3", "parakeet", "moonshine"])
    p.add_argument("--hotwords", default="", help="comma-separated names/terms to bias qwen3 toward")
    p.add_argument("--threads", type=int, default=min(4, os.cpu_count() or 4))
    p.add_argument("--models-dir", default=str(DATA / "models"))
    p.add_argument("--out", help="bench: also write the Markdown table here")
    p.add_argument("--details", help="bench: also write every transcript as JSON here")
    return p.parse_args(argv)


def run(args):
    if sys.platform != "win32":
        sys.exit("dictation mode is Windows-only; `bench` runs anywhere")
    import numpy as np
    import sounddevice as sd
    from PySide6 import QtWidgets

    from . import engines
    from .clipboard import WinClipboard, paste
    from .hotkey import CANCEL, START, STOP, HotkeyHook
    from .overlay import Pill

    DATA.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.StreamHandler(), logging.FileHandler(DATA / "kaho-spike.log", encoding="utf-8")])
    log = logging.info
    hotwords = [w.strip() for w in args.hotwords.split(",") if w.strip()]
    log(f"loading {args.engine} …")
    t = time.monotonic()
    rec = engines.load(args.engine, args.models_dir, hotwords=hotwords, threads=args.threads)
    engines.transcribe(rec, np.zeros(engines.SAMPLE_RATE // 2, dtype=np.float32))  # warm-up
    log(f"ready in {time.monotonic() - t:.1f}s — hold Right Ctrl, talk, release. Ctrl+C here to quit.")

    app = QtWidgets.QApplication([])
    pill = Pill()
    clip = WinClipboard()
    chunks, lock = [], threading.Lock()
    started = [0.0]

    def on_audio(indata, frames, t_info, status):
        with lock:
            chunks.append(indata[:, 0].copy())

    stream = sd.InputStream(samplerate=engines.SAMPLE_RATE, channels=1, dtype="float32", callback=on_audio)

    def finish(audio):
        try:
            t0 = time.monotonic()
            text = engines.transcribe(rec, audio)
            dt = time.monotonic() - t0
            if not text:
                log(f"[{dt:.2f}s] (nothing heard)")
                pill.show_state.emit("Cancelled")
                return
            paste(clip, text, log)
            log(f"[{dt:.2f}s transcribe, {len(audio) / engines.SAMPLE_RATE:.1f}s audio] {len(text.split())} words pasted")
            pill.show_state.emit("Pasted")
        except Exception as e:  # noqa: BLE001 — the spike must keep listening after one bad dictation
            log(f"failed: {e!r}")
            pill.show_state.emit(f"Error: {e}"[:40])

    def on_action(action):
        if action == START:
            with lock:
                chunks.clear()
            started[0] = time.monotonic()
            stream.start()
            pill.show_state.emit("Recording")
        elif action in (STOP, CANCEL):
            stream.stop()
            held = time.monotonic() - started[0]
            with lock:
                audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
            if action == CANCEL or held < MIN_SECONDS:
                log("cancelled (shortcut or too short)")
                pill.show_state.emit("Cancelled")
                return
            pill.show_state.emit("Transcribing…")
            threading.Thread(target=finish, args=(audio,), daemon=True).start()

    hook = HotkeyHook(on_action)
    hook.start()
    hook.ready.wait(5)
    if hook.error or not hook.ready.is_set():
        sys.exit(f"could not watch the keyboard: {hook.error or 'hook did not start'}")
    # Qt's event loop swallows Ctrl+C in the console; restore the default so it quits
    import signal

    signal.signal(signal.SIGINT, signal.SIG_DFL)
    sys.exit(app.exec())


def main(argv=None):
    args = parse(sys.argv[1:] if argv is None else argv)
    if args.command == "bench":
        from . import bench

        bench.main(args.models_dir, args.threads, args.out, args.details)
    else:
        run(args)


if __name__ == "__main__":
    main()
