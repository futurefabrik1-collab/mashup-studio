import json, os, tempfile, unittest

from mashup_finder import arrange, trackmap
from tests import fixtures as fx


def maps(bpm_a=120.0, bpm_b=126.0, b_layout=fx.B_LAYOUT):
    sa, ba = fx.a_track(bpm_a)
    sb, bb = fx.track(bpm_b, b_layout)
    return trackmap.track_map(sa, ba), trackmap.track_map(sb, bb, phrases=False)


def lane_cover(arr, lane, a, b):
    """Output bars in [a, b) covered by blocks of `lane`, each bar once."""
    bars = [k for blk in arr["lanes"][lane] for k in range(blk["out_bar"], blk["out_bar"] + int(blk["bars"]))]
    return sorted(k for k in bars if a <= k < b)


class ArrangeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ma, cls.mb = maps()

    def check_valid(self, arr, tpl):                                # done-when 5
        o = 0
        for s, t in zip(arr["sections"], tpl["sections"]):
            self.assertEqual((s["out_bar"], s["bars"]), (o, t["bars"]))   # contiguous, no gaps or overlaps
            for stem in t["inst_stems"]:
                if stem in arr["lanes"] and not (stem == "drums" and "drop without drums" in " ".join(s["fallbacks"])):
                    self.assertEqual(lane_cover(arr, stem, o, o + t["bars"]), list(range(o, o + t["bars"])), (s, stem))
            if s.get("vocal"):
                lo = o + t["bars"] - t["vocal_bars"]
                self.assertEqual(lane_cover(arr, "vocal", o, o + t["bars"]), list(range(lo, o + t["bars"])))
            o += t["bars"]
        self.assertEqual(arr["bars"], o)
        m = arr["tempo_mult"]
        for lane, blocks in arr["lanes"].items():
            for b in blocks:
                n_src, span = (arr["a_bars"], b["bars"] / m) if b["src"] == "A" else (arr["b_bars"], b["bars"])
                pre = max([-f["at"] for f in b["fx"] if f["type"] == "pickup"] or [0]) / (m if b["src"] == "A" else 1)
                self.assertTrue(0 <= b["src_bar"] - pre and b["src_bar"] + span <= n_src, b)   # source inside its track
                self.assertTrue(0 <= b["out_bar"] and b["out_bar"] + b["bars"] <= arr["bars"], b)

    def test_all_templates_are_valid(self):
        for name in ("club", "dj-tool", "short"):
            tpl = arrange.load_template(name)
            for v in arrange.variations(self.ma, self.mb, tpl, 1.0, 2, n=3, seed=1):
                self.check_valid(v, tpl)

    def test_club_uses_the_loudest_hook_and_both_drops(self):
        arr = arrange.arrange(self.ma, self.mb, arrange.load_template("club"), 1.0, 2)
        self.assertEqual([s["vocal"]["phrase"] for s in arr["sections"] if s.get("vocal")], [32, 32, 16, 32])
        self.assertEqual([s["b_label"] for s in arr["sections"]], ["intro", "build", "drop", "break", "drop", "outro"])
        self.assertEqual(arr["fallbacks"], [])
        src = [{b["src_bar"] for b in arr["lanes"]["drums"] if s["out_bar"] <= b["out_bar"] < s["out_bar"] + s["bars"]}
               for s in arr["sections"] if s["inst"] == "drop"]       # the drums of each drop section, wherever it sits
        self.assertEqual([len(x) for x in src], [1, 1])               # each drop plays one of B's drops (looped)
        self.assertEqual(len(src[0] | src[1]), 2)                     # the two drops use B's two drop sections
        self.assertTrue(all(b["shift"] == 2 and b["gain_db"] == arr["lanes"]["vocal"][0]["gain_db"] for b in arr["lanes"]["vocal"]))

    def test_fallbacks_are_recorded(self):
        no_break = [(8, "d-O"), (8, "D-O"), (16, "DBO"), (16, "DBO"), (8, "d-o")]
        ma, mb = maps(b_layout=no_break)
        arr = arrange.arrange(ma, mb, arrange.load_template("club"), 1.0, 0)
        brk = next(s for s in arr["sections"] if s["inst"] == "break")
        self.assertEqual(brk["b_label"], "drop")
        self.assertEqual(lane_cover(arr, "drums", brk["out_bar"], brk["out_bar"] + brk["bars"]), [])
        self.assertIn("break: no break in B: a drop without drums", arr["fallbacks"])

    def test_variations_differ_and_repeat_exactly(self):           # done-when 7 (arrangement side)
        tpl = arrange.load_template("club")
        dump = lambda seed: [json.dumps(v, indent=1) for v in arrange.variations(self.ma, self.mb, tpl, 1.0, 2, n=3, seed=seed)]
        first = dump(1)
        self.assertEqual(len({arrange.signature(json.loads(v)) for v in first}), 3)
        self.assertEqual(first, dump(1))
        self.assertNotEqual(first, dump(2))

    def test_half_time_maps_one_a_bar_to_two_output_bars(self):    # done-when 4 (arrangement side)
        ma, mb = maps(87.0, 174.0)
        m = arrange.tempo_mult(ma["bpm"], mb["bpm"])
        self.assertEqual(m, 2.0)
        arr = arrange.arrange(ma, mb, arrange.load_template("short"), m, 0)
        self.assertEqual(arr["tempo_mult"], 2.0)
        self.check_valid(arr, arrange.load_template("short"))

    def test_hand_edited_arrangement_is_checked(self):
        arr = arrange.arrange(self.ma, self.mb, arrange.load_template("short"), 1.0, 0)
        arr.update(output_bpm=126.0, sources={arrange.source_name(s, l): {} for s, l in (("A", "vocal"), ("B", "drums"), ("B", "bass"), ("B", "other"))})
        cases = [(lambda a: a["lanes"].update(keys=[]), "unknown lane"),
                 (lambda a: a["lanes"]["drums"][0].update(src="A"), "has no drums source"),
                 (lambda a: a["lanes"]["drums"][0].update(out_bar=-4), "out_bar"),
                 (lambda a: a["lanes"]["bass"][0].update(bars=0), "bars must be"),
                 (lambda a: a["lanes"]["drums"].append(dict(a["lanes"]["bass"][0])), "says lane 'bass'"),
                 (lambda a: a["lanes"]["drums"][0].update(fade_out=a["lanes"]["drums"][0]["bars"] + 1), "fade_out must be"),
                 (lambda a: a["lanes"]["drums"][0].update(fade_in=-1), "fade_in must be"),
                 (lambda a: a["lanes"]["drums"][0].update(fade_in="slow"), "fade_in must be")]
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "arrangement.json")
            with open(f, "w") as fh:
                json.dump(arr, fh)
            self.assertEqual(arrange.load_arrangement(f)["lanes"], arr["lanes"])
            beat = json.loads(json.dumps(arr))
            beat["lanes"]["vocal"][0]["out_bar"] = 4.25                    # the editor's 1-beat snap
            with open(f, "w") as fh:
                json.dump(beat, fh)
            self.assertEqual(arrange.load_arrangement(f)["lanes"]["vocal"][0]["out_bar"], 4.25)
            for edit, msg in cases:
                bad = json.loads(json.dumps(arr))
                edit(bad)
                with open(f, "w") as fh:
                    json.dump(bad, fh)
                with self.assertRaisesRegex(ValueError, msg):
                    arrange.load_arrangement(f)

    def test_bad_template_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "bad.toml")
            with open(f, "w") as fh:
                fh.write('[[section]]\nbars = 6\ninst = "drop"\n')
            with self.assertRaisesRegex(ValueError, "multiple of 4"):
                arrange.load_template(f)


if __name__ == "__main__":
    unittest.main()


class LengthenTest(unittest.TestCase):
    MB = {"bars": 64, "sections": [{"label": "intro", "start": 0, "bars": 4}, {"label": "build", "start": 4, "bars": 8},
                                   {"label": "drop", "start": 12, "bars": 16}, {"label": "break", "start": 28, "bars": 4},
                                   {"label": "drop", "start": 32, "bars": 32}]}

    def test_a_short_intro_runs_on_into_the_track_instead_of_looping_4_bars(self):
        self.assertEqual(arrange._lengthen(self.MB, 0, 4, "intro", 32), (0, 12))     # up to the first drop, not into it

    def test_a_short_break_grows_backward_when_a_drop_follows(self):
        self.assertEqual(arrange._lengthen(self.MB, 28, 32, "break", 32), (28, 32))  # boxed in by drops: left as is

    def test_drops_and_long_enough_sources_are_left_alone(self):
        self.assertEqual(arrange._lengthen(self.MB, 12, 20, "drop", 32), (12, 20))
        self.assertEqual(arrange._lengthen(self.MB, 4, 12, "build", 8), (4, 12))

    def test_no_section_plays_the_same_4_bars_more_than_twice_when_b_allows(self):
        ma, mb = maps()
        first = mb["sections"][0]
        self.assertGreater(first["bars"], 4)
        mb = dict(mb, sections=[{"label": "intro", "start": 0, "bars": 4},
                                {"label": "build", "start": 4, "bars": first["bars"] - 4}] + mb["sections"][1:])
        arr = arrange.arrange(ma, mb, arrange.load_template("club"), 1.0, 0)
        intro = arr["sections"][0]
        blocks = [b for b in arr["lanes"]["drums"] if b["out_bar"] < intro["bars"]]
        self.assertLessEqual(len(blocks), intro["bars"] // 8, blocks)          # was 8 blocks of the same 4 bars
        self.assertTrue(all(b["bars"] >= 8 for b in blocks), blocks)
