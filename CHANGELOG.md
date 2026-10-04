# Changelog
## 2.4.0 — 2026-10-04

A reliability release. An independent audit of the whole application
found eighteen reproducible defects; this fixes all of them. The worst
could cost a dictation the user had already finished speaking.

- A stuck microphone no longer loses your words. macOS's audio stack
  can block forever while releasing a device, and Kaho waited for that
  release before handing the recording to the model — so a hang
  discarded speech that had already been captured. The transcript is
  now submitted first and the device released afterwards. (Swapping
  stop for abort would not have helped: both route through the same
  blocking call.)
- A dictation that takes too long says so instead of vanishing. The
  watchdog used to hide the pill, which erased the only sign that
  anything was still running — and because cancellability was read off
  whether the pill was visible, it also silently stopped Escape from
  working on that job.
- Cancelling one dictation can no longer discard another. Cancellation
  and spoken-instruction ranges were shared between recordings, so
  stopping one dictation and cancelling the next threw away the first,
  and starting a new one erased the previous one's instruction
  boundaries.
- A failed rewrite never costs you the transcript. Three paths could
  throw away recognised speech: a tokenizer without a chat template, a
  malformed custom endpoint, and a provider returning a null response.
  All three now fall back to pasting what you said.
- The reported time is the time you actually waited. It used to start
  after the worker picked the job up, so microphone shutdown and queue
  waiting were invisible — a four-second stall logged as 0.00s.
- Audio that is too quiet, silent, or missing now says so on the pill
  rather than only in a log file. A microphone turned down read as the
  app being broken.
- A slow device release is no longer reported as a wedged one. The old
  five-second threshold fired on healthy recordings and sent people to
  Restart Kaho for something that recovered on its own.
- An instruction too short to use is no longer transcribed into your
  message.
- A dictation already finished can no longer be stopped by a leftover
  timer from an abandoned one, and starting a new one can no longer
  hide the pill of a recording still in progress.
- A microphone that fails to open is no longer left claimed. The stream
  was not closed when it failed to start, so one bad open made every
  later open fail too — and any error other than a PortAudio one left
  the app unable to record at all until it was restarted.
- Changing the hotkey while holding it no longer strands the recording.
  Releasing the old key stopped matching, so the microphone stayed open
  with nothing to end it.
- A damaged settings file or history entry no longer stops the app
  starting. Both checks accepted valid JSON of the wrong shape and then
  crashed on it later.
- Warmups no longer pile up behind each other when several dictations
  follow in quick succession.

2.3.0 was built but never published: Apple's notary service stalled on
it for hours, and by the time that was clear the build was eight fixes
behind. Its contents are included here.

## 2.2.0 — 2026-10-02

- Bring your own key for the rewrite step. On-device stays the default
  and the fallback; a key only ever adds quality, and nothing degrades
  without one. OpenAI, Anthropic, or any OpenAI-compatible endpoint —
  Groq, OpenRouter, a local Ollama — so one setting covers all of them.

  Speech-to-text stays on-device and is not configurable. It is fast
  and accurate enough now that sending audio to a server would cost
  latency, money and the privacy claim for no real gain. Only the
  transcript is ever sent, and only when a key is set.

  The key lives in the Keychain, not in settings.json, which is plain
  JSON that anything able to read the home directory can open. Every
  failure path — no key, no network, bad endpoint, unexpected response
  — falls back to the on-device model and says so, so a cloud problem
  can never cost the user their words.

- Say how you want it written, in the same breath. Press right Shift
  while still holding the hotkey and everything after it is an
  instruction rather than part of the message. The key is held, not
  latched: release it and you are dictating again, so a thought can be
  interrupted with an aside and then carry on — "formal, two sentences",
  "casual, like texting a friend", "turn this into bullet points". The
  instruction overrides the configured rewrite mode for that dictation
  only.

  Every other tool solves this with stored configuration: per-app rules,
  saved modes, prompt libraries. The same app needs a different voice
  for a leader, a junior and family, so the context cannot predict the
  intent — only the speaker knows, and only at that moment. Here the
  instruction is speech: said once, used once, never stored.

  It is one recording, not two. The split is recorded as a position in
  the audio, because stopping and reopening the stream loses about 60 ms
  of speech at exactly the moment the user is mid-sentence.

- Tap to start, tap to stop. Holding the hotkey is still the default and
  is self-limiting — let go and it ends, so a forgotten recording cannot
  run for minutes — but holding a key through a 200-word prompt is
  tiring, and for anyone who cannot hold a modifier down at all it was
  the difference between usable and not. Menu bar and Settings both
  offer the choice.


- Transcription moves from Whisper large-v3-turbo (mlx-whisper) to
  Qwen3-ASR 1.7B 8-bit (mlx-audio). Latency from `tools/benchmark.py`
  on an M4 Max, both models run back to back on the same clips (warm);
  accuracy from a 109-clip benchmark with the dictionary:

  | | Whisper | Qwen3-ASR 1.7B |
  |---|---|---|
  | 1.4 s clip | 0.51 s | 0.12 s |
  | 10.3 s clip | 0.60 s | 0.42 s |
  | 29.6 s clip | 0.78 s | 1.06 s |
  | WER, LibriSpeech clean / other | 1.5% / 1.6% | 1.3% / 0.6% |
  | WER, jargon dictations | 4.4% | 3.4% |
  | dictionary terms spelled right | 87% | 87% |

  Whisper pads every clip to a 30 s window, so its cost barely moves with
  length; this model's grows with it. They cross at roughly 15-20 s.
  Across 677 logged dictations the median is 6.3 s and 84% are under
  20 s, so most get faster and the longest ~15% get slower. An earlier
  run showed a larger gain (0.88 s vs 0.30 s); it was measured while the
  app was dictating on the same GPU, which slowed Whisper more. Parakeet v3 was faster still but takes
  no vocabulary, so it spelled names wrong (44% of terms). Costs: a
  ~2.3 GB first download instead of ~1.6 GB, ~1.1 GB more memory, and a
  slower first load (about 4 s).
- The dictionary is passed to the model as hotwords, and a new spelling
  pass fixes close misses afterwards: the model still wrote "Soto" for
  "Sotto" (the old name) in 8 of 12 clips despite the hotword. A word is replaced only
  when it is within one letter of a term's length, at least 85% similar,
  not an English word (or a regular inflection of one), and capitalized as
  the name the model took it for, so "motto", "a swift reply" and "the
  postmen came" are left alone.
- Dependencies shrink: dropping mlx-whisper removes torch, numba,
  llvmlite, sympy and tiktoken from the lock; mlx-audio adds itself and
  miniaudio.

## 2.1.0 — 2026-09-30

- Escape cancels a dictation. During recording the audio is dropped and
  never transcribed; during transcription or rewriting the result is
  computed and then discarded, because MLX inference is a single
  blocking call that cannot be interrupted. Suppressing the paste is
  the part that matters — the damage is unwanted text landing in a
  document.
- ⌘C, ⌘V, ⌘X and ⌘A work. macOS routes them through the menu bar, and
  the app never installed an Edit menu, so none of them did anything:
  copying a transcript out of History needed a right-click, and the
  dictionary editor could not be pasted into.
- A wedged audio device now says so and can be recovered. PortAudio's
  stop can block forever inside CoreAudio, and the guard that skips
  recording while an audio op is stuck only wrote to the log — so the
  symptom was dictation quietly doing nothing. The pill reports it, and
  a new Restart Kaho menu item quits and reopens the app, which is the
  only cure.
- Language can be pinned. Whisper detects per 30-second window when it
  is not told one, and on short or noisy audio it guesses wrong — an
  English sentence comes back transliterated. Detection stays default.
- `tools/benchmark.py` measures each stage of a dictation against fixed
  clips, so "it feels slower" can be checked against numbers.

## 2.0.0 — 2026-09-29

- Renamed from Sotto to Kaho (Hindi कहो, "say it"). A commercial product
  with a near-identical name was already on the market, which would have
  confused anyone searching for either one. The app, bundle identifier
  (`com.utsavanand.kaho`), log file, support directory and repository move
  together.
- Settings, transcript history and the dictionary carry over: on first
  launch Kaho renames `~/Library/Application Support/Sotto` to `.../Kaho`
  when the old directory exists and the new one does not.
- macOS ties Microphone and Accessibility permission to the bundle
  identifier, so it treats Kaho as a new app: grant both once more. The
  recording pill names what is missing if a paste is blocked.

- Clean up skips the rewrite when there is nothing to clean. Before
  generating, one forward pass asks the rewrite model "already clean? A)
  yes B) no" and reads the two letter probabilities — a decision, not
  generation, ~85 ms. At 90%+ confidence the transcript is pasted as-is,
  saving the 0.35-1.4 s rewrite. On 24 made-up dictations it skipped all
  15 clean ones and none of the 9 messy ones. The question's direction
  matters: phrased "does it need cleanup?" the model said yes to every
  transcript. Structured and Caveman still always rewrite — the same check
  could not reliably tell when a sentence should become a list. A failed
  check falls through to the rewrite.

## 1.7.10 — 2026-09-28

- Idle cold-start hidden. Analysis of 514 logged dictations: 1.1 s median
  back-to-back, but a 7.1 s p90 (17.8 s worst) after an hour idle — macOS
  pages out the models, and the rewrite model's 2.3 GB dominated the
  page-in. Starting a recording after 2+ minutes of inference idleness now
  fires a tiny warmup on the worker queue, so the page-in overlaps the
  seconds the user spends speaking instead of following them. Verified
  live: warmup 1.18 s during speech, post-idle dictation back at 0.90 s.
  Each warmup logs itself, so the next latency analysis can measure it.

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
