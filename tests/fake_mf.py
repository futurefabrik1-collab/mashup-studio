"""`mf` with the audio work faked, for tests/test_app.py and scripted browser sessions of `mf app`:

    python tests/fake_mf.py ROOT <mf arguments>

ROOT stands in for the repo: runs/ is written there, and the two "library" tracks live in ROOT/tracks. The find
step lists their two pairs (A's vocal over B, which checks out, and B's over A, which fails); with a top above 2,
repeated up to it and 3 past it (for live audition and Prepare); Demucs and Beat This!
return the synthetic stems of tests/fixtures.py, and the preview is a few bars of them. Everything else is the real
CLI: check's ranking and audition page, mashup, export. Nothing outside ROOT is read or written."""
import datetime, json, os, subprocess, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import soundfile as sf                                                   # noqa: E402

from mashup_finder import cli, criteria, deep, engine                    # noqa: E402
from tests import fixtures as fx                                         # noqa: E402

ROOT = os.path.abspath(sys.argv[1])
TRACKS = os.path.join(ROOT, "tracks")
A, B = os.path.join(TRACKS, "Artist A - Vocal Song.wav"), os.path.join(TRACKS, "Artist B - Bass Track.wav")
MAKE = {A: lambda: fx.a_track(120.0), B: lambda: fx.b_track(126.0)}
_stems = {}


def _track(path):
    if path not in _stems:
        _stems[path] = MAKE[path]()
    return _stems[path]


def separate(path, log=print, hq=False):
    log(f"    separating {os.path.basename(path)} (fixture)")
    return _track(path)[0]


def beats(path, y=None):
    return _track(path)[1]


def find(a, log=print):
    brief = criteria.load(a.brief) if a.brief else {}
    a.top = getattr(a, "top", None) or brief.get("match", {}).get("top", 12)
    name = a.name or brief.get("name") or "fixture"
    run = os.path.join(cli.RUNS, datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + name)
    os.makedirs(run, exist_ok=True)
    pair = lambda i, v, n, vl, il, vb, ib, score: {
        "id": i, "vocal": v, "inst": n, "score": score, "stretch": ib / vb - 1, "shift": 2, "relation": "fifth",
        "tempo_mult": 1.0, "vocal_label": vl, "inst_label": il, "vocal_path": v, "inst_path": n, "vocal_bpm": vb,
        "inst_bpm": ib, "vocal_key": "C 8B", "inst_key": "D 10B", "tuning_fix": 0.0}
    P = [pair(0, A, B, "Artist A - Vocal Song", "Artist B - Bass Track", 120.0, 126.0, 80.0),
         pair(1, B, A, "Artist B - Bass Track", "Artist A - Vocal Song", 126.0, 120.0, 60.0)]
    for i in range(2, a.top + 3 if a.top > 2 else 2):                     # a bigger top: more of both, and 3 past it
        P.append({**P[i % 2], "id": i, "score": P[i % 2]["score"] - i, "inst_bpm": 126.0 + i % 3})
    with open(os.path.join(run, "shortlist.json"), "w") as f:
        json.dump({"args": vars(a), "brief": brief, "pairs": P}, f, indent=1)
    log(f"2 fixture tracks, {len(P)} pairs -> {run}")
    return run


def analyse_pair(a_path, b_path, pair, outdir, log=print, max_stretch=None):
    time.sleep(1)                                                         # long enough to be seen as a job
    if a_path != A:
        return {"ok": False, "why": "no vocal found in the first track"}
    os.makedirs(outdir, exist_ok=True)
    st, _ = fx.track(126.0, [(8, "DBO")], [(0, 8, 440.0, 0.5)])
    with tempfile.TemporaryDirectory() as td:
        wav = os.path.join(td, "p.wav")
        sf.write(wav, st.sum(0).T * 0.5, fx.SR)
        f = f"{pair['id']:03d}.mp3"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", wav, "-b:a", "128k", os.path.join(outdir, f)], check=True)
    return {"ok": True, "preview": f, "stretch_audio": 0.05, "shift_audio": 2, "tempo_mult": 1.0, "harm_z": 1.5,
            "harm_rank": 1, "vocal_active": 0.8, "vocal_share": 0.4, "drift_ms": 5, "warp": "linear", "bpm_a": 120.0,
            "bpm_b": 126.0, "bars": 8, "vocal_bar": 32, "inst_bar": 16}


os.makedirs(TRACKS, exist_ok=True)
for path in (A, B):
    if not os.path.exists(path):
        sf.write(path, _track(path)[0].sum(0).T * 0.5, fx.SR, subtype="PCM_16")
cli.RUNS = engine.RUNS = os.path.join(ROOT, "runs")
cli._find, cli._prune_cache = find, lambda *a, **k: None
deep.separate, deep.beats, deep.analyse_pair = separate, beats, analyse_pair

if __name__ == "__main__":
    cli.main(sys.argv[2:])
