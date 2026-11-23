"""Brief drafting: the answer check, one retry, the rule-based fallback, the login check, and the app's endpoints."""
import json, os, subprocess, tempfile, unittest
from unittest import mock

from mashup_finder import app, draft

GOOD = 'name = "gig"\n\n[inst]\ntempo = [140, 180]\nexclude = ["neuro"]\n\n[vocal]\nyears = [1980, 1999]\n\n[match]\ntop = 30\n'


class DraftTest(unittest.TestCase):
    def test_a_fenced_answer_is_taken_and_the_words_kept_as_comments(self):
        r = draft.draft("gig", "140 and 180\nno neuro", ask=lambda s, u: "```toml\n" + GOOD + "```\n")
        self.assertEqual(r["by"], "claude")
        self.assertTrue(r["text"].startswith("# Brief, as written:\n#   140 and 180\n#   no neuro\n"))
        self.assertEqual(draft.check(r["text"], "gig")["inst"]["tempo"], [140, 180])

    def test_a_bad_answer_is_asked_for_once_more_with_the_reason(self):
        asked = []

        def ask(system, user):
            asked.append(user)
            return GOOD if len(asked) == 2 else GOOD.replace("[match]", "[matching]")
        self.assertEqual(draft.draft("gig", "words", ask=ask)["by"], "claude")
        self.assertIn("rejected: unknown top-level keys", asked[1])

    def test_no_claude_gives_the_rule_draft(self):
        def ask(system, user):
            raise FileNotFoundError("the claude CLI is not installed")
        r = draft.draft("gig", "Two tempos, 140 and 180. 80s and 90s vocals. No neuro, no jump up.", ask=ask)
        b = draft.check(r["text"], "gig")
        self.assertEqual((r["by"], b["inst"]["tempo"], b["vocal"]["years"], b["inst"]["exclude"]),
                         ("rules", [140, 180], [1980, 1999], ["neuro", "jump up"]))
        self.assertIn("not installed", r["note"])

    def test_decades_with_apostrophes_are_years_not_tempos(self):
        b = draft.check(draft.by_rules("gig", "bass house matched with 80's and 90' pop hits"), "gig")
        self.assertEqual((b["vocal"]["years"], b["inst"]["tempo"]), ([1980, 1999], [174]))

    def test_decade_years_end_with_the_last_decade_named(self):
        asked = []

        def ask(system, user):                                   # first answer runs "70s and 80s" on to 1999
            asked.append(user)
            return GOOD if len(asked) == 1 else GOOD.replace("[1980, 1999]", "[1970, 1989]")
        r = draft.draft("gig", "140 and 180, 70s and 80s vocals", ask=ask)
        self.assertEqual((r["by"], draft.check(r["text"], "gig")["vocal"]["years"]), ("claude", [1970, 1989]))
        self.assertIn("decades named are years = [1970, 1989]", asked[1])
        self.assertEqual(draft.draft("gig", "80s and 90s, also 2003", ask=lambda s, u: GOOD)["by"], "claude")   # exact years: Claude's call

    def test_two_bad_answers_fall_back_to_rules(self):
        self.assertEqual(draft.draft("gig", "126 bpm", ask=lambda s, u: "sorry, no")["by"], "rules")

    def test_check_refuses_unknown_keys_a_wrong_name_and_missing_playlists(self):
        for bad in (GOOD.replace("exclude", "genre"), GOOD.replace('"gig"', '"other"'),
                    GOOD.replace("tempo = [140, 180]", 'from = ["/no/such/list.m3u8"]'), GOOD.replace("[140, 180]", '"fast"')):
            with self.assertRaises(ValueError):
                draft.check(bad, "gig")

    def test_ssd_folder_is_a_known_key_and_one_folder_name(self):
        self.assertEqual(draft.check('ssd_folder = "Club night 20 Nov"\n' + GOOD, "gig")["ssd_folder"], "Club night 20 Nov")
        for bad in ('"a/b"', '".hidden"', '""', "3"):
            with self.assertRaises(ValueError, msg=bad):
                draft.check(f"ssd_folder = {bad}\n" + GOOD, "gig")

    def test_a_song_brief_is_checked_and_the_drafter_knows_it(self):
        song = 'name = "gig"\n\n[song]\ntext = "a view to a kill"\nrole = "vocal"\n'
        self.assertEqual(draft.check(song, "gig")["song"]["role"], "vocal")           # a song alone is enough
        for bad in ('role = "lead"', 'text = ""', 'text = "x"\nmood = "sad"', 'text = "x"\nrole = ["vocal"]'):
            with self.assertRaises(ValueError, msg=bad):
                draft.check(f'name = "gig"\n[song]\n{bad}\n', "gig")
        self.assertIn("[song]", draft.SYSTEM)
        b = draft.check(draft.by_rules("gig", "Best matches for “A View To A Kill”, 80s vibe"), "gig")   # without Claude too
        self.assertEqual((b["song"], b["match"]["per_inst"]), ({"text": "A View To A Kill", "role": "either"}, 30))
        for words, text in (("best mashup matches for Don't You Want Me by The Human League", "Don't You Want Me"),
                            ("Best partners for Wham! 🎉 Club Tropicana", "Wham! 🎉 Club Tropicana")):
            self.assertEqual(draft.check(draft.by_rules("gig", words), "gig")["song"]["text"], text, words)
        self.assertNotIn("song", draft.check(draft.by_rules("gig", "Mashups for a wedding, 126 bpm, no techno"), "gig"))
        self.assertIn("[song] makes it a brief about one song", draft._reference())

    def test_match_values_are_whole_numbers_and_a_percentage(self):
        for bad in ("per_vocal = 6.0", "per_inst = -1", "top = 0", "limit = true", "max_stretch = 0", "max_stretch = \"8\""):
            with self.assertRaises(ValueError, msg=bad):
                draft.check(GOOD.replace("top = 30", bad), "gig")
        self.assertEqual(draft.check(GOOD.replace("top = 30", "max_stretch = 7.5"), "gig")["match"]["max_stretch"], 7.5)
        with self.assertRaisesRegex(ValueError, "unknown keys in \\[match\\]: \\['min_score'\\]"):   # the name first: Claude is told that
            draft.check(GOOD.replace("top = 30", "min_score = 0.6"), "gig")

    def test_empty_words_are_refused(self):
        with self.assertRaises(ValueError):
            draft.draft("gig", "  ")


class StatusTest(unittest.TestCase):
    """draft.status() asks `claude auth status` in _claude's environment; the CLI itself is stubbed here."""

    def status(self, stdout="", rc=0, exc=None):
        seen = {}

        def run(cmd, **kw):
            seen.update(cmd=cmd, env=kw["env"])
            if exc:
                raise exc
            return subprocess.CompletedProcess(cmd, rc, stdout, "")
        with mock.patch.object(draft, "_cli", lambda: ("/bin/claude", {"PATH": "/bin"})):
            return draft.status(run), seen

    def test_logged_in_logged_out_and_no_answer(self):
        r, seen = self.status(json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))
        self.assertEqual((r, seen["cmd"][1:]), ({"works": True, "why": "logged in (claude.ai)"}, ["auth", "status", "--json"]))
        self.assertFalse(self.status(json.dumps({"loggedIn": False}), rc=1)[0]["works"])
        self.assertIn("did not answer", self.status("Error: something")[0]["why"])
        self.assertIn("did not answer", self.status(exc=subprocess.TimeoutExpired("claude", 30))[0]["why"])

    def test_the_check_uses_drafting_environment_and_says_when_the_cli_is_missing(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-shell", "CLAUDE_CODE_ENTRYPOINT": "x", "HOME_ISH": "kept"}):
            with mock.patch.object(draft.os.path, "exists", lambda p: True):
                seen = {}
                draft.status(lambda cmd, **kw: seen.update(env=kw["env"]) or subprocess.CompletedProcess(cmd, 0, "{}", ""))
        self.assertEqual(seen["env"].get("HOME_ISH"), "kept")
        self.assertFalse([k for k in seen["env"] if k.startswith(("ANTHROPIC_", "CLAUDE"))])
        with mock.patch.object(draft.shutil, "which", lambda name: None), mock.patch.object(draft.os.path, "exists", lambda p: False):
            self.assertEqual(draft.status(lambda *a, **k: self.fail("ran")), {"works": False, "why": "the claude CLI is not installed"})


class AppDraftTest(unittest.TestCase):
    def test_the_login_check_is_kept_until_asked_again(self):
        calls = []
        with tempfile.TemporaryDirectory() as root:
            a = app.App(root=root, ssd=root, claude_check=lambda: calls.append(1) or {"works": False, "why": "not logged in"})
            self.assertEqual((a.claude()["works"], a.claude()["why"]), (False, "not logged in"))
            a.claude()
            self.assertEqual(len(calls), 1)
            a.claude(recheck=True)
            self.assertEqual(len(calls), 2)

    def test_the_app_drafts_without_saving_and_asks_before_replacing(self):
        with tempfile.TemporaryDirectory() as root:
            a = app.App(root=root, ssd=os.path.join(root, "ssd"), drafter=lambda n, w: {"text": GOOD, "by": "claude", "note": "ok"})
            self.assertEqual(a.draft_brief("gig", "words")["text"], GOOD)
            self.assertFalse(os.path.exists(os.path.join(root, "briefs", "gig.toml")))
            a.save_brief("gig", GOOD)
            with self.assertRaises(app.Refused) as e:
                a.draft_brief("gig", "words")
            self.assertEqual(e.exception.status, 409)
            self.assertEqual(a.draft_brief("gig", "words", confirm=True)["by"], "claude")
            with self.assertRaises(ValueError):
                a.draft_brief("../x", "words")


if __name__ == "__main__":
    unittest.main()
