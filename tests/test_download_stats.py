"""The stats README: lifetime counts through file replacements, ours vs outside."""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))
import download_stats


def snap(day, asset, count, tag="v1"):
    return {"day": day, "tag": tag, "asset": asset, "downloads": str(count)}


class TestDownloadStats(unittest.TestCase):
    def test_a_replaced_file_keeps_the_downloads_before_the_reset(self):
        # The 2.4.0 zip: 3 downloads, replaced, then 1 more
        rows = [snap("2026-10-10", "a.zip", 3), snap("2026-10-11", "a.zip", 1),
                snap("2026-10-12", "a.zip", 2)]
        lifetime, daily, resets = download_stats.lifetime_and_daily(rows)
        self.assertEqual(lifetime[("v1", "a.zip")], 5)
        self.assertEqual(daily[("2026-10-11", ("v1", "a.zip"))], 1)
        self.assertEqual(daily[("2026-10-12", ("v1", "a.zip"))], 1)
        self.assertEqual(resets, [("2026-10-11", "a.zip", 3, 1)])

    def test_a_snapshot_recorded_twice_counts_once(self):
        rows = [snap("2026-10-10", "a.zip", 3), snap("2026-10-10", "a.zip", 3),
                snap("2026-10-11", "a.zip", 4)]
        lifetime, daily, _ = download_stats.lifetime_and_daily(rows)
        self.assertEqual(lifetime[("v1", "a.zip")], 4)
        self.assertEqual(daily[("2026-10-10", ("v1", "a.zip"))], 3)

    def test_outside_is_total_minus_ours_and_never_negative(self):
        rows = [snap("2026-10-13", "k.dmg", 4), snap("2026-10-13", "old.dmg", 12)]
        internal = [{"day": "2026-10-13", "asset": "k.dmg", "downloads": "2", "note": ""},
                    {"day": "2026-10-13", "asset": "old.dmg", "downloads": "50", "note": ""}]
        releases = [["v1", "k.dmg", "2026-10-13", "4"], ["v1", "old.dmg", "2026-10-02", "12"]]
        out = download_stats.render(rows, internal, releases, "2026-10-13")
        self.assertIn("**Outside downloads: 2** of 16", out)
        self.assertIn("| v1 | k.dmg | 2026-10-13 | 4 | 2 | 2 |", out)
        self.assertIn("| v1 | old.dmg | 2026-10-02 | 12 | 12 | 0 |", out)

    def test_live_counts_ahead_of_todays_snapshot_are_included(self):
        rows = [snap("2026-10-19", "k.dmg", 2)]
        releases = [["v1", "k.dmg", "2026-10-13", "4"], ["v2", "new.dmg", "2026-10-19", "1"]]
        out = download_stats.render(rows, [], releases, "2026-10-19")
        self.assertIn("| v1 | k.dmg | 2026-10-13 | 4 | 0 | 4 |", out)
        self.assertIn("| v2 | new.dmg | 2026-10-19 | 1 | 0 | 1 |", out)

    def test_the_first_snapshot_is_labelled_as_everything_before_it(self):
        rows = [snap("2026-10-10", "a.dmg", 7), snap("2026-10-11", "a.dmg", 8)]
        out = download_stats.render(rows, [], [["v1", "a.dmg", "2026-09-26", "8"]], "2026-10-11")
        self.assertIn("| up to 2026-10-10 | a.dmg | 7 |", out)
        self.assertIn("| 2026-10-11 | a.dmg | 1 |", out)
