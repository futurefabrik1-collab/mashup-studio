"""Render checks on synthetic audio: done-when 2, 3, 4, 6, 8 and 10 of docs/ENGINE-PLAN.md."""
import json, os, unittest

import numpy as np
import soundfile as sf

from mashup_finder import engine, render
from tests import fixtures as fx

SR = fx.SR
CLUB_80 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "club-80.toml")   # the bar positions below are its


def onset(x, at, win=0.02):
    """First sample within +-win s of `at` where |x| passes 30 % of the window's peak."""
    a, b = max(0, int(at - win * SR)), int(at + win * SR)
    seg = np.abs(x[a:b])
    return a + int(np.argmax(seg > 0.3 * seg.max())) if seg.max() > 0 else None


def peak_hz(x, at, dur=0.1):
    seg = x[int(at):int(at + dur * SR)] * np.hanning(int(dur * SR))
    spec = np.abs(np.fft.rfft(seg, 16 * len(seg)))
    return np.argmax(spec) * SR / (16 * len(seg))


def bus(d, lane):
    return sf.read(os.path.join(d, "ableton", f"{lane}.wav"), dtype="float32")[0].T


def make(bpm_a, bpm_b, template, shift, a_kwargs=None, out_bpm=None):
    sa, ba = fx.a_track(bpm_a, **(a_kwargs or {}))
    sb, bb = fx.b_track(bpm_b)
    d = fx.tmpdir("mf-render-")
    done = engine.make(sa, ba, sb, bb, shift, lambda v: d, template, out_bpm=out_bpm, log=lambda *_: None)
    return done[0]["arrangement"], d


class ClubRenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.arr, cls.dir = make(120.0, 126.0, CLUB_80, 2)
        cls.again = fx.tmpdir("mf-rerender-")         # the same arrangement through --render
        cls.r = render.render(os.path.join(cls.dir, "arrangement.json"), cls.again)
        cls.bl = 4 * 60 / 126 * SR

    def test_length_and_bar_clicks_on_the_grid(self):            # done-when 2
        drums = bus(self.again, "drums").mean(0)
        self.assertLess(abs(len(drums) - self.arr["bars"] * self.bl), self.bl / 4)
        checked = 0
        for b in self.arr["lanes"]["drums"]:
            for k in range(b["out_bar"], b["out_bar"] + b["bars"]):
                o = onset(drums, k * self.bl)
                self.assertIsNotNone(o, f"no click near bar {k}")
                self.assertLess(abs(o - k * self.bl) / SR, 0.010, f"bar {k}")
                checked += 1
        self.assertEqual(checked, 64)                                # every bar with drums (the break has none)

    def test_hook_burst_lands_where_the_arrangement_says(self):    # done-when 3
        blk = next(b for b in self.arr["lanes"]["vocal"] if b["src_bar"] == 32)
        want = blk["out_bar"] * self.bl
        voc = bus(self.again, "vocal").mean(0)
        got = onset(voc, want)
        self.assertLess(abs(got - want) / SR, 0.010)
        f = peak_hz(voc, got + 0.02 * SR)
        self.assertLess(abs(f / (440 * 2 ** (2 / 12)) - 1), 0.01, f"{f:.1f} Hz")

    def test_loudness(self):                                        # done-when 6
        lufs, tp = render.measure(sf.read(os.path.join(self.dir, "mashup.wav"), dtype="float32")[0].T)
        self.assertLessEqual(abs(lufs + 10), 1.0, lufs)
        self.assertLessEqual(tp, -1.0)

    def test_ableton_buses_sum_to_the_premaster(self):              # done-when 8
        n = sf.info(os.path.join(self.again, "mashup.wav")).frames
        total = sum(bus(self.again, lane).astype(np.float64) for lane in render.LANES)
        self.assertEqual(total.shape[1], n)
        self.assertLess(np.abs(total - self.r["premaster"]).max(), 1e-5)
        for f in ("ableton/TEMPO.txt", "mashup.mp3", "render.json", "blocks/A-vocals.wav", "blocks/B-drums.wav"):
            self.assertTrue(os.path.getsize(os.path.join(self.dir, f)) > 0, f)

    def test_rerender_is_byte_identical(self):                      # one render path, deterministic
        for lane in render.LANES:
            with open(os.path.join(self.dir, "ableton", f"{lane}.wav"), "rb") as a, \
                 open(os.path.join(self.again, "ableton", f"{lane}.wav"), "rb") as b:
                self.assertEqual(a.read(), b.read(), lane)

    def test_moving_one_block_moves_only_that_block(self):          # done-when 10
        with open(os.path.join(self.dir, "arrangement.json")) as f:
            arr = json.load(f)
        i, blk = next((i, b) for i, b in enumerate(arr["lanes"]["drums"]) if b["out_bar"] == 16)   # the build
        moved = dict(blk, out_bar=40)                                # into the break, where drums are silent
        arr["lanes"]["drums"][i] = moved
        edited = os.path.join(self.dir, "arrangement-edited.json")
        with open(edited, "w") as f:
            json.dump(arr, f, indent=1)
        out = fx.tmpdir("mf-edited-")
        render.render(edited, out)
        for lane in ("vocal", "bass", "other"):
            with open(os.path.join(self.again, "ableton", f"{lane}.wav"), "rb") as a, open(os.path.join(out, "ableton", f"{lane}.wav"), "rb") as b:
                self.assertEqual(a.read(), b.read(), lane)
        before, after = bus(self.again, "drums"), bus(out, "drums")
        src = arr["sources"]["B-drums"]
        audio = sf.read(os.path.join(self.dir, src["file"]), dtype="float32")[0].T
        s_old, seg_old = render.render_block(blk, src, audio, self.bl)
        s_new, seg_new = render.render_block(moved, src, audio, self.bl)
        np.testing.assert_array_equal(seg_old, seg_new)               # the block's own audio is unchanged
        expect = before.astype(np.float64)
        expect[:, s_old:s_old + seg_old.shape[1]] -= seg_old
        expect[:, s_new:s_new + seg_new.shape[1]] += seg_new
        self.assertLess(np.abs(expect - after).max(), 1e-6)
        outside = np.ones(before.shape[1], bool)
        outside[s_old:s_old + seg_old.shape[1]] = outside[s_new:s_new + seg_new.shape[1]] = False
        np.testing.assert_array_equal(before[:, outside], after[:, outside])   # byte-identical everywhere else

    def test_moving_a_vocal_block_keeps_the_duck_local(self):       # done-when 10, the vocal lane
        with open(os.path.join(self.dir, "arrangement.json")) as f:
            arr = json.load(f)
        i, blk = next((i, b) for i, b in enumerate(arr["lanes"]["vocal"]) if b["out_bar"] == 40)   # first verse block
        arr["lanes"]["vocal"][i] = dict(blk, out_bar=72)              # into the outro, where no vocal plays
        edited = os.path.join(self.dir, "arrangement-vocal-edit.json")
        with open(edited, "w") as f:
            json.dump(arr, f, indent=1)
        out = fx.tmpdir("mf-vocal-edit-")
        render.render(edited, out)
        for lane in ("drums", "bass"):
            with open(os.path.join(self.again, "ableton", f"{lane}.wav"), "rb") as a, open(os.path.join(out, "ableton", f"{lane}.wav"), "rb") as b:
                self.assertEqual(a.read(), b.read(), lane)
        reach = np.zeros(int(round(arr["bars"] * self.bl)), bool)
        for ob in (40, 72):                                            # the block, its crossfade and the duck's release
            reach[int((ob * self.bl) - render.XF):int((ob + blk["bars"]) * self.bl) + render.RELEASE] = True
        for lane in ("vocal", "other"):
            a, b = bus(self.again, lane), bus(out, lane)
            np.testing.assert_array_equal(a[:, ~reach], b[:, ~reach], lane)
            self.assertTrue((a[:, reach] != b[:, reach]).any(), lane)

    def test_render_out_makes_a_self_contained_copy(self):
        elsewhere = fx.tmpdir("mf-arr-elsewhere-")                       # an arrangement whose sources live elsewhere
        with open(os.path.join(self.dir, "arrangement.json")) as f:
            arr = json.load(f)
        for s in arr["sources"].values():
            s["file"] = os.path.relpath(os.path.join(self.dir, s["file"]), elsewhere)
        with open(os.path.join(elsewhere, "arrangement.json"), "w") as f:
            json.dump(arr, f)
        out = fx.tmpdir("mf-copy-")
        render.render(os.path.join(elsewhere, "arrangement.json"), out)
        with open(os.path.join(out, "arrangement.json")) as f:
            copied = json.load(f)
        self.assertTrue(all(s["file"].startswith("blocks/") and os.path.exists(os.path.join(out, s["file"])) for s in copied["sources"].values()))
        for lane in render.LANES:
            with open(os.path.join(self.again, "ableton", f"{lane}.wav"), "rb") as a, open(os.path.join(out, "ableton", f"{lane}.wav"), "rb") as b:
                self.assertEqual(a.read(), b.read(), lane)
        arr["sources"]["B-bass"]["file"] = "nowhere/B-bass.wav"
        with open(os.path.join(elsewhere, "arrangement.json"), "w") as f:
            json.dump(arr, f)
        bad_out = fx.tmpdir("mf-bad-")
        with self.assertRaisesRegex(FileNotFoundError, "nowhere/B-bass.wav"):
            render.render(os.path.join(elsewhere, "arrangement.json"), bad_out)
        self.assertEqual(os.listdir(bad_out), [])                       # refused before writing anything

    def test_filter_sweep_enters_without_a_step(self):              # the lead-in is filtered with the block
        other = bus(self.again, "other").mean(0)
        k = int(round(16 * self.bl))                                    # the build's lowpass sweep starts here
        steps = np.abs(np.diff(other[k - 2 * render.XF:k + int(0.2 * SR)]))
        around, after = steps[: 3 * render.XF].max(), steps[3 * render.XF:].max()
        self.assertLess(around, 3 * after, f"step {around:.4f} at the bar line vs {after:.4f} after")


class EdgeTest(unittest.TestCase):
    """No steps where a duck releases, a fade starts on a bar line, or a filter starts inside a block."""

    def test_duck_weight_is_smooth_and_ends_at_zero(self):
        seg = np.zeros((2, 3 * SR), np.float32)
        seg[:, SR:] = np.sin(np.arange(2 * SR) / SR * 2 * np.pi * 440)  # the vocal runs to the block's end
        w = render.duck_weight(seg, render.vocal_reference(seg, 0.0))
        self.assertEqual(len(w), seg.shape[1] + render.RELEASE)
        self.assertEqual(w[-1], 0.0)
        self.assertLess(np.abs(np.diff(w)).max(), 0.01)

    @staticmethod
    def phrases():
        """A sung-like vocal: 440 Hz swelling up and down twice a second, so its level varies like syllables."""
        t = np.arange(4 * SR) / SR
        v = (0.3 * np.sin(2 * np.pi * 440 * t) * np.sin(np.pi * (t % 0.5) / 0.5) ** 2).astype(np.float32)
        return np.stack([v, v])

    @staticmethod
    def loss(w):
        """How many dB the duck takes off a flat bed, on average over the vocal."""
        bed = np.ones((2, len(w)), np.float32)
        return -20 * np.log10(render.duck(bed, w)[:, SR // 4:3 * SR].mean())

    def test_duck_follows_the_block_gain(self):                     # N3
        seg = self.phrases()
        ref = render.vocal_reference(seg, 0.0)
        full, down6, quiet = (self.loss(render.duck_weight(seg * 10 ** (g / 20), ref)) for g in (0.0, -6.0, -60.0))
        self.assertGreater(full, 1.5)                                   # the arranged level ducks the bed
        self.assertLess(down6, full - 0.5)                              # a block at -6 dB ducks it less
        self.assertLess(quiet, 0.05)                                    # a near-silent block barely at all

    def test_an_unedited_block_ducks_as_deep_as_before_n3(self):
        """At the arrangement's vocal gain a block ducks as it did when each block was normalised on its own."""
        seg = self.phrases()
        m = np.concatenate([seg.mean(0), np.zeros(render.RELEASE, np.float32)])     # duck_weight at 139c1f9:
        sm = render._envelope(m)
        old = np.clip(sm / (0.5 * np.percentile(sm[sm > 0], 95)), 0, 1)
        old = np.interp(np.arange(len(m)), np.r_[0, (np.arange(len(sm)) + 0.5) * render.BLOCK], np.r_[0, old]).astype(np.float32)
        old[-5 * render.BLOCK:] *= np.linspace(1, 0, 5 * render.BLOCK)
        before, now = self.loss(old), self.loss(render.duck_weight(seg, render.vocal_reference(seg, 0.0)))
        self.assertLess(abs(now - before), 0.1 * before, f"{now:.2f} dB now, {before:.2f} dB before N3")

    def test_duck_weight_starts_from_zero(self):
        seg = np.ones((2, SR), np.float32) * np.sin(np.arange(SR) / SR * 2 * np.pi * 440)   # loud from sample 0
        w = render.duck_weight(seg, render.vocal_reference(seg, 0.0))
        self.assertEqual(w[0], 0.0)
        self.assertLess(np.abs(np.diff(w)).max(), 0.01)

    def test_fades_inside_a_block_have_no_jump(self):
        bl, n = SR // 2, 4 * (SR // 2)
        seg = np.ones((2, n), np.float32)
        render._apply_fx(seg, {"type": "fade_in"}, 0, bl, 3 * bl, bl, render.XF)       # starts a bar into the block
        self.assertEqual(np.abs(seg[:, :bl]).max(), 0.0)
        self.assertLess(np.abs(np.diff(seg[0])).max(), 1e-3)
        seg = np.ones((2, n), np.float32)
        render._apply_fx(seg, {"type": "fade_out"}, 0, 0, 2 * bl, bl, render.XF)        # ends half-way through
        self.assertEqual(np.abs(seg[:, 2 * bl:]).max(), 0.0)
        self.assertLess(np.abs(np.diff(seg[0])).max(), 1e-3)

    def test_fade_in_on_a_later_bar_silences_the_lead_in(self):
        own, n = render.XF, 2 * SR
        seg = np.ones((2, own + n), np.float32)
        render._apply_fx(seg, {"type": "fade_in"}, 1000, 1000 + own, 1000 + own + n, SR, own)
        self.assertEqual(np.abs(seg[:, :own + 1]).max(), 0.0)
        self.assertLess(np.abs(np.diff(seg[0])).max(), 1e-3)

    def test_filter_starting_inside_a_block_has_no_step(self):
        t = np.arange(2 * SR) / SR
        x = (0.3 * np.sin(2 * np.pi * 2000 * t) + 0.3 * np.sin(2 * np.pi * 110 * t)).astype(np.float32)
        seg = np.stack([x, x])
        a = SR + 123                                                  # the sweep starts inside the block, off a zero crossing
        render._apply_fx(seg, {"type": "lowpass_sweep_up"}, 0, a, 2 * SR, SR, render.XF)
        d = np.abs(np.diff(seg[0]))
        self.assertLess(d[a - 50:a + render.XF + 50].max(), 1.5 * d[: a - 50].max())


class FadeTest(unittest.TestCase):
    """The editor's fade handles: a block's fade_in (a swell before its first beat) and fade_out (over its last bars),
    and the crossfade a split gives its two halves."""
    bl = SR // 2
    audio = np.tile((0.5 * np.sin(np.arange(16 * SR // 2) / SR * 2 * np.pi * 220)).astype(np.float32), (2, 1))
    src = {"bar0": 0, "bar_len": SR // 2, "shift": 0.0}

    def block(self, out_bar=4, bars=4, src_bar=4, **fields):
        return render.render_block({"lane": "drums", "src": "B", "out_bar": out_bar, "bars": bars, "src_bar": src_bar, "fx": [], **fields},
                                   self.src, self.audio, self.bl)

    def test_without_fades_a_block_renders_as_before(self):
        s, a = self.block()
        for zero in ({"fade_in": 0}, {"fade_out": 0}, {"fade_in": 0, "fade_out": 0}):
            t, b = self.block(**zero)
            self.assertEqual(s, t)
            self.assertTrue(np.array_equal(a, b), zero)

    def test_fades_shape_only_the_edges(self):
        s, a = self.block()
        t, b = self.block(fade_in=0.5, fade_out=1)
        self.assertEqual(t, 4 * self.bl - self.bl // 2)                  # starts half a bar early
        self.assertEqual(np.abs(b[:, 0]).max(), 0.0)                    # from silence
        self.assertLess(np.abs(b[:, -1]).max(), 1e-3)                   # to silence
        mid = slice(s - t + render.XF, b.shape[1] - self.bl)            # the block between its fades: as without
        self.assertTrue(np.allclose(b[:, mid], a[:, render.XF:a.shape[1] - self.bl]))
        tail = np.abs(b[0, -self.bl:]).reshape(-1, self.bl // 10).max(1)
        self.assertTrue(np.all(np.diff(tail) < 0))                      # the last bar goes down all the way

    def test_a_split_crossfades_at_the_cut(self):
        whole, w = self.block()
        cut, out = 2, np.zeros((2, 10 * self.bl), np.float32)
        for b0, n, sb, f in ((4, cut, 4, {"fade_out": 0.0625}), (4 + cut, 4 - cut, 4 + cut, {"fade_in": 0.0625})):
            s, seg = self.block(b0, n, sb, **f)
            out[:, s:s + seg.shape[1]] += seg
        c = (4 + cut) * self.bl                                         # around the cut: as one block, no dip or bump
        near = slice(c - self.bl // 4, c + self.bl // 4)
        self.assertLess(np.abs(out[:, near] - w[:, near.start - whole:near.stop - whole]).max(), 1e-4)


class LimiterTest(unittest.TestCase):
    def test_ceiling_holds_on_long_tracks_and_at_both_ends(self):
        c = 10 ** (-1.3 / 20)
        for n, at in ((100_000, 50_000), (9_408_000, 6_237_000), (9_408_000, 0), (9_408_000, 9_407_999)):
            x = np.zeros((2, n), np.float32)
            x[:, at] = 2.0
            self.assertLessEqual(np.abs(render._limit(x, -1.3)).max(), c * 1.001, (n, at))


class DjToolTest(unittest.TestCase):                                # done-when 6 on a template that opens on a drop
    def test_loudness_and_no_clipping(self):
        _, d = make(120.0, 126.0, "dj-tool", 2)
        y = sf.read(os.path.join(d, "mashup.wav"), dtype="float32")[0].T
        lufs, tp = render.measure(y)
        self.assertLessEqual(abs(lufs + 10), 1.0, lufs)
        self.assertLessEqual(tp, -1.0)
        self.assertLess(np.abs(y).max(), 0.99)
        with open(os.path.join(d, "render.json")) as f:
            self.assertTrue(json.load(f)["loudness_ok"])


class HalfTimeTest(unittest.TestCase):                             # done-when 4
    def test_half_time_vocal_aligns(self):
        a_vocal = [(8, 8, 330.0, 0.5), (16, 8, 440.0, 0.6), (32, 8, 440.0, 0.5)]
        arr, d = make(87.0, 174.0, "short", 0, {"bars": 48, "vocal": a_vocal})
        self.assertEqual(arr["tempo_mult"], 2.0)
        bl = 4 * 60 / 174 * SR
        drums, voc = bus(d, "drums").mean(0), bus(d, "vocal").mean(0)
        for b in arr["lanes"]["drums"]:
            for k in range(b["out_bar"], b["out_bar"] + b["bars"]):
                self.assertLess(abs(onset(drums, k * bl) - k * bl) / SR, 0.010, f"bar {k}")
        for b in arr["lanes"]["vocal"]:
            for k in range(b["out_bar"], b["out_bar"] + b["bars"]):  # a vocal burst on every output downbeat
                self.assertLess(abs(onset(voc, k * bl) - k * bl) / SR, 0.010, f"vocal bar {k}")


class DriftTest(unittest.TestCase):
    def test_drifting_instrumental_is_warped_onto_the_grid(self):
        sa, ba = fx.a_track(120.0)
        sb, bb = fx.track(126.0, fx.B_LAYOUT, wobble_ms=30.0)       # a live player, inside deep's 25-40 ms band
        d = fx.tmpdir("mf-drift-")
        arr = engine.make(sa, ba, sb, bb, 0, lambda v: d, "short", log=lambda *_: None)[0]["arrangement"]
        self.assertEqual(arr["sources"]["B-drums"]["warp"], "2-bar blocks")
        self.assertGreater(arr["sources"]["B-drums"]["grid_drift_ms"], render.STEADY_MS)
        bl = 4 * 60 / arr["output_bpm"] * SR
        drums = bus(d, "drums").mean(0)
        errs = [abs(onset(drums, k * bl) - k * bl) / SR for b in arr["lanes"]["drums"] for k in range(b["out_bar"], b["out_bar"] + b["bars"])]
        self.assertLess(max(errs), 0.010, f"worst bar-line click {max(errs) * 1000:.1f} ms off the grid")


class TempoTest(unittest.TestCase):
    def test_tempo_flag_sets_the_output_length(self):
        arr, d = make(120.0, 126.0, "short", 0, out_bpm=128.0)
        frames = sf.info(os.path.join(d, "mashup.wav")).frames
        self.assertLess(abs(frames - arr["bars"] * 4 * 60 / 128 * SR), 60 / 128 * SR)
        self.assertEqual(arr["output_bpm"], 128.0)
        self.assertEqual(arr["sources"]["B-drums"]["warp"], "linear")


if __name__ == "__main__":
    unittest.main()
