"""The candidate speech engines, all through sherpa-onnx (one native package,
no Python ML runtime). Models download once into the models directory."""

import pathlib
import sys
import tarfile
import time
import urllib.request

from .text import clean_transcript

RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"
MODELS = {
    # Qwen3-ASR is the Mac's model family; only 0.6B is packaged for sherpa-onnx
    "qwen3": "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25",
    "parakeet": "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8",
    # Moonshine v2 tiny: English only, a speed reference rather than a candidate
    "moonshine": "sherpa-onnx-moonshine-tiny-en-quantized-2026-02-27",
}
SAMPLE_RATE = 16000


def ensure_model(engine, models_dir):
    """Download and unpack an engine's model on first use; return its folder."""
    name = MODELS[engine]
    folder = pathlib.Path(models_dir) / name
    if folder.is_dir():
        return folder
    folder.parent.mkdir(parents=True, exist_ok=True)
    archive = folder.parent / f"{name}.tar.bz2"
    url = f"{RELEASES}/{name}.tar.bz2"
    print(f"downloading {engine} model ({name}) — first run only", flush=True)
    last = [0.0]

    def progress(blocks, block_size, total):
        done = blocks * block_size
        if total > 0 and time.monotonic() - last[0] > 2:
            last[0] = time.monotonic()
            print(f"  {min(done, total) / 1e6:6.0f} of {total / 1e6:.0f} MB", flush=True)

    urllib.request.urlretrieve(url, archive, progress)
    with tarfile.open(archive, "r:bz2") as tar:
        tar.extractall(folder.parent, filter="data")
    archive.unlink()
    return folder


def load(engine, models_dir, hotwords=(), threads=4):
    """A warm-ready recognizer. hotwords only applies to qwen3."""
    import sherpa_onnx

    d = ensure_model(engine, models_dir)
    R = sherpa_onnx.OfflineRecognizer
    if engine == "qwen3":
        return R.from_qwen3_asr(
            conv_frontend=str(d / "conv_frontend.onnx"),
            encoder=str(d / "encoder.int8.onnx"),
            decoder=str(d / "decoder.int8.onnx"),
            tokenizer=str(d / "tokenizer"),
            num_threads=threads,
            # Newline-separated scored best on the jargon clips (13/18 terms
            # vs 12/18 comma- or space-separated, 6/18 with none)
            hotwords="\n".join(hotwords),
        )
    if engine == "parakeet":
        return R.from_transducer(
            encoder=str(d / "encoder.int8.onnx"),
            decoder=str(d / "decoder.int8.onnx"),
            joiner=str(d / "joiner.int8.onnx"),
            tokens=str(d / "tokens.txt"),
            model_type="nemo_transducer",
            num_threads=threads,
        )
    if engine == "moonshine":
        return R.from_moonshine_v2(
            encoder=str(d / "encoder_model.ort"),
            decoder=str(d / "decoder_model_merged.ort"),
            tokens=str(d / "tokens.txt"),
            num_threads=threads,
        )
    sys.exit(f"unknown engine {engine!r}; choose from {', '.join(MODELS)}")


def transcribe(recognizer, audio, sample_rate=SAMPLE_RATE):
    stream = recognizer.create_stream()
    stream.accept_waveform(sample_rate, audio)
    recognizer.decode_stream(stream)
    return clean_transcript(stream.result.text)
