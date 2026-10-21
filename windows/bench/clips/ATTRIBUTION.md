# Clip sources

- `libri/*.flac`: 10 utterances from LibriSpeech test-clean (Panayotov, Chen,
  Povey and Khudanpur, "LibriSpeech: an ASR corpus based on public domain audio
  books", ICASSP 2015), fetched via the Hugging Face dataset
  `openslr/librispeech_asr`. Licensed **CC BY 4.0**
  (https://creativecommons.org/licenses/by/4.0/). Unmodified audio; reference
  transcripts are in `manifest.json`.
- `jargon/*.flac`: synthetic speech generated for this project with Qwen3-TTS
  (`mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-bf16`, voices Ryan and
  Vivian), resampled to 16 kHz mono. The sentences are made up.
