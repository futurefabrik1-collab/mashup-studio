"""The first run: with no settings file `mf app` serves the setup page; Save writes the settings, Scan now runs
`mf scan` as a job, and from the next start the Studio exports into the chosen folder. Everything in temp folders;
the folder dialog is a stand-in."""
import http.client, json, os, sys, threading, unittest
from unittest import mock

from mashup_finder import app, config
from tests import fixtures as fx
from tests.test_scan import ffmpeg

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_mf.py")


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = fx.tmpdir("mf-setup-")
        self.settings = os.path.join(self.tmp, "support", "config.toml")
        env = mock.patch.dict(os.environ, {"MF_CONFIG": self.settings})
        env.start()
        self.addCleanup(env.stop)
        self.music, self.drive, self.root = (os.path.join(self.tmp, d) for d in ("music", "drive", "root"))
        for d in (self.music, self.drive, self.root):
            os.makedirs(d)
        ffmpeg(os.path.join(self.music, "a.mp3"), "-metadata", "artist=Artist A", "-metadata", "title=Song A",
               "-metadata", "TBPM=128", "-metadata", "TKEY=Am")
        self.picked = []
        self.srv = app.make_server(root=self.root, mf=[sys.executable, "-u", FAKE, self.root], trash=fx.tmpdir("mf-setup-trash-"),
                                   claude_check=lambda: {"works": False, "why": "stand-in"},
                                   folder_picker=lambda what: self.picked.append(what) or self.music)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def call(self, method, path, body=None, raw=False):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        c.request(method, path, body=None if body is None else json.dumps(body).encode(),
                  headers={"Host": f"127.0.0.1:{self.port}", "X-Token": self.srv.token})
        r = c.getresponse()
        out = r.read()
        c.close()
        return (r.status, out.decode()) if raw else (r.status, json.loads(out or b"null"))

    def test_first_run_setup_save_scan_and_the_export_folder(self):
        code, page = self.call("GET", "/", raw=True)
        self.assertEqual(code, 200)
        self.assertIn("Where is your music?", page)
        self.assertNotIn(self.srv.token, page)                           # the page reads it from its address
        self.assertNotIn("/* studio.css */", page)
        self.assertNotIn('data-view="briefs"', page)
        code, s = self.call("GET", "/api/setup")
        self.assertEqual((code, s["saved"], s["catalog_ok"], s["file"]), (200, False, False, self.settings))
        self.assertEqual(s["catalog"], os.path.join(self.tmp, "support", "catalog.csv"))
        self.assertEqual(self.call("POST", "/api/scan", {})[1]["error"], "save the folders first")

        self.assertEqual(self.call("POST", "/api/pick-folder", {"what": "music"})[1], {"ok": True, "path": self.music})
        self.assertEqual((self.call("POST", "/api/pick-folder", {"what": "home"})[0], self.picked), (400, ["music"]))

        export = os.path.join(self.drive, "Mashups")                    # not there yet: made, inside a folder that is
        for music, out, why in (([os.path.join(self.tmp, "gone")], export, "is not a folder"),
                                ([], export, "add your music folder"),
                                ([self.music], os.path.join(self.tmp, "unplugged", "Mashups"), "is not there"),
                                ([self.music], "Mashups", "choose the export folder"),
                                ([self.music], self.music + "/", "cannot be a music folder")):
            code, j = self.call("POST", "/api/setup", {"music_folders": music, "export_folder": out})
            self.assertEqual(code, 400)
            self.assertIn(why, j["error"])
        self.assertFalse(os.path.exists(self.settings) or os.path.exists(export))     # nothing written by a refusal

        code, j = self.call("POST", "/api/setup", {"music_folders": [self.music + "/", self.music], "export_folder": export + "/"})
        self.assertEqual((code, j["file"]), (200, self.settings))
        self.assertTrue(os.path.isdir(export))
        self.assertEqual((config.load()["music_folders"], config.load()["export_folder"]), ([self.music], export))
        self.assertEqual(self.srv.app.ssd, export)                       # this Studio exports there from now on
        self.assertIn("Where is your music?", self.call("GET", "/", raw=True)[1])      # until it restarts

        code, j = self.call("POST", "/api/scan", {})
        self.assertEqual(code, 200)
        self.assertEqual(self.srv.app.jobs.wait(j["job"], 60), "done", self.srv.app.jobs.state())
        last = self.call("GET", "/api/jobs")[1][-1]
        self.assertEqual((last["title"], last["lines"][0]), ("scan the music folders", "scan: 1/1 Artist A - Song A"))
        self.assertTrue(self.call("GET", "/api/setup")[1]["catalog_ok"])

        # The next start: the Studio's page, and Copy to export folder files a mashup under the chosen folder.
        studio = app.App(root=self.root)
        self.assertFalse(studio.first_run)
        self.assertEqual(studio.ssd, export)
        pair = {"vocal_label": "Artist A - Song A", "inst_label": "Artist B - Track B", "vocal_key": "A minor 8A",
                "inst_key": "C major 8B"}
        self.assertEqual(studio._ssd_path(pair, None, {"template": "club", "v": 1}),
                         os.path.join(export, "Artist A - Song A x Artist B - Track B (8A-8B)", "MASHUP - club"))
        self.assertEqual(self.call("GET", "/setup", raw=True)[0], 200)     # the settings stay reachable

    def test_the_studio_page_says_export_folder(self):
        with open(app.PAGE, encoding="utf-8") as f:
            page = f.read()
        self.assertIn("Copy to export folder", page)
        self.assertNotIn("SSD", page)


if __name__ == "__main__":
    unittest.main()
