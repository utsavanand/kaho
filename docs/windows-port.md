# Kaho on Windows: port assessment and plan

Written October 2026, measured against `kaho.py` on `main` after 2.4.0 and
the screen-vocabulary feature (#6). Research was done on October 4 from
primary sources (llama.cpp source and PRs, Hugging Face model cards, Microsoft
Learn, competitor repositories). Anything not checked against a primary
source is marked **unverified**.

## Summary

Porting Kaho is not blocked by its Apple-only speech stack. Qwen3-ASR, the
model Kaho uses, has first-party support in llama.cpp and a packaged build in
sherpa-onnx, and both run on Windows. The cost is elsewhere:

- **Most of the Mac app is UI and macOS glue.** About half the code needs
  rewriting: the menu bar UI, overlay, settings and history windows, the
  hotkey, paste and clipboard handling. The other half (transcript handling,
  the dictionary, rewrite prompts, the job pipeline, settings, history) ports
  as it is.
- **Two components have to be written, not adopted.** A hotkey layer that
  can detect a modifier key held on its own, and a paste that confirms the
  text arrived. The most mature cross-platform competitor (Handy) wrote both
  itself after the libraries fell short.
- **Latency decides the engine.** Qwen3-ASR through llama.cpp was reported
  at 2.6 s (0.6B) and 3.1 s (1.7B) for a 5 s clip on an M3 MacBook Air.
  Kaho promises under 1.5 s. Parakeet TDT 0.6B is reported at 250 to 400 ms.
  Nobody has published warm Windows latency for a 2 to 5 s utterance, so
  that has to be measured first.

## 1. How much of the code is Mac-specific

Two measures, because they answer different questions:

| Measure | Result |
|---|---|
| Code lines that call a macOS API directly (AppKit, Quartz, ApplicationServices, MLX, Carbon, CoreFoundation, `NS*`, `CG*`, `AX*`, `pbcopy`, `/Applications`, `xattr`) | 224 of 2,670 non-comment lines (8.4%) |
| Lines inside functions and classes that would need rewriting, by role (measured at 2.4.0, 2,755 lines in top-level definitions) | UI chrome 876, platform glue 401, inference 194: about 53% |
| Ports unchanged | about 1,284 lines (47%) |

The first number shows the macOS calls are concentrated. The second is the
honest cost: a function that makes one AppKit call still has to be rewritten
around a Windows equivalent. All seven classes subclass `AppKit.NSObject` or
`NSView`.

Features added since that measurement add to the Mac-only surface: the
single-instance lock (portable), Move to Applications (Mac only, no Windows
equivalent needed), the Accessibility permission flow (Mac only; Windows has
no consent prompt), and reading on-screen words through Accessibility (#6;
Windows equivalent is UI Automation).

**What helps:**

- `transcribe()` is one `asr.generate(audio, hotwords, language)` call, so
  the inference surface is small.
- API keys are behind `get_api_key` and `set_api_key`; Windows uses DPAPI or
  Credential Manager.
- Audio uses `sounddevice` (PortAudio), which already runs on Windows. Only
  `pick_input_device`, which matches device names like "MacBook", and the
  CoreAudio deadlock workarounds are Mac-specific.
- The test suite stubs the platform modules in `sys.modules` and already
  runs on `ubuntu-latest` in CI. Adding `windows-latest` is incremental.

**What has to happen first:** `kaho.py` imports AppKit, Quartz,
ApplicationServices and `mlx_audio` at the top, and loads Carbon with
`ctypes.CDLL` at import time, so the module can't even be imported on
Windows. Moving these behind a platform module is the seam for the whole
port.

`/usr/share/dict/words` doesn't exist on Windows. `english_words()` copes
without it, but `respell()` gets weaker, so a word list (~2.5 MB) should be
bundled.

## 2. Speech engines

MLX is Apple-only. The candidates:

| Engine and model | 5 s utterance | Notes |
|---|---|---|
| Qwen3-ASR 0.6B, llama.cpp Q4_K_M | 2.6 s (M3 Air) | WER 3.06% |
| Qwen3-ASR 1.7B, llama.cpp Q8_0 | 3.1 s (M3 Air) | WER 2.84% |
| Qwen3-ASR 1.7B, FP16 | 29.4 s | never ship this |
| Parakeet TDT 0.6B v3 (ONNX) | ~250 to 400 ms | CC-BY-4.0, 25 languages, punctuation and casing |
| Moonshine v2 Tiny | ~100 to 200 ms | MIT, English only |
| Whisper base.en | ~0.9 to 1.5 s | pads every clip to 30 s, the wrong cost curve for push-to-talk |

The latency figures come from the October 4 research, not from runs on a
Windows machine. **Unverified on Windows.**

**llama.cpp** (Qwen3-ASR merged in PR #19441, 12 April 2026): needs the main
GGUF plus an `mmproj` audio encoder file. 1.7B Q4_K_M plus mmproj is 1.64 GB;
0.6B Q8_0 is about 1.16 GB. Windows binaries: Vulkan 33.3 MB, CPU 19.4 MB,
CUDA 153 MB plus a 423 MB CUDA runtime. Its `/v1/audio/transcriptions`
endpoint accepts `file`, `prompt`, `language`, `response_format`, `stream`,
`max_tokens` and `temperature`, and has **no hotword parameter** (confirmed
by reading the source). The dictionary could go in `prompt` as soft biasing,
which would need an eval. Output arrives as `language English<asr_text>…`
and needs one line of stripping (issue #26749).

**sherpa-onnx**: ships `sherpa-onnx-qwen3-asr-0.6B-int8` with a documented
`--qwen3-asr-hotwords` flag, so the dictionary keeps working. Native library
of 6 to 20 MB for x64, x86 and arm64, no Python runtime needed. Only 0.6B is
packaged.

**NPU**: no Qwen3-ASR build exists for QNN, OpenVINO or Ryzen AI, and
DirectML is officially legacy. Don't plan on the NPU.

## 3. Hotkey and paste

**`RegisterHotKey` can't detect a modifier held on its own.** It needs
modifier plus key and gives no key-up event. Hold-to-talk needs a
`WH_KEYBOARD_LL` low-level hook on its own thread with a message pump.
Risks: Windows silently removes a hook whose callback takes longer than
`LowLevelHooksTimeout` (~300 ms); antivirus and EDR products flag keyboard
hooks as keylogger behaviour; UIPI blocks injecting input into elevated
windows.

**Right Alt can't be the default.** On most non-US keyboard layouts it's
AltGr, used to type characters, and Microsoft advises against Ctrl+Alt
shortcuts for that reason. The Windows default should be Right Ctrl, with
Shift for instructions (either Shift, as on the Mac since #11). Competitors
use function keys (Parley uses F10).

**Paste needs confirmation.** Handy's Windows paste (`paste_tx/windows.rs`,
22 KB) publishes the text with delayed rendering: the clipboard holds a null
handle owned by a hidden message-only window, and `WM_RENDERFORMAT` arrives
only when an app actually reads it. That's a read receipt, which solves
"SendInput succeeded but nothing landed". Restore is guarded by
`GetClipboardSequenceNumber()`, the Windows equivalent of Kaho's
`changeCount` guard, and Chrome's opt-out clipboard formats keep transcripts
out of clipboard history.

## 4. UI

PySide6. The hard requirement is an overlay that never takes focus, or the
paste lands in the wrong window. On the Mac that's a non-activating panel;
on Win32 it's `WS_EX_NOACTIVATE | WS_EX_TRANSPARENT` with
`SW_SHOWNOACTIVATE`. In Qt, `Qt.WindowDoesNotAcceptFocus` is the flag that
matters; `Qt.WA_ShowWithoutActivating` does **not** set `WS_EX_NOACTIVATE`,
despite common belief. Prove this on day one.

The settings window is laid out with absolute coordinates, which moves to Qt
layouts; `NSTimer` becomes `QTimer`. Both are mechanical.

## 5. Hardware

**Not researched.** The install-base analysis never completed, so there is
no data here on how many Windows machines have enough RAM or a usable GPU.

One point from the code does hold: RAM, not GPU, is the constraint, and it's
smaller than it looks. Rewrite is off by default, so the baseline is the
speech model alone: about 0.94 GB at 0.6B, about 2.2 GB at 1.7B Q8_0.
Defaulting to 0.6B on low-end machines widens reach.

## 6. Packaging and signing

- **Avoid MSIX.** Its container model works against a global keyboard hook.
  Use Inno Setup or NSIS with a signed EXE, as shipping competitors do.
- **Azure Trusted Signing**, $9.99 a month on the Basic tier. Since April
  2026 individuals no longer need three years of business history, but
  individual certificates are **US and Canada only**. Check eligibility
  first.
- **PyInstaller and Defender false positives** are documented and common.
  Mitigations: latest bootloader, bootloader built from source, one-folder
  rather than one-file builds, signing, and submitting the build to
  Microsoft. An unsigned 400 MB one-folder bundle is close to the worst case
  for SmartScreen.
- **Unverified:** current OV and EV certificate prices, and whether EV still
  gives instant SmartScreen trust.

The macOS notarization holds of October 2026 came from PyInstaller's 45 MB
launcher with the code archive appended (fixed with `append_pkg=False`). A
large launcher may draw the same scrutiny from Defender, so the Windows build
should keep the launcher small from the start.

## 7. Competitors

The field has converged on a warm, resident speech engine, using CUDA on
NVIDIA and Vulkan elsewhere.

- **Handy**: Mac-first, ported to Windows in mid-2025. Ships a catalogue of
  69 models and defaults to Parakeet Unified EN 0.6B Q8_0; Whisper is now a
  minority. Wrote its own modifier-only hotkey crate (`handy-keys`) and the
  confirmed paste described above. Its CI silently installs the NSIS
  package, extracts the MSI, checks every staged DLL, and fails if the
  app-local Visual C++ runtime is missing.
- **Vibe**: Windows-first. Runs a pure-Rust Whisper engine in a separate
  `vibe-server.exe`. Most of its 94 Windows issues are that server process
  crashing (`0xC0000005`). It loads the Vulkan library at runtime, so a
  machine without Vulkan falls back to CPU instead of crashing. Kaho should
  do the same.
- **Parley**: whisper.cpp with Vulkan on AMD, faster-whisper with CUDA on
  NVIDIA, int8 on CPU. About 135 ms warm round trip with large-v3-turbo
  loaded. F10 hotkey. Unsigned, and its docs explain the SmartScreen warning.

Nobody ships Qwen3-ASR on Windows. The field moved to Parakeet and Whisper,
which is consistent with the latency concern in section 2.

## 8. Proposed plan

**Week 1: measure before porting.** On a real mid-range Windows laptop:

1. Warm latency for 2 to 5 s utterances: Qwen3-ASR 0.6B (sherpa-onnx) against
   Parakeet TDT v3 (ONNX). This picks the engine, and may decide whether the
   port keeps Kaho's 1.5 s promise.
2. A PySide6 overlay that provably never takes focus.
3. A `WH_KEYBOARD_LL` hold on Right Ctrl, including key-up, with a slow
   callback to check the hook timeout.

**Then the port, in this order:**

1. Move the macOS imports behind a platform module and get the test suite
   green on `windows-latest`. No Windows features yet.
2. Speech engine chosen in week 1, loaded as a warm sidecar process. Load the
   GPU backend at runtime with a CPU fallback, never as a hard link.
   sherpa-onnx if Qwen3-ASR is fast enough (keeps the dictionary), otherwise
   Parakeet through onnx-asr (numpy plus onnxruntime, no PyTorch).
3. Rewrite off in v1, as on the Mac by default. Dictation only.
4. PySide6 tray, overlay and settings.
5. Keyboard hook on its own thread, Right Ctrl by default, either Shift for
   instructions.
6. Clipboard paste with a read receipt, restore guarded by the clipboard
   sequence number.
7. Inno Setup or NSIS installer, signed with Azure Trusted Signing if
   eligible, and a CI job that installs the package and checks its DLLs.

**Not in v1:** cloud speech recognition. Kaho has no cloud ASR path today
(cloud is rewrite-only), so it would be new code, and it would break the
promise that audio never leaves the machine.

**Decisions needed before week 1:**

- Who owns the port, and on which Windows machine the week-1 measurements
  run.
- Whether keeping Qwen3-ASR matters more than keeping the 1.5 s latency
  promise, if week 1 shows they conflict.
- Azure Trusted Signing eligibility (US or Canada individual, or a company).
