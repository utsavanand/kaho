#!/usr/bin/env python3
"""Measure how long a dictation takes, stage by stage.

Run it before and after a change to see what moved:

    .venv/bin/python tools/benchmark.py                  # measure
    .venv/bin/python tools/benchmark.py --save baseline  # record a baseline
    .venv/bin/python tools/benchmark.py --against baseline

The clips are generated with `say`, so a run is reproducible on any Mac and
two runs are comparable. That is the point: the log tells you a dictation
took 2.9 s but not whether that was the audio, the model, or a cold cache.

Reported per stage, in the order a real dictation hits them:

  transcribe   Whisper. Scales with clip length, in 30 s windows.
  clean-check  One forward pass asking "already clean?" (1.7.11).
  rewrite      Generation, only when the check says it is needed.

Cold numbers are reported separately from warm ones. The first inference
after launch pays model load and Metal kernel compilation, and mixing it
into an average hides both.
"""

import argparse
import json
import pathlib
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASELINE_DIR = ROOT / "build-bench"

# Short, medium and long: Whisper pads to 30 s windows, so cost is a step
# function of length, not a line. One clip per side of the first step.
CLIPS = {
    "short": "Move the launch to Thursday.",
    "medium": (
        "So I think we should move the launch to next Thursday, because QA is "
        "still working through the payment bugs and I don't want to ship on top "
        "of those. Let me know if that breaks anything on your side."
    ),
    "long": (
        "Okay so there are a couple of different things I want to cover here. "
        "The first one is the training data and the APIs around it, which I "
        "think is its own feature and should be scoped separately. The second "
        "is registering the model and the harness and the corresponding evals, "
        "and that is really the other main piece of work. Then there is "
        "promotion and rollout on top of that. So I think we need to divide "
        "this into core features and then decide which of them are horizontals "
        "and which are verticals, because right now it is all one undifferentiated "
        "lump and nobody can estimate it."
    ),
}


def make_clip(text, path):
    """Render text to 16 kHz mono float32, the shape the app records."""
    aiff = path.with_suffix(".aiff")
    subprocess.run(["say", "-r", "170", "-o", str(aiff), text], check=True)
    subprocess.run(
        # 16-bit PCM, not float32: Python's wave module rejects format 3.
        # Converted to float32 on load, which is what the recorder produces.
        ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
         str(aiff), str(path)],
        check=True, capture_output=True,
    )
    aiff.unlink()


def load_audio(path):
    import wave

    import numpy as np
    with wave.open(str(path)) as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def timed(fn):
    t0 = time.monotonic()
    result = fn()
    return result, time.monotonic() - t0


def measure(repeats):
    import kaho

    kaho.model_path = kaho.huggingface_hub.snapshot_download(
        kaho.MODEL_REPO, revision=kaho.MODEL_REVISION
    )
    results = {}
    with tempfile.TemporaryDirectory() as tmp:
        clips = {}
        for name, text in CLIPS.items():
            path = pathlib.Path(tmp) / f"{name}.wav"
            make_clip(text, path)
            clips[name] = load_audio(path)

        for name, audio in clips.items():
            seconds = len(audio) / kaho.SAMPLE_RATE
            runs = []
            for _ in range(repeats + 1):  # +1: the first is the cold one
                text, elapsed = timed(lambda clip=audio: kaho.transcribe(clip))
                runs.append(elapsed)
            results[f"transcribe/{name}"] = {
                "audio_seconds": round(seconds, 2),
                "cold": round(runs[0], 3),
                "warm": [round(r, 3) for r in runs[1:]],
                "text": text,
            }
            print(f"  transcribe/{name:<7} {seconds:5.1f}s audio  "
                  f"cold {runs[0]:.2f}s  warm {statistics.median(runs[1:]):.2f}s")

        kaho.ensure_rewriter()
        while kaho.rewriter is None and kaho.rewriter_thread.is_alive():
            time.sleep(0.5)
        if kaho.rewriter is None:
            print("  (rewrite model unavailable — skipping rewrite stages)")
            return results

        messy = "So um I think we should uh move the launch to Thursday you know"
        for stage, fn in (
            ("clean-check", lambda: kaho.clean_as_is_probability(messy)),
            ("rewrite/clean", lambda: kaho.rewrite(messy, "clean")),
            ("rewrite/structured", lambda: kaho.rewrite(CLIPS["long"], "structured")),
        ):
            runs = [timed(fn)[1] for _ in range(repeats + 1)]
            results[stage] = {
                "cold": round(runs[0], 3),
                "warm": [round(r, 3) for r in runs[1:]],
            }
            print(f"  {stage:<19} cold {runs[0]:.2f}s  "
                  f"warm {statistics.median(runs[1:]):.2f}s")
    return results


def warm_median(entry):
    return statistics.median(entry["warm"])


def compare(current, baseline):
    print("\nversus baseline (warm median):")
    regressed = False
    for key, now in current.items():
        was = baseline.get(key)
        if not was:
            print(f"  {key:<19} {warm_median(now):.2f}s  (new)")
            continue
        a, b = warm_median(was), warm_median(now)
        delta = (b - a) / a * 100 if a else 0
        flag = ""
        # 10% is above this benchmark's own run-to-run spread
        if delta > 10:
            flag, regressed = "  ← SLOWER", True
        elif delta < -10:
            flag = "  ← faster"
        print(f"  {key:<19} {a:.2f}s → {b:.2f}s  ({delta:+.0f}%){flag}")
    return regressed


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repeats", type=int, default=3,
                    help="warm runs per stage (default 3)")
    ap.add_argument("--save", metavar="NAME", help="write results as a baseline")
    ap.add_argument("--against", metavar="NAME", help="compare against a baseline")
    args = ap.parse_args()

    import kaho
    print(f"Kaho {kaho.APP_VERSION} — {args.repeats} warm runs per stage\n")
    results = measure(args.repeats)

    regressed = False
    if args.against:
        path = BASELINE_DIR / f"{args.against}.json"
        if not path.exists():
            sys.exit(f"no baseline at {path}")
        regressed = compare(results, json.loads(path.read_text())["results"])

    if args.save:
        BASELINE_DIR.mkdir(exist_ok=True)
        path = BASELINE_DIR / f"{args.save}.json"
        path.write_text(json.dumps(
            {"version": kaho.APP_VERSION, "results": results}, indent=2) + "\n")
        print(f"\nsaved to {path}")

    return 1 if regressed else 0


if __name__ == "__main__":
    sys.exit(main())
