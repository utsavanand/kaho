"""Transcript cleanup and scoring. Pure functions, tested on any OS."""

import re

# Qwen3-ASR can emit its language tag ahead of the text; llama.cpp's build
# always does (issue #26749). Pasting it would put "language English" into
# the user's document.
_LANG_PREFIX = re.compile(r"^\s*language\s+[A-Za-z]+\s*<asr_text>\s*", re.IGNORECASE)
# CJK ideographs in output for an English utterance: a hotwords prompt can
# tip the 0.6B model into Chinese ("卡荷" for "Kaho"), which the benchmark
# reports separately rather than burying in WER.
_CJK = re.compile(r"[㐀-鿿豈-﫿]")


def clean_transcript(text):
    return _LANG_PREFIX.sub("", text).strip()


def has_cjk(text):
    return bool(_CJK.search(text))


def normalize(text):
    """Lowercase words for WER: punctuation out, apostrophes kept, hyphens split."""
    text = text.lower().replace("-", " ")
    text = re.sub(r"[^\w\s']", " ", text)
    return [w.strip("'") for w in text.split() if w.strip("'")]


def word_errors(reference, hypothesis):
    """(edits, reference_length) by word-level Levenshtein distance."""
    ref, hyp = normalize(reference), normalize(hypothesis)
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1], len(ref)


def term_hits(terms, hypothesis):
    """How many dictionary terms appear spelled exactly (case-insensitive, whole word)."""
    return sum(
        1 for t in terms if re.search(rf"(?<![\w-]){re.escape(t)}(?![\w-])", hypothesis, re.IGNORECASE)
    )
