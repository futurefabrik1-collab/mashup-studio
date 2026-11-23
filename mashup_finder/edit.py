"""`mf edit <mashup dir>`: the block editor. A stdlib HTTP server on 127.0.0.1 serves editor.html and a small
API over the folder's arrangement.json, blocks/, mashup.mp3 and clip.mp3.

Safety: it listens on 127.0.0.1 only. Every API call needs the session token that is written into the page,
and the Host header must be this server, so another web page open in the browser (or a DNS-rebinding site)
cannot save or render. Only the lanes of an arrangement can be changed: sources, tempo and the rest stay as
on disk, so a posted arrangement cannot point the server at other files."""
import datetime, http.server, io, json, os, secrets, shutil, threading, webbrowser
from urllib.parse import parse_qs, unquote, urlparse

import soundfile as sf

from . import arrange, render

HOST = "127.0.0.1"
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "editor.html")
MAX_POST = 8 * 1024 * 1024


def backup(path, ext=None):
    """Copy path to <name>.<timestamp><ext> beside it (ext: its own extension) without ever replacing an earlier
    backup, and return the backup's file name."""
    root, own = os.path.splitext(path)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    b, k = f"{root}.{stamp}{ext or own}", 1
    while os.path.exists(b):
        b, k = f"{root}.{stamp}-{k}{ext or own}", k + 1
    shutil.copy2(path, b)
    return os.path.basename(b)


class Editor:
    """One mashup folder being edited. `lock` (one save or render at a time) is shared by every Editor of the
    folder: the app makes one per request."""

    def __init__(self, folder, lock=None):
        self.dir = os.path.abspath(os.path.expanduser(folder))
        self.path = os.path.join(self.dir, "arrangement.json")
        if not os.path.isfile(self.path):
            raise SystemExit(f"{self.dir} has no arrangement.json (make one with `mf mashup`)")
        arr = arrange.load_arrangement(self.path)                # refuse a broken file up front
        # published name -> file, for the arrangement's blocks/ sources only (a save cannot change them)
        self.sources = {os.path.basename(s["file"]): os.path.join(self.dir, s["file"]) for s in arr["sources"].values()}
        self.lock = lock or threading.Lock()

    def load(self):
        return arrange.load_arrangement(self.path)

    def save(self, lanes):
        """Replace the lanes, keep everything else as on disk, back up the previous file. The file gets the
        vocal_gain_db the loader worked out, so the duck reference stays put while block gains change."""
        with self.lock:
            arr = self.load()
            arr["lanes"] = lanes
            arrange.validate(arr, "the edited arrangement")
            name = backup(self.path)
            render.write_json(self.path, arr)
            return name

    def render(self):
        with self.lock:
            lines = []
            r = render.render(self.path, log=lines.append)
            return {"lufs": r["lufs"], "true_peak": r["true_peak"], "loudness_ok": r["loudness_ok"], "log": lines}


class Handler(http.server.BaseHTTPRequestHandler):
    """The editor's routes for one mashup folder. `mf app` (app.py) extends it and serves many folders, each
    under its own prefix: editor.html uses relative URLs, so the same page works at / and at /m/<run>/<mashup>/."""
    server_version = "mf-edit"
    frame_ancestors = "'none'"                                   # the app shows the editor in a frame: 'self' there

    def log_message(self, fmt, *args):                          # quiet; errors reach the page instead
        pass

    def _host_ok(self):
        port = self.server.server_address[1]
        return self.headers.get("Host") in (f"{HOST}:{port}", f"localhost:{port}")

    def _token_ok(self, url, query_ok=False):
        """The X-Token header; `?t=` only where the page cannot set a header (an <audio> element)."""
        t = self.headers.get("X-Token") or (parse_qs(url.query).get("t", [""])[0] if query_ok else "")
        return secrets.compare_digest(t, self.server.token)

    def _send(self, code, body, ctype="application/json", extra=()):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_bytes(self, data, ctype):
        """With byte ranges, which Safari's media elements need."""
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            try:
                start = int(a) if a else max(0, len(data) - int(b))
                end = min(int(b), len(data) - 1) if a and b else len(data) - 1
            except ValueError:
                start, end = 0, -1
            if 0 <= start <= end:
                return self._send(206, data[start:end + 1], ctype, [("Accept-Ranges", "bytes"),
                                  ("Content-Range", f"bytes {start}-{end}/{len(data)}")])
            return self._send(416, b"", ctype, [("Content-Range", f"bytes */{len(data)}")])
        self._send(200, data, ctype, [("Accept-Ranges", "bytes")])

    def _page(self, path):
        with open(path, encoding="utf-8") as f:
            return self._html(f.read())

    def _html(self, page):
        """An HTML page with the session token written in. No other site may frame it."""
        page = page.replace("__TOKEN__", self.server.token)
        return self._send(200, page.encode(), "text/html; charset=utf-8",
                          [("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                            f"media-src 'self' blob:; connect-src 'self'; frame-ancestors {self.frame_ancestors}")])

    def _post_body(self, url):
        """The JSON object posted, or None once an error has been sent (wrong host or token, bad length or body)."""
        if not self._host_ok():
            return self._send(403, {"error": "wrong host"})
        if not self._token_ok(url):
            return self._send(403, {"error": "missing or wrong session token"})
        n = self.headers.get("Content-Length") or "0"
        n = int(n) if n.isdigit() else -1
        if not 0 <= n <= MAX_POST:
            return self._send(413 if n > MAX_POST else 400, {"error": "bad or too large Content-Length"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = None
        return body if isinstance(body, dict) else self._send(400, {"ok": False, "error": "send a JSON object"})

    def editor_get(self, ed, path, url):
        """GET for one mashup folder; `path` is relative to its page ("/", "/api/arrangement" ...)."""
        if path == "/":
            return self._page(PAGE)
        if not self._token_ok(url, query_ok=path in ("/mashup.mp3", "/clip.mp3")):
            return self._send(403, {"error": "missing or wrong session token"})
        try:
            if path == "/api/arrangement":
                return self._send(200, {"arrangement": ed.load(), "folder": os.path.basename(ed.dir),
                                        "has_mix": os.path.exists(os.path.join(ed.dir, "mashup.mp3")),
                                        "lanes": arrange.LANES, "sr": render.SR,
                                        "source_of": {l: {s: arrange.source_name(s, l) for s in "AB"} for l in arrange.LANES}})
            if path.startswith("/blocks/"):
                f = ed.sources.get(path[len("/blocks/"):])
                if not f:
                    return self._send(404, {"error": "not a source of this arrangement"})
                y, sr = sf.read(f, dtype="float32")              # 16-bit for the preview: every browser decodes it
                buf = io.BytesIO()
                sf.write(buf, y.clip(-1, 1), sr, subtype="PCM_16", format="WAV")
                return self._send_bytes(buf.getvalue(), "audio/wav")
            if path in ("/mashup.mp3", "/clip.mp3"):                 # the mix, or its clip (made in `mf app`)
                f = os.path.join(ed.dir, path[1:])
                if not os.path.exists(f):
                    return self._send(404, {"error": f"no {path[1:]} yet"})
                with open(f, "rb") as fh:
                    return self._send_bytes(fh.read(), "audio/mpeg")
        except Exception as e:                                   # a missing or broken blocks/ file: say which
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        self._send(404, {"error": "not found"})

    def editor_post(self, ed, path, body):
        try:
            if path == "/api/save":
                if "lanes" not in body:
                    return self._send(400, {"error": "send {\"lanes\": {...}}"})
                return self._send(200, {"ok": True, "backup": ed.save(body["lanes"])})
            if path == "/api/render":
                return self._send(200, {"ok": True, **ed.render()})
        except (ValueError, OSError) as e:                       # validation errors and bad files go to the page
            return self._send(400, {"ok": False, "error": str(e)})
        except Exception as e:                                   # and so does anything else a render hits (ffmpeg ...)
            return self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
        self._send(404, {"error": "not found"})

    def route_get(self, path, url):                              # the app overrides these two
        self.editor_get(self.server.editor, path, url)

    def route_post(self, path, url, body):
        self.editor_post(self.server.editor, path, body)

    def do_GET(self):
        url = urlparse(self.path)
        if not self._host_ok():
            return self._send(403, {"error": "wrong host"})
        if url.path == "/favicon.ico":                          # browsers ask without the token; no console error
            return self._send(204, b"", "image/x-icon")
        self.route_get(unquote(url.path), url)

    do_HEAD = do_GET

    def do_POST(self):
        url = urlparse(self.path)
        body = self._post_body(url)
        if body is not None:
            self.route_post(unquote(url.path), url, body)


def bind(handler, port=0, host=HOST):
    """A server for handler, not yet serving, with a fresh session token. Only 127.0.0.1: there is no way to
    listen anywhere else."""
    if host != HOST:
        raise ValueError(f"only {HOST}, not {host!r}")
    srv = http.server.ThreadingHTTPServer((HOST, port), handler)
    srv.daemon_threads, srv.token = True, secrets.token_urlsafe(24)
    return srv


def run(srv, what, open_browser=True, log=print, stop=lambda: None):
    """Serve until Ctrl-C (or a shutdown from the page), then stop() and close."""
    url = f"http://{HOST}:{srv.server_address[1]}/"
    log(f"{what}\n  {url}  (Ctrl-C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop()
        srv.server_close()


def make_server(folder, port=0, host=HOST):
    """The editor's server, not yet serving."""
    editor = Editor(folder)                                      # check the folder before opening a socket
    srv = bind(Handler, port, host)
    srv.editor = editor
    return srv


def serve(folder, port=0, open_browser=True, log=print):
    srv = make_server(folder, port)
    run(srv, f"editing {srv.editor.dir}", open_browser, log)
