"""Draft a brief (briefs/*.toml) from a few sentences: "140 and 180, jungle and breaks, 80s/90s vocals, no neuro".

The words go to Claude through the local `claude` CLI (Claude Code, the user's own login; no tools, one answer),
with the filter reference from criteria.py and the playlists found in the settings' playlists_folder. The answer
has to be a TOML brief with only known keys, or it is asked for once more. Without the CLI, or when it fails twice, a small
rule-based draft is returned instead (tempos, years, "no X"), and the note says so. Nothing is saved here: the
page shows the draft for editing."""
import datetime, glob, json, os, re, shutil, subprocess, tomllib

from . import config, criteria

SIDE_KEYS = {"tempo", "tempo_tol", "years", "unknown_year", "include", "exclude", "from", "acapella_only"}
MATCH_KEYS = {"max_stretch", "per_track", "per_vocal", "per_inst", "limit", "top"}
PLAYLISTS = config.load()["playlists_folder"]          # "": none
MODEL = "sonnet"
TIMEOUT = 120
SYSTEM = """You turn a DJ's description of a gig or job into a search brief for Mashup Finder, a tool that pairs
the VOCAL of one track ([vocal]) with the INSTRUMENTAL of another ([inst]) from the DJ's own library.
Answer with the TOML file only: no code fence, no text before or after.

Format:
{reference}
Rules:
- First line: name = "<the given name>". Then [song] (only for a brief about one song), [inst], [vocal], [match].
  Only the keys in the reference.
- A brief about one song ("best matches for <song>", "mashups of <song>") becomes [song]: text = the song's
  artist and/or title words, role = "vocal" if the song should give the top line, "beat" if it should carry the
  mashup, else "either". Then [inst] and [vocal] only for what the DJ says about the partners, and per_vocal and
  per_inst as high as top, so the song can meet many partners on either side.
- [inst] is the track that carries the mashup (the beat); [vocal] is the record the top line comes from.
- tempo is a list of BPM zones, not a range: "140 and 180" is [140, 180]; "around 128" is [128]. Tempo belongs
  on [inst] unless the DJ says otherwise. Do not put a tempo on [vocal]: any vocal within max_stretch is found,
  also at half and double time.
- Decades become years, from the first year of the earliest decade to the last year of the latest:
  "80s/90s" is years = [1980, 1999]; "70s and 80s" is [1970, 1989]; "90s" is [1990, 1999].
- include / exclude are whole words matched in artist, title, genre, album and folder path. Genre tags in this
  library are thin, so use include only for words the DJ gave, with their common spellings
  ("drum & bass", "dnb", "d&b"). "no X" becomes exclude. Always exclude "karaoke" on [vocal], and
  "instrumental" too unless the DJ wants instrumentals as vocal sources.
- `from` only with a playlist path from the list below, and only when its name clearly fits the description.
- [match]: max_stretch = 6, per_vocal = 1, per_inst = 4, limit = 100, top = 30, unless the description says
  otherwise ("quick test" -> top = 10; "lots of options" -> top = 50).
- Put a short # comment on every line where you interpreted something, and a # comment line for anything in
  the description that no filter can express (energy, mood, crowd), so the DJ sees what was left out.

Playlists on this machine:
{playlists}
Today is {today}."""


def _reference():
    doc = criteria.__doc__
    return doc[doc.index("Each side"):]


def _playlists():
    base = os.path.expanduser(PLAYLISTS)
    found = [] if not base else sorted(glob.glob(os.path.join(base, "**", "*.m3u8"), recursive=True))[:60]
    return "\n".join("  " + f.replace(os.path.expanduser("~"), "~", 1) for f in found) or "  (none)"


def check(text, name=None):
    """The brief as parsed TOML, or ValueError saying what is wrong with it. name: what its name line must say
    (None: any, for a brief being typed)."""
    try:
        b = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"not valid TOML: {e}")
    extra = set(b) - {"name", "ssd_folder", "song", "inst", "vocal", "match"}
    if extra:
        raise ValueError(f"unknown top-level keys: {sorted(extra)}")
    f = b.get("ssd_folder")
    if "ssd_folder" in b and not (isinstance(f, str) and f.strip() and "/" not in f and not f.startswith(".")):
        raise ValueError("ssd_folder must be a folder name (no /, not starting with a dot)")
    for side in ("inst", "vocal"):
        if not isinstance(b.get(side, {}), dict):
            raise ValueError(f"[{side}] is a table of filters")
        bad = set(b.get(side, {})) - SIDE_KEYS
        if bad:
            raise ValueError(f"unknown keys in [{side}]: {sorted(bad)}")
        s = b.get(side, {})
        if "tempo" in s and not (isinstance(s["tempo"], list) and s["tempo"] and all(isinstance(t, (int, float)) and 40 <= t <= 240 for t in s["tempo"])):
            raise ValueError(f"[{side}] tempo must be a list of BPMs")
        if "tempo_tol" in s and not (isinstance(s["tempo_tol"], (int, float)) and 0 < s["tempo_tol"] <= 50):
            raise ValueError(f"[{side}] tempo_tol is a percentage")
        if "years" in s and not (isinstance(s["years"], list) and len(s["years"]) == 2 and all(isinstance(y, int) for y in s["years"])):
            raise ValueError(f"[{side}] years must be [from, to]")
        for k in ("include", "exclude", "from"):
            if k in s and not (isinstance(s[k], list) and all(isinstance(w, str) for w in s[k])):
                raise ValueError(f"[{side}] {k} must be a list of words")
        for f in s.get("from", []):
            if not os.path.exists(os.path.expanduser(f)):
                raise ValueError(f"[{side}] from: {f} does not exist")
    song = b.get("song", {})
    if not isinstance(song, dict) or set(song) - {"text", "role"}:
        raise ValueError("[song] takes text and role")
    if "song" in b and not (isinstance(song.get("text"), str) and song["text"].strip()):
        raise ValueError("[song] text must name the song (artist and/or title words)")
    if not isinstance(song.get("role", "either"), str) or song.get("role", "either") not in criteria.ROLES:
        raise ValueError(f'[song] role is one of {", ".join(criteria.ROLES)}')
    m = b.get("match", {})
    if not isinstance(m, dict):
        raise ValueError("[match] is a table of numbers")
    if set(m) - MATCH_KEYS:
        raise ValueError(f"unknown keys in [match]: {sorted(set(m) - MATCH_KEYS)}")
    for k, v in m.items():
        if k == "max_stretch" and not (isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v <= 50):
            raise ValueError("[match] max_stretch is a percentage")
        if k != "max_stretch" and not (isinstance(v, int) and not isinstance(v, bool) and v >= 1):
            raise ValueError(f"[match] {k} is a whole number, 1 or more")
    if name is not None and b.get("name") != name:
        raise ValueError(f'the first line must be name = "{name}"')
    if not b.get("inst") and not b.get("vocal") and not b.get("song"):
        raise ValueError("no [song], [inst] or [vocal] filters")
    return b


def _unfence(text):
    m = re.search(r"```(?:toml)?\s*\n(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip() + "\n"


def _cli():
    """(the claude CLI, the environment it runs in). FileNotFoundError without it. The CLI's own login (the Claude
    subscription) only: no Claude session variables, and no ANTHROPIC_* key or endpoint from the shell (a ~/.zshrc
    API key would win over the login and may be invalid or another account's)."""
    exe = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    if not os.path.exists(exe):
        raise FileNotFoundError("the claude CLI is not installed")
    return exe, {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "ANTHROPIC_"))}


def _claude(system, user):
    exe, env = _cli()
    r = subprocess.run([exe, "-p", "--model", MODEL, "--tools", "", "--strict-mcp-config", "--system-prompt", system],
                       input=user, capture_output=True, text=True, timeout=TIMEOUT, env=env, cwd=os.path.expanduser("~"))
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError((r.stderr or r.stdout or "no answer").strip()[-300:])
    return r.stdout


def status(run=subprocess.run):
    """{'works', 'why'}: whether drafting can use Claude, from `claude auth status` in _claude's environment (so a
    shell API key does not count). Starts the CLI; nothing is sent to Claude."""
    try:
        exe, env = _cli()
        r = run([exe, "auth", "status", "--json"], capture_output=True, text=True, timeout=30, env=env, cwd=os.path.expanduser("~"))
        s = json.loads(r.stdout)
    except FileNotFoundError as e:
        return {"works": False, "why": str(e)}
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        return {"works": False, "why": f"claude auth status did not answer: {str(e) or type(e).__name__}"[:300]}
    if isinstance(s, dict) and s.get("loggedIn") is True:
        return {"works": True, "why": f"logged in ({s.get('authMethod') or 'Claude login'})"}
    return {"works": False, "why": "not logged in: run claude auth login in Terminal"}


def _header(words):
    lines = [w.strip() for w in words.strip().splitlines() if w.strip()]
    return "# Brief, as written:\n" + "".join(f"#   {w}\n" for w in lines)


def _decades(words):
    """[first, last] year of the decades named ("70s and 80s" -> [1970, 1989]), or None."""
    d = sorted({(1900 if int(x) >= 30 else 2000) + int(x) for x in re.findall(r"(?<!\d)(\d0)(?:['’]s?|s)(?![a-z])", words.lower())})
    return [d[0], d[-1] + 9] if d else None


def _years_match_decades(b, words):
    """ValueError when the words name decades (and no exact years) but a years filter spans something else."""
    want = _decades(words)
    if not want or re.search(r"\b(19[5-9]\d|20[0-2]\d)\b", words):
        return
    for side in ("inst", "vocal"):
        got = b.get(side, {}).get("years")
        if got is not None and got != want:
            raise ValueError(f"[{side}] years = {got}, but the decades named are years = {want}")


SONG = re.compile(r"best (?:mashup )?(?:matches|mashups|partners) for\s+(?:[\"“]([^\"”\n]+)[\"”]|([^,.;\n]+))", re.I)


def by_rules(name, words):
    """A plain draft without Claude: BPM numbers, decades or years, "no X" words, and "best matches for <song>"."""
    song = SONG.search(words)
    if song:                                                     # one song, any partner: no tempo or year guesses
        text = song.group(1) or re.sub(r"\s+by\s+.*$", "", song.group(2), flags=re.I)   # "<title> by <artist>": the title
        return (f'name = "{name}"\n\n[song]\ntext = {json.dumps(text.strip(), ensure_ascii=False)}\nrole = "either"\n\n'
                '[vocal]\nexclude = ["karaoke"]\n\n[match]\nmax_stretch = 8\nper_vocal = 30\nper_inst = 30\nlimit = 100\ntop = 30\n')
    w = words.lower()
    tempos = sorted({int(t) for t in re.findall(r"(?<![\d.])(\d{2,3})(?!\d|\.\d|['’]|s\b)", w) if 60 <= int(t) <= 200})
    years = sorted({int(y) for y in re.findall(r"\b(19[5-9]\d|20[0-2]\d)\b", w)})
    span = [years[0], years[-1]] if len(years) >= 2 else _decades(w)
    tempos = [t for t in tempos if t not in years]
    no = [x.strip() for x in re.findall(r"\bno\s+([a-z][a-z &/-]{1,24}?)(?=[,.;\n]|\s+or\b|\s+and\b|$)", w)]
    out = [f'name = "{name}"', "", "[inst]                          # the track carrying the vocal"]
    out += [f"tempo = {tempos or [174]}" + ("" if tempos else "          # no tempo found in the brief: set it"), "tempo_tol = 4"]
    if no:
        out.append("exclude = [" + ", ".join(f'"{x}"' for x in no) + "]")
    out += ["", "[vocal]                         # the record giving the top line"]
    if span:
        out.append(f"years = {span}")
    out += ['exclude = ["instrumental", "karaoke"]', "", "[match]", "max_stretch = 6", "per_vocal = 1", "per_inst = 4", "limit = 100", "top = 30"]
    return "\n".join(out) + "\n"


def draft(name, words, ask=_claude):
    """{'text': the brief's TOML, 'by': 'claude' | 'rules', 'note': what the page says about it}."""
    words = (words or "").strip()
    if not words:
        raise ValueError("write the brief first: tempo, style, where the vocals come from, what to avoid")
    system = SYSTEM.format(reference=_reference(), playlists=_playlists(), today=datetime.date.today().isoformat())
    user, why = f'Name: {name}\n\nThe DJ\'s description:\n{words}\n', None
    for _ in range(2):
        try:
            text = _unfence(ask(system, user))
            _years_match_decades(check(text, name), words)
            return {"text": _header(words) + text, "by": "claude", "note": "Drafted by Claude: check the lines, then Save or Run."}
        except ValueError as e:                                  # a bad answer: say what was wrong, once
            why = str(e)
            user += f"\nYour last answer was rejected: {why}. Answer again with the corrected TOML only.\n"
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as e:
            why = str(e) or type(e).__name__
            break
    text = by_rules(name, words)
    check(text, name)
    return {"text": _header(words) + text, "by": "rules",
            "note": f"Claude was not available ({why}): this is a simple draft from the numbers and \"no …\" words. Check it."}
