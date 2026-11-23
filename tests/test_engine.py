"""Variations end to end (done-when 7) and the CLI surface (done-when 9)."""
import json, os, subprocess, sys, unittest

from unittest import mock

from mashup_finder import arrange, deep, engine
from tests import fixtures as fx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class VariationsTest(unittest.TestCase):
    def run_once(self):
        sa, ba = fx.a_track(120.0)
        sb, bb = fx.b_track(126.0)
        d = fx.tmpdir("mf-var-")
        done = engine.make(sa, ba, sb, bb, 2, lambda v: os.path.join(d, f"v{v}"), "short", n=3, seed=1, log=lambda *_: None)
        return d, done

    def test_three_variations_differ_and_rerun_is_byte_identical(self):
        d1, done = self.run_once()
        self.assertEqual(len({arrange.signature(x["arrangement"]) for x in done}), 3)
        d2, _ = self.run_once()
        for v in (1, 2, 3):
            with open(os.path.join(d1, f"v{v}", "arrangement.json"), "rb") as a, open(os.path.join(d2, f"v{v}", "arrangement.json"), "rb") as b:
                self.assertEqual(a.read(), b.read(), f"v{v}")
        self.assertTrue(all(x["arrangement"]["stems"] == "htdemucs" for x in done))       # the default model, recorded
        self.assertEqual(os.stat(os.path.join(d1, "v1", "blocks", "A-vocals.wav")).st_ino,
                         os.stat(os.path.join(d1, "v3", "blocks", "A-vocals.wav")).st_ino)   # sources shared, not copied
        page = engine.write_page(os.path.join(d1, "variations.html"), "fixture", done)
        with open(page) as f:
            self.assertEqual(f.read().count("<audio"), 3)


class FolderTest(unittest.TestCase):
    """--folder resolution through MASHUP-FINDER.csv, with and without the run (no audio work)."""

    def setUp(self):
        self.root = fx.tmpdir("mf-folder-")
        self.runs = os.path.join(self.root, "runs")
        self.dest = os.path.join(self.root, "Mashups")
        self.folder = os.path.join(self.dest, "180 BPM", "Singer [Remix] x Band (8A-8A)")
        os.makedirs(self.folder)
        for f in ("VOCAL - Singer [Remix].mp3", "INST - Band.mp3", "VOCAL STEM - Singer.wav"):
            open(os.path.join(self.folder, f), "w").close()
        with open(os.path.join(self.dest, "MASHUP-FINDER.csv"), "w", newline="", encoding="utf-8") as fh:
            fh.write("folder,vocal,inst,stretch_pct,shift_st,tempo_mult,run,vocal_src,inst_src\n"
                     f'180 BPM/Singer [Remix] x Band (8A-8A),Singer,Band,1.0,-1,1.0,20261111-1200-x,/lib/v.mp3,/lib/i.mp3\n')
        self.old_runs, engine.RUNS = engine.RUNS, self.runs
        self.old_analyse = engine.deep.analyse_pair

    def tearDown(self):
        engine.RUNS, engine.deep.analyse_pair = self.old_runs, self.old_analyse

    def test_pair_comes_from_the_run_when_it_exists(self):
        run = os.path.join(self.runs, "20261111-1200-x")
        os.makedirs(run)
        with open(os.path.join(run, "results.json"), "w") as f:
            json.dump([{"id": 7, "vocal_path": "/lib/v.mp3", "inst_path": "/lib/i.mp3", "vocal_label": "Singer", "inst_label": "Band",
                        "tuning_fix": 0.25, "deep": {"ok": True, "shift_audio": -1}}], f)
        p = engine._pair_from_folder(self.folder, log=lambda *_: None)
        self.assertEqual((p["vocal"], p["shift"], p["stem"], p["base"]), ("/lib/v.mp3", -0.75, "007-", os.path.join(run, "mashups")))

    def test_run_gone_reanalyses_the_folder_copies(self):
        seen = {}
        engine.deep.analyse_pair = lambda v, i, pair, out, log: seen.update(v=v, i=i, pair=pair) or {"ok": True, "shift_audio": 2}
        p = engine._pair_from_folder(self.folder, log=lambda *_: None)
        self.assertEqual(os.path.basename(seen["v"]), "VOCAL - Singer [Remix].mp3")   # brackets are not a glob pattern
        self.assertEqual(os.path.basename(seen["i"]), "INST - Band.mp3")
        self.assertEqual((seen["pair"]["shift"], p["shift"], p["stem"]), (-1, 2, ""))
        self.assertTrue(p["base"].startswith(os.path.join(self.runs, "_folders")))


class CliTest(unittest.TestCase):
    def mf(self, *args):
        return subprocess.run([sys.executable, "-m", "mashup_finder.cli", *args], cwd=ROOT, capture_output=True, text=True)

    def test_help_still_works(self):
        top = self.mf("--help")
        self.assertEqual(top.returncode, 0)
        for cmd in ("find", "check", "run", "export", "mashup"):
            self.assertIn(cmd, top.stdout)
            self.assertEqual(self.mf(cmd, "--help").returncode, 0, cmd)
        self.assertIn("--render", self.mf("mashup", "--help").stdout)

    def test_zero_variations_is_refused_up_front(self):
        r = self.mf("mashup", "no-such-run", "1", "--variations", "0")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--variations must be at least 1", r.stderr)

    def test_mashup_needs_a_pair(self):
        r = self.mf("mashup")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("give RUN PAIR_ID", r.stderr)


class BetterStemsTest(unittest.TestCase):
    """mf mashup --hq-stems: htdemucs_ft for the final render's stems, only when its weights are on this Mac."""

    def test_the_switch_falls_back_without_the_weights_and_downloads_nothing(self):
        from huggingface_hub import constants
        hub, logs = fx.tmpdir("mf-hf-"), []
        snap = os.path.join(hub, "models--adefossez--HTDemucs-ft", "snapshots", "abc123")   # the Hugging Face cache's layout
        with mock.patch.object(constants, "HF_HUB_CACHE", hub):
            self.assertEqual(deep.hq_missing(), list(deep.HQ_FILES))   # an empty cache
            self.assertEqual(deep.separator(False, logs.append), ("htdemucs", ""))           # off: as always
            self.assertEqual(deep.separator(True, logs.append), ("htdemucs", ""))            # on, missing: htdemucs, said
            self.assertIn("htdemucs_ft weights not on this Mac", logs[0])
            os.makedirs(snap)
            os.makedirs(os.path.join(hub, "models--adefossez--HTDemucs-ft", "refs"))
            with open(os.path.join(hub, "models--adefossez--HTDemucs-ft", "refs", "main"), "w") as f:
                f.write("abc123")
            for f in deep.HQ_FILES[:-1]:
                open(os.path.join(snap, f), "w").close()
            self.assertEqual(deep.hq_missing(), [deep.HQ_FILES[-1]])   # one model short: still off
            open(os.path.join(snap, deep.HQ_FILES[-1]), "w").close()
            self.assertEqual((deep.hq_missing(), deep.separator(True, logs.append)), ([], ("htdemucs_ft", "-ft")))   # cached apart

    def test_mf_mashup_takes_the_flag_off_by_default(self):
        out = subprocess.run([sys.executable, "-m", "mashup_finder.cli", "mashup", "--help"], cwd=ROOT, capture_output=True, text=True).stdout
        self.assertIn("--hq-stems", out)
        from mashup_finder import cli
        seen = {}
        with mock.patch.object(engine, "cli", lambda a: seen.update(hq=a.hq_stems)):
            cli.main(["mashup", "somerun", "0"])
            self.assertFalse(seen["hq"])
            cli.main(["mashup", "somerun", "0", "--hq-stems"])
            self.assertTrue(seen["hq"])


if __name__ == "__main__":
    unittest.main()


class DoubleGridTest(unittest.TestCase):
    def test_half_time_grid_is_doubled(self):
        from mashup_finder.engine import double_grid
        bt = {"beats": [0.0, 0.6857, 1.3714, 2.0571], "downbeats": [0.0], "bpm": 87.5}
        d = double_grid(bt)
        self.assertEqual(d["bpm"], 175.0)
        self.assertEqual(len(d["beats"]), 7)
        self.assertAlmostEqual(d["beats"][1], 0.343, places=3)
        self.assertEqual(d["downbeats"], [0.0])
