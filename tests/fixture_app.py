"""Mashup Studio on fixtures, for tests/pagecheck.mjs (and by hand): python tests/fixture_app.py [PORT]

Everything lives in a temp folder removed at exit: a brief, one run made by tests/fake_mf.py (the real CLI with the
audio work faked), its first pair kept with a short mashup made, tests/fixtures' small catalog, and stand-ins for the
SSD, the Trash, the drafter and the Claude check. Nothing outside the temp folder is read or written, the real claude
CLI is never run. Prints `ready <url> <run> <mashup>` once it serves."""
import os, sys, tempfile, threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import fixtures as fx                                          # noqa: E402  (first: no user settings)
from mashup_finder import app, catalog, draft                            # noqa: E402

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_mf.py")


def main(port):
    tmp = tempfile.mkdtemp(prefix="mf-pagecheck-")
    root, ssd, trash, lib = (os.path.join(tmp, d) for d in ("root", "ssd", "trash", "lib"))
    for d in (os.path.join(root, "briefs"), ssd, trash, lib):
        os.makedirs(d)
    catalog.CATALOG, catalog.SUPER_MASSIVE = fx.catalog_csv(lib), []
    with open(os.path.join(root, "briefs", "demo.toml"), "w") as f:
        f.write('name = "demo"\n\n[inst]\ntempo = [126]\ntempo_tol = 4\n\n[match]\nmax_stretch = 8\ntop = 8\nper_vocal = 8\n')
    srv = app.make_server(port, root=root, ssd=ssd, trash=trash, mf=[sys.executable, "-u", FAKE, root],
                          drafter=lambda n, w: {"text": draft.by_rules(n, w), "by": "rules", "note": "stand-in drafter"},
                          claude_check=lambda: {"works": True, "why": "stand-in"})
    a = srv.app
    assert a.jobs.wait(a.run_brief("demo"), 120) == "done"
    run = next(r["name"] for r in a.run_list())
    a.mark(run, 0, "keep")
    assert a.jobs.wait(a.make_mashup(run, 0, "short", 1, None), 300) == "done"
    mashup = a.mashups(os.path.join(a.runs, run))[0]["dir"]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"ready http://127.0.0.1:{srv.server_address[1]}/ {run} {mashup}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        import shutil
        shutil.rmtree(tmp, True)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
