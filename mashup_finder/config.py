"""Where Mashup Studio's files are: one TOML file, ~/Library/Application Support/Mashup Studio/config.toml, or the
file $MF_CONFIG names. The Studio's first-run setup page writes it; the modules that need a path read it once, when
they are imported (Restart in the page reads it again).

  music_folders     folders `mf scan` reads (a list)
  catalog           the catalog CSV that find, check and the Studio read (`mf scan` writes it; default: beside this file)
  export_folder     where Export and "Copy to export folder" file pair folders
  cache_folder      separated stems and beats (default: _cache in the program folder)
  extra_sources     optional analysis files in super_massive.json's format (vocal presence, key confidence)
  playlists_folder  optional: where the brief drafter looks for playlists to suggest
"""
import json, os, tomllib

SUPPORT = os.path.expanduser("~/Library/Application Support/Mashup Studio")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))          # the program folder (a clone of the repo)
LISTS = ("music_folders", "extra_sources")


def path():
    return os.path.expanduser(os.environ.get("MF_CONFIG") or os.path.join(SUPPORT, "config.toml"))


def exists():
    return os.path.isfile(path())


def defaults():
    return {"music_folders": [], "catalog": os.path.join(os.path.dirname(path()), "catalog.csv"),
            "export_folder": os.path.expanduser("~/Music/Mashup Studio"), "cache_folder": os.path.join(ROOT, "_cache"),
            "extra_sources": [], "playlists_folder": ""}


def load():
    """The settings, with the defaults for what the file leaves out (no file: all defaults). A broken file, a key it
    does not know or a value of the wrong type stops the program with a message that names the file."""
    cfg = defaults()
    if exists():
        try:
            with open(path(), "rb") as f:
                data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise SystemExit(f"{path()}: {e}")
        for k, v in data.items():
            if k not in cfg:
                raise SystemExit(f"{path()}: unknown setting {k!r} (known: {', '.join(cfg)})")
            ok = isinstance(v, list) and all(isinstance(x, str) for x in v) if k in LISTS else isinstance(v, str)
            if not ok:
                raise SystemExit(f"{path()}: {k} must be {'a list of paths' if k in LISTS else 'a path'}")
            cfg[k] = v
    return {k: [os.path.expanduser(p) for p in v] if k in LISTS else os.path.expanduser(v) for k, v in cfg.items()}


def save(cfg):
    """Write the known settings in cfg as TOML (making the folder) and return the file's path."""
    p = path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    lines = ["# Mashup Studio settings, written by its setup page. Edit freely, then Restart in the page."]
    # A JSON string is a TOML basic string once DEL is escaped too (JSON leaves it; TOML refuses it raw). Not
    # ensure_ascii: JSON writes characters beyond U+FFFF as surrogate pairs, which TOML refuses.
    s = lambda x: json.dumps(x, ensure_ascii=False).replace("\x7f", "\\u007f")
    for k in defaults():
        if k in cfg:
            v = cfg[k]
            lines.append(f"{k} = " + ("[" + ", ".join(map(s, v)) + "]" if k in LISTS else s(v)))
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(p + ".tmp", p)
    return p
