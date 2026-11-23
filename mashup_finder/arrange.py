"""Mashup engine, phase 2: templates (TOML data in templates/) and the arranger.

arrange() fills each template section from B's sections of the matching type and assigns A's vocal phrases
by role. The result is the editable arrangement: lanes (vocal, drums, bass, other) of blocks, with the
template sections kept as labels over the timeline. A block is

    {"lane", "out_bar", "bars", "src": "A" | "B", "src_bar", "gain_db", "fx", "shift"}

Bars are 0-based. `bars` counts output bars; one A bar covers `tempo_mult` output bars (2 when the vocal
runs half-time, 0.5 double-time), so a vocal block reads bars / tempo_mult A bars from src_bar.
fx entries are {"type", "at", "bars"}: the effect spans `bars` output bars starting `at` bars from the block's
start (a section-wide sweep keeps its shape when a section is made of several blocks, and moves with them)."""
import json, os, tomllib

import numpy as np

from . import deep

TEMPLATES = os.path.join(deep.ROOT, "templates")
FORMAT = "mashup-arrangement/2"
INST_TYPES = ("intro", "build", "drop", "break", "outro", "any")
LANE_SOURCE = {"vocal": "vocals", "drums": "drums", "bass": "bass", "other": "other"}   # lane -> source stem
LANES = tuple(LANE_SOURCE)
INST_STEMS = LANES[1:]
ROLES = ("hook", "verse", "adlib", "none")
FX = ("lowpass_sweep_up", "highpass_sweep_down", "drop_cut", "filter_open", "fade_in", "fade_out")
FILTER_FX = ("lowpass_sweep_up", "highpass_sweep_down", "filter_open")
ROLE_FALLBACK = {"hook": ("verse", "adlib"), "verse": ("hook", "adlib"), "adlib": ("verse", "hook")}
VOCAL_UNDER_BED_DB = 1.0


def load_template(name):
    """A template name (templates/<name>.toml) or a path to a .toml file, checked and with defaults filled in."""
    path = name if name.endswith(".toml") else os.path.join(TEMPLATES, name + ".toml")
    with open(path, "rb") as f:
        t = tomllib.load(f)
    secs = t.get("section") or []
    if not secs:
        raise ValueError(f"{path}: no [[section]] entries")
    for i, s in enumerate(secs, 1):
        where = f"{path}, section {i}"
        bars = s.get("bars")
        if not isinstance(bars, int) or bars <= 0 or bars % 4:
            raise ValueError(f"{where}: bars must be a positive multiple of 4")
        if s.get("inst") not in INST_TYPES:
            raise ValueError(f"{where}: inst must be one of {', '.join(INST_TYPES)}")
        s.setdefault("name", s["inst"])
        s.setdefault("inst_stems", list(INST_STEMS))
        s.setdefault("vocal", "none")
        s.setdefault("vocal_bars", bars)
        s.setdefault("fx", [])
        if not s["inst_stems"] or set(s["inst_stems"]) - set(INST_STEMS):
            raise ValueError(f"{where}: inst_stems must come from {', '.join(INST_STEMS)}")
        if s["vocal"] not in ROLES:
            raise ValueError(f"{where}: vocal must be one of {', '.join(ROLES)}")
        if not 0 < s["vocal_bars"] <= bars or s["vocal_bars"] % 4:
            raise ValueError(f"{where}: vocal_bars must be a multiple of 4, at most bars")
        if set(s["fx"]) - set(FX):
            raise ValueError(f"{where}: fx must come from {', '.join(FX)}")
    return {"name": t.get("name", os.path.splitext(os.path.basename(path))[0]), "sections": secs}


def source_name(src, lane):
    """The blocks/ source a block plays from: A-vocals, B-drums, B-bass or B-other."""
    return f"{src}-{LANE_SOURCE[lane]}"


def load_arrangement(path):
    """Read an arrangement.json (arranged or hand-edited) and check it, naming the block at fault."""
    with open(path) as f:
        arr = validate(json.load(f), path)
    if "vocal_gain_db" not in arr:              # arranged before it was recorded: the most common vocal block gain
        gains = [b.get("gain_db", 0.0) for b in arr["lanes"].get("vocal", [])]
        arr["vocal_gain_db"] = max(set(gains), key=gains.count) if gains else 0.0
    return arr


def validate(arr, path="arrangement"):
    """Check an arrangement dict (from a file or the editor) and return it; ValueError names the fault."""
    if not isinstance(arr, dict):
        raise ValueError(f"{path}: not a JSON object")
    if arr.get("format") != FORMAT:
        raise ValueError(f"{path}: format is {arr.get('format')!r}, expected {FORMAT!r}")
    for k in ("output_bpm", "bars", "tempo_mult", "lanes", "sources"):
        if k not in arr:
            raise ValueError(f"{path}: missing {k!r}")
    if not isinstance(arr.get("vocal_gain_db", 0.0), (int, float)):
        raise ValueError(f"{path}: vocal_gain_db must be a number")
    if not isinstance(arr["lanes"], dict) or not all(isinstance(v, list) for v in arr["lanes"].values()):
        raise ValueError(f"{path}: lanes must map lane names to lists of blocks")
    if set(arr["lanes"]) - set(LANES):
        raise ValueError(f"{path}: unknown lane(s) {sorted(set(arr['lanes']) - set(LANES))}; lanes are {', '.join(LANES)}")
    for lane, blocks in arr["lanes"].items():
        for i, b in enumerate(blocks):
            where = f"{path}: lanes.{lane}[{i}]"
            if not isinstance(b, dict):
                raise ValueError(f"{where}: a block must be an object")
            if b.get("lane", lane) != lane:
                raise ValueError(f"{where}: says lane {b['lane']!r}; move it into lanes.{b['lane']}")
            if b.get("src") not in ("A", "B") or source_name(b["src"], lane) not in arr["sources"]:
                raise ValueError(f"{where}: src {b.get('src')!r} has no {lane} source (sources: {', '.join(arr['sources'])})")
            if not isinstance(b.get("out_bar"), (int, float)) or b["out_bar"] < 0:
                raise ValueError(f"{where}: out_bar must be >= 0 (bars; 0.25 = one beat)")
            if not isinstance(b.get("bars"), (int, float)) or b["bars"] <= 0:
                raise ValueError(f"{where}: bars must be > 0")
            if not isinstance(b.get("gain_db", 0.0), (int, float)) or not isinstance(b.get("shift", 0.0), (int, float)):
                raise ValueError(f"{where}: gain_db and shift must be numbers")
            for f, most, words in (("fade_in", 4, "4 bars"), ("fade_out", b["bars"], "the block's length")):   # the fade handles
                if f in b and not (isinstance(b[f], (int, float)) and not isinstance(b[f], bool) and 0 <= b[f] <= most):
                    raise ValueError(f"{where}: {f} must be 0 to {words} (bars)")
            if not isinstance(b.get("fx", []), list):
                raise ValueError(f"{where}: fx must be a list")
            if not isinstance(b.get("src_bar"), (int, float)):
                raise ValueError(f"{where}: src_bar must be a number")
            for f in b.get("fx", []):
                if not isinstance(f, dict) or f.get("type") not in FX + ("pickup",) or not isinstance(f.get("at"), (int, float)) or not f.get("bars", 0) > 0:
                    raise ValueError(f"{where}: fx {f} needs a type from {', '.join(FX + ('pickup',))}, 'at' and 'bars' > 0")
    return arr


def tempo_mult(bpm_a, bpm_b):
    """Output (B) bars per A bar, same rule as deep.analyse_pair: 1, or 2 (half-time vocal) or 0.5 (double)."""
    return min((1.0, 2.0, 0.5), key=lambda k: abs(bpm_b / (bpm_a * k) - 1))


def _num(x):
    return int(x) if float(x).is_integer() else round(float(x), 4)


def _pick(cands, weight, rng, used):
    """Greedy (rng None): least used, then heaviest, then earliest. Seeded: weighted draw."""
    if rng is None:
        return min(cands, key=lambda c: (used.get(c["start"], 0), -weight(c), c["start"]))
    w = np.array([weight(c) for c in cands], float) + 1e-9
    return cands[int(rng.choice(len(cands), p=w / w.sum()))]


def _inst_source(mb, want, bars, rng, used):
    """(start, end, label used, fallback note or None, drop the drums?) from B's sections."""
    secs = mb["sections"]
    by = lambda lab: [s for s in secs if lab in ("any", s["label"])]
    weight = lambda s: s["bars"]
    if by(want):
        s = _pick(by(want), weight, rng, used)
        return s["start"], s["start"] + min(s["bars"], bars), s["label"], None, False
    drops = by("drop")
    if want == "build" and drops and drops[0]["start"] >= 8:
        d = drops[0]["start"]
        return d - 8, d, "drop", "no build in B: the 8 bars before the first drop", False
    if want == "break" and drops:
        s = _pick(drops, weight, rng, used)
        return s["start"], s["start"] + min(s["bars"], bars), "drop", "no break in B: a drop without drums", True
    s = _pick(by("any"), weight, rng, used)
    return s["start"], s["start"] + min(s["bars"], bars), s["label"], f"no {want} in B: any section ({s['label']})", False


MIN_LOOP = 16    # a B source shorter than this (and than the section) is lengthened before it loops


def _lengthen(mb, b0, b1, want, need):
    """A source range too short for its section loops audibly (a 4-bar intro played 8 times). Lengthen it with
    B's own neighbouring bars, as the track plays them, to min(need, MIN_LOOP): forward first, never into a drop,
    then backward, never into a drop. Drops are left alone: a drop repeating is how club edits work."""
    target = min(need, MIN_LOOP)
    if want == "drop" or b1 - b0 >= target:
        return b0, b1
    drops = [(s["start"], s["start"] + s["bars"]) for s in mb["sections"] if s["label"] == "drop"]
    hi = min([a for a, _ in drops if a >= b1] + [mb["bars"]])
    lo = max([z for _, z in drops if z <= b0] + [0])
    b1 = min(hi, b0 + target)
    b0 = max(lo, b1 - target)
    return b0, b1


def _vocal_phrase(ma, role, prev, rng):
    """(phrase, fallback note or None). Never the previous section's phrase back to back, unless it is a hook
    or nothing else is left."""
    ph = ma["phrases"]
    for r in (role,) + ROLE_FALLBACK[role]:
        cands = [p for p in ph if p["role"] == r and (r == "hook" or p["start"] != prev)]
        if cands:
            break
    else:
        cands = [p for p in ph if p["role"] in (role,) + ROLE_FALLBACK[role]]
        if not cands:
            return None, f"no vocal phrase in A for {role}"
        r = cands[0]["role"]
    if rng is None:
        p = min(cands, key=lambda p: p["rank"])
    else:
        w = np.array([p["score"] for p in cands], float) + 1e-9
        p = cands[int(rng.choice(len(cands), p=w / w.sum()))]
    return p, (None if r == role else f"no {role} in A: used a {r}")


def vocal_gain_db(ma, mb):
    """One gain for every vocal block: A's active vocal about 1 dB under B's full bed (its drop bars if any)."""
    act = ma["vocal_active"]
    v = float(np.sqrt((ma["rms"]["vocals"][act] ** 2).mean())) if act.any() else 0.0
    drops = [b for s in mb["sections"] if s["label"] == "drop" for b in range(s["start"], s["start"] + s["bars"])]
    bars = np.array(drops or range(mb["bars"]))
    bed = float(np.sqrt(sum((mb["rms"][s][bars] ** 2).mean() for s in INST_STEMS)))
    return round(20 * np.log10(bed / v) - VOCAL_UNDER_BED_DB, 2) if v > 0 and bed > 0 else 0.0


def _block(lane, out_bar, bars, src, src_bar, gain_db=0.0, shift=0.0):
    return {"lane": lane, "out_bar": int(out_bar), "bars": _num(bars), "src": src, "src_bar": _num(src_bar),
            "gain_db": gain_db, "fx": [], "shift": shift}


def arrange(ma, mb, tpl, m, shift, variation=0, seed=1, attempt=0):
    """Variation 0 is the greedy best; others draw from a generator seeded by (seed, variation, attempt)."""
    rng = None if variation == 0 else np.random.default_rng([seed, variation, attempt])
    shift, g_voc = round(float(shift), 3), vocal_gain_db(ma, mb)
    used, prev, o, sections, notes = {}, None, 0, [], []
    lanes = {k: [] for k in LANES}
    has_vocal = np.zeros(sum(t["bars"] for t in tpl["sections"]), bool)
    for t in tpl["sections"]:
        S, mine = t["bars"], []
        b0, b1, label, fb, no_drums = _inst_source(mb, t["inst"], S, rng, used)
        used[b0] = used.get(b0, 0) + 1
        b0, b1 = _lengthen(mb, b0, b1, t["inst"], S)
        if b1 - b0 < S and b1 - b0 > 4:
            b1 = b0 + (b1 - b0) // 4 * 4                     # a loop is a whole number of 4-bar phrases
        stems = [s for s in t["inst_stems"] if not (no_drums and s == "drums")]
        for k in range(0, S, b1 - b0):                       # B loops when the section needs more bars
            for stem in stems:
                mine.append(_block(stem, o + k, min(b1 - b0, S - k), "B", b0))
        sec = {"name": t["name"], "out_bar": o, "bars": S, "inst": t["inst"], "b_label": label,
               "fallbacks": [fb] if fb else []}
        if t["vocal"] != "none":
            p, vfb = _vocal_phrase(ma, t["vocal"], prev, rng)
            if vfb:
                sec["fallbacks"].append(vfb)
            if p:
                per, start = int(round(p["bars"] * m)), o + S - t["vocal_bars"]
                for i, q in enumerate(range(start, o + S, per)):   # the phrase repeats to fill its bars
                    blk = _block("vocal", q, min(per, o + S - q), "A", p["start"], g_voc, shift)
                    pre = max(1, int(round(m)))
                    if i == 0 and q >= pre and p["start"] >= pre / m and not has_vocal[q - pre:q].any():
                        blk["fx"].append({"type": "pickup", "at": -pre, "bars": pre})   # keeps the pickups
                    mine.append(blk)
                has_vocal[start:o + S] = True
                sec["vocal"] = {"role": p["role"], "phrase": p["start"], "phrase_bars": p["bars"]}
                prev = p["start"]
        for blk in mine:                                      # section fx, spanning the section
            for f in t["fx"]:
                if blk["lane"] != "vocal" or f not in FILTER_FX:
                    blk["fx"].append({"type": f, "at": o - blk["out_bar"], "bars": S})
            lanes[blk["lane"]].append(blk)
        notes += [f"{t['name']}: {n}" for n in sec["fallbacks"]]
        sections.append(sec)
        o += S
    for lane in lanes.values():
        lane.sort(key=lambda b: b["out_bar"])
    return {"format": FORMAT, "bars_are": "0-based; out_bar and bars count output bars",
            "template": tpl["name"], "variation": variation, "seed": seed, "tempo_mult": m, "shift": shift, "vocal_gain_db": g_voc,
            "bpm_a": round(ma["bpm"], 3), "bpm_b": round(mb["bpm"], 3), "a_bars": ma["bars"], "b_bars": mb["bars"],
            "bars": o, "sections": sections, "lanes": lanes, "fallbacks": notes}


def signature(arr):
    return json.dumps({k: [(b["out_bar"], b["src_bar"]) for b in v] for k, v in arr["lanes"].items()})


def variations(ma, mb, tpl, m, shift, n=1, seed=1, tries=25):
    """n arrangements; each differs from the earlier ones in B sections or vocal phrases where the material
    allows. When it does not, the repeat is kept and marked 'duplicate_of'."""
    out, seen = [], {}
    for v in range(n):
        for attempt in range(tries if v else 1):
            arr = arrange(ma, mb, tpl, m, shift, v, seed, attempt)
            if signature(arr) not in seen:
                break
        else:
            arr["duplicate_of"] = seen[signature(arr)]
        seen.setdefault(signature(arr), v)
        out.append(arr)
    return out
