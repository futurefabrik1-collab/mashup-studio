"""packaging/build-app.sh against a stub manifest: file:// archives of a system program stand in for uv, ffmpeg,
ffprobe and rubberband, so nothing is downloaded and nothing is signed. Checks the app's layout and version, the
checksum refusal, the arm64 and system-libraries rule, a member that would leave the app, and the DMG. Skipped
without the Mac build tools (Xcode's swiftc, lipo, otool; hdiutil), or outside a git checkout."""
import hashlib, os, plistlib, re, shutil, subprocess, tarfile, unittest, zipfile

from tests import fixtures as fx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "packaging", "build-app.sh")
PROGRAM = "/usr/bin/true"                     # a system Mach-O: arm64e (and x86_64), loads only /usr/lib
TOOLS = (all(shutil.which(t) for t in ("lipo", "otool", "hdiutil", "ditto", "xcrun"))
         and subprocess.run(["xcrun", "--find", "swiftc"], capture_output=True).returncode == 0)


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


@unittest.skipUnless(TOOLS and os.path.exists(os.path.join(ROOT, ".git")), "needs Xcode, hdiutil and a git checkout")
class BuildAppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = fx.tmpdir("mf-build-")
        src = os.path.join(cls.tmp, "src")
        os.makedirs(src)
        text = os.path.join(src, "LICENSE.txt")
        with open(text, "w") as f:
            f.write("a licence\n")
        with tarfile.open(os.path.join(src, "uv.tar.gz"), "w:gz") as t:
            t.add(PROGRAM, "uv-aarch64-apple-darwin/uv")
        for name in ("ffmpeg", "ffprobe"):
            with zipfile.ZipFile(os.path.join(src, f"{name}.zip"), "w") as z:
                z.write(PROGRAM, name)
        with tarfile.open(os.path.join(src, "rb.tar.bz2"), "w:bz2") as t:
            t.add(PROGRAM, "rb/rubberband")
            t.add(text, "rb/COPYING")
        url = lambda f: f"file://{os.path.join(src, f)}"
        cls.rows = [("bin/uv", "uv.tar.gz", "uv-aarch64-apple-darwin/uv"), ("bin/ffmpeg", "ffmpeg.zip", "ffmpeg"),
                    ("bin/ffprobe", "ffprobe.zip", "ffprobe"), ("bin/rubberband", "rb.tar.bz2", "rb/rubberband"),
                    ("licenses/rubberband/COPYING", "rb.tar.bz2", "rb/COPYING"), ("licenses/uv/LICENSE-MIT", "LICENSE.txt", "-")]
        cls.rows = [(dest, sha(os.path.join(src, f)), url(f), member) for dest, f, member in cls.rows]
        cls.ok = cls.build(cls, "ok", cls.rows)                       # one real build, shared by the layout tests
        cls.app = os.path.join(cls.tmp, "ok", "dist", "Mashup Studio.app")

    def build(self, name, rows, *args):
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        manifest = os.path.join(d, "third-party.txt")
        with open(manifest, "w") as f:
            f.write("# test manifest\n" + "".join(f"{dest}  {s}  {u}  {m}\n" for dest, s, u, m in rows))
        env = {**os.environ, "MANIFEST": manifest, "BUILD_CACHE": os.path.join(d, "cache"), "DIST": os.path.join(d, "dist")}
        return subprocess.run([BUILD, *args], env=env, capture_output=True, text=True, timeout=600)

    def test_the_app_holds_the_public_code_the_programs_their_licences_and_its_version(self):
        self.assertEqual(self.ok.returncode, 0, self.ok.stderr)
        with open(os.path.join(ROOT, "pyproject.toml")) as f:
            version = re.search(r'^version = "([\d.]+)"$', f.read(), re.M).group(1)
        with open(os.path.join(self.app, "Contents", "Info.plist"), "rb") as f:
            info = plistlib.load(f)
        self.assertEqual((info["CFBundleShortVersionString"], info["CFBundleExecutable"]), (version, "mashup-studio"))
        res = os.path.join(self.app, "Contents", "Resources")
        self.assertEqual(sorted(os.listdir(res)), ["AppIcon.icns", "app", "bin", "licenses"])
        self.assertEqual(sorted(os.listdir(os.path.join(res, "bin"))), ["ffmpeg", "ffprobe", "rubberband", "uv"])
        for p in ["bin/uv", "bin/rubberband"]:                       # thinned to the one arm64 slice
            self.assertEqual(subprocess.run(["lipo", "-archs", os.path.join(res, p)], capture_output=True, text=True).stdout.split(), ["arm64e"])
        exe = os.path.join(self.app, "Contents", "MacOS", "mashup-studio")
        self.assertIn("arm64", subprocess.run(["lipo", "-archs", exe], capture_output=True, text=True).stdout)
        app = sorted(os.listdir(os.path.join(res, "app")))
        self.assertEqual(app, ["LICENSE", "README.md", "briefs", "mashup_finder", "pyproject.toml",
                               "requirements.lock", "templates"])
        self.assertEqual(os.listdir(os.path.join(res, "app", "briefs")), ["90s-mashup.toml"])     # the example only
        self.assertTrue(os.path.isfile(os.path.join(res, "licenses", "SOURCES.md")))
        with open(os.path.join(res, "licenses", "rubberband", "COPYING")) as f:
            self.assertEqual(f.read(), "a licence\n")
        dmg = os.path.join(self.tmp, "ok", "dist", f"Mashup Studio-{version}.dmg")
        self.assertGreater(os.path.getsize(dmg), 0)

    def test_a_download_whose_checksum_differs_is_refused_and_dropped(self):
        rows = [(d, "0" * 64 if d == "bin/ffmpeg" else s, u, m) for d, s, u, m in self.rows]
        r = self.build("bad-sha", rows, "--no-dmg")
        self.assertEqual(r.returncode, 1)
        self.assertIn("sha256 mismatch for file://", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "bad-sha", "cache", "0" * 64)))

    def test_a_program_loading_a_library_from_outside_the_system_is_refused(self):
        d = os.path.join(self.tmp, "brew")
        os.makedirs(d)
        for name, text in (("lib.c", "int f(void) { return 1; }\n"), ("main.c", "int f(void); int main(void) { return f(); }\n")):
            with open(os.path.join(d, name), "w") as fh:
                fh.write(text)
        cc = lambda *a: subprocess.run(["xcrun", "clang", "-arch", "arm64", *a], cwd=d, check=True, capture_output=True)
        cc("-dynamiclib", "lib.c", "-install_name", "/opt/homebrew/lib/libstub.dylib", "-o", "libstub.dylib")
        cc("main.c", "libstub.dylib", "-o", "ffmpeg")
        with zipfile.ZipFile(os.path.join(d, "ffmpeg.zip"), "w") as z:
            z.write(os.path.join(d, "ffmpeg"), "ffmpeg")
        zp = os.path.join(d, "ffmpeg.zip")
        rows = [(dst, sha(zp), f"file://{zp}", "ffmpeg") if dst == "bin/ffmpeg" else (dst, s, u, m) for dst, s, u, m in self.rows]
        r = self.build("brew-build", rows, "--no-dmg")
        self.assertEqual(r.returncode, 1)
        self.assertIn("loads libraries from outside the system: /opt/homebrew/lib/libstub.dylib", r.stderr)

    def test_a_member_that_would_leave_the_app_and_an_unknown_option_are_refused(self):
        rows = [(d, s, u, "../../etc/x" if d == "bin/uv" else m) for d, s, u, m in self.rows]
        r = self.build("escape", rows, "--no-dmg")
        self.assertEqual(r.returncode, 1)
        self.assertIn("leaves the app folder", r.stderr)
        self.assertEqual(self.build("usage", self.rows, "--publish").returncode, 2)


if __name__ == "__main__":
    unittest.main()
