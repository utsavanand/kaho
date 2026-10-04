# kaho — design

A local clone of Wispr Flow's core loop: hold a key anywhere on macOS, speak,
release, and the transcribed text is inserted into whatever app has focus.
Transcription runs entirely on-device.

Revised after design review. Material changes from v1: transcription moved off
the key-listener callback onto a worker thread (blocking the callback stalls
the macOS event tap, delays keystrokes system-wide, and can silently kill the
listener); startup warmup inference added; clipboard save/restore dropped
(reinstated in 1.7.8, see Post-v1 revisions); `no_speech_prob` filter cut;
secure-input and TCC failure modes documented.

**Goals, Stack and Flow below describe the code as it stands**; Post-v1
revisions records how it got there, and CHANGELOG.md has the per-release
detail.

## Goals

- Hold-to-talk dictation that works in any app (editor, browser, terminal, Slack)
- Fully local: audio never leaves the machine, works offline
- Fast enough to feel like typing: < 1.5 s **end-to-end** (key-release to text
  visible) in steady state on this machine (M4 Max). Transcription cost grows
  with the length of the dictation (about 0.2 s for 5 s of speech, 0.5 s for
  14 s); Whisper, before 2.2.0, padded every input to a 30 s window, so short
  and long cost nearly the same. The first inference after startup is much
  slower (Metal warmup), which is why startup runs a throwaway transcribe on a
  zero buffer.

## Non-goals (v1)

- No UI, menu bar icon, or settings screen — constants at the top of one file
  *(superseded: v1.1 added the menu bar item and windows; v1.4 added
  menu-based settings — hotkey choice and rewrite mode — persisted to
  settings.json)*
- No streaming/live transcription (transcribe once on key release)
- No Wispr-style tone rewriting, dictionary, or per-app formatting
  *(partially superseded: v1.4 added optional rewrite, with three modes by
  v1.6 — filler removal, structured output, and LLM-ready compression — via a
  pinned Qwen3-4B-Instruct through mlx-lm, lazy-loaded, falling back to the
  raw transcript on any failure. Dictionary and per-app formatting remain
  out.)*
- No clipboard preservation: dictation overwrites the clipboard
  *(superseded: 1.7.8 restores it after a delay, guarded by `changeCount`.
  The three objections that ruled it out in v1 are all answered — the timing
  race by waiting `CLIPBOARD_RESTORE_SECONDS`, the clipboard-manager noise by
  declaring the transcript transient, and the non-text destruction by going
  through `NSPasteboard` for a string rather than round-tripping `pbpaste`.)*
- No auto-start at login (documented as a manual `launchd` step, not built)
- No Windows/Linux

## Stack

- Python 3.13, single process, one file (`kaho.py`) + `run.sh`
- **Model**: `mlx-community/Qwen3-ASR-1.7B-8bit` via `mlx-audio`, since
  2.2.0. It replaced `whisper-large-v3-turbo` (via `mlx-whisper`) after a
  benchmark on this machine: 0.12 s vs 0.51 s on a 1.4 s clip and 0.42 vs
  0.60 at 10 s, but slower past ~15-20 s (1.06 vs 0.78 at 30 s); 1.3% vs
  1.5% WER on LibriSpeech clean, 3.4% vs 4.4% on jargon, both with the
  dictionary. Parakeet v3 was faster still but takes no vocabulary, so it was
  out. Dictionary terms go in as `hotwords`; the model still misspells some
  (it wrote "Soto" for the old name, Sotto), so `respell()` corrects close misses afterwards. Weights ~2.3 GB,
  about 1.1 GB more resident than Whisper. Model downloads from Hugging Face
  on first run, then cached in `~/.cache/huggingface`. Inference itself is
  offline.
- **Audio capture**: `sounddevice` (bundles PortAudio), 16 kHz mono float32 —
  the model's native input format, no resampling or ffmpeg needed
- **Hotkey**: `NSEvent` global + local monitors for `flagsChanged`, installed
  on the main run loop (`install_hotkey_monitors`). Deliberately not a
  `CGEventTap`, which would additionally require the Input Monitoring grant,
  and no longer `pynput`, whose key handling calls TIS APIs off the main
  thread — macOS 15 kills that with `EXC_BREAKPOINT` (both switches are in
  Post-v1 revisions). Each hotkey is matched on its raw keycode *plus* the
  device-specific `NX_DEVICE*KEYMASK` bit: the aggregate
  `NSEventModifierFlagOption` stays set while LEFT Option is held, which made
  a right-Option release look like a press and left recording stuck on.
  Default key: hold **right Option**. Caveat: right Option is a dead-key
  modifier (composes ø, ∆, …), so it's only conflict-free when held *alone* —
  and Cmd+V must not be synthesized while it's still physically down, or apps
  receive Cmd+Opt+V ("Paste and Match Style" or nothing). The worker-thread
  structure guarantees the paste happens after release.
- **Text insertion**: set the clipboard with `pbcopy`, then post a synthetic
  Cmd+V with `Quartz.CGEventPost`. Pasting is instant regardless of length;
  per-character synthetic typing is 10-100× slower and drops characters in
  some apps.

## Flow

```
right-Option down ──▶ ignore unless state is "ready"
                      else claim the recording, queue "open stream" on the
                      audio thread, show the overlay pill, and start a
                      background read of the focused window's names (≤150 ms)
right-Option up   ──▶ held past TAP_MAX_SECONDS (0.45 s)? queue "stop stream"
                      a tap? defer that stop by one double-tap window
                      second tap inside DOUBLE_TAP_SECONDS (0.9 s)? lock
                      hands-free — the next tap past the grace period stops it
stop              ──▶ under 0.3 s, or peak below the speech floor? drop it
                      else put the audio ndarray on a Queue and return
worker thread     ──▶ loops on Queue.get(): transcribe (dictionary + any
                      screen words that arrived as hotwords) ▸ optional
                      rewrite ▸ pbcopy ▸ Cmd+V ▸ history
                      logs one line per event (text, timing, or why dropped)
```

- The monitor callbacks only flip state and enqueue — they return in
  microseconds. All slow work (transcription, paste) lives on one
  `threading.Thread(daemon=True)` with a `queue.Queue`. Nothing larger: no
  pool, no executor, no framework.
- Model is loaded once at startup and stays resident; startup then runs a
  warmup `transcribe()` on 1 s of zeros so the first real dictation doesn't
  pay Metal kernel compilation
- The mic stream is opened per-hold, not kept open, so the mic indicator dot
  only shows while the key is held. Cost: stream open takes ~100-200 ms, so
  speech in the first instant after keydown can clip — hold, breathe, speak.
- A second hold during a long transcription queues behind it on the Queue
- Every PortAudio call is queued onto one dedicated audio thread, so start
  and stop are asynchronous and the live stream changes hands between that
  thread and the run loop — see Serialized audio thread below

## macOS permissions (manual, one-time)

Kaho.app needs exactly two grants (Input Monitoring stopped being required
in 1.1.1, when the CGEventTap was replaced with NSEvent monitors):

1. **Microphone** — prompted automatically on first recording
2. **Accessibility** — required to observe the global hotkey and post the
   synthetic Cmd+V

The TCC grant is bound to the binary's identity: rebuilding via install.sh,
upgrading Homebrew Python, or switching between the source build and the
notarized bundle silently invalidates it, and the symptom is "runs but sees
no keys" or "transcribes but never pastes." Startup checks
`CGPreflightPostEventAccess()` and shows a pointer to System Settings; since
1.7.9 the same check runs before every paste, so a grant revoked mid-session
surfaces on the pill instead of failing silently. When running from the repo
with run.sh, the grants attach to the launching terminal instead of Kaho.

## Failure modes

- **Secure input**: password fields, `sudo` prompts, and Keychain dialogs
  enable secure event input, which blocks event taps process-wide — both the
  hotkey and the synthetic paste stop working, by OS design. Since 1.7.9 the
  paste path checks for it (`IsSecureEventInputEnabled` through Carbon, plus
  a live Accessibility preflight) and names the holding process in the log
  rather than failing silently; the transcript is left on the clipboard and
  the pill says "Not pasted — ⌘V". A stuck secure-input session (usually a
  terminal with Secure Keyboard Entry) is still the first suspect when the
  hotkey itself stops responding.
- Hugging Face unreachable on first run → the model download raises; retry when
  online (one-time download)
- No microphone permission or device held exclusively by another app →
  stream open raises per-hold; caught and printed with the reason, process
  keeps running
- The speech model hallucinating on silence (observed with Whisper, the
  classic "thank you for watching"; the floors were kept for Qwen3-ASR) →
  two floors, both set from observed failures rather than guessed in advance,
  which is why v1 cut its `no_speech_prob` threshold. `MIN_SECONDS` drops
  accidental taps and `MIN_PEAK` drops audio too quiet to be speech (0.025:
  real dictation peaks at 0.05+, the hallucinations that reached users all
  sat under 0.02). `looks_hallucinated()` then catches the repetition loops
  that clear both — one word emitted hundreds of times — before they reach
  the clipboard.
- Every event prints one console line, so "nothing happened" is always
  distinguishable from "hotkey not firing"

## Execution plan

The original v1 plan, kept as the record of what was verified before the app
existed. For today's setup see [CONTRIBUTING.md](CONTRIBUTING.md).

1. Scaffold the repo: `kaho.py`, `run.sh`, `requirements.txt`, `README.md`
2. `python3 -m venv .venv` and `pip install -r requirements.txt`
3. Verify the model end-to-end without a mic: generate a spoken wav with
   macOS `say`, load it, run it through the same `transcribe()` call the app
   uses, and check the text matches the input phrase
4. Run the app, confirm it starts, loads the model, warms up, and registers
   the listener (mic + paste need the Accessibility grant, so hold-to-talk is
   a manual user test)
5. Manual test must include: dictate once, then immediately dictate again —
   verifies the hotkey survives a completed transcription (catches the
   event-tap-death regression if the threading structure is ever undone)

## Post-v1 revisions

### Mic device selection (bug fix)

First real-world failure: with AirPods connected they are the default input,
and Bluetooth mics switch A2DP→HFP when recording starts — the switch takes
~1 s (start of speech lost) and HFP audio is narrowband, so transcriptions
came out wrong or empty. Fix: prefer the built-in microphone (device name
containing "MacBook" or "Built-in") over the system default, and log each
dictation's device, duration, and peak level so audio-path failures are
visible in the log instead of manifesting as mystery transcripts. A peak of
exactly ~0 additionally means macOS delivered no signal (mic permission), and
is reported as such instead of being transcribed into a hallucination.

### Packaging as Kaho.app

Users install by cloning the repo and running `install.sh`, which builds the
.app locally: venv in `~/Library/Application Support/Kaho`, a hand-rolled
bundle (Info.plist + zsh launcher that execs the venv python) in
`/Applications`, ad-hoc codesigned. Chosen over the alternatives because:

- Building locally means no quarantine attribute → no Gatekeeper block → no
  $99/yr notarization needed
- TCC prompts attribute to "Kaho" (the bundle), not the user's terminal —
  which also removes the v1 gotcha of grants dying with the terminal binding
- py2app/PyInstaller bundling of MLX + model was rejected: multi-GB artifact,
  fragile, and still unsigned
- A native Swift rewrite (WhisperKit) is the "real product" path but 10× the
  code for the same v1 behavior

`LSUIElement` makes it a menu-bar-only app (no Dock icon). The status item is
built directly on `NSStatusBar` — rumps provided it until 1.1.0, where its
item turned out to be invisible when the app was launched from a bundle — and
a 0.3 s `NSTimer` polls the state variable, because AppKit UI must only be
touched from the main thread. All logs go to `~/Library/Logs/Kaho.log` as
well as stdout, since a double-clicked app has no terminal.

### Hotkey via NSEvent monitors (v1.1.1)

The CGEventTap was replaced with NSEvent global+local monitors for
flagsChanged. Same capability for a single modifier hotkey, but taps are gated
on the Input Monitoring permission while NSEvent monitors need only
Accessibility — one less grant for users (and how commercial dictation apps
avoid the Input Monitoring prompt). Caveat: with Accessibility missing the
global monitor silently never fires, so the startup preflight check is the
only signal.

### Serialized audio thread (v1.7.5)

CoreAudio's open/stop can block indefinitely on a HAL mutex held by another
audio client — observed as a full main-thread deadlock with Wispr Flow running
— so every PortAudio call is queued onto one dedicated thread (`audio_control`).
The hotkey and the UI stay alive whatever the audio stack does, and serializing
the ops means a wedged device pins that one thread instead of leaking a new one
per recording. `audio_wedged()` reports an op stuck over 5 s, and the overlay
arms a watchdog so a pill can never outlive a pipeline that never reports back.

Because start and stop are now asynchronous, ownership of the live stream moves
between the run loop and the audio thread. `recording_lock` guards that handoff:
without it a key release could land between `_open_stream`'s "is this recording
still current?" check and its publish of the stream, leaving a stream nobody
closed and the microphone live.

### Hands-free double-tap (v1.7.4 – v1.7.6)

A double-tap locks recording until the next tap. Three constants carry it, each
paid for by a bug: a press under `TAP_MAX_SECONDS` (0.45 s) counts as a tap; two
taps within `DOUBLE_TAP_SECONDS` make a pair (0.9 s — 0.5 s was tighter than a
natural double-tap and real attempts silently missed); and `LOCK_GRACE_SECONDS`
ignores the tail of the locking double-tap, which otherwise cancelled the lock
it had just engaged.

The first tap's stop is *deferred* by one double-tap window rather than executed.
Tearing the stream down and reopening it ~60 ms later handed back a stream that
captured silence — PortAudio had not finished releasing the device — and Whisper
hallucinated fluent paragraphs from that noise floor.

### Clipboard restore (v1.7.8)

The v1 non-goal above stood until 1.7.8: dictation overwrote the clipboard,
and losing whatever you had copied is a real cost paid on every dictation. It is now restored, and each of the original objections is met
head-on rather than waived.

The timing race — restore too early and a slow app pastes the *old* clipboard —
is handled by waiting `CLIPBOARD_RESTORE_SECONDS` (1.5 s) after the keystroke,
and by checking `changeCount` before writing: a clipboard the user changed in
the meantime is left alone, so the failure mode is "your transcript stays on
the clipboard", never "your copy is gone". The transcript is also declared with
`org.nspasteboard.TransientType`, the convention that asks clipboard managers
not to archive it — a dictated transcript is private more often than a normal
copy. Non-text content survives because the restore goes through
`NSPasteboard` for a string instead of round-tripping `pbpaste`.
