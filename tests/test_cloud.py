"""cloud.py: model ranking, request shapes and failure handling."""
import io
import json
import unittest
import wave
from unittest import mock

import cloud


def answer(urlopen, *bodies):
    urlopen.return_value.__enter__.return_value.read.side_effect = [
        json.dumps(b).encode() for b in bodies]


def http_error(code):
    return cloud.urllib.error.HTTPError("https://x", code, "no", {}, io.BytesIO(b"secret body"))


class TestRewriteRanking(unittest.TestCase):
    """Best quality that still answers in a second or two."""

    def test_anthropic_prefers_the_newest_sonnet(self):
        ids = ["claude-opus-5-5", "claude-haiku-5-5", "claude-sonnet-5-5",
               "claude-sonnet-4-5-20250929", "claude-3-5-haiku-20241022"]
        self.assertEqual(cloud.pick("rewrite", "anthropic", ids), "claude-sonnet-5-5")

    def test_a_new_haiku_beats_an_old_sonnet(self):
        ids = ["claude-3-7-sonnet-20250219", "claude-haiku-4-5-20251001"]
        self.assertEqual(cloud.pick("rewrite", "anthropic", ids), "claude-haiku-4-5-20251001")

    def test_openai_skips_slow_and_non_chat_models(self):
        ids = ["gpt-5-pro", "o3", "gpt-5", "gpt-5.1", "gpt-5-nano", "whisper-1",
               "gpt-4o-transcribe", "text-embedding-3-large", "gpt-realtime", "gpt-5-codex"]
        self.assertEqual(cloud.pick("rewrite", "openai", ids), "gpt-5.1")

    def test_openai_ranks_a_dated_snapshot_with_its_alias(self):
        ids = ["gpt-4.1-2025-04-14", "gpt-5"]
        self.assertEqual(cloud.pick("rewrite", "openai", ids), "gpt-5")

    def test_gemini_prefers_flash_over_a_newer_pro(self):
        ids = ["gemini-3-pro-preview", "gemini-2.5-flash", "gemini-2.5-flash-lite",
               "gemini-2.5-flash-preview-tts", "text-embedding-004"]
        self.assertEqual(cloud.pick("rewrite", "gemini", ids), "gemini-2.5-flash")

    def test_groq_ranks_by_family(self):
        ids = ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "qwen/qwen3-32b",
               "moonshotai/kimi-k2-instruct", "meta-llama/llama-guard-4-12b", "groq/compound"]
        self.assertEqual(cloud.pick("rewrite", "groq", ids), "moonshotai/kimi-k2-instruct")

    def test_openrouter_takes_the_best_vendor_model(self):
        ids = ["meta-llama/llama-3.3-70b-instruct", "openai/gpt-5", "google/gemini-2.5-flash",
               "anthropic/claude-opus-4.1", "anthropic/claude-sonnet-4.5",
               "anthropic/claude-3.7-sonnet:thinking"]
        self.assertEqual(cloud.pick("rewrite", "openrouter", ids), "anthropic/claude-sonnet-4.5")

    def test_openrouter_ignores_other_families_from_the_same_vendor(self):
        # The live list has Gemma under google/; it crashed the sort once
        ids = ["google/gemma-3-27b-it", "google/gemini-2.5-flash", "google/gemini-2.5-pro",
               "openai/gpt-oss-120b", "openai/gpt-5"]
        self.assertEqual(cloud.rank_rewrite_models("openrouter", ids),
                         ["google/gemini-2.5-flash", "google/gemini-2.5-pro", "openai/gpt-5"])

    def test_gpt_6_takes_sol_not_the_slow_flagship(self):
        ids = ["gpt-6-astra", "gpt-6.2-astra", "gpt-6.2-sol", "gpt-6.2-sol-pro", "gpt-6.1-terra",
               "gpt-6-luna", "gpt-5.5", "gpt-5.6-sol"]
        self.assertEqual(cloud.pick("rewrite", "openai", ids), "gpt-6.2-sol")

    def test_gemini_4_takes_neon(self):
        ids = ["gemini-4-argon", "gemini-4-carbon", "gemini-4-hydrogen", "gemini-4-neon",
               "gemini-3.8-flash", "gemini-3.5-flash-lite"]
        self.assertEqual(cloud.pick("rewrite", "gemini", ids), "gemini-4-neon")

    def test_an_unknown_tier_of_a_new_generation_is_still_considered(self):
        self.assertEqual(cloud.pick("rewrite", "gemini", ["gemini-3.8-flash", "gemini-5-xenon"]),
                         "gemini-3.8-flash")
        self.assertEqual(cloud.pick("rewrite", "openai", ["gpt-6.2-sol", "gpt-7-nova"]),
                         "gpt-7-nova")

    def test_openrouter_skips_batch_and_free_variants(self):
        ids = ["anthropic/claude-sonnet-5.5:batch", "anthropic/claude-sonnet-5.5:free",
               "anthropic/claude-sonnet-5.5"]
        self.assertEqual(cloud.rank_rewrite_models("openrouter", ids),
                         ["anthropic/claude-sonnet-5.5"])

    def test_nothing_usable_picks_nothing(self):
        self.assertIsNone(cloud.pick("rewrite", "openai", ["whisper-1", "tts-1"]))


class TestSttRanking(unittest.TestCase):
    def test_openai_prefers_the_full_transcribe_model(self):
        ids = ["whisper-1", "gpt-4o-mini-transcribe", "gpt-4o-transcribe",
               "gpt-4o-transcribe-diarize", "gpt-5"]
        self.assertEqual(cloud.pick("stt", "openai", ids), "gpt-4o-transcribe")

    def test_groq_prefers_turbo(self):
        self.assertEqual(cloud.pick("stt", "groq", ["whisper-large-v3", "whisper-large-v3-turbo",
                                                    "llama-3.3-70b-versatile"]),
                         "whisper-large-v3-turbo")

    def test_deepgram_takes_the_newest_general_nova(self):
        ids = ["base", "nova-2", "nova-3", "nova-3-medical", "whisper"]
        self.assertEqual(cloud.pick("stt", "deepgram", ids), "nova-3")


class TestCustomUrl(unittest.TestCase):
    def test_an_old_full_chat_url_becomes_the_base(self):
        self.assertEqual(cloud.base_url("custom", "https://h/v1/chat/completions/"), "https://h/v1")


class TestListModels(unittest.TestCase):
    def test_gemini_ids_lose_their_prefix(self):
        with mock.patch.object(cloud.urllib.request, "urlopen") as urlopen:
            answer(urlopen, {"data": [{"id": "models/gemini-2.5-flash"}]})
            self.assertEqual(cloud.list_models("gemini", "k"), ["gemini-2.5-flash"])
        self.assertEqual(urlopen.call_args.args[0].headers["Authorization"], "Bearer k")

    def test_deepgram_lists_architectures(self):
        with mock.patch.object(cloud.urllib.request, "urlopen") as urlopen:
            answer(urlopen, {"stt": [{"canonical_name": "nova-3-general", "architecture": "nova-3"},
                                     {"canonical_name": "nova-2-meeting", "architecture": "nova-2"}]})
            self.assertEqual(cloud.list_models("deepgram", "k"), ["nova-2", "nova-3"])
        self.assertEqual(urlopen.call_args.args[0].headers["Authorization"], "Token k")

    def test_a_bad_key_is_reported_without_the_body(self):
        with mock.patch.object(cloud.urllib.request, "urlopen", side_effect=http_error(401)), \
             self.assertRaises(cloud.CloudError) as caught:
            cloud.verify_key("openai", "sk-secret")
        self.assertEqual(str(caught.exception), "HTTP 401")

    def test_deepgram_checks_the_key_itself(self):
        with mock.patch.object(cloud.urllib.request, "urlopen") as urlopen:
            answer(urlopen, {"projects": []}, {"stt": []})
            cloud.verify_key("deepgram", "k")
        self.assertTrue(urlopen.call_args_list[0].args[0].full_url.endswith("/projects"))

    def test_openrouter_checks_the_key_itself(self):
        with mock.patch.object(cloud.urllib.request, "urlopen") as urlopen:
            answer(urlopen, {"data": {"label": "k"}}, {"data": [{"id": "openai/gpt-5"}]})
            self.assertEqual(cloud.verify_key("openrouter", "k"), ["openai/gpt-5"])
        self.assertTrue(urlopen.call_args_list[0].args[0].full_url.endswith("/key"))

    def test_a_custom_server_without_a_model_list_is_still_accepted(self):
        with mock.patch.object(cloud.urllib.request, "urlopen", side_effect=http_error(404)):
            self.assertEqual(cloud.verify_key("custom", "k", "https://h/v1"), [])


class TestChat(unittest.TestCase):
    def setUp(self):
        cloud._reasoning_choice.clear()

    def test_reasoning_is_switched_off(self):
        with mock.patch.object(cloud.urllib.request, "urlopen") as urlopen:
            answer(urlopen, {"choices": [{"message": {"content": " Done. "}}]})
            self.assertEqual(cloud.chat("openai", "k", "gpt-5.1", "p"), "Done.")
        self.assertEqual(json.loads(urlopen.call_args.args[0].data)["reasoning_effort"], "none")

    def test_a_model_that_rejects_it_gets_the_next_setting_and_keeps_it(self):
        ok = json.dumps({"choices": [{"message": {"content": "Done."}}]}).encode()
        reads = iter([ok, ok])
        def fake(request, **_):
            body = json.loads(request.data)
            if body.get("reasoning_effort") == "none":
                raise http_error(400)
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = next(reads)
            return response
        with mock.patch.object(cloud.urllib.request, "urlopen", side_effect=fake) as urlopen:
            cloud.chat("openai", "k", "gpt-5", "p")
            cloud.chat("openai", "k", "gpt-5", "p")
        efforts = [json.loads(c.args[0].data).get("reasoning_effort") for c in urlopen.call_args_list]
        self.assertEqual(efforts, ["none", "minimal", "minimal"])

    def test_an_auth_failure_is_not_retried(self):
        with mock.patch.object(cloud.urllib.request, "urlopen", side_effect=http_error(401)) as u, \
             self.assertRaises(cloud.CloudError):
            cloud.chat("openai", "k", "gpt-5", "p")
        self.assertEqual(u.call_count, 1)

    def test_anthropic_shape(self):
        with mock.patch.object(cloud.urllib.request, "urlopen") as urlopen:
            answer(urlopen, {"content": [{"type": "text", "text": "Hi."}]})
            self.assertEqual(cloud.chat("anthropic", "k", "claude-sonnet-5-5", "p"), "Hi.")
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/messages"))
        self.assertEqual(request.headers["X-api-key"], "k")

    def test_a_null_reply_is_an_error_not_a_crash(self):
        with mock.patch.object(cloud.urllib.request, "urlopen") as urlopen, \
             self.assertRaises(cloud.CloudError):
            answer(urlopen, {"choices": [{"message": {"content": None}}]})
            cloud.chat("groq", "k", "llama-3.3-70b-versatile", "p")


try:
    import numpy as np
    HAVE_NUMPY = isinstance(np.zeros(1), np.ndarray)
except ImportError:
    HAVE_NUMPY = False


@unittest.skipUnless(HAVE_NUMPY, "encodes real audio")
class TestTranscribe(unittest.TestCase):
    def setUp(self):
        self.audio = np.zeros(1600, dtype="float32")

    def test_the_wav_is_16_bit_mono(self):
        with wave.open(io.BytesIO(cloud.wav_bytes(self.audio, 16000))) as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()),
                             (1, 2, 16000, 1600))

    def test_whisper_style_upload_carries_the_vocabulary(self):
        with mock.patch.object(cloud.urllib.request, "urlopen") as urlopen:
            answer(urlopen, {"text": " Hello Kaho. "})
            out = cloud.transcribe("groq", "k", "whisper-large-v3-turbo", self.audio, 16000,
                                   language="en", terms=["Kaho", "Duckterm"])
        self.assertEqual(out, "Hello Kaho.")
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/audio/transcriptions"))
        body = request.data
        self.assertIn(b"Words that may come up: Kaho, Duckterm", body)
        self.assertIn(b'name="language"\r\n\r\nen', body)
        self.assertIn(b"RIFF", body)

    def test_deepgram_takes_raw_audio_and_keyterms(self):
        reply = {"results": {"channels": [{"alternatives": [{"transcript": "hi there"}]}]}}
        with mock.patch.object(cloud.urllib.request, "urlopen") as urlopen:
            answer(urlopen, reply)
            out = cloud.transcribe("deepgram", "k", "nova-3", self.audio, 16000, terms=["Kaho"])
        self.assertEqual(out, "hi there")
        request = urlopen.call_args.args[0]
        self.assertIn("keyterm=Kaho", request.full_url)
        self.assertIn("detect_language=true", request.full_url)
        self.assertTrue(request.data.startswith(b"RIFF"))

    def test_a_long_vocabulary_is_cut_to_fit_the_prompt(self):
        terms = [f"Term{i:03d}" for i in range(300)]
        with mock.patch.object(cloud.urllib.request, "urlopen") as urlopen:
            answer(urlopen, {"text": "ok"})
            cloud.transcribe("openai", "k", "gpt-4o-transcribe", self.audio, 16000, terms=terms)
        body = urlopen.call_args.args[0].data
        prompt = body.split(b'name="prompt"\r\n\r\n')[1].split(b"\r\n")[0]
        self.assertLessEqual(len(prompt), cloud.PROMPT_CHARS)
        self.assertFalse(prompt.endswith(b","))


if __name__ == "__main__":
    unittest.main()
