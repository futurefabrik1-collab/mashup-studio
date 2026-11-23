"""Mashup engine, phase 1: what is where in one track.

track_map() works on arrays only (stems + a beat dict), so tests can feed synthetic audio. Bars come from
deep._bars. Sections are 8-bar phrases (shorter at the edges) labelled from per-stem energy and merged into
runs. Vocal phrases are runs of active vocal bars, cut into 4- or 8-bar phrases, each with a role:
hook (the most repeated, by per-bar vocal chroma), verse, or adlib (short)."""
import numpy as np

from . import deep

HI, LO = 0.6, 0.35            # stem level (1 = this stem's loud bars) for "high" and "low"
SAME = 0.85                   # chroma cosine above which two vocal phrases count as the same line


def track_map(stems, bt, phrases=True):
    """stems: (4, 2, N) in deep.STEMS order. bt: {'beats', 'downbeats', 'bpm', ...}.
    Bars are 0-based; edges[k]:edges[k + 1] is bar k in samples."""
    edges = deep._bars(bt, stems.shape[-1])
    n = len(edges) - 1
    if n < 4:
        raise ValueError("no usable bar grid (fewer than 4 bars)")
    rms = {s: deep._bar_rms(stems[i], edges) for i, s in enumerate(deep.STEMS)}
    level = {s: r / (np.percentile(r, 95) + 1e-9) for s, r in rms.items()}
    v = rms["vocals"]                                   # activity and exposure: same rules as deep.analyse_pair
    active = v > 0.1 * v.max() if v.max() > 0 else np.zeros(n, bool)
    share = np.clip(v / (deep._bar_rms(stems.sum(0), edges) + 1e-9), 0, 1)
    return {"edges": edges, "bpm": float(bt["bpm"]), "bars": n, "rms": rms, "level": level, "vocal_active": active,
            "vocal_share": share, "sections": sections(level, n),
            "phrases": vocal_phrases(stems[3], edges, active, share) if phrases else []}


def _phrase_bounds(e, n, size=8):
    """Phrase boundaries every `size` bars, phased where the energy changes most (ties: earliest phase)."""
    d = np.abs(np.diff(e, prepend=e[0]))
    p = max(range(size), key=lambda k: (round(float(d[k::size].sum()), 6), -k))
    return sorted({0, n, *range(p, n, size)})


def sections(level, n):
    """Runs of same-labelled phrases: [{'label', 'start', 'bars'}]. Labels, per phrase:
    drop = drums and bass both high; intro = the low-drum phrases at the start; outro = after the last drop;
    between drops: break if drums are low, else build; before the first drop (after the intro): build."""
    e = level["drums"] + level["bass"] + level["other"]
    b = _phrase_bounds(e, n)
    seg = list(zip(b[:-1], b[1:]))
    mean = lambda s, a, z: float(level[s][a:z].mean())
    drop = [mean("drums", a, z) >= HI and mean("bass", a, z) >= HI for a, z in seg]
    low = [mean("drums", a, z) < LO for a, z in seg]
    drops = [i for i, x in enumerate(drop) if x]
    labels = []
    for i in range(len(seg)):
        if drop[i]:
            lab = "drop"
        elif not drops or i < drops[0]:
            lab = "intro" if all(low[:i + 1]) else "build"
        elif i > drops[-1]:
            lab = "outro"
        else:
            lab = "break" if low[i] else "build"
        labels.append(lab)
    out = []
    for (a, z), lab in zip(seg, labels):
        if out and out[-1]["label"] == lab:
            out[-1]["bars"] += z - a
        else:
            out.append({"label": lab, "start": a, "bars": z - a})
    return out


def _active_runs(active):
    """[start, end) runs of active bars; a single silent bar inside a run (a breath) is bridged."""
    runs, k, n = [], 0, len(active)
    while k < n:
        if not active[k]:
            k += 1
            continue
        s = k
        while k < n and (active[k] or (k + 1 < n and active[k + 1])):
            k += 1
        runs.append((s, k))
    return runs


def vocal_phrases(voc, edges, active, share):
    """[{'start', 'bars', 'role', 'rep', 'score', 'rank'}], rank 1 = best (activity + exposure + repetition)."""
    n = len(active)
    cut = []
    for s, e in _active_runs(active):
        if e - s <= 2:
            cut.append((s, 4, "adlib"))
            continue
        for a in range(s, e, 8):
            r = min(8, e - a)
            if r >= 3:
                cut.append((a, 4 if r <= 5 else 8, None))
    cut = [(a, min(L, (n - a) // 4 * 4), role) for a, L, role in cut]
    cut = [c for c in cut if c[1] >= 4]
    if not cut:
        return []
    C = deep._bar_chroma(voc, edges)
    vec = np.array([C[a:a + L][active[a:a + L]].mean(0) if active[a:a + L].any() else C[a:a + L].mean(0) for a, L, _ in cut])
    vec -= vec.mean(1, keepdims=True)              # drop the flat part (quiet frames read as flat chroma)
    vec /= np.linalg.norm(vec, axis=1, keepdims=True) + 1e-9
    sim = vec @ vec.T
    long_ = np.array([role is None for _, _, role in cut])
    rep = ((sim > SAME) & long_[None, :]).sum(1) - 1          # same-sounding long phrases, not counting itself
    rep = np.where(long_, rep, 0)
    out = []
    for i, (a, L, role) in enumerate(cut):
        out.append({"start": int(a), "bars": int(L), "role": role, "rep": int(rep[i]),
                    "score": round(float(active[a:a + L].mean() + share[a:a + L].mean() + 0.25 * rep[i]), 4)})
    longs = [p for p in out if p["role"] is None]
    if longs:
        top = max(p["rep"] for p in longs)
        if top == 0:                                           # nothing repeats: the strongest phrase is the hook
            top_p = max(longs, key=lambda p: (p["score"], -p["start"]))
            for p in longs:
                p["role"] = "hook" if p is top_p else "verse"
        else:
            for p in longs:
                p["role"] = "hook" if p["rep"] == top else "verse"
    for r, p in enumerate(sorted(out, key=lambda p: (-p["score"], p["start"])), 1):
        p["rank"] = r
    return out
