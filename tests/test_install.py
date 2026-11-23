"""packaging/install.sh and mf against a temp HOME, with uv, Homebrew and the audio tools stubbed: no network, nothing
installed. The stub uv's venv is a python that runs this test's own interpreter, so `mf --help` really runs."""
import os, plistlib, subprocess, sys, unittest

from tests import fixtures as fx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(ROOT, "packaging", "install.sh")
UV = """#!/bin/bash
echo "uv $*" >> "$STUB_LOG"
if [ "$1" = venv ]; then
  mkdir -p "$4/bin"
  printf '#!/bin/sh\\nexec "%s" "$@"\\n' "$REAL_PY" > "$4/bin/python"
  chmod +x "$4/bin/python"
fi
"""


def stub(folder, name, body='#!/bin/sh\necho "$(basename "$0") $*" >> "$STUB_LOG"\n'):
    p = os.path.join(folder, name)
    with open(p, "w") as f:
        f.write(body)
    os.chmod(p, 0o755)
    return p


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.home, self.bin = fx.tmpdir("mf-install-home-"), fx.tmpdir("mf-install-bin-")
        self.log = os.path.join(self.bin, "log")
        stub(self.bin, "uv", UV)
        self.support = os.path.join(self.home, "Library", "Application Support", "Mashup Studio")
        self.app = os.path.join(self.home, "Applications", "Mashup Studio.app")

    def run_install(self, tools=("ffmpeg", "ffprobe", "rubberband"), brew=False):
        for t in tools + (("brew",) if brew else ()):
            stub(self.bin, t)
        env = {"HOME": self.home, "PATH": f"{self.bin}:/usr/bin:/bin", "UV": os.path.join(self.bin, "uv"),
               "STUB_LOG": self.log, "REAL_PY": sys.executable}
        return subprocess.run(["/bin/bash", INSTALL], env=env, capture_output=True, text=True, timeout=120)

    def logged(self):
        with open(self.log) as f:
            return f.read().splitlines()

    def test_install_makes_the_venv_and_the_app(self):
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Installed.", r.stdout)                            # after `mf --help` ran on the venv's python
        venv = os.path.join(self.support, "venv")
        self.assertEqual(self.logged(), [f"uv venv --python 3.12 {venv}", f"uv pip install --python {venv}/bin/python -e {ROOT}"])
        with open(os.path.join(self.support, "mf-path")) as f:
            self.assertEqual(f.read(), os.path.join(ROOT, "mf") + "\n")
        with open(os.path.join(self.app, "Contents", "Info.plist"), "rb") as f:
            self.assertEqual(plistlib.load(f)["CFBundleExecutable"], "mashup-studio")
        for f in ("Contents/MacOS/mashup-studio", "Contents/Info.plist"):
            with open(os.path.join(self.app, f)) as fh:
                text = fh.read()
            self.assertNotIn(self.home, text)                            # no user path in the app: $HOME at run time
            self.assertNotIn("/Users/", text)
        self.assertTrue(os.access(os.path.join(self.app, "Contents", "MacOS", "mashup-studio"), os.X_OK))
        self.assertTrue(os.path.isfile(os.path.join(self.app, "Contents", "Resources", "AppIcon.icns")))

        # The .command the app opens runs `mf app` from the path install.sh wrote.
        echo = stub(self.bin, "echo-mf", '#!/bin/sh\necho "mf $*"\n')
        with open(os.path.join(self.support, "mf-path"), "w") as f:
            f.write(echo + "\n")
        r = subprocess.run(["/bin/bash", os.path.join(self.support, "Mashup Studio.command")], capture_output=True, text=True)
        self.assertEqual(r.stdout, "mf app\n")

        os.remove(self.log)                                              # again: the venv stays, the package updates
        self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual(self.logged(), [f"uv pip install --python {venv}/bin/python -e {ROOT}"])

    def test_missing_audio_tools(self):
        r = self.run_install(tools=("ffprobe",))
        self.assertEqual(r.returncode, 1)
        self.assertIn("brew install ffmpeg rubberband", r.stderr)
        self.assertFalse(os.path.exists(self.support) or os.path.exists(self.app))
        r = self.run_install(tools=("ffmpeg", "ffprobe"), brew=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.logged()[0], "brew install rubberband")

    def test_mf_says_where_its_python_is_missing(self):
        env = {"HOME": self.home, "PATH": "/usr/bin:/bin"}
        for extra in ({}, {"MF_PYTHON": os.path.join(self.home, "nope", "python")}):
            r = subprocess.run([os.path.join(ROOT, "mf"), "--help"], env={**env, **extra}, capture_output=True, text=True)
            self.assertEqual(r.returncode, 1)
            self.assertIn("run packaging/install.sh first", r.stderr)
        r = subprocess.run([os.path.join(ROOT, "mf"), "scan", "--help"], env={**env, "MF_PYTHON": sys.executable},
                           capture_output=True, text=True, cwd=self.home)                  # from any folder
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--catalog", r.stdout)


if __name__ == "__main__":
    unittest.main()
