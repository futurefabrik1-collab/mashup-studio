import unittest

from mashup_finder import trackmap
from tests import fixtures as fx


class TrackMapTest(unittest.TestCase):
    def test_sections_follow_the_layout(self):
        st, bt = fx.b_track(126.0)
        m = trackmap.track_map(st, bt, phrases=False)
        self.assertEqual(m["bars"], sum(n for n, _ in fx.B_LAYOUT))
        self.assertEqual([s["label"] for s in m["sections"]], fx.B_LABELS)
        self.assertEqual([s["bars"] for s in m["sections"]], [n for n, _ in fx.B_LAYOUT])

    def test_vocal_phrases_and_roles(self):
        st, bt = fx.a_track(120.0)
        m = trackmap.track_map(st, bt)
        roles = {p["start"]: p["role"] for p in m["phrases"]}
        self.assertEqual(roles, {16: "verse", 32: "hook", 48: "verse", 64: "hook", 80: "hook", 90: "adlib"})
        best = min(m["phrases"], key=lambda p: p["rank"])
        self.assertEqual(best["start"], 32)                    # the loudest hook ranks first
        self.assertTrue(all(p["bars"] in (4, 8) for p in m["phrases"]))


if __name__ == "__main__":
    unittest.main()
