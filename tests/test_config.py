"""config.py: the settings file (defaults, round trip, refusals) and that every path the program uses comes from it."""
import json, os, subprocess, sys, unittest
from unittest import mock

from mashup_finder import app, cli, config
from tests import fixtures as fx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.file = os.path.join(fx.tmpdir("mf-config-"), "sub", "config.toml")
        env = mock.patch.dict(os.environ, {"MF_CONFIG": self.file})
        env.start()
        self.addCleanup(env.stop)

    def write(self, text):
        os.makedirs(os.path.dirname(self.file), exist_ok=True)
        with open(self.file, "w", encoding="utf-8") as f:
            f.write(text)

    def test_no_file_gives_the_defaults(self):
        self.assertFalse(config.exists())
        cfg = config.load()
        self.assertEqual(cfg["music_folders"], [])
        self.assertEqual(cfg["catalog"], os.path.join(os.path.dirname(self.file), "catalog.csv"))     # beside the settings
        self.assertEqual(cfg["export_folder"], os.path.expanduser("~/Music/Mashup Studio"))
        self.assertEqual(cfg["cache_folder"], os.path.join(ROOT, "_cache"))
        self.assertEqual((cfg["extra_sources"], cfg["playlists_folder"]), ([], ""))

    def test_saved_settings_load_back_and_tildes_expand(self):
        odd = 'Music/"quoted" \\ back\tslash \x7f é \U0001F3B5'
        self.assertEqual(config.save({"music_folders": ["~/Music", odd], "export_folder": "/Volumes/X/Out", "catalog": "~/c.csv"}),
                         self.file)
        cfg = config.load()
        self.assertEqual(cfg["music_folders"], [os.path.expanduser("~/Music"), odd])
        self.assertEqual((cfg["export_folder"], cfg["catalog"]), ("/Volumes/X/Out", os.path.expanduser("~/c.csv")))
        self.assertEqual(cfg["cache_folder"], config.defaults()["cache_folder"])          # left out: the default
        self.assertEqual(os.listdir(os.path.dirname(self.file)), ["config.toml"])         # no .tmp left behind

    def test_a_broken_file_stops_with_its_name(self):
        for text, why in (('catalog = "/a.csv"\ncolour = "red"\n', "unknown setting 'colour'"),
                          ('music_folders = "/Music"\n', "music_folders must be a list of paths"),
                          ("catalog = 3\n", "catalog must be a path"),
                          ("catalog = \n", "")):
            self.write(text)
            with self.assertRaises(SystemExit) as e:
                config.load()
            self.assertIn(self.file, str(e.exception))
            self.assertIn(why, str(e.exception))

    def test_every_path_the_program_uses_comes_from_the_settings(self):
        self.write('catalog = "/lib/my.csv"\nexport_folder = "/Volumes/Out/Mixes"\ncache_folder = "/scratch/cache"\n'
                   'extra_sources = ["/a/sm.json", "/b/sm.json"]\nplaylists_folder = "/lib/lists"\n')
        # Module constants are set on import: read them in a fresh interpreter.
        out = subprocess.run([sys.executable, "-c", "import json; from mashup_finder import catalog, deep, draft; "
                              "print(json.dumps([catalog.CATALOG, catalog.SUPER_MASSIVE, deep.CACHE, draft.PLAYLISTS]))"],
                             cwd=ROOT, capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(out), ["/lib/my.csv", ["/a/sm.json", "/b/sm.json"], "/scratch/cache", "/lib/lists"])
        self.assertEqual(app.App(root=fx.tmpdir("mf-config-app-")).ssd, "/Volumes/Out/Mixes")    # read when made
        with mock.patch.object(cli, "_export") as export:
            cli.main(["export", "/no/run"])
        self.assertEqual(export.call_args[0][2], "/Volumes/Out/Mixes")


if __name__ == "__main__":
    unittest.main()
