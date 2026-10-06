<div align="center">
  <img src="assets/logo.svg" width="128" height="128" alt="Kaho logo">
  <h1>Kaho</h1>
  <p><em>कहो — Hindi for “say it”</em></p>

  <p>
    <img src="https://img.shields.io/badge/macOS-14%2B-000000?logo=apple&logoColor=white" alt="macOS 14+">
    <img src="https://img.shields.io/badge/Apple%20Silicon-arm64-0071e3" alt="Apple Silicon">
    <img src="https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white" alt="Python 3.13">
    <a href="https://github.com/utsavanand/kaho/actions/workflows/ci.yml"><img src="https://github.com/utsavanand/kaho/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
    <a href="https://github.com/utsavanand/kaho/releases/latest"><img src="https://img.shields.io/github/v/release/utsavanand/kaho" alt="Latest release"></a>
    <a href="LICENSE"><img src="https://img.shields.io/github/license/utsavanand/kaho" alt="License"></a>
  </p>

  <p><a href="https://github.com/utsavanand/kaho/releases/latest"><strong>Download for macOS</strong></a>
  · <a href="https://kaho.utsava.xyz">kaho.utsava.xyz</a>
  · <a href="https://github.com/utsavanand/kaho/releases/download/v1.7.8/sotto-explainer.mp4">3-minute explainer video</a></p>
</div>

Hold a key anywhere on macOS, speak, release — your words are typed into
whatever app has focus. Everything runs on your Mac via
[Apple MLX](https://github.com/ml-explore/mlx): no cloud, no account, no
telemetry.

![How Kaho works](assets/flow.svg)

- **Works everywhere** — any app that accepts paste
- **Fast** — under 1.5 s from key-release to text (about 0.15 s to transcribe a
  short dictation on an M4 Max), with Qwen3-ASR 1.7B, which beat Whisper
  large-v3-turbo on accuracy in our benchmark
- **Hands-free** — double-tap the hotkey to lock recording, tap to stop
- **Recording pill** — floating mic-level indicator with an elapsed timer,
  then live progress ("Transcribing…", "Rewriting…", "Pasted") so you always
  know what it's doing
- **Dictionary** — list your names, products, and jargon; the model stops
  guessing "cow" for "Kaho"
- **On-device** — audio never leaves the machine; works offline. (Optional:
  point the *rewrite* step at a cloud model with your own key — transcript
  only, never audio.)
- **Small** — one Python file, seven dependencies

## The menu

Recent transcripts (click to copy), your hotkey, rewrite mode, history,
log, and one-click bug reports:

<img src="assets/menu.svg" width="640" alt="Kaho menu: transcripts, Hotkey, Trigger, Language and Rewrite submenus, Settings, History, Open Log, Report a Bug">

- **Settings…** — a real window (⌘,) for hotkey, trigger, language and rewrite
- **Edit Dictionary…** — names and jargon the model should spell your way
- **Hotkey** — right Option (default), right Command, right Control, or right
  Shift. Right-side only: the left keys are needed for typing.
- **Say how you want it written** — while still holding the hotkey (or
  during hands-free), press either Shift and keep talking. It is *held*, not latched — release it and you
  are dictating again, so a thought can be interrupted with an aside and then
  carry on. Everything said while it is down is an instruction, not message: *"tell Mark I can't make the offsite, suggest
  November"* → ⇧ → *"formal, two sentences"* becomes "Mark, I am unable to
  attend the offsite; I recommend scheduling it in November." The same words
  with *"casual, like texting a friend"* come out as "Hey Mark, I'm sorry but
  I can't make the offsite…". It can change what the message says as well as
  how it reads: *"drop the part about shipping"*, *"it's 4 pm, not 3"*, *"the
  PR is number six"*. Nothing to configure and nothing stored — the
  instruction is spoken once and forgotten.
- **Rewrite model** — on this Mac by default. You can point the rewrite step
  at OpenAI, Anthropic, or any OpenAI-compatible endpoint (Groq, OpenRouter,
  a local Ollama) with your own key, which is worth it for "turn this ramble
  into a paragraph" where a 4B model has a ceiling. The key is stored in your
  Keychain, never in Kaho's settings file. **Your audio never leaves the Mac
  either way** — only the transcript is sent, and only when a key is set. If
  the request fails for any reason, Kaho falls back to the on-device model
  rather than losing your words.
- **Trigger** — hold the key while you speak (default), or tap once to start
  and once to stop. Holding is self-limiting, so a forgotten recording cannot
  run for minutes; tapping is easier through a long prompt, and is the only
  usable mode if you cannot hold a modifier down.
- **Language** — detected automatically by default. Pin yours if short
  dictations come back in the wrong script: Whisper guesses per 30-second
  window, and on brief or noisy audio it guesses wrong.
- **Restart Kaho** — recovers a stuck microphone. macOS's audio stack can
  wedge mid-recording; the pill says so when it happens.
- **Report a Bug…** — opens a pre-filled Mail draft with diagnostics and the
  log attached; nothing sends until you review it.

If your menu bar is full, macOS hides the icon behind the notch. Kaho
detects that and adds a Dock icon with the same menu, so Settings and
History stay reachable — no alert, no lost settings.

## Rewrite (optional)

A second on-device model (Qwen3-4B-Instruct, ~2.3 GB on first enable,
~0.5 s per dictation) polishes the transcript before it's pasted. Three
modes: **Clean up** keeps your wording and strips filler, **Structured**
gives the dictation the shape it needs (crisp prose, bullets for parallel
items, numbered steps for a sequence), and **Caveman** compresses hard for
pasting into an AI assistant — one line, every instruction kept, the words and
formatting around them cut.

<img src="assets/rewrite.svg" width="900" alt="Rewrite example: filler-laden dictation becomes clean prose or a structured list">

In Clean up, Kaho first asks the same model whether the transcript is
already clean — one forward pass, ~85 ms, no text generated — and pastes it
as-is when the model is at least 90% sure, skipping the rewrite entirely.

Off by default. If the model isn't loaded yet or a rewrite fails, the raw
transcript is pasted — you never lose words.

## Install

Requires an Apple Silicon Mac on macOS 14+, and Python 3.13 — that exact
minor version, because the lock file pins hash-verified 3.13 wheels.

On a fresh Mac, two one-time steps first:

```sh
# 1. Homebrew (also installs git via the Xcode Command Line Tools)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
# then run the eval line(s) the installer prints, so `brew` is on your PATH

# 2. Python 3.13
brew install python@3.13
```

Then:

```sh
git clone https://github.com/utsavanand/kaho && cd kaho
./install.sh
open /Applications/Kaho.app
```

`install.sh` builds `Kaho.app` locally (hash-verified Python environment +
ad-hoc-signed bundle), so there are no Gatekeeper warnings. On first launch
Kaho asks for two permissions:

| Permission | Why |
|---|---|
| Microphone | recording while the hotkey is held |
| Accessibility | observing the global hotkey, sending the paste |

Until Accessibility is granted the menu bar shows ⚠️ and the first menu
item opens the right Settings pane. Kaho notices the grant and restarts
itself, so there's no need to quit and reopen it. The downloaded app also
offers to move itself into Applications if it's opened from the disk image
or Downloads, since permissions granted there don't follow the app.

First launch downloads the speech model (~2.3 GB). The menu bar shows the
percentage, and "Ready to dictate" appears when it's done. To run at login:
System Settings → General → Login Items.

## Usage

Put your cursor where the text should go, hold <kbd>⌥ right Option</kbd>,
speak, release. Double-tap instead to record hands-free; tap once to stop.
Everything else lives in the 🎙 menu.

## FAQ

<details>
<summary><strong>Where do I see everything I've dictated?</strong></summary>

🎙 → History…, or — if the menu bar icon is hidden behind the notch — just
launch Kaho again (Launchpad, Finder, or `open /Applications/Kaho.app`)
while it's running: the History window opens. Only one Kaho ever runs; a
second launch brings the running one forward and quits.
</details>

<details>
<summary><strong>The hotkey does nothing.</strong></summary>

If the menu bar shows ⚠️, Accessibility isn't granted yet: click the first
menu item and switch Kaho on. If it's already switched on and Kaho still
can't paste, Kaho says so after a short wait and walks you through removing
the old entry and adding Kaho again. Re-running `install.sh` rebuilds the
bundle and can reset the grant. Also make sure you're pressing the key shown
in 🎙 → Hotkey — it's the **right**-side key.
</details>

<details>
<summary><strong>It suddenly stopped working everywhere.</strong></summary>

Some app is holding macOS *secure input* (password fields, `sudo` prompts,
and Keychain dialogs block global key observation by design — usually it's a
terminal that never released it). Close that app or its window.
</details>

<details>
<summary><strong>I'm wearing AirPods and the transcripts were wrong.</strong></summary>

Fixed by design: Kaho always records from the built-in microphone. Bluetooth
mics switch to a low-quality codec when recording starts and lose ~1 s of
audio during the switch, garbling the start of every dictation.
</details>

<details>
<summary><strong>It typed "Thank you." when I said nothing.</strong></summary>

Speech models hallucinate on silence. Holds under 0.3 s and audio below a
loudness floor are dropped, but a longer silent hold can still produce one
of these.
</details>

<details>
<summary><strong>Why did my clipboard change?</strong></summary>

It changes for about a second and a half, then changes back. Kaho pastes by
writing the transcript to the clipboard and sending <kbd>⌘V</kbd>, then puts
your previous clipboard back — unless you copied something else in the
meantime, in which case yours wins and the transcript is left alone. The
transcript is marked transient, so clipboard managers that honour that flag
(Maccy, Paste, Raycast) will not archive your dictations.
</details>

<details>
<summary><strong>Can I change the models?</strong></summary>

The speech and rewrite models are constants at the top of `kaho.py`; re-run
`./install.sh` after editing. `mlx-community/Qwen3-ASR-0.6B-8bit` is about
twice as fast again (0.94 GB), a little worse on names and jargon.
</details>

## Architecture

```mermaid
flowchart LR
    K["hotkey (hold or double-tap)"] --> M["NSEvent global monitor (main run loop)"]
    M --> R["Recorder — sounddevice, 16 kHz"]
    M -.-> O["Overlay pill — live mic level"]
    R --> Q[["audio queue"]]
    Q --> W["Worker thread"]
    W --> T["mlx-audio — Qwen3-ASR 1.7B on the Apple GPU"]
    T --> RW["optional rewrite — Qwen3-4B via mlx-lm"]
    RW --> P["Clipboard + synthetic Cmd+V"]
    P --> A["Focused app"]
    T --> H[("history.jsonl")]
    H --> V["History window"]
```

The hotkey handler and UI live on the main run loop; recording and inference
run off it, so a slow transcription can never stall key handling. The full
trade-off discussion: [DESIGN.md](DESIGN.md).

## Privacy

Audio is captured only while the hotkey is held, processed in memory, never
written to disk or sent anywhere. Transcripts go to the clipboard, the local
log, and the local history file (`~/Library/Application Support/Kaho/`) —
delete them any time. Rewriting runs entirely on-device too. The models are
fetched once from Hugging Face; nothing else touches the network.

## Development

```sh
python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh    # runs from the repo, logs to the terminal
```

Design rationale: [DESIGN.md](DESIGN.md) · Contributing:
[CONTRIBUTING.md](CONTRIBUTING.md)

## Uninstall

```sh
rm -rf /Applications/Kaho.app ~/Library/Logs/Kaho.log
rm -rf "$HOME/Library/Application Support/Kaho"
```

The cached models live in `~/.cache/huggingface` if you want those gone too.

## Acknowledgments

Built on [mlx-audio](https://github.com/Blaizzy/mlx-audio),
[mlx-lm](https://github.com/ml-explore/mlx-lm),
[sounddevice](https://github.com/spatialaudio/python-sounddevice), and
[PyObjC](https://github.com/ronaldoussoren/pyobjc). Interaction model
inspired by [Wispr Flow](https://wisprflow.ai).

## License

[MIT](LICENSE)
