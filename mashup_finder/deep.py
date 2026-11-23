"""Stage 2: audio check of shortlisted pairs, and a 30-50 s preview of each.

Per track (cached by path): Demucs htdemucs stems (drums, bass, other, vocals) and Beat This!
beats/downbeats. Per pair: pick the vocal phrase of A and the instrumental phrase of B on the
bar grid, measure how well the vocal's pitch classes sit on B's harmony at every transposition,
then render the vocal (time-stretched + pitch-shifted with Rubber Band R3) over B's stems.

Licensing: htdemucs weights are not cleared for commercial use. Fine for a personal tool; a commercial
product would need another separator.
"""
import hashlib, json, os, subprocess, tempfile
import numpy as np

from . import config

SR = 44100
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = config.load()["cache_folder"]
STEMS = ("drums", "bass", "other", "vocals")
SEPARATOR = "htdemucs"
HQ_SEPARATOR = "htdemucs_ft"            # four fine-tuned models, one per stem: better, about 4x slower (mf mashup --hq-stems)
HQ_REPO = "adefossez/HTDemucs-ft"         # Demucs 4.1 loads named models from Hugging Face first (demucs.hf), else the old .th files
HQ_FILES = ("htdemucs_ft.yaml", "f7e0c4bc.safetensors", "d12395a8.safetensors", "92cfc3b6.safetensors", "04573f0d.safetensors")

_models = {}


def _key(path):
    return hashlib.sha1(path.encode()).hexdigest()[:16]


def decode(path):
    """Any audio file -> float32 (2, N) at 44.1 kHz via ffmpeg."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "f32le", "-ac", "2", "-ar", str(SR), "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).reshape(-1, 2).T.copy()


def _device():
    import torch
    return "mps" if torch.backends.mps.is_available() else "cpu"


def hq_missing():
    """The htdemucs_ft files not yet in the Hugging Face cache, where Demucs loads them from: the bag's yaml and one
    safetensors per model. All are needed; with any missing, Demucs would download them. Nothing is downloaded here."""
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:                                   # a Demucs without the Hugging Face loader: not offered
        return list(HQ_FILES)
    return [f for f in HQ_FILES if not isinstance(try_to_load_from_cache(HQ_REPO, f), str)]


def separator(hq, log=print):
    """(model name, cache suffix) for separate(): htdemucs_ft when asked and its weights are on this Mac, else htdemucs."""
    missing = hq_missing() if hq else []
    if missing:
        log(f"    {HQ_SEPARATOR} weights not on this Mac ({', '.join(missing)}): using {SEPARATOR}")
    return (HQ_SEPARATOR, "-ft") if hq and not missing else (SEPARATOR, "")


def separate(path, log=print, hq=False):
    """(4, 2, N) float32 stems in STEMS order, cached as one 8-channel 16-bit FLAC (~1/4 of raw). hq: htdemucs_ft
    (the final render's stems, cached apart) if its weights are here."""
    import soundfile as sf
    name, suffix = separator(hq, log)
    f = os.path.join(CACHE, "stems", _key(path) + suffix + ".flac")
    if os.path.exists(f):
        y, _ = sf.read(f, dtype="float32")
        return y.T.reshape(4, 2, -1)
    import torch
    from demucs.pretrained import get_model
    from demucs.apply import apply_model
    if name not in _models:
        m = get_model(name); m.eval(); _models[name] = m
    m = _models[name]
    y = decode(path)
    log(f"    separating {os.path.basename(path)} ({y.shape[1] / SR:.0f} s)")
    with torch.no_grad():
        st = apply_model(m, torch.from_numpy(y)[None], device=_device(), progress=False)[0].cpu().numpy()
    st = st[[m.sources.index(s) for s in STEMS]]
    os.makedirs(os.path.dirname(f), exist_ok=True)
    sf.write(f + ".tmp", np.clip(st.reshape(8, -1).T, -1, 1), SR, subtype="PCM_16", format="FLAC")
    os.replace(f + ".tmp", f)                                # a cut-off write never poisons the cache
    return st


def beats(path, y=None):
    """{'beats': [...], 'downbeats': [...], 'bpm': line-fit tempo, 'beats_per_bar': median}."""
    f = os.path.join(CACHE, "beats", _key(path) + ".json")
    if os.path.exists(f):
        return _beat_grid(*(np.asarray(json.load(open(f))[k], float) for k in ("beats", "downbeats")))
    from beat_this.inference import Audio2Beats
    if "bt" not in _models:
        _models["bt"] = Audio2Beats(checkpoint_path="final0", device=_device(), dbn=False)
    if y is None:
        y = decode(path)
    b, db = _models["bt"](y.mean(0), SR)
    b, db = np.asarray(b, float), np.asarray(db, float)
    raw = {"beats": b.round(3).tolist(), "downbeats": db.round(3).tolist()}   # the cache keeps what Beat This! said
    os.makedirs(os.path.dirname(f), exist_ok=True)
    with open(f + ".tmp", "w") as fh:                       # whole or not at all: a Cancel can come any time
        json.dump(raw, fh)
    os.replace(f + ".tmp", f)
    return _beat_grid(*(np.asarray(raw[k], float) for k in ("beats", "downbeats")))


def _dedupe(t):
    """Drop detections closer than half the median interval to the one before. Beat This! sometimes marks one beat
    twice ~80 ms apart; 121 of 222 cached tracks had some, and on Sweet Dreams they made 126 BPM read as 170.8."""
    if t.size < 3:
        return t
    gap, keep = 0.5 * np.median(np.diff(t)), [t[0]]
    for x in t[1:]:
        if x - keep[-1] >= gap:
            keep.append(x)
    return np.asarray(keep)


def _beat_grid(b, db):
    """{'beats', 'downbeats', 'bpm', 'beats_per_bar'} from raw detections, doubled ones dropped."""
    b, db = _dedupe(b), _dedupe(db)
    # Beat This! times are 20 ms-quantised: tempo from a line fit, not the median interval.
    bpm = 60.0 / np.polyfit(np.arange(b.size), b, 1)[0] if b.size >= 8 else 0.0
    bpb = 0
    if db.size >= 3:
        idx = np.searchsorted(b, db - 0.03)
        bpb = int(np.median(np.diff(idx)))
    return {"beats": b.round(3).tolist(), "downbeats": db.round(3).tolist(), "bpm": round(float(bpm), 2),
            "beats_per_bar": bpb}


def _grid(bt, steady_ms=25):
    """Beat times with tracker dropouts and doubles repaired.
    Each detected beat gets an integer beat index from the running period, so a missed beat
    leaves a gap in the index instead of shifting every later bar. A line fit over the indices
    gives the tempo; if every beat sits within `steady_ms` of it (programmed music) the exact
    line is returned, otherwise the detected beats with gaps filled by interpolation."""
    b = np.asarray(bt["beats"], float)
    if b.size < 8:
        return b
    period = 60.0 / bt["bpm"] if bt.get("bpm") else np.median(np.diff(b))
    idx = np.concatenate([[0], np.cumsum(np.maximum(np.round(np.diff(b) / period), 0))]).astype(int)
    keep = np.concatenate([[True], np.diff(idx) > 0])          # drop doubled beats (same index)
    b, idx = b[keep], idx[keep]
    slope, t0 = np.polyfit(idx, b, 1)
    full = np.arange(idx[-1] + 1)
    if np.percentile(np.abs(b - (t0 + slope * idx)), 95) < steady_ms / 1000:
        return t0 + slope * full
    return np.interp(full, idx, b)


def _bars(bt, n_samples, bpb=4):
    """Bar boundaries in samples: every `bpb` beats, phased to agree with most detected downbeats.
    Downbeats alone are unreliable on acapellas and sparse intros (missing or doubled bars);
    beats are not, so bars are built from beats and downbeats only choose the phase."""
    b, db = _grid(bt), np.asarray(bt["downbeats"])
    if b.size < 2 * bpb:
        return np.array([], int)
    near = np.abs(b[:, None] - db[None, :]).min(1) < 0.07 if db.size else np.zeros(b.size, bool)
    phase = max(range(bpb), key=lambda p: near[p::bpb].sum())
    return np.clip((b[phase::bpb] * SR).astype(int), 0, n_samples)


def _bar_rms(x, edges):
    """RMS per bar of a (2, N) or (N,) signal."""
    m = x.mean(0) if x.ndim == 2 else x
    return np.array([np.sqrt(np.mean(m[a:b] ** 2)) if b > a else 0.0 for a, b in zip(edges[:-1], edges[1:])])


def _bar_chroma(x, edges):
    """12-bin chroma per bar (L1-normalised) of a mono signal at SR."""
    import librosa
    y = librosa.resample(x.mean(0) if x.ndim == 2 else x, orig_sr=SR, target_sr=22050)
    hop = 512
    C = librosa.feature.chroma_cqt(y=y, sr=22050, hop_length=hop)
    fr = (edges * 22050 / SR / hop).astype(int)
    out = []
    for a, b in zip(fr[:-1], fr[1:]):
        v = C[:, a:max(b, a + 1)].mean(1)
        out.append(v / (v.sum() + 1e-9))
    return np.array(out)


def _windows(n_bars, L, step=4):
    return list(range(2, max(3, n_bars - L), step))


MAX_STRETCH = 0.12                                    # the audio check's limit, unless the brief allows more


def analyse_pair(a_path, b_path, pair, outdir, log=print, max_stretch=None):
    """Pick phrases, score harmonic fit, render preview. Returns a metrics dict. max_stretch: the brief's
    Max stretch (a fraction); the audio may stretch up to it when it is above MAX_STRETCH."""
    sa, sb = separate(a_path, log), separate(b_path, log)
    ba = beats(a_path, sa.sum(0)); bb = beats(b_path, sb.sum(0))
    res = {"bpm_a": ba["bpm"], "bpm_b": bb["bpm"], "meter_a": ba["beats_per_bar"], "meter_b": bb["beats_per_bar"]}
    if ba["bpm"] <= 0 or bb["bpm"] <= 0:
        return {**res, "ok": False, "why": "no beat grid"}
    # Both are treated as 4/4 (the library is overwhelmingly so). A detected triple meter on both
    # sides is only flagged: Beat This! downbeats on acapellas often read 2 or 3 beats per bar.
    res["meter_flag"] = 3 in (ba["beats_per_bar"], bb["beats_per_bar"])

    # Tempo from the audio, not the catalog: recompute stretch with the half/double choice.
    mults = [1.0, 2.0, 0.5]
    m = min(mults, key=lambda k: abs(bb["bpm"] / (ba["bpm"] * k) - 1))
    stretch = bb["bpm"] / (ba["bpm"] * m) - 1
    res.update(tempo_mult=m, stretch_audio=round(stretch, 4))
    if abs(stretch) > max(MAX_STRETCH, max_stretch or 0):
        return {**res, "ok": False, "why": f"audio tempo stretch {stretch * 100:+.1f}%"}

    ea, eb = _bars(ba, sa.shape[-1]), _bars(bb, sb.shape[-1])
    bar_b = 4 * 60.0 / bb["bpm"]
    L = 16 if bar_b < 2.2 else 8                      # instrumental bars carrying the vocal
    La = max(2, int(round(L / m)))                      # matching vocal bars in A
    if len(ea) < La + 3 or len(eb) < L + 6:
        return {**res, "ok": False, "why": "track too short for a phrase"}

    # A: vocal phrase = window with the most vocal bars, weighted by how exposed the vocal is.
    v_rms = _bar_rms(sa[3], ea)
    mix_rms = _bar_rms(sa.sum(0), ea) + 1e-9
    active = v_rms > 0.1 * v_rms.max()
    share = np.clip(v_rms / mix_rms, 0, 1)
    wa = max(_windows(len(ea) - 1, La), key=lambda i: (active[i:i + La].mean(), share[i:i + La].mean()))
    vocal_active = float(active[wa:wa + La].mean())
    vocal_share = float(share[wa:wa + La][active[wa:wa + La]].mean()) if active[wa:wa + La].any() else 0.0

    # B: candidate instrumental phrases = strong groove (drums + bass), little of B's own vocal (less bleed).
    groove = _bar_rms(sb[0] + sb[1], eb)
    bv = _bar_rms(sb[3], eb) / (_bar_rms(sb.sum(0), eb) + 1e-9)
    starts = [j for j in _windows(len(eb) - 1, L) if j >= 4]
    gs = {j: groove[j:j + L].mean() / (groove.max() + 1e-9) - 0.5 * bv[j:j + L].mean() for j in starts}
    cands = sorted(starts, key=lambda j: -gs[j])[:6]

    # Harmonic fit: vocal chroma (A, its phrase) against B's harmony (bass + other), bar against bar.
    ca = _bar_chroma(sa[3], ea[wa:wa + La + 1])
    # Put A's bars on B's bar grid: half-time vocal bars cover two B bars, double-time pairs merge.
    ca = np.repeat(ca, int(m), axis=0) if m >= 1 else ca[: len(ca) // 2 * 2].reshape(-1, 2, 12).mean(1)
    best = None
    for j in cands:
        cb = _bar_chroma(sb[1] + sb[2], eb[j:j + L + 1])
        n = min(len(ca), len(cb))
        act = np.repeat(active[wa:wa + La], int(m))[:n] if m >= 1 else np.ones(n, bool)
        cbm = cb[:n] / (cb[:n].max(1, keepdims=True) + 1e-9)          # B pitch-class strength 0..1
        fits = []
        for s in range(12):
            f = (np.roll(ca[:n], s, axis=1) * cbm).sum(1)
            fits.append(float(f[act].mean()) if act.any() else 0.0)
        fits = np.array(fits)
        allowed = {s: fits[s % 12] - 0.01 * abs(s) for s in range(-3, 4)}
        s_best = max(allowed, key=allowed.get)
        cand = {"j": j, "fits": fits, "shift": s_best, "fit": fits[s_best % 12], "groove": gs[j]}
        if best is None or cand["fit"] + 0.2 * cand["groove"] > best["fit"] + 0.2 * best["groove"]:
            best = cand
    fits = best["fits"]
    rank = int((fits > best["fit"] + 1e-9).sum()) + 1                  # 1 = best of all 12 transpositions
    z = float((best["fit"] - fits.mean()) / (fits.std() + 1e-9))
    res.update(ok=True, vocal_bar=int(wa), inst_bar=int(best["j"]), bars=L, vocal_active=round(vocal_active, 2),
               vocal_share=round(vocal_share, 3), harm_fit=round(float(best["fit"]), 3), harm_z=round(z, 2),
               harm_rank=rank, shift_audio=int(best["shift"]), shift_catalog=pair["shift"], groove=round(float(best["groove"]), 2))

    # Anchors (samples) mapping A's vocal onto B's bars. One A bar covers r = L / La B bars
    # (2 when the vocal runs half-time, 0.5 double-time); the pre-roll keeps pickups before the downbeat.
    j, r = best["j"], L / La
    pre_b = max(1, int(round(r)))
    pre_a = int(round(pre_b / r))
    src, tgt = [ea[wa - pre_a], ea[wa + La]], [eb[j - pre_b], eb[j + L]]
    res["drift_linear_ms"] = _drift_ms(ba, bb, src, tgt)
    res["warp"] = "linear"
    if res["drift_linear_ms"] is None or res["drift_linear_ms"] > 40:
        # Tempo drifts on one side: warp in blocks of 2 B bars so every block starts on its bar.
        t_idx = list(range(0, L + pre_b, 2)) + [L + pre_b]
        tgt = [eb[j - pre_b + t] for t in t_idx]
        src = list(np.interp([wa - pre_a + t / r for t in t_idx], np.arange(len(ea)), ea))
        res["warp"] = "2-bar blocks"
    res["drift_ms"] = _drift_ms(ba, bb, src, tgt)
    res["preview"] = render(sa, sb, eb, j, L, src, tgt, best["shift"] + pair.get("tuning_fix", 0.0), outdir, pair["id"])
    return res


def _drift_ms(ba, bb, src, tgt):
    """95th-percentile distance (ms) between vocal beats, mapped piecewise-linearly through the
    sample anchors src -> tgt, and the nearest instrumental beat. Large = the sides drift apart."""
    A = _grid(ba); B = _grid(bb)
    src, tgt = np.asarray(src) / SR, np.asarray(tgt) / SR
    A = A[(A >= src[0]) & (A <= src[-1])]
    B = B[(B >= tgt[0] - 0.5) & (B <= tgt[-1] + 0.5)]
    if A.size < 4 or B.size < 4:
        return None
    mapped = np.interp(A, src, tgt)
    d = np.abs(mapped[:, None] - B[None, :]).min(1)
    return int(round(1000 * float(np.percentile(d, 95))))


def _rubberband(x, duration, semitones):
    """Offline Rubber Band R3: stretch (2, N) to `duration` seconds, shift pitch, keep formants."""
    import soundfile as sf
    with tempfile.TemporaryDirectory() as td:
        i, o = os.path.join(td, "i.wav"), os.path.join(td, "o.wav")
        sf.write(i, x.T, SR, subtype="FLOAT")
        subprocess.run(["rubberband", "-q", "-3", "-F", "--centre-focus", "-D", f"{duration:.6f}",
                        "-p", f"{semitones:.4f}", i, o], check=True, capture_output=True)
        y, _ = sf.read(o, dtype="float32")
    return y.T


def _warp_blocks(x, src, tgt, semitones, xf=0.015):
    """Stretch each source block [src[k], src[k+1]] to target length tgt[k+1]-tgt[k] and butt them
    together with short equal-power crossfades. `tgt` is relative to the output start."""
    n_x = int(xf * SR)
    out = np.zeros((2, int(tgt[-1]) + n_x), np.float32)
    for k in range(len(src) - 1):
        a, b = int(src[k]), int(src[k + 1]) + n_x
        y = _rubberband(x[:, a:b], (tgt[k + 1] - tgt[k] + n_x) / SR, semitones)
        p = int(tgt[k])
        y = y[:, : out.shape[1] - p]
        if k > 0:
            y[:, :n_x] *= np.sin(np.linspace(0, np.pi / 2, n_x)) ** 2   # fade in under previous tail
        if k < len(src) - 2 and y.shape[1] >= n_x:
            y[:, -n_x:] *= np.cos(np.linspace(0, np.pi / 2, n_x)) ** 2
        out[:, p:p + y.shape[1]] += y
    return out[:, : int(tgt[-1])]


def render(sa, sb, eb, jb, L, src, tgt, semitones, outdir, pid, lead_bars=4):
    """Preview: `lead_bars` of B's instrumental, then A's vocal (warped through the src -> tgt sample
    anchors) over L bars of it, then a tail bar."""
    s0 = eb[jb - lead_bars]
    s1 = eb[min(jb + L + 1, len(eb) - 1)]
    inst = sb[0] + sb[1] + sb[2]
    inst = inst[:, s0:s1].copy()
    vb0 = tgt[0]
    if len(src) == 2:
        voc = _rubberband(sa[3][:, src[0]:src[1]], (tgt[1] - tgt[0]) / SR, semitones)
    else:
        voc = _warp_blocks(sa[3], src, np.asarray(tgt) - vb0, semitones)
    # Fade the pre-roll bar in so the previous line's tail doesn't land on the lead-in.
    ramp = np.ones(voc.shape[1], np.float32)
    k = min(eb[jb] - vb0, ramp.size)
    ramp[:k] = np.linspace(0, 1, k) ** 2
    ramp[-int(0.05 * SR):] = np.linspace(1, 0, int(0.05 * SR))
    voc = voc * ramp
    # Levels: vocal sits ~2 dB under the instrumental's RMS during the vocal section.
    off = vb0 - s0
    seg = inst[:, off:off + voc.shape[1]]
    n = min(seg.shape[1], voc.shape[1])
    vr = np.sqrt(np.mean(voc[:, :n] ** 2)) + 1e-9
    ir = np.sqrt(np.mean(seg[:, :n] ** 2)) + 1e-9
    voc = voc * (ir / vr) * 10 ** (-2 / 20)
    out = inst.copy()
    out[:, off:off + n] += voc[:, :n]
    fo = int(1.5 * SR)
    out[:, -fo:] *= np.linspace(1, 0, fo)
    out[:, : int(0.02 * SR)] *= np.linspace(0, 1, int(0.02 * SR))
    out *= 0.89 / (np.abs(out).max() + 1e-9)
    os.makedirs(outdir, exist_ok=True)
    mp3 = os.path.join(outdir, f"{pid:03d}.mp3")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "2", "-i", "-",
                    "-codec:a", "libmp3lame", "-q:a", "2", mp3], input=out.T.astype(np.float32).tobytes(), check=True)
    return os.path.basename(mp3)
