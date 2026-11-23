"""Load the music catalog into flat numpy arrays for pair search.

The catalog is the CSV the settings name (config.py; `mf scan` writes one from music folders). Optional extra
sources in super_massive.json's format add vocal presence, key confidence and tuning by path.
Read-only: nothing here writes to the catalog or the drives.
"""
import csv, json, os, re
import numpy as np
from . import config
from .compat import parse_key

_settings = config.load()
CATALOG = _settings["catalog"]
SUPER_MASSIVE = _settings["extra_sources"]

ACAPELLA = re.compile(r"a\s?cap+el+a|acapella|vocals? only|\bvox only\b", re.I)
INSTRUMENTAL = re.compile(r"\binstrumental\b|\binst\b|\bdub\b|\bbeats? only\b", re.I)
CLUB = re.compile(r"original mix|extended|\bdub\b|\bremix\b|\bedit\b|\bvip\b|records\]|\b\d{1,2}[AB] - \d{2,3}\b", re.I)
JUNK_ARTIST = re.compile(r"\.(com|es|net|org|ru|de|co|info)\b|www\.|unknown|various", re.I)
VERSION = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]|\s+-\s+.*(mix|edit|remix|version|dub|instrumental).*$", re.I)


def base_title(t):
    return re.sub(r"[^a-z0-9]+", " ", VERSION.sub("", t or "").lower()).strip()


def _super_massive():
    out = {}
    for p in SUPER_MASSIVE:
        if not os.path.exists(p):
            continue
        for e in json.load(open(p)):
            f = e.get("library_file")
            if not f or f in out:
                continue
            d = e.get("data") or {}
            c, h = d.get("content") or {}, d.get("harmony") or {}
            out[f] = {"has_vocals": c.get("has_vocals"), "voice_presence": c.get("voice_presence"),
                      "key_conf": h.get("key_confidence"), "tuning": h.get("tuning_cents"),
                      "genre_sm": e.get("genre")}
    return out


def _year(tag, fallback):
    """Release year from the tag, else a 19xx/20xx in brackets in the name or path, else None."""
    m = re.search(r"\b(19[5-9]\d|20[0-2]\d)\b", tag or "") or re.search(r"[\(\[](19[5-9]\d|20[0-2]\d)[\)\]]", fallback)
    return int(m.group(1)) if m else None


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


class Catalog:
    """Columnar view: self.rows (list of dicts) plus aligned numpy arrays."""

    def __init__(self, kinds=("track", "own-project"), require_online=True, min_dur=90):
        sm = _super_massive()
        rows = []
        for r in csv.DictReader(open(CATALOG, encoding="utf-8-sig")):
            if r["kind"] not in kinds:
                continue
            k = parse_key(r["key_camelot"]) if parse_key(r["key_camelot"]) is not None else parse_key(r["key"])
            bpm = _f(r["bpm"])
            if k is None or not (60 <= bpm <= 200) or _f(r["dur"]) < min_dur:
                continue
            if require_online and not os.path.exists(r["path"]):
                continue
            x = sm.get(r["path"], {})
            name = f"{r['artist']} {r['title']}"
            rows.append({
                "artist": r["artist"].strip(), "title": r["title"].strip(), "genre": (r["genre"] or x.get("genre_sm") or "").strip(),
                "bpm": bpm, "bpm_src": r["bpm_src"], "key": k, "dur": _f(r["dur"]), "path": r["path"],
                "rb_plays": int(_f(r["rb_plays"])), "in_rekordbox": r["in_rekordbox"] == "True",
                "energy": _f(r["energy"], np.nan), "dance": _f(r["dance"], 0.6), "has_vocals": x.get("has_vocals"),
                "voice_presence": x.get("voice_presence"), "key_conf": x.get("key_conf"), "tuning": x.get("tuning"),
                "acapella": bool(ACAPELLA.search(name)), "club": bool(CLUB.search(name)), "instrumental": bool(INSTRUMENTAL.search(name)),
                "base": base_title(r["title"]), "year": _year(r["year"], name + " " + r["path"]),
                "text": f"{name} {r['genre']} {r['album']} {r['path']}".lower(),
            })
        self.rows, self.source = rows, CATALOG              # source: the csv read (playlists find their tracks through it)
        n = len(rows)
        self.bpm = np.array([r["bpm"] for r in rows])
        self.key = np.array([r["key"] for r in rows], dtype=np.int16)
        self.artist = np.array([r["artist"].lower() for r in rows])
        self.base = np.array([r["base"] for r in rows])
        self.genre = np.array([r["genre"].lower() for r in rows])
        self.known = np.array([bool(r["artist"]) and not r["artist"][:2].isdigit() and not JUNK_ARTIST.search(r["artist"])
                               for r in rows])
        self.familiar = np.array([r["in_rekordbox"] or r["rb_plays"] > 0 for r in rows])
        self.club = np.array([r["club"] for r in rows])
        self.acapella = np.array([r["acapella"] for r in rows])
        self.instrumental = np.array([r["instrumental"] for r in rows])
        # Vocal source candidates: known acapellas, or tracks an extra source flagged as vocal and not titled instrumental.
        self.vocal_ok = np.array([r["acapella"] or (r["has_vocals"] is not False and not r["instrumental"]) for r in rows])
        self.dance = np.array([r["dance"] for r in rows])
        self.key_conf = np.array([_f(r["key_conf"], 0.5) for r in rows])
        self.tuning = np.array([_f(r["tuning"], 0.0) for r in rows])
        self.n = n

    def search(self, q):
        q = q.lower()
        return [i for i, r in enumerate(self.rows) if q in f"{r['artist']} - {r['title']}".lower()]

    def label(self, i):
        r = self.rows[i]
        return f"{r['artist'] or '?'} - {r['title']}"
