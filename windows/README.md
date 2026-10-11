# Kaho for Windows: milestone 1 spike

Measure before porting (see the Windows port assessment, PR #14). This folder is
self-contained: `kaho.py` is not imported or refactored yet.

## What's here

| Path | What it is |
|---|---|
| `kaho_spike/engines.py` | Three speech engines through sherpa-onnx (one native package, no PyTorch): **Qwen3-ASR 0.6B int8** (the Mac's model family, with dictionary hotwords), **Parakeet TDT 0.6B v3 int8**, and **Moonshine v2 tiny** (English only, a speed reference). Models download on first use. |
| `kaho_spike/bench.py` | Warm latency (best of 3, after a warm-up), WER, dictionary terms spelled right, and wrong-script output, on the committed clips. |
| `kaho_spike/hotkey.py` | Right Ctrl hold-to-talk: a pure state machine (`step`), plus a `WH_KEYBOARD_LL` hook on its own thread that only enqueues events, because Windows drops a hook whose callback runs past ~300 ms. sherpa-onnx releases the GIL while decoding (measured: other threads stall at most 7 ms), so transcription can't starve the hook. |
| `kaho_spike/clipboard.py` | Paste with Ctrl+V via `SendInput`, then restore the previous clipboard only if `GetClipboardSequenceNumber` hasn't moved. Transcripts are flagged to stay out of clipboard history (Win+V). |
| `kaho_spike/overlay.py` | The pill: PySide6, `WindowDoesNotAcceptFocus` plus `WS_EX_NOACTIVATE`, so it never steals the paste target's focus. |
| `bench/clips/` | 10 LibriSpeech test-clean utterances (6 × 2–5 s, 4 × 8–12.5 s) and 8 synthetic jargon dictations (Qwen3-TTS), 1.8 MB. See `bench/clips/ATTRIBUTION.md`. |
| `tests/` | Pure-logic tests; run anywhere with `python -m unittest discover -s windows/tests -t windows`. |

CI (`.github/workflows/windows-spike.yml`) runs on `windows-latest`. It installs the
hook, runs the benchmark (the table is in the job summary), builds `kaho-spike.exe`
with PyInstaller, reruns the benchmark from the packaged exe, and uploads the
artifact **kaho-spike-windows-x64**.

## Testing on a Windows PC

1. Open the latest **Windows spike** run under the repo's Actions tab and download the
   **kaho-spike-windows-x64** artifact. Unzip it anywhere.
2. Run `kaho-spike.exe`. SmartScreen warns because the build is unsigned: **More info →
   Run anyway**. The first run downloads the Qwen3-ASR model (about 840 MB).
3. Click into Notepad, **hold Right Ctrl, talk, release**. The text should appear.
   Also check that Right Ctrl + C does not paste, that your previous clipboard comes
   back, and that clicking the pill doesn't take focus from Notepad.
   `kaho-spike.exe --engine parakeet` tries the faster engine.
4. Run `kaho-spike.exe bench` in a Command Prompt in that folder and send back the
   table it prints.

`README.txt` inside the artifact has the same steps for a non-developer.

## Known limits

Unsigned (SmartScreen warns); CPU only; no tray icon, settings, rewrite or history
yet; apps running as administrator refuse the paste (UIPI). Moonshine is
English-only. The jargon clips are synthetic speech, so treat their numbers as
relative, not absolute.
