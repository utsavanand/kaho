#!/usr/bin/env python3
"""Compare speech and rewrite models on public Hugging Face datasets.

benchmark.py answers "did this change make Kaho slower?" with clips made by
`say`. This answers a different question — how much accuracy a smaller model
gives up — and for that synthetic speech is too easy. So it uses real data:

  speech   distil-whisper/earnings22   earnings calls: names, numbers, jargon
           openslr/librispeech_asr     read audiobooks, the standard baseline
  rewrite  google-research-datasets/disfl_qa   "Norse no wait Normandy"
           grammarly/coedit            edits asked for in words, the way a
                                       Shift instruction or a spoken edit of
                                       selected text asks for them

Rows come from the datasets-server API and are cached in build-bench/data, so
there is no `datasets` dependency and a second run reads from disk:

    .venv/bin/python tools/model_benchmark.py speech
    .venv/bin/python tools/model_benchmark.py rewrite
    .venv/bin/python tools/model_benchmark.py rewrite --models mlx-community/Qwen3-1.7B-4bit

Scores:

  WER          word error rate after lowercasing and stripping punctuation;
               lower is better
  chrF         character n-gram F-score against the dataset's reference
               (0–100); higher is better. "unchanged" is the score of pasting
               the input as-is, so a model has to beat it to be worth running
  false skip   dirty text the clean check would have pasted untouched; the
               failure a user notices
  memory       MLX's peak allocation while the model ran
"""

import argparse
import gc
import json
import pathlib
import random
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "build-bench" / "data"
RESULTS_DIR = ROOT / "build-bench"
ROWS_API = "https://datasets-server.huggingface.co/rows"

SPEECH_MODELS = {
    "mlx-community/Qwen3-ASR-1.7B-8bit": "a8379a2e2f9e313c9292cdf1af4055ab56d50d55",
    "mlx-community/Qwen3-ASR-0.6B-8bit": "89e96d92ba34aca20b3e29fb10cc284097d1219f",
}
REWRITE_MODELS = {
    "mlx-community/Qwen3-4B-Instruct-2507-4bit": "50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b",
    "mlx-community/Qwen3-1.7B-4bit": "3b1b1768f8f8cf8351c712464f906e86c2b8269e",
    "mlx-community/Qwen3-1.7B-8bit": "8c24f6782a91421513803ce527a27dcc560ab904",
}

COEDIT_TASKS = ("gec", "paraphrase", "neutralize", "simplification", "coherence", "clarity")


# --- data ------------------------------------------------------------------

def fetch_rows(dataset, config, split, total, n, blocks=6):
    """n rows in a few contiguous blocks spread across the split: spread so
    they don't all come from one recording, blocks because the API answers a
    burst of one-row requests with 429."""
    per = -(-n // blocks)
    rows = []
    for b in range(blocks):
        q = urllib.parse.urlencode({"dataset": dataset, "config": config, "split": split,
                                    "offset": int(b * total / blocks), "length": per})
        for attempt in range(6):
            try:
                with urllib.request.urlopen(f"{ROWS_API}?{q}", timeout=120) as r:
                    rows += [x["row"] for x in json.load(r)["rows"]]
                break
            except urllib.error.HTTPError as e:
                if e.code != 429 or attempt == 5:
                    raise
                time.sleep(5 * 2 ** attempt)
    return rows[:n]


def cached(name, build):
    path = DATA_DIR / f"{name}.json"
    if path.exists():
        return json.loads(path.read_text())
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = build()
    path.write_text(json.dumps(data, indent=1))
    return data


def speech_set(name, dataset, config, split, text_key, total, n):
    def build():
        items = []
        for i, row in enumerate(fetch_rows(dataset, config, split, total, n)):
            audio = DATA_DIR / name / f"{i:03d}{pathlib.Path(urllib.parse.urlparse(row['audio'][0]['src']).path).suffix}"
            audio.parent.mkdir(parents=True, exist_ok=True)
            urllib.request.urlretrieve(row["audio"][0]["src"], audio)
            items.append({"audio": str(audio.relative_to(DATA_DIR)), "text": row[text_key]})
        return items
    return cached(name, build)


def load_audio(path):
    import miniaudio
    import numpy as np
    d = miniaudio.decode_file(str(DATA_DIR / path), output_format=miniaudio.SampleFormat.FLOAT32,
                              nchannels=1, sample_rate=16000)
    return np.asarray(d.samples, dtype=np.float32)


def rewrite_sets(n):
    def disfl():
        rows = fetch_rows("google-research-datasets/disfl_qa", "default", "test", 3643, n)
        return [{"src": r["disfluent question"], "tgt": r["original question"]} for r in rows]

    def coedit():
        # Validation has only gec, paraphrase and neutralize; the other kinds
        # of edit come from train, which is fine — nothing here is trained.
        # Sampled per task so every kind is represented.
        import huggingface_hub
        rows = {}
        for split in ("validation", "train"):
            path = huggingface_hub.hf_hub_download("grammarly/coedit", f"{split}.jsonl",
                                                   repo_type="dataset")
            rows[split] = [json.loads(line) for line in pathlib.Path(path).read_text().splitlines()]
        rng = random.Random(0)
        out = []
        for task in COEDIT_TASKS:
            pool = ([r for r in rows["validation"] if r["task"] == task]
                    or [r for r in rows["train"] if r["task"] == task])
            for r in rng.sample(pool, min(n // 3, len(pool))):
                instruction, _, text = r["src"].partition(": ")
                out.append({"task": task, "instruction": instruction, "src": text, "tgt": r["tgt"]})
        return out

    return cached(f"disfl_qa-{n}", disfl), cached(f"coedit-{n}", coedit)


# --- scoring ---------------------------------------------------------------

# Earnings22's references keep every "uh" and mark gaps "inaudible"; a model
# that leaves those out is doing what dictation should, so neither side is
# charged for them (Whisper's English normalizer drops the same words)
FILLERS = {"uh", "um", "eh", "ah", "er", "erm", "hmm", "mm", "mhm", "inaudible"}


def norm_words(s):
    s = s.lower().replace("’", "'")
    s = re.sub(r"[^\w\s']", " ", s)
    return [w for w in s.split() if w not in FILLERS]


def wer(ref, hyp):
    r, h = norm_words(ref), norm_words(hyp)
    d = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        prev, d[0] = d[0], i
        for j, hw in enumerate(h, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (rw != hw))
    return d[len(h)], max(len(r), 1)


def chrf(hyp, ref, n=6, beta=2):
    """chrF (Popović 2015): character n-grams, whitespace removed."""
    h, r = hyp.replace(" ", ""), ref.replace(" ", "")
    precs, recs = [], []
    for k in range(1, n + 1):
        hc = Counter(h[i:i + k] for i in range(len(h) - k + 1))
        rc = Counter(r[i:i + k] for i in range(len(r) - k + 1))
        if not hc or not rc:
            continue
        match = sum((hc & rc).values())
        precs.append(match / sum(hc.values()))
        recs.append(match / sum(rc.values()))
    if not precs:
        return 0.0
    p, rr = statistics.mean(precs), statistics.mean(recs)
    if p + rr == 0:
        return 0.0
    return 100 * (1 + beta ** 2) * p * rr / (beta ** 2 * p + rr)


# --- models ----------------------------------------------------------------

def mx_memory():
    import mlx.core as mx
    get_peak = getattr(mx, "get_peak_memory", None) or mx.metal.get_peak_memory
    reset = getattr(mx, "reset_peak_memory", None) or mx.metal.reset_peak_memory
    active = getattr(mx, "get_active_memory", None) or mx.metal.get_active_memory
    return get_peak, reset, active


def release():
    import mlx.core as mx
    gc.collect()
    (getattr(mx, "clear_cache", None) or mx.metal.clear_cache)()


def bench_speech(n):
    import kaho
    sets = {
        "earnings22": speech_set("earnings22", "distil-whisper/earnings22", "chunked", "test",
                                 "transcription", 57391, n),
        "librispeech": speech_set("librispeech", "openslr/librispeech_asr", "clean", "test",
                                  "text", 2620, n),
    }
    audio = {k: [load_audio(x["audio"]) for x in v] for k, v in sets.items()}
    peak, reset, active = mx_memory()
    results = {}
    for repo, rev in SPEECH_MODELS.items():
        release()
        reset()
        kaho.asr = kaho.load_model(kaho.huggingface_hub.snapshot_download(repo, revision=rev))
        weights = active()
        kaho.transcribe(audio["librispeech"][0], use_dictionary=False)  # warmup
        res = {"weights_gb": round(weights / 1e9, 2)}
        for name, items in sets.items():
            errs = words = 0
            secs = audio_secs = 0.0
            for item, clip in zip(items, audio[name]):
                t0 = time.monotonic()
                hyp = kaho.transcribe(clip, use_dictionary=False)
                secs += time.monotonic() - t0
                audio_secs += len(clip) / 16000
                e, w = wer(item["text"], hyp)
                errs, words = errs + e, words + w
            res[name] = {"wer": round(100 * errs / words, 2), "n": len(items),
                         "rtf": round(secs / audio_secs, 4),
                         "sec_per_clip": round(secs / len(items), 3)}
        res["peak_gb"] = round(peak() / 1e9, 2)
        results[repo] = res
        print(f"{repo}\n  {json.dumps(res)}", flush=True)
        kaho.asr = None
    return results


def bench_rewrite(n, models):
    import mlx.core as mx
    import mlx_lm

    import kaho
    disfl, coedit = rewrite_sets(n)
    kaho.mlx_lm = mlx_lm
    kaho.settings["rewrite_backend"] = "local"
    kaho.log = lambda *a, **k: None  # per-call log lines would drown the table
    peak, reset, active = mx_memory()
    results = {"unchanged": {
        "clean_chrf": round(statistics.mean(chrf(x["src"], x["tgt"]) for x in disfl), 1),
        **{f"{t}_chrf": round(statistics.mean(chrf(x["src"], x["tgt"]) for x in coedit
                                              if x["task"] == t), 1) for t in COEDIT_TASKS},
    }}
    print(f"unchanged\n  {json.dumps(results['unchanged'])}", flush=True)
    for repo in models:
        release()
        reset()
        path = kaho.huggingface_hub.snapshot_download(repo, revision=REWRITE_MODELS.get(repo))
        model, tokenizer = mlx_lm.load(path)
        weights = active()
        mlx_lm.generate(model, tokenizer, prompt="hi", max_tokens=1)
        kaho.rewriter = (model, tokenizer)
        res = {"weights_gb": round(weights / 1e9, 2)}

        # Clean up, end to end: the check, then generation when it says so
        t0 = time.monotonic()
        scores, skipped = [], 0
        for x in disfl:
            out = kaho.rewrite(x["src"], "clean")
            skipped += out is None
            scores.append(chrf(out or x["src"], x["tgt"]))
        res["clean_chrf"] = round(statistics.mean(scores), 1)
        res["clean_skipped"] = skipped
        res["clean_sec"] = round((time.monotonic() - t0) / len(disfl), 3)

        # The clean check alone, on text with a known answer: disfluent and
        # ungrammatical inputs must not be skipped; their fixed versions may be
        dirty = [x["src"] for x in disfl] + [x["src"] for x in coedit if x["task"] == "gec"]
        clean = [x["tgt"] for x in disfl] + [x["tgt"] for x in coedit if x["task"] == "gec"]
        thr = kaho.SKIP_REWRITE_CONFIDENCE
        res["false_skip_pct"] = round(100 * statistics.mean(
            kaho.clean_as_is_probability(t) >= thr for t in dirty), 1)
        res["true_skip_pct"] = round(100 * statistics.mean(
            kaho.clean_as_is_probability(t) >= thr for t in clean), 1)

        # Instructions: what a Shift instruction or a spoken edit does
        t0 = time.monotonic()
        for task in COEDIT_TASKS:
            items = [x for x in coedit if x["task"] == task]
            res[f"{task}_chrf"] = round(statistics.mean(
                chrf(kaho.rewrite_with_instruction(x["src"], x["instruction"]) or x["src"], x["tgt"])
                for x in items), 1)
        res["instruct_sec"] = round((time.monotonic() - t0) / len(coedit), 3)
        res["peak_gb"] = round(peak() / 1e9, 2)
        results[repo] = res
        print(f"{repo}\n  {json.dumps(res)}", flush=True)
        kaho.rewriter = None
        del model, tokenizer
        mx.eval([])
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=("speech", "rewrite"))
    ap.add_argument("-n", type=int, default=60, help="rows per dataset (default 60)")
    ap.add_argument("--models", nargs="+", help="rewrite models to run (default: all)")
    args = ap.parse_args()
    if args.what == "speech":
        results = bench_speech(args.n)
    else:
        results = bench_rewrite(args.n, args.models or list(REWRITE_MODELS))
    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"models-{args.what}-{time.strftime('%Y%m%d-%H%M')}.json"
    out.write_text(json.dumps(results, indent=2) + "\n")
    print(f"\nsaved to {out}")


if __name__ == "__main__":
    main()
