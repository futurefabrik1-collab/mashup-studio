"""`mf scan [FOLDER ...]`: write the catalog CSV from music folders.

Tags (artist, title, album, genre, year, BPM, key) and the duration come from ffprobe, which comes with ffmpeg. A
file without a BPM tag gets its tempo from Beat This! (deep.beats, which also caches the beats for the audio check);
one without a key tag, a key estimate from its pitch-class profile. The CSV has the catalog's columns
(catalog.Catalog reads it). Incremental: <catalog>.scan.json keeps each file's size and modification time, and a
file with neither changed is not read again; files that are gone leave the catalog. Progress lines
"scan: n/N artist - title" are what the Studio shows while it runs.

Never writes over a CSV it did not write itself (no .scan.json beside it), and refuses a music folder that is not
there (an unplugged drive would otherwise empty the catalog)."""
import csv, json, os, subprocess
import numpy as np

from .compat import NAMES, camelot, parse_key

AUDIO = (".mp3", ".m4a", ".aiff", ".aif", ".wav", ".flac")
COLUMNS = ["artist", "title", "album", "genre", "year", "bpm", "bpm_src", "key", "dur", "fmt", "kbps", "size_mb", "source",
           "online", "status", "in_rekordbox", "rb_plays", "rb_playlists", "tf_catalog", "energy", "dance", "valence", "mood",
           "key_camelot", "kind", "sources", "copies", "new_since_oct", "path"]
# Krumhansl-Kessler key profiles, C major and C minor.
MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
KEY_SECONDS = 120                                   # of the middle of a track, for the key estimate


def files(folders, skip=()):
    """Every audio file under the folders, sorted; hidden ones (._ resource forks) and the folders in skip (the
    Studio's own export and cache folders, which may sit inside a music folder) left out."""
    skip, out = {os.path.realpath(s) for s in skip}, []

    def unreadable(e):                                   # its tracks would leave the catalog as "gone"
        raise SystemExit(f"{e.filename} cannot be read ({e.strerror}): the catalog was not changed")
    for top in folders:
        for d, subs, names in os.walk(top, onerror=unreadable):
            if os.path.realpath(d) in skip:
                subs[:] = []
                continue
            subs[:] = [s for s in subs if not s.startswith(".")]
            out += [os.path.join(d, n) for n in names if not n.startswith(".") and n.lower().endswith(AUDIO)]
    return sorted(set(out))


def probe(path):
    """(tags with lower-case names, duration in seconds, bit rate in bit/s), read by ffprobe."""
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration,bit_rate:format_tags:stream_tags",
                        "-of", "json", path], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise ValueError(f"ffprobe could not read it: {r.stderr.strip()[:200]}")
    j = json.loads(r.stdout or "{}")
    fmt, tags = j.get("format", {}), {}
    for s in j.get("streams", []):                     # some containers keep their tags on the stream
        tags.update({k.lower(): v for k, v in (s.get("tags") or {}).items()})
    tags.update({k.lower(): v for k, v in (fmt.get("tags") or {}).items()})
    return tags, float(fmt.get("duration") or 0), float(fmt.get("bit_rate") or 0)


def tag_bpm(tags):
    for k in ("bpm", "tbpm", "tmpo", "tempo"):
        try:
            v = float(str(tags.get(k, "")).replace(",", ".").split()[0])
        except (ValueError, IndexError):
            continue
        if 40 <= v <= 300:
            return round(v, 2)
    return None


def tag_key(tags):
    for k in ("initialkey", "key", "tkey"):
        v = str(tags.get(k, "")).strip()
        if v and parse_key(v) is not None:
            return v
    return None


def key_of(path):
    """A key estimate in Camelot notation ("8A" is A minor): the chromagram of up to KEY_SECONDS from the middle,
    against the major and minor profile at all twelve tonics."""
    import librosa
    from . import deep
    y = deep.decode(path).mean(0)
    mid, half = y.size // 2, KEY_SECONDS * deep.SR // 2
    chroma = librosa.feature.chroma_cqt(y=y[max(0, mid - half):mid + half], sr=deep.SR, hop_length=4096).mean(axis=1)
    _, tonic, minor = max((np.corrcoef(chroma, np.roll(p, t))[0, 1], t, m) for t in range(12) for m, p in ((0, MAJOR), (1, MINOR)))
    return camelot(parse_key(NAMES[tonic] + "m" * minor))


def bpm_of(path):
    from . import deep
    bpm = deep.beats(path)["bpm"]
    if not bpm:
        raise ValueError("Beat This! found no beat")
    return round(float(bpm), 2)


def row(path, st, bpm_fn, key_fn):
    """The catalog row of one file (st: its os.stat)."""
    tags, dur, rate = probe(path)
    bpm, src = tag_bpm(tags), "tag"
    if bpm is None:
        bpm, src = bpm_fn(path), "beat_this"
    key = tag_key(tags) or key_fn(path)
    artist, title = tags.get("artist", "").strip(), tags.get("title", "").strip()
    if not title:                                       # untagged: "Artist - Title.mp3", else the file name
        stem = os.path.splitext(os.path.basename(path))[0]
        artist, title = (s.strip() for s in stem.split(" - ", 1)) if " - " in stem else (artist, stem)
    r = dict.fromkeys(COLUMNS, "")
    r.update(artist=artist, title=title, album=tags.get("album", "").strip(), genre=tags.get("genre", "").strip(),
             year=str(tags.get("date") or tags.get("year") or "")[:4], bpm=bpm, bpm_src=src, key=key, dur=round(dur, 1),
             fmt=os.path.splitext(path)[1].lstrip(".").lower(), kbps=round(rate / 1000) if rate else "",
             size_mb=round(st.st_size / 1e6, 2), source="scan", online="True", in_rekordbox="False", rb_plays=0,
             key_camelot=camelot(parse_key(key)), kind="track", path=path)
    return r


def _write(catalog, rows, seen):
    """The catalog and its .scan.json, each whole or not at all."""
    os.makedirs(os.path.dirname(os.path.abspath(catalog)), exist_ok=True)
    with open(catalog + ".tmp", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(catalog + ".tmp", catalog)
    with open(catalog + ".scan.json.tmp", "w", encoding="utf-8") as f:
        json.dump(seen, f)
    os.replace(catalog + ".scan.json.tmp", catalog + ".scan.json")


def scan(folders, catalog, bpm_fn=bpm_of, key_fn=key_of, log=print, skip=()):
    """Write catalog from the audio under folders, minus the folders in skip: {"read", "kept", "gone", "failed"}
    counts. Ctrl-C (the Studio's Cancel) writes what was read so far, and keeps the earlier rows of the files not
    reached."""
    folders = [os.path.abspath(os.path.expanduser(f)) for f in folders if f.strip()]    # the Studio runs elsewhere
    if not folders:
        raise SystemExit("no music folders: give one (mf scan FOLDER) or set music_folders in the settings")
    for f in folders:
        if not os.path.isdir(f):
            raise SystemExit(f"{f} is not there: connect its drive, or leave it out (the catalog was not changed)")
        if os.path.realpath(f) in map(os.path.realpath, skip):
            raise SystemExit(f"{f} is the export or cache folder, not a music folder (the catalog was not changed)")
    state_file = catalog + ".scan.json"
    if os.path.exists(catalog) and not os.path.exists(state_file):
        raise SystemExit(f"{catalog} was not written by mf scan, so it is left as it is: scan into another file (--catalog)")
    old, state = {}, {}
    if os.path.exists(catalog):
        with open(catalog, encoding="utf-8-sig", newline="") as f:
            old = {r["path"]: r for r in csv.DictReader(f)}
        with open(state_file, encoding="utf-8") as f:
            state = json.load(f)
    found = files(folders, skip)
    for f in folders:                                    # an empty mount point left by an unplugged drive
        inside = lambda p: p.startswith(f.rstrip("/") + "/")
        if not any(map(inside, found)) and any(map(inside, old)):
            raise SystemExit(f"{f} has no audio files now, but the catalog has tracks there: is its drive connected?"
                             " (the catalog was not changed)")
    rows, seen, counts, n = [], {}, {"read": 0, "kept": 0, "gone": 0, "failed": 0}, 0
    try:
        for n, path in enumerate(found, 1):
            try:
                st = os.stat(path)                       # a broken link, or a file gone meanwhile: skipped below
                sig = [st.st_size, st.st_mtime_ns]
                if state.get(path) == sig and path in old:
                    rows.append(old[path]); seen[path] = sig; counts["kept"] += 1
                    continue
                r = row(path, st, bpm_fn, key_fn)
            except Exception as e:                       # one unreadable file must not stop the scan
                log(f"scan: {n}/{len(found)} skipped {path}: {e}")
                counts["failed"] += 1
                continue
            rows.append(r); seen[path] = sig; counts["read"] += 1
            log(f"scan: {n}/{len(found)} {r['artist'] or '?'} - {r['title']}")
    except KeyboardInterrupt:
        rest = [p for p in found[max(n - 1, 0):] if p in old and p not in seen]      # from the one it was reading
        rows += [old[p] for p in rest]
        seen.update({p: state[p] for p in rest if p in state})
        _write(catalog, sorted(rows, key=lambda r: r["path"]), seen)
        log(f"scan: stopped at {n}/{len(found)}: {catalog} keeps what was read")
        raise
    counts["gone"] = len(set(old) - set(seen))
    _write(catalog, rows, seen)
    log(f"scan: done: {counts['read']} read, {counts['kept']} unchanged, {counts['gone']} gone, {counts['failed']} failed"
        f" -> {catalog}")
    return counts
