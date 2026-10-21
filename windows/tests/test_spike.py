"""Tests for the Windows spike's pure logic. Run anywhere:

    python -m unittest discover -s windows/tests -t windows
"""

import json
import pathlib
import unittest

from kaho_spike.clipboard import should_restore
from kaho_spike.hotkey import CANCEL, HOLDING, IDLE, START, STOP, VK_RCONTROL, step
from kaho_spike.text import clean_transcript, has_cjk, normalize, term_hits, word_errors

VK_C, VK_LCONTROL = 0x43, 0xA2


class TestHotkey(unittest.TestCase):
    def run_keys(self, events):
        state, actions = IDLE, []
        for vk, down in events:
            state, action = step(state, vk, down)
            actions.append(action)
        return state, [a for a in actions if a]

    def test_hold_and_release_records_once(self):
        self.assertEqual(self.run_keys([(VK_RCONTROL, True), (VK_RCONTROL, False)]), (IDLE, [START, STOP]))

    def test_auto_repeat_while_held_is_ignored(self):
        events = [(VK_RCONTROL, True)] * 5 + [(VK_RCONTROL, False)]
        self.assertEqual(self.run_keys(events), (IDLE, [START, STOP]))

    def test_a_shortcut_with_right_ctrl_cancels_instead_of_pasting(self):
        events = [(VK_RCONTROL, True), (VK_C, True), (VK_C, False), (VK_RCONTROL, False)]
        self.assertEqual(self.run_keys(events), (IDLE, [START, CANCEL]))

    def test_left_ctrl_never_starts_a_recording(self):
        self.assertEqual(self.run_keys([(VK_LCONTROL, True), (VK_LCONTROL, False)]), (IDLE, []))

    def test_other_keys_while_idle_do_nothing(self):
        state, action = step(IDLE, VK_C, True)
        self.assertEqual((state, action), (IDLE, None))

    def test_still_holding_after_key_down(self):
        self.assertEqual(step(IDLE, VK_RCONTROL, True), (HOLDING, START))


class TestClipboard(unittest.TestCase):
    def test_restores_when_untouched(self):
        self.assertTrue(should_restore(41, 41))

    def test_leaves_a_newer_copy_alone(self):
        self.assertFalse(should_restore(41, 42))


class TestText(unittest.TestCase):
    def test_strips_the_qwen_language_tag(self):
        self.assertEqual(clean_transcript("language English<asr_text>Hello there."), "Hello there.")

    def test_leaves_ordinary_text_alone(self):
        self.assertEqual(clean_transcript("  The language of Kaho is speech. "), "The language of Kaho is speech.")

    def test_normalize_drops_punctuation_and_case(self):
        self.assertEqual(normalize('Abhimanyu said, "The Grafana dashboard!"'),
                         ["abhimanyu", "said", "the", "grafana", "dashboard"])

    def test_word_errors_counts_substitutions_insertions_deletions(self):
        self.assertEqual(word_errors("the cat sat", "the cat sat"), (0, 3))
        self.assertEqual(word_errors("the cat sat", "the bat sat down"), (2, 3))
        self.assertEqual(word_errors("the cat sat", "cat"), (2, 3))

    def test_term_hits_needs_the_exact_spelling(self):
        terms = ["Qwen3-ASR", "MLX", "Siobhan"]
        self.assertEqual(term_hits(terms, "We moved to Qwen3-ASR on MLX."), 2)
        self.assertEqual(term_hits(terms, "We moved to Qwen 3 ASR on M L X, Shavon."), 0)

    def test_wrong_script_is_detected(self):
        self.assertTrue(has_cjk("卡荷 paste the transcript"))
        self.assertFalse(has_cjk("Kaho pastes the transcript"))


class TestClips(unittest.TestCase):
    def test_every_manifest_clip_exists_and_has_its_terms(self):
        clips = pathlib.Path(__file__).resolve().parent.parent / "bench" / "clips"
        manifest = json.loads((clips / "manifest.json").read_text(encoding="utf-8"))
        for item in manifest["items"]:
            self.assertTrue((clips / item["file"]).is_file(), item["file"])
            for t in item.get("terms", []):
                self.assertIn(t.lower(), item["reference"].lower())


if __name__ == "__main__":
    unittest.main()
