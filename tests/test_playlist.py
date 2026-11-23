"""A brief's source playlists (`from`): a rekordbox export finds its tracks in the catalog by any copy and either
unicode form, says what it could not find, and the Studio can pick or take one in."""
import csv, os, unicodedata, unittest

from mashup_finder import app, catalog, criteria
from tests import fixtures as fx

ROWS = [("Björk", "Hyperballad", 126, "8A", "Electronic", 1996), ("Röyksopp", "Eple", 126, "8A", "Electronic", 2001),
        ("Artist B", "Bass Track", 126, "10B", "Breaks", 2020), ("Slow One", "No Tempo", 0, "8A", "Ambient", 2000)]


class PlaylistTest(unittest.TestCase):
    def setUp(self):
        self.lib = fx.tmpdir("mf-pl-")
        fx.catalog_csv(self.lib, ROWS)
        self.path = {t: os.path.join(self.lib, "library", f"{a} - {t}.mp3") for a, t, *_ in ROWS}
        self.copy = os.path.join(self.lib, "elsewhere", "02 Bass Track (USB copy).mp3")       # another copy, not the best
        with open(os.path.join(self.lib, "catalog-all-files.csv"), "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["artist", "title", "path"])
            w.writeheader()
            for a, t, *_ in ROWS:
                w.writerow({"artist": a, "title": t, "path": self.path[t]})
            w.writerow({"artist": "Artist B", "title": "Bass Track", "path": self.copy})
        self.m3u = os.path.join(self.lib, "Set.m3u8")
        with open(self.m3u, "w", encoding="utf-8") as fh:                                   # what rekordbox writes
            fh.write("#EXTM3U\n#EXTINF:300,Björk - Hyperballad\n" + unicodedata.normalize("NFD", self.path["Hyperballad"]) + "\n"
                     f"{self.copy}\n{self.path['Eple']}\n{self.path['Eple']}\n/gone/Nobody - Nothing.mp3\n{self.path['No Tempo']}\n")
        with fx.fixture_catalog(self.lib):
            self.cat = catalog.Catalog()

    def test_any_copy_and_either_unicode_form_is_found_and_the_rest_is_said(self):
        p = criteria.playlist(self.cat, [self.m3u])
        self.assertEqual((p["found"], p["total"]), (3, 5))                           # the repeated line counts once
        self.assertEqual(sorted(self.cat.rows[i]["title"] for i in p["idx"]), ["Bass Track", "Eple", "Hyperballad"])
        self.assertEqual([(os.path.basename(m["path"]), m["why"]) for m in p["missing"]],
                         [("Nobody - Nothing.mp3", "not in the catalog"), ("Slow One - No Tempo.mp3", criteria.LEFT_OUT)])

    def test_the_brief_uses_them_and_its_line_says_how_many_were_found(self):
        b = {"vocal": {"from": [self.m3u]}, "inst": {"tempo": [126]}}
        self.assertEqual(sorted(self.cat.rows[i]["title"] for i in criteria.select(self.cat, b["vocal"])), ["Bass Track", "Eple", "Hyperballad"])
        self.assertEqual(criteria.describe(self.cat, b), "vocal: 3 tracks (playlist: 3 of 5 found) · inst: 3 tracks")

    def test_a_csv_with_a_path_column_works_too_and_a_missing_file_is_said(self):
        f = os.path.join(self.lib, "split.csv")
        with open(f, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=["no", "side", "path"])
            w.writeheader()
            w.writerow({"no": 1, "side": "non-bass", "path": self.copy})
        self.assertEqual(criteria.playlist(self.cat, [f])["found"], 1)
        with self.assertRaises(ValueError) as e:
            criteria.playlist(self.cat, [os.path.join(self.lib, "nope.m3u8")])
        self.assertIn("playlist not found", str(e.exception))

    def test_the_live_count_says_it_per_side(self):
        a = app.App(root=fx.tmpdir("mf-pl-root-"), ssd="/nonexistent", load_catalog=lambda: self.cat)
        c = a.count(f'name = "x"\n[vocal]\nfrom = ["{self.m3u}"]\n[inst]\ntempo = [126]\n')
        self.assertEqual(set(c["playlists"]), {"vocal"})
        v = c["playlists"]["vocal"]
        self.assertEqual((v["found"], v["total"], len(v["missing"]), v["more"]), (3, 5, 2, 0))

    def test_a_dropped_playlist_is_kept_in_briefs_and_a_picked_one_is_checked(self):
        root = fx.tmpdir("mf-pl-root-")
        picked = []
        a = app.App(root=root, ssd="/nonexistent", picker=lambda: picked.pop() if picked else None)
        r1 = a.save_playlist("Set.m3u8", "#EXTM3U\n/a.mp3\n")
        self.assertEqual(r1["path"], os.path.join(root, "briefs", "playlists", "Set.m3u8"))
        self.assertEqual(a.save_playlist("Set.m3u8", "#EXTM3U\n/a.mp3\n"), r1)           # same file again: the same one
        self.assertEqual(os.path.basename(a.save_playlist("Set.m3u8", "#EXTM3U\n/b.mp3\n")["path"]), "Set 2.m3u8")
        for bad in ("notes.txt", "../x.m3u8.exe"):
            with self.assertRaises(ValueError):
                a.save_playlist(bad, "x")
        self.assertEqual(os.path.basename(a.save_playlist("../../evil.m3u8", "x")["path"]), "evil.m3u8")   # stays in briefs/playlists
        self.assertEqual(a.pick_playlist(), {"path": None})                                     # cancelled
        picked.append(self.m3u)
        self.assertEqual(a.pick_playlist(), {"path": self.m3u})
        picked.append(os.path.join(self.lib, "catalog.txt"))
        with self.assertRaises(ValueError):
            a.pick_playlist()


if __name__ == "__main__":
    unittest.main()
