"""Stage 1: score every (vocal A, instrumental B) pair from catalog metadata alone.

Fast enough for the whole library (~20k tracks, ~400M ordered pairs) because tempo is the
first cut: for each vocal track only instrumentals inside the stretch window are scored.
Scores are 0-100; they rank candidates for the audio check, they do not promise a good mashup.
"""
import numpy as np
from .compat import KEY_COST, KEY_SHIFT, KEY_REL, TEMPO_MULTS

W = {
    "stretch": 300.0,      # points lost per 1.0 (=100 %) time-stretch -> 8 % costs 24
    "key": 40.0,           # points lost per unit of KEY_COST (relative 6, fifth 14, each semitone 12)
    "low_key_conf": 6.0,   # either key estimate is weak
    "tuning": 0.1,         # per cent of tuning mismatch beyond 20 cents
    "acapella": 10.0, "instrumental": 6.0, "familiar": 4.0, "genre_contrast": 4.0,
    "groove": 30.0, "club_vocal": 15.0, "same_artist": 8.0, "unknown_artist": 10.0,
    "taste": 6.0,          # most points the rated mashups can add or take (taste())
}


def candidates(cat, vocal_idx=None, inst_idx=None):
    """(vocals, instrumentals) score_pairs pairs up: vocal sources, and tracks that are not acapellas."""
    V = np.arange(cat.n) if vocal_idx is None else np.asarray(vocal_idx)
    I = np.arange(cat.n) if inst_idx is None else np.asarray(inst_idx)
    return V[cat.vocal_ok[V]], I[~cat.acapella[I]]


def score_pairs(cat, max_stretch=0.08, vocal_idx=None, inst_idx=None, per_vocal=5, min_score=40.0):
    """Return list of dicts sorted by score. vocal_idx / inst_idx restrict either side (seed mode)."""
    V, I_all = candidates(cat, vocal_idx, inst_idx)
    # A wide Max stretch must be able to find something: the stretch cost alone would push a 20 % pair under
    # min_score, so the floor drops to leave a pair at the limit 20 points (no change up to about 13 %).
    floor = min(min_score, 100 - W["stretch"] * max_stretch - 20)
    order = np.argsort(cat.bpm[I_all])
    I_sorted, bpm_sorted = I_all[order], cat.bpm[I_all][order]
    out = []
    for a in V:
        cand = []
        for m in TEMPO_MULTS:
            lo, hi = np.searchsorted(bpm_sorted, [cat.bpm[a] * m * (1 - max_stretch), cat.bpm[a] * m * (1 + max_stretch)])
            cand.append(I_sorted[lo:hi])
        B = np.unique(np.concatenate(cand))
        B = B[(B != a) & (cat.base[B] != cat.base[a])]
        if B.size == 0:
            continue
        kc = KEY_COST[cat.key[a], cat.key[B]]
        ok = np.isfinite(kc)
        B, kc = B[ok], kc[ok]
        if B.size == 0:
            continue
        ratios = np.stack([cat.bpm[B] / (cat.bpm[a] * m) for m in TEMPO_MULTS])
        mi = np.argmin(np.abs(ratios - 1), axis=0)
        ratio = ratios[mi, np.arange(B.size)]
        stretch = np.abs(ratio - 1)
        s = 100 - W["stretch"] * stretch - W["key"] * kc
        s -= W["low_key_conf"] * ((cat.key_conf[B] < 0.15) | (cat.key_conf[a] < 0.15))
        s -= W["tuning"] * np.clip(np.abs(cat.tuning[B] - cat.tuning[a]) - 20, 0, None)
        s += W["acapella"] * cat.acapella[a] + W["instrumental"] * cat.instrumental[B]
        s += W["familiar"] * (cat.familiar[a] + cat.familiar[B].astype(float))
        s += W["genre_contrast"] * ((cat.genre[B] != cat.genre[a]) & (cat.genre[B] != "") & (cat.genre[a] != ""))
        s += W["groove"] * (cat.dance[B] - 0.7)      # instrumental needs a beat to carry the vocal
        s -= W["club_vocal"] * (cat.club[a] and not cat.acapella[a])   # club cuts rarely carry a usable vocal
        s -= W["same_artist"] * (cat.artist[B] == cat.artist[a])
        s -= W["unknown_artist"] * ((~cat.known[B]).astype(float) + (not cat.known[a]))
        top = np.argsort(-s)[:per_vocal]
        for t in top:
            if s[t] < floor:
                continue
            b = int(B[t])
            out.append({"vocal": int(a), "inst": b, "score": round(float(s[t]), 1),
                        "stretch": round(float(ratio[t] - 1), 4), "tempo_mult": float(TEMPO_MULTS[mi[t]]),
                        "shift": int(KEY_SHIFT[cat.key[a], cat.key[b]]), "relation": KEY_REL[cat.key[a], cat.key[b]]})
    out.sort(key=lambda p: -p["score"])
    return out


LIKE = {"vocal_artist": 0.4, "beat_artist": 0.3, "beat_genre": 0.15, "beat_tempo": 0.15}   # how a pair resembles a rated one


def taste(cat, P, rated):
    """Mark's taste, from the mashups he rated: rated = [(vocal path, instrumental path, stars 1-5)]. Each pair
    gains or loses up to W["taste"] points by how much it resembles rated pairs (LIKE: same vocal artist, same beat
    artist, same beat genre, beat tempo within 4 %, also at half or double), times (stars - 3) / 2 for each, summed and
    capped. Pairs are re-sorted. Nothing rated, or nothing in this catalog: P unchanged."""
    at = {r["path"]: i for i, r in enumerate(cat.rows)}
    R = [(at[v], at[b], (st - 3) / 2) for v, b, st in rated if v in at and b in at and st != 3]
    if not R or not P:
        return P
    a, b = np.array([p["vocal"] for p in P]), np.array([p["inst"] for p in P])
    same = lambda col, ix, r: (col[ix] == col[r]) & (col[r] != "")
    t, beat = np.zeros(len(P)), np.outer(cat.bpm[b], TEMPO_MULTS)
    for ra, rb, w in R:                                       # a few ratings, each over all pairs at once
        near = np.min(np.abs(beat / cat.bpm[rb] - 1), axis=1) <= 0.04
        t += w * (LIKE["vocal_artist"] * same(cat.artist, a, ra) + LIKE["beat_artist"] * same(cat.artist, b, rb)
                  + LIKE["beat_genre"] * same(cat.genre, b, rb) + LIKE["beat_tempo"] * near)
    for p, x in zip(P, np.clip(t * W["taste"], -W["taste"], W["taste"])):
        p["taste"] = round(float(x), 1)
        p["score"] = round(p["score"] + p["taste"], 1)
    P.sort(key=lambda p: -p["score"])
    return P


def diversify(pairs, max_per_track=2, limit=50, per_vocal=None, per_inst=None, song=None):
    """Cap how often one track appears so the shortlist isn't 50 partners of one song.
    per_vocal / per_inst override max_per_track for that side (e.g. 1 vocal each, 4 per instrumental).
    song(i) maps a track to its song, so two copies, remasters or versions of one song count as one vocal, and as
    one instrumental."""
    song = song or (lambda i: i)
    cap_v = per_vocal if per_vocal is not None else max_per_track
    cap_i = per_inst if per_inst is not None else max_per_track
    nv, ni, done, res = {}, {}, set(), []
    for p in pairs:
        v, i = song(p["vocal"]), song(p["inst"])
        if nv.get(v, 0) >= cap_v or ni.get(i, 0) >= cap_i:
            continue
        if frozenset((p["vocal"], p["inst"])) in done:      # A>>B already listed; skip B>>A
            continue
        done.add(frozenset((p["vocal"], p["inst"])))
        nv[v] = nv.get(v, 0) + 1
        ni[i] = ni.get(i, 0) + 1
        res.append(p)
        if len(res) >= limit:
            break
    return res
