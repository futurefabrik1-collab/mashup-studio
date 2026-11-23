"""`mf app`: Mashup Studio, the whole flow in one local page: briefs -> find + check -> audition -> mashups ->
block editor -> copy to the SSD. It is the editor's server grown (edit.py): the same 127.0.0.1-only bind, session
token and Host check, one page (app.html), and each mashup's editor at /m/<run>/<mashup>/.

Long work runs as jobs: the mf CLI itself in a subprocess (mf run, mf mashup, mf export) or ssd_copy.py, one job
at a time from a queue, the last output line shown as progress. Cancel is Ctrl-C (SIGINT) to that process. The
server never waits on a job, so the page stays responsive while Demucs runs. Finished jobs are kept in
runs/jobs.json, so the job log survives a restart.

One Studio at a time: `mf app` listens on a fixed port (cli: 8765), and a second start opens the browser on the
one already running. Restart (from the page) re-executes the same command in the same process."""
import collections, contextlib, errno, glob, http.client, itertools, json, os, random, re, shutil, signal, subprocess, sys, \
    threading, time, tomllib, webbrowser
from urllib.parse import parse_qs

import numpy as np

from . import arrange, audition, catalog, cli, config, criteria, deep, draft, edit, engine, render

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "app.html")
SETUP = os.path.join(HERE, "setup.html")                       # the first-run page: folders, then a scan
CSS = os.path.join(HERE, "studio.css")                         # app.html's style, written into it when served
MF = [sys.executable, "-u", "-m", "mashup_finder.cli"]          # unbuffered, so progress lines arrive as printed
COPY = ("mashup.wav", "arrangement.json", "ableton")   # what Copy to SSD takes from a mashup folder: no mp3 on the SSD
SSD_COPY = [sys.executable, "-u", os.path.join(HERE, "ssd_copy.py")]
TRASH = "/usr/bin/trash"                                        # macOS's own: Finder can put the folder back
RESTARTED = "MF_APP_RESTARTED"                                  # set across Restart's exec: no second browser tab
# Run as a job's first command when the page has not confirmed: the target may have appeared while the job waited.
STILL_FREE = ["/bin/sh", "-c", 'if [ -e "$1" ]; then echo "$1 appeared meanwhile: copy again to write over it"; exit 1; fi', "sh"]
PREPARE = 3                                                     # pairs Prepare checks ahead
GIVE_WAY = 12                                                   # seconds a Restart or Delete waits for a cancelled Prepare
CLIP = 30.0                                                     # seconds of a mashup's clip.mp3
SURPRISE = 3                                                    # pairs Surprise me checks
KEEP_FINISHED = 100                                             # finished jobs the page can still see, and runs/jobs.json keeps
ACTIVE = ("queued", "running")
TAIL = 40                                                       # lines of a job the page shows and runs/jobs.json keeps
NEW_RUN = "<new run>"                                           # the run of a find + check: not made yet
LOGGED = ("id", "title", "state", "rc", "started", "ended")     # what runs/jobs.json keeps of a job, with its last lines
MASHUP_DIR = re.compile(r"^\d{3}-(.+)-v(\d+)$")                 # engine.cli: <pair id>-<template file name>-v<n>, in a run


PLAYLIST_TYPES = (".m3u8", ".m3u", ".csv")          # a brief's `from`: rekordbox playlist exports, or a csv with a path column
PLAYLIST_MISSING_SHOWN = 50                         # the live count lists this many of a playlist's tracks not found


def pick_file():
    """The Mac's Open dialog, in front: the chosen file's path, or None when cancelled."""
    r = subprocess.run(["osascript", "-e", "activate", "-e",
                        'POSIX path of (choose file with prompt "Source playlist for the brief (.m3u8, .m3u or .csv)")'],
                       capture_output=True, text=True, timeout=900)
    return r.stdout.strip() or None if r.returncode == 0 else None


FOLDER_PROMPTS = {"music": "Your music folder (Mashup Studio only reads it)", "export": "Where finished mashups go"}


def pick_folder(what):
    """The Mac's choose-folder dialog, in front: the chosen folder's path, or None when cancelled."""
    r = subprocess.run(["osascript", "-e", "activate", "-e", f'POSIX path of (choose folder with prompt "{FOLDER_PROMPTS[what]}")'],
                       capture_output=True, text=True, timeout=900)
    return (r.stdout.strip().rstrip("/") or "/") if r.returncode == 0 and r.stdout.strip() else None


class Refused(Exception):
    """A request the page has to change: status 400, 404, 409 (exists: may confirm), 423 (busy), 503 (no SSD)."""

    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status


def child(parent, name):
    """parent/name for a plain name that exists there (no separators, no dot files), else 404."""
    if not isinstance(name, str) or not name or "/" in name or "\\" in name or name.startswith(".") \
            or not os.path.exists(os.path.join(parent, name)):
        raise Refused(404, f"no {name!r} in {os.path.basename(parent)}")
    return os.path.join(parent, name)


def _job(jid, title, **fields):
    """A job's record: waiting to run, unless fields say otherwise (a job read back from the log)."""
    return {"id": jid, "title": title, "state": "queued", "rc": None, "started": None, "ended": None, "restored": False,
            "lines": collections.deque(maxlen=400), "cmds": [], "proc": None, "cancel": False, "key": None, "run": None,
            "background": False, "top": None, **fields}


class Jobs:
    """One job at a time; the rest wait in order. A job is a list of commands run one after another; it stops at
    the first that fails. Cancel drops a waiting job, or sends SIGINT to the running command (SIGKILL 10 s later).
    log: a JSON file the finished jobs are written to and read back from at the next start (marked restored)."""

    def __init__(self, cwd, log=None):
        self.cwd, self.log, self.lock, self.closed = cwd, log, threading.Lock(), False
        self.all = self._load()
        self.ids = itertools.count(max((j["id"] for j in self.all), default=0) + 1)
        self.wake = threading.Condition(self.lock)
        threading.Thread(target=self._worker, daemon=True).start()

    def _load(self):
        try:
            with open(self.log, encoding="utf-8") as f:
                old = json.load(f)
        except (TypeError, OSError, ValueError):                 # no log, none yet, or not readable: start empty
            return []
        old = [j for j in old if isinstance(j, dict) and isinstance(j.get("id"), int)] if isinstance(old, list) else []
        return [_job(j["id"], str(j.get("title")), rc=j.get("rc"), started=j.get("started"), ended=j.get("ended"), restored=True,
                     state=j["state"] if j.get("state") in ("done", "failed", "cancelled") else "cancelled",
                     lines=collections.deque(map(str, j.get("lines") or []), maxlen=400))
                for j in old[-KEEP_FINISHED:]]

    def _ended(self, j, state):
        """j is finished (call under self.lock): wake waiters, write the log."""
        j["state"], j["ended"] = state, time.time()
        self.wake.notify_all()
        self._save()

    def _save(self):
        if not self.log:
            return
        done = [{k: x[k] for k in LOGGED} | {"lines": list(x["lines"])[-TAIL:]} for x in self.all if x["state"] not in ACTIVE]
        try:
            os.makedirs(os.path.dirname(self.log), exist_ok=True)
            render.write_json(self.log, done[-KEEP_FINISHED:])
        except OSError:                                          # the log is a convenience: a full disk must not stop the queue
            pass

    def give_way(self, run=None, wait=0):
        """Cancel the background jobs (Prepare), or those of one run: something else wants the machine or the run.
        wait: seconds to let a running one end (a Restart or a Delete that should not be refused for it)."""
        with self.lock:
            gone = self._give_way(run)
            end = time.monotonic() + wait
            while any(j["state"] in ACTIVE for j in gone) and time.monotonic() < end:
                self.wake.wait(end - time.monotonic())

    def _give_way(self, run=None):
        """give_way under self.lock (held): the jobs cancelled."""
        gone = [x for x in self.all if x["background"] and x["state"] in ACTIVE and run in (None, x["run"])]
        for x in gone:
            self._cancel(x)
        return gone

    def close(self):
        """Take no more jobs (Restart). Refused while one is waiting or running (a Prepare is cancelled first)."""
        self.give_way(wait=GIVE_WAY)
        with self.lock:
            n = sum(j["state"] in ACTIVE for j in self.all)
            if n:
                raise Refused(423, f"{n} job{'s are' if n > 1 else ' is'} running or waiting: restart when "
                                   f"{'they are' if n > 1 else 'it is'} done, or cancel first")
            self.closed = True

    def add(self, title, cmds, key=None, run=None, background=False, top=None):
        """Queue a job; key names what it works on, for busy(); run: the run folder it writes (or NEW_RUN); top: the
        pairs a check checks. A background job (Prepare) gives way: adding any other job cancels it first."""
        with self.lock:
            if not background:
                self._give_way()
            if self.closed:
                raise Refused(423, "Mashup Studio is restarting: try again in a moment")
            j = _job(next(self.ids), title, cmds=cmds, key=key, run=run, background=background, top=top)
            done = [x["id"] for x in self.all if x["state"] not in ACTIVE]
            drop = set(done[:-KEEP_FINISHED])
            self.all = [x for x in self.all if x["id"] not in drop] + [j]
            self.wake.notify_all()                               # the worker, and anyone in wait()
        return j["id"]

    def _find(self, jid):
        j = next((j for j in self.all if j["id"] == jid), None)
        if j is None:
            raise Refused(404, f"no job {jid}")
        return j

    def cancel(self, jid):
        with self.lock:
            self._cancel(self._find(jid))

    def stop_all(self):
        with self.lock:
            for j in self.all:
                self._cancel(j)

    def _cancel(self, j):
        if j["state"] == "queued":
            self._ended(j, "cancelled")
        elif j["state"] == "running":
            j["cancel"] = True
            self._interrupt(j["proc"])

    def state(self, tail=TAIL, last=20):
        with self.lock:
            return [{k: j[k] for k in LOGGED + ("restored",)} | {"now": time.time(), "lines": list(j["lines"])[-tail:]}
                    for j in self.all[-last:]]

    def busy(self, *keys):
        """Whether a waiting or running job has one of these keys."""
        return any(j["key"] in keys for j in self.active())

    def active(self):
        """The waiting and running jobs: (id, key, run, state, started, top) each."""
        with self.lock:
            return [{k: j[k] for k in ("id", "key", "run", "state", "started", "top")} for j in self.all if j["state"] in ACTIVE]

    def wait(self, jid, timeout=None):
        """Until job jid has finished; its final state (tests and scripted checks)."""
        end = None if timeout is None else time.monotonic() + timeout
        with self.lock:
            while self._find(jid)["state"] in ACTIVE:
                left = None if end is None else end - time.monotonic()
                if left is not None and left <= 0:
                    raise TimeoutError(f"job {jid} still running")
                self.wake.wait(left)
            return self._find(jid)["state"]

    @staticmethod
    def _interrupt(p):
        if p and p.poll() is None:
            p.send_signal(signal.SIGINT)
            threading.Timer(10, lambda: p.poll() is None and p.kill()).start()

    def _worker(self):
        while True:
            with self.lock:
                while (j := next((j for j in self.all if j["state"] == "queued"), None)) is None:
                    self.wake.wait()
                j["state"], j["started"] = "running", time.time()
            rc = 0
            for cmd in j["cmds"]:
                try:
                    p = subprocess.Popen(cmd, cwd=self.cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, text=True, bufsize=1, errors="replace")
                except OSError as e:
                    with self.lock:
                        j["lines"].append(f"could not start {cmd[0]}: {e}")
                    rc = 127
                    break
                with self.lock:
                    j["proc"] = p
                    if j["cancel"]:
                        self._interrupt(p)
                with p.stdout:
                    for line in p.stdout:
                        with self.lock:
                            j["lines"].append(line.rstrip("\n"))
                rc = p.wait()
                if rc or j["cancel"]:
                    break
            with self.lock:
                j["proc"], j["rc"] = None, rc
                self._ended(j, "cancelled" if j["cancel"] else "done" if rc == 0 else "failed")


class App:
    """The folders the app works on, and what each button does. root holds runs/ and briefs/; templates are the
    package's (arrange.TEMPLATES), where `mf mashup` reads them. trash: None for the Trash (/usr/bin/trash), or a
    folder deleted runs and mashups are moved into (tests). claude_check: draft.status, or a stand-in (tests).
    load_catalog: the music catalog for the live count (read once per start), or a fixture (tests)."""

    def __init__(self, root=cli.ROOT, ssd=None, mf=MF, drafter=draft.draft, trash=None, claude_check=draft.status,
                 load_catalog=catalog.Catalog, picker=None, folder_picker=None):
        ssd = ssd or config.load()["export_folder"]              # the export folder: the settings', unless given
        self.ssd, self.mf, self.drafter, self.trash, self.claude_check = ssd, list(mf), drafter, trash, claude_check
        self.picker, self.folder_picker = picker or pick_file, folder_picker or pick_folder
        self.first_run = not config.exists()                     # "/" is the setup page until a Restart after it
        self.load_catalog, self._catalog, self._catalog_lock = load_catalog, None, threading.Lock()
        self.runs, self.briefs = os.path.join(root, "runs"), os.path.join(root, "briefs")
        # self.lock: picks, briefs, and every "check, then claim" of a mashup folder (see editing())
        self.jobs, self.lock, self.locks = Jobs(root, os.path.join(self.runs, "jobs.json")), threading.RLock(), {}
        self._claude = None

    def state(self):
        return {"jobs": self.jobs.state(), "ssd": {"path": self.ssd, "ok": os.path.isdir(self.ssd)},
                "templates": self.templates(), "half_below": engine.HALF_BELOW, "checking": self.checking(),
                "hq_missing": deep.hq_missing()}

    def checking(self):
        """The run a running find + check or Prepare is checking: {run, done, total, job}, else None. done counts the
        results so far, total the pairs it checks, job is the job's id."""
        for j in self.jobs.active():
            run = self._run_of(j) if j["state"] == "running" and (j["key"] or ("",))[0] in ("brief", "prepare") else None
            if run:
                d = os.path.join(self.runs, run)
                s, res = self._json(os.path.join(d, "shortlist.json"), {}), self._json(os.path.join(d, "results.json"), [])
                top = j["top"] or (s.get("args") or {}).get("top") or cli.TOP
                return {"run": run, "done": len(res), "total": min(top, len(s.get("pairs", []))), "job": j["id"]}
        return None

    def _run_of(self, j):
        """The run a job works on: its own, or for a running find + check, the run whose shortlist it wrote since it
        started (a new run, or one reused within the same minute; only one job runs at a time). None: none yet."""
        if j["run"] != NEW_RUN:
            return j["run"]
        if j["state"] != "running" or not os.path.isdir(self.runs):
            return None
        written = {}
        for name in os.listdir(self.runs):
            f = os.path.join(self.runs, name, "shortlist.json")
            if os.path.isfile(f):
                written[name] = os.path.getmtime(f)
        name = max(written, key=written.get, default=None)        # the shortlist written last
        return name if name and written[name] >= j["started"] else None

    @staticmethod
    def templates():
        return sorted(os.path.splitext(os.path.basename(f))[0] for f in glob.glob(os.path.join(arrange.TEMPLATES, "*.toml")))

    def brief_names(self):
        return sorted(os.path.splitext(f)[0] for f in os.listdir(self.briefs) if f.endswith(".toml")) if os.path.isdir(self.briefs) else []

    def brief(self, name):
        with open(child(self.briefs, f"{name}.toml"), encoding="utf-8") as f:
            return {"name": name, "text": f.read()}

    def run_list(self):
        out = []
        for name in sorted(os.listdir(self.runs), reverse=True) if os.path.isdir(self.runs) else []:
            d = os.path.join(self.runs, name)
            if not os.path.exists(os.path.join(d, "shortlist.json")):
                continue
            res = self._json(os.path.join(d, "results.json"), [])
            out.append({"name": name, "checked": len(res), "ok": sum(1 for p in res if p.get("deep", {}).get("ok")),
                        "kept": list(self.marks(d).values()).count("keep"), "mashups": len(self.mashups(d))})
        return out

    def audition_page(self, run):
        """The run's standalone audition page, served: marks through the API (audition.py)."""
        return audition.page(self._json(os.path.join(child(self.runs, run), "results.json"), []), run, run, served=True)

    def run(self, name):
        d = child(self.runs, name)
        by_pair = collections.defaultdict(list)
        for m in self.mashups(d):
            by_pair[m["pair"]].append(m)
        return {"name": name, "brief": (self._brief_of(d) or {}).get("name"), "pairs": self._json(os.path.join(d, "results.json"), []),
                "marks": self.marks(d), "mashups": by_pair}

    def all_mashups(self):
        """Every mashup folder in every run, newest first, with its pair's names: what the Mashups page lists."""
        out = []
        for run in sorted(os.listdir(self.runs)) if os.path.isdir(self.runs) else []:
            d = os.path.join(self.runs, run)
            ms = self.mashups(d)
            if not ms:
                continue
            pairs, brief, ssd = {p["id"]: p for p in self._json(os.path.join(d, "results.json"), [])}, self._brief_of(d), os.path.isdir(self.ssd)
            for m in ms:
                p = pairs.get(m["pair"], {})
                out.append({**m, "run": run, "vocal_label": p.get("vocal_label", "?"), "inst_label": p.get("inst_label", "?"),
                            "made": os.path.getmtime(os.path.join(d, "mashups", m["dir"])),
                            "on_ssd": os.path.exists(self._ssd_path(p, brief, m)) if ssd and p else None})   # None: can't tell
        return sorted(out, key=lambda m: -m["made"])

    def marks(self, run_dir):
        return {str(k["id"]): k.get("mark", "keep") for k in self._json(os.path.join(run_dir, "picks.json"), [])}

    def mashups(self, run_dir):
        base = os.path.join(run_dir, "mashups")
        return [m for name in (sorted(os.listdir(base)) if os.path.isdir(base) else []) if (m := self._describe(base, name))]

    def _describe(self, base, name):
        """A mashup folder: its pair from arrangement.json; its template and variation from the folder name, which
        engine.cli makes from the template's file name (the name Make is given); loudness; whether it has a mix."""
        n, arr = MASHUP_DIR.match(name), self._json(os.path.join(base, name, "arrangement.json"), {})
        pid = (arr.get("pair") or {}).get("id")
        if not n or not isinstance(pid, int):
            return None
        r = self._json(os.path.join(base, name, "render.json"), {})
        return {"dir": name, "pair": pid, "template": n[1], "v": int(n[2]), "lufs": r.get("integrated_lufs"),
                "has_mix": os.path.exists(os.path.join(base, name, "mashup.mp3")),
                "has_clip": os.path.exists(os.path.join(base, name, "clip.mp3")),
                "rating": self._json(os.path.join(base, name, "rating.json"), {}).get("rating")}

    def rate(self, run, mashup, stars):
        """1 to 5 stars for a mashup, or None to clear: its folder's rating.json, with the pair's tracks, which the
        catalog match learns from (pairs.taste)."""
        if stars is not None and not (isinstance(stars, int) and not isinstance(stars, bool) and 1 <= stars <= 5):
            raise ValueError("a rating is 1 to 5 stars, or null to clear it")
        d, src, m = self._mashup(run, mashup)
        p = self._pair(d, m["pair"])
        with self.lock:
            render.write_json(os.path.join(src, "rating.json"), {"rating": stars, **{k: p[k] for k in ("vocal_path", "inst_path", "vocal_label", "inst_label")}})
        return {"ok": True, "rating": stars}

    def clip(self, run, mashup):
        """clip.mp3 in the mashup folder: CLIP seconds of the mix from the section start where they are loudest
        (RMS of mashup.wav), with a 0.5 s fade in and a 2 s fade out. Not while it is made, copied, saved or rendered."""
        with self.editing(run, mashup) as ed:                    # claims the folder, as a render does
            if not os.path.exists(os.path.join(ed.dir, "mashup.wav")):
                raise ValueError("render the mashup first (no mashup.wav)")
            start, dur = render.clip(ed.dir, ed.load(), CLIP)
        return {"ok": True, "start": round(start, 2), "seconds": round(dur, 2)}

    def _mashup(self, run, mashup):
        """(run folder, mashup folder, its description), else 404."""
        src = child(os.path.join(child(self.runs, run), "mashups"), mashup)
        m = self._describe(os.path.dirname(src), mashup)
        if m is None:
            raise Refused(404, f"{mashup} is not a mashup folder")
        return os.path.dirname(os.path.dirname(src)), src, m

    @staticmethod
    def _json(path, default):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    def _brief_of(self, run_dir):
        return self._json(os.path.join(run_dir, "shortlist.json"), {}).get("brief")

    def _pair(self, run_dir, pid):
        p = next((p for p in self._json(os.path.join(run_dir, "results.json"), []) if p["id"] == pid), None)
        if p is None:
            raise Refused(404, f"no pair {pid} in {os.path.basename(run_dir)}")
        return p

    def _lock(self, folder):
        """The folder's save/render lock, shared by all its Editors (re-entrant: Editor.save takes it again)."""
        with self.lock:
            return self.locks.setdefault(folder, threading.RLock())

    def _free(self, folder):
        """Whether no save or render holds the folder now (call under self.lock, where those start)."""
        lock = self._lock(folder)
        if lock.acquire(blocking=False):
            lock.release()
            return True
        return False

    def close(self):
        """Take no more work, for a restart: refused while the editor saves or renders (in a request's thread, which
        the restart would cut off) or a job waits or runs. editing() refuses from then on."""
        with self.lock:                                          # where editing() claims a folder
            if not all(map(self._free, list(self.locks))):
                raise Refused(423, "the editor is saving or rendering: restart when it is done")
            self.jobs.close()

    def editor(self, run, mashup):
        """An Editor to read runs/<run>/mashups/<mashup> with."""
        _, src, _ = self._mashup(run, mashup)
        return edit.Editor(src, self._lock(src))

    @contextlib.contextmanager
    def editing(self, run, mashup):
        """An Editor for one save or render, refused (423) while a Make or Copy of the mashup waits or runs, or while
        another save or render of it is going. Checked and claimed in one step under self.lock, as Make and Copy do,
        so a save cannot slip in after Make's backup, nor a render under a copy."""
        _, src, m = self._mashup(run, mashup)
        lock = self._lock(src)
        self.jobs.give_way()                                     # a render is heavy: no Prepare beside it
        with self.lock:
            if self.jobs.closed:
                raise Refused(423, "Mashup Studio is restarting: save again in a moment")
            if self._made_or_copied(run, mashup, m):
                raise Refused(423, f"{mashup} is being made or copied: edit it when that is done")
            if not lock.acquire(blocking=False):
                raise Refused(423, f"{mashup} is already saving or rendering")
        try:
            yield edit.Editor(src, lock)
        finally:
            lock.release()

    @staticmethod
    def _brief_name(name):
        if not isinstance(name, str) or not name[:1].isalnum() or len(name) > 80 \
                or not all(c.isalnum() or c in "._-" for c in name):
            raise ValueError("a brief name is letters, digits, dot, dash or underscore")

    def draft_brief(self, name, words, confirm=False):
        """A draft brief from the DJ's own words (draft.py: Claude through the local CLI, else simple rules).
        Not saved: the page shows it for editing. An existing brief of that name is only replaced after a confirm."""
        self._brief_name(name)
        if not isinstance(words, str):
            raise ValueError("send the brief's words")
        if os.path.exists(os.path.join(self.briefs, f"{name}.toml")) and confirm is not True:
            raise Refused(409, f"a brief called {name} already exists")
        return self.drafter(name, words)

    def claude(self, recheck=False):
        """Whether drafting can use Claude ({works, why, at}): checked once, then kept until the page asks again (a
        check starts the claude CLI, about a second)."""
        if recheck or self._claude is None:
            self._claude = {**self.claude_check(), "at": time.time()}
        return self._claude

    def count(self, text):
        """The live count for a brief as typed (not saved): its catalog match only, no audio, nothing written.
        {line, why, pairs, song: {text, matches, examples} for a song brief}."""
        if not isinstance(text, str):
            raise ValueError("send the brief's text")
        brief = draft.check(text)                                # what the match cannot read is a 400, said plainly
        seed, role = criteria.song(brief)
        cat = self.catalog()
        _, n = cli.quick_match(cat, brief, seed, role, rated=cli.ratings(self.runs))
        line, why = cli.match_line(n)
        out = {"line": line, "why": why, "pairs": n["pairs"], "playlists": {}}
        for side in ("vocal", "inst"):                           # each side's playlists: how many of their tracks were found
            files = (brief.get(side) or {}).get("from")
            if files:
                p = criteria.playlist(cat, files)
                out["playlists"][side] = {"found": p["found"], "total": p["total"], "missing": p["missing"][:PLAYLIST_MISSING_SHOWN],
                                          "more": max(0, len(p["missing"]) - PLAYLIST_MISSING_SHOWN)}
        if seed:
            hits = n["hits"]
            out["song"] = {"text": seed, "matches": len(hits), "hint":
                           f"{len(hits)} track{'s' * (len(hits) != 1)} match: " + " · ".join(cat.label(i) for i in hits[:3]) + " …" * (len(hits) > 3)
                           if hits else f"No track in the catalog matches “{seed}”: check the spelling (or plug in the drive)."}
        return out

    def pick_playlist(self):
        """A playlist file chosen in the Mac's own Open dialog: {"path"}, or {"path": None} when cancelled."""
        path = self.picker()
        if path and os.path.splitext(path)[1].lower() not in PLAYLIST_TYPES:
            raise ValueError(f"{os.path.basename(path)} is not a playlist: pick an {' / '.join(PLAYLIST_TYPES)} file")
        return {"path": path or None}

    def setup(self):
        """What the setup page shows: the settings file and its folders (or a suggestion), whether the catalog is
        there yet, and which of the programs the Studio runs are missing."""
        s = config.load()
        music = s["music_folders"] or [f for f in [os.path.expanduser("~/Music")] if os.path.isdir(f)]
        return {"file": config.path(), "saved": config.exists(), "music_folders": music, "export_folder": s["export_folder"],
                "catalog": s["catalog"], "catalog_ok": os.path.isfile(s["catalog"]),
                "missing": [p for p in ("ffmpeg", "ffprobe", "rubberband") if not shutil.which(p)]}

    def pick_folder(self, what):
        """A folder chosen in the Mac's own dialog: {"path"}, or {"path": None} when cancelled."""
        if what not in FOLDER_PROMPTS:
            raise ValueError(f"what must be one of {', '.join(FOLDER_PROMPTS)}")
        return {"path": self.folder_picker(what)}

    def save_setup(self, music_folders, export_folder):
        """Write the settings file from the setup page: music folders that are there, and an export folder that is
        there or is made inside a folder that is (never on a drive that is not connected); the other settings are
        kept. The Studio exports there from now on; the rest takes effect at the next start ({"file"})."""
        if not isinstance(music_folders, list) or not all(isinstance(f, str) for f in music_folders):
            raise ValueError("music_folders must be a list of folders")
        folders = list(dict.fromkeys(os.path.expanduser(f.strip()).rstrip("/") or "/" for f in music_folders if f.strip()))
        if not folders:
            raise ValueError("add your music folder")
        for f in folders:
            if not (os.path.isabs(f) and os.path.isdir(f)):
                raise ValueError(f"{f} is not a folder: choose it with Add a folder")
        out = os.path.expanduser(export_folder.strip()).rstrip("/") if isinstance(export_folder, str) else ""
        if not os.path.isabs(out):
            raise ValueError("choose the export folder")
        if os.path.realpath(out) in map(os.path.realpath, folders):       # a scan leaves the export folder out
            raise ValueError("the export folder cannot be a music folder: choose a folder inside or beside it")
        if not os.path.isdir(out):
            if not os.path.isdir(os.path.dirname(out)):
                raise ValueError(f"{os.path.dirname(out)} is not there: choose an export folder on a connected drive")
            os.mkdir(out)
        d = config.defaults()
        kept = {k: v for k, v in config.load().items() if v != d[k]}          # what was set before, not the defaults
        with self.lock:
            path = config.save({**kept, "music_folders": folders, "export_folder": out})
            self.ssd = out
        return {"file": path}

    def scan(self):
        """`mf scan` as a job: the settings' music folders into its catalog."""
        if not config.exists():
            raise ValueError("save the folders first")
        with self.lock:
            if self.jobs.busy(("scan",)):
                raise Refused(423, "a scan is already running")
            return self.jobs.add("scan the music folders", [self.mf + ["scan"]], ("scan",))

    def save_playlist(self, name, text):
        """A playlist dropped on the page, kept in briefs/playlists/ (the page cannot see where the file was): {"path"}.
        The same name and content is the same file; another content under a taken name gets a free name."""
        if not isinstance(name, str) or not isinstance(text, str):
            raise ValueError("send the playlist's file name and text")
        stem, ext = os.path.splitext(os.path.basename(name))
        if ext.lower() not in PLAYLIST_TYPES:
            raise ValueError(f"{name} is not a playlist: drop an {' / '.join(PLAYLIST_TYPES)} file")
        stem = re.sub(r"[^\w .()-]+", "-", stem).strip(" .-") or "playlist"
        d = os.path.join(self.briefs, "playlists")
        os.makedirs(d, exist_ok=True)
        with self.lock:
            k = 1
            while True:
                f = os.path.join(d, f"{stem}{f' {k}' if k > 1 else ''}{ext.lower()}")
                if not os.path.exists(f):
                    break
                with open(f, encoding="utf-8", errors="replace") as fh:
                    if fh.read() == text:
                        return {"path": f}
                k += 1
            tmp = f + ".part"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, f)
        return {"path": f}

    def catalog(self):
        """The music catalog, read once per start (the first time takes seconds); ValueError when no track is online."""
        with self._catalog_lock:
            if self._catalog is None:
                self._catalog = self.load_catalog()
            if not self._catalog.n:                              # the drive is not plugged in: read it again next time
                self._catalog = None
                raise ValueError("no track of the catalog is on this Mac right now: is the music drive plugged in?")
            return self._catalog

    def surprise(self, pick=random.choice):
        """Surprise me: a strong vocal from the library at random (a vocal source by a known artist, not a club cut,
        one you have played or put in rekordbox if there are any), and a small find + check for it (top 3)."""
        cat = self.catalog()
        ok = cat.vocal_ok & cat.known & ~cat.club & ~cat.acapella
        pool = np.flatnonzero(ok & cat.familiar) if (ok & cat.familiar).any() else np.flatnonzero(ok)
        if not pool.size:
            raise ValueError("no vocal by a known artist in the catalog")
        label = cat.label(int(pick(list(pool))))
        name = "surprise-" + re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:30]
        cmd = self.mf + ["run", "--seed", label, "--role", "vocal", "--top", str(SURPRISE), "--limit", "20", "--name", name]
        return {"ok": True, "vocal": label, "job": self.jobs.add(f"surprise: {label}", [cmd], ("brief", name), NEW_RUN, top=SURPRISE)}

    def save_brief(self, name, text):
        self._brief_name(name)
        if not isinstance(text, str):
            raise ValueError("send the brief's text")
        tomllib.loads(text)                                      # a TOML error (ValueError) goes back to the page
        os.makedirs(self.briefs, exist_ok=True)
        f = os.path.join(self.briefs, f"{name}.toml")
        with self.lock:                                          # two quick saves keep two backups
            backup = edit.backup(f, ".toml.bak") if os.path.exists(f) else None
            with open(f + ".tmp", "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(f + ".tmp", f)
        return backup

    def mark(self, run, pid, mark):
        """Keep, skip or clear (None) pair pid in runs/<run>/picks.json: the list the audition page exports, so
        `mf export RUN --picks runs/<run>/picks.json` reads it; each entry also carries its mark."""
        if mark not in ("keep", "skip", None):
            raise ValueError("mark is keep, skip or null")
        d = child(self.runs, run)
        p = self._pair(d, pid)
        with self.lock:
            f = os.path.join(d, "picks.json")
            picks = [k for k in self._json(f, []) if k["id"] != pid]
            if mark:
                picks.append({k: p[k] for k in ("id", "vocal_label", "inst_label", "vocal_path", "inst_path")}
                             | {"deep": p.get("deep", {}), "mark": mark})
            render.write_json(f, sorted(picks, key=lambda k: k["id"]))
        return self.marks(d)

    def run_brief(self, name):
        f = child(self.briefs, f"{name}.toml")
        return self.jobs.add(f"run brief {name}", [self.mf + ["run", "--brief", f]], ("brief", name), NEW_RUN)

    def make_mashup(self, run, pid, template, variations, tempo, confirm=False, hq=False):
        """`mf mashup RUN ID`. It writes into the pair's <id>-<template>-v<n> folders, replacing what is there, so
        existing ones need the page's confirm; their arrangement.json (with any editor changes) is backed up first."""
        d = child(self.runs, run)
        if not self._pair(d, pid).get("deep", {}).get("ok"):
            raise ValueError(f"pair {pid} failed the audio check: no mashup")
        if template not in self.templates():
            raise ValueError(f"no template {template!r}")
        if not isinstance(variations, int) or not 1 <= variations <= 8:
            raise ValueError("variations: 1 to 8")
        cmd = self.mf + ["mashup", d, str(pid), "--template", template, "--variations", str(variations)] + ["--hq-stems"] * (hq is True)
        if tempo not in (None, ""):
            if not isinstance(tempo, (int, float)) or not 60 <= tempo <= 220:
                raise ValueError("tempo: 60 to 220 BPM, or empty for the instrumental's")
            cmd += ["--tempo", str(tempo)]
        t = engine._safe(template)                               # as engine.cli writes it into the folder names
        key = ("make", run, pid, t)
        with self.lock:
            if self.jobs.busy(key):
                raise Refused(423, f"a {template} mashup of pair {pid} is already waiting or running")
            old = [os.path.join(d, "mashups", m["dir"]) for m in self.mashups(d)
                   if m["pair"] == pid and m["template"] == t and m["v"] <= variations]
            if old and confirm is not True:
                raise Refused(409, f"{', '.join(map(os.path.basename, old))} already made: Make again replaces "
                                   f"{'it' if len(old) == 1 else 'them'} (each arrangement.json is kept as a backup)")
            if not all(map(self._free, old)):
                raise Refused(423, "it is rendering or saving in the editor: try again when that is done")
            for f in old:                                        # editor saves are refused from here until the job ends
                edit.backup(os.path.join(f, "arrangement.json"))
            return self.jobs.add(f"mashup {run} pair {pid} ({template} x{variations})", [cmd], key, run)

    def _ssd_ok(self):
        if not os.path.isdir(self.ssd):
            raise Refused(503, f"{self.ssd} is not there: connect its drive, or choose another export folder in Settings"
                               " (nothing was copied)")

    def export(self, run):
        d = child(self.runs, run)
        self._ssd_ok()
        if "keep" not in self.marks(d).values():
            raise ValueError("keep at least one pair first")
        with self.lock:                                          # not while the run is being moved to the Trash
            return self.jobs.add(f"export kept pairs of {run} to the export folder",
                                 [self.mf + ["export", d, "--picks", os.path.join(d, "picks.json"), "--dest", self.ssd]], ("export", run), run)

    def copy_target(self, run, mashup):
        """Where Copy to SSD puts a mashup: the pair's folder (as `mf export` names it), MASHUP - <template>[ vN]."""
        return self._target(*self._mashup(run, mashup)[::2])

    def _target(self, run_dir, m):
        return self._ssd_path(self._pair(run_dir, m["pair"]), self._brief_of(run_dir), m)

    def _ssd_path(self, pair, brief, m):
        sub, pair_folder = cli._pair_folder(pair, brief)
        return os.path.join(self.ssd, sub, pair_folder, f"MASHUP - {m['template']}" + (f" v{m['v']}" if m["v"] > 1 else ""))

    def make_all(self, run, template, variations=1):
        """Make for every kept pair that has no `template` mashup yet: normal jobs, one per pair. The rest are
        listed with the reason ({jobs, skipped})."""
        d, jobs, skipped = child(self.runs, run), [], []
        for pid in sorted(int(k) for k, v in self.marks(d).items() if v == "keep"):
            try:                                                 # an existing one asks first (409): skipped here
                jobs.append(self.make_mashup(run, pid, template, variations, None))
            except (Refused, ValueError) as e:
                skipped.append(f"pair {pid}: {e}")
        return {"ok": True, "jobs": jobs, "skipped": skipped}

    def copy_all(self, run):
        """Copy to SSD every rendered mashup of the run that is not on the SSD yet: normal jobs, never over an
        existing folder ({jobs, skipped})."""
        d = child(self.runs, run)
        self._ssd_ok()
        jobs, skipped, taken = [], [], {}
        for m in self.mashups(d):
            try:                                                 # an existing target asks first (409): skipped here
                dst = self._target(d, m)
                if dst in taken:                                 # two pairs of the same two tracks: one folder
                    raise ValueError(f"goes to the same export folder as {taken[dst]}")
                jobs.append(self.copy(run, m["dir"]))
                taken[dst] = m["dir"]
            except (Refused, ValueError) as e:
                skipped.append(f"{m['dir']}: {e}")
        return {"ok": True, "jobs": jobs, "skipped": skipped}

    def prepare(self, run, n=PREPARE):
        """Check the next n pairs of the run's shortlist ahead, while the queue is idle: a background job, which any
        other job cancels when it is added (finished pairs stay in results.json). {job: id or None, why}."""
        d = child(self.runs, run)
        total = len(self._json(os.path.join(d, "shortlist.json"), {}).get("pairs", []))
        ids = [p["id"] for p in self._json(os.path.join(d, "results.json"), [])]
        under = max(ids) + 1 if ids else 0                       # top covers every pair checked so far: none drops out
        top = min(total, under + max(0, n - (under - len(ids))))  # n to check, counting gaps a cancelled check left
        with self.lock:
            if len(ids) >= total:
                return {"ok": True, "job": None, "why": "every pair of the shortlist is checked"}
            if self.jobs.active() or not all(map(self._free, list(self.locks))):
                return {"ok": True, "job": None, "why": "another job, or the editor, is working"}
            cmd = self.mf + ["check", d, "--top", str(top)]
            return {"ok": True, "job": self.jobs.add(f"prepare {run}: check the next pairs ahead", [cmd], ("prepare", run), run,
                                                     background=True, top=top)}

    def copy(self, run, mashup, confirm=False):
        """Copy a mashup into its pair folder on the SSD (ssd_copy.py: whole files only, then dot_clean). An
        existing target is only written over when the page has confirmed; nothing on the SSD is ever deleted
        (the copy adds and replaces files, it does not prune)."""
        d, src, m = self._mashup(run, mashup)
        self._ssd_ok()
        dst = self._target(d, m)
        items = [i for i in COPY if os.path.exists(os.path.join(src, i))]
        if "mashup.wav" not in items:
            raise ValueError("render the mashup first (no mashup.wav)")
        with self.lock:
            if self.jobs.busy(("make", run, m["pair"], m["template"])) or not self._free(src):
                raise Refused(423, f"{mashup} is being rendered or made: copy it when that is done")
            if os.path.exists(dst) and confirm is not True:
                raise Refused(409, f"{dst} already exists: confirm to write over it")
            guard = [] if confirm is True else [STILL_FREE + [dst]]
            return self.jobs.add(f"copy {mashup} to the export folder", guard + [SSD_COPY + [src, dst, *items]], ("copy", run, mashup), run)

    def delete_run(self, run, confirm=False):
        """Move runs/<run> to the Trash, once the page has confirmed (409 asks). Refused while a job works on it:
        a Make, Copy or Export of it, the find + check writing it, or an editor save or render in it."""
        d = child(self.runs, run)
        if not os.path.isfile(os.path.join(d, "shortlist.json")):
            raise Refused(404, f"{run} is not a run")
        self.jobs.give_way(run, wait=GIVE_WAY)                   # a Prepare of it stops first
        with self.lock:
            ms = self.mashups(d)
            if self._run_busy(run) or not all(self._free(os.path.join(d, "mashups", m["dir"])) for m in ms):
                raise Refused(423, f"a job or the editor is working on {run}: delete it when that is done")
            if confirm is not True:
                with_ms = f" and its {len(ms)} mashup{'s' if len(ms) > 1 else ''}" if ms else ""
                raise Refused(409, f"Move the run {run}{with_ms} to the Trash?")
            return self._to_trash(d)

    def delete_mashup(self, run, mashup, confirm=False):
        """Move one mashup folder to the Trash, as delete_run; refused while it is made, copied, saved or rendered."""
        _, src, m = self._mashup(run, mashup)
        with self.lock:
            if self._made_or_copied(run, mashup, m) or not self._free(src):
                raise Refused(423, f"{mashup} is being made, copied, saved or rendered: delete it when that is done")
            if confirm is not True:
                raise Refused(409, f"Move the mashup {mashup} to the Trash?")
            return self._to_trash(src)

    def _made_or_copied(self, run, mashup, m):
        """Whether a Make of the mashup's pair and template, or a Copy of it, waits or runs."""
        return self.jobs.busy(("make", run, m["pair"], m["template"]), ("copy", run, mashup))

    def _run_busy(self, run):
        """A waiting or running Make, Copy, Export or Prepare of the run, or the find + check writing it."""
        return any(self._run_of(j) == run for j in self.jobs.active())

    def _to_trash(self, path):
        """Moved, never deleted: to the Trash with macOS's trash command, or into self.trash (a folder) with a
        free name. The answer says where."""
        if self.trash is None:
            r = subprocess.run([TRASH, path], capture_output=True, text=True, timeout=60)
            if r.returncode or os.path.exists(path):
                raise OSError(f"could not move it to the Trash: {(r.stderr or r.stdout).strip() or f'trash exited {r.returncode}'}")
            return {"ok": True, "to": "the Trash"}
        name, k = os.path.basename(path), 1
        while os.path.lexists(dst := os.path.join(self.trash, name + (f" {k}" if k > 1 else ""))):
            k += 1
        shutil.move(path, dst)
        return {"ok": True, "to": dst}


class Handler(edit.Handler):
    server_version = "mf-app"
    shutdown_after_reply = False                                 # set by Quit and Restart
    frame_ancestors = "'self'"                                   # the Editor tab frames /m/<run>/<mashup>/

    @property
    def app(self):
        return self.server.app

    def _in_editor(self, path, act):
        """For /m/<run>/<mashup>/<rest>: act(run, mashup, "/<rest>"), with refusals sent as their status."""
        parts = path.split("/", 4)                               # "", "m", run, mashup, rest
        try:
            if len(parts) < 5:
                raise Refused(404, "open a mashup as /m/<run>/<mashup>/")
            act(parts[2], parts[3], "/" + parts[4])
        except Refused as e:
            self._send(e.status, {"ok": False, "error": str(e)})
        except (SystemExit, ValueError) as e:                    # no valid arrangement.json in the folder
            self._send(404, {"ok": False, "error": str(e)})

    def _edit_post(self, run, mashup, rest, b):
        with self.app.editing(run, mashup) as ed:
            self.editor_post(ed, rest, b)

    def _answer(self, fn):
        try:
            return self._send(200, fn())
        except Refused as e:
            return self._send(e.status, {"ok": False, "error": str(e)})
        except (ValueError, OSError, SystemExit) as e:           # SystemExit: config.load() on a broken settings file
            return self._send(400, {"ok": False, "error": str(e)})
        except Exception as e:                                   # a bug: say what it was, not "bad request"
            return self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

    def route_get(self, path, url):
        if path in ("/", "/setup"):
            page = SETUP if path == "/setup" or self.app.first_run else PAGE
            with open(page, encoding="utf-8") as f, open(CSS, encoding="utf-8") as c:
                return self._html(f.read().replace("/* studio.css */", c.read(), 1))
        if path.startswith("/m/"):
            return self._in_editor(path, lambda run, mashup, rest: self.editor_get(self.app.editor(run, mashup), rest, url))
        parts = path.split("/")
        if parts[1:2] == ["r"] and parts[3:] == ["audition.html"]:   # a page: it carries the token, like /
            try:
                return self._html(self.app.audition_page(parts[2]))
            except Refused as e:
                return self._send(e.status, {"error": str(e)})
        if not self._token_ok(url, query_ok=path.startswith("/r/")):
            return self._send(403, {"error": "missing or wrong session token"})
        if path.startswith("/r/"):                               # /r/<run>/previews/<file>: an audition preview
            try:
                if len(parts) != 5 or parts[3] != "previews":
                    raise Refused(404, "not a preview")
                f = child(os.path.join(child(self.app.runs, parts[2]), "previews"), parts[4])
            except Refused as e:
                return self._send(404, {"error": str(e)})
            with open(f, "rb") as fh:
                return self._send_bytes(fh.read(), "audio/mpeg")
        q = parse_qs(url.query)
        name = q.get("name", [""])[0]
        routes = {"/api/state": self.app.state, "/api/briefs": self.app.brief_names, "/api/runs": self.app.run_list, "/api/mashups": self.app.all_mashups,
                  "/api/brief": lambda: self.app.brief(name), "/api/run": lambda: self.app.run(name),
                  "/api/jobs": lambda: self.app.jobs.state(last=KEEP_FINISHED),
                  "/api/claude": lambda: self.app.claude("recheck" in q), "/api/setup": self.app.setup}
        if path in routes:
            return self._answer(routes[path])
        self._send(404, {"error": "not found"})

    def route_post(self, path, url, b):
        if path.startswith("/m/"):
            return self._in_editor(path, lambda run, mashup, rest: self._edit_post(run, mashup, rest, b))
        a = self.app
        routes = {
            "/api/brief": lambda: {"ok": True, "backup": a.save_brief(b.get("name"), b.get("text"))},
            "/api/draft-brief": lambda: {"ok": True, **a.draft_brief(b.get("name"), b.get("words"), b.get("confirm"))},
            "/api/run-brief": lambda: {"ok": True, "job": a.run_brief(b.get("name"))},
            "/api/mark": lambda: {"ok": True, "marks": a.mark(b.get("run"), b.get("id"), b.get("mark"))},
            "/api/mashup": lambda: {"ok": True, "job": a.make_mashup(b.get("run"), b.get("id"), b.get("template"),
                                                                       b.get("variations", 1), b.get("tempo"), b.get("confirm"), b.get("hq"))},
            "/api/export": lambda: {"ok": True, "job": a.export(b.get("run"))},
            "/api/copy": lambda: {"ok": True, "job": a.copy(b.get("run"), b.get("mashup"), b.get("confirm"))},
            "/api/surprise": lambda: a.surprise(),
            "/api/clip": lambda: a.clip(b.get("run"), b.get("mashup")),
            "/api/rate": lambda: a.rate(b.get("run"), b.get("mashup"), b.get("rating")),
            "/api/make-all": lambda: a.make_all(b.get("run"), b.get("template"), b.get("variations", 1)),
            "/api/copy-all": lambda: a.copy_all(b.get("run")),
            "/api/prepare": lambda: a.prepare(b.get("run")),
            "/api/count": lambda: a.count(b.get("text")),
            "/api/pick-playlist": lambda: {"ok": True, **a.pick_playlist()},
            "/api/save-playlist": lambda: {"ok": True, **a.save_playlist(b.get("name"), b.get("text"))},
            "/api/setup": lambda: {"ok": True, **a.save_setup(b.get("music_folders"), b.get("export_folder"))},
            "/api/pick-folder": lambda: {"ok": True, **a.pick_folder(b.get("what"))},
            "/api/scan": lambda: {"ok": True, "job": a.scan()},
            "/api/delete-run": lambda: a.delete_run(b.get("run"), b.get("confirm")),
            "/api/delete-mashup": lambda: a.delete_mashup(b.get("run"), b.get("mashup"), b.get("confirm")),
            "/api/cancel": lambda: a.jobs.cancel(b.get("id")) or {"ok": True},
            "/api/quit": self._quit,
            "/api/restart": self._restart,
        }
        if path in routes:
            try:
                return self._answer(routes[path])
            finally:                                             # after the answer, so the process ends after it is sent
                if self.shutdown_after_reply:                    # (and also when the page went away before it)
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
        self._send(404, {"error": "not found"})

    def _quit(self):
        self.app.jobs.stop_all()
        self.shutdown_after_reply = True
        return {"ok": True}

    def _restart(self):
        """Stop and start again: serve() re-executes the process (App.close says when not)."""
        self.app.close()
        self.server.restart = self.shutdown_after_reply = True
        return {"ok": True}


def make_server(port=0, host=edit.HOST, **app_args):
    """The app's server, not yet serving; app_args go to App (root, ssd, mf, drafter, trash, claude_check)."""
    srv = edit.bind(Handler, port, host)
    srv.app, srv.restart = App(**app_args), False
    return srv


def studio_at(port):
    """Whether a Mashup Studio answers on 127.0.0.1:port."""
    c = http.client.HTTPConnection(edit.HOST, port, timeout=3)
    try:
        c.request("HEAD", "/", headers={"Host": f"{edit.HOST}:{port}"})
        return c.getresponse().getheader("Server", "").startswith(Handler.server_version + " ")
    except (OSError, http.client.HTTPException):                 # nothing there, or not a web server
        return False
    finally:
        c.close()


def serve(port=0, open_browser=True, log=print, **app_args):
    """Serve until Quit; Restart runs the same command again in this process. If a Studio already has the port,
    open the browser on it instead and return."""
    if os.environ.pop(RESTARTED, None):                          # the page is open and reconnects by itself
        open_browser = False
    try:
        srv = make_server(port, **app_args)
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        if not studio_at(port):
            raise SystemExit(f"{edit.HOST}:{port} is in use by another program: start with --port <another port>")
        url = f"http://{edit.HOST}:{port}/"
        log(f"Mashup Studio is already running: {url}")
        if open_browser:
            webbrowser.open(url)
        return
    edit.run(srv, "Mashup Studio (Quit in the page)", open_browser, log, srv.app.jobs.stop_all)
    if srv.restart:
        log("Restarting Mashup Studio")
        sys.stdout.flush()
        os.execve(sys.executable, sys.orig_argv, {**os.environ, RESTARTED: "1"})
