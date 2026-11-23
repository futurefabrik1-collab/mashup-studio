"""The catalog match (cli.quick_match, `mf find`/`mf run`) on a small fixture catalog: [song] briefs and the counts
the app's live line shows. Nothing reads the real catalog: catalog.CATALOG points at tests/fixtures' CSV."""
import contextlib, io, json, os, unittest
from unittest import mock

from mashup_finder import audition, catalog, cli, deep
from tests import fixtures as fx


class FindTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = fx.tmpdir("mf-find-")
        with fx.fixture_catalog(cls.dir):
            cls.cat = catalog.Catalog()

    def setUp(self):                                                    # `mf find` loads the catalog itself
        self.enterContext(fx.fixture_catalog(self.dir))
        self.enterContext(mock.patch.object(cli, "RUNS", fx.tmpdir("mf-find-runs-")))

    def run_find(self, *args, brief=None, folder=False, name="t"):
        """`mf find` with these arguments (and a brief's text); the shortlist's (vocal, inst) labels, and with
        folder=True the run folder's name too."""
        if brief is not None:
            f = os.path.join(fx.tmpdir("mf-brief-"), "b.toml")
            with open(f, "w") as fh:
                fh.write(brief)
            args += ("--brief", f)
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["find", *args] + (["--name", name] if name else []))
        run = os.path.join(cli.RUNS, os.listdir(cli.RUNS)[0])
        with open(os.path.join(run, "shortlist.json")) as f:
            pairs = json.load(f)["pairs"]
        for d in os.listdir(cli.RUNS):
            os.rename(os.path.join(cli.RUNS, d), os.path.join(fx.tmpdir("mf-old-run-"), d))
        labels = [(p["vocal_label"], p["inst_label"]) for p in pairs]
        return (labels, os.path.basename(run)) if folder else labels

    def test_a_song_brief_finds_what_seed_and_role_find(self):
        for role, flag in (("vocal", "vocal"), ("beat", "inst"), ("either", "any")):
            song = self.run_find(brief=f'name = "s"\n[song]\ntext = "a view to a kill"\nrole = "{role}"\n')
            seed = self.run_find("--seed", "a view to a kill", "--role", flag)
            self.assertEqual(song, seed, role)
            self.assertTrue(song, role)
            side = {"vocal": lambda p: "A View To A Kill" in p[0], "beat": lambda p: "A View To A Kill" in p[1],
                    "either": lambda p: "A View To A Kill" in p[0] + p[1]}[role]
            self.assertTrue(all(map(side, song)), (role, song))
        both = self.run_find(brief='name = "s"\n[song]\ntext = "a view to a kill"\n')         # role: either by default
        self.assertTrue(any("A View To A Kill" in v for v, _ in both) and any("A View To A Kill" in i for _, i in both))

    def test_per_vocal_still_caps_a_song_brief(self):
        capped = self.run_find(brief='name = "s"\n[song]\ntext = "hungry like the wolf"\nrole = "vocal"\n[match]\nper_vocal = 2\n')
        self.assertEqual(len(capped), 2)
        self.assertGreater(len(self.run_find(brief='name = "s"\n[song]\ntext = "hungry like the wolf"\nrole = "vocal"\n[match]\nper_vocal = 6\n')), 2)

    def test_a_song_briefs_run_is_named_after_the_brief(self):            # so --fresh finds its earlier runs
        _, run = self.run_find(brief='name = "club-song"\n[song]\ntext = "only you"\n', folder=True, name=None)
        self.assertTrue(run.endswith("-club-song"), run)
        for song, said in (('text = "only you"\nrole = "inst"', "role is one of vocal, beat, either"),
                           ('role = "vocal"', "text must name the song")):            # never a whole-library run instead
            with self.assertRaises(SystemExit) as e:
                self.run_find(brief=f'name = "s"\n[song]\n{song}\n')
            self.assertIn(said, str(e.exception))

    def test_command_line_seed_wins_over_the_brief(self):
        got = self.run_find("--seed", "only you", "--role", "vocal", brief='name = "s"\n[song]\ntext = "a view to a kill"\nrole = "vocal"\n')
        self.assertTrue(got and all(v == "Yazoo - Only You" for v, _ in got), got)

    def test_ratings_move_what_resembles_them_and_nothing_else(self):
        from mashup_finder.pairs import W
        path = {self.cat.label(i): self.cat.rows[i]["path"] for i in range(self.cat.n)}
        brief = {"inst": {"tempo": [126]}, "match": {"per_vocal": 3, "per_inst": 3, "limit": 30}}
        base, _ = cli.quick_match(self.cat, brief)
        same, _ = cli.quick_match(self.cat, brief, rated=[])
        self.assertEqual(base, same)                                    # no ratings: the same pairs, the same order
        yazoo = lambda p: self.cat.label(p["vocal"]).startswith("Yazoo")
        rank = lambda P, who: sum(i for i, p in enumerate(P) if yazoo(p)) / sum(map(yazoo, P))   # mean place of its pairs
        loved = [(path["Yazoo - Only You"], path["Beat Maker 1 - Groove 1 (Instrumental)"], 5)]
        up, _ = cli.quick_match(self.cat, brief, rated=loved)
        hated = [(path["Yazoo - Only You"], path["Beat Maker 1 - Groove 1 (Instrumental)"], 1)]
        down, _ = cli.quick_match(self.cat, brief, rated=hated)
        self.assertLess(rank(up, "Yazoo"), rank(base, "Yazoo"))         # the rated vocal artist moves up ...
        self.assertGreater(rank(down, "Yazoo"), rank(base, "Yazoo"))    # ... or down
        self.assertTrue(all(abs(p["taste"]) <= W["taste"] for p in up + down))   # capped
        for P in (up, down):                                           # the rated vocal artist moves most, others
            self.assertGreater(min(abs(p["taste"]) for p in P if yazoo(p)), max(abs(p["taste"]) for p in P if not yazoo(p)))

    def test_versions_of_one_song_count_once_on_both_sides(self):
        from mashup_finder.pairs import diversify
        song = lambda i: (self.cat.artist[i].replace("&", "and"), self.cat.base[i])     # as quick_match passes it
        idx = {self.cat.label(i): i for i in range(self.cat.n)}
        wolf = [idx["Duran Duran - Hungry Like The Wolf"], idx["duran duran - hungry like the wolf (night version)"]]
        other = [idx["Yazoo - Only You"], idx["Kate Bush - Running Up That Hill"]]
        as_vocal = [{"vocal": w, "inst": o} for w, o in zip(wolf, other)]
        as_beat = [{"vocal": o, "inst": w} for w, o in zip(wolf, other)]
        self.assertEqual(len(diversify(as_vocal, per_vocal=1, per_inst=5, song=song)), 1)
        self.assertEqual(len(diversify(as_beat, per_vocal=5, per_inst=1, song=song)), 1)    # one beat, in two versions
        self.assertEqual(len(diversify(as_beat, per_vocal=5, per_inst=1)), 2)               # by track, as before

    def test_counts_and_the_line_the_page_shows(self):
        P, n = cli.quick_match(self.cat, {"song": {"text": "a view to a kill", "role": "vocal"}, "inst": {"tempo": [126]}},
                               "a view to a kill", "vocal")
        line, why = cli.match_line(n)
        self.assertEqual((len(n["hits"]), n["sides"], why), (2, [[2, 14]], None))
        self.assertEqual(line, f"2 vocals × 14 beats → {len(P)} pair{'s' * (len(P) != 1)}")
        _, n = cli.quick_match(self.cat, {}, "no such song")
        self.assertEqual(cli.match_line(n), ("0 tracks → 0 pairs", "no track matches the song"))
        _, n = cli.quick_match(self.cat, {"inst": {"tempo": [150]}})
        self.assertEqual(cli.match_line(n)[1], "no beat passes the [inst] filters")
        _, n = cli.quick_match(self.cat, {"vocal": {"years": [1950, 1951]}})
        self.assertEqual(cli.match_line(n)[1], "no vocal passes the [vocal] filters")
        _, n = cli.quick_match(self.cat, {"vocal": {"include": ["back to life"]}, "inst": {"tempo": [174]}, "match": {"max_stretch": 2}})
        self.assertIn("within 2% of each other's tempo", cli.match_line(n)[1])
        _, n = cli.quick_match(self.cat, {}, "groove 1", "vocal")                # instrumentals asked to give the vocal
        self.assertEqual(cli.match_line(n)[1], "the song cannot give the vocal (an instrumental, or left out by [vocal])")
        self.assertEqual(cli.match_line({"hits": [3], "role": "any", "sides": [[1, 19052], [19052, 1]], "pairs": 30, "max_stretch": 8.0})[0],
                         "1 vocal × 19,052 beats + 19,052 vocals × 1 beat → 30 pairs")


class CheckTest(unittest.TestCase):
    """mf check's order (best promise first) and its results.json, which the app reads while it runs."""

    def test_the_audio_check_goes_by_promise_and_results_stay_whole(self):
        run = fx.tmpdir("mf-check-")
        spec = [(0.06, 0, 90.0), (0.00, 3, 80.0), (0.01, 0, 70.0), (0.01, 0, 75.0), (-0.02, -1, 60.0)]   # stretch, shift, score
        pairs = [{"id": i, "stretch": st, "shift": sh, "score": sc, "vocal_label": f"v{i}", "inst_label": f"i{i}",
                  "vocal_path": f"/v{i}", "inst_path": f"/i{i}"} for i, (st, sh, sc) in enumerate(spec)]
        with open(os.path.join(run, "shortlist.json"), "w") as f:
            json.dump({"args": {"top": 4}, "brief": {}, "pairs": pairs}, f)
        order, seen = [], []

        def analyse(a, b, p, outdir, log, max_stretch=None):
            res = os.path.join(run, "results.json")
            if os.path.exists(res):
                with open(res) as f:
                    seen.append(len(json.load(f)))                        # whole JSON at every step
            order.append(p["id"])
            return {"ok": True, "stretch_audio": p["stretch"], "shift_audio": p["shift"], "harm_z": p["id"] * 0.5, "harm_rank": 1,
                    "vocal_active": 0.7, "vocal_share": 0.3, "drift_ms": 0}
        log = []
        with mock.patch.object(deep, "analyse_pair", analyse), mock.patch.object(audition, "write", lambda *a: "page"), \
                mock.patch.object(cli, "_prune_cache", lambda: None):
            cli._check(run, 4, log=log.append)
        self.assertEqual(order, [3, 2, 0, 1])                           # 300|stretch| + 12|shift|, then score: id 4 is past top
        self.assertEqual(seen, [1, 2, 3])
        self.assertEqual([l[:5] for l in log if l.startswith("[")], ["[1/4]", "[2/4]", "[3/4]", "[4/4]"])
        with open(os.path.join(run, "results.json")) as f:
            res = json.load(f)
        self.assertEqual([p["id"] for p in res], [0, 3, 1, 2])          # ranked by final at the end, as before (score + 4 x id here)


if __name__ == "__main__":
    unittest.main()
