"""Mashup engine: one path from a found pair to rendered mashups (analyse -> arrange -> arrangement.json ->
render), and the glue for `mf mashup`. See docs/ENGINE-PLAN.md."""
import csv, glob, html, json, os, re, tempfile

from . import arrange, config, deep, render, trackmap

RUNS = os.path.join(config.DATA, "runs")


def make(sa, ba, sb, bb, shift, out_dir_for, template="club", n=1, seed=1, out_bpm=None, pair=None, log=print,
         stems=deep.SEPARATOR):
    """Arrange and render n variations. out_dir_for(v) gives variation v's folder (v from 1). The warped
    sources (blocks/) are written once, into the first folder, and hard-linked into the others. stems: the
    Demucs model sa and sb came from, recorded in each arrangement.json ("htdemucs" or "htdemucs_ft")."""
    ma, mb = trackmap.track_map(sa, ba), trackmap.track_map(sb, bb, phrases=False)
    m = arrange.tempo_mult(ma["bpm"], mb["bpm"])
    out_bpm = float(out_bpm or mb["bpm"])
    if n < 1:
        raise ValueError("n (variations) must be at least 1")
    arrs = arrange.variations(ma, mb, arrange.load_template(template), m, shift, n, seed)
    dirs = [os.path.abspath(out_dir_for(v)) for v in range(1, len(arrs) + 1)]
    log(f"  warping the sources to {out_bpm:.2f} BPM (blocks/)")
    sources = render.build_sources(sa, ma, sb, mb, m, shift, out_bpm, dirs[0])
    done = []
    for v, (arr, d) in enumerate(zip(arrs, dirs), 1):
        render.link_sources(dirs[0], d)
        arr.update(output_bpm=out_bpm, sources=sources, pair=pair or {}, stems=stems)
        path = os.path.join(d, "arrangement.json")
        render.write_json(path, arr)
        log(f"  v{v}: {arr['bars']} bars{' (same as v%d: no other material)' % (arr['duplicate_of'] + 1) if 'duplicate_of' in arr else ''}")
        r = render.render(path, log=log)
        done.append({"dir": d, "arrangement": arr, "lufs": r["lufs"], "true_peak": r["true_peak"]})
    return done


def _pair_from_run(run, pid):
    """{'vocal', 'inst', 'shift', 'meta', 'base', 'stem'} for pair pid of a checked run."""
    with open(os.path.join(run, "results.json")) as f:
        p = {q["id"]: q for q in json.load(f)}.get(pid)
    if p is None:
        raise SystemExit(f"no pair {pid} in {os.path.basename(run)}/results.json")
    d = p.get("deep") or {}
    if not d.get("ok"):
        raise SystemExit(f"pair {pid} has no audio check result: {d.get('why', 'not checked')}")
    return {"vocal": p["vocal_path"], "inst": p["inst_path"], "shift": d["shift_audio"] + p.get("tuning_fix", 0.0),
            "meta": {"run": os.path.basename(run), "id": pid, "vocal": p["vocal_label"], "inst": p["inst_label"]},
            "base": os.path.join(run, "mashups"), "stem": f"{pid:03d}-"}


def _pair_from_folder(folder, log=print):
    """The same record for an exported pair folder: found through MASHUP-FINDER.csv and its run; if the run
    is gone, the originals in the folder are analysed again (output then goes to runs/_folders/)."""
    folder = os.path.abspath(os.path.expanduser(folder))
    row = None
    for root in (os.path.dirname(folder), os.path.dirname(os.path.dirname(folder))):
        f = os.path.join(root, "MASHUP-FINDER.csv")
        if os.path.exists(f):
            with open(f, encoding="utf-8", newline="") as fh:
                row = next((r for r in csv.DictReader(fh) if r["folder"] == os.path.relpath(folder, root)), None)
        if row:
            break
    if row is None:
        raise SystemExit(f"{folder} is not listed in a MASHUP-FINDER.csv above it")
    run = os.path.join(RUNS, row["run"])
    if os.path.exists(os.path.join(run, "results.json")):
        with open(os.path.join(run, "results.json")) as f:
            hit = next((p for p in json.load(f) if p["vocal_path"] == row["vocal_src"] and p["inst_path"] == row["inst_src"]), None)
        if hit:
            return _pair_from_run(run, hit["id"])
    voc = glob.glob(os.path.join(glob.escape(folder), "VOCAL - *"))
    inst = glob.glob(os.path.join(glob.escape(folder), "INST - *"))
    if not voc or not inst:
        raise SystemExit(f"run {row['run']} is gone and {folder} has no 'VOCAL - ' / 'INST - ' originals")
    log(f"run {row['run']} is gone: analysing the folder's originals again")
    with tempfile.TemporaryDirectory() as td:
        d = deep.analyse_pair(voc[0], inst[0], {"id": 0, "shift": int(float(row["shift_st"]))}, td, log)
    if not d.get("ok"):
        raise SystemExit(f"audio check failed: {d.get('why')}")
    return {"vocal": voc[0], "inst": inst[0], "shift": d["shift_audio"],
            "meta": {"folder": folder, "vocal": row["vocal"], "inst": row["inst"]},
            "base": os.path.join(RUNS, "_folders", _safe(os.path.basename(folder))), "stem": ""}


def write_page(path, title, done):
    """Audition page for variations, in audition.py's style (its CSS is reused)."""
    from . import audition
    css = re.search(r"<style>(.*?)</style>", audition.PAGE, re.S).group(1)
    cards = []
    for i, d in enumerate(done, 1):
        a = d["arrangement"]
        secs = " · ".join(f"{s['name']} {s['bars']}" + (f" ({s['vocal']['role']} @A{s['vocal']['phrase'] + 1})" if s.get("vocal") else "")
                          for s in a["sections"])
        mp3 = os.path.relpath(os.path.join(d["dir"], "mashup.mp3"), os.path.dirname(path))
        cards.append(f'<div class="card"><div class="rk">v{i}</div><div class="t">{html.escape(os.path.basename(d["dir"]))}</div>'
                     f'<div class="m">{html.escape(secs)}<br>{a["output_bpm"]:.1f} BPM · {d["lufs"]} LUFS · '
                     f'{d["true_peak"]} dBTP{" · same as v%d" % (a["duplicate_of"] + 1) if "duplicate_of" in a else ""}</div>'
                     f'<div><audio controls preload="none" src="{html.escape(mp3)}"></audio></div></div>')
    with open(path, "w") as f:
        f.write(f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
                f'content="width=device-width,initial-scale=1"><title>Mashups: {html.escape(title)}</title>'
                f'<style>{css}</style></head><body><header><h1>Mashup variations</h1></header><main><p class="sub">{html.escape(title)}</p>'
                f'{"".join(cards)}</main></body></html>')
    return path


def double_grid(bt):
    """A beat grid tracked at half speed (D&B at 87 instead of 174): add the off-beats, double the BPM.
    Downbeats stay, so bars still start on them; bars become 4 of the new beats."""
    b = bt["beats"]
    mids = [round((x + y) / 2, 3) for x, y in zip(b, b[1:])]
    beats = sorted(b + mids)
    return {**bt, "beats": beats, "bpm": round(bt["bpm"] * 2, 2), "doubled": True}


HALF_BELOW = 100  # instrumentals tracked below this are read as half-time (--keep-half to keep them)


def _safe(s):
    return re.sub(r"[^A-Za-z0-9._ -]+", "-", s).strip()[:80]


def cli(a, log=print):
    """`mf mashup` (see cli.py for the arguments)."""
    if a.variations < 1:
        raise SystemExit("mf mashup: --variations must be at least 1")
    if a.render:
        r = render.render(a.render, a.out, log)
        log(f"rendered {os.path.abspath(a.out or os.path.dirname(a.render))}: {r['lufs']} LUFS, {r['true_peak']} dBTP")
        return
    if a.folder:
        p = _pair_from_folder(a.folder, log)
    elif a.run is not None and a.pair_id is not None:
        p = _pair_from_run(a.run if os.path.isdir(a.run) else os.path.join(RUNS, a.run), a.pair_id)
    else:
        raise SystemExit("mf mashup: give RUN PAIR_ID, --folder DIR, or --render arrangement.json")
    stem = p["stem"] + _safe(os.path.splitext(os.path.basename(a.template))[0])
    if a.out:
        out_for = (lambda v: a.out) if a.variations == 1 else (lambda v: os.path.join(a.out, f"v{v}"))
    else:
        out_for = lambda v: os.path.join(p["base"], f"{stem}-v{v}")
    log(f"{p['meta']['vocal']}  >>  {p['meta']['inst']}")
    hq = getattr(a, "hq_stems", False)                      # the final render's stems: htdemucs_ft if it is here
    sa, sb = deep.separate(p["vocal"], log, hq=hq), deep.separate(p["inst"], log, hq=hq)
    ba, bb = deep.beats(p["vocal"], sa.sum(0)), deep.beats(p["inst"], sb.sum(0))
    if ba["bpm"] <= 0 or bb["bpm"] <= 0:
        raise SystemExit("no beat grid on one side")
    if bb["bpm"] < HALF_BELOW and not getattr(a, "keep_half", False):
        bb = double_grid(bb)
        log(f"  instrumental tracked at half speed: using {bb['bpm']:.2f} BPM (--keep-half to keep {bb['bpm'] / 2:.2f})")
    if a.tempo and abs(a.tempo / bb["bpm"] - 1) > 0.15:
        log(f"note: --tempo {a.tempo} is {a.tempo / bb['bpm'] * 100 - 100:+.0f}% from the instrumental's {bb['bpm']:.1f} BPM")
    stems = deep.separator(hq, log=lambda *_: None)[0]      # what separate() used: htdemucs if the ft weights are gone
    done = make(sa, ba, sb, bb, p["shift"], out_for, a.template, a.variations, a.seed, a.tempo, p["meta"], log, stems)
    for d in done:
        log(f"  {d['dir']}  ({d['lufs']} LUFS, {d['true_peak']} dBTP)")
    if len(done) > 1:
        page = os.path.join(os.path.dirname(done[0]["dir"]), f"{stem}-variations.html")
        log("  page: " + write_page(page, f"{p['meta']['vocal']} >> {p['meta']['inst']}", done))
