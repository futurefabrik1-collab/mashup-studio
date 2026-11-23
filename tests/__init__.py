"""Tests never read the user's settings: the test run makes an empty settings file (all defaults, the catalog
beside it in a temp folder) before any test module imports mashup_finder, and the processes tests start inherit it.
Tests that need other settings point MF_CONFIG elsewhere themselves."""
import atexit, os, shutil, tempfile

if not os.environ.get("MF_TEST_SETTINGS"):          # the test run itself, not a process a test started
    _d = tempfile.mkdtemp(prefix="mf-tests-settings-")
    atexit.register(shutil.rmtree, _d, True)
    open(os.path.join(_d, "config.toml"), "w").close()
    os.environ["MF_CONFIG"] = os.environ["MF_TEST_SETTINGS"] = os.path.join(_d, "config.toml")
