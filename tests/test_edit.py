"""The editor's server (mf edit): API, safety and the arrangement it writes."""
import http.client, io, json, os, threading, unittest
from unittest import mock

import numpy as np
import soundfile as sf

from mashup_finder import edit, engine
from tests import fixtures as fx


class EditServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sa, ba = fx.a_track(120.0)
        sb, bb = fx.b_track(126.0)
        cls.dir = fx.tmpdir("mf-edit-")
        engine.make(sa, ba, sb, bb, 2, lambda v: cls.dir, "short", log=lambda *_: None)
        cls.srv = edit.make_server(cls.dir)
        cls.port = cls.srv.server_address[1]
        cls.token = cls.srv.token
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def req(self, method, path, body=None, token=True, host=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
        h = {"Host": host or f"127.0.0.1:{self.port}", **(headers or {})}
        if token:
            h["X-Token"] = self.token
        data = None if body is None else json.dumps(body).encode()
        if data is not None:
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        out = (r.status, r.read(), dict(r.getheaders()))
        c.close()
        return out

    @staticmethod
    def write(path, data):
        with open(path, "wb") as f:
            f.write(data)

    def arrangement(self):
        with open(os.path.join(self.dir, "arrangement.json")) as f:
            return json.load(f)

    def test_page_and_load(self):
        code, body, hdr = self.req("GET", "/", token=False)
        self.assertEqual(code, 200)
        self.assertIn(self.token.encode(), body)
        self.assertIn("frame-ancestors 'none'", hdr["Content-Security-Policy"])
        code, body, _ = self.req("GET", "/api/arrangement")
        self.assertEqual(code, 200)
        j = json.loads(body)
        self.assertEqual(j["arrangement"]["lanes"], self.arrangement()["lanes"])
        self.assertTrue(j["has_mix"])
        self.assertEqual((j["lanes"], j["sr"], j["source_of"]["vocal"]["A"]), (["vocal", "drums", "bass", "other"], fx.SR, "A-vocals"))
        self.assertEqual(self.req("GET", "/favicon.ico", token=False)[0], 204)

    def test_token_and_host_are_required(self):
        self.assertEqual(self.req("GET", "/api/arrangement", token=False)[0], 403)
        self.assertEqual(self.req("GET", "/api/arrangement", headers={"X-Token": "x" * 32}, token=False)[0], 403)
        self.assertEqual(self.req("GET", "/", token=False, host=f"evil.example:{self.port}")[0], 403)   # DNS rebinding
        self.assertEqual(self.req("POST", "/api/save", {"lanes": {}}, token=False)[0], 403)
        self.assertEqual(self.req("POST", f"/api/save?t={self.token}", {"lanes": {}}, token=False)[0], 403)  # POST needs the header
        self.assertEqual(self.req("GET", f"/api/arrangement?t={self.token}", token=False)[0], 403)  # ?t= is for the mix only
        self.assertEqual(self.req("GET", f"/blocks/B-drums.wav?t={self.token}", token=False)[0], 403)
        for n in ("-1", "x", str(edit.MAX_POST + 1)):                    # a bad length is refused, not read
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
            c.putrequest("POST", "/api/save", skip_host=True)
            for k, v in (("Host", f"127.0.0.1:{self.port}"), ("X-Token", self.token), ("Content-Length", n)):
                c.putheader(k, v)
            c.endheaders()
            self.assertIn(c.getresponse().status, (400, 413), n)
            c.close()

    def test_blocks_are_served_and_nothing_else(self):
        y, sr = sf.read(os.path.join(self.dir, "blocks", "B-drums.wav"), dtype="float32")
        code, whole, hdr = self.req("GET", "/blocks/B-drums.wav")
        got, got_sr = sf.read(io.BytesIO(whole), dtype="float32")
        self.assertEqual((code, sf.info(io.BytesIO(whole)).subtype, got_sr, got.shape), (200, "PCM_16", sr, y.shape))
        self.assertLess(np.abs(got - y.clip(-1, 1)).max(), 1e-4)        # the same audio, in 16 bits
        code, part, hdr = self.req("GET", "/blocks/B-drums.wav", headers={"Range": "bytes=100-199"})
        self.assertEqual((code, part, hdr["Content-Range"]), (206, whole[100:200], f"bytes 100-199/{len(whole)}"))
        for bad in ("/blocks/../arrangement.json", "/blocks/..%2Farrangement.json", "/blocks/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
                    "/blocks/arrangement.json", "/../../../etc/passwd", "/blocks//etc/passwd", "/render.json"):
            self.assertIn(self.req("GET", bad)[0], (403, 404), bad)
        code, body, hdr = self.req("GET", f"/mashup.mp3?t={self.token}", token=False)
        self.assertEqual((code, hdr["Content-Type"]), (200, "audio/mpeg"))

    def test_save_keeps_a_backup_and_only_changes_lanes(self):
        before = self.arrangement()
        lanes = json.loads(json.dumps(before["lanes"]))
        lanes["drums"][0]["out_bar"] += 4
        evil = {"lanes": lanes, "sources": {"B-drums": {"file": "/etc/passwd"}}, "output_bpm": 999}
        code, body, _ = self.req("POST", "/api/save", evil)
        self.assertEqual(code, 200, body)
        backup = json.loads(body)["backup"]
        with open(os.path.join(self.dir, backup)) as f:
            self.assertEqual(json.load(f), before)
        after = self.arrangement()
        self.assertEqual(after["lanes"]["drums"][0]["out_bar"], before["lanes"]["drums"][0]["out_bar"] + 4)
        self.assertEqual((after["sources"], after["output_bpm"]), (before["sources"], before["output_bpm"]))
        self.assertEqual(self.req("POST", "/api/save", {"lanes": before["lanes"]})[0], 200)   # put it back

    def test_invalid_save_is_refused_and_changes_nothing(self):
        before = self.arrangement()
        for lanes, msg in (({"keys": []}, "unknown lane"), ({**before["lanes"], "drums": [{"lane": "drums", "src": "A", "out_bar": 0, "bars": 4, "src_bar": 0}]}, "no drums source"),
                           ({**before["lanes"], "bass": [{"lane": "bass", "src": "B", "out_bar": -1, "bars": 4, "src_bar": 0}]}, "out_bar"), ("nope", "lanes must")):
            code, body, _ = self.req("POST", "/api/save", {"lanes": lanes})
            self.assertEqual(code, 400)
            self.assertIn(msg, json.loads(body)["error"])
        self.assertEqual(self.arrangement(), before)

    def test_save_pins_the_vocal_gain_of_an_older_arrangement(self):
        path = os.path.join(self.dir, "arrangement.json")
        before = self.arrangement()
        self.addCleanup(self.write, path, json.dumps(before).encode())  # the other tests share this file
        old = {k: v for k, v in before.items() if k != "vocal_gain_db"}   # mashups made before it was recorded
        with open(path, "w") as f:
            json.dump(old, f)
        gains = [b["gain_db"] for b in old["lanes"]["vocal"]]
        lanes = json.loads(json.dumps(old["lanes"]))
        lanes["vocal"][0]["gain_db"] -= 6                               # turning one block down moves no reference
        self.assertEqual(self.req("POST", "/api/save", {"lanes": lanes})[0], 200)
        self.assertEqual(self.arrangement()["vocal_gain_db"], max(set(gains), key=gains.count))

    def test_a_broken_block_file_is_named(self):
        f = os.path.join(self.dir, "blocks", "B-bass.wav")
        with open(f, "rb") as fh:
            good = fh.read()
        self.addCleanup(self.write, f, good)
        self.write(f, b"not a wav")
        code, body, _ = self.req("GET", "/blocks/B-bass.wav")
        self.assertEqual(code, 500)
        self.assertIn("B-bass.wav", json.loads(body)["error"])

    def test_render(self):
        code, body, _ = self.req("POST", "/api/render", {})
        self.assertEqual(code, 200, body)
        j = json.loads(body)
        self.assertTrue(j["ok"] and j["loudness_ok"])
        self.assertLessEqual(abs(j["lufs"] + 10), 1.0)

    def test_a_render_failure_reaches_the_page(self):
        def broken(*a, **k):
            raise RuntimeError("ffmpeg exited with 1")
        with mock.patch.object(edit.render, "render", broken):
            code, body, _ = self.req("POST", "/api/render", {})
        self.assertEqual((code, json.loads(body)["error"]), (500, "RuntimeError: ffmpeg exited with 1"))

    def test_listens_on_127_0_0_1_only(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")
        for host in ("0.0.0.0", "", "localhost", "::", "192.168.1.10"):
            with self.assertRaises(ValueError, msg=host):
                edit.make_server(self.dir, host=host)

    def test_folder_without_an_arrangement_is_refused(self):
        with self.assertRaises(SystemExit):
            edit.make_server(fx.tmpdir("mf-empty-"))


if __name__ == "__main__":
    unittest.main()
