"""`mf app` (app.py): the job queue, the server's routes and safety, picks.json, export and Copy to SSD.
Jobs run tests/fake_mf.py (the real CLI with the audio work faked) on a temp root; a temp folder stands in for the
SSD, so nothing under runs/, _cache/ or /Volumes is touched."""
import http.client, http.server, json, os, re, shutil, socket, subprocess, sys, threading, time, unittest
from unittest import mock
from urllib.parse import quote

from mashup_finder import app, audition, catalog, cli, edit, engine, render, ssd_copy
from tests import fixtures as fx

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_mf.py")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = [sys.executable, "-c"]
BRIEF = 'name = "demo"\n\n[inst]\ntempo = [126]\n\n[match]\ntop = 2\n'


def request(port, method, path, body=None, headers=None, timeout=60):
    """(status, body, response) of one request to 127.0.0.1:port, with its Host header."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        c.request(method, path, body=body, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
        r = c.getresponse()
        return r.status, r.read(), r
    finally:
        c.close()


class JobsTest(unittest.TestCase):
    def setUp(self):
        self.jobs = app.Jobs(fx.tmpdir("mf-jobs-"))

    def test_one_at_a_time_in_order(self):
        a = self.jobs.add("a", [PY + ["import time; print('a1', flush=True); time.sleep(1); print('a2')"]])
        b = self.jobs.add("b", [PY + ["print('b')"]])
        time.sleep(0.5)
        st = {j["id"]: j["state"] for j in self.jobs.state()}
        self.assertEqual((st[a], st[b]), ("running", "queued"))
        self.assertEqual((self.jobs.wait(a, 20), self.jobs.wait(b, 20)), ("done", "done"))
        lines = {j["id"]: j["lines"] for j in self.jobs.state()}
        self.assertEqual((lines[a], lines[b]), (["a1", "a2"], ["b"]))

    def test_cancel_a_waiting_and_a_running_job(self):
        a = self.jobs.add("long", [PY + ["import time; print('started', flush=True); time.sleep(60)"], PY + ["print('never')"]])
        b = self.jobs.add("next", [PY + ["print('never either')"]])
        c = self.jobs.add("after", [PY + ["print('runs')"]])
        for _ in range(100):                                            # until the first command is running
            if any(j["id"] == a and j["lines"] for j in self.jobs.state()):
                break
            time.sleep(0.05)
        self.jobs.cancel(b)
        t = time.time()
        self.jobs.cancel(a)                                             # Ctrl-C to the running command
        self.assertEqual(self.jobs.wait(a, 15), "cancelled")
        self.assertLess(time.time() - t, 5)
        self.assertEqual((self.jobs.wait(b, 5), self.jobs.wait(c, 20)), ("cancelled", "done"))
        lines = {j["id"]: j["lines"] for j in self.jobs.state()}
        self.assertNotIn("never", lines[a])
        self.assertEqual((lines[b], lines[c]), ([], ["runs"]))

    def test_a_failing_command_ends_the_job(self):
        a = self.jobs.add("fails", [PY + ["import sys; print('oops'); sys.exit(3)"], PY + ["print('not run')"]])
        self.assertEqual(self.jobs.wait(a, 20), "failed")
        j = next(j for j in self.jobs.state() if j["id"] == a)
        self.assertEqual((j["rc"], j["lines"]), (3, ["oops"]))

    def test_finished_jobs_are_not_kept_forever(self):
        with mock.patch.object(app, "KEEP_FINISHED", 2):
            ids = [self.jobs.add(f"j{i}", [PY + ["pass"]]) for i in range(4)]
            self.jobs.wait(ids[-1], 30)
            last = self.jobs.add("last", [PY + ["pass"]])
            self.assertEqual([j["id"] for j in self.jobs.state()], ids[-2:] + [last])


    def test_finished_jobs_survive_a_restart(self):
        log = os.path.join(fx.tmpdir("mf-jobs-log-"), "runs", "jobs.json")
        jobs = app.Jobs(fx.tmpdir("mf-jobs-"), log)
        a = jobs.add("first", [PY + ["print('one'); print('two')"]])
        b = jobs.add("second", [PY + ["import sys; sys.exit(2)"]])
        self.assertEqual((jobs.wait(a, 20), jobs.wait(b, 20)), ("done", "failed"))
        again = app.Jobs(fx.tmpdir("mf-jobs-"), log)                    # the next start
        old = again.state()
        self.assertEqual([(j["id"], j["title"], j["state"], j["rc"], j["lines"], j["restored"]) for j in old],
                         [(a, "first", "done", 0, ["one", "two"], True), (b, "second", "failed", 2, [], True)])
        self.assertTrue(all(j["started"] and j["ended"] for j in old))
        c = again.add("third", [PY + ["pass"]])
        self.assertGreater(c, b)                                        # ids go on, so the page never mixes two jobs up
        again.wait(c, 20)
        with mock.patch.object(app, "KEEP_FINISHED", 2):
            d = again.add("fourth", [PY + ["pass"]])
            again.wait(d, 20)
        with open(log) as f:
            self.assertEqual([j["id"] for j in json.load(f)], [c, d])   # capped: the last ones
        with open(log, "w") as f:
            f.write("not json")
        self.assertEqual(app.Jobs(fx.tmpdir("mf-jobs-"), log).state(), [])

    def test_background_jobs_give_way(self):
        bg = self.jobs.add("prepare", [PY + ["import time; print('go', flush=True); time.sleep(60)"]], ("prepare", "r1"), "r1", background=True)
        other = self.jobs.add("prepare", [PY + ["pass"]], ("prepare", "r2"), "r2", background=True)
        self.jobs.give_way("r2")                                        # one run's: the other goes on
        self.assertEqual(self.jobs.wait(other, 5), "cancelled")
        for _ in range(100):
            if any(j["id"] == bg and j["lines"] for j in self.jobs.state()):
                break
            time.sleep(0.05)
        t = time.monotonic()
        self.jobs.close()                                               # Restart: the Prepare is cancelled and waited for
        self.assertLess(time.monotonic() - t, 5)
        self.assertEqual(self.jobs.wait(bg, 1), "cancelled")

    def test_close_waits_for_jobs_then_takes_no_more(self):
        a = self.jobs.add("long", [PY + ["import time; time.sleep(60)"]])
        with self.assertRaises(app.Refused) as e:
            self.jobs.close()
        self.assertEqual((e.exception.status, str(e.exception)[:30]), (423, "1 job is running or waiting: r"))
        self.jobs.cancel(a)
        self.jobs.wait(a, 15)
        self.jobs.close()
        with self.assertRaises(app.Refused):
            self.jobs.add("late", [PY + ["pass"]])


class SsdCopyTest(unittest.TestCase):
    """ssd_copy.py, Copy to SSD's job: at the target there is the old file or the whole new one, never part of one."""

    def setUp(self):
        self.src, self.dst = fx.tmpdir("mf-cp-src-"), os.path.join(fx.tmpdir("mf-cp-ssd-"), "MASHUP - club")
        os.makedirs(os.path.join(self.src, "ableton", "Samples"))
        for rel, data in (("mashup.mp3", b"m" * 5000), ("ableton/a.als", b"a"), ("ableton/Samples/s.wav", b"s"), ("ableton/._a.als", b"x")):
            with open(os.path.join(self.src, rel), "wb") as f:
                f.write(data)

    def test_files_and_folders_are_copied_without_appledouble_files(self):
        ssd_copy.main(self.src, self.dst, "mashup.mp3", "ableton")
        got = sorted(os.path.relpath(os.path.join(d, n), self.dst) for d, _, ns in os.walk(self.dst) for n in ns)
        self.assertEqual(got, ["ableton/Samples/s.wav", "ableton/a.als", "mashup.mp3"])

    def test_the_folders_own_appledouble_file_beside_it_is_removed(self):
        parent = os.path.dirname(self.dst)
        os.makedirs(os.path.join(parent, "MASHUP - dj-tool"))
        for rel in ("._MASHUP - club", "._MASHUP - dj-tool", "MASHUP - dj-tool/._mashup.mp3"):   # what exFAT leaves
            with open(os.path.join(parent, rel), "wb") as f:
                f.write(b"x")
        ssd_copy.main(self.src, self.dst, "mashup.mp3")
        self.assertEqual(sorted(os.listdir(parent)), ["._MASHUP - dj-tool", "MASHUP - club", "MASHUP - dj-tool"])
        self.assertEqual(os.listdir(os.path.join(parent, "MASHUP - dj-tool")), ["._mashup.mp3"])   # other folders untouched

    def test_a_new_pair_folders_own_appledouble_file_is_removed_too(self):
        ssd = os.path.dirname(self.dst)
        dst = os.path.join(ssd, "A x B (1A-1A)", "MASHUP - club")       # pair folder not there yet: the copy makes it
        real = ssd_copy.ditto
        def ditto_like_exfat(s, d):                                       # what exFAT leaves beside the new folders
            real(s, d)
            for f in (os.path.join(ssd, "._A x B (1A-1A)"), os.path.join(ssd, "A x B (1A-1A)", "._MASHUP - club")):
                open(f, "wb").close()
        with open(os.path.join(ssd, "._other"), "wb"):
            pass
        ssd_copy.main(self.src, dst, "mashup.mp3", copy=ditto_like_exfat)
        self.assertEqual(sorted(os.listdir(ssd)), ["._other", "A x B (1A-1A)"])          # other ._ files untouched
        self.assertEqual(os.listdir(os.path.join(ssd, "A x B (1A-1A)")), ["MASHUP - club"])

    def test_an_interrupted_copy_leaves_the_old_file_or_none(self):
        os.makedirs(self.dst)
        with open(os.path.join(self.dst, "mashup.mp3"), "wb") as f:
            f.write(b"old")

        def half(src, dst):                                             # Ctrl-C half-way through a file
            with open(dst, "wb") as f:
                f.write(b"half")
            raise KeyboardInterrupt
        for item in ("mashup.mp3", "ableton"):
            with self.assertRaises(KeyboardInterrupt):
                ssd_copy.main(self.src, self.dst, item, copy=half)
        with open(os.path.join(self.dst, "mashup.mp3"), "rb") as f:
            self.assertEqual(f.read(), b"old")
        self.assertEqual(sorted(os.listdir(self.dst)), ["ableton", "mashup.mp3"])
        self.assertEqual(os.listdir(os.path.join(self.dst, "ableton")), [])     # no temp, no half file

    def test_the_apps_cancel_removes_the_part_file(self):
        code = ("import sys, time; sys.path.insert(0, %r); import ssd_copy\n"
                "def slow(src, dst):\n    open(dst, 'wb').write(b'half'); print('writing', flush=True); time.sleep(60)\n"
                "ssd_copy.main(sys.argv[1], sys.argv[2], 'mashup.mp3', copy=slow)") % os.path.dirname(ssd_copy.__file__)
        jobs = app.Jobs(fx.tmpdir("mf-jobs-"))
        jid = jobs.add("copy", [PY + [code, self.src, self.dst]])
        for _ in range(200):
            if any(j["id"] == jid and "writing" in j["lines"] for j in jobs.state()):
                break
            time.sleep(0.05)
        self.assertEqual(os.listdir(self.dst), [".part-mashup.mp3"])   # mid-copy: only the temp
        jobs.cancel(jid)                                                # SIGINT, as the page's Cancel
        self.assertEqual(jobs.wait(jid, 15), "cancelled")
        self.assertEqual(os.listdir(self.dst), [])


class AppServerTest(unittest.TestCase):
    """One fixture run (brief -> find + check) and one short mashup, made through the app's own jobs."""

    @classmethod
    def setUpClass(cls):
        cls.root, cls.ssd = fx.tmpdir("mf-app-"), fx.tmpdir("mf-ssd-")
        os.makedirs(os.path.join(cls.root, "briefs"))
        with open(os.path.join(cls.root, "briefs", "demo.toml"), "w") as f:
            f.write(BRIEF)
        cls.trash = fx.tmpdir("mf-trash-")
        cls.srv = app.make_server(root=cls.root, ssd=cls.ssd, mf=[sys.executable, "-u", FAKE, cls.root], trash=cls.trash,
                                  claude_check=lambda: {"works": True, "why": "stand-in"})
        cls.port, cls.token, cls.app = cls.srv.server_address[1], cls.srv.token, cls.srv.app
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        code, j = cls.call("POST", "/api/run-brief", {"name": "demo"})
        assert code == 200 and cls.app.jobs.wait(j["job"], 120) == "done", cls.app.jobs.state()
        cls.rn = cls.call("GET", "/api/runs")[1][0]["name"]             # runs/ also holds jobs.json
        cls.call("POST", "/api/mark", {"run": cls.rn, "id": 0, "mark": "keep"})
        code, j = cls.call("POST", "/api/mashup", {"run": cls.rn, "id": 0, "template": "short", "variations": 1})
        assert code == 200 and cls.app.jobs.wait(j["job"], 300) == "done", cls.app.jobs.state()
        cls.mashup = "000-short-v1"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    @classmethod
    def call(cls, method, path, body=None, token=True, host=None, raw=False):
        h = {"X-Token": cls.token} if token else {}
        if host:
            h["Host"] = host
        code, out, _ = request(cls.port, method, path, None if body is None else json.dumps(body).encode(), h)
        return (code, out) if raw else (code, json.loads(out or b"null"))

    def test_page_host_and_token(self):
        code, page = self.call("GET", "/", raw=True)
        self.assertEqual(code, 200)
        self.assertNotIn(self.token.encode(), page)                     # the page gets it from its address (edit.KEY_JS)
        self.assertIn(edit.key_js(app.Handler.key_check).encode(), page)
        self.assertIn(b"[hidden]{display:none!important}", page)       # studio.css, written into the page
        self.assertNotIn(b"/* studio.css */", page)
        self.assertEqual(self.call("GET", "/", token=False, host=f"evil.example:{self.port}", raw=True)[0], 403)
        for path in ("/api/state", "/api/runs", f"/api/run?name={self.rn}"):
            self.assertEqual(self.call("GET", path, token=False)[0], 403, path)
        self.assertEqual(self.call("POST", "/api/run-brief", {"name": "demo"}, token=False)[0], 403)
        for host in ("0.0.0.0", "localhost", ""):
            with self.assertRaises(ValueError):
                app.make_server(host=host, root=self.root)

    def test_the_fixture_run_is_listed_and_auditioned(self):
        code, runs = self.call("GET", "/api/runs")
        self.assertEqual(code, 200)
        self.assertEqual([(r["name"], r["checked"], r["ok"], r["kept"], r["mashups"]) for r in runs], [(self.rn, 2, 1, 1, 1)])
        code, run = self.call("GET", f"/api/run?name={self.rn}")
        self.assertEqual((run["brief"], [p["id"] for p in run["pairs"]], run["marks"]), ("demo", [0, 1], {"0": "keep"}))
        self.assertEqual([(m["dir"], m["has_mix"]) for m in run["mashups"]["0"]], [(self.mashup, True)])
        self.assertLessEqual(abs(run["mashups"]["0"][0]["lufs"] + 10), 1.0)   # read from the mashup's render.json
        preview = run["pairs"][0]["deep"]["preview"].split("/")[-1]
        code, mp3 = self.call("GET", f"/r/{self.rn}/previews/{preview}?t={self.token}", token=False, raw=True)
        self.assertEqual(code, 200)
        self.assertGreater(len(mp3), 1000)

    def test_briefs_save_with_a_backup(self):
        self.assertEqual(self.call("GET", "/api/briefs"), (200, ["demo"]))
        code, j = self.call("POST", "/api/brief", {"name": "second", "text": BRIEF})
        self.assertEqual((code, j["backup"]), (200, None))
        code, j = self.call("POST", "/api/brief", {"name": "second", "text": BRIEF + "# edited\n"})
        code, k = self.call("POST", "/api/brief", {"name": "second", "text": BRIEF + "# again\n"})   # within the same second
        self.assertTrue(j["backup"].startswith("second.") and j["backup"].endswith(".toml.bak"), j)
        self.assertNotEqual(j["backup"], k["backup"])                   # the first backup is not written over
        for name, text in ((j["backup"], BRIEF), (k["backup"], BRIEF + "# edited\n")):
            with open(os.path.join(self.root, "briefs", name)) as f:
                self.assertEqual(f.read(), text)
        self.assertEqual(self.call("GET", "/api/brief?name=second")[1]["text"], BRIEF + "# again\n")
        for bad in ({"name": "second", "text": "name = ["}, {"name": "../evil", "text": BRIEF}, {"name": "a/b", "text": BRIEF}):
            self.assertEqual(self.call("POST", "/api/brief", bad)[0], 400, bad)
        self.assertEqual(sorted(f for f in os.listdir(os.path.join(self.root, "briefs")) if f.endswith(".toml")), ["demo.toml", "second.toml"])

    def test_picks_json_keeps_both_marks_and_export_skips_skipped(self):
        self.call("POST", "/api/mark", {"run": self.rn, "id": 1, "mark": "skip"})
        with open(os.path.join(self.root, "runs", self.rn, "picks.json")) as f:
            picks = json.load(f)
        self.assertEqual([(k["id"], k["mark"]) for k in picks], [(0, "keep"), (1, "skip")])
        self.assertTrue(all(k["inst_path"] and "deep" in k for k in picks))              # what `mf export` reads
        dest = fx.tmpdir("mf-ssd-export-")
        self.app.ssd = dest
        try:
            code, j = self.call("POST", "/api/export", {"run": self.rn})
            self.assertEqual((code, self.app.jobs.wait(j["job"], 120)), (200, "done"))
        finally:
            self.app.ssd = self.ssd
        folders = sorted(n for n in os.listdir(dest) if os.path.isdir(os.path.join(dest, n)))      # one folder: no tempo folders
        self.assertEqual(folders, ["Artist A - Vocal Song x Artist B - Bass Track (8B-10B)"])   # pair 1 was skipped
        self.call("POST", "/api/mark", {"run": self.rn, "id": 1, "mark": None})
        self.assertEqual(self.call("GET", f"/api/run?name={self.rn}")[1]["marks"], {"0": "keep"})

    def test_copy_to_ssd_never_overwrites_unasked_and_never_deletes(self):
        target = self.app.copy_target(self.rn, self.mashup)
        self.assertEqual(target, os.path.join(self.ssd, "Artist A - Vocal Song x Artist B - Bass Track (8B-10B)", "MASHUP - short"))
        code, j = self.call("POST", "/api/copy", {"run": self.rn, "mashup": self.mashup})
        self.assertEqual((code, self.app.jobs.wait(j["job"], 60)), (200, "done"))
        self.assertEqual(sorted(os.listdir(target)), ["ableton", "arrangement.json", "mashup.wav"])     # no mp3 on the SSD
        src = os.path.join(self.root, "runs", self.rn, "mashups", self.mashup)
        with open(os.path.join(src, "mashup.wav"), "rb") as a, open(os.path.join(target, "mashup.wav"), "rb") as b:
            self.assertEqual(a.read(), b.read())
        mine = os.path.join(target, "my notes.txt")                    # something Mark put there himself
        with open(mine, "w") as f:
            f.write("keep me")
        with open(os.path.join(target, "arrangement.json"), "w") as f:
            f.write("changed on the SSD")
        code, j = self.call("POST", "/api/copy", {"run": self.rn, "mashup": self.mashup})
        self.assertEqual(code, 409)                                     # exists: the page must confirm first
        self.assertIn("already exists", j["error"])
        with open(os.path.join(target, "arrangement.json")) as f:
            self.assertEqual(f.read(), "changed on the SSD")            # nothing was written
        code, j = self.call("POST", "/api/copy", {"run": self.rn, "mashup": self.mashup, "confirm": True})
        self.assertEqual((code, self.app.jobs.wait(j["job"], 60)), (200, "done"))
        with open(os.path.join(target, "arrangement.json")) as f, open(os.path.join(src, "arrangement.json")) as g:
            self.assertEqual(f.read(), g.read())                        # written over, as confirmed
        with open(mine) as f:
            self.assertEqual(f.read(), "keep me")                       # and nothing deleted

    def test_a_waiting_copy_checks_its_target_when_it_runs(self):
        self.app.ssd = fx.tmpdir("mf-ssd-race-")
        try:
            self.app.jobs.add("hold", [PY + ["import time; time.sleep(2)"]])   # both copies wait behind this
            calls = [self.call("POST", "/api/copy", {"run": self.rn, "mashup": self.mashup}) for _ in range(2)]
            self.assertEqual([c for c, _ in calls], [200, 200])          # the target was free when both were asked
            self.assertEqual([self.app.jobs.wait(j["job"], 60) for _, j in calls], ["done", "failed"])
            second = next(j for j in self.app.jobs.state() if j["id"] == calls[1][1]["job"])
            self.assertIn("appeared meanwhile", second["lines"][-1])     # not written over without a confirm
        finally:
            self.app.ssd = self.ssd

    def test_copy_to_ssd_leaves_no_appledouble_files_in_the_copied_folder(self):
        self.app.ssd = fx.tmpdir("mf-ssd-dot-")
        planted = os.path.join(self.root, "runs", self.rn, "mashups", self.mashup, "ableton", "._mashup.als")
        with open(planted, "wb") as f:
            f.write(b"\0\5\26\7 AppleDouble")
        self.addCleanup(os.remove, planted)
        try:
            target = self.app.copy_target(self.rn, self.mashup)
            os.makedirs(target)
            for f in (os.path.join(target, "._mashup.mp3"), os.path.join(os.path.dirname(target), "._other")):
                with open(f, "wb") as fh:
                    fh.write(b"\0\5\26\7")
            code, j = self.call("POST", "/api/copy", {"run": self.rn, "mashup": self.mashup, "confirm": True})
            self.assertEqual((code, self.app.jobs.wait(j["job"], 60)), (200, "done"))
            left = [n for _, _, ns in os.walk(target) for n in ns if n.startswith(("._", ".part-"))]
            self.assertEqual(left, [])
            self.assertTrue(os.listdir(os.path.join(target, "ableton")))
            self.assertTrue(os.path.exists(os.path.join(os.path.dirname(target), "._other")))   # outside the copy: untouched
        finally:
            self.app.ssd = self.ssd

    def test_a_briefs_ssd_folder_takes_its_copies(self):
        runs = os.path.join(self.root, "runs")
        other = self.rn + "-club"
        shutil.copytree(os.path.join(runs, self.rn), os.path.join(runs, other))
        self.addCleanup(shutil.rmtree, os.path.join(runs, other), True)
        f = os.path.join(runs, other, "shortlist.json")
        with open(f) as fh:
            s = json.load(fh)
        s["brief"]["ssd_folder"] = "Club night 20 Nov"
        with open(f, "w") as fh:
            json.dump(s, fh)
        self.assertEqual(os.path.relpath(self.app.copy_target(other, self.mashup), self.ssd),
                         "Club night 20 Nov/Artist A - Vocal Song x Artist B - Bass Track (8B-10B)/MASHUP - short")

    def test_the_standalone_audition_page_uses_the_apps_marks(self):
        path = f"/r/{self.rn}/audition.html"
        code, page, r = request(self.port, "GET", path)
        self.assertEqual(code, 200)
        self.assertNotIn(self.token.encode(), page)                      # served: marks go through the API, with
        self.assertIn(edit.key_js(app.Handler.key_check).encode(), page)   # the key the Studio's tab keeps
        self.assertIn(f'RUN = "{self.rn}"'.encode(), page)
        self.assertIn(b'fetch("/api/mark"', page)
        self.assertIn("frame-ancestors 'self'", r.getheader("Content-Security-Policy"))
        self.assertEqual(self.call("GET", "/r/nope/audition.html", token=False, raw=True)[0], 404)
        self.assertEqual(self.call("GET", path, token=False, host=f"evil.example:{self.port}", raw=True)[0], 403)
        self.assertIn(b"SERVED = true", page)
        with open(audition.write(fx.tmpdir("mf-aud-"), [], "x")) as fh:
            self.assertIn("SERVED = false", fh.read())                  # opened as a file: its own marks, in the browser

    def test_a_run_and_a_mashup_go_to_the_trash_when_asked_and_free(self):
        runs, copy = os.path.join(self.root, "runs"), self.rn + "-copy"
        shutil.copytree(os.path.join(runs, self.rn), os.path.join(runs, copy))
        self.addCleanup(shutil.rmtree, os.path.join(runs, copy), True)
        body = {"run": copy, "mashup": self.mashup}
        code, j = self.call("POST", "/api/delete-mashup", body)
        self.assertEqual(code, 409)                                      # the page confirms first
        self.assertIn("to the Trash?", j["error"])
        hold = self.app.jobs.add("hold", [PY + ["import time; time.sleep(60)"]])
        try:
            code, c = self.call("POST", "/api/copy", {**body, "confirm": True})   # waits behind hold
            self.assertEqual(code, 200)
            self.assertEqual(self.call("POST", "/api/delete-mashup", {**body, "confirm": True})[0], 423)
            self.assertEqual(self.call("POST", "/api/delete-run", {"run": copy, "confirm": True})[0], 423)
            self.app.jobs.cancel(c["job"])
        finally:
            self.app.jobs.cancel(hold)
        code, j = self.call("POST", "/api/delete-mashup", {**body, "confirm": True})
        self.assertEqual(code, 200)
        self.assertTrue(os.path.isfile(os.path.join(j["to"], "arrangement.json")))   # moved whole, not deleted
        self.assertFalse(os.path.exists(os.path.join(runs, copy, "mashups", self.mashup)))
        self.assertEqual(self.call("POST", "/api/delete-run", {"run": copy})[0], 409)
        code, j = self.call("POST", "/api/delete-run", {"run": copy, "confirm": True})
        self.assertEqual((code, j["to"]), (200, os.path.join(self.trash, copy)))
        self.assertTrue(os.path.isfile(os.path.join(j["to"], "shortlist.json")))
        self.assertNotIn(copy, [r["name"] for r in self.call("GET", "/api/runs")[1]])
        shutil.copytree(os.path.join(runs, self.rn), os.path.join(runs, copy))
        code, j = self.call("POST", "/api/delete-run", {"run": copy, "confirm": True})
        self.assertEqual(j["to"], os.path.join(self.trash, copy + " 2"))  # the first one stays in the Trash
        for bad in ("jobs.json", "..", "nope"):
            self.assertEqual(self.call("POST", "/api/delete-run", {"run": bad, "confirm": True})[0], 404, bad)

    def test_a_run_a_find_and_check_is_writing_is_not_deleted(self):
        runs, older, newer = os.path.join(self.root, "runs"), self.rn + "-older", self.rn + "-newer"
        shutil.copytree(os.path.join(runs, self.rn), os.path.join(runs, older))
        for r in (older, newer):
            self.addCleanup(shutil.rmtree, os.path.join(runs, r), True)
        time.sleep(0.05)
        hold = self.app.jobs.add("run brief demo", [PY + ["import time; time.sleep(60)"]], ("brief", "demo"), app.NEW_RUN)
        try:
            for _ in range(200):
                if any(j["state"] == "running" for j in self.app.jobs.active()):
                    break
                time.sleep(0.05)
            os.makedirs(os.path.join(runs, newer))                       # as the find + check makes its run
            shutil.copy(os.path.join(runs, self.rn, "shortlist.json"), os.path.join(runs, newer))
            self.assertEqual(self.call("POST", "/api/delete-run", {"run": newer})[0], 423)
            self.assertEqual(self.call("POST", "/api/delete-run", {"run": older})[0], 409)   # made before it: free, asks
            os.utime(os.path.join(runs, older, "shortlist.json"))        # as a run reused within the same minute
            self.assertEqual(self.call("POST", "/api/delete-run", {"run": older})[0], 423)
        finally:
            self.app.jobs.cancel(hold)

    def test_prepare_checks_the_next_pairs_ahead_and_gives_way(self):
        runs, more = os.path.join(self.root, "runs"), self.rn + "-more"
        shutil.copytree(os.path.join(runs, self.rn), os.path.join(runs, more))
        self.addCleanup(shutil.rmtree, os.path.join(runs, more), True)
        f = os.path.join(runs, more, "shortlist.json")
        with open(f) as fh:
            s = json.load(fh)
        s["pairs"] += [{**s["pairs"][0], "id": i} for i in range(2, 7)]  # five more pairs, unchecked
        with open(f, "w") as fh:
            json.dump(s, fh)
        code, j = self.call("POST", "/api/prepare", {"run": more})
        self.assertEqual(code, 200)
        self.assertIsNotNone(j["job"])
        self.assertIsNone(self.call("POST", "/api/prepare", {"run": more})[1]["job"])   # one at a time
        for _ in range(200):                                            # while it runs, the page is told what it checks
            st = self.call("GET", "/api/state")[1]
            if st["checking"]:
                break
            time.sleep(0.05)
        self.assertEqual((st["checking"]["run"], st["checking"]["total"]), (more, 5))   # the 2 checked + the next 3
        self.assertEqual(self.app.jobs.wait(j["job"], 60), "done")
        with open(os.path.join(runs, more, "results.json")) as fh:
            self.assertEqual(sorted(p["id"] for p in json.load(fh)), [0, 1, 2, 3, 4])
        jid = self.call("POST", "/api/prepare", {"run": more})[1]["job"]
        for _ in range(200):
            if any(x["id"] == jid and x["state"] == "running" for x in self.app.jobs.state()):
                break
            time.sleep(0.05)
        other = self.app.jobs.add("the user's own job", [PY + ["pass"]])   # any normal job: Prepare gives way
        self.assertEqual((self.app.jobs.wait(jid, 20), self.app.jobs.wait(other, 20)), ("cancelled", "done"))
        with open(os.path.join(runs, more, "results.json")) as fh:
            res = json.load(fh)
        render.write_json(os.path.join(runs, more, "results.json"), [p for p in res if p["id"] not in (2, 3)])   # as a cut-off check
        jid = self.call("POST", "/api/prepare", {"run": more})[1]["job"]
        self.assertEqual(self.app.jobs.wait(jid, 60), "done")
        with open(os.path.join(runs, more, "results.json")) as fh:
            self.assertEqual(sorted(p["id"] for p in json.load(fh)), [0, 1, 2, 3, 4, 5])   # the 2 gaps and 1 new: 3 checks
        hold = self.call("POST", "/api/prepare", {"run": more})[1]["job"]          # the last pair, then nothing left
        self.assertEqual(self.app.jobs.wait(hold, 60), "done")
        self.assertEqual(self.call("POST", "/api/prepare", {"run": more})[1],
                         {"ok": True, "job": None, "why": "every pair of the shortlist is checked"})

    def test_a_find_and_check_names_its_run_while_it_checks(self):
        with open(os.path.join(self.root, "briefs", "live.toml"), "w") as f:
            f.write('name = "live"\n\n[inst]\ntempo = [126]\n\n[match]\ntop = 4\n')   # fake mf: 4 to check (and 3 more)
        self.addCleanup(os.remove, os.path.join(self.root, "briefs", "live.toml"))
        jid = self.call("POST", "/api/run-brief", {"name": "live"})[1]["job"]
        seen = []
        while any(j["id"] == jid and j["state"] in ("queued", "running") for j in self.app.jobs.state()):
            c = self.call("GET", "/api/state")[1]["checking"]
            if c:
                seen.append((c["run"], c["done"], c["total"]))
                self.assertEqual(c["job"], jid)                         # the job it belongs to (Surprise me lands on its own)
            time.sleep(0.1)
        self.assertEqual(self.app.jobs.wait(jid, 60), "done")
        run = next(r["name"] for r in self.call("GET", "/api/runs")[1] if r["name"].endswith("-live"))
        self.addCleanup(shutil.rmtree, os.path.join(self.root, "runs", run), True)
        self.assertTrue(seen and {r for r, _, _ in seen} == {run}, seen)  # the new run, nothing else
        self.assertEqual({t for _, _, t in seen}, {4})
        self.assertGreaterEqual(max(d for _, d, _ in seen), 3)           # done grows as pairs pass
        self.assertIsNone(self.call("GET", "/api/state")[1]["checking"])

    def test_make_all_kept_and_copy_all_queue_normal_jobs(self):
        code, j = self.call("POST", "/api/make-all", {"run": self.rn, "template": "short"})
        self.assertEqual((code, j["jobs"], len(j["skipped"])), (200, [], 1))
        self.assertTrue(j["skipped"][0].startswith("pair 0: 000-short-v1 already made"), j)          # never made again unasked
        hold = self.app.jobs.add("hold", [PY + ["import time; time.sleep(60)"]])
        try:
            code, j = self.call("POST", "/api/make-all", {"run": self.rn, "template": "dj-tool"})
            self.assertEqual((code, len(j["jobs"]), j["skipped"]), (200, 1, []))
            self.app.jobs.cancel(j["jobs"][0])
            hq = self.call("POST", "/api/mashup", {"run": self.rn, "id": 0, "template": "club", "variations": 1, "hq": True})[1]["job"]
            self.assertEqual(next(x for x in self.app.jobs.all if x["id"] == hq)["cmds"][0][-1], "--hq-stems")   # better stems, asked
            self.app.jobs.cancel(hq)
        finally:
            self.app.jobs.cancel(hold)
        self.app.ssd = fx.tmpdir("mf-ssd-all-")
        try:
            self.assertEqual([m["on_ssd"] for m in self.call("GET", "/api/mashups")[1]], [False])
            code, j = self.call("POST", "/api/copy-all", {"run": self.rn})
            self.assertEqual((code, len(j["jobs"]), j["skipped"]), (200, 1, []))
            self.assertEqual(self.app.jobs.wait(j["jobs"][0], 60), "done")
            skipped = self.call("POST", "/api/copy-all", {"run": self.rn})[1]["skipped"]
            self.assertEqual(len(skipped), 1)
            self.assertIn("000-short-v1: ", skipped[0])
            self.assertIn("already exists", skipped[0])                  # never written over unasked
            self.assertEqual([m["on_ssd"] for m in self.call("GET", "/api/mashups")[1]], [True])
            self.app.ssd = os.path.join(self.root, "no-such-drive")
            self.assertEqual([m["on_ssd"] for m in self.call("GET", "/api/mashups")[1]], [None])   # can't tell
        finally:
            self.app.ssd = self.ssd

    def test_a_rating_is_kept_with_the_mashup_and_feeds_the_match(self):
        folder = os.path.join(self.root, "runs", self.rn, "mashups", self.mashup)
        self.addCleanup(lambda: os.path.exists(os.path.join(folder, "rating.json")) and os.remove(os.path.join(folder, "rating.json")))
        self.assertEqual(self.call("POST", "/api/rate", {"run": self.rn, "mashup": self.mashup, "rating": 4}), (200, {"ok": True, "rating": 4}))
        self.assertEqual([m["rating"] for m in self.call("GET", "/api/mashups")[1]], [4])
        self.assertEqual(cli.ratings(os.path.join(self.root, "runs"))[0][2], 4)           # what mf find learns from
        self.call("POST", "/api/rate", {"run": self.rn, "mashup": self.mashup, "rating": None})
        self.assertEqual(([m["rating"] for m in self.call("GET", "/api/mashups")[1]], cli.ratings(os.path.join(self.root, "runs"))), ([None], []))
        for bad in (0, 6, 2.5, "5", True):
            self.assertEqual(self.call("POST", "/api/rate", {"run": self.rn, "mashup": self.mashup, "rating": bad})[0], 400, bad)

    def test_a_clip_is_30_s_from_the_loudest_section_start(self):
        import soundfile as sf
        folder = os.path.join(self.root, "runs", self.rn, "mashups", self.mashup)
        self.addCleanup(lambda: os.path.exists(os.path.join(folder, "clip.mp3")) and os.remove(os.path.join(folder, "clip.mp3")))
        code, j = self.call("POST", "/api/clip", {"run": self.rn, "mashup": self.mashup})
        self.assertEqual(code, 200, j)
        with open(os.path.join(folder, "arrangement.json")) as f:
            arr = json.load(f)
        y, sr = sf.read(os.path.join(folder, "mashup.wav"), dtype="float32", always_2d=True)
        bar = 4 * 60 / arr["output_bpm"]
        starts = [s["out_bar"] * bar for s in arr["sections"] if (s["out_bar"] * bar + 30) * sr <= len(y)]
        loudness = {t: float((y[int(round(t * sr)):int(round(t * sr)) + 30 * sr] ** 2).mean()) for t in starts}
        self.assertAlmostEqual(j["start"], max(loudness, key=loudness.get), delta=0.02)   # a section start, the loudest
        info = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                               os.path.join(folder, "clip.mp3")], capture_output=True, text=True).stdout
        self.assertAlmostEqual(float(info), 30.0, delta=0.1)
        self.assertTrue(self.call("GET", "/api/mashups")[1][0]["has_clip"])
        code, mp3 = self.call("GET", f"/m/{self.rn}/{self.mashup}/clip.mp3?t={self.token}", token=False, raw=True)
        self.assertEqual((code, mp3[:3] in (b"ID3", b"\xff\xfb")), (200, True))
        hold = self.app.jobs.add("hold", [PY + ["import time; time.sleep(60)"]])
        try:                                                            # not while it is copied (or made)
            c = self.call("POST", "/api/copy", {"run": self.rn, "mashup": self.mashup, "confirm": True})[1]["job"]
            self.assertEqual(self.call("POST", "/api/clip", {"run": self.rn, "mashup": self.mashup})[0], 423)
            self.app.jobs.cancel(c)
        finally:
            self.app.jobs.cancel(hold)

    def test_make_again_asks_first_and_keeps_the_edited_arrangement(self):
        base, folder = f"/m/{self.rn}/{self.mashup}/", os.path.join(self.root, "runs", self.rn, "mashups", self.mashup)
        lanes = self.call("GET", base + "api/arrangement")[1]["arrangement"]["lanes"]
        lanes["drums"][0]["gain_db"] = -7.5                             # an edit made in the editor
        self.assertEqual(self.call("POST", base + "api/save", {"lanes": lanes})[0], 200)
        body = {"run": self.rn, "id": 0, "template": "short", "variations": 1}
        code, j = self.call("POST", "/api/mashup", body)
        self.assertEqual(code, 409)
        self.assertIn("already made", j["error"])
        before = set(os.listdir(folder))
        code, j = self.call("POST", "/api/mashup", {**body, "confirm": True})
        self.assertEqual(code, 200)
        self.assertEqual(self.call("POST", "/api/copy", {"run": self.rn, "mashup": self.mashup})[0], 423)   # not while it is made
        self.assertEqual(self.call("POST", "/api/mashup", {**body, "confirm": True})[0], 423)               # nor made twice at once
        self.assertEqual(self.app.jobs.wait(j["job"], 300), "done")
        new = set(os.listdir(folder)) - before
        self.assertEqual(len(new), 1, new)                               # the backup
        with open(os.path.join(folder, new.pop())) as f:
            self.assertEqual(json.load(f)["lanes"]["drums"][0]["gain_db"], -7.5)   # the edit survives in it
        with open(os.path.join(folder, "arrangement.json")) as f:
            self.assertEqual(json.load(f)["lanes"]["drums"][0]["gain_db"], 0.0)    # the new arrangement replaced it

    def test_the_editor_waits_for_a_make_or_a_copy_of_its_mashup(self):
        base = f"/m/{self.rn}/{self.mashup}/"
        lanes = self.call("GET", base + "api/arrangement")[1]["arrangement"]["lanes"]
        hold = self.app.jobs.add("hold", [PY + ["import time; time.sleep(60)"]])   # what follows waits behind it
        try:
            code, j = self.call("POST", "/api/mashup", {"run": self.rn, "id": 0, "template": "short", "variations": 1, "confirm": True})
            self.assertEqual(code, 200)
            self.assertEqual(self.call("POST", base + "api/save", {"lanes": lanes})[0], 423)   # it would be replaced unseen
            self.assertEqual(self.call("POST", base + "api/render", {})[0], 423)
            code, k = self.call("POST", "/api/mashup", {"run": self.rn, "id": 0, "template": "dj-tool", "variations": 1})
            self.assertEqual(code, 200)                                  # another template of the pair still queues
            for jid in (j["job"], k["job"]):
                self.app.jobs.cancel(jid)
            code, c = self.call("POST", "/api/copy", {"run": self.rn, "mashup": self.mashup, "confirm": True})
            self.assertEqual(code, 200)
            self.assertEqual(self.call("POST", base + "api/save", {"lanes": lanes})[0], 423)   # nor while it is copied
            self.app.jobs.cancel(c["job"])
            self.assertEqual(self.call("POST", base + "api/save", {"lanes": lanes})[0], 200)   # free again
        finally:
            self.app.jobs.cancel(hold)

    def test_a_mashup_is_named_by_its_template_file(self):
        # A template file club-long.toml copied from club.toml still says name = "club" inside; engine.cli names the
        # folder after the file, and so must the confirm and the SSD folder.
        mashups = os.path.join(self.root, "runs", self.rn, "mashups")
        other = os.path.join(mashups, "000-club-long-v1")
        os.makedirs(other)
        self.addCleanup(shutil.rmtree, other)
        shutil.copy(os.path.join(mashups, self.mashup, "arrangement.json"), other)   # says template "short" inside
        self.assertEqual(os.path.basename(self.app.copy_target(self.rn, "000-club-long-v1")), "MASHUP - club-long")
        with mock.patch.object(app.App, "templates", staticmethod(lambda: ["club-long", "short"])):
            code, j = self.call("POST", "/api/mashup", {"run": self.rn, "id": 0, "template": "club-long", "variations": 1})
        self.assertEqual(code, 409)                                      # Make asks before replacing it
        self.assertIn("000-club-long-v1", j["error"])

    def test_a_template_name_engine_cleans_up_is_still_matched(self):
        # engine.cli writes "club (long)" as "club -long-" into the folder name: Make, the editor and Copy must agree
        mashups = os.path.join(self.root, "runs", self.rn, "mashups")
        other = os.path.join(mashups, f"000-{engine._safe('club (long)')}-v1")
        os.makedirs(other)
        self.addCleanup(shutil.rmtree, other)
        shutil.copy(os.path.join(mashups, self.mashup, "arrangement.json"), other)
        body = {"run": self.rn, "id": 0, "template": "club (long)", "variations": 1}
        hold = self.app.jobs.add("hold", [PY + ["import time; time.sleep(60)"]])
        try:
            with mock.patch.object(app.App, "templates", staticmethod(lambda: ["club (long)", "short"])):
                self.assertEqual(self.call("POST", "/api/mashup", body)[0], 409)        # it asks first
                code, j = self.call("POST", "/api/mashup", {**body, "confirm": True})
            self.assertEqual(code, 200)
            base = f"/m/{self.rn}/{quote(os.path.basename(other))}/"            # as the page encodes it
            self.assertEqual(self.call("POST", base + "api/render", {})[0], 423)       # the editor waits for it
            self.app.jobs.cancel(j["job"])
        finally:
            self.app.jobs.cancel(hold)

    def test_a_missing_ssd_is_refused(self):
        self.app.ssd = os.path.join(self.root, "no-such-drive")
        try:
            for path, body in (("/api/export", {"run": self.rn}), ("/api/copy", {"run": self.rn, "mashup": self.mashup})):
                code, j = self.call("POST", path, body)
                self.assertEqual(code, 503, path)
                self.assertIn("is not there: connect its drive", j["error"])
            self.assertFalse(self.call("GET", "/api/state")[1]["ssd"]["ok"])
        finally:
            self.app.ssd = self.ssd

    def test_paths_are_checked(self):
        for path in (f"/r/{self.rn}/previews/..%2F..%2Fshortlist.json", f"/r/{self.rn}/shortlist.json", "/r/../briefs/demo.toml",
                     "/m/../briefs/x/", f"/m/{self.rn}/../shortlist.json", f"/m/{self.rn}/{self.mashup}/blocks/..%2Farrangement.json",
                     "/api/run?name=..", "/api/brief?name=../briefs/demo", "/api/run?name=%2Fetc"):
            code, _ = self.call("GET", path + ("&" if "?" in path else "?") + f"t={self.token}", raw=True)
            self.assertIn(code, (400, 403, 404), path)
        for body in ({"run": "..", "id": 0, "mark": "keep"}, {"run": self.rn, "id": 99, "mark": "keep"}, {"run": self.rn, "id": 0, "mark": "love"}):
            self.assertIn(self.call("POST", "/api/mark", body)[0], (400, 404), body)
        for body in ({"run": self.rn, "id": 0, "template": "../x", "variations": 1}, {"run": self.rn, "id": 1, "template": "club", "variations": 1},
                     {"run": self.rn, "id": 0, "template": "club", "variations": 99}, {"run": self.rn, "id": 0, "template": "club", "variations": 1, "tempo": "fast"}):
            self.assertEqual(self.call("POST", "/api/mashup", body)[0], 400, body)

    def test_the_editor_works_inside_the_app(self):
        base = f"/m/{self.rn}/{self.mashup}/"
        code, page, r = request(self.port, "GET", base)
        self.assertEqual(code, 200)
        self.assertNotIn(self.token.encode(), page)
        self.assertIn(edit.key_js(app.Handler.key_check).encode(), page)   # the frame shares the tab's storage
        self.assertIn(b'api("api/arrangement")', page)                 # relative: works under any prefix
        self.assertIn("frame-ancestors 'self'", r.getheader("Content-Security-Policy"))   # the Editor tab may frame it
        code, j = self.call("GET", base + "api/arrangement")
        self.assertEqual((code, j["folder"]), (200, self.mashup))
        lanes = j["arrangement"]["lanes"]
        code, j = self.call("POST", base + "api/save", {"lanes": lanes})
        self.assertEqual(code, 200)
        self.assertTrue(os.path.exists(os.path.join(self.root, "runs", self.rn, "mashups", self.mashup, j["backup"])))
        self.assertEqual(self.call("GET", base + "api/arrangement", token=False)[0], 403)

    def test_get_stays_fast_while_a_job_runs(self):                     # done-when 3
        jid = self.app.jobs.add("busy", [PY + ["import time\nt = time.time()\nwhile time.time() - t < 4: sum(range(10000))"]])
        time.sleep(0.5)
        times = []
        for _ in range(20):
            t = time.perf_counter()
            self.assertEqual(self.call("GET", "/api/state")[0], 200)
            times.append(time.perf_counter() - t)
        self.assertEqual(next(j for j in self.app.jobs.state() if j["id"] == jid)["state"], "running")
        self.assertLess(max(times), 0.2, times)
        self.app.jobs.wait(jid, 30)


class QuitTest(unittest.TestCase):
    def test_quit_stops_the_server_and_its_job(self):
        srv = app.make_server(root=fx.tmpdir("mf-quit-"), ssd=fx.tmpdir("mf-quit-ssd-"))
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        jid = srv.app.jobs.add("long", [PY + ["import time; print('go', flush=True); time.sleep(60)"]])
        time.sleep(1)
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        c.request("POST", "/api/quit", body=b"{}", headers={"Host": f"127.0.0.1:{srv.server_address[1]}", "X-Token": srv.token})
        self.assertEqual(c.getresponse().status, 200)
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertEqual(srv.app.jobs.wait(jid, 15), "cancelled")
        srv.server_close()


class CountTest(unittest.TestCase):
    """The Briefs page's live count: a brief as typed, matched against a fixture catalog (never the real one)."""

    def test_the_live_count_reads_the_catalog_once_and_writes_nothing(self):
        lib, loads = fx.tmpdir("mf-count-lib-"), []

        def load():
            loads.append(1)
            with fx.fixture_catalog(lib):
                return catalog.Catalog()
        root = fx.tmpdir("mf-count-")
        srv = app.make_server(root=root, ssd=root, load_catalog=load)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        port = srv.server_address[1]

        def count(text):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("POST", "/api/count", body=json.dumps({"text": text}).encode(),
                      headers={"Host": f"127.0.0.1:{port}", "X-Token": srv.token})
            r = c.getresponse()
            return r.status, json.loads(r.read())
        t = time.perf_counter()
        code, c = count('name = "x"\n[song]\ntext = "a view to a kill"\nrole = "vocal"\n[inst]\ntempo = [126]\n')
        self.assertLess(time.perf_counter() - t, 2)                     # the first count includes reading the catalog
        self.assertEqual(code, 200)
        self.assertRegex(c["line"], r"^2 vocals × 14 beats → \d+ pairs?$")
        self.assertEqual((c["why"], c["song"]["matches"]), (None, 2))
        self.assertEqual(c["song"]["hint"], "2 tracks match: Duran Duran - A View To A Kill · Duran Duran - A View To A Kill (Extended Mix)")
        code, c = count('name = "x"\n[song]\ntext = "no such song"\n')
        self.assertEqual((code, c["pairs"], c["why"], c["song"]["matches"]), (200, 0, "no track matches the song", 0))
        self.assertEqual(count('[inst]\ntempo = [150]\n')[1]["why"], "no beat passes the [inst] filters")
        for bad in ("tempo = [", '[inst]\ntempo = "fast"\n', 'song = "x"\n', '[song]\ntext = "x"\nrole = "inst"\n',
                    '[inst]\ntempo = [126]\n[match]\nlimit = "lots"\n', '[inst]\ntempo = [126]\ntempo_tol = "4"\n'):
            self.assertEqual(count(bad)[0], 400, bad)
        self.assertEqual(len(loads), 1)                                 # read once, kept
        self.assertEqual(os.listdir(root), [])                         # no run folder, nothing written

    def test_an_empty_catalog_is_said_and_read_again(self):             # the music drive was not plugged in yet
        loads = []
        a = app.App(root=fx.tmpdir("mf-count-"), ssd="/nonexistent", load_catalog=lambda: loads.append(1) or mock.Mock(n=0))
        for _ in range(2):
            with self.assertRaises(ValueError) as e:
                a.count('[inst]\ntempo = [126]\n')
            self.assertIn("is the music drive plugged in", str(e.exception))
        self.assertEqual(len(loads), 2)


class SurpriseTest(unittest.TestCase):
    def test_a_strong_vocal_is_picked_and_a_small_run_queued(self):
        lib, root = fx.tmpdir("mf-surprise-lib-"), fx.tmpdir("mf-surprise-")

        def load():
            with fx.fixture_catalog(lib):
                return catalog.Catalog()
        a = app.App(root=root, ssd=root, mf=[sys.executable, "-u", FAKE, root], load_catalog=load)
        offered = []
        r = a.surprise(pick=lambda pool: offered.append(len(pool)) or pool[0])
        self.assertTrue(offered[0] > 0)
        self.assertNotIn("Instrumental", r["vocal"])                   # a vocal source, by a known artist
        self.assertEqual(a.jobs.wait(r["job"], 60), "done")
        runs = [n for n in os.listdir(os.path.join(root, "runs")) if os.path.isdir(os.path.join(root, "runs", n))]
        self.assertEqual(len(runs), 1)
        self.assertIn("surprise-", runs[0])
        with open(os.path.join(root, "runs", runs[0], "shortlist.json")) as f:
            args = json.load(f)["args"]
        self.assertEqual((args["seed"], args["role"], args["top"]), (r["vocal"], "vocal", 3))
        self.assertEqual(len(a.run(runs[0])["pairs"]), 3)               # three checked


class RestartTest(unittest.TestCase):
    @staticmethod
    def post(port, token, path):
        code, out, _ = request(port, "POST", path, b"{}", {"X-Token": token}, timeout=10)
        return code, json.loads(out)

    def test_restart_waits_for_jobs_then_stops_the_server(self):
        srv = app.make_server(root=fx.tmpdir("mf-rs-"), ssd=fx.tmpdir("mf-rs-ssd-"))
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        port = srv.server_address[1]
        jid = srv.app.jobs.add("long", [PY + ["import time; time.sleep(60)"]])
        code, j = self.post(port, srv.token, "/api/restart")
        self.assertEqual(code, 423)
        self.assertIn("running or waiting", j["error"])
        srv.app.jobs.cancel(jid)
        srv.app.jobs.wait(jid, 15)
        held, release = threading.Event(), threading.Event()            # an editor render holds its folder's lock
        lock = srv.app._lock("/some/mashup")
        threading.Thread(target=lambda: (lock.acquire(), held.set(), release.wait(), lock.release()), daemon=True).start()
        held.wait(5)
        code, j = self.post(port, srv.token, "/api/restart")
        self.assertEqual(code, 423)
        self.assertIn("the editor is saving or rendering", j["error"])
        release.set()
        for _ in range(100):                                            # until the render's thread has let go
            if srv.app._free("/some/mashup"):
                break
            time.sleep(0.02)
        code, j = self.post(port, srv.token, "/api/restart")
        self.assertEqual((code, j["key"]), (200, srv.next_token))      # the page reconnects with it (minted once)
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertTrue(srv.restart)
        srv.server_close()

    def test_a_restart_whose_answer_is_lost_still_stops(self):
        srv = app.make_server(root=fx.tmpdir("mf-rs-"), ssd=fx.tmpdir("mf-rs-ssd-"))
        srv.handle_error = lambda *a: None                              # the lost answer's traceback
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()

        def lost(handler, fn):                                          # the page went away before the answer
            fn()
            raise BrokenPipeError("the page is gone")
        with mock.patch.object(app.Handler, "_answer", lost):
            with self.assertRaises((http.client.HTTPException, OSError)):
                self.post(srv.server_address[1], srv.token, "/api/restart")
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertTrue(srv.restart)
        srv.server_close()

    def test_restart_runs_the_same_command_again_in_the_same_process(self):
        root = fx.tmpdir("mf-rs-")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        code = ("import sys, webbrowser; sys.path.insert(0, %r); from mashup_finder import app; "
                "webbrowser.open = lambda url: print('browser', url, flush=True); app.serve(%d, root=%r, ssd=%r)") % (ROOT, port, root, root)
        p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(p.stdout.close)
        self.addCleanup(p.kill)
        first = next(line for line in p.stdout if line.startswith("browser ")).split("#key=")[1].strip()

        code, out, r = request(port, "POST", "/api/restart", b"{}", {"X-Token": first}, timeout=10)
        second = json.loads(out)["key"]                                 # the open page reconnects with it
        self.assertEqual(code, 200)
        self.assertNotEqual(second, first)
        for _ in range(300):
            try:
                if request(port, "GET", "/api/state", headers={"X-Token": second}, timeout=5)[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(0.1)
        else:
            self.fail("the restarted Studio never answered to the next key")
        self.assertNotIn(second.encode(), request(port, "GET", "/")[1])
        self.assertIsNone(p.poll())                                     # the same process, started again
        self.assertEqual(self.post(port, second, "/api/quit")[0], 200)
        self.assertEqual(p.wait(15), 0)
        out = p.stdout.read()
        self.assertIn("Restarting Mashup Studio", out)
        self.assertNotIn("browser ", out)                               # no second tab: the open page reconnects
        self.assertNotIn(second, out)


class OneStudioTest(unittest.TestCase):
    def test_a_second_start_opens_the_running_studio(self):
        srv = app.make_server(root=fx.tmpdir("mf-one-"), ssd=fx.tmpdir("mf-one-ssd-"))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port, logs = srv.server_address[1], []
        try:
            opened = threading.Event()
            with mock.patch.object(app.webbrowser, "open", side_effect=lambda url: opened.set()) as browser:
                app.serve(port, log=logs.append, root=fx.tmpdir("mf-one-"), ssd=fx.tmpdir("mf-one-ssd-"))
                self.assertTrue(opened.wait(5))
            browser.assert_called_once_with(f"http://127.0.0.1:{port}/#key={srv.token}")   # by the running Studio
            self.assertEqual(logs, [f"Mashup Studio is already running: http://127.0.0.1:{port}/"])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_a_studio_that_will_not_open_its_page_is_not_opened_for_it(self):
        class Refuses(http.server.BaseHTTPRequestHandler):           # says it is a Studio (or holds its port), refuses
            server_version, sys_version = "mf-app", "Python/3.12"

            def do_HEAD(self):
                self.send_response(200)
                self.end_headers()

            def do_POST(self):
                self.send_response(403)
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), Refuses)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        logs = []
        with mock.patch.object(app.webbrowser, "open") as browser:
            app.serve(srv.server_address[1], log=logs.append, root=fx.tmpdir("mf-one-"), ssd=fx.tmpdir("mf-one-ssd-"))
        browser.assert_not_called()                                     # its page could be anyone's
        self.assertIn("did not open its page", logs[-1])
        self.assertIn(f"lsof -nP -iTCP:{srv.server_address[1]}", logs[-1])  # how to see whose it is, not its address

    def test_only_a_second_start_can_ask_the_studio_to_open_its_page(self):
        srv = app.make_server(root=fx.tmpdir("mf-one-"), ssd=fx.tmpdir("mf-one-ssd-"))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        port = srv.server_address[1]

        ask = lambda method, headers: request(port, method, "/api/open-page", headers=headers, timeout=10)[:2]
        calls, release = [], threading.Event()

        def slow(url):                                                  # a browser that takes its time to open
            calls.append(url)
            release.wait(10)
        with mock.patch.object(app.webbrowser, "open", side_effect=slow):
            self.assertEqual(ask("POST", {})[0], 403)                   # a form on another site cannot set the header
            self.assertEqual(ask("OPTIONS", {"Origin": "http://evil.example"})[0], 501)   # nor ask for it: no CORS
            self.assertEqual(ask("POST", {"X-Open-Page": "1", "Host": f"evil.example:{port}"})[0], 403)
            self.assertEqual(calls, [])
            code, body = ask("POST", {"X-Open-Page": "1"})              # answered while the browser still opens
            self.assertEqual(ask("POST", {"X-Open-Page": "1"})[0], 200)  # a repeat within 2 s opens nothing more
            release.set()
            for _ in range(50):
                if calls:
                    break
                time.sleep(0.1)
        self.assertEqual(code, 200)
        self.assertNotIn(srv.token.encode(), body)                      # the key goes to the browser only
        self.assertEqual(calls, [f"http://127.0.0.1:{port}/#key={srv.token}"])

    def test_a_port_another_program_holds_is_refused(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()

            def not_http():
                c, _ = s.accept()
                c.recv(1024)
                c.sendall(b"HELLO\r\n")
                c.close()
            threading.Thread(target=not_http, daemon=True).start()
            with self.assertRaises(SystemExit) as e:
                app.serve(s.getsockname()[1], open_browser=False, root=fx.tmpdir("mf-one-"), ssd=fx.tmpdir("mf-one-ssd-"))
        self.assertIn("--port", str(e.exception))

    def test_the_server_header_names_the_mac_apps_build_when_it_started_the_studio(self):
        header = "from mashup_finder import app; print(app.Handler.server_version + ' ' + app.Handler.sys_version)"
        env = {k: v for k, v in os.environ.items() if k != "MASHUP_STUDIO_BUILD"}
        run = lambda env: subprocess.run(PY + [header], cwd=ROOT, env=env, capture_output=True, text=True).stdout.strip()
        self.assertRegex(run(env), r"^mf-app Python/3")                              # mf app, a source install
        self.assertEqual(run({**env, "MASHUP_STUDIO_BUILD": "app/1.0.0 ab12"}), "mf-app app/1.0.0 ab12")


class CliTest(unittest.TestCase):
    def test_pair_folder_is_what_export_uses(self):
        p = {"vocal_label": "A: B", "inst_label": "C/D", "vocal_key": "C 8B", "inst_key": "D 10B", "inst_bpm": 87.0, "deep": {"bpm_b": 174.0}}
        self.assertEqual(cli._pair_folder(p, {"inst": {"tempo": [140, 180]}}), ("", "A- B x C-D (8B-10B)"))   # one folder: no tempo folders
        self.assertEqual(cli._pair_folder(p, None), ("", "A- B x C-D (8B-10B)"))

    def test_a_briefs_ssd_folder_is_one_plain_folder(self):
        p = {"vocal_label": "A", "inst_label": "C", "vocal_key": "C 8B", "inst_key": "D 10B", "inst_bpm": 87.0, "deep": {"bpm_b": 174.0}}
        for given, want in (("Club night 20 Nov", "Club night 20 Nov"), ("../x", "-x"), ("..", ""), ("a/b", "a-b"), ("", "")):
            self.assertEqual(cli._pair_folder(p, {"ssd_folder": given})[0], want, given)


if __name__ == "__main__":
    unittest.main()
