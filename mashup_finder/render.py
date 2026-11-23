"""Mashup engine, phase 3: sources and blocks -> audio.

build_sources() puts each source stem once onto a uniform bar grid at the output tempo (the vocal also
pitch-shifted) and writes blocks/<A|B>-<stem>.wav: source bar j starts at sample bar0 + j * bar_len.
render() reads arrangement.json and those files and places every block on its lane. A block's audio depends
only on the block and its source, so moving one block changes nothing else on any lane; the one mix move that
links lanes is B's other stem ducked and dipped under the vocal, and that follows each vocal block locally.
Then two-pass loudness and the writes: mashup.wav / .mp3, ableton/ (one pre-master WAV per lane, from output
bar 1) and render.json."""
import json, os, re, shutil, struct, subprocess, tempfile

import numpy as np
import soundfile as sf
from scipy import signal
from scipy.ndimage import minimum_filter1d

from . import deep
from .arrange import LANE_SOURCE, LANES, load_arrangement, source_name

SR = deep.SR
XF = int(0.010 * SR)                       # equal-power crossfade at every block edge, ending on the bar line
BLOCK = int(0.010 * SR)                    # automation and envelope resolution
STEADY_MS = 10                             # a bar grid this close to a straight line is stretched in one pass
RELEASE = int(1.0 * SR)                    # the duck follows a vocal block this long (its 150 ms release decays fully)
FILTER_FX = ("lowpass_sweep_up", "highpass_sweep_down", "filter_open")
VOCAL_HP = signal.butter(2, 150, "highpass", fs=SR, output="sos")   # vocal blocks, and the duck reference with them
TARGET_LUFS, CEILING_DBTP = -10.0, -1.0


def bar_len(out_bpm):
    return 4 * 60 / out_bpm * SR


def write_json(path, obj):
    """Written to a new file and swapped in, so a reader (the editor, a render) never sees half a file."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(json.dumps(obj, indent=1, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def write_float_wav(path, x):
    """(2, n) as a 32-bit float WAV with a plain header (libsndfile's PEAK chunk holds a timestamp, so equal
    audio would not give equal files). Written to a new file and swapped in, so hard links elsewhere
    (variations sharing blocks/) keep their old content."""
    data = np.ascontiguousarray(x.T, dtype="<f4").tobytes()
    fmt = struct.pack("<HHIIHHH", 3, 2, SR, SR * 8, 8, 32, 0)            # IEEE float, stereo, cbSize 0
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 12 + 8 + len(data)) + b"WAVE")
        f.write(b"fmt " + struct.pack("<I", len(fmt)) + fmt)
        f.write(b"fact" + struct.pack("<II", 4, x.shape[1]))
        f.write(b"data" + struct.pack("<I", len(data)) + data)
    os.replace(tmp, path)


def _fit(y, n):
    """Trim or zero-pad (2, m) to (2, n)."""
    out = np.zeros((2, n), np.float32)
    out[:, : min(n, y.shape[1])] = y[:, :n]
    return out


def _slice(x, a, n):
    """x[:, a:a + n] with zeros outside x."""
    out = np.zeros((2, n), np.float32)
    lo, hi = max(a, 0), min(a + n, x.shape[1])
    if hi > lo:
        out[:, lo - a:hi - a] = x[:, lo:hi]
    return out


def _warp_to_grid(x, edges, bl, semis):
    """x (2, N) on its own bar grid -> (y, bar0, warp, drift_ms) with bar j of x at bar0 + j * bl in y.
    Two bars of lead-in keep pickups and crossfades before bar 0. Same tempo, no shift and a steady grid:
    a plain shift. Steady: one Rubber Band pass. Otherwise 2-bar blocks (deep._warp_blocks), so every
    other bar line lands on the grid."""
    n = len(edges) - 1
    j = np.arange(n + 1)
    slope, icpt = np.polyfit(j, edges, 1)
    drift = float(np.abs(edges - (icpt + slope * j)).max() / SR * 1000)
    lead = int(round(2 * bl))
    L = lead + int(round(n * bl)) + XF
    ratio = (edges[-1] - edges[0]) / (n * bl)              # source samples per output sample
    lead_src = int(round(lead * ratio))
    if drift <= STEADY_MS:
        if semis == 0 and abs(ratio - 1) * n * bl < 0.001 * SR:
            return _slice(x, int(edges[0]) - lead, L), lead, "none", round(drift, 1)
        seg = _slice(x, int(edges[0]) - lead_src, int(round(L * ratio)))
        return _fit(deep._rubberband(seg, L / SR, semis), L), lead, "linear", round(drift, 1)
    t = list(range(0, n, 2)) + [n]
    src = [int(edges[0]) - lead_src] + [int(edges[k]) for k in t]
    tgt = [0] + [lead + k * bl for k in t]
    pad = max(0, -src[0])
    xp = np.concatenate([np.zeros((2, pad), np.float32), x], axis=1)
    y = deep._warp_blocks(xp, [s + pad for s in src], np.asarray(tgt), semis)
    return _fit(y, L), lead, "2-bar blocks", round(drift, 1)


def build_sources(sa, ma, sb, mb, m, shift, out_bpm, out_dir):
    """Write blocks/A-vocals.wav and blocks/B-{drums,bass,other}.wav (32-bit float); return the manifest
    stored in arrangement.json as "sources" (paths relative to the arrangement's folder)."""
    os.makedirs(os.path.join(out_dir, "blocks"), exist_ok=True)
    bl, out = bar_len(out_bpm), {}
    for lane, stem in LANE_SOURCE.items():
        src = "A" if lane == "vocal" else "B"
        stems, mp, k = (sa, ma, m) if src == "A" else (sb, mb, 1.0)
        semis = round(float(shift), 3) if src == "A" else 0.0
        y, bar0, warp, drift = _warp_to_grid(stems[deep.STEMS.index(stem)], mp["edges"], k * bl, semis)
        name = source_name(src, lane)
        write_float_wav(os.path.join(out_dir, "blocks", f"{name}.wav"), y)
        out[name] = {"file": f"blocks/{name}.wav", "bar0": bar0, "bar_len": k * bl, "bars": mp["bars"],
                     "shift": semis, "warp": warp, "grid_drift_ms": drift}
    return out


def _link(a, b):
    """b becomes a hard link to a (a copy across volumes), replacing whatever b was."""
    if os.path.exists(b) and os.path.samefile(a, b):
        return
    tmp = b + ".link"
    if os.path.exists(tmp):
        os.remove(tmp)
    try:
        os.link(a, tmp)
    except OSError:
        shutil.copy2(a, tmp)
    os.replace(tmp, b)


def link_sources(src_dir, dst_dir):
    """Give dst_dir the same blocks/ as src_dir (hard links, no extra disk). Existing files are replaced, so a
    folder never keeps sources from an older render."""
    os.makedirs(os.path.join(dst_dir, "blocks"), exist_ok=True)
    for f in os.listdir(os.path.join(src_dir, "blocks")):
        _link(os.path.join(src_dir, "blocks", f), os.path.join(dst_dir, "blocks", f))


def _sweep(x, a, b, kind, f0, f1, r0, ramp):
    """2nd-order Butterworth on x[:, a:b] in place. The cutoff moves from f0 to f1 (log) over `ramp` samples
    from r0 (both in x's sample index), one coefficient set per 10 ms block, state carried across blocks.
    Starting inside the audio (a > 0), the state is primed on the 50 ms before a, so it does not start cold."""
    zi = np.zeros((2, 1, 2))
    if a > 0:
        sos = signal.butter(2, min(f0, 0.45 * SR), btype=kind, fs=SR, output="sos")
        for c in (0, 1):
            _, zi[c] = signal.sosfilt(sos, x[c, max(0, a - 5 * BLOCK):a], zi=signal.sosfilt_zi(sos) * x[c, max(0, a - 5 * BLOCK)])
    for s in range(a, b, BLOCK):
        fc = min(f0 * (f1 / f0) ** float(np.clip((s - r0) / max(ramp, 1), 0, 1)), 0.45 * SR)
        sos = signal.butter(2, fc, btype=kind, fs=SR, output="sos")
        e = min(s + BLOCK, b)
        for c in (0, 1):
            x[c, s:e], zi[c] = signal.sosfilt(sos, x[c, s:e], zi=zi[c])


def _apply_fx(seg, f, s_abs, a, b, bl, own):
    """One fx entry on seg, whose first sample is output sample s_abs. [a, b): the fx span in output samples.
    own: seg index where the block itself starts (after its pickup or crossfade lead-in). A filter or fade whose
    span starts with the block also covers that lead-in, so nothing jumps at the bar line. A filter starting
    inside the block (an edited `at` > 0) is crossfaded in over 10 ms."""
    lo, hi = max(a - s_abs, 0), min(b - s_abs, seg.shape[1])
    if hi <= lo:
        return
    kind = f["type"]
    if kind in FILTER_FX + ("fade_in", "fade_out") and a - s_abs <= own:
        lo = 0
    if kind in FILTER_FX:
        f0, f1, ramp = {"lowpass_sweep_up": (300, 20000, b - a), "highpass_sweep_down": (1000, 20, b - a),
                        "filter_open": (600, 20000, (b - a) // 2)}[kind]
        dry = seg[:, lo:lo + XF].copy() if lo > 0 else None
        _sweep(seg, lo, hi, "highpass" if kind == "highpass_sweep_down" else "lowpass", f0, f1, a - s_abs, ramp)
        if dry is not None:
            r = np.linspace(0, 1, dry.shape[1])
            seg[:, lo:lo + dry.shape[1]] = dry * (1 - r) + seg[:, lo:lo + dry.shape[1]] * r
        return
    t = np.clip((np.arange(lo, hi) + s_abs - a) / (b - a), 0, 1)
    if kind == "fade_in":                                    # silent before the fade starts
        seg[:, lo:hi] *= t
        seg[:, :lo] = 0
    elif kind == "fade_out":                                 # silent after the fade ends
        seg[:, lo:hi] *= 1 - t
        seg[:, hi:] = 0
    elif kind == "drop_cut":                                 # silence the span's last beat
        c, r = b - int(round(bl / 4)), int(0.005 * SR)
        idx = np.arange(lo, hi) + s_abs
        g = np.ones(hi - lo, np.float32)
        g[idx >= c] = 0
        ramp = (idx >= c - r) & (idx < c)
        g[ramp] = (c - idx[ramp]) / r
        seg[:, lo:hi] *= g


def _edges(blk, bl, span):
    """(lead-in samples, its rise, tail samples, its fall) of a block: a pickup swells in over its bars (squared), the
    editor's fade_in / fade_out (bars) ramp linearly, else the 10 ms equal-power crossfade. span: the block's length."""
    pick = next((f for f in blk.get("fx", []) if f["type"] == "pickup"), None)
    if pick:
        lead, rise = int(round(pick["bars"] * bl)), lambda n: np.linspace(0, 1, n) ** 2
    elif blk.get("fade_in"):
        lead, rise = int(round(blk["fade_in"] * bl)), lambda n: np.linspace(0, 1, n)
    else:
        lead, rise = XF, lambda n: np.sin(np.linspace(0, np.pi / 2, n))
    if blk.get("fade_out"):
        return lead, rise, min(int(round(blk["fade_out"] * bl)), span), lambda n: np.linspace(1, 0, n)
    return lead, rise, XF, lambda n: np.cos(np.linspace(0, np.pi / 2, n))


def render_block(blk, src, audio, bl):
    """(first output sample, (2, n) audio) of one block. It starts before out_bar by its pickup (vocal
    pre-roll, faded in) or by the crossfade, and fades out over its last 10 ms. A block's fade_in and fade_out
    (bars, from the editor) set those two crossfades' lengths: fade_in swells in before the block's first beat,
    fade_out fades over its last bars. Two halves of a split, each with a short one, crossfade at the cut."""
    start = int(round(blk["out_bar"] * bl))
    end = int(round((blk["out_bar"] + blk["bars"]) * bl))
    fx = blk.get("fx", [])
    lead, rise, tail, fall = _edges(blk, bl, end - start)
    pre = min(lead, start)
    seg = _slice(audio, int(round(src["bar0"] + blk["src_bar"] * src["bar_len"])) - pre, end - start + pre)
    d_shift = round(blk.get("shift", src["shift"]) - src["shift"], 3)
    if d_shift:                                              # an edited block: re-shift from the source
        seg = _fit(deep._rubberband(seg, seg.shape[1] / SR, d_shift), seg.shape[1])
    seg *= 10 ** (blk.get("gain_db", 0.0) / 20)
    if pre:                                                  # handle fades are linear: two halves of one audio sum flat
        seg[:, :pre] *= rise(pre)
    seg[:, seg.shape[1] - tail:] *= fall(tail)
    for f in fx:
        if f["type"] != "pickup":
            o = blk["out_bar"] + f["at"]
            _apply_fx(seg, f, start - pre, int(round(o * bl)), int(round((o + f["bars"]) * bl)), bl, pre)
    if blk["lane"] == "vocal":
        seg = signal.sosfiltfilt(VOCAL_HP, seg, axis=1)
    return start - pre, seg.astype(np.float32)


def clip(out_dir, arr, seconds):
    """clip.mp3 next to mashup.wav: `seconds` of the mix from the section start (or the top) where they are loudest
    (RMS), with a 0.5 s fade in and a 2 s fade out, written via a temp name. A mix shorter than that: all of it.
    Returns (start, length) in seconds."""
    wav = os.path.join(out_dir, "mashup.wav")
    y, sr = sf.read(wav, dtype="float32", always_2d=True)
    n, bl = int(seconds * sr), bar_len(arr["output_bpm"]) * sr / SR
    starts = sorted({int(round(s["out_bar"] * bl)) for s in arr.get("sections", [])} | {0})
    a = max([s for s in starts if s + n <= len(y)] or [0], key=lambda s: float(np.mean(y[s:s + n] ** 2)))
    dur = min(seconds, (len(y) - a) / sr)
    out = os.path.join(out_dir, "clip.mp3")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{a / sr:.3f}", "-t", f"{dur:.3f}", "-i", wav, "-af",
                    f"afade=t=in:d=0.5,afade=t=out:st={max(0.0, dur - 2):.3f}:d=2", "-codec:a", "libmp3lame", "-q:a", "2",
                    "-f", "mp3", out + ".tmp"], check=True)
    os.replace(out + ".tmp", out)
    return a / sr, dur


def _envelope(m):
    """RMS per 10 ms of a mono signal, 10 ms attack, 150 ms release."""
    nb = len(m) // BLOCK
    env = np.sqrt((m[: nb * BLOCK].reshape(nb, BLOCK) ** 2).mean(1))
    att, rel, e = np.exp(-1.0), np.exp(-1 / 15), 0.0
    sm = np.empty_like(env)
    for i, x in enumerate(env):
        e = x + (e - x) * (att if x > e else rel)
        sm[i] = e
    return sm


def vocal_reference(audio, gain_db):
    """The envelope level that counts as "vocal fully present": half the 95th percentile of the vocal source's
    envelope (high-passed like the blocks) at the arrangement's vocal gain, the level the duck was normalised to
    per block before. It depends only on the source, so a block's duck never depends on other blocks, and it
    stays put when one block's gain changes, so that block ducks the bed more or less."""
    sm = _envelope(signal.sosfiltfilt(VOCAL_HP, audio.mean(0)))
    return 0.5 * float(np.percentile(sm[sm > 0], 95)) * 10 ** (gain_db / 20) if (sm > 0).any() else 0.0


def duck_weight(seg, ref):
    """0..1 per sample over seg and a release tail: how present this vocal block is, its envelope over `ref`
    (vocal_reference). A block turned down ducks the bed less; a near-silent one barely at all."""
    m = np.concatenate([seg.mean(0), np.zeros(RELEASE, np.float32)])
    sm = _envelope(m)
    nb = len(sm)
    if ref <= 0 or not sm.any():
        return np.zeros(len(m), np.float32)
    w = np.clip(sm / ref, 0, 1)
    w = np.interp(np.arange(len(m)), np.r_[0, (np.arange(nb) + 0.5) * BLOCK], np.r_[0, w]).astype(np.float32)   # from 0, smooth
    w[-5 * BLOCK:] *= np.linspace(1, 0, 5 * BLOCK)          # the tail ends at 0, never with a step
    return w


def _peaking(f, gain_db, q):
    """RBJ peaking EQ as one sos row."""
    A, w = 10 ** (gain_db / 40), 2 * np.pi * f / SR
    al = np.sin(w) / (2 * q)
    b = [1 + al * A, -2 * np.cos(w), 1 - al * A]
    a = [1 + al / A, -2 * np.cos(w), 1 - al / A]
    return np.array([b + a]) / a[0]


def duck(other, w):
    """B's other stem 3.5 dB down and a 3 kHz dip, both following the vocal weight w."""
    if not w.any():
        return other
    dipped = signal.sosfiltfilt(_peaking(3000, -3.0, 0.8), other, axis=1)
    return ((other * (1 - w) + dipped * w) * 10 ** (-3.5 * w / 20)).astype(np.float32)


def measure(x):
    """(integrated LUFS, true peak dBTP) of (2, n) audio, by ffmpeg ebur128."""
    with tempfile.TemporaryDirectory() as td:
        f = os.path.join(td, "m.wav")
        sf.write(f, x.T, SR, subtype="FLOAT")
        err = subprocess.run(["ffmpeg", "-nostats", "-hide_banner", "-i", f, "-filter_complex", "ebur128=peak=true",
                              "-f", "null", "-"], capture_output=True, text=True, check=True).stderr
    num = lambda pat: float(re.findall(pat, err)[-1].replace("inf", "1e9"))
    return num(r"I:\s+(-?[\d.]+|-inf) LUFS"), num(r"Peak:\s+(-?[\d.]+|-inf) dBFS")


def _limit(x, ceiling_db, look_ms=5, release_db_per_ms=0.1):
    """Look-ahead peak limiter on 4x-oversampled peaks (stereo-linked). The gain reaches its floor before
    each peak (forward min, then a backward box average) and recovers at `release_db_per_ms`."""
    c = 10 ** (ceiling_db / 20)
    n = x.shape[1]
    p = np.abs(signal.resample_poly(x, 4, 1, axis=1)).max(0)[: 4 * n].reshape(n, 4).max(1)
    gr = np.minimum(1.0, c / np.maximum(p.astype(np.float64), 1e-12))
    L = int(look_ms / 1000 * SR)
    g1 = minimum_filter1d(gr, size=L + 1, origin=-(L // 2), mode="nearest")      # min over [i, i + L]
    # float64: a float32 running sum over a 3-minute track (9 M samples) cannot resolve the box average
    cs = np.concatenate([[0], np.cumsum(np.concatenate([np.full(L, g1[0]), g1]), dtype=np.float64)])
    g2 = (cs[L + 1:] - cs[:-(L + 1)]) / (L + 1)              # mean over [i - L, i]; never above gr
    blk = int(0.001 * SR)
    nb = -(-n // blk)
    bmin = np.pad(g2, (0, nb * blk - n), constant_values=1).reshape(nb, blk).min(1)
    up, r = 10 ** (release_db_per_ms / 20), np.empty(nb)
    r[0] = bmin[0]
    for i in range(1, nb):
        r[i] = min(bmin[i], r[i - 1] * up, 1.0)
    return (x * np.repeat(r, blk)[:n]).astype(np.float32)


def master(pre):
    """Gain to -10 LUFS integrated and limit to -1 dBTP, re-measuring until both hold (two passes normally).
    Returns (audio, LUFS, dBTP, both targets met)."""
    lufs, _ = measure(pre)
    if lufs < -69:
        raise ValueError("the mix is silent")
    g, ceil = 10 ** ((TARGET_LUFS - lufs) / 20), CEILING_DBTP - 0.3
    for _ in range(5):
        y = _limit(pre * g, ceil)
        lufs, tp = measure(y)
        ok = abs(lufs - TARGET_LUFS) <= 0.3 and tp <= CEILING_DBTP
        if ok:
            break
        g *= 10 ** ((TARGET_LUFS - lufs) / 20)
        if tp > CEILING_DBTP:
            ceil -= tp - CEILING_DBTP + 0.1
    return y, round(lufs, 1), round(tp, 1), ok


def lanes_audio(arr, base):
    """The four lanes, and the duck weight that the vocal blocks put on B's other stem."""
    bl = bar_len(arr["output_bpm"])
    n_bars = max([arr["bars"]] + [b["out_bar"] + b["bars"] for v in arr["lanes"].values() for b in v])
    N = int(round(n_bars * bl))
    cache, buses, w = {}, {}, np.zeros(N, np.float32)

    def audio(name):
        if name not in cache:
            cache[name] = np.ascontiguousarray(sf.read(os.path.join(base, arr["sources"][name]["file"]), dtype="float32")[0].T)
        return cache[name]

    vocals = arr["lanes"].get("vocal", [])
    ref = vocal_reference(audio(source_name(vocals[0]["src"], "vocal")), arr["vocal_gain_db"]) if vocals else 0.0
    for lane in LANES:
        bus = np.zeros((2, N), np.float32)
        for blk in arr["lanes"].get(lane, []):
            name = source_name(blk["src"], lane)
            s, seg = render_block(dict(blk, lane=lane), arr["sources"][name], audio(name), bl)
            lo, hi = max(s, 0), min(s + seg.shape[1], N)
            bus[:, lo:hi] += seg[:, lo - s:hi - s]
            if lane == "vocal":
                wb = duck_weight(seg, ref)
                hi = min(s + len(wb), N)
                w[lo:hi] = np.maximum(w[lo:hi], wb[lo - s:hi - s])
        buses[lane] = bus
    return buses, w


def render(arr_path, out_dir=None, log=print):
    """Render arrangement.json (arranged or hand-edited) into out_dir (default: its own folder)."""
    base = os.path.dirname(os.path.abspath(arr_path))
    arr = load_arrangement(arr_path)
    out_dir = os.path.abspath(out_dir or base)
    missing = [s["file"] for s in arr["sources"].values() if not os.path.exists(os.path.join(base, s["file"]))]
    if missing:
        raise FileNotFoundError(f"{arr_path}: source file(s) not found next to it: {', '.join(missing)}")
    buses, w = lanes_audio(arr, base)
    buses["other"] = duck(buses["other"], w)
    pre = sum(buses.values())
    y, lufs, tp, ok = master(pre)
    if not ok:
        log(f"  warning: loudness ended at {lufs} LUFS / {tp} dBTP, outside -10 +-0.3 LUFS / -1 dBTP")

    abl = os.path.join(out_dir, "ableton")
    os.makedirs(abl, exist_ok=True)
    wav = os.path.join(out_dir, "mashup.wav")
    sf.write(wav, y.T, SR, subtype="PCM_24")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", wav, "-codec:a", "libmp3lame", "-q:a", "2",
                    os.path.join(out_dir, "mashup.mp3")], check=True)
    for k, v in buses.items():
        write_float_wav(os.path.join(abl, f"{k}.wav"), v)
    bpm = arr["output_bpm"]
    with open(os.path.join(abl, "TEMPO.txt"), "w") as f:
        f.write(f"{bpm:.2f} BPM\nEach WAV starts at output bar 1 and is already at this tempo: "
                f"set Live to {bpm:.2f} BPM and drop them in with warp off.\n")
    write_json(os.path.join(out_dir, "render.json"), {"output_bpm": bpm, "length_s": round(y.shape[1] / SR, 3),
               "integrated_lufs": lufs, "true_peak_dbtp": tp, "loudness_ok": ok})
    if out_dir != base:                                      # a self-contained copy: arrangement + its sources
        os.makedirs(os.path.join(out_dir, "blocks"), exist_ok=True)
        for s in arr["sources"].values():
            _link(os.path.join(base, s["file"]), os.path.join(out_dir, "blocks", os.path.basename(s["file"])))
            s["file"] = "blocks/" + os.path.basename(s["file"])
        write_json(os.path.join(out_dir, "arrangement.json"), arr)
    return {"buses": buses, "premaster": pre, "master": y, "lufs": lufs, "true_peak": tp, "loudness_ok": ok,
            "bar_len": bar_len(bpm)}
