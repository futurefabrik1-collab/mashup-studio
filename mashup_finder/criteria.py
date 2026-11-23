"""Briefs: saved search criteria for one gig or job, as a TOML file in briefs/.

Each side ([vocal] = the track giving the vocal, [inst] = the track giving the instrumental)
takes the same filters; every filter is optional and they combine with AND:

  tempo      = [140, 180]   # BPM zones this side must sit in, at half/double too (catalog BPMs often are)
  tempo_tol  = 4            # +- percent around each zone (default 4)
  years      = [1980, 1999] # release year range, inclusive
  unknown_year = false      # keep tracks with no year (default false when years is set)
  include    = ["jungle", "breaks"]   # any of these whole words in artist/title/genre/album/folder
  exclude    = ["neuro", "jump up"]   # none of these
  from       = ["~/path/list.m3u8"]   # only tracks listed in these playlists (m3u/m3u8/csv with a path column):
                                      # any copy of a track counts (see catalog_paths), so a rekordbox export works
  acapella_only = false

[match] takes: max_stretch (percent, default 8), per_track (default 2; or per_vocal / per_inst separately),
limit (default 40), top (default 12).

Top level, beside name: ssd_folder = "Club night 20 Nov" files the run's pairs in that folder of the export folder
(Export and Copy to export folder), instead of straight into the export folder.

[song] makes it a brief about one song, like `mf find --seed`: text = "a view to a kill" (artist and/or title words)
and role = "vocal" (the song gives the top line), "beat" (the song carries the mashup) or "either" (both ways, the
default). [vocal] and [inst] still filter both sides; per_vocal caps how many beats one vocal gets. --seed on the
command line replaces [song] (with --role, which alone does not).
"""
import csv, os, re, tomllib, unicodedata
import numpy as np

from . import catalog

ROLES = {"vocal": "vocal", "beat": "inst", "either": "any"}     # a [song]'s role -> mf find's --role


def load(path):
    with open(os.path.expanduser(path), "rb") as fh:
        b = tomllib.load(fh)
    b.setdefault("name", os.path.splitext(os.path.basename(path))[0])
    return b


def song(brief, seed=None, role="any"):
    """(seed, role) for a find: --seed and --role, else the brief's [song] text and role."""
    if seed or not brief.get("song"):
        return seed, role
    s = brief["song"]
    if not isinstance(s, dict) or not isinstance(s.get("text"), str) or not s["text"].strip():
        raise ValueError("[song] text must name the song (artist and/or title words)")
    if not isinstance(s.get("role", "either"), str) or s.get("role", "either") not in ROLES:
        raise ValueError(f'[song] role is one of {", ".join(ROLES)}')
    return s["text"], ROLES[s.get("role", "either")]


def _playlist_paths(f):
    """The file paths a playlist lists, in its order: an m3u/m3u8 (rekordbox exports these) or a csv with a path column."""
    f = os.path.expanduser(f)
    if not os.path.isfile(f):
        raise ValueError(f"playlist not found: {f}")
    if f.lower().endswith(".csv"):
        with open(f, encoding="utf-8-sig", newline="") as fh:
            return [r["path"] for r in csv.DictReader(fh) if r.get("path")]
    base, out = os.path.dirname(f), []
    with open(f, encoding="utf-8-sig", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line if os.path.isabs(line) else os.path.normpath(os.path.join(base, line)))
    return out


# Which catalog track a file is. The catalog keeps one "best copy" per track, so a playlist pointing at another copy
# of it still finds it.
def _nfc(p):
    return unicodedata.normalize("NFC", p or "")


def _norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(feat|ft|featuring)\b.*", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _track_key(r):
    a, t = _norm(r.get("artist")), _norm(r.get("title"))
    if a and t:
        return "at:" + a + "|" + t
    return "fn:" + _norm(re.sub(r"^\s*\d{1,3}[\s._-]+", "", os.path.splitext(os.path.basename(r["path"]))[0].lower()))


_PATHS = {}


def catalog_paths(unique=None, all_files=None):
    """{NFC file path: catalog path} for every file of a track the catalog keeps: its best copy (catalog-unique-
    tracks.csv) and every other copy (catalog-all-files.csv beside it). Read again when either file changes."""
    unique = unique or catalog.CATALOG
    all_files = all_files or os.path.join(os.path.dirname(unique), "catalog-all-files.csv")
    stamp = tuple(os.path.getmtime(f) if os.path.exists(f) else None for f in (unique, all_files))
    hit = _PATHS.get((unique, all_files))
    if hit and hit[0] == stamp:
        return hit[1]
    by_key, out = {}, {}
    with open(unique, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            by_key[_track_key(r)] = r["path"]
            out[_nfc(r["path"])] = r["path"]
    if os.path.exists(all_files):
        with open(all_files, encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                k = _track_key(r)
                if k in by_key:
                    out.setdefault(_nfc(r["path"]), by_key[k])
    _PATHS[(unique, all_files)] = (stamp, out)
    return out


LEFT_OUT = "in the catalog, but left out: no BPM or key, under 90 s, or its drive is not plugged in"


def playlist(cat, files):
    """files (a side's `from`) against cat: {"idx": catalog indices, "total": playlist entries, "found": entries
    found, "missing": [{"path", "why"}]}. An entry is found when it, or any copy of the same track, is in cat."""
    entries = list(dict.fromkeys(p for f in files for p in _playlist_paths(f)))
    to_cat = catalog_paths(getattr(cat, "source", None))
    at = getattr(cat, "_by_path", None)
    if at is None:
        at = cat._by_path = {_nfc(r["path"]): i for i, r in enumerate(cat.rows)}
    idx, missing = set(), []
    for p in entries:
        c = to_cat.get(_nfc(p))
        i = at.get(_nfc(c or p))
        if i is not None:
            idx.add(i)
        else:
            missing.append({"path": p, "why": LEFT_OUT if c else "not in the catalog"})
    return {"idx": sorted(idx), "total": len(entries), "found": len(entries) - len(missing), "missing": missing}


def _words(words):
    """Whole-word, case-insensitive match for any of `words` ("rave" must not hit "Travel")."""
    return re.compile("|".join(rf"(?<![a-z0-9]){re.escape(w.lower())}(?![a-z0-9])" for w in words))


def select(cat, side):
    """Indices of catalog tracks passing one side's filters, or None if the side has no filters."""
    if not side:
        return None
    keep = np.ones(cat.n, bool)
    if "tempo" in side:
        tol = side.get("tempo_tol", 4) / 100
        zone = np.zeros(cat.n, bool)
        for t in side["tempo"]:
            for m in (1, 0.5, 2):            # catalog BPMs are often half or double (D&B logged at 87)
                zone |= np.abs(cat.bpm / (t * m) - 1) <= tol
        keep &= zone
    if "years" in side:
        lo, hi = side["years"]
        yrs = np.array([r["year"] if r["year"] is not None else -1 for r in cat.rows])
        ok = (yrs >= lo) & (yrs <= hi)
        if side.get("unknown_year", False):
            ok |= yrs == -1
        keep &= ok
    text = [r["text"] for r in cat.rows]
    if side.get("include"):
        rx = _words(side["include"])
        keep &= np.array([bool(rx.search(t)) for t in text])
    if side.get("exclude"):
        rx = _words(side["exclude"])
        keep &= np.array([not rx.search(t) for t in text])
    if side.get("from"):
        listed = np.zeros(cat.n, bool)
        listed[playlist(cat, side["from"])["idx"]] = True
        keep &= listed
    if side.get("acapella_only"):
        keep &= cat.acapella
    return np.flatnonzero(keep)


def describe(cat, b):
    """One line per side: how many tracks pass, and for a side with playlists how many of their tracks were found."""
    out = []
    for name in ("vocal", "inst"):
        side = b.get(name)
        idx = select(cat, side)
        line = f"{name}: {'all ' + str(cat.n) if idx is None else len(idx)} tracks"
        if (side or {}).get("from"):
            p = playlist(cat, side["from"])
            line += f" (playlist: {p['found']} of {p['total']} found)"
        out.append(line)
    return " · ".join(out)
