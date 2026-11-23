"""mf — find mashup pairs in the music catalog.

  mf find  [--brief FILE] [--seed TEXT] [--role vocal|inst|any] [--genre TEXT] [--max-stretch PCT] [--limit N] [--name NAME]
  mf check RUN [--top N]          audio check + previews for the top N of a find run, writes audition.html
  mf run   (find options) [--top N]   both in one go
  mf mashup RUN PAIR_ID | --folder DIR | --render ARRANGEMENT.json   arranged full-length mashup (docs/ENGINE-PLAN.md)
  mf edit MASHUP_DIR             block editor in the browser (127.0.0.1 only)
  mf app                         Mashup Studio: all of the above in one local page (127.0.0.1 only)
  mf scan  [FOLDER ...] [--catalog CSV]   read music folders into the catalog (default: the settings' folders and CSV)
"""
import argparse, datetime, glob, json, os, re, sys, time
import numpy as np

from . import config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS = os.path.join(config.DATA, "runs")


def ratings(runs=None):
    """[(vocal path, instrumental path, stars)] of every rated mashup (rating.json in its folder, from `mf app`)."""
    out = []
    for f in glob.glob(os.path.join(runs or RUNS, "*", "mashups", "*", "rating.json")):
        try:
            with open(f) as fh:
                r = json.load(fh)
            if isinstance(r.get("rating"), int):
                out.append((r["vocal_path"], r["inst_path"], r["rating"]))
        except (OSError, ValueError, KeyError, AttributeError):  # a broken file is no rating
            pass
    return out


def quick_match(cat, brief, seed=None, role="any", genre=None, max_stretch=None, per_track=None, limit=None, skip_vocals=(), rated=()):
    """The catalog part of a find, no audio and nothing written: (pairs best first, counts). Command-line values
    win over the brief's [match]. counts: "hits" (the tracks the seed text matches, or None), "role", "sides"
    ([vocals, beats] per direction scored), "pairs", "max_stretch" (percent). rated: ratings(), Mark's taste."""
    from .pairs import candidates, score_pairs, diversify, taste
    from . import criteria
    m = brief.get("match", {})
    max_stretch = (max_stretch if max_stretch is not None else m.get("max_stretch", 8.0)) / 100
    per_track = per_track if per_track is not None else m.get("per_track", 2)
    limit = limit if limit is not None else m.get("limit", 40)
    beats_each = max(5, m.get("per_vocal") or 0)    # the quick match's beats per vocal: the brief's Per vocal when above 5
    counts = {"hits": None, "role": role, "sides": [], "pairs": 0, "max_stretch": round(max_stretch * 100, 1)}

    def both(x, y):                          # AND of two optional index lists (None = no filter)
        return y if x is None else x if y is None else np.intersect1d(x, y)

    vi, ii = criteria.select(cat, brief.get("vocal")), criteria.select(cat, brief.get("inst"))
    hits = None
    if seed:
        hits = np.array(cat.search(seed), int)
        counts["hits"] = hits.tolist()
        if not hits.size:
            return [], counts
    if genre:
        g = np.array([i for i in range(cat.n) if genre.lower() in cat.rows[i]["genre"].lower()], int)
        if seed and role == "vocal":
            ii = both(ii, g)
        elif seed and role == "inst":
            vi = both(vi, g)
        else:
            vi, ii = both(vi, g), both(ii, g)
    sides = [(vi, ii)] if hits is None else \
        [(both(vi, hits), ii)] * (role in ("vocal", "any")) + [(vi, both(ii, hits))] * (role in ("inst", "any"))
    P = []
    for v, i in sides:
        counts["sides"].append([len(x) for x in candidates(cat, v, i)])
        P += score_pairs(cat, max_stretch, vocal_idx=v, inst_idx=i, per_vocal=beats_each)
    if hits is not None:
        P.sort(key=lambda p: -p["score"])
    P = taste(cat, P, rated)
    P = [p for p in P if cat.rows[p["vocal"]]["path"] not in skip_vocals]
    P = diversify(P, max_per_track=per_track if not seed else 10 ** 6, limit=limit,
                  per_vocal=m.get("per_vocal"), per_inst=m.get("per_inst"),
                  song=lambda i: (cat.artist[i].replace("&", "and"), cat.base[i]))
    counts["pairs"] = len(P)
    return P, counts


def match_line(n):
    """quick_match's counts in words, and why there are no pairs: ("1 vocal × 12 beats → 7 pairs", None)."""
    many = lambda k, w: f"{k:,} {w}{'' if k == 1 else 's'}"
    line = " + ".join(f"{many(v, 'vocal')} × {many(b, 'beat')}" for v, b in n["sides"]) or "0 tracks"
    song, no_v, no_b = n["hits"] is not None, all(v == 0 for v, _ in n["sides"]), all(b == 0 for _, b in n["sides"])
    why = (None if n["pairs"] else
           "no track matches the song" if n["hits"] == [] else
           "the song cannot give the vocal (an instrumental, or left out by [vocal])" if song and n["role"] == "vocal" and no_v else
           "the song cannot carry the beat (an acapella, or left out by [inst])" if song and n["role"] == "inst" and no_b else
           "no vocal passes the [vocal] filters" if no_v else
           "no beat passes the [inst] filters" if no_b else
           f"no vocal and beat within {n['max_stretch']:g}% of each other's tempo in keys that fit")
    return f"{line} → {many(n['pairs'], 'pair')}", why


def _find(a):
    from .catalog import Catalog
    from .compat import key_name, camelot
    from . import criteria
    t = time.time()
    cat = Catalog()
    brief = criteria.load(a.brief) if a.brief else {}
    a.top = getattr(a, "top", None) or brief.get("match", {}).get("top", TOP)
    try:
        seed, role = criteria.song(brief, a.seed, a.role)
    except ValueError as e:
        sys.exit(f"{a.brief}: {e}")
    if brief:
        print(f"brief {brief['name']}: {criteria.describe(cat, brief)}")
    seen = set()
    if a.fresh:
        # Skip vocals already tried in earlier runs of this brief / name.
        tag = re.sub(r"[^a-z0-9]+", "-", (a.name or brief.get("name") or seed or a.genre or "library").lower()).strip("-")[:40]
        for d in os.listdir(RUNS):
            f = os.path.join(RUNS, d, "results.json")             # only pairs that were audio-checked
            if d.endswith("-" + tag) and os.path.exists(f):
                seen |= {q["vocal_path"] for q in json.load(open(f))}
    P, n = quick_match(cat, brief, seed, role, a.genre, a.max_stretch, a.per_track, a.limit, seen, ratings())
    if seed:
        if not n["hits"]:
            sys.exit(f"no online track matches {seed!r}")
        print(f"seed: {len(n['hits'])} track(s), e.g. {cat.label(n['hits'][0])}")
    if a.fresh:                                                  # printed after the seed line, as it always was
        print(f"fresh: skipping {len(seen)} vocals checked in earlier '{tag}' runs")
    for i, p in enumerate(P):
        va, ib = cat.rows[p["vocal"]], cat.rows[p["inst"]]
        p.update(id=i, vocal_label=cat.label(p["vocal"]), inst_label=cat.label(p["inst"]),
                 vocal_path=va["path"], inst_path=ib["path"], vocal_bpm=va["bpm"], inst_bpm=ib["bpm"],
                 vocal_key=f"{key_name(va['key'])} {camelot(va['key'])}", inst_key=f"{key_name(ib['key'])} {camelot(ib['key'])}",
                 tuning_fix=round(((ib["tuning"] or 0) - (va["tuning"] or 0)) / 100, 3) if va["tuning"] is not None and ib["tuning"] is not None else 0.0)
    name = a.name or re.sub(r"[^a-z0-9]+", "-", (a.seed or brief.get("name") or seed or a.genre or "library").lower()).strip("-")[:40]
    run = os.path.join(RUNS, datetime.datetime.now().strftime("%Y%m%d-%H%M") + "-" + name)
    os.makedirs(run, exist_ok=True)
    json.dump({"args": vars(a), "brief": brief, "pairs": P}, open(os.path.join(run, "shortlist.json"), "w"), indent=1, ensure_ascii=False)
    print(f"{cat.n} online tracks, {len(P)} pairs in {time.time() - t:.0f} s -> {run}")
    for p in P:
        print(f"{p['id']:3d} {p['score']:5.1f}  {p['vocal_label'][:42]:42s} >> {p['inst_label'][:42]:42s} "
              f"{p['stretch'] * 100:+5.1f}% {p['shift']:+d}st {p['relation']}")
    return run


STRETCH_COST, SHIFT_COST = 300, 12             # final ranking: points per 100 % stretch, per semitone of shift
TOP = 12                                        # pairs the audio check takes, unless told


def promise(p):
    """How much the catalog match expects of a pair, best first: the stretch and shift costs the final ranking uses,
    then the score. The audio check goes in this order, so stopping early keeps the likeliest pairs."""
    return STRETCH_COST * abs(p["stretch"]) + SHIFT_COST * abs(p["shift"]), -p["score"]


def _check(run, top, redo=False, log=print):
    from . import deep, audition
    from .render import write_json
    S = json.load(open(os.path.join(run, "shortlist.json")))
    res_f = os.path.join(run, "results.json")
    done = {p["id"]: p for p in json.load(open(res_f))} if os.path.exists(res_f) and not redo else {}
    pairs = S["pairs"][:top]
    ms = S.get("args", {}).get("max_stretch")                 # as the shortlist was made: --max-stretch, then the brief
    ms = (ms if ms is not None else S.get("brief", {}).get("match", {}).get("max_stretch", 8.0)) / 100
    out_dir = os.path.join(run, "previews")
    for p in pairs:
        if p["id"] in done and done[p["id"]].get("deep"):
            p["deep"] = done[p["id"]]["deep"]
    todo = sorted((p for p in pairs if "deep" not in p), key=promise)
    for k, p in enumerate(todo, 1):
        t = time.time()
        log(f"[{k}/{len(todo)}] {p['vocal_label']} >> {p['inst_label']}")
        try:
            d = deep.analyse_pair(p["vocal_path"], p["inst_path"], p, out_dir, log, max_stretch=ms)
            if d.get("preview"):
                d["preview"] = "previews/" + d["preview"]
        except Exception as e:                       # one bad file must not kill the run
            d = {"ok": False, "why": f"error: {e!r}"[:200]}
        p["deep"] = d
        log(f"    {'ok' if d.get('ok') else 'FAIL ' + d.get('why', '')} in {time.time() - t:.0f} s")
        done[p["id"]] = p
        write_json(res_f, list(done.values()))                   # whole, for the app reading it meanwhile
    for p in pairs:
        d = p["deep"]
        if not d.get("ok"):
            p["final"] = -1.0
            continue
        # Re-rank with what the audio said: real stretch and shift, harmonic fit, vocal quality.
        p["final"] = round(p["score"]
                           - STRETCH_COST * (abs(d["stretch_audio"]) - abs(p["stretch"]))
                           - SHIFT_COST * (abs(d["shift_audio"]) - abs(p["shift"]))
                           + 8 * d["harm_z"] - 4 * (d["harm_rank"] - 1)
                           + 10 * (d["vocal_active"] - 0.7) + 20 * (d["vocal_share"] - 0.3)
                           - 0.3 * max(0, (d.get("drift_ms") or 0) - 30), 1)
    pairs.sort(key=lambda p: -p["final"])
    _prune_cache()
    write_json(res_f, pairs)
    f = audition.write(run, pairs, os.path.basename(run))
    log(f"audition page: {f}")
    return f


CACHE_CAP_GB = 8


def _prune_cache(cap_gb=CACHE_CAP_GB):
    """Stems are ~90 MB a track: drop the least recently used beyond the cap (re-separated on demand)."""
    from . import deep
    d = os.path.join(deep.CACHE, "stems")
    if not os.path.isdir(d):
        return
    fs = sorted((os.path.join(d, f) for f in os.listdir(d)), key=os.path.getatime, reverse=True)
    total = 0
    for f in fs:
        total += os.path.getsize(f)
        if total > cap_gb * 1e9:
            os.remove(f)


def _safe(s):
    return re.sub(r'[/:\\*?"<>|]+', "-", s).strip()[:90]


def _pair_folder(p, brief):
    """(sub folder, pair folder) that a checked pair is filed under on export, e.g. ("", "A x B (8A-9A)"): every pair
    folder sits directly in the SSD's Mashups folder, unless the brief names an ssd_folder to file its pairs in.
    `mf app` copies mashups into the same folder."""
    cam = lambda s: s.split()[-1]
    name = f"{_safe(p['vocal_label'])[:70]} x {_safe(p['inst_label'])[:70]} ({cam(p['vocal_key'])}-{cam(p['inst_key'])})"
    own = _safe(str((brief or {}).get("ssd_folder") or "")).lstrip(".")     # one folder name, never .. or hidden
    return own, name


def _export(run, picks_file, dest, log=print):
    """Copy kept pairs into pair folders: originals (VOCAL - / INST -), the separated stems as WAV
    (vocal of A, instrumental of B) and a NOTES.txt (no mp3 preview: no mp3s on the SSD). Never overwrites a file. Picks marked
    "skip" (by `mf app`, which keeps both marks in the run's picks.json) are left out."""
    import csv, shutil
    import soundfile as sf
    from . import deep
    from .compat import parse_key, camelot
    R = {p["id"]: p for p in json.load(open(os.path.join(run, "results.json")))}
    brief = json.load(open(os.path.join(run, "shortlist.json"))).get("brief")
    picks = json.load(open(os.path.expanduser(picks_file)))
    log_rows = []
    for k in picks:
        if k.get("mark", "keep") != "keep":
            continue
        p = R.get(k["id"])
        if not p or p["inst_path"] != k["inst_path"]:
            log(f"skip: pick {k.get('id')} is not in {os.path.basename(run)}")
            continue
        d = p["deep"]
        sub, name = _pair_folder(p, brief)
        folder = os.path.join(dest, sub, name)
        os.makedirs(folder, exist_ok=True)

        def put(src_fn, fname):
            out = os.path.join(folder, fname)
            if os.path.exists(out):
                return "exists"
            part = os.path.join(folder, "~part-" + fname)        # a cancelled export leaves no half file under the real name
            try:
                src_fn(part)
                os.replace(part, out)
            finally:
                if os.path.exists(part):
                    os.remove(part)
            return "copied"

        ext = lambda f: os.path.splitext(f)[1]
        put(lambda o: shutil.copy2(p["vocal_path"], o), f"VOCAL - {_safe(p['vocal_label'])}{ext(p['vocal_path'])}")
        put(lambda o: shutil.copy2(p["inst_path"], o), f"INST - {_safe(p['inst_label'])}{ext(p['inst_path'])}")
        if d.get("ok"):
            put(lambda o: sf.write(o, deep.separate(p["vocal_path"])[3].T, deep.SR, subtype="PCM_16"),
                f"VOCAL STEM - {_safe(p['vocal_label'])}.wav")
            put(lambda o: sf.write(o, deep.separate(p["inst_path"])[:3].sum(0).T, deep.SR, subtype="PCM_16"),
                f"INST STEM (no vocals) - {_safe(p['inst_label'])}.wav")
            notes = (f"Vocal:  {p['vocal_label']}  ({d['bpm_a']:.1f} BPM, catalog key {p['vocal_key']})\n"
                     f"Inst:   {p['inst_label']}  ({d['bpm_b']:.1f} BPM, catalog key {p['inst_key']})\n"
                     f"Warp the vocal {d['stretch_audio'] * 100:+.1f}%"
                     f"{' as half-time (one vocal beat = two inst beats)' if d['tempo_mult'] == 2 else ' as double-time' if d['tempo_mult'] == 0.5 else ''},"
                     f" shift {d['shift_audio']:+d} semitones (harmonic fit rank {d['harm_rank']}/12).\n"
                     f"Preview uses vocal bar {d['vocal_bar']} over instrumental bar {d['inst_bar']} ({d['bars']} bars); beat drift {d['drift_ms']} ms.\n"
                     f"Stems: Demucs htdemucs (personal use). Originals untouched.\n")
            put(lambda o: open(o, "w").write(notes), "NOTES.txt")
        log(f"  {sub}/{name}")
        log_rows.append({"folder": f"{sub}/{name}", "vocal": p["vocal_label"], "inst": p["inst_label"],
                         "stretch_pct": round(d.get("stretch_audio", p["stretch"]) * 100, 1),
                         "shift_st": d.get("shift_audio", p["shift"]), "tempo_mult": d.get("tempo_mult", p["tempo_mult"]),
                         "run": os.path.basename(run), "vocal_src": p["vocal_path"], "inst_src": p["inst_path"]})
    if log_rows:
        f = os.path.join(dest, "MASHUP-FINDER.csv")
        new = not os.path.exists(f)
        with open(f, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(log_rows[0]))
            if new:
                w.writeheader()
            w.writerows(log_rows)
    log(f"{len(log_rows)} pair folders in {dest}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mf", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("find", "run"):
        s = sub.add_parser(name)
        s.add_argument("--seed", help="artist/title text; finds partners for these tracks")
        s.add_argument("--role", default="any", choices=("vocal", "inst", "any"), help="seed provides the vocal, the instrumental, or either")
        s.add_argument("--genre", help="restrict the other side (or both, without a seed) to this genre text")
        s.add_argument("--brief", help="TOML brief with vocal/inst criteria (see briefs/ and criteria.py)")
        s.add_argument("--max-stretch", type=float, help="max time-stretch in percent (default 8, or the brief's)")
        s.add_argument("--limit", type=int, help="pairs in the shortlist (default 40)")
        s.add_argument("--per-track", type=int, help="max appearances of one track in a library-wide list (default 2)")
        s.add_argument("--name")
        s.add_argument("--fresh", action="store_true", help="skip vocals already audio-checked in earlier runs of the same brief/name")
        if name == "run":
            s.add_argument("--top", type=int, help="pairs to audio-check (default 12)")
    c = sub.add_parser("check")
    c.add_argument("run")
    c.add_argument("--top", type=int, default=12)
    c.add_argument("--redo", action="store_true", help="re-run the audio check for pairs already checked")
    e = sub.add_parser("export", help="copy kept pairs (picks.json from the audition page) into pair folders")
    e.add_argument("run")
    e.add_argument("--picks", default="~/Downloads/picks.json")
    e.add_argument("--dest", default=config.load()["export_folder"], help="default: the settings' export_folder")
    x = sub.add_parser("mashup", help="arrange a found pair into a full-length mashup (docs/ENGINE-PLAN.md)")
    x.add_argument("run", nargs="?", help="run folder (name under runs/ or a path)")
    x.add_argument("pair_id", nargs="?", type=int, help="pair id in that run's results.json")
    x.add_argument("--folder", help="an exported pair folder (resolved through MASHUP-FINDER.csv)")
    x.add_argument("--render", metavar="ARRANGEMENT", help="re-render an (edited) arrangement.json; no re-arranging")
    x.add_argument("--template", default="club", help="club, dj-tool, short, or a path to a .toml (templates/)")
    x.add_argument("--tempo", type=float, help="output BPM (default: the instrumental's)")
    x.add_argument("--keep-half", action="store_true",
                   help="keep an instrumental tracked under 100 BPM as is (default: doubled, e.g. D&B 87 -> 174)")
    x.add_argument("--variations", type=int, default=1, help="number of different arrangements (default 1)")
    x.add_argument("--hq-stems", action="store_true", help="separate with htdemucs_ft (better, about 4x slower; needs its weights on disk)")
    x.add_argument("--seed", type=int, default=1, help="variations are fixed for a given seed")
    x.add_argument("--out", help="output folder (default runs/<run>/mashups/<id>-<template>-v<n>/)")
    ed = sub.add_parser("edit", help="block editor for a mashup folder (its arrangement.json), in the browser")
    ed.add_argument("folder", help="a mashup output folder (holds arrangement.json and blocks/)")
    ed.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: any free one)")
    ed.add_argument("--no-browser", action="store_true", help="print the address instead of opening the browser")
    st = sub.add_parser("app", help="Mashup Studio: briefs, runs, audition, mashups and the editor in one page")
    st.add_argument("--port", type=int, default=8765, help="port on 127.0.0.1 (default 8765; a second start opens the running one)")
    st.add_argument("--no-browser", action="store_true", help="print the address instead of opening the browser")
    sc = sub.add_parser("scan", help="read music folders into the catalog: tags, else Beat This! tempo and a key estimate")
    sc.add_argument("folders", nargs="*", metavar="FOLDER", help="default: the settings' music_folders")
    sc.add_argument("--catalog", help="the CSV to write (default: the settings' catalog)")
    a = ap.parse_args(argv)
    if a.cmd == "scan":
        from .scan import scan
        settings = config.load()
        scan(a.folders or settings["music_folders"], a.catalog or settings["catalog"],
             skip=[settings["export_folder"], settings["cache_folder"]])     # the Studio's own output
    elif a.cmd == "app":
        from .app import serve
        config.seed_data()
        serve(a.port, not a.no_browser)
    elif a.cmd == "edit":
        from .edit import serve
        serve(a.folder, a.port, not a.no_browser)
    elif a.cmd == "mashup":
        from .engine import cli as mashup
        mashup(a)
    elif a.cmd == "export":
        _export(a.run if os.path.isdir(a.run) else os.path.join(RUNS, a.run), a.picks, a.dest)
    elif a.cmd == "find":
        _find(a)
    elif a.cmd == "check":
        _check(a.run if os.path.isdir(a.run) else os.path.join(RUNS, a.run), a.top, a.redo)
    else:
        run = _find(a)
        _check(run, a.top)


if __name__ == "__main__":
    main()
