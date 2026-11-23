"""Doubled Beat This! detections are dropped before the tempo is fitted."""
import json, os, tempfile, unittest
from unittest import mock

import numpy as np

from mashup_finder import deep


class DedupeTest(unittest.TestCase):
    def test_a_beat_marked_twice_is_counted_once(self):
        b = np.arange(200) * 60 / 126.0
        doubled = np.sort(np.concatenate([b, b[50:150:2] + 0.08]))     # 50 doubles, as on Sweet Dreams
        self.assertEqual(deep._dedupe(doubled).tolist(), b.tolist())
        db = b[::4]
        g = deep._beat_grid(doubled, np.sort(np.concatenate([db, db[10:20] + 0.08])))
        self.assertAlmostEqual(g["bpm"], 126.0, places=1)
        self.assertEqual((len(g["beats"]), len(g["downbeats"]), g["beats_per_bar"]), (200, 50, 4))

    def test_a_clean_grid_is_left_alone(self):
        b = np.arange(64) * 0.5
        self.assertEqual(deep._dedupe(b).tolist(), b.tolist())

    def test_the_cache_keeps_the_raw_detections_and_a_read_cleans_them(self):
        b = np.arange(100) * 0.5
        raw = {"beats": np.sort(np.concatenate([b, b[::5] + 0.08])).round(3).tolist(), "downbeats": b[::4].tolist(),
               "bpm": 150.0, "beats_per_bar": 4}                      # an old cache file, bpm from the doubled grid
        with tempfile.TemporaryDirectory() as d, mock.patch.object(deep, "CACHE", d):
            f = os.path.join(d, "beats", deep._key("x.mp3") + ".json")
            os.makedirs(os.path.dirname(f))
            json.dump(raw, open(f, "w"))
            g = deep.beats("x.mp3")
        self.assertEqual((g["bpm"], len(g["beats"])), (120.0, 100))


if __name__ == "__main__":
    unittest.main()
