# Contributing

## Setup

```sh
git clone https://github.com/utsavanand/sotto && cd sotto
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

`run.sh` runs the same code the app bundle runs, with logs in your terminal.
Note: permission grants (Accessibility, Input Monitoring, Microphone) attach
to your terminal in dev mode, separately from the Sotto.app grants.

## Before opening a PR

- `ruff check sotto.py` passes (CI enforces this)
- Test the full loop by voice: dictate twice in a row (verifies the hotkey
  survives a completed transcription — the event-tap regression in DESIGN.md)
- Read [DESIGN.md](DESIGN.md) first — several "obvious cleanups" are
  deliberately avoided and documented there, e.g. replacing the NSEvent
  monitors with a CGEventTap (costs every user the Input Monitoring grant),
  transcribing on the event callback (stalls the macOS event tap), or
  stopping the audio stream between the halves of a double-tap (the reopened
  stream records silence)

## Scope

Sotto is deliberately one file with constants instead of configuration. Bug
fixes and accuracy/latency improvements are welcome. Features that add UI,
config files, or new dependencies need a strong case for why they can't be a
constant or a fork.
