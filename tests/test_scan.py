"""`mf scan` (scan.py) on audio made here by ffmpeg: tags read, untagged files analysed (stub analysers, except one
real key estimate), the catalog's columns, incremental rescans, cancel, and the refusals that protect a catalog."""
import csv, os, shutil, subprocess, sys, unittest
from unittest import mock

from mashup_finder import catalog, scan
from tests import fixtures as fx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def ffmpeg(out, *args, tone="sine=f=440:d=3"):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", tone, *args, out], check=True)


def chord(out, freqs):
    """A sustained chord of sine tones, 20 s."""
    ins = [a for f in freqs for a in ("-f", "lavfi", "-i", f"sine=f={f}:d=20")]
    subprocess.run(["ffmpeg", "-v", "error", "-y", *ins, "-filter_complex", f"amix=inputs={len(freqs)}", "-ar", "22050", out],
                   check=True)


def library(d):
    """Five audio files (three tagged, two not), plus what a scan must pass over."""
    tag = lambda **kv: [a for k, v in kv.items() for a in ("-metadata", f"{k}={v}")]
    ffmpeg(os.path.join(d, "a.mp3"), *tag(artist="Artist A", title="Song A", album="Album A", genre="House", date="1999-05-01",
                                          TBPM="128", TKEY="Am"))
    os.makedirs(os.path.join(d, "sub"))
    ffmpeg(os.path.join(d, "sub", "b.flac"), *tag(artist="Artist B", title="Song B", BPM="174", INITIALKEY="8B"))
    ffmpeg(os.path.join(d, "sub", "c.aiff"), "-write_id3v2", "1", *tag(artist="Artist C", title="Song C", TBPM="100", TKEY="C"))
    ffmpeg(os.path.join(d, "Some Artist - Some Song.wav"))                                     # no tags at all
    ffmpeg(os.path.join(d, "sub", "e.m4a"), "-c:a", "aac", *tag(artist="Artist E", title="Song E"))   # no BPM, no key
    ffmpeg(os.path.join(d, "._a.mp3"))                                                         # a resource fork
    os.makedirs(os.path.join(d, ".hidden"))
    ffmpeg(os.path.join(d, ".hidden", "x.mp3"))
    with open(os.path.join(d, "notes.txt"), "w") as f:
        f.write("not audio")


class ScanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = os.path.join(fx.tmpdir("mf-scan-lib-"), "music")
        os.makedirs(cls.lib)
        library(cls.lib)

    def setUp(self):
        self.tmp = fx.tmpdir("mf-scan-")
        self.music = shutil.copytree(self.lib, os.path.join(self.tmp, "music"))
        self.csv = os.path.join(self.tmp, "support", "catalog.csv")
        self.calls, self.lines = [], []

    def bpm(self, path):
        self.calls.append(("bpm", os.path.basename(path)))
        return 123.0

    def key(self, path):
        self.calls.append(("key", os.path.basename(path)))
        return "5A"

    def scan(self, **kw):
        self.calls, self.lines = [], []
        return scan.scan([self.music], self.csv, **{"bpm_fn": self.bpm, "key_fn": self.key, "log": self.lines.append, **kw})

    def rows(self):
        with open(self.csv, encoding="utf-8", newline="") as f:
            return {os.path.relpath(r["path"], self.music): r for r in csv.DictReader(f)}

    def test_tags_are_used_untagged_files_analysed_in_the_catalogs_columns(self):
        self.assertEqual(self.scan(), {"read": 5, "kept": 0, "gone": 0, "failed": 0})
        with open(self.csv, encoding="utf-8") as f, open(fx.catalog_csv(fx.tmpdir("mf-scan-fx-")), encoding="utf-8-sig") as g:
            self.assertEqual(f.readline(), g.readline())                   # the header of the catalog the tests use
        rows = self.rows()
        self.assertEqual(sorted(rows), ["Some Artist - Some Song.wav", "a.mp3", "sub/b.flac", "sub/c.aiff", "sub/e.m4a"])
        a = rows["a.mp3"]
        self.assertEqual({k: a[k] for k in ("artist", "title", "album", "genre", "year", "bpm", "bpm_src", "key", "key_camelot",
                                            "fmt", "kind", "dur")},
                         {"artist": "Artist A", "title": "Song A", "album": "Album A", "genre": "House", "year": "1999",
                          "bpm": "128.0", "bpm_src": "tag", "key": "Am", "key_camelot": "8A", "fmt": "mp3", "kind": "track",
                          "dur": "3.0"})
        self.assertEqual([rows[f][k] for f in ("sub/b.flac", "sub/c.aiff") for k in ("bpm", "key_camelot")], ["174.0", "8B", "100.0", "8B"])
        w = rows["Some Artist - Some Song.wav"]
        self.assertEqual((w["artist"], w["title"], w["bpm"], w["bpm_src"], w["key"], w["key_camelot"]),
                         ("Some Artist", "Some Song", "123.0", "beat_this", "5A", "5A"))
        self.assertEqual((rows["sub/e.m4a"]["artist"], rows["sub/e.m4a"]["bpm_src"]), ("Artist E", "beat_this"))
        self.assertEqual(sorted(self.calls), [("bpm", "Some Artist - Some Song.wav"), ("bpm", "e.m4a"),
                                              ("key", "Some Artist - Some Song.wav"), ("key", "e.m4a")])
        self.assertEqual(self.lines[0], "scan: 1/5 Some Artist - Some Song")
        self.assertTrue(self.lines[-1].startswith("scan: done: 5 read, 0 unchanged, 0 gone, 0 failed -> "))
        with mock.patch.object(catalog, "CATALOG", self.csv), mock.patch.object(catalog, "SUPER_MASSIVE", []):
            self.assertEqual(catalog.Catalog(min_dur=1).n, 5)              # the search reads every row

    def test_a_second_scan_reads_nothing_new_then_follows_changes(self):
        self.scan()
        first = text(self.csv)
        self.assertEqual(self.scan(), {"read": 0, "kept": 5, "gone": 0, "failed": 0})
        self.assertEqual((self.calls, text(self.csv)), ([], first))
        st = os.stat(os.path.join(self.music, "sub", "e.m4a"))
        os.utime(os.path.join(self.music, "sub", "e.m4a"), ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
        os.remove(os.path.join(self.music, "a.mp3"))
        self.assertEqual(self.scan(), {"read": 1, "kept": 3, "gone": 1, "failed": 0})
        self.assertEqual(sorted(self.calls), [("bpm", "e.m4a"), ("key", "e.m4a")])
        self.assertNotIn("a.mp3", self.rows())

    def test_an_unreadable_file_is_skipped_and_the_rest_written(self):
        with open(os.path.join(self.music, "broken.mp3"), "wb") as f:
            f.write(b"not audio at all")
        self.assertEqual(self.scan()["failed"], 1)
        self.assertEqual(len(self.rows()), 5)
        self.assertTrue(any(l.startswith("scan: 3/6 skipped ") and "broken.mp3" in l for l in self.lines), self.lines)
        self.assertEqual(self.scan()["failed"], 1)                         # tried again next time, not remembered

    def test_cancel_writes_what_was_read_and_keeps_the_rest(self):
        self.scan()
        for f in ("Some Artist - Some Song.wav", "sub/e.m4a"):             # both need analysis again
            os.utime(os.path.join(self.music, f), ns=(0, 10**18))

        def cancel(path):
            if path.endswith(".m4a"):
                raise KeyboardInterrupt
            return 99.0
        with self.assertRaises(KeyboardInterrupt):
            self.scan(bpm_fn=cancel)
        rows = self.rows()
        self.assertEqual(len(rows), 5)                                     # the row being read keeps its old values
        self.assertEqual((rows["Some Artist - Some Song.wav"]["bpm"], rows["sub/e.m4a"]["bpm"]), ("99.0", "123.0"))
        self.assertIn("scan: stopped at 5/5", self.lines[-1])
        self.assertEqual(self.scan(), {"read": 1, "kept": 4, "gone": 0, "failed": 0})       # only the one not finished

    def test_the_studios_own_folders_broken_links_and_relative_folders(self):
        self.music = os.path.realpath(self.music)                          # what a relative folder resolves to
        export, cache = os.path.join(self.music, "Mashup Studio"), os.path.join(self.music, "_cache")
        for d in (export, cache):                                          # inside the music folder, as by default
            os.makedirs(os.path.join(d, "A x B"))
            shutil.copy(os.path.join(self.music, "a.mp3"), os.path.join(d, "A x B", "VOCAL - a.mp3"))
        os.symlink(os.path.join(self.tmp, "unplugged", "x.mp3"), os.path.join(self.music, "link.mp3"))
        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            counts = scan.scan(["music"], self.csv, bpm_fn=self.bpm, key_fn=self.key, log=self.lines.append,
                               skip=[export, cache])
        finally:
            os.chdir(cwd)
        self.assertEqual(counts, {"read": 5, "kept": 0, "gone": 0, "failed": 1})                # the broken link
        self.assertEqual(sorted(self.rows()), ["Some Artist - Some Song.wav", "a.mp3", "sub/b.flac", "sub/c.aiff", "sub/e.m4a"])
        self.assertTrue(all(os.path.isabs(r["path"]) for r in self.rows().values()))

    def test_refuses_an_unreadable_folder_and_the_export_folder_as_music(self):
        self.scan()
        sub = os.path.join(self.music, "sub")
        os.chmod(sub, 0)
        try:
            with self.assertRaises(SystemExit) as e:
                self.scan()
        finally:
            os.chmod(sub, 0o755)
        self.assertIn("cannot be read", str(e.exception))
        self.assertEqual(len(self.rows()), 5)                              # its tracks are not "gone"
        with self.assertRaises(SystemExit) as e:
            self.scan(skip=[self.music + "/"])
        self.assertIn("is the export or cache folder", str(e.exception))
        self.assertEqual(scan.scan(["", self.music], self.csv, bpm_fn=self.bpm, key_fn=self.key, log=self.lines.append)["kept"], 5)

    def test_refuses_a_folder_emptied_like_an_unplugged_drive(self):
        self.scan()
        for f in scan.files([self.music]):
            os.remove(f)                                                   # the mount point stays, empty
        with self.assertRaises(SystemExit) as e:
            self.scan()
        self.assertIn("has no audio files now", str(e.exception))
        self.assertEqual(len(self.rows()), 5)

    def test_refuses_a_missing_folder_and_a_catalog_it_did_not_write(self):
        with self.assertRaises(SystemExit) as e:
            scan.scan([os.path.join(self.tmp, "unplugged")], self.csv)
        self.assertIn("is not there", str(e.exception))
        self.assertFalse(os.path.exists(self.csv))
        os.makedirs(os.path.dirname(self.csv))
        with open(self.csv, "w") as f:
            f.write("someone else's catalog\n")
        with self.assertRaises(SystemExit) as e:
            self.scan()
        self.assertIn("was not written by mf scan", str(e.exception))
        self.assertEqual(text(self.csv), "someone else's catalog\n")
        with self.assertRaises(SystemExit):
            scan.scan([], self.csv)

    def test_mf_scan_on_the_command_line(self):
        for f in ("Some Artist - Some Song.wav", "sub/e.m4a"):             # tagged files only: no analysis
            os.remove(os.path.join(self.music, f))
        r = subprocess.run([sys.executable, "-m", "mashup_finder.cli", "scan", self.music, "--catalog", self.csv],
                           cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines()[0], "scan: 1/3 Artist A - Song A")
        self.assertEqual(len(self.rows()), 3)
        r = subprocess.run([sys.executable, "-m", "mashup_finder.cli", "scan"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stderr.strip()), (1, "no music folders: give one (mf scan FOLDER) or set "
                                                               "music_folders in the settings"))

    def test_the_key_estimate_on_synthetic_chords(self):
        d = fx.tmpdir("mf-scan-key-")
        chord(os.path.join(d, "a-minor.wav"), (220.0, 261.63, 329.63))
        chord(os.path.join(d, "c-major.wav"), (261.63, 329.63, 392.0))
        self.assertEqual(scan.key_of(os.path.join(d, "a-minor.wav")), "8A")
        self.assertEqual(scan.key_of(os.path.join(d, "c-major.wav")), "8B")


if __name__ == "__main__":
    unittest.main()
