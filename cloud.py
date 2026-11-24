"""Cloud providers for a user's own API key: rewriting and transcription.

Kept out of kaho.py so it can be tested without AppKit or the models. Every
network call raises CloudError on failure; kaho.py catches it and falls back
to the on-device model, so a dead network or a bad key costs quality and a
little time, never the user's words.

"Auto" picks the model from the ones the key can actually use, by asking
the provider for its list. A hardcoded model goes stale — the 2.x builds
asked for gpt-5 and claude-sonnet-5 long after newer ones shipped — and a
user's key may not have access to whichever one is hardcoded. The ranking
wants the best model that still answers in a second or two: a rewrite sits
between the user letting go of the key and the paste, so the flagship that
takes ten seconds is the wrong choice even though it writes slightly better.
"""
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave

import numpy as np

AUTO = "auto"

# name -> label, what it can do, base URL, where to get a key
PROVIDERS = {
    "openai": {"label": "OpenAI", "kinds": ("rewrite", "stt"),
               "base": "https://api.openai.com/v1",
               "keys": "https://platform.openai.com/api-keys"},
    "anthropic": {"label": "Anthropic", "kinds": ("rewrite",),
                  "base": "https://api.anthropic.com/v1",
                  "keys": "https://console.anthropic.com/settings/keys"},
    "gemini": {"label": "Google Gemini", "kinds": ("rewrite",),
               # The OpenAI-compatible surface: one request shape for both
               "base": "https://generativelanguage.googleapis.com/v1beta/openai",
               "keys": "https://aistudio.google.com/apikey"},
    "groq": {"label": "Groq", "kinds": ("rewrite", "stt"),
             "base": "https://api.groq.com/openai/v1",
             "keys": "https://console.groq.com/keys"},
    "openrouter": {"label": "OpenRouter", "kinds": ("rewrite",),
                   "base": "https://openrouter.ai/api/v1",
                   "keys": "https://openrouter.ai/settings/keys"},
    "deepgram": {"label": "Deepgram", "kinds": ("stt",),
                 "base": "https://api.deepgram.com/v1",
                 "keys": "https://console.deepgram.com/"},
    "custom": {"label": "Custom (OpenAI-compatible)", "kinds": ("rewrite", "stt"),
               "base": "", "keys": ""},
}

REWRITE_TIMEOUT = 10
# Longer: the request carries the audio, and a minute of it is a few MB
STT_TIMEOUT = 20
VERIFY_TIMEOUT = 10


class CloudError(Exception):
    """A request that failed. The message is safe to log: it never carries
    the key, the transcript or the response body."""


def providers_for(kind):
    return [name for name, p in PROVIDERS.items() if kind in p["kinds"]]


def base_url(provider, custom_url=""):
    if provider == "custom":
        # Older settings stored the full chat URL; the base is what's needed
        url = (custom_url or "").strip().rstrip("/")
        for suffix in ("/chat/completions", "/audio/transcriptions"):
            url = url.removesuffix(suffix)
        return url
    return PROVIDERS[provider]["base"]


def _headers(provider, key, json_body=True):
    if provider == "anthropic":
        h = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    elif provider == "deepgram":
        h = {"Authorization": f"Token {key}"}
    else:
        h = {"Authorization": f"Bearer {key}"}
    if json_body:
        h["content-type"] = "application/json"
    return h


def _request(url, headers, data=None, timeout=REWRITE_TIMEOUT, context=None, method=None):
    """One HTTP call returning parsed JSON. Raises CloudError."""
    try:
        # Request() inside the try: a malformed custom URL raises ValueError
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method or ("POST" if data is not None else "GET"))
        with urllib.request.urlopen(req, timeout=timeout, context=context) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise CloudError(f"HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise CloudError(type(e).__name__) from None
    except ValueError as e:
        raise CloudError(f"bad request or reply ({type(e).__name__})") from None


# --- models -----------------------------------------------------------------

def list_models(provider, key, custom_url="", context=None):
    """Every model id the key can use, as the provider reports them."""
    base = base_url(provider, custom_url)
    if not base:
        raise CloudError("no endpoint set")
    if provider == "deepgram":
        data = _request(f"{base}/models", _headers(provider, key, False),
                        timeout=VERIFY_TIMEOUT, context=context)
        # Architectures ("nova-3") are what /listen takes as the model
        return sorted({m.get("architecture") or m.get("canonical_name", "")
                       for m in data.get("stt", []) if isinstance(m, dict)} - {""})
    url = f"{base}/models"
    if provider == "anthropic":
        url += "?limit=1000"
    data = _request(url, _headers(provider, key, False), timeout=VERIFY_TIMEOUT, context=context)
    items = data.get("data", data.get("models", [])) if isinstance(data, dict) else data
    ids = []
    for m in items or []:
        mid = m.get("id") if isinstance(m, dict) else m
        if isinstance(mid, str) and mid:
            # Gemini's list prefixes "models/"; its chat endpoint takes either
            ids.append(mid.removeprefix("models/"))
    return ids


def verify_key(provider, key, custom_url="", context=None):
    """Check a key before it is saved. Returns the model ids it can use.

    Listing models needs a valid key everywhere except OpenRouter, whose
    list is public; there /key answers only for a real one.
    """
    # OpenRouter's and Deepgram's model lists are public, so they would take
    # any key; these two answer only for a real one
    check = {"openrouter": "/key", "deepgram": "/projects"}.get(provider)
    if check:
        _request(f"{base_url(provider)}{check}", _headers(provider, key, False),
                 timeout=VERIFY_TIMEOUT, context=context)
    try:
        return list_models(provider, key, custom_url, context)
    except CloudError as e:
        # Some self-hosted servers have no model list; the model is typed in
        if provider == "custom" and str(e) == "HTTP 404":
            return []
        raise


def _version(text):
    """Numbers in a model name as a comparable tuple: claude-sonnet-4-5 ->
    (4, 5), gpt-4.1 -> (4, 1). Date stamps are dropped, so a dated snapshot
    ranks with its alias rather than above every newer model."""
    text = re.sub(r"\d{4}-\d{2}-\d{2}|\d{8}|\d{2}-\d{4}", "", text)
    nums = [int(n) for n in re.findall(r"\d+", text) if int(n) < 100]
    return tuple(nums[:3])


_NOT_CHAT = re.compile(
    r"embed|tts|whisper|transcri|audio|realtime|image|dall-e|moderation|"
    r"search|guard|vision|davinci|babbage|codex|computer-use|deep-research|"
    r"thinking|live|aqa|native|robotics|compound|-diarize|customtools|safeguard|"
    # OpenRouter's variants: :batch is queued for hours, :free rate-limited
    r":")

# Tiers within one generation, best for this job first: strong enough to
# rewrite well, quick enough to sit between release and paste. Vendors keep
# renaming tiers, so a name not listed here gets the middle rank rather than
# being skipped, and a new generation still gets considered.
_TIERS = {
    # Anthropic: Opus is the slow flagship
    "anthropic": {"sonnet": 3, "haiku": 2, "opus": 1},
    # GPT-6: Astra the slow flagship, Sol just below it, Terra mid, Luna the
    # small fast one. Before that: plain gpt-N, -chat (Instant), -mini.
    "openai": {"sol": 3, "terra": 2, "luna": 1, "astra": 1, "chat": 2, "mini": 1},
    # Gemini 4: Neon "fast, mid-priced", Hydrogen the efficient workhorse,
    # Carbon and Argon the reasoning flagships. Before that: Flash, Pro, Lite.
    "gemini": {"neon": 4, "hydrogen": 3, "flash": 3, "carbon": 2, "pro": 2,
               "argon": 1, "flash-lite": 1},
}
_MIDDLE = 2


def _tier(vendor, name):
    """The best-matching tier's rank; longer names first, so flash-lite is
    not read as flash."""
    for tier in sorted(_TIERS[vendor], key=len, reverse=True):
        if re.search(rf"(^|-){re.escape(tier)}(-|$)", name):
            return _TIERS[vendor][tier]
    return None


def _family_rank(provider, mid):
    """Higher is preferred among models of the same generation, None to skip."""
    m = mid.lower()
    if _NOT_CHAT.search(m):
        return None
    if provider == "groq":
        # Open models, ranked by how well they rewrite at Groq's speed
        for rank, family in ((5, "kimi-k2"), (4, "gpt-oss-120b"), (3, "qwen3"),
                             (2, "llama-4-maverick"), (1, "llama-3.3-70b")):
            if family in m:
                return rank
        return None
    name = m.split("/")[-1]
    if provider == "anthropic" or m.startswith(("anthropic/", "claude")):
        return _tier("anthropic", name) if name.startswith("claude") else None
    if provider == "gemini" or m.startswith("google/"):
        if not name.startswith("gemini"):
            return None  # Gemma and the rest
        rank = _tier("gemini", name)
        return _MIDDLE if rank is None else rank
    if provider == "openai" or m.startswith(("openai/", "gpt")):
        # The o-series and -pro think for seconds before answering
        if not name.startswith("gpt-") or re.search(r"-pro\b|nano|oss|instruct", name):
            return None
        rank = _tier("openai", name)
        if rank is None:
            # gpt-5 itself is the flagship of its line; an unknown suffix sits mid
            rank = 3 if re.fullmatch(r"gpt-[\d.]+", name) else _MIDDLE
        return rank
    # A custom server's names mean nothing to this ranking; on OpenRouter,
    # only the families above are ranked (Gemma is Google's, but no Gemini)
    return 0 if provider == "custom" else None


def rank_rewrite_models(provider, ids):
    """Model ids for rewriting, best first. Empty when none look usable."""
    if provider == "custom":
        return list(ids)
    if provider == "openrouter":
        # Vendors in order of how well their quick models write; within one,
        # the same ranking as asking that vendor directly
        vendors = ("anthropic/", "google/", "openai/")
        out = []
        for v in vendors:
            sub = [i for i in ids if i.startswith(v)]
            out += sorted((i for i in sub if _family_rank("openrouter", i) is not None),
                          key=lambda i: _rewrite_key("openrouter", i), reverse=True)
        return out
    usable = [i for i in ids if _family_rank(provider, i) is not None]
    return sorted(usable, key=lambda i: _rewrite_key(provider, i), reverse=True)


def _rewrite_key(provider, mid):
    v = _version(mid.split("/")[-1].replace("4o", "4.0"))
    rank = _family_rank(provider, mid)
    released = "preview" not in mid and "exp" not in mid
    if provider == "groq" or "gemini" in mid:
        # Family first. Groq's families are different labs, so their version
        # numbers don't compare; Gemini's Pro always reasons before it
        # answers, so any Flash is the quicker choice.
        return (rank, v, released, -len(mid))
    # Generation first, then family, then the minor version: a new Haiku
    # beats an old Sonnet, but within a generation Sonnet wins. Aliases
    # before dated snapshots, and previews after releases.
    return (v[:1], rank, v[1:], released, -len(mid))


def rank_stt_models(provider, ids):
    """Transcription model ids, best first."""
    if provider == "groq":
        # Turbo: near-identical accuracy at several times the speed
        order = ("whisper-large-v3-turbo", "whisper-large-v3")
        return [o for o in order if o in ids] + [i for i in ids if i.startswith("distil-whisper")]
    if provider == "deepgram":
        novas = [i for i in ids if i.startswith("nova") and "medical" not in i]
        return sorted(novas, key=_version, reverse=True)
    if provider == "openai":
        def key(i):
            # A full transcribe model over mini, mini over whisper-1
            return (("transcribe" in i) + ("transcribe" in i and "mini" not in i), _version(i))
        return sorted((i for i in ids if ("transcribe" in i or "whisper" in i)
                       and "diarize" not in i and "realtime" not in i),
                      key=key, reverse=True)
    return [i for i in ids if "whisper" in i or "transcri" in i] or list(ids)


def pick(kind, provider, ids):
    ranked = rank_rewrite_models(provider, ids) if kind == "rewrite" else rank_stt_models(provider, ids)
    return ranked[0] if ranked else None


# --- rewriting --------------------------------------------------------------

# Reasoning switched off, or as low as the model allows: these models
# otherwise think for seconds before answering a cleanup that needs none.
# Tried in order; a model that rejects one gets the next.
_REASONING = {
    "openai": ({"reasoning_effort": "none"}, {"reasoning_effort": "minimal"}, {}),
    "gemini": ({"reasoning_effort": "none"}, {}),
    "openrouter": ({"reasoning": {"enabled": False}}, {}),
}
_reasoning_choice = {}  # (provider, model) -> index into its variants


def _reasoning_variants(provider, model):
    if provider == "groq":
        if "qwen" in model:
            return ({"reasoning_effort": "none"}, {})
        if "gpt-oss" in model:
            return ({"reasoning_effort": "low"}, {})
    return _REASONING.get(provider, ({},))


def chat(provider, key, model, prompt, custom_url="", context=None):
    """One prompt to a cloud model. Returns the text. Raises CloudError."""
    base = base_url(provider, custom_url)
    if not base or not model:
        raise CloudError("endpoint or model not set")
    if provider == "anthropic":
        body = {"model": model, "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}]}
        data = _request(f"{base}/messages", _headers(provider, key),
                        json.dumps(body).encode(), context=context)
        try:
            return "".join(b.get("text", "") for b in data["content"]).strip()
        except (KeyError, TypeError, AttributeError):
            raise CloudError("unexpected response shape") from None

    variants = _reasoning_variants(provider, model)
    start = _reasoning_choice.get((provider, model), 0)
    for i in range(start, len(variants)):
        body = {"model": model, "messages": [{"role": "user", "content": prompt}],
                **variants[i]}
        try:
            data = _request(f"{base}/chat/completions", _headers(provider, key),
                            json.dumps(body).encode(), context=context)
        except CloudError as e:
            # A 400 is the model refusing the reasoning setting; anything
            # else (auth, network, overload) would fail the same way again
            if str(e) == "HTTP 400" and i + 1 < len(variants):
                continue
            raise
        _reasoning_choice[(provider, model)] = i
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError, AttributeError):
            # AttributeError: "content": null gives None.strip()
            raise CloudError("unexpected response shape") from None
    raise CloudError("no request shape accepted")


# --- transcription ----------------------------------------------------------

def wav_bytes(audio, sample_rate):
    """16-bit mono WAV of float audio in [-1, 1]."""
    pcm = (np.clip(np.asarray(audio, dtype=np.float32).reshape(-1), -1, 1) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _multipart(fields, file_bytes):
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                     f"\r\n\r\n{value}\r\n".encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                 f'filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode()
                 + file_bytes + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


# Whisper-style prompts are cut at 224 tokens; stay well inside it
PROMPT_CHARS = 600
KEYTERMS_MAX = 50


def transcribe(provider, key, model, audio, sample_rate, language=None, terms=(),
               custom_url="", context=None):
    """Speech to text. `language` is an ISO code or None to detect; `terms`
    are spellings to favour. Raises CloudError."""
    base = base_url(provider, custom_url)
    if not base or not model:
        raise CloudError("endpoint or model not set")
    audio_bytes = wav_bytes(audio, sample_rate)
    if provider == "deepgram":
        params = [("model", model), ("smart_format", "true"), ("punctuate", "true")]
        params.append(("language", language) if language else ("detect_language", "true"))
        params += [("keyterm", t) for t in list(terms)[:KEYTERMS_MAX]]
        url = f"{base}/listen?{urllib.parse.urlencode(params)}"
        headers = {**_headers(provider, key, False), "Content-Type": "audio/wav"}
        data = _request(url, headers, audio_bytes, timeout=STT_TIMEOUT, context=context)
        try:
            return data["results"]["channels"][0]["alternatives"][0]["transcript"].strip()
        except (KeyError, IndexError, TypeError, AttributeError):
            raise CloudError("unexpected response shape") from None
    fields = {"model": model, "response_format": "json"}
    if language:
        fields["language"] = language
    if terms:
        # A prompt of the words to expect is how these models take a
        # vocabulary; written as a sentence they are less likely to echo it
        prompt = "Words that may come up: "
        for term in terms:
            if len(prompt) + len(term) + 2 > PROMPT_CHARS:
                break
            prompt += term + ", "
        fields["prompt"] = prompt.rstrip(", ")
    body, ctype = _multipart(fields, audio_bytes)
    headers = {**_headers(provider, key, False), "Content-Type": ctype}
    data = _request(f"{base}/audio/transcriptions", headers, body,
                    timeout=STT_TIMEOUT, context=context)
    try:
        return data["text"].strip()
    except (KeyError, TypeError, AttributeError):
        raise CloudError("unexpected response shape") from None
