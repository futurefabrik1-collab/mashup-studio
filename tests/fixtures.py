"""Synthetic stems for the engine tests: clicks on every beat (drums), a 55 Hz sine (bass), a pad (other)
and tone bursts (vocal) at known bars and tempos. No Demucs, no Beat This!: the beat dict is built from
the same beat times the clicks are placed on.

A layout is a list of (bars, "DBO"): one letter per stem (drums, bass, other), upper case loud,
lower case quiet, "-" off. Vocal phrases are (start_bar, bars, freq_hz, amplitude), 0-based bars."""
import atexit, os, shutil, tempfile

import numpy as np

SR = 44100


def tmpdir(prefix):
    """A temp folder removed when the test run ends (renders are ~0.4 GB each)."""
    d = tempfile.mkdtemp(prefix=prefix)
    atexit.register(shutil.rmtree, d, True)
    return d
T0 = 0.1                                    # first downbeat, seconds

# The layout the B tests are built on, with the section labels it should get.
B_LAYOUT = [(8, "d-O"), (8, "D-O"), (16, "DBO"), (8, "--O"), (16, "DBO"), (8, "d-o")]
B_LABELS = ["intro", "build", "drop", "break", "drop", "outro"]
# A: verses at 16 and 48 (two melodies), the hook three times (32 is the loudest), an adlib at 90.
A_VOCAL = [(16, 8, 330.0, 0.5), (32, 8, 440.0, 0.6), (48, 8, 392.0, 0.5), (64, 8, 440.0, 0.5),
           (80, 8, 440.0, 0.5), (90, 2, 587.0, 0.5)]


def _tone(f, n, t0=0):
    return np.sin(2 * np.pi * f * (np.arange(n) + t0) / SR)


def track(bpm, layout, vocal=(), wobble_ms=0.0):
    """(4, 2, N) float32 stems in deep.STEMS order, and a beat dict like deep.beats() returns.
    wobble_ms: the beats drift around the straight grid by up to this much (a live player), over 16 bars."""
    spb = 60.0 / bpm
    bars = sum(n for n, _ in layout)
    i = np.arange(bars * 4 + 1)                         # one extra beat closes the last bar
    beats = T0 + i * spb + wobble_ms / 1000 * np.sin(2 * np.pi * i / 64)
    N = int((beats[-1] + 0.5) * SR)
    st = np.zeros((4, N), np.float32)
    flags = [f for n, f in layout for _ in range(n)]
    nc = int(0.03 * SR)
    click = (_tone(2000, nc) * np.exp(-np.arange(nc) / (0.005 * SR))).astype(np.float32)
    for k, (d, b, o) in enumerate(flags):
        s0, s1 = int(round(beats[4 * k] * SR)), int(round(beats[4 * k + 4] * SR))
        if d != "-":
            for i in range(4):
                s = int(round(beats[4 * k + i] * SR))
                st[0, s:s + nc] += (0.8 if i == 0 else 0.5) * (1.0 if d == "D" else 0.25) * click
        if b != "-":
            st[1, s0:s1] += (0.5 if b == "B" else 0.15) * _tone(55, s1 - s0, s0)
        if o != "-":
            pad = sum(_tone(f, s1 - s0, s0) for f in (220.0, 277.18, 329.63)) / 3
            st[2, s0:s1] += (0.15 if o == "O" else 0.05) * pad
    nb = int(min(0.15, 0.6 * spb) * SR)
    env = np.minimum(1, np.arange(nb) / (0.002 * SR)) * np.minimum(1, (nb - np.arange(nb)) / (0.01 * SR))
    for start, n, f, amp in vocal:
        for i in range(4 * start, 4 * (start + n)):
            s = int(round(beats[i] * SR))
            st[3, s:s + nb] += amp * env * _tone(f, nb)
    bt = {"beats": beats.tolist(), "downbeats": beats[::4].tolist(), "bpm": float(bpm), "beats_per_bar": 4}
    return np.repeat(st[:, None, :], 2, axis=1), bt


def b_track(bpm=126.0):
    return track(bpm, B_LAYOUT)


def a_track(bpm=120.0, bars=96, vocal=A_VOCAL):
    return track(bpm, [(bars, "DBo")], vocal)


# A tiny music catalog in catalog.py's CSV format, for the catalog match (`mf find`, the app's live count): one song
# in two versions, a few other vocals, and instrumentals around 126 and 174 BPM. (artist, title, bpm, camelot key,
# genre, year)
CATALOG_ROWS = [
    ("Duran Duran", "A View To A Kill", 128.0, "8A", "pop", "1985"),
    ("Duran Duran", "A View To A Kill (Extended Mix)", 128.0, "8A", "pop", "1985"),
    ("Duran Duran", "Hungry Like The Wolf", 126.0, "9A", "pop", "1982"),
    ("duran duran", "hungry like the wolf (night version)", 126.0, "9A", "pop", "1982"),
    ("Kate Bush", "Running Up That Hill", 108.0, "11A", "pop", "1985"),
    ("Yazoo", "Only You", 124.0, "8B", "synth pop", "1982"),
    ("Soul II Soul", "Back To Life", 100.0, "7A", "rnb", "1989"),
] + [(f"Beat Maker {n}", f"Groove {n} (Instrumental)", bpm, key, "house", "2016")
     for n, (bpm, key) in enumerate([(126.0, "8A"), (125.0, "9A"), (127.0, "8B"), (128.0, "7A"), (124.0, "8A"), (126.0, "10A"),
                                     (130.0, "8A"), (122.0, "9A"), (174.0, "8A"), (172.0, "9A"), (87.0, "8A"), (64.0, "8A")], 1)]
CATALOG_COLUMNS = ["artist", "title", "album", "genre", "year", "bpm", "bpm_src", "key", "dur", "fmt", "kbps", "size_mb", "source",
                   "online", "status", "in_rekordbox", "rb_plays", "rb_playlists", "tf_catalog", "energy", "dance", "valence",
                   "mood", "key_camelot", "kind", "sources", "copies", "new_since_oct", "path"]


def catalog_csv(folder, rows=CATALOG_ROWS):
    """Write folder/catalog.csv (catalog.CATALOG's format) and an empty file per track, since the catalog keeps only
    tracks that are on disk. Returns the CSV's path."""
    import csv, os
    os.makedirs(os.path.join(folder, "library"), exist_ok=True)
    f = os.path.join(folder, "catalog.csv")
    with open(f, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CATALOG_COLUMNS)
        w.writeheader()
        for artist, title, bpm, key, genre, year in rows:
            path = os.path.join(folder, "library", f"{artist} - {title}.mp3")
            open(path, "a").close()
            w.writerow({"artist": artist, "title": title, "album": "", "genre": genre, "year": year, "bpm": bpm, "bpm_src": "tag",
                        "key": "", "dur": 240, "key_camelot": key, "kind": "track", "in_rekordbox": "False", "rb_plays": 0,
                        "energy": 0.7, "dance": 0.8, "path": path})
    return f


def fixture_catalog(folder):
    """A context in which catalog.Catalog() reads a fixture catalog written to folder (catalog_csv), nothing else."""
    import contextlib
    from unittest import mock
    from mashup_finder import catalog
    f = os.path.join(folder, "catalog.csv")
    if not os.path.exists(f):
        catalog_csv(folder)
    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch.object(catalog, "CATALOG", f))
    stack.enter_context(mock.patch.object(catalog, "SUPER_MASSIVE", []))
    return stack
