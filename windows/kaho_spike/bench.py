"""Benchmark the candidate engines on the committed clips; print a Markdown table.

Warm latency is the best of 3 runs per clip, after one warm-up decode, so
it reflects a dictation with the model already loaded, as Kaho keeps it.
"""

import json
import os
import pathlib
import platform
import statistics
import sys
import time

import soundfile as sf

from . import engines
from .text import has_cjk, term_hits, word_errors

CLIPS = pathlib.Path(getattr(sys, "_MEIPASS", pathlib.Path(__file__).parent.parent)) / "bench" / "clips"
CONFIGS = [
    ("qwen3", False),
    ("qwen3", True),  # + dictionary terms as hotwords
    ("parakeet", False),
    ("moonshine", False),
]
# tools/benchmark.py on an M4 Max (Qwen3-ASR 1.7B, MLX); WER and terms from
# the 109-clip Mac benchmark. Different clips, so a bar, not a like-for-like row.
MAC_BAR = "| **Mac bar**: Qwen3-ASR 1.7B, MLX, M4 Max | 0.12 s (1.4 s clip) | 0.42 s (10.3 s clip) | — | 1.3% | — | 87% | — | — |"


def cpu_name():
    if platform.system() == "Windows":
        try:
            import winreg

            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            return winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        except OSError:
            pass
    return platform.processor() or platform.machine()


def run_config(engine, hotwords, items, terms, models_dir, threads):
    t = time.monotonic()
    rec = engines.load(engine, models_dir, hotwords=terms if hotwords else (), threads=threads)
    load_s = time.monotonic() - t
    clips = [(it, *sf.read(CLIPS / it["file"], dtype="float32")) for it in items]
    engines.transcribe(rec, clips[0][1], clips[0][2])  # warm-up
    rows = []
    for it, audio, sr in clips:
        best, text = None, ""
        for _ in range(3):
            t = time.monotonic()
            text = engines.transcribe(rec, audio, sr)
            dt = time.monotonic() - t
            best = dt if best is None else min(best, dt)
        rows.append({"item": it, "latency": best, "text": text, "seconds": len(audio) / sr})
    return load_s, rows


def summarize(rows):
    def lat(pred):
        v = [r["latency"] for r in rows if pred(r)]
        return f"{statistics.median(v):.2f} s" if v else "—"

    def wer(set_name):
        e = n = 0
        for r in rows:
            if r["item"]["set"] == set_name:
                de, dn = word_errors(r["item"]["reference"], r["text"])
                e, n = e + de, n + dn
        return f"{100 * e / n:.1f}%" if n else "—"

    jargon = [r for r in rows if r["item"]["set"] == "jargon"]
    hits = sum(term_hits(r["item"]["terms"], r["text"]) for r in jargon)
    total = sum(len(r["item"]["terms"]) for r in jargon)
    drift = sum(has_cjk(r["text"]) for r in rows)
    return {
        "short": lat(lambda r: r["item"]["set"] == "libri" and r["seconds"] <= 5),
        "long": lat(lambda r: r["item"]["set"] == "libri" and r["seconds"] > 5),
        "jargon_lat": lat(lambda r: r["item"]["set"] == "jargon"),
        "wer_libri": wer("libri"),
        "wer_jargon": wer("jargon"),
        "terms": f"{hits}/{total} ({100 * hits / total:.0f}%)" if total else "—",
        "drift": f"{drift}/{len(rows)}",
    }


def main(models_dir, threads, out=None, details=None):
    manifest = json.loads((CLIPS / "manifest.json").read_text(encoding="utf-8"))
    items, terms = manifest["items"], manifest["terms"]
    lines = [
        f"### Kaho Windows spike: engine benchmark ({platform.system()} {platform.release()})",
        "",
        f"CPU: {cpu_name()} · logical cores: {os.cpu_count()} · threads used: {threads} · Python {platform.python_version()}",
        "",
        (
            f"Clips: {sum(i['set'] == 'libri' for i in items)} LibriSpeech test-clean (6 × 2–5 s, 4 × 8–12.5 s), "
            f"{sum(i['set'] == 'jargon' for i in items)} synthetic jargon dictations (Qwen3-TTS). "
            "Latency is warm, best of 3, median per group."
        ),
        "",
        "| Engine | Latency 2–5 s | Latency 8–12.5 s | Latency jargon | WER LibriSpeech | WER jargon | Dictionary terms right | Wrong-script output | Model load |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    all_rows = {}
    for engine, hw in CONFIGS:
        label = f"{engine}{' + hotwords' if hw else ''}"
        print(f"running {label} …", file=sys.stderr, flush=True)
        load_s, rows = run_config(engine, hw, items, terms, models_dir, threads)
        s = summarize(rows)
        all_rows[label] = [{"file": r["item"]["file"], "latency": round(r["latency"], 3), "text": r["text"]} for r in rows]
        lines.append(
            f"| {label} | {s['short']} | {s['long']} | {s['jargon_lat']} | {s['wer_libri']} | {s['wer_jargon']} "
            f"| {s['terms']} | {s['drift']} | {load_s:.1f} s |"
        )
    lines.append(MAC_BAR)
    lines += ["", "Hotwords apply to qwen3 only. Moonshine is English-only, a speed reference."]
    table = "\n".join(lines)
    print(table, flush=True)
    if out:
        pathlib.Path(out).write_text(table + "\n", encoding="utf-8")
    if details:
        pathlib.Path(details).write_text(json.dumps(all_rows, indent=1, ensure_ascii=False), encoding="utf-8")
    return table
