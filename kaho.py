"""Kaho: hold the hotkey (right Option by default) anywhere, speak, release —
locally transcribed text is pasted into the focused app. See DESIGN.md."""

import collections
import ctypes
import difflib
import json
import multiprocessing
import os
import platform
import queue
import re
import subprocess
import threading
import time
import traceback
import urllib.parse
import urllib.request

import AppKit
import huggingface_hub
import numpy as np
import Quartz
import sounddevice as sd
from mlx_audio.stt.utils import load_model
from PyObjCTools import AppHelper

# kVK_* keycodes from Carbon's Events.h. Raw keycodes, not characters: pynput
# was dropped because its character mapping calls TIS (Text Input Source) APIs
# off the main thread, which macOS 15 kills with EXC_BREAKPOINT
# (dispatch_assert_queue).
V_KEYCODE = 9
# Each hotkey pairs its keycode with the NX_DEVICE*KEYMASK bit from IOKit's
# IOLLEvent.h. The device-specific bit is essential: the aggregate
# NSEventModifierFlagOption stays set while LEFT Option is held, which made a
# right-Option release look like a press and left recording stuck on. Only
# right-side modifiers are offered — the left ones are needed for typing
# special characters and app shortcuts.
HOTKEYS = {  # name -> (keycode, device-specific modifier bit, label)
    "right_option": (61, 0x0040, "Right Option (⌥)"),
    "right_command": (54, 0x0010, "Right Command (⌘)"),
    "right_control": (62, 0x2000, "Right Control (⌃)"),
    "right_shift": (60, 0x0004, "Right Shift (⇧)"),
}

# Pressing a second right-side modifier mid-dictation splits the recording:
# everything before it is the message, everything after is an instruction for
# how to write it. Whichever modifier is not serving as the hotkey is used,
# so the two can never collide.
INSTRUCTION_FALLBACK = "right_shift"

# On-device is the default and the fallback: a key only ever adds a better
# rewrite, it never becomes load-bearing. Nothing degrades without one, and
# the audio never leaves the Mac either way — only the transcript is sent,
# and only when a key is set.
REWRITE_BACKENDS = {
    "local": "On this Mac (Qwen3 4B)",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "custom": "Custom (OpenAI-compatible)",
}

# Defaults chosen so the common case needs only a key. Overridable for
# self-hosted and proxy endpoints, which is the point of "custom".
BACKEND_DEFAULTS = {
    "openai": ("https://api.openai.com/v1/chat/completions", "gpt-5"),
    "anthropic": ("https://api.anthropic.com/v1/messages", "claude-sonnet-5"),
    "custom": ("", ""),
}

KEYCHAIN_SERVICE = "com.utsavanand.kaho.apikey"
# Generous, because a frontier model on a long dictation is not fast; the
# fallback to on-device matters more than the exact number
API_TIMEOUT_SECONDS = 30


def instruction_key():
    """(keycode, device mask) of the modifier that starts an instruction."""
    name = INSTRUCTION_FALLBACK
    if settings["hotkey"] == name:
        name = "right_command"
    keycode, mask, _ = HOTKEYS[name]
    return keycode, mask


def instruction_label():
    name = INSTRUCTION_FALLBACK
    if settings["hotkey"] == name:
        name = "right_command"
    return HOTKEYS[name][2]


# Replaced Whisper large-v3-turbo in 2.2.0. On an M4 Max (tools/benchmark.py):
# 0.12 s vs 0.51 s on a 1.4 s clip, 0.42 vs 0.60 at 10 s, but 1.06 vs 0.78 at
# 30 s — Whisper pads every clip to 30 s, this model's cost grows with length,
# and they cross around 15-20 s. Most dictations are well under that (median
# 6.3 s over 677 logged). Lower WER too (1.3% vs 1.5% LibriSpeech clean, 3.4%
# vs 4.4% on jargon with the dictionary). Parakeet v3 was faster still but
# takes no vocabulary, so it spelled names wrong and the Dictionary would
# have been dead weight.
MODEL_REPO = "mlx-community/Qwen3-ASR-1.7B-8bit"
# Pinned HF revision: the repo name is a mutable reference, the commit is not.
# Update deliberately (huggingface.co/api/models/<repo> -> "sha") after
# checking the diff, since the model runs inside an app holding mic and
# Accessibility permissions.
MODEL_REVISION = "a8379a2e2f9e313c9292cdf1af4055ab56d50d55"
MODEL_SIZE_LABEL = "~2.3 GB"
# The Instruct-2507 (non-thinking) variant: the 1.7B model echoed long rambly
# transcripts back unchanged in clean mode, and thinking-mode Qwen3 burned 7+
# seconds per dictation. 4B-Instruct rewrites reliably in ~0.3-0.8s on M-series.
REWRITE_REPO = "mlx-community/Qwen3-4B-Instruct-2507-4bit"
REWRITE_REVISION = "50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b"
REWRITE_SIZE_LABEL = "~2.3 GB"
# Left to detect, a speech model can guess wrong on short or noisy audio —
# Whisper sent English sentences back transliterated into Hindi or Spanish.
# Pinning the language removes that failure mode. The names double as what
# Qwen3-ASR expects, and all nine are in its supported list.
# "auto" stays the default so multilingual users are not forced to choose.
LANGUAGES = {
    "auto": "Detect automatically",
    "en": "English",
    "hi": "Hindi",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "pt": "Portuguese",
    "it": "Italian",
    "ja": "Japanese",
    "zh": "Chinese",
}

# Hold is the default because it is self-limiting: let go and it stops, so a
# forgotten recording cannot run for minutes. Toggle exists because holding a
# key through a 200-word prompt is tiring, and for anyone who cannot hold a
# modifier down at all it is the difference between usable and not.
TRIGGERS = {
    "hold": "Hold to dictate",
    "toggle": "Tap to start, tap to stop",
}

REWRITE_MODES = {
    "off": "Off",
    "clean": "Clean up",
    "structured": "Structured",
    "caveman": "Caveman",
}
REWRITE_HINTS = {
    "off": "Paste exactly what was heard.",
    "clean": "Remove filler words and fix punctuation. Your wording is kept.",
    "structured": "Give the dictation the shape it needs — crisp sentences, steps, or bullets.",
    "caveman": "Compress hard for prompting an LLM — every instruction kept, words minimised.",
}
# The instruction is spoken, so it arrives as loose speech ("uh, make this
# formal, short") rather than a tidy directive. The prompt says to follow its
# intent, and spells out the two failure modes seen in testing: answering the
# instruction as if it were a question, and treating it as new content to
# include in the output.
INSTRUCTION_PROMPT = (
    "You rewrite dictated speech according to a spoken instruction.\n\n"
    "INSTRUCTION (how the user wants it written):\n{instruction}\n\n"
    "MESSAGE (what the user dictated):\n{text}\n\n"
    "Rewrite MESSAGE following INSTRUCTION. Rules:\n"
    "- Output only the rewritten message. No preamble, no explanation, no "
    "quotes around it.\n"
    "- Never answer or comment on the instruction. It describes how to "
    "write, it is not a question and not part of the message.\n"
    "- Keep every fact, name, number and request from MESSAGE. You are "
    "changing how it reads, not what it says.\n"
    "- The instruction was spoken, so ignore its filler words and follow "
    "what it means.\n"
    "- If the instruction asks for something the message cannot support, "
    "write the message as well as you can and change nothing else."
)

REWRITE_PROMPTS = {
    "clean": (
        "You clean up dictated speech. Rewrite the transcript below:\n"
        "- remove filler words (um, uh, like, you know, I mean, so, basically, "
        "actually, sort of, kind of, yeah)\n"
        "- drop false starts, self-corrections, and repeated words\n"
        "- fix punctuation, capitalization, and sentence breaks\n"
        "Keep the speaker's own words, tone, and meaning. Do not summarize, "
        "shorten, reorder, or add anything. Never answer questions that appear "
        "in the transcript — only clean them up. Reply with the cleaned text "
        "only — no preamble, no quotes.\n\nTranscript:\n{text}"
    ),
    # Structured picks the shape from the content instead of forcing bullets
    # onto everything — a single thought stays a paragraph.
    "structured": (
        "You give dictated speech the structure it deserves, keeping the "
        "speaker's voice.\n"
        "First decide the shape, then write only the result:\n"
        "- Does the speaker walk through steps in order (first/then/after "
        "that/finally)? → a NUMBERED list, one step per line. TWO steps is "
        "already enough; never leave a sequence as a run-on sentence.\n"
        "- Do they list two or more parallel things (and…and…and, or "
        "\"there's three things\")? → a BULLET list, one item per line.\n"
        "- Otherwise (a single idea, an explanation, one or two sentences) → "
        "PROSE. Do not invent a list.\n"
        "When it is a list, do not flatten it back into one sentence — that is "
        "the most common mistake. When it is prose, do not force bullets.\n"
        "If the speaker framed the list with a statement (\"the release is "
        "blocked, there's three things\"), keep that framing as a lead-in line "
        "above the list — dropping it loses why the items matter. Every list "
        "line still starts with \"- \" or \"1. \".\n"
        "This is transcription, not composition: reuse the speaker's own words "
        "and phrasing wherever they are already clear. Never substitute more "
        "polished vocabulary for theirs, and never write a sentence whose "
        "content they did not say.\n"
        "In every case:\n"
        "- remove filler words, false starts, stutters, and repetition; fix "
        "grammar, punctuation, and sentence breaks\n"
        "- keep the speaker's own words, tone, and level of certainty — this is "
        "their voice tidied, not your summary\n"
        "- keep every substantive detail, name, and number\n"
        "- keep a condition attached to what it qualifies: \"do X, but only if "
        "Y\" stays together, never split into two items — splitting turns a "
        "conditional into an unconditional one\n"
        "- never add information, opinions, or headings the speaker did not "
        "give, and never answer questions in the transcript\n"
        "Reply with the rewritten text only — no preamble.\n\n"
        # Worked examples: rules alone left the model flattening sequences back
        # into run-on sentences and padding with invented closing lines.
        "Example transcript:\n"
        "okay so to ship this you first uh you run the tests, then you tag the "
        "release, and then after that you push\n"
        "Example reply:\n"
        "1. Run the tests\n2. Tag the release\n3. Push\n\n"
        "Example transcript:\n"
        "yeah I looked and um the bug only happens on Safari, something to do "
        "with the flexbox gap thing I think\n"
        "Example reply:\n"
        "The bug only happens on Safari — something to do with the flexbox gap, "
        "I think.\n\n"
        "Example transcript:\n"
        "so we need to um fix the header, and also update the changelog, and uh "
        "email the beta folks\n"
        "Example reply:\n"
        "- Fix the header\n- Update the changelog\n- Email the beta folks\n\n"
        "Transcript:\n{text}"
    ),
    # Caveman targets LLM prompts: an agent needs the constraints and the ask,
    # not the social scaffolding of speech.
    "caveman": (
        "You compress dictated speech into the shortest text that still "
        "carries the full meaning, for pasting into an AI assistant as a "
        "prompt.\n"
        "- keep every instruction, constraint, name, number, path, and "
        "technical term EXACTLY as spoken\n"
        "- cut all filler, hedging, politeness, and social scaffolding "
        "(\"I was thinking maybe we could\" becomes the bare instruction)\n"
        "- drop articles and auxiliary verbs where meaning survives without "
        "them; use fragments and imperatives freely\n"
        "- never drop a requirement to save words, and never invent one\n"
        "Formatting is overhead too: write ONE line, separating asks with "
        "\"; \". No bullets, no numbering, no line breaks, no trailing spaces "
        "— every one of those costs tokens the model does not need.\n"
        "Aim for roughly a third of the original length. Reply with the "
        "compressed text only.\n\n"
        "Example transcript:\n"
        "hey can you um look at the login page, it's broken on mobile I think, "
        "and maybe check the signup flow too but only if you have time\n"
        "Example reply:\n"
        "check login page broken on mobile; if time, check signup flow too\n\n"
        "Transcript:\n{text}"
    ),
}
# The speech model often returns a transcript Clean up would leave untouched, and
# generating it anyway costs 0.7-1.4 s. One forward pass reads how likely the
# first answer token is A vs B: a decision, not generation. The direction
# matters — asked "does it need cleanup?" Qwen3-4B answered yes to every
# transcript, clean or not; asked this way round it separated 15 clean and 9
# messy made-up dictations at 1.00 vs 0.00.
CLEAN_CHECK_PROMPT = (
    "Is this dictated transcript already clean enough to paste exactly as-is: "
    "no filler words (um, uh, like, you know), no false starts or "
    "self-corrections, no repeated words, and correct punctuation?\n"
    "A) Yes, paste it as-is\n"
    "B) No, it needs cleanup\n"
    "Answer with the letter only.\n\nTranscript:\n{text}"
)
# A wrong skip pastes filler the user asked to have removed; a wrong rewrite
# only costs time. So skip only when the check is close to certain.
SKIP_REWRITE_CONFIDENCE = 0.9
SAMPLE_RATE = 16_000
MIN_SECONDS = 0.3
# Speech models invent fluent text from near-silence. Measured on Whisper,
# the model before 2.2.0: 0.6s at peak 0.005 produced a paragraph of German,
# and 0.013 produced "videos" 400 times. Every real dictation in practice
# peaks at 0.05+, every hallucination under 0.02. Kept for the current model:
# the floor costs nothing on real speech.
MIN_PEAK = 0.025
# Delay before handing the clipboard back after a paste. Slow apps read the
# pasteboard well after the Cmd+V keystroke; restoring too early makes them
# paste the previous contents instead of the transcript.
CLIPBOARD_RESTORE_SECONDS = 1.5
TAP_MAX_SECONDS = 0.45  # a press shorter than this counts as a tap
# Two taps within this window lock hands-free mode. 0.5s was tighter than a
# natural double-tap: real attempts logged at 0.6-0.8s apart missed the pair,
# so the gesture silently did nothing.
DOUBLE_TAP_SECONDS = 0.9
# Ignore a stop tap arriving right after locking. Without it, the release of
# the second tap — or a third from an over-eager double-tap — cancelled the
# lock instantly, recording a fraction of a second of silence that the
# speech model then hallucinated a paragraph from.
LOCK_GRACE_SECONDS = 0.6
HISTORY_SIZE = 10
LONG_RECORDING_SECONDS = 60  # elapsed counter turns amber past this
# Upper bound on transcribe+rewrite before the overlay gives up and hides.
# Generous: a 5-minute dictation plus a rewrite stays well inside it.
PIPELINE_TIMEOUT_SECONDS = 90
# After this much inference idleness, macOS has typically paged out the model
# weights (2.3 GB for Qwen alone), and the next dictation pays 5-10 s of
# page-in — measured: median 1.1 s back-to-back vs p90 7.1 s after an hour
# idle, worst 17.8 s. A warmup fired at record-start hides that behind the
# seconds the user spends speaking.
WARM_IDLE_SECONDS = 120
LOG_PATH = os.path.expanduser("~/Library/Logs/Kaho.log")
SUPPORT_DIR = os.path.expanduser("~/Library/Application Support/Kaho")
HISTORY_PATH = os.path.join(SUPPORT_DIR, "history.jsonl")
SETTINGS_PATH = os.path.join(SUPPORT_DIR, "settings.json")
DICTIONARY_PATH = os.path.join(SUPPORT_DIR, "dictionary.txt")
# The terms ride in the model's prompt on every dictation. There is no hard
# window any more (Whisper's was 224 tokens), but each term is prefill paid
# per dictation, and a bloated list dilutes the bias on the words you do say.
DICTIONARY_MAX_TERMS = 120
# "soto" vs "sotto" scores 0.89; "sato" (a real surname) 0.67 and is left alone
RESPELL_MIN_SIMILARITY = 0.85
# Ships with macOS: 236k words, the guard that keeps respell() off real words
ENGLISH_WORDS_PATH = "/usr/share/dict/words"
DICTIONARY_TEMPLATE = """\
# Kaho dictionary — one term per line.
#
# The speech model picks the likeliest spelling when audio is ambiguous, so
# listing your names, products, and jargon here biases it toward yours. Lines
# starting with # are ignored. Edits apply to the next dictation; no
# restart needed.

# Uncommented because the app cannot say its own name without it: the
# speech model hears "Kaho" and writes "Kahoot".
Kaho

# Your own names, products and jargon go below.
# Duckterm
# Kubernetes
"""
TITLES = {"loading": "…", "ready": "🎙", "recording": "🔴", "error": "⚠️"}
# The one place the version is written. install.sh and packaging/Kaho.spec read
# it from here, and tools/check_docs_sync.py fails the build when the top of
# CHANGELOG.md disagrees — the Kaho.spec copy had silently sat at 1.7.3 for six
# releases, which is what a bundle built without KAHO_VERSION would have shipped.
APP_VERSION = "2.2.0"
BUG_REPORT_EMAIL = "getutsava@gmail.com"
SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"

jobs = queue.Queue()
audio_ops = queue.Queue()  # serialized PortAudio operations, see audio_control()
audio_op_started = None  # monotonic start of the op in flight, None when idle
# Guards the recording handoff — state, record_buf, stream, locked — between the
# main run loop (hotkey, UI) and the audio thread. Without it _open_stream's
# check-then-publish raced a key release: stop_recording read `stream` while it
# was still None, so the stream published an instant later belonged to nobody,
# was never closed, and left the microphone live until the next recording
# overwrote the reference. Held only around these assignments, never across a
# PortAudio call — those can block for seconds, which is why they run on the
# audio thread in the first place.
recording_lock = threading.Lock()
record_buf = None  # per-recording frame list; identity marks the active recording
stream = None
monitors = []
state = "loading"  # loading | ready | recording
input_device = None
input_name = "system default"
history = collections.deque(maxlen=HISTORY_SIZE)  # (time_str, text), newest first
history_version = 0
overlay = None
history_win = None
settings_win = None
status_item = None  # StatusItem delegate, so windows can refresh the menu
locked = False
# Bumped by Escape. A job carries the value it was queued with; when they no
# longer match, the result is dropped instead of pasted. MLX inference is one
# blocking call that cannot be interrupted, so cancelling suppresses the paste
# rather than stopping the work — which is the part that matters, since the
# damage is unwanted text landing in the user's document.
job_generation = 0
# Set by Escape during a recording, cleared when the audio is dropped. The
# generation counter cannot cover this case: _finish_recording reads the
# counter on the audio thread after the bump has landed.
cancel_recording = False
# Frame ranges of the recording that were spoken as instruction rather than
# message, as [start, end) pairs; the open range has end None. Held, not
# latched: the key can be pressed and released as often as the user likes,
# so a thought can be interrupted with "make this formal" and then continue.
# Recorded as positions in one recording rather than separate ones, because
# stopping and reopening the stream loses ~60 ms at exactly the moment the
# user is mid-sentence.
instruction_spans = []
press_time = 0.0
last_tap = 0.0
lock_time = 0.0  # when hands-free last engaged, for the grace period
settings = {"hotkey": "right_option", "rewrite": "off", "language": "auto",
            "trigger": "hold", "rewrite_backend": "local",
            "api_url": "", "api_model": ""}
mlx_lm = None  # imported lazily by _load_rewriter — pulls in transformers (~2s)
rewriter = None  # (model, tokenizer) once loaded
rewriter_thread = None
last_inference = 0.0  # monotonic time of the last real or warmup inference
WARMUP = object()  # sentinel job: page the models back in before the audio lands
rewriter_lock = threading.Lock()  # see ensure_rewriter
asr = None  # the speech model, loaded by backend
_english_words = None  # see english_words


# Transcripts are sensitive: create log/history files 0600 instead of the
# umask default, in case the parent directory permissions ever loosen
def _private_opener(path, flags):
    return os.open(path, flags, 0o600)


def log(msg):
    # Best-effort by design: log() runs inside the exception handlers that
    # keep the workers alive, so it must never raise (full disk, broken pipe)
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    try:
        print(line, flush=True)
    except OSError:
        pass
    try:
        with open(LOG_PATH, "a", opener=_private_opener) as f:
            f.write(line + "\n")
    except OSError:
        pass


def hotkey_label():
    return HOTKEYS[settings["hotkey"]][2]


def load_settings():
    """Unknown values fall back to defaults — a settings file written by a
    newer version must not brick this one."""
    try:
        with open(SETTINGS_PATH) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return
    if saved.get("hotkey") in HOTKEYS:
        settings["hotkey"] = saved["hotkey"]
    # "bullets" became "structured" in 1.6.0 — without this the saved value no
    # longer matches and the mode silently reverts to Off
    mode = {"bullets": "structured"}.get(saved.get("rewrite"), saved.get("rewrite"))
    if mode in REWRITE_MODES:
        settings["rewrite"] = mode
    if saved.get("language") in LANGUAGES:
        settings["language"] = saved["language"]
    if saved.get("trigger") in TRIGGERS:
        settings["trigger"] = saved["trigger"]
    if saved.get("rewrite_backend") in REWRITE_BACKENDS:
        settings["rewrite_backend"] = saved["rewrite_backend"]
    for key in ("api_url", "api_model"):
        if isinstance(saved.get(key), str):
            settings[key] = saved[key]


def save_settings():
    try:
        with open(SETTINGS_PATH, "w", opener=_private_opener) as f:
            json.dump(settings, f)
    except OSError as e:
        log(f"could not save settings: {e}")


def read_dictionary():
    """Terms the user wants spelled their way. Re-read per dictation — the
    file is tiny, and edits should not need a relaunch."""
    try:
        with open(DICTIONARY_PATH) as f:
            lines = f.readlines()
    except OSError:
        return []
    terms = []
    for line in lines:
        term = line.strip()
        if term and not term.startswith("#"):
            terms.append(term)
    if len(terms) > DICTIONARY_MAX_TERMS:
        log(
            f"dictionary has {len(terms)} terms; using the first "
            f"{DICTIONARY_MAX_TERMS} (every term is prompt the model reads per dictation)"
        )
        terms = terms[:DICTIONARY_MAX_TERMS]
    return terms


def english_words():
    """Lowercased macOS word list, loaded once. Empty if the file is missing —
    respell() then relies on its similarity and length rules alone."""
    global _english_words
    if _english_words is None:
        try:
            with open(ENGLISH_WORDS_PATH) as f:
                _english_words = {line.strip().lower() for line in f}
        except OSError:
            _english_words = set()
    return _english_words


def is_english(word):
    """True for a listed word or a regular inflection of one. The macOS list
    is mostly base forms: it has "motion" but not "motions" or "reviewed"."""
    low = word.lower()
    words = english_words()
    if low in words:
        return True
    return any(
        low.endswith(end) and low[: -len(end)] in words
        for end in ("s", "es", "ed", "d", "ing", "er", "ers", "ly")
    )


def _compact(s):
    return re.sub(r"[\s-]", "", s).lower()


def respell(text, terms):
    """Replace near-misses of dictionary terms with the dictionary's spelling.

    The model takes the dictionary as hotwords yet still wrote "Soto" for
    "Sotto" in 8 of 12 benchmark clips. Deliberately narrow, because a wrong
    "fix" is worse than the miss: a word is only replaced when it is within
    one letter of the term's length, at least RESPELL_MIN_SIMILARITY alike,
    not itself an English word ("motto" stays "motto"), and capitalized as
    the name the model took it for — the word list misses irregular forms,
    and "the postmen came" must not become "the Postman came". Exact matches
    also get the dictionary's casing ("PostgresQL" -> "PostgreSQL"), and two
    words that join into a term are merged ("Postgre SQL" -> "PostgreSQL").
    """
    if not terms:
        return text
    by_compact = {_compact(t): t for t in terms}
    single = [t for t in terms if " " not in t]
    words = list(re.finditer(r"[A-Za-z][A-Za-z'-]*", text))
    out, pos, i = [], 0, 0
    while i < len(words):
        m = words[i]
        # Two words the model split apart, separated by one space
        if i + 1 < len(words):
            nxt = words[i + 1]
            joined = text[m.start():nxt.end()]
            if nxt.start() - m.end() == 1 and _compact(joined) in by_compact:
                out += [text[pos:m.start()], by_compact[_compact(joined)]]
                pos, i = nxt.end(), i + 2
                continue
        word = m.group()
        # Respell the stem and keep a possessive: "Soto's" -> "Sotto's"
        stem, suffix = (word[:-2], word[-2:]) if word.lower().endswith("'s") else (word, "")
        # An English word is left as spoken, even an exact match: with "Swift"
        # in the dictionary, "a swift reply" must not become "a Swift reply"
        if is_english(stem):
            replacement = None
        else:
            replacement = by_compact.get(stem.lower()) or _near_miss(stem, single)
        if replacement and replacement != stem:
            out += [text[pos:m.start()], replacement + suffix]
            pos = m.end()
        i += 1
    out.append(text[pos:])
    return "".join(out)


def _near_miss(word, terms):
    low = word.lower()
    if len(low) < 4:
        return None
    best, best_ratio = None, RESPELL_MIN_SIMILARITY
    for term in terms:
        if abs(len(term) - len(low)) > 1:
            continue
        # A capitalized term ("Postman") only claims a word the model also
        # capitalized; a lowercase one ("kubectl") claims either
        if term[0].isupper() and not word[0].isupper():
            continue
        ratio = difflib.SequenceMatcher(None, low, term.lower()).ratio()
        if ratio >= best_ratio:
            best, best_ratio = term, ratio
    return best


def ensure_dictionary_file():
    if os.path.exists(DICTIONARY_PATH):
        return
    try:
        with open(DICTIONARY_PATH, "w", opener=_private_opener) as f:
            f.write(DICTIONARY_TEMPLATE)
    except OSError as e:
        log(f"could not create the dictionary file: {e}")


def append_history(text):
    global history_version
    history.appendleft((time.strftime("%H:%M"), text))
    history_version += 1
    try:
        with open(HISTORY_PATH, "a", opener=_private_opener) as f:
            f.write(json.dumps({"t": time.time(), "text": text}) + "\n")
    except OSError as e:
        log(f"could not persist history entry: {e}")


def read_history_file():
    """Returns [(epoch, text)], oldest first, skipping damaged lines — a
    truncated final write must never take the whole app down."""
    try:
        with open(HISTORY_PATH) as f:
            lines = f.readlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        try:
            e = json.loads(line)
            entries.append((float(e["t"]), str(e["text"])))
        except (ValueError, KeyError, TypeError):
            log("skipping a malformed history line")
    return entries


def load_history():
    global history_version
    for epoch, text in read_history_file()[-HISTORY_SIZE:]:
        history.appendleft((time.strftime("%H:%M", time.localtime(epoch)), text))
    history_version += 1


# Bluetooth mics (AirPods etc.) switch to the low-quality HFP codec when
# recording starts, losing ~1s of audio during the switch — so prefer the
# built-in mic over the system default.
def pick_input_device():
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and ("MacBook" in d["name"] or "Built-in" in d["name"]):
            return i, d["name"]
    return None, sd.query_devices(kind="input")["name"] + " (system default)"


# CoreAudio open/stop can block indefinitely on a HAL mutex held by another
# audio client (observed as a full main-thread deadlock with Wispr Flow
# running), so every PortAudio call runs on one dedicated audio thread — the
# hotkey and UI stay alive no matter what the audio stack does, and
# serializing the ops means a wedged device pins at most that one thread
# instead of leaking a new one per recording.
def audio_control():
    global audio_op_started
    while True:
        op = audio_ops.get()
        audio_op_started = time.monotonic()
        try:
            op()
        except Exception as e:  # noqa: BLE001
            log(f"audio operation failed: {e!r}")
        audio_op_started = None


def audio_wedged():
    started = audio_op_started
    return started is not None and time.monotonic() - started > 5


def relaunch():
    """Quit and start a fresh copy.

    The only way out of a wedged audio device: PortAudio's stop can block
    forever inside CoreAudio (observed in FinishStoppingStream after a long
    dictation), and that thread cannot be interrupted from here.
    """
    # NOT NSBundle.mainBundle(): the process runs out of Homebrew's
    # Python.app, so that returns the Python framework and reopening it does
    # nothing at all — the app would quit and never come back. This file
    # lives in Kaho.app/Contents/Resources, so walk up to the bundle.
    resources = os.path.dirname(os.path.abspath(__file__))
    path = os.path.dirname(os.path.dirname(resources))
    if not path.endswith(".app"):
        log(f"not running from an app bundle ({path}) — quit and start it again by hand")
        AppKit.NSApp.terminate_(None)
        return
    # `open -n` after a delay: the replacement has to start once this copy is
    # gone, or macOS just activates the dying instance instead of launching one
    subprocess.Popen(
        ["/bin/sh", "-c", f'sleep 1; open -n "{path}"'],
        start_new_session=True,
    )
    log(f"relaunching {path} to clear the wedged audio device")
    AppKit.NSApp.terminate_(None)


def start_recording():
    global state, record_buf
    if state != "ready":
        return
    if audio_wedged():
        # Say so on screen, not just in the log: this used to fail silently,
        # and the only symptom was dictation quietly not working
        log(
            "audio device is not responding — recording skipped. "
            "Use Restart Kaho in the menu to recover."
        )
        overlay.show_wedged()
        return
    with recording_lock:
        # Re-checked under the lock: the audio thread also moves `state` back to
        # "ready" when a mic open fails
        if state != "ready":
            return
        state = "recording"
        instruction_spans.clear()
        buf = []
        record_buf = buf
    overlay.show()
    # Fire a warmup while the user is still speaking: the page-in of idle
    # model weights overlaps the recording instead of delaying the paste.
    # Same queue as real jobs, so it can never race the model.
    if state == "recording" and time.monotonic() - last_inference > WARM_IDLE_SECONDS:
        jobs.put(WARMUP)
    audio_ops.put(lambda: _open_stream(buf))


def _open_stream(buf):
    global stream, state, locked
    try:
        s = sd.InputStream(
            device=input_device,
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            callback=lambda data, *_: buf.append(data.copy()),
        )
        s.start()
    except sd.PortAudioError as e:
        log(f"mic open failed: {e}\n(System Settings > Privacy & Security > Microphone)")
        with recording_lock:
            current = buf is record_buf and state == "recording"
            if current:
                state = "ready"
                locked = False
        if current:
            AppHelper.callAfter(overlay.hide)
        return
    # Publishing under the same lock stop_recording holds is what keeps the
    # shutdown owned by exactly one side: either stop_recording already took
    # this recording down — it then passed s=None to _finish_recording and we
    # close the stream here — or it has yet to run and will find the stream in
    # `stream` and close it there.
    with recording_lock:
        current = buf is record_buf and state == "recording"
        if current:
            stream = s
    if not current:
        # Released before the stream finished opening — discard
        _shutdown_stream(s)


def _shutdown_stream(s):
    # close() must run even when stop() raises, or the abandoned stream can
    # keep the microphone device busy and wedge every later open
    try:
        s.stop()
    except sd.PortAudioError as e:
        log(f"mic stop failed: {e}")
    finally:
        try:
            s.close()
        except sd.PortAudioError:
            pass


def stop_recording():
    global state, stream, record_buf
    with recording_lock:
        if state != "recording":
            return
        state = "ready"
        # `stream` is still None when the open is in flight; _open_stream then
        # sees the recording is gone and closes its own stream
        s, buf = stream, record_buf
        stream = None
        record_buf = None
    # The pill stays up: the worker switches it to "Transcribing…" and hides
    # it when the paste lands. _finish_recording hides it for dropped audio,
    # and the watchdog below covers the case where the audio thread is wedged
    # in CoreAudio and _finish_recording never runs at all.
    overlay.setPhase_("transcribing")
    overlay.armWatchdog()
    audio_ops.put(lambda: _finish_recording(s, buf))


def _audio_or_drop_reason(buf):
    """Returns (audio, log line), exactly one of which is None.

    Either the recording is usable and the line describes it, or it is dropped
    and the line says why — every case ends with one log call and one hide, so
    a future rule cannot forget either.
    """
    if not buf:
        return None, "dropped: no audio captured"
    audio = np.concatenate(buf)[:, 0]
    secs = len(audio) / SAMPLE_RATE
    if secs < MIN_SECONDS:
        return None, f"dropped: {secs:.2f}s is under the {MIN_SECONDS}s minimum"
    peak = float(np.abs(audio).max())
    if peak < 1e-6:
        return None, (
            f"dropped: {secs:.1f}s of pure silence — macOS delivered no mic signal "
            "(check System Settings > Privacy & Security > Microphone)"
        )
    if peak < MIN_PEAK:
        return None, f"dropped: {secs:.1f}s too quiet to be speech (peak {peak:.3f})"
    return audio, f"recorded {secs:.1f}s on '{input_name}' (peak {peak:.3f}), transcribing..."


def _finish_recording(s, buf):
    if s is not None:
        t0 = time.monotonic()
        _shutdown_stream(s)
        if time.monotonic() - t0 > 3:
            log("audio device was slow to release — another audio app may be fighting for the mic")
    global cancel_recording
    # Consume the flag at the single point the job is queued. An earlier check
    # can be bypassed: when the key is released before the open lands,
    # _open_stream finishes the recording itself, on its own queued op.
    if cancel_recording:
        cancel_recording = False
        AppHelper.callAfter(overlay.hide)
        return
    audio, message = _audio_or_drop_reason(buf)
    log(message)
    if audio is None:
        AppHelper.callAfter(overlay.hide)
        return
    # Close an unterminated span: the user released the hotkey while still
    # holding the instruction key
    total = sum(len(chunk) for chunk in buf) if buf else 0
    spans = [[a, total if b is None else b] for a, b in instruction_spans]
    jobs.put((audio, job_generation, spans))


def schedule_deferred_stop(tap_time):
    """Stop recording unless a second tap arrives first.

    A tap alone should end the dictation, but a tap that turns out to be the
    first half of a double-tap must not tear the audio stream down — reopening
    it ~60ms later returns a stream that records silence.
    """
    def fire(_timer):
        # A newer tap or a lock superseded this one; leave the stream alone
        if locked or last_tap != tap_time:
            return
        stop_recording()

    AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
        DOUBLE_TAP_SECONDS, False, fire
    )


def handle_flags_changed(event):
    global locked, press_time, last_tap, lock_time
    keycode, device_mask, _ = HOTKEYS[settings["hotkey"]]
    instr_code, instr_mask = instruction_key()
    if event.keyCode() == instr_code and state == "recording":
        buf = record_buf
        frame = sum(len(chunk) for chunk in buf) if buf else 0
        held = bool(event.modifierFlags() & instr_mask)
        open_span = instruction_spans and instruction_spans[-1][1] is None
        if held and not open_span:
            instruction_spans.append([frame, None])
            overlay.setPhase_("instructing")
        elif not held and open_span:
            instruction_spans[-1][1] = frame
            # Back to dictating: the pill returns to the level meter so the
            # two halves are always distinguishable on screen
            overlay.setPhase_("recording")
        return
    if event.keyCode() != keycode:
        return
    now = time.monotonic()
    if settings["trigger"] == "toggle":
        # One tap starts, the next stops. Only key-down matters, so the hold
        # and double-tap timing below is skipped entirely rather than being
        # made conditional in six places.
        if event.modifierFlags() & device_mask:
            if state == "recording":
                locked = False
                stop_recording()
            else:
                start_recording()
                # Only latch if it actually started: start_recording declines
                # when the audio device is wedged, and a stale lock would then
                # make the next tap try to stop a recording that never began
                if state == "recording":
                    # Reuses the hands-free flag so the recording outlives the
                    # key release, which is the whole point of toggle mode
                    locked = True
                    lock_time = now
        return
    if event.modifierFlags() & device_mask:  # key down
        if locked:
            # A tap arriving within the grace period is the tail of the
            # double-tap that just locked, not a deliberate stop
            if now - lock_time < LOCK_GRACE_SECONDS:
                return
            locked = False
            stop_recording()
        elif state == "recording" and now - last_tap < DOUBLE_TAP_SECONDS:
            # Second tap of a double-tap arriving before the pending stop ran.
            # Leave the live stream alone; key-up decides lock vs stop.
            press_time = now
        else:
            press_time = now
            start_recording()
    else:  # key up
        if locked or state != "recording":
            return
        if now - press_time < TAP_MAX_SECONDS:
            # Double-tap: keep recording hands-free until the next tap.
            # The stream is NOT stopped between the two taps. Tearing it down
            # and reopening ~60ms later handed back a stream that captured
            # silence — PortAudio had not finished releasing the device — so
            # hands-free recorded a quiet room while the user spoke.
            # last_tap starts at 0.0, so `now - last_tap` was only small
            # enough to match on a genuine second tap — but monotonic()
            # counts from boot, meaning the very first tap after a launch
            # near boot matched too, and thereafter the stale 0.0 never
            # updated because it was assigned in the else branch only.
            # Recording it on every tap makes the window mean what it says.
            first_tap = last_tap == 0.0
            recent = not first_tap and now - last_tap < DOUBLE_TAP_SECONDS
            last_tap = now
            if recent:
                locked = True
                lock_time = now
                last_tap = 0.0  # consumed; the next tap starts a fresh pair
                log(f"hands-free recording — tap {hotkey_label()} to stop")
                return
            # First short tap: hold the stream open briefly in case a second
            # tap is coming. Stopping immediately is what forced the reopen
            # that captured silence.
            schedule_deferred_stop(now)
            return
        stop_recording()


ESCAPE_KEYCODE = 53


def cancel_pending_job():
    """Escape: drop whatever is in flight instead of pasting it.

    MLX inference is a single blocking call, so the transcription or rewrite
    already running cannot be stopped — bumping the generation makes the
    worker throw the result away when it finishes. Recording, by contrast,
    really is stopped.
    """
    global job_generation, locked, cancel_recording
    if state != "recording" and not overlay.is_working():
        return False
    job_generation += 1
    if state == "recording":
        # NOT a generation bump: _finish_recording runs later, on the audio
        # thread, and reads job_generation at that point — so any bump made
        # here is already folded in by the time it queues the job, and the
        # recording pastes anyway. The audio has to be dropped outright.
        cancel_recording = True
        locked = False
        stop_recording()
        log("cancelled the recording")
    else:
        log("cancelled — the result will be discarded")
    overlay.hide()
    return True


def handle_key_down(event):
    if event.keyCode() == ESCAPE_KEYCODE:
        return cancel_pending_job()
    return False


# NSEvent monitors instead of a CGEventTap: same job for a single modifier
# key, but gated on Accessibility only — a tap would additionally require
# the Input Monitoring permission (this is how Wispr Flow gets away with
# fewer grants). With Accessibility missing the global monitor silently
# never fires, hence the startup permission check.
def install_hotkey_monitors():
    global monitors
    monitors = [
        AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            AppKit.NSEventMaskFlagsChanged, handle_flags_changed
        ),
        AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            AppKit.NSEventMaskFlagsChanged, lambda e: (handle_flags_changed(e), e)[1]
        ),
        # Escape cancels. Global so it works while dictating into another app,
        # which is the only place a dictation is ever in flight.
        AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            AppKit.NSEventMaskKeyDown, handle_key_down
        ),
        # Locally, swallow the Escape that cancels so it does not also reach
        # the focused window; pass every other key through untouched.
        AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            AppKit.NSEventMaskKeyDown, lambda e: None if handle_key_down(e) else e
        ),
    ]


def looks_hallucinated(text):
    """True when the model has fallen into a repetition loop.

    Even above the silence floor it sometimes emits one word hundreds of
    times ("videos videos videos..."). Pasting that into the user's editor
    is worse than pasting nothing, so the worker drops it.
    """
    words = text.split()
    if len(words) < 20:
        return False
    # A genuine sentence reuses words; 400 repeats of one token does not.
    unique_ratio = len({w.lower().strip(".,!?") for w in words}) / len(words)
    return unique_ratio < 0.12


def transcribe(audio, use_dictionary=True):
    terms = read_dictionary() if use_dictionary else []
    # Whisper needed condition_on_previous_text=False to stop one 30 s
    # window's bad guess seeding the next. This model decodes the whole
    # dictation in one pass, so there is no window-to-window context to cut.
    text = asr.generate(
        audio,
        hotwords=terms or None,
        # None lets the model detect; it takes the language's name, not its code
        language=None if settings["language"] == "auto" else LANGUAGES[settings["language"]],
    ).text.strip()
    return respell(text, terms)


def ensure_rewriter():
    """Start the loader unless the model is already loaded or on its way.

    The check and the start have to be atomic: this is called from the backend
    thread at startup and from the menu and the settings window on the main
    thread, and two callers landing together each downloaded and loaded their
    own copy of a 2.3 GB model.
    """
    global rewriter_thread
    with rewriter_lock:
        if rewriter or (rewriter_thread and rewriter_thread.is_alive()):
            return
        rewriter_thread = threading.Thread(target=_load_rewriter, daemon=True)
        rewriter_thread.start()


def _load_rewriter():
    global mlx_lm, rewriter
    try:
        # Deferred import: pulls in transformers (~2s), skipped entirely when
        # rewrite stays off
        import mlx_lm
        log(f"loading rewrite model {REWRITE_REPO}@{REWRITE_REVISION[:8]} (first run downloads ~2.3 GB)...")
        t0 = time.monotonic()
        path = huggingface_hub.snapshot_download(REWRITE_REPO, revision=REWRITE_REVISION)
        model, tokenizer = mlx_lm.load(path)
        # Warmup: pays Metal kernel compilation now instead of on the first
        # real dictation
        mlx_lm.generate(model, tokenizer, prompt="hi", max_tokens=1)
        rewriter = (model, tokenizer)
        log(f"rewrite model ready in {time.monotonic() - t0:.1f}s")
    except Exception as e:  # noqa: BLE001
        # The repr alone has twice sent a debugging session down the wrong
        # path: an ImportError names the missing module, not the import that
        # went looking for it
        log(f"rewrite model failed to load: {e!r} — dictations paste unrewritten")
        log(traceback.format_exc().rstrip())


def clean_as_is_probability(text):
    """How sure the rewrite model is that Clean up would change nothing."""
    import mlx.core as mx

    model, tokenizer = rewriter
    ids = tokenizer.apply_chat_template(
        [{"role": "user", "content": CLEAN_CHECK_PROMPT.format(text=text)}],
        add_generation_prompt=True,
        enable_thinking=False,
    )
    letters = mx.array([tokenizer.encode(c, add_special_tokens=False)[0] for c in "AB"])
    logits = model(mx.array([ids]))[0, -1]
    return mx.softmax(logits[letters].astype(mx.float32))[0].item()


def rewrite(text, mode):
    """Returns the rewritten text, or None to paste the transcript as-is."""
    # Checked before the local model, not after: a cloud backend has to work
    # even when the on-device model never loaded
    backend = settings["rewrite_backend"]
    if backend != "local":
        t0 = time.monotonic()
        out = call_rewrite_api(backend, REWRITE_PROMPTS[mode].format(text=text))
        if out:
            log(f"[rewrite {mode} via {backend} {time.monotonic() - t0:.2f}s]")
            return out
    if rewriter is None:
        log("rewrite skipped: model not loaded yet — pasted the raw transcript")
        return None
    if mode == "clean":
        t0 = time.monotonic()
        # A failed check must not cost the rewrite: fall through and generate
        try:
            p = clean_as_is_probability(text)
        except Exception as e:  # noqa: BLE001
            log(f"clean check failed: {e!r} — rewriting anyway")
            p = 0.0
        if p >= SKIP_REWRITE_CONFIDENCE:
            log(f"[rewrite skipped {time.monotonic() - t0:.2f}s] already clean (p={p:.2f})")
            return None
    model, tokenizer = rewriter
    prompt = tokenizer.apply_chat_template(
        [{"role": "user", "content": REWRITE_PROMPTS[mode].format(text=text)}],
        add_generation_prompt=True,
        enable_thinking=False,  # Qwen3: answer directly, no chain-of-thought
    )
    t0 = time.monotonic()
    # A failed rewrite must never cost the user their words — fall back to
    # pasting the raw transcript
    try:
        out = mlx_lm.generate(
            model,
            tokenizer,
            prompt=prompt,
            max_tokens=2 * len(tokenizer.encode(text)) + 64,
        ).strip()
    except Exception as e:  # noqa: BLE001
        log(f"rewrite failed: {e!r} — pasted the raw transcript")
        return None
    if not out:
        log("rewrite returned nothing — pasted the raw transcript")
        return None
    log(f"[rewrite {mode} {time.monotonic() - t0:.2f}s]")
    return out


def get_api_key(backend):
    """The stored key for a backend, or "" if there is none.

    Keychain rather than settings.json: the settings file is plain JSON in
    Application Support, and an API key sitting in it is a credential leaked
    to anything that can read the user's home directory.
    """
    out = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
         "-a", backend, "-w"],
        capture_output=True, text=True, check=False,
    )
    return out.stdout.strip() if out.returncode == 0 else ""


def set_api_key(backend, key):
    if not key:
        subprocess.run(
            ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE,
             "-a", backend],
            capture_output=True, check=False,
        )
        return
    # -U updates in place rather than erroring when one already exists
    subprocess.run(
        ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE,
         "-a", backend, "-w", key],
        capture_output=True, check=False,
    )


def call_rewrite_api(backend, prompt):
    """Send one prompt to a cloud model. Returns the text, or None on failure.

    Returning None rather than raising is deliberate: every caller falls back
    to the on-device model, so a dead network or a bad key costs latency and
    quality, never the user's words.
    """
    key = get_api_key(backend)
    if not key:
        log(f"{backend}: no API key stored — using the on-device model")
        return None
    url = settings.get("api_url") or BACKEND_DEFAULTS[backend][0]
    model = settings.get("api_model") or BACKEND_DEFAULTS[backend][1]
    if not url or not model:
        log(f"{backend}: endpoint or model not set — using the on-device model")
        return None

    if backend == "anthropic":
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        body = {"model": model, "max_tokens": 2048,
                "messages": [{"role": "user", "content": prompt}]}
    else:
        # OpenAI's shape, which Groq, OpenRouter, Ollama and most proxies
        # also speak — hence one "custom" option rather than one per vendor
        headers = {"Authorization": f"Bearer {key}",
                   "content-type": "application/json"}
        body = {"model": model,
                "messages": [{"role": "user", "content": prompt}]}

    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECONDS) as r:
            data = json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        # Never log the key, and never log the response body: both can carry
        # the transcript or the credential into a file the user may share
        log(f"{backend} request failed: {type(e).__name__} — using the on-device model")
        return None
    try:
        if backend == "anthropic":
            return "".join(b.get("text", "") for b in data["content"]).strip()
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError):
        log(f"{backend}: unexpected response shape — using the on-device model")
        return None


def rewrite_with_instruction(text, instruction):
    """Rewrite the dictation the way the spoken instruction asked.

    Returns None to paste the transcript unchanged, matching rewrite().
    """
    backend = settings["rewrite_backend"]
    if backend != "local":
        t0 = time.monotonic()
        out = call_rewrite_api(
            backend, INSTRUCTION_PROMPT.format(instruction=instruction, text=text))
        if out:
            log(f'[instructed via {backend} {time.monotonic() - t0:.2f}s] "{instruction}"')
            return out
        # call_rewrite_api already said why; fall through to on-device
    if rewriter is None:
        log("instruction ignored: rewrite model not loaded — pasted as-is")
        return None
    model, tokenizer = rewriter
    prompt = tokenizer.apply_chat_template(
        [{"role": "user", "content": INSTRUCTION_PROMPT.format(
            instruction=instruction, text=text)}],
        add_generation_prompt=True,
        enable_thinking=False,
    )
    t0 = time.monotonic()
    try:
        out = mlx_lm.generate(
            model, tokenizer, prompt=prompt,
            # Room to expand: "make it a formal email" legitimately produces
            # more words than were dictated
            max_tokens=3 * len(tokenizer.encode(text)) + 128,
        ).strip()
    except Exception as e:  # noqa: BLE001
        log(f"instructed rewrite failed: {e!r} — pasted the raw transcript")
        return None
    if not out:
        log("instructed rewrite returned nothing — pasted the raw transcript")
        return None
    log(f'[instructed {time.monotonic() - t0:.2f}s] "{instruction}"')
    return out


def set_clipboard(text):
    subprocess.run("pbcopy", input=text.encode(), check=True)


# Clipboard managers (Maccy, Paste, Raycast) archive everything that lands on
# the pasteboard. These types are the community convention asking them not to:
# a dictated transcript is transient, and often private.
TRANSIENT_TYPES = (
    "org.nspasteboard.TransientType",
    "org.nspasteboard.AutoGeneratedType",
)


def secure_input_holder():
    """Best-effort name of the process holding secure input. Failure path
    only — ioreg is slow, and the name just makes the log line actionable."""
    try:
        out = subprocess.run(
            ["ioreg", "-l", "-w", "0"], capture_output=True, text=True, timeout=4, check=False
        ).stdout
        m = re.search(r'"kCGSSessionSecureInputPID"=(\d+)', out)
        if m:
            name = subprocess.run(
                ["ps", "-p", m.group(1), "-o", "comm="],
                capture_output=True, text=True, timeout=2, check=False,
            ).stdout.strip()
            return os.path.basename(name) or None
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def paste_blocked_reason():
    """Why a synthetic Cmd+V would go nowhere, or None if it should land.

    Checked per paste, not just at startup: Accessibility can be revoked
    while running, and secure input comes and goes with password prompts.
    """
    if not Quartz.CGPreflightPostEventAccess():
        return (
            "Accessibility permission is missing — enable Kaho in "
            "System Settings > Privacy & Security > Accessibility"
        )
    if _carbon.IsSecureEventInputEnabled():
        holder = secure_input_holder()
        held = f" (held by {holder})" if holder else ""
        return (
            f"secure input is active{held} — a password prompt or a terminal "
            "with Secure Keyboard Entry is blocking synthetic keystrokes"
        )
    return None


def paste(text):
    """Paste the transcript, then hand the clipboard back.

    Overwriting the clipboard on every dictation was the documented v1
    behaviour, and it is a genuine annoyance: copy something, dictate, and the
    copy is gone. Restoring is guarded by changeCount so a clipboard the user
    changed in the meantime is never clobbered.
    """
    pb = AppKit.NSPasteboard.generalPasteboard()
    previous = pb.stringForType_(AppKit.NSPasteboardTypeString)

    pb.clearContents()
    pb.declareTypes_owner_([AppKit.NSPasteboardTypeString, *TRANSIENT_TYPES], None)
    pb.setString_forType_(text, AppKit.NSPasteboardTypeString)
    change_count = pb.changeCount()

    for key_down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(None, V_KEYCODE, key_down)
        Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    if previous is None:
        return

    def restore(_timer):
        # Only restore if nothing else touched the pasteboard since our write;
        # otherwise we would overwrite whatever the user just copied.
        if pb.changeCount() != change_count:
            return
        pb.clearContents()
        pb.setString_forType_(previous, AppKit.NSPasteboardTypeString)

    # Long enough for slow apps to read the pasteboard before we put it back.
    # Restoring too early is the classic bug: the app pastes the OLD clipboard.
    AppHelper.callAfter(
        lambda: AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            CLIPBOARD_RESTORE_SECONDS, False, restore
        )
    )


# All slow work happens here: the hotkey handler runs on the main run loop,
# and blocking it would freeze the menu bar and delay key handling.
def split_audio(audio, spans):
    """Separate the dictated message from the spoken instruction.

    Returns (message, instruction), either of which may be empty. The key can
    be pressed and released repeatedly, so both halves are gathered from
    however many pieces the user produced.
    """
    if not spans:
        return audio, None
    message, instruction = [], []
    cursor = 0
    for start, end in spans:
        if start > cursor:
            message.append(audio[cursor:start])
        instruction.append(audio[start:end])
        cursor = end
    if cursor < len(audio):
        message.append(audio[cursor:])
    return (
        np.concatenate(message) if message else audio[:0],
        np.concatenate(instruction) if instruction else None,
    )


def run_job(audio, generation, spans, t0):
    """Transcribe, optionally rewrite, and paste one recording.

    Split out of worker() so the cancel path can be tested against the real
    code rather than a copy of it. `continue` in the loop becomes `return`.
    """
    global last_inference
    instruction = None
    message_audio, instruction_audio = split_audio(audio, spans)
    if instruction_audio is not None:
        if len(instruction_audio) < SAMPLE_RATE * MIN_SECONDS:
            log("instruction too short to use — pasting the dictation as-is")
        elif len(message_audio) < SAMPLE_RATE * MIN_SECONDS:
            log("nothing dictated to apply the instruction to — pasting as-is")
        else:
            # Two transcriptions, one recording: the stream stayed open across
            # every toggle, so no speech is lost at the boundaries.
            audio = message_audio
            instruction = transcribe(instruction_audio).strip()
            if not instruction:
                log("instruction was empty — pasting the dictation as-is")
    text = transcribe(audio)
    if generation != job_generation:
        # The models did run, so this still counts against idle warmup
        last_inference = time.monotonic()
        log("cancelled during transcription — nothing pasted")
        return
    if looks_hallucinated(text):
        log(f"dropped: transcription looks like a repetition loop ({len(text.split())} words)")
        AppHelper.callAfter(overlay.hide)
        return
    mode = settings["rewrite"]
    if text and instruction:
        # A spoken instruction overrides the configured mode: the user just
        # said what they want, which is more specific than a saved setting.
        AppHelper.callAfter(overlay.setPhase_, "rewriting")
        text = rewrite_with_instruction(text, instruction) or text
        if generation != job_generation:
            last_inference = time.monotonic()
            log("cancelled during rewriting — nothing pasted")
            return
    elif text and mode != "off":
        AppHelper.callAfter(overlay.setPhase_, "rewriting")
        text = rewrite(text, mode) or text
        if generation != job_generation:
            last_inference = time.monotonic()
            log("cancelled during rewriting — nothing pasted")
            return
    if text:
        reason = paste_blocked_reason()
        if reason is None:
            paste(text)
            append_history(text)
            AppHelper.callAfter(overlay.finish)
        else:
            # Leave the transcript on the clipboard (no restore) so one manual
            # Cmd+V recovers the dictation, and say so on the pill — a silent
            # no-op here reads as a dead app.
            set_clipboard(text)
            append_history(text)
            log(f"paste blocked: {reason} — transcript is on the clipboard")
            AppHelper.callAfter(overlay.blocked)
    else:
        AppHelper.callAfter(overlay.hide)
    last_inference = time.monotonic()
    log(f"[{time.monotonic() - t0:.2f}s] {text or '(empty transcription, nothing pasted)'}")


def worker():
    global last_inference
    while True:
        job = jobs.get()
        t0 = time.monotonic()
        if job is WARMUP:
            try:
                transcribe(np.zeros(SAMPLE_RATE // 2, dtype=np.float32), use_dictionary=False)
                if rewriter is not None and settings["rewrite"] != "off":
                    model, tokenizer = rewriter
                    mlx_lm.generate(model, tokenizer, prompt="hi", max_tokens=1)
                log(f"[warmup {time.monotonic() - t0:.2f}s] models paged back in")
            except Exception as e:  # noqa: BLE001
                log(f"warmup failed (harmless): {e!r}")
            last_inference = time.monotonic()
            continue
        # The sole worker must outlive any single bad job, or dictation dies
        # silently while the UI still shows ready
        try:
            run_job(*job, t0)
        except Exception as e:  # noqa: BLE001
            AppHelper.callAfter(overlay.hide)
            log(f"transcription failed: {e!r} — dictation continues")


def backend():
    global state, input_device, input_name, last_inference
    # Without this boundary a failed download/device/model init leaves the
    # menu bar stuck on "…" forever with no explanation
    global asr
    try:
        input_device, input_name = pick_input_device()
        log(f"mic: {input_name}")
        log(f"loading {MODEL_REPO}@{MODEL_REVISION[:8]} (first run downloads {MODEL_SIZE_LABEL})...")
        t0 = time.monotonic()
        asr = load_model(huggingface_hub.snapshot_download(MODEL_REPO, revision=MODEL_REVISION))
        # Warmup on silence: pays model load + Metal kernel compilation now
        # instead of on the first real dictation
        transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), use_dictionary=False)
        last_inference = time.monotonic()
        log(f"model ready in {time.monotonic() - t0:.1f}s — hold {hotkey_label()} to dictate")
        state = "ready"
        threading.Thread(target=worker, daemon=True).start()
        if settings["rewrite"] != "off":
            ensure_rewriter()
    except Exception as e:  # noqa: BLE001
        state = "error"
        log(f"startup failed: {e!r}")
        AppHelper.callAfter(startup_failed_alert, e)


def startup_failed_alert(error):
    choice = run_alert(
        "Kaho failed to start",
        f"{error}\n\nIf this was the first run, check your internet connection "
        "(the model downloads once from Hugging Face) and relaunch. Details are "
        "in the log.",
        ["Open Log", "Quit"],
    )
    if choice == 0:
        subprocess.run(["open", LOG_PATH], check=False)
    else:
        AppKit.NSApp.terminate_(None)


def run_alert(title, text, buttons):
    AppKit.NSApp.activateIgnoringOtherApps_(True)
    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(text)
    for b in buttons:
        alert.addButtonWithTitle_(b)
    # Join all Spaces and float over fullscreen apps — otherwise the alert
    # opens on another desktop and the user never sees it
    alert.window().setCollectionBehavior_(
        AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
        | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
    )
    return alert.runModal() - AppKit.NSAlertFirstButtonReturn


def prompt_missing_permissions():
    """Trigger the native macOS permission prompt, then explain the relaunch."""
    if Quartz.CGPreflightPostEventAccess():
        return
    Quartz.CGRequestPostEventAccess()
    log("missing permission: Accessibility")
    choice = run_alert(
        "Kaho needs the Accessibility permission",
        "Accessibility lets Kaho see the hotkey and paste the transcribed "
        "text.\n\nEnable Kaho in System Settings > Privacy & Security > "
        "Accessibility (it may be listed as \"Python\"), then quit Kaho from "
        "the 🎙 menu and open it again — grants only apply on a fresh launch.",
        ["Open System Settings", "Later"],
    )
    if choice == 0:
        AppKit.NSWorkspace.sharedWorkspace().openURL_(AppKit.NSURL.URLWithString_(SETTINGS_URL))


def status_item_onscreen():
    """True only if every status-bar window we own is actually on screen.

    A hidden status item reports more than one layer-25 window: one phantom
    that claims to be onscreen and the real one that does not. Returning on
    the first match therefore reported "visible" for an icon buried in the
    notch, and the Dock fallback never ran — leaving no way into the app.
    """
    pid = os.getpid()
    wins = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID)
    found = [
        w for w in (wins or [])
        if w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowLayer") == 25
    ]
    if not found:
        return None
    return all(w.get("kCGWindowIsOnscreen", False) for w in found)


OVERLAY_SIZE = (196, 36)
BAR_COUNT = 22
# Labels for the phases that run after the key is released. Without these the
# pill vanished on release and multi-second transcribe+rewrite work looked
# like nothing was happening.
PHASE_LABELS = {
    "transcribing": "Transcribing…",
    "rewriting": "Rewriting…",
    "done": "Pasted",
    "blocked": "Not pasted — ⌘V",
    "wedged": "Mic stuck — Restart Kaho",
    "instructing": "Instruction…",
}

# Secure input (password fields, Terminal's "Secure Keyboard Entry", sudo
# prompts) silently swallows synthetic keystrokes: transcription succeeds,
# the clipboard fills, and nothing appears — which reads as "Kaho is
# broken". Carbon exposes the state so we can say so instead.
_carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
_carbon.IsSecureEventInputEnabled.restype = ctypes.c_bool


class LevelView(AppKit.NSView):
    def drawRect_(self, _rect):
        bounds = self.bounds()
        mid = bounds.size.height / 2
        ticks = getattr(self, "ticks", 0)
        phase = getattr(self, "phase", "recording")
        if phase == "recording":
            # Record dot, gently pulsing
            pulse = 0.55 + 0.45 * abs(np.sin(ticks * 0.18))
            AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(
                1.0, 0.27, 0.23, pulse
            ).setFill()
            AppKit.NSBezierPath.bezierPathWithOvalInRect_(((14, mid - 4), (8, 8))).fill()
            # Waveform: flat dotted line at rest, bars rise only on speech
            levels = getattr(self, "levels", [])
            AppKit.NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.9).setFill()
            for i in range(BAR_COUNT):
                lvl = levels[i] if i < len(levels) else 0.0
                h = 2.5 + lvl * 20
                bar = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    ((30 + i * 4.6, mid - h / 2), (3, h)), 1.5, 1.5
                )
                bar.fill()
            draw_elapsed(self, bounds)
            return
        draw_phase(phase, ticks, bounds)


# Module-level, not LevelView methods: PyObjC maps every method on an NSObject
# subclass to an ObjC selector, and these arities have no valid selector name
def draw_elapsed(view, bounds):
    started = getattr(view, "started", None)
    if started is None:
        return
    secs = int(time.monotonic() - started)
    # Amber past the soft limit: a nudge to wrap up, not a hard stop —
    # accuracy holds, but very long holds are usually accidental
    color = (
        AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.72, 0.30, 0.95)
        if secs >= LONG_RECORDING_SECONDS
        else AppKit.NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.65)
    )
    label = f"{secs // 60}:{secs % 60:02d}"
    attrs = {
        AppKit.NSFontAttributeName: AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(
            11, AppKit.NSFontWeightMedium
        ),
        AppKit.NSForegroundColorAttributeName: color,
    }
    text = AppKit.NSAttributedString.alloc().initWithString_attributes_(label, attrs)
    size = text.size()
    text.drawAtPoint_(
        (bounds.size.width - size.width - 12, (bounds.size.height - size.height) / 2)
    )


def draw_phase(phase, ticks, bounds):
    # Three dots cycling left-to-right: cheap to draw, reads as "working"
    # without a spinner's implication of a known duration
    r, g, b = ((1.0, 0.72, 0.30) if phase in ("blocked", "wedged", "instructing")
               else (0.48, 0.64, 0.97))
    for i in range(3):
        alpha = 0.9 if phase in ("done", "blocked", "wedged") else 0.25 + 0.65 * (
            0.5 + 0.5 * np.sin(ticks * 0.28 - i * 0.9)
        )
        AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, alpha).setFill()
        AppKit.NSBezierPath.bezierPathWithOvalInRect_(
            ((14 + i * 11, bounds.size.height / 2 - 3), (6, 6))
        ).fill()
    attrs = {
        AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(12),
        AppKit.NSForegroundColorAttributeName: AppKit.NSColor.colorWithCalibratedWhite_alpha_(
            1.0, 0.92
        ),
    }
    text = AppKit.NSAttributedString.alloc().initWithString_attributes_(
        PHASE_LABELS.get(phase, ""), attrs
    )
    size = text.size()
    text.drawAtPoint_((52, (bounds.size.height - size.height) / 2))


class Overlay(AppKit.NSObject):
    """Floating bottom-center pill with a live mic level animation."""

    def build(self):
        size = OVERLAY_SIZE
        panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), size),
            AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        panel.setOpaque_(False)
        # The default window shadow is drawn for the square panel frame, not
        # the rounded pill inside it — on a light background it reads as a
        # faint rectangular border around the pill
        panel.setHasShadow_(False)
        panel.setBackgroundColor_(AppKit.NSColor.clearColor())
        panel.setLevel_(AppKit.NSScreenSaverWindowLevel)
        panel.setIgnoresMouseEvents_(True)
        panel.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        # Frosted-glass HUD background instead of a flat fill
        effect = AppKit.NSVisualEffectView.alloc().initWithFrame_(((0, 0), size))
        effect.setMaterial_(AppKit.NSVisualEffectMaterialHUDWindow)
        effect.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
        effect.setState_(AppKit.NSVisualEffectStateActive)
        effect.setWantsLayer_(True)
        effect.layer().setCornerRadius_(size[1] / 2)
        effect.layer().setMasksToBounds_(True)
        panel.setContentView_(effect)
        view = LevelView.alloc().initWithFrame_(((0, 0), size))
        effect.addSubview_(view)
        self.panel, self.view, self.timer = panel, view, None
        self.watchdog = None
        self.done_timer = None

    def show(self):
        # A new recording always wins: cancel anything still pending from the
        # last one, or a late hide/watchdog would tear down this recording's
        # display mid-dictation
        self.cancelWatchdog()
        if self.done_timer:
            self.done_timer.invalidate()
            self.done_timer = None
        screen = AppKit.NSScreen.mainScreen().frame()
        w, h = OVERLAY_SIZE
        x = screen.origin.x + (screen.size.width - w) / 2
        self.panel.setFrame_display_(((x, screen.origin.y + 110), (w, h)), True)
        self.view.levels = []
        self.view.phase = "recording"
        self.view.started = time.monotonic()
        self.rms_history = collections.deque(maxlen=30)
        self.displayed = 0.0
        self.panel.orderFrontRegardless()
        self.startTimer()

    def startTimer(self):
        if self.timer:
            self.timer.invalidate()
        self.timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.07, self, "tick:", None, True
        )

    def setPhase_(self, phase):
        """Switch the pill to a post-release phase, reviving it if hidden.

        Called from the worker thread via AppHelper.callAfter, so it must
        tolerate arriving after hide() — a fast dictation can finish before
        the phase change is delivered.
        """
        self.view.phase = phase
        self.view.setNeedsDisplay_(True)
        if not self.panel.isVisible():
            self.panel.orderFrontRegardless()
        if not self.timer:
            self.startTimer()

    def armWatchdog(self):
        """Hide the pill if the pipeline never reports back.

        A CoreAudio deadlock blocks the audio thread inside PortAudio's
        stop/close, so _finish_recording never runs and nothing else would
        ever take the pill down. A stuck overlay floating over every app is
        worse than losing the progress display.
        """
        self.cancelWatchdog()
        self.watchdog = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            PIPELINE_TIMEOUT_SECONDS, self, "watchdogFired:", None, False
        )

    def cancelWatchdog(self):
        wd = getattr(self, "watchdog", None)
        if wd:
            wd.invalidate()
        self.watchdog = None

    def watchdogFired_(self, _timer):
        self.watchdog = None
        if self.panel.isVisible():
            log("overlay timed out waiting for the pipeline — hiding it")
            self.hide()

    def blocked(self):
        """Warn that the transcript did not paste. Lingers longer than the
        "Pasted" flash — this one the user genuinely needs to read."""
        self.setPhase_("blocked")
        if self.done_timer:
            self.done_timer.invalidate()
        self.done_timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            3.5, self, "hideTimer:", None, False
        )

    def is_working(self):
        """True while a dictation is in flight and could still be cancelled."""
        return self.panel.isVisible() and getattr(self.view, "phase", None) in (
            "transcribing",
            "rewriting",
        )

    def show_wedged(self):
        """Report a stuck audio device, and stay up until it is dealt with.

        Unlike every other phase this one does not time out: the device stays
        broken until the app is relaunched, so a pill that faded away would
        just let the next dictation fail silently too.
        """
        self.cancelWatchdog()
        if self.done_timer:
            self.done_timer.invalidate()
            self.done_timer = None
        self.panel.orderFrontRegardless()
        self.setPhase_("wedged")

    def finish(self):
        """Flash 'Pasted' briefly, then hide — a silent disappearance makes a
        failed dictation and a successful one look identical."""
        self.setPhase_("done")
        # Retained: an unreferenced NSTimer can be collected before it fires,
        # which left the pill stuck on "Pasted" forever — and a stuck pill
        # also swallowed the next recording's animation.
        if self.done_timer:
            self.done_timer.invalidate()
        self.done_timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.45, self, "hideTimer:", None, False
        )

    def hideTimer_(self, _timer):
        self.done_timer = None
        self.hide()

    def hide(self):
        self.cancelWatchdog()
        if self.done_timer:
            self.done_timer.invalidate()
            self.done_timer = None
        if self.timer:
            self.timer.invalidate()
            self.timer = None
        self.panel.orderOut_(None)

    def tick_(self, _timer):
        if getattr(self.view, "phase", "recording") != "recording":
            self.view.ticks = getattr(self.view, "ticks", 0) + 1
            self.view.setNeedsDisplay_(True)
            return
        rms = 0.0
        buf = record_buf
        if buf:
            chunk = buf[-1]
            rms = float(np.sqrt((chunk**2).mean()))
        self.rms_history.append(rms)
        # Noise gate with an absolute margin: the floor is the quietest recent
        # level, and nothing moves until rms clears floor*2 + 0.004. Ambient
        # room noise therefore draws a flat dotted line; only speech animates.
        # (Pure min/max normalization amplified silence-level jitter.)
        floor = sorted(self.rms_history)[max(0, len(self.rms_history) // 5)]
        gate = floor * 2.0 + 0.004
        if rms <= gate:
            target = 0.0
        else:
            ceiling = max(max(self.rms_history), gate + 0.03)
            target = min(1.0, (rms - gate) / (ceiling - gate))
        # Fast attack, slow decay reads as speech rather than jitter
        if target > self.displayed:
            self.displayed = 0.5 * self.displayed + 0.5 * target
        else:
            self.displayed = 0.75 * self.displayed + 0.25 * target
        if self.displayed < 0.04:
            self.displayed = 0.0
        self.view.ticks = getattr(self.view, "ticks", 0) + 1
        self.view.levels = (getattr(self.view, "levels", []) + [self.displayed])[-BAR_COUNT:]
        self.view.setNeedsDisplay_(True)


class SettingsWindow(AppKit.NSObject):
    """Real window for hotkey and rewrite mode.

    The menu bar item carries the same settings, but it is unreachable when
    macOS hides the status item behind the notch on a crowded menu bar — which
    is the normal case on this machine. A window can always be opened by
    relaunching the app.
    """

    def show(self):
        if not getattr(self, "window", None):
            self.buildWindow()
        self.syncControls()
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)

    def buildWindow(self):
        mask = (
            AppKit.NSWindowStyleMaskTitled
            | AppKit.NSWindowStyleMaskClosable
            | AppKit.NSWindowStyleMaskMiniaturizable
        )
        window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (460, 492)), mask, AppKit.NSBackingStoreBuffered, False
        )
        window.setTitle_("Kaho Settings")
        window.setReleasedWhenClosed_(False)
        window.center()
        content = window.contentView()

        content.addSubview_(make_label("Hotkey", 24, 444, 13, bold=True))
        self.hotkey_popup = AppKit.NSPopUpButton.alloc().initWithFrame_pullsDown_(
            ((24, 412), (412, 26)), False
        )
        for name, (_, _, label) in HOTKEYS.items():
            self.hotkey_popup.addItemWithTitle_(label)
            self.hotkey_popup.lastItem().setRepresentedObject_(name)
        self.hotkey_popup.setTarget_(self)
        self.hotkey_popup.setAction_("hotkeyChanged:")
        content.addSubview_(self.hotkey_popup)
        content.addSubview_(
            make_label("Right-side keys only: the left ones are for typing.",
                       24, 390, 11, dim=True)
        )

        content.addSubview_(make_label("Trigger", 24, 356, 13, bold=True))
        self.trigger_popup = AppKit.NSPopUpButton.alloc().initWithFrame_pullsDown_(
            ((24, 324), (412, 26)), False
        )
        for mode, label in TRIGGERS.items():
            self.trigger_popup.addItemWithTitle_(label)
            self.trigger_popup.lastItem().setRepresentedObject_(mode)
        self.trigger_popup.setTarget_(self)
        self.trigger_popup.setAction_("triggerChanged:")
        content.addSubview_(self.trigger_popup)

        content.addSubview_(make_label("Language", 24, 302, 13, bold=True))
        self.language_popup = AppKit.NSPopUpButton.alloc().initWithFrame_pullsDown_(
            ((24, 270), (412, 26)), False
        )
        for code, label in LANGUAGES.items():
            self.language_popup.addItemWithTitle_(label)
            self.language_popup.lastItem().setRepresentedObject_(code)
        self.language_popup.setTarget_(self)
        self.language_popup.setAction_("languageChanged:")
        content.addSubview_(self.language_popup)
        content.addSubview_(
            make_label(
                "Pick yours if detection gets it wrong on short dictations.",
                24, 252, 11, dim=True,
            )
        )

        content.addSubview_(make_label("Rewrite", 24, 224, 13, bold=True))
        self.rewrite_popup = AppKit.NSPopUpButton.alloc().initWithFrame_pullsDown_(
            ((24, 192), (412, 26)), False
        )
        for mode, label in REWRITE_MODES.items():
            self.rewrite_popup.addItemWithTitle_(label)
            self.rewrite_popup.lastItem().setRepresentedObject_(mode)
        self.rewrite_popup.setTarget_(self)
        self.rewrite_popup.setAction_("rewriteChanged:")
        content.addSubview_(self.rewrite_popup)

        self.hint = make_label("", 24, 148, 11, dim=True)
        self.hint.setFrame_(((24, 140), (412, 44)))
        # Hints run two lines for the longer modes
        self.hint.cell().setWraps_(True)
        content.addSubview_(self.hint)

        self.status = make_label("", 24, 96, 11, dim=True)
        self.status.setFrame_(((24, 88), (412, 34)))
        self.status.cell().setWraps_(True)
        content.addSubview_(self.status)

        self.dict_button = AppKit.NSButton.alloc().initWithFrame_(((24, 48), (200, 26)))
        self.dict_button.setTitle_("Edit Dictionary…")
        self.dict_button.setBezelStyle_(AppKit.NSBezelStyleRounded)
        self.dict_button.setTarget_(self)
        self.dict_button.setAction_("openDictionary:")
        content.addSubview_(self.dict_button)
        content.addSubview_(
            make_label(
                "Names and jargon to spell your way.", 232, 52, 11, dim=True
            )
        )

        self.notice = make_label("", 24, 16, 11, dim=True)
        self.notice.setFrame_(((24, 8), (412, 30)))
        self.notice.cell().setWraps_(True)
        content.addSubview_(self.notice)
        self.window = window

    def syncControls(self):
        for i in range(self.hotkey_popup.numberOfItems()):
            if self.hotkey_popup.itemAtIndex_(i).representedObject() == settings["hotkey"]:
                self.hotkey_popup.selectItemAtIndex_(i)
        for i in range(self.trigger_popup.numberOfItems()):
            if self.trigger_popup.itemAtIndex_(i).representedObject() == settings["trigger"]:
                self.trigger_popup.selectItemAtIndex_(i)
        for i in range(self.language_popup.numberOfItems()):
            if self.language_popup.itemAtIndex_(i).representedObject() == settings["language"]:
                self.language_popup.selectItemAtIndex_(i)
        for i in range(self.rewrite_popup.numberOfItems()):
            if self.rewrite_popup.itemAtIndex_(i).representedObject() == settings["rewrite"]:
                self.rewrite_popup.selectItemAtIndex_(i)
        self.refreshHint()

    def refreshHint(self):
        if status_item_onscreen() is False:
            self.notice.setStringValue_(
                "Your menu bar is full, so macOS hides Kaho's icon behind the "
                "notch — reach Kaho from the Dock instead. Everything runs on "
                "this Mac."
            )
        else:
            self.notice.setStringValue_(
                "Everything runs on this Mac. Audio never leaves the device."
            )
        mode = settings["rewrite"]
        self.hint.setStringValue_(REWRITE_HINTS.get(mode, ""))
        if mode == "off":
            self.status.setStringValue_("")
        elif rewriter is not None:
            self.status.setStringValue_(f"Rewrite model loaded ({REWRITE_SIZE_LABEL}).")
        else:
            self.status.setStringValue_(
                f"Loading the rewrite model ({REWRITE_SIZE_LABEL} on first use). "
                "Dictations paste unrewritten until it is ready."
            )

    def openDictionary_(self, _sender):
        open_dictionary()

    def hotkeyChanged_(self, sender):
        apply_hotkey(sender.selectedItem().representedObject())

    def triggerChanged_(self, sender):
        apply_trigger(sender.selectedItem().representedObject())

    def languageChanged_(self, sender):
        apply_language(sender.selectedItem().representedObject())

    def rewriteChanged_(self, sender):
        apply_rewrite(sender.selectedItem().representedObject())


def make_label(text, x, y, size, bold=False, dim=False):
    field = AppKit.NSTextField.alloc().initWithFrame_(((x, y), (412, 18)))
    field.setStringValue_(text)
    field.setBezeled_(False)
    field.setDrawsBackground_(False)
    field.setEditable_(False)
    field.setSelectable_(False)
    font = (
        AppKit.NSFont.boldSystemFontOfSize_(size)
        if bold
        else AppKit.NSFont.systemFontOfSize_(size)
    )
    field.setFont_(font)
    if dim:
        field.setTextColor_(AppKit.NSColor.secondaryLabelColor())
    return field


class HistoryWindow(AppKit.NSObject):
    """Scrollable read-only window with every transcription ever made."""

    def show(self):
        if not getattr(self, "window", None):
            self.buildWindow()
        self.text_view.setString_(self.renderText())
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)

    def buildWindow(self):
        mask = (
            AppKit.NSWindowStyleMaskTitled
            | AppKit.NSWindowStyleMaskClosable
            | AppKit.NSWindowStyleMaskResizable
            | AppKit.NSWindowStyleMaskMiniaturizable
        )
        window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (480, 560)), mask, AppKit.NSBackingStoreBuffered, False
        )
        window.setTitle_("Kaho History")
        window.setReleasedWhenClosed_(False)
        window.center()
        scroll = AppKit.NSScrollView.alloc().initWithFrame_(window.contentView().bounds())
        scroll.setHasVerticalScroller_(True)
        scroll.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        tv = AppKit.NSTextView.alloc().initWithFrame_(scroll.bounds())
        tv.setEditable_(False)
        # Explicit: read-only must not mean unselectable, or there is no way
        # to get a transcript out of this window
        tv.setSelectable_(True)
        tv.setFont_(AppKit.NSFont.systemFontOfSize_(13))
        tv.setTextContainerInset_((14, 14))
        tv.setAutoresizingMask_(AppKit.NSViewWidthSizable)
        tv.setVerticallyResizable_(True)
        tv.textContainer().setWidthTracksTextView_(True)
        scroll.setDocumentView_(tv)
        window.setContentView_(scroll)
        self.window, self.text_view = window, tv

    def renderText(self):
        entries = read_history_file()
        if not entries:
            return f"No transcriptions yet.\n\nHold {hotkey_label()}, speak, release."
        blocks = []
        for epoch, text in reversed(entries):
            stamp = time.strftime("%b %d, %H:%M", time.localtime(epoch))
            blocks.append(f"{stamp}\n{text}")
        return "\n\n".join(blocks)


# Module-level, not a StatusItem method: PyObjC maps method names to ObjC
# selectors, and a 4-argument method without matching underscores is rejected
# at class creation with BadPrototypeError
def show_dock_icon():
    """Promote the accessory app to a regular one, giving it a Dock icon and
    an app menu — the only reachable UI when the status item is hidden."""
    AppKit.NSApp.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
    apply_app_icon()
    install_app_menu()


def set_process_name():
    """Make the app menu say "Kaho", not "Python".

    macOS titles the app menu from the running executable's bundle — here
    Homebrew's Python.app — so it must be overridden in that bundle's info
    dictionary before the menu is built.
    """
    try:
        bundle = AppKit.NSBundle.mainBundle()
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        info["CFBundleName"] = "Kaho"
    except Exception as e:  # noqa: BLE001
        log(f"could not set the app menu name: {e!r}")


def apply_app_icon():
    """Set the Dock icon explicitly.

    The process runs out of Homebrew's Python.app, so macOS shows the Python
    rocket rather than Kaho's icon — the .icns in our bundle is never
    consulted for a process whose executable lives elsewhere.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    # Installed: Resources/Kaho.icns beside this file. From the repo
    # (run.sh): assets/Kaho.icns.
    for icns in (
        os.path.join(here, "Kaho.icns"),
        os.path.join(here, "assets", "Kaho.icns"),
    ):
        image = AppKit.NSImage.alloc().initWithContentsOfFile_(icns)
        if image:
            AppKit.NSApp.setApplicationIconImage_(image)
            return
    log("could not load the Dock icon — falling back to the Python icon")


def install_app_menu():
    """Minimal app menu: macOS renders an empty bar for a promoted accessory
    app otherwise, and ⌘Q would not work."""
    set_process_name()
    main_menu = AppKit.NSMenu.alloc().init()
    app_item = AppKit.NSMenuItem.alloc().init()
    main_menu.addItem_(app_item)
    # The submenu's own title is what macOS renders in bold as the app menu
    app_menu = AppKit.NSMenu.alloc().initWithTitle_("Kaho")
    for title, action, key in MENU_ACTIONS:
        if action in APP_MENU_OMITS:
            continue
        if action == "quit:":
            app_menu.addItem_(AppKit.NSMenuItem.separatorItem())
            # NSApp's own selector, so ⌘Q works with no target of ours
            action = "terminate:"
        item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
        if action != "terminate:":
            item.setTarget_(status_item)
        app_menu.addItem_(item)
    app_item.setSubmenu_(app_menu)

    # macOS routes ⌘C/⌘V/⌘X/⌘A through the menu bar, so without an Edit menu
    # they do nothing anywhere in the app — copying out of History needed a
    # right-click, and the dictionary editor could not be pasted into.
    # Targets stay nil on purpose: AppKit then sends each action down the
    # responder chain to whatever view has focus.
    edit_item = AppKit.NSMenuItem.alloc().init()
    main_menu.addItem_(edit_item)
    edit_menu = AppKit.NSMenu.alloc().initWithTitle_("Edit")
    for title, action, key in (
        ("Undo", "undo:", "z"),
        ("Redo", "redo:", "Z"),
        (None, None, None),
        ("Cut", "cut:", "x"),
        ("Copy", "copy:", "c"),
        ("Paste", "paste:", "v"),
        ("Select All", "selectAll:", "a"),
    ):
        if title is None:
            edit_menu.addItem_(AppKit.NSMenuItem.separatorItem())
            continue
        edit_menu.addItem_(
            AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
        )
    edit_item.setSubmenu_(edit_menu)

    AppKit.NSApp.setMainMenu_(main_menu)


# One table for both menus. The status item shows all of it; the app menu drops
# Open Log and quits through NSApp's own terminate: so ⌘Q works without a
# target. Two hand-maintained copies drifted twice, which is why
# tools/check_docs_sync.py reads this list.
MENU_ACTIONS = (
    ("Settings…", "showSettings:", ","),
    ("Edit Dictionary…", "editDictionary:", ""),
    ("History…", "showHistory:", "h"),
    ("Open Log", "openLog:", ""),
    ("Report a Bug…", "reportBug:", ""),
    ("Restart Kaho", "restart:", ""),
    ("Quit Kaho", "quit:", "q"),
)
APP_MENU_OMITS = ("openLog:",)


def open_dictionary():
    ensure_dictionary_file()
    subprocess.run(["open", "-t", DICTIONARY_PATH], check=False)


def refresh_settings_ui():
    """Both surfaces show the same settings, so a change on either refreshes
    the other — the menu's checkmarks and the window's popups."""
    rebuild_status_menu()
    if settings_win and getattr(settings_win, "window", None):
        settings_win.syncControls()


def apply_hotkey(name):
    settings["hotkey"] = name
    save_settings()
    log(f"hotkey: {hotkey_label()}")
    refresh_settings_ui()


def apply_trigger(mode):
    global locked
    settings["trigger"] = mode
    save_settings()
    # A mode switch mid-recording would strand the lock in the other mode's
    # meaning, so end any recording in flight first
    if state == "recording":
        locked = False
        stop_recording()
    log(f"trigger: {TRIGGERS[mode]}")
    refresh_settings_ui()


def apply_rewrite_backend(name):
    """Switch the rewrite model, prompting for a key the first time."""
    settings["rewrite_backend"] = name
    if name != "local":
        settings["api_url"], settings["api_model"] = BACKEND_DEFAULTS[name]
    save_settings()
    log(f"rewrite model: {REWRITE_BACKENDS[name]}")
    if name != "local" and not get_api_key(name):
        prompt_for_api_key(name)
    refresh_settings_ui()


def prompt_for_api_key(backend):
    """Ask for the key in a secure field, so it is never on screen or on disk.

    NSSecureTextField rather than a text file: the whole reason the key lives
    in the Keychain is that a plaintext copy in Application Support is a
    credential anything can read.
    """
    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_(f"{REWRITE_BACKENDS[backend]} API key")
    alert.setInformativeText_(
        "Stored in your Keychain, never in Kaho's settings file.\n\n"
        "Your dictated text is sent to this service to be rewritten. Audio "
        "always stays on this Mac. Leave empty to keep using the on-device "
        "model."
    )
    field = AppKit.NSSecureTextField.alloc().initWithFrame_(((0, 0), (300, 24)))
    alert.setAccessoryView_(field)
    alert.addButtonWithTitle_("Save")
    alert.addButtonWithTitle_("Cancel")
    # Same as ask(): without this the alert can open on another Space and the
    # user never sees it. Safe to run modally here because this is reached
    # from a menu click while idle — runModal starves NSTimers, which is why
    # nothing on the recording path may ever show an alert.
    alert.window().setCollectionBehavior_(
        AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
        | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
    )
    AppKit.NSApp.activateIgnoringOtherApps_(True)
    if alert.runModal() == AppKit.NSAlertFirstButtonReturn:
        key = field.stringValue().strip()
        set_api_key(backend, key)
        if key:
            log(f"{backend}: API key saved to the Keychain")
            return
    # No key: a cloud backend without one silently falls back on every
    # dictation, which reads as the setting not working
    settings["rewrite_backend"] = "local"
    save_settings()
    log("no key given — staying on the on-device model")


def apply_language(code):
    settings["language"] = code
    save_settings()
    log(f"language: {LANGUAGES[code]}")
    refresh_settings_ui()


def apply_rewrite(mode):
    settings["rewrite"] = mode
    save_settings()
    if mode != "off":
        ensure_rewriter()
    refresh_settings_ui()


def rebuild_status_menu():
    """Refresh the menu's checkmarks after a settings window change."""
    if status_item:
        status_item.rebuildMenu()


def build_submenu(target, title, entries, selected, action):
    parent = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
    sub = AppKit.NSMenu.alloc().init()
    for label, value in entries:
        entry = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(label, action, "")
        entry.setTarget_(target)
        entry.setRepresentedObject_(value)
        entry.setState_(
            AppKit.NSControlStateValueOn if value == selected else AppKit.NSControlStateValueOff
        )
        sub.addItem_(entry)
    parent.setSubmenu_(sub)
    return parent


class StatusItem(AppKit.NSObject):
    def refresh_(self, _timer):
        button = self.item.button()
        if button.title() != TITLES[state]:
            button.setTitle_(TITLES[state])
        if self.menu_version != history_version:
            self.menu_version = history_version
            self.rebuildMenu()
        self.ticks += 1
        # `is not True` rather than `is False`: status_item_onscreen() returns
        # None when CGWindowListCopyWindowInfo tells us nothing, which happens
        # without Screen Recording permission. Treating unknown as "visible"
        # left users with a hidden status item AND no Dock icon — no way into
        # the app at all. An unnecessary Dock icon is the harmless failure.
        if self.ticks == 10 and status_item_onscreen() is not True:
            log("menu bar icon is hidden behind the notch — showing a Dock icon instead")
            # A hidden status item leaves no way in, so fall back to a Dock
            # icon: that gives a clickable target and a real app menu. An
            # accessory app has neither by default.
            #
            # Deliberately NOT an alert. runModal() spins a nested run loop
            # that starves every NSTimer in the process, so an unnoticed alert
            # froze the recording overlay mid-dictation — and it fired on
            # every launch, since a full menu bar is a permanent condition.
            show_dock_icon()

    def rebuildMenu(self):
        menu = AppKit.NSMenu.alloc().init()
        if history:
            for stamp, text in history:
                label = text if len(text) <= 60 else text[:57] + "…"
                entry = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    f"{stamp}  {label}", "copyTranscript:", ""
                )
                entry.setTarget_(self)
                entry.setRepresentedObject_(text)
                entry.setToolTip_("Click to copy")
                menu.addItem_(entry)
        else:
            placeholder = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "No transcriptions yet", None, ""
            )
            placeholder.setEnabled_(False)
            menu.addItem_(placeholder)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        menu.addItem_(
            build_submenu(
                self,
                "Hotkey",
                [(label, name) for name, (_, _, label) in HOTKEYS.items()],
                settings["hotkey"],
                "setHotkey:",
            )
        )
        menu.addItem_(
            build_submenu(
                self, "Rewrite model",
                [(l, b) for b, l in REWRITE_BACKENDS.items()],
                settings["rewrite_backend"], "setRewriteBackend:",
            )
        )
        menu.addItem_(
            build_submenu(
                self, "Trigger", [(l, m) for m, l in TRIGGERS.items()],
                settings["trigger"], "setTrigger:",
            )
        )
        menu.addItem_(
            build_submenu(
                self, "Language", [(l, c) for c, l in LANGUAGES.items()],
                settings["language"], "setLanguage:",
            )
        )
        menu.addItem_(
            build_submenu(
                self, "Rewrite", [(l, m) for m, l in REWRITE_MODES.items()],
                settings["rewrite"], "setRewrite:",
            )
        )
        for title, action, key in MENU_ACTIONS:
            entry = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
            entry.setTarget_(self)
            menu.addItem_(entry)
        self.item.setMenu_(menu)

    def setHotkey_(self, sender):
        apply_hotkey(sender.representedObject())

    def setRewriteBackend_(self, sender):
        apply_rewrite_backend(sender.representedObject())

    def setTrigger_(self, sender):
        apply_trigger(sender.representedObject())

    def setLanguage_(self, sender):
        apply_language(sender.representedObject())

    def setRewrite_(self, sender):
        apply_rewrite(sender.representedObject())

    def copyTranscript_(self, sender):
        set_clipboard(sender.representedObject())

    def showSettings_(self, _sender):
        settings_win.show()

    def showHistory_(self, _sender):
        history_win.show()

    def openLog_(self, _sender):
        subprocess.run(["open", LOG_PATH], check=False)

    def editDictionary_(self, _sender):
        open_dictionary()

    def reportBug_(self, _sender):
        body = (
            "Describe the bug — what did you do, what did you expect, what "
            "happened instead?\n\n\n"
            "If the issue is visual, attach a screenshot (press ⇧⌘4).\n\n"
            "--- diagnostics (keep this section) ---\n"
            f"Kaho {APP_VERSION} · macOS {platform.mac_ver()[0]} · "
            f"Python {platform.python_version()}\n"
            f"mic: {input_name} · state: {state}\n"
            f"hotkey: {hotkey_label()} · rewrite: {settings['rewrite']}\n"
            f"speech model: {MODEL_REPO}@{MODEL_REVISION[:8]}\n"
            f"rewrite model: {REWRITE_REPO}@{REWRITE_REVISION[:8]} "
            f"(loaded: {rewriter is not None})\n\n"
            "The attached Kaho.log includes recent transcripts — delete "
            "anything private before sending.\n"
        )
        service = AppKit.NSSharingService.sharingServiceNamed_(
            AppKit.NSSharingServiceNameComposeEmail
        )
        if service:
            service.setRecipients_([BUG_REPORT_EMAIL])
            service.setSubject_(f"Kaho bug report ({APP_VERSION})")
            items = [body]
            if os.path.exists(LOG_PATH):
                items.append(AppKit.NSURL.fileURLWithPath_(LOG_PATH))
            service.performWithItems_(items)
        else:
            # No Mail.app account: fall back to a mailto: draft in the default
            # mail handler (no attachment — mailto can't carry one; the body
            # asks for the log instead)
            body += f"\nPlease also attach {LOG_PATH}\n"
            url = (
                f"mailto:{BUG_REPORT_EMAIL}"
                f"?subject={urllib.parse.quote(f'Kaho bug report ({APP_VERSION})')}"
                f"&body={urllib.parse.quote(body)}"
            )
            AppKit.NSWorkspace.sharedWorkspace().openURL_(AppKit.NSURL.URLWithString_(url))

    def restart_(self, _sender):
        relaunch()

    def quit_(self, _sender):
        AppKit.NSApp.terminate_(None)


class AppDelegate(AppKit.NSObject):
    # Launching Kaho again while it runs (Launchpad, Finder, `open`) lands
    # here — show the history window, since the menu bar icon can be hidden
    # behind the notch on a crowded menu bar
    def applicationShouldHandleReopen_hasVisibleWindows_(self, _app, _has_windows):
        history_win.show()
        return False


def install_status_item():
    delegate = StatusItem.alloc().init()
    item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
        AppKit.NSVariableStatusItemLength
    )
    item.button().setTitle_(TITLES[state])
    delegate.item = item
    delegate.menu_version = -1  # forces the first rebuildMenu from refresh_
    delegate.ticks = 0
    timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        0.3, delegate, "refresh:", None, True
    )
    log(f"status item installed (visible: {not item.button().isHidden()})")
    return delegate, item, timer


def migrate_sotto_support_dir():
    """Carry settings, history and the dictionary across the 2.0 rename.

    The app was named Sotto through 1.7.x. install.sh does the same before it
    builds the venv; this covers the DMG, where the app is the first thing to
    run under the new name. Declines when the new directory already exists —
    the user's current data always wins over an old leftover.
    """
    old_support = os.path.expanduser("~/Library/Application Support/Sotto")
    if not os.path.isdir(old_support) or os.path.exists(SUPPORT_DIR):
        return False
    try:
        os.rename(old_support, SUPPORT_DIR)
    except OSError as e:
        log(f"could not migrate old Sotto data: {e}")
        return False
    # A source-install venv in there refers to the old path throughout
    subprocess.run(["rm", "-rf", os.path.join(SUPPORT_DIR, "venv")], check=False)
    log("migrated settings and history from the Sotto era")
    return True


def main():
    global overlay, history_win, settings_win, status_item
    migrate_sotto_support_dir()
    os.makedirs(SUPPORT_DIR, exist_ok=True)
    ensure_dictionary_file()
    # Migrate transcript files created by older versions to private mode;
    # _private_opener only covers newly created files
    for path in (LOG_PATH, HISTORY_PATH):
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    # Before sharedApplication(): AppKit reads the bundle name once while
    # building its menus, so a later override can arrive too late
    set_process_name()
    app = AppKit.NSApplication.sharedApplication()
    # Accessory: menu-bar only. Without this the process inherits Python.app's
    # bundle identity and takes over the app menu as "Python".
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    load_settings()
    load_history()
    refs = install_status_item()  # tuple keeps the AppKit objects alive
    status_item = refs[0]
    overlay = Overlay.alloc().init()
    overlay.build()
    history_win = HistoryWindow.alloc().init()
    settings_win = SettingsWindow.alloc().init()
    threading.Thread(target=audio_control, daemon=True).start()
    install_hotkey_monitors()
    prompt_missing_permissions()
    threading.Thread(target=backend, daemon=True).start()
    AppHelper.runEventLoop()


if __name__ == "__main__":
    # A frozen bundle re-executes its own binary to create worker processes.
    # Without this the child re-runs main() instead, so the app launched twice
    # — two menu bar items, two model loads. Harmless when not frozen.
    multiprocessing.freeze_support()
    main()
