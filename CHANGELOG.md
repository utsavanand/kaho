# Changelog
## 1.7.9 — 2026-09-27

- A blocked paste now says so instead of silently doing nothing. When
  Accessibility has been revoked, or a password prompt / terminal with
  Secure Keyboard Entry is holding secure input, synthetic keystrokes go
  nowhere — transcription succeeded, the clipboard filled, and the app
  looked dead. The pill now shows an amber "Not pasted — ⌘V", the log
  names the process holding secure input, and the transcript stays on
  the clipboard (no restore) so one manual paste recovers it. Checked
  per paste, not just at startup.

## 1.7.8 — 2026-09-26

- Dictation no longer destroys your clipboard. Sotto saves what you had
  copied, pastes the transcript, and puts the original back — guarded by
  the pasteboard's changeCount so it never overwrites something you copied
  in the meantime. Transcripts are also marked transient so clipboard
  managers stop archiving them.
- The app bundle drops from 904 MB to 477 MB. mlx-whisper declares torch as
  a dependency but only imports it from torch_whisper.py, a conversion
  module nothing loads. (numba and scipy look equally unused but are not —
  transcribe.py imports timing.py at module load, and excluding them kills
  the app at startup.)

## 1.7.7 — 2026-09-26

- Raised the silence floor from 0.012 to 0.025. A clip at peak 0.013 slipped
  through and Whisper emitted "videos" four hundred times. Every genuine
  dictation observed peaks at 0.05+; every hallucination under 0.02.
- Added a repetition-loop guard. Even above the floor Whisper sometimes
  loops one word, and pasting that into an editor is worse than pasting
  nothing, so a transcript whose unique-word ratio collapses is dropped.

## 1.7.6 — 2026-09-26

- The Dock fallback now actually appears when the menu bar icon is hidden.
  A buried status item reports two layer-25 windows — a phantom claiming to
  be onscreen and the real hidden one — and the check returned on the first
  match, so it concluded "visible" and skipped the fallback. On a notched
  Mac with a full menu bar that left no way into the app at all: no visible
  icon, no Dock icon, no menu. It now requires every status window to be
  onscreen, and treats an inconclusive answer as hidden.

## 1.7.5 — 2026-09-25

- Hands-free actually records your voice now. The lock was working all
  along; the audio was not. A double-tap fires start/stop/start within
  ~60 ms, and those queue onto one serialized audio thread, so the second
  open ran while PortAudio was still releasing the device and handed back
  a stream that captured silence. Whisper then hallucinated fluent text
  from the noise floor — the paragraphs of German and "little little
  little" came from there.
- The first tap's stop is now deferred: if a second tap follows inside the
  double-tap window, the stop is cancelled and the stream is never torn
  down, so recording continues straight into hands-free mode.

## 1.7.4 — 2026-09-25

- Hands-free double-tap actually works now. The state machine was right,
  but the timing was not: DOUBLE_TAP_SECONDS of 0.5 was tighter than a
  natural double-tap, and real attempts 0.6-0.8 s apart silently missed
  the pair. Widened to 0.9 s, with the tap window at 0.45 s.
- A tap within 0.6 s of locking no longer cancels it. The tail of an
  eager double-tap was stopping the recording it had just started, which
  is what made the feature look dead.
- Clips too quiet to be speech are dropped instead of transcribed.
  Whisper invented a paragraph of German from 0.6 s at peak 0.005; real
  dictation peaks at 0.03+, so the new floor sits well below genuine
  speech while catching a room recorded by accident.

## 1.7.3 — 2026-09-25

- New app icon: a waveform tapering from white to blue, left to right — a
  voice dropping to a whisper. Replaces the blushing speech-bubble face,
  which read as a toy rather than a tool.

## 1.7.2 — 2026-09-25

- Hands-free mode works again. last_tap was only assigned in a branch the
  double-tap path returned before reaching, so it stayed at its initial
  0.0 — and since time.monotonic() counts from boot, a *single* tap
  satisfied the "within 0.5 s of the last tap" test and locked recording.
  The real second tap then read as the stop tap, so the gesture looked
  dead. Every tap now records its timestamp, and a consumed pair resets.

## 1.7.1 — 2026-09-24

- assets/menu.svg shows Edit Dictionary…, and CI now fails when the
  illustration falls behind sotto.py. The README's images had drifted
  behind renamed modes and new menu items three times, and nothing in CI
  looked at them.

## 1.7.0 — 2026-09-24

- Custom dictionary: list proper nouns and jargon in
  `~/Library/Application Support/Sotto/dictionary.txt` (Edit Dictionary… in
  the menu or Settings) and Whisper is biased toward your spellings via
  initial_prompt. Measured on synthesized speech: "Soto" and "duct term"
  became "Sotto" and "Duckterm". Re-read per dictation, so edits need no
  relaunch; capped at 120 terms since Whisper's prompt window is 224 tokens.
- Long dictations no longer drift. Whisper fed each 30 s window's output
  forward as the next window's context, so one bad guess compounded through
  the rest of a long recording. That carryover is now off
  (condition_on_previous_text=False); the dictionary supplies cross-window
  consistency instead, without the feedback loop.

## 1.6.2 — 2026-09-24

- Caveman mode drops its bullets and line breaks, writing one line with
  "; " between asks. The markup was ~16% of the output's tokens (3 of 19
  on a two-ask dictation) in a mode whose whole purpose is spending fewer
  tokens. The freed budget goes to detail instead: "button overflows"
  now survives where the bulleted version dropped it.

## 1.6.1 — 2026-09-23

- The app menu says "Sotto", not "Python". macOS titles it from the running
  executable's bundle — Homebrew's Python.app — so CFBundleName is now
  overridden before AppKit builds its menus.

## 1.6.0 — 2026-09-23

- "Bullet points" is now "Structured": instead of forcing every dictation
  into bullets, it picks the shape the content calls for — prose for a single
  thought, bullets for parallel items, numbered steps for a sequence, and a
  lead-in line above a list when you framed one. Your wording is kept; it
  tidies grammar and stutters rather than rewriting in its own voice.
- Existing settings migrate automatically (bullets -> structured).

## 1.5.1 — 2026-09-23

- Fixed the overlay freezing on "Pasted" and then swallowing the next
  recording's animation. Cause was the notch warning: NSAlert.runModal()
  spins a nested run loop that starves every NSTimer in the process, so an
  alert sitting unnoticed behind other windows froze the pill mid-cycle.
  That warning is now silent — the Dock icon appears and the Settings
  window explains it, instead of a modal that fired on every launch.
- The Dock icon is Sotto's, not the Python rocket: a process running out of
  Homebrew's Python.app never consults our bundle's .icns, so it is now set
  explicitly at runtime.
- Retained the "Pasted" hide timer, which an unreferenced NSTimer could
  otherwise have been collected before firing.

## 1.5.0 — 2026-09-23

- Settings window (⌘,) for hotkey and rewrite mode. The menu bar item still
  carries both, but macOS hides the status icon behind the notch on a full
  menu bar — which made every setting unreachable on affected machines.
- When the icon is hidden, Sotto now also promotes itself to a Dock app with
  a real app menu, so there is always a way in.
- New "Caveman" rewrite mode: compresses dictation for pasting into an AI
  assistant, keeping every instruction, constraint, name, and number while
  cutting hedging and filler (~⅓ the original length).
- Bullet points no longer splits a conditional across two bullets — "do X,
  but only if Y" stayed one bullet, instead of reading as an unconditional
  task plus a stray fragment.

## 1.4.2 — 2026-09-23

- The progress pill can no longer get stuck on screen. When CoreAudio
  deadlocks (another audio app holding the HAL mutex), the audio thread
  blocks inside PortAudio's stop and the transcribe step never runs, so
  nothing took the overlay down. A 90 s watchdog now hides it regardless.

## 1.4.1 — 2026-09-23

- The recording pill no longer vanishes on key release: it stays up through
  "Transcribing…" and "Rewriting…" (animated dots), then flashes "Pasted"
  before hiding, so multi-second work is visible instead of looking idle.
  Dropped recordings (too short, silent, no audio) hide it immediately.
- Elapsed timer on the pill while recording, turning amber past 60 s — a
  nudge on long holds, not a hard stop

## 1.4.0 — 2026-09-21

- Hotkey is now chosen from the menu bar (🎙 → Hotkey): right Option
  (default), right Command, right Control, or right Shift; persisted in
  `~/Library/Application Support/Sotto/settings.json` (0600)
- Optional on-device rewrite before pasting (🎙 → Rewrite): *Clean up* strips
  filler words, false starts, and repeats and fixes punctuation; *Bullet
  points* turns a dictated ramble into a list. Runs
  Qwen3-4B-Instruct-2507-4bit via mlx-lm (pinned revision, ~2.3 GB downloaded
  on first enable, ~0.5 s per rewrite); a failed or not-yet-loaded rewrite
  falls back to pasting the raw transcript
- New app icon: proper macOS squircle with standard margins (the old one
  filled the full square), bolder glyph
- Alerts, logs, and the history placeholder name the configured hotkey
  instead of hardcoding "right Option"
- Report a Bug… menu item: opens a Mail draft addressed to the maintainer
  with version/mic/settings diagnostics in the body and Sotto.log attached
  (mailto: fallback without attachment when no Mail account is configured);
  the user reviews the draft — and the privacy warning about transcripts in
  the log — before anything is sent

## 1.3.5 — 2026-09-01

- install.sh recreates the venv when it was built by a pre-3.13 Python, so an
  upgrade can't pair old wheels with the 3.13 hash lock
- Existing log/history files are chmodded 0600 at startup (the private opener
  only covered newly created files)
- CI runs static checks on Python 3.13 (matching production) and adds an
  arm64 macOS job that dry-run resolves the hashed lock

## 1.3.4 — 2026-09-01

Security and hardening release.

- Supply chain: dependencies install from requirements.lock — every package
  pinned to an exact version with a sha256 hash (--require-hashes); the
  Whisper model is pinned to an immutable Hugging Face revision instead of a
  mutable repo reference. Requires Python 3.13 (the lock pins 3.13 wheels).
- log() is best-effort and can no longer throw from inside the exception
  handlers that keep the workers alive (full disk, broken pipe)
- Log and history files are created 0600 — transcripts stay private even if
  parent directory permissions loosen
- CI actions pinned by commit SHA, ruff pinned to an exact version

## 1.3.3 — 2026-09-01

- Audio operations are serialized through one dedicated thread: a wedged
  CoreAudio device now pins at most one thread instead of leaking one per
  recording, and new recordings are refused with a clear log line while the
  device is unresponsive (>5 s)
- A failed stream stop() no longer skips close(), which could keep the
  microphone busy and break every later recording

## 1.3.2 — 2026-09-01

Reliability release: fixes a main-thread deadlock and addresses a code review.

- Fixed: CoreAudio's stop call could block forever on a HAL mutex held by
  another audio client (observed with Wispr Flow running), freezing the
  hotkey, menu bar, and overlay. All PortAudio open/stop calls now run on
  background threads; the main thread can no longer be taken hostage.
- Fixed: holding left Option masked a right-Option release (aggregate
  modifier flag), leaving recording stuck on — now uses the device-specific
  right-Option bit
- A transcription error no longer kills the worker thread silently
- A failed startup (network, device, model cache) now shows an error alert
  and ⚠️ in the menu bar instead of hanging at "…" forever
- A damaged history line no longer prevents launch; bad lines are skipped
- install.sh stages the new bundle before replacing the old one, so a failed
  build can't destroy a working install
- Dependencies pinned to tested version ranges

## 1.3.1 — 2026-09-01

- Recording pill redesign: frosted-glass HUD background, finer 24-bar
  waveform, pulsing record dot
- Real noise gate: the waveform is a flat dotted line until the mic level
  clears an absolute margin above the rolling noise floor — ambient noise no
  longer animates the bars (min/max normalization was amplifying
  silence-level jitter)

## 1.3.0 — 2026-09-01

- Hands-free mode: double-tap right Option to lock recording on, tap once to
  stop and paste
- Recording pill is smaller and calmer: levels are normalized against a
  rolling ambient-noise floor with fast-attack/slow-decay smoothing, so the
  bars sit flat in a quiet room and move on speech
- Launching Sotto while it's already running opens the History window —
  reachable even when the menu bar icon is hidden behind the notch
- README: release badge and an architecture diagram

## 1.2.0 — 2026-09-01

- On-screen recording indicator: a floating pill at the bottom of the screen
  with a live mic level animation while the hotkey is held — visible over
  fullscreen apps, so recording state no longer depends on the menu bar icon
- History window: transcripts persist to
  ~/Library/Application Support/Sotto/history.jsonl and 🎙 > History… opens a
  scrollable window with every transcription; the menu still shows the last
  10 with click-to-copy, now surviving restarts

## 1.1.1 — 2026-09-01

- Input Monitoring is no longer required: the hotkey is observed with NSEvent
  global monitors (Accessibility only) instead of a CGEventTap. Sotto now
  needs the same two grants as Wispr Flow: Microphone and Accessibility.

## 1.1.0 — 2026-09-01

- Permission popups on launch: missing Input Monitoring / Accessibility now
  trigger the native macOS prompts plus an alert with an Open System Settings
  button, instead of failing silently into the log
- Transcription history in the menu bar: the last 10 transcripts are listed in
  the dropdown, click one to copy it back to the clipboard
- Replaced rumps with direct AppKit (status item was invisible when launched
  from the app bundle); the app no longer shows as "Python" in the menu bar

## 1.0.1 — 2026-09-01

- Fix crash on macOS Sequoia: replaced pynput with a Quartz CGEventTap on the
  main run loop and CGEventPost for the paste. pynput's key handling calls
  Text Input Source APIs from a background thread, which macOS 15 terminates
  with EXC_BREAKPOINT (dispatch_assert_queue) on the first key event.
- One dependency fewer; failed tap creation now logs a permissions pointer
  at startup instead of silently seeing no keys.

## 1.0.0 — 2026-09-01

Initial release.

- Hold-to-talk dictation: hold right Option, speak, release — transcript is
  pasted into the focused app
- On-device transcription with Whisper large-v3-turbo via MLX (Apple Silicon
  GPU); ~0.5 s per utterance on an M4 Max
- Menu bar app (`…` loading / `🎙` ready / `🔴` recording) with Open Log and
  Quit; built locally by `install.sh`, no notarization needed
- Records from the built-in microphone even when Bluetooth headphones are
  connected — Bluetooth mics lose ~1 s of audio to a codec switch when
  recording starts, which garbled transcripts
- Per-dictation log line with mic, duration, peak level, latency, and
  transcript in `~/Library/Logs/Sotto.log`
